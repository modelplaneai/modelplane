"""llm-d multi-pod backend: LeaderWorkerSet + Service + HTTPRoute.

Selected only for multi-node replicas (pipeline > 1), so this always renders a
LeaderWorkerSet whose gang size is the per-worker node count.

Routing is plain Gateway API — `HTTPRoute -> Service`, exactly like native.py —
NOT a GAIE `InferencePool`. The control-plane gateway is Traefik (which is not
GAIE-conformant: it can't consume an `InferencePool` backendRef or call an
ext-proc endpoint-picker), so the llm-d path routes to a Service that selects
the LWS *leader* pods (only the leader serves the OpenAI API; workers just join
the gang). Inference-aware endpoint picking (KV-/load-aware) is tracked
separately in issue #8 as a Traefik-compatible in-path picker.

Multi-node bootstrap: the LWS leader and worker run different commands (no
`LWS_WORKER_INDEX` branch). The leader starts the Ray head then execs the
engine; workers join the leader's Ray cluster and block. This mirrors the
upstream LWS/vLLM/KServe convention. `LWS_LEADER_ADDRESS` / `LWS_WORKER_INDEX` /
`LWS_GROUP_SIZE` (injected by LWS into every pod) are the documented public
contract a custom bootstrap is written against.

Non-vLLM engines (FOLLOW-UP): the escape hatch — a user-supplied container
`command` that bypasses this injection and owns coordination against the
`LWS_*` contract (e.g. SGLang's `--nnodes/--node-rank/--dist-init-addr`, which
is symmetric across pods) — needs `command` added to the curated Container in
the ModelReplica CRD first; it is NOT in the v0.1 schema (only args/env/image/
name). Until then vLLM/Ray is the only multi-node bootstrap.

Weight loading mirrors native: the engine's --model arg is passed through
unmodified (no hf:// rewrite), so the engine fetches from its source at startup
using credentials from engine.env.
"""

from crossplane.function import resource
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
        deployment_name: str,
    ) -> dict[str, k8sobjv1alpha1.Object]:
        engine = base.engine_container(replica)
        pc = cluster.status.providerConfigRef.name
        name = resource.child_name(deployment_name)

        tensor = int(replica.spec.workers.topology.tensor)
        # nodes_per_worker == pipeline: the LWS gang size (leader + workers).
        size = base.nodes_per_worker(replica)
        pipeline = int(replica.spec.workers.topology.pipeline or 1)

        # v0.1 always injects the vLLM/Ray bootstrap. The args are folded into the
        # leader command (consumed as "$@"); the worker only joins the gang.
        # TODO(#65 follow-up): once `command` is added to the curated Container,
        # bypass injection when set so non-vLLM engines (SGLang etc.) can run
        # their own symmetric command on both templates against the LWS_* contract.
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
                                    "value": f"/{replica.metadata.namespace}/{deployment_name}/",
                                }
                            }
                        ],
                        # Strip the /<ns>/<deployment>/ routing prefix so the engine
                        # (which serves /v1/...) sees the path it expects.
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
