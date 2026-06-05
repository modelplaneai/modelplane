"""llm-d multi-pod backend: LeaderWorkerSet + Service + HTTPRoute.

Selected only for multi-node replicas (pipeline > 1), so this always renders a
LeaderWorkerSet whose gang size is the per-worker node count.

Routing is plain Gateway API — `HTTPRoute -> Service`, exactly like native.py —
NOT a GAIE `InferencePool`. The HTTPRoute attaches to the *workload* cluster's
inference gateway (Envoy Gateway, named `inference-gateway`, installed by
ServingStack) and the Service selects the LWS *leader* pods (only the leader
serves the OpenAI API; workers just join the gang).

Why a Service, not a GAIE `InferencePool`: v0.1 does no KV-/load-aware endpoint
picking, so the `InferencePool` + EPP this path originally emitted aren't needed
yet. Reintroducing them is a *workload-gateway* concern — it needs a
GAIE-conformant workload gateway (Envoy Gateway's `InferencePool` v1 support is
unconfirmed; alternatively switch the workload gateway to Istio/agentgateway).
That is independent of the control-plane gateway (Traefik, named `modelplane`),
which never sees these resources. (Issue #8 — inference-aware routing *across
replicas* on the control plane — is a separate problem at that layer.)

Multi-node bootstrap: the LWS leader and worker run different commands (no
`LWS_WORKER_INDEX` branch). The leader starts the Ray head then execs the
engine; workers join the leader's Ray cluster and block. This mirrors the
upstream LWS/vLLM/KServe convention. `LWS_LEADER_ADDRESS` / `LWS_WORKER_INDEX` /
`LWS_GROUP_SIZE` (injected by LWS into every pod) are the documented public
contract a custom bootstrap is written against.

Non-vLLM engines: if the engine container sets its own `command`, we inject no
bootstrap — that command runs verbatim on both templates and owns cross-node
coordination against the `LWS_*` contract (e.g. SGLang's symmetric
`--nnodes/--node-rank/--dist-init-addr`). vLLM/Ray is the turnkey default used
when no `command` is set.

Weight loading mirrors native: the engine's --model arg is passed through
unmodified (no hf:// rewrite), so the engine fetches from its source at startup
using credentials from engine.env.
"""

from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

from function.backends import base

# Namespace for serving workloads on remote clusters.
_REMOTE_NAMESPACE = "default"

# Port the engine serves the OpenAI-compatible API on.
_ENGINE_PORT = 8000

# Label joining the LWS pods and the Service selector (mirrors native.py).
_LABEL_SERVING = "modelplane.ai/serving"

# Label set only on the LWS leader pod. The Service selects on it so traffic
# reaches the gang leader (the only pod that serves the OpenAI API for vLLM
# multi-node; for symmetric engines like SGLang the API server also runs on
# rank 0).
_LABEL_ROLE = "modelplane.ai/lws-role"

# Default vLLM multi-node bootstrap, split across the leader and worker
# templates. `ray start --head` daemonizes and returns, so the engine becomes
# the container's foreground process; `--block` keeps the worker alive for the
# pod's lifetime. Without this, vLLM's pipeline-parallel placement group sees
# only the local node and waits forever.
_LEADER_BOOTSTRAP = 'set -e\nray start --head --port=6379\nexec python3 -m vllm.entrypoints.openai.api_server "$@"'
_WORKER_BOOTSTRAP = 'exec ray start --address="$LWS_LEADER_ADDRESS:6379" --block'


def _object(provider_config: str, manifest: dict) -> k8sobjv1alpha1.Object:
    return k8sobjv1alpha1.Object(
        spec=k8sobjv1alpha1.Spec(
            providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                kind="ClusterProviderConfig",
                name=provider_config,
            ),
            readiness=k8sobjv1alpha1.Readiness(policy="DeriveFromObject"),
            forProvider=k8sobjv1alpha1.ForProvider(manifest=manifest),
        ),
    )


def _engine_args(engine, tensor: int, pipeline: int) -> list[str]:
    """vLLM engine args with parallelism flags injected (only if not already set).

    --model is passed through unmodified (no hf:// rewrite).
    """
    args = list(engine.args or [])
    if not any(a.startswith("--tensor-parallel-size") for a in args):
        args.append(f"--tensor-parallel-size={tensor}")
    if pipeline > 1 and not any(a.startswith("--pipeline-parallel-size") for a in args):
        args.append(f"--pipeline-parallel-size={pipeline}")
    return args


class LLMDBackend:
    def build(
        self,
        replica: v1alpha1.ModelReplica,
        cluster: icv1alpha1.InferenceCluster,
    ) -> dict[str, k8sobjv1alpha1.Object]:
        engine = base.engine_container(replica)
        pc = cluster.status.providerConfigRef.name
        # Name resources after the replica (unique per placement) so multiple
        # replicas of one deployment can co-exist on the same InferenceCluster.
        name = replica.metadata.name

        tensor = int(replica.spec.workers.topology.tensor)
        # nodes_per_worker == pipeline: the LWS gang size (leader + workers).
        size = base.nodes_per_worker(replica)
        pipeline = int(replica.spec.workers.topology.pipeline or 1)

        # A user-supplied command owns cross-node coordination: inject neither the
        # Ray bootstrap nor vLLM-specific parallelism flags. It runs verbatim on
        # both templates (e.g. SGLang's symmetric launch against the LWS_* env).
        user_command = list(engine.command) if engine.command else None
        if user_command:
            leader_command = worker_command = user_command
            args = list(engine.args or [])
        else:
            # Args are folded into the leader command (consumed as "$@"); the
            # worker only joins the gang.
            args = _engine_args(engine, tensor, pipeline)
            leader_command = ["/bin/sh", "-c", _LEADER_BOOTSTRAP, "vllm", *args]
            worker_command = ["/bin/sh", "-c", _WORKER_BOOTSTRAP]

        pull_secrets = None
        tmpl = replica.spec.workers.template
        if tmpl.spec.imagePullSecrets:
            pull_secrets = [s.model_dump(exclude_none=True) for s in tmpl.spec.imagePullSecrets]
        env = [e.model_dump(exclude_none=True) for e in engine.env] if engine.env else None

        def container(command: list[str], *, serving: bool) -> dict:
            c = {
                "name": "engine",
                "image": engine.image,
                # GPUs PER POD (one tensor-parallel shard runs per pod in the gang).
                "resources": {"limits": {"nvidia.com/gpu": str(tensor)}},
                # vLLM tensor parallelism needs a large /dev/shm.
                "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                "command": command,
            }
            # A user command takes args the normal way; an injected bootstrap
            # folds args into the command itself.
            if user_command and args:
                c["args"] = args
            if env:
                c["env"] = env
            if serving:
                c["ports"] = [{"containerPort": _ENGINE_PORT}]
                c["readinessProbe"] = {
                    "httpGet": {"path": "/health", "port": _ENGINE_PORT},
                    "initialDelaySeconds": 30,
                    "periodSeconds": 10,
                }
            return c

        def pod_spec(c: dict) -> dict:
            spec = {"containers": [c], "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}]}
            if pull_secrets:
                spec["imagePullSecrets"] = pull_secrets
            return spec

        # Only the leader serves the OpenAI API → it carries the role label the
        # Service selects on, plus the serving port and readiness probe.
        leader_pod = {
            "metadata": {"labels": {_LABEL_SERVING: name, _LABEL_ROLE: "leader"}},
            "spec": pod_spec(container(leader_command, serving=True)),
        }
        worker_pod = {
            "metadata": {"labels": {_LABEL_SERVING: name}},
            "spec": pod_spec(container(worker_command, serving=False)),
        }

        # LeaderWorkerSet: spec.replicas gangs, each of `size` pods (leader+workers).
        leader_worker_set = {
            "apiVersion": "leaderworkerset.x-k8s.io/v1",
            "kind": "LeaderWorkerSet",
            "metadata": {"name": name, "namespace": _REMOTE_NAMESPACE},
            "spec": {
                "replicas": int(replica.spec.workers.count or 1),
                "leaderWorkerTemplate": {
                    "size": size,
                    "leaderTemplate": leader_pod,
                    "workerTemplate": worker_pod,
                },
            },
        }

        # Service selects the leader pods of every gang in this replica.
        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "namespace": _REMOTE_NAMESPACE},
            "spec": {
                "selector": {_LABEL_SERVING: name, _LABEL_ROLE: "leader"},
                "ports": [{"port": 80, "targetPort": _ENGINE_PORT}],
            },
        }

        # HTTPRoute -> Service (plain Gateway API; Traefik- and Envoy-compatible).
        http_route = {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "HTTPRoute",
            "metadata": {"name": name, "namespace": _REMOTE_NAMESPACE},
            "spec": {
                "parentRefs": [{"name": "inference-gateway", "namespace": "modelplane-system"}],
                "rules": [
                    {
                        "matches": [
                            {
                                "path": {
                                    "type": "PathPrefix",
                                    "value": f"/{replica.metadata.namespace}/{name}/",
                                }
                            }
                        ],
                        # The control plane rewrites the public /<ns>/<service>/
                        # prefix to this replica's /<ns>/<replica>/ (per-IC
                        # addressing); strip it here so the engine sees /v1/...
                        "filters": [
                            {
                                "type": "URLRewrite",
                                "urlRewrite": {"path": {"type": "ReplacePrefixMatch", "replacePrefixMatch": "/"}},
                            }
                        ],
                        "backendRefs": [{"name": name, "port": 80}],
                    }
                ],
            },
        }

        return {
            "model-serving": _object(pc, leader_worker_set),
            "model-service": _object(pc, service),
            "model-route": _object(pc, http_route),
        }
