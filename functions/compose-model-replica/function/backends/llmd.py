"""llm-d multi-pod backend: LeaderWorkerSet + Service + HTTPRoute.

Selected for replicas that need cross-pod coordination: multi-node replicas
(pipeline > 1) and disaggregated replicas (a prefill block, even at pipeline 1).
Renders a LeaderWorkerSet whose gang size is the per-worker node count; a
disaggregated replica additionally emits a separate internal prefill pod set
(see _prefill_objects).

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


# vLLM NixlConnector config enabling disaggregated KV transfer. NixlConnector does
# not distinguish kv_role (the routing sidecar drives the prefill->decode direction
# at request time), so both roles run kv_both; see the vLLM NixlConnector usage
# guide. Without this the engines run as plain servers and no KV handoff occurs.
_NIXL_KV_TRANSFER_CONFIG = '{"kv_connector":"NixlConnector","kv_role":"kv_both"}'


def _with_nixl_kv_transfer(args: list[str]) -> list[str]:
    """Append the NixlConnector --kv-transfer-config unless the user already set one."""
    if any(a.startswith("--kv-transfer-config") for a in args):
        return args
    return [*args, f"--kv-transfer-config={_NIXL_KV_TRANSFER_CONFIG}"]


def _build_commands(
    engine,
    tensor: int,
    pipeline: int,
    replica,
    *,
    disagg: bool,
) -> tuple[list[str] | None, list[str], list[str], list[str]]:
    """Compute (user_command, leader_command, worker_command, args) for the decode LWS.

    user_command is None on the turnkey vLLM path (the caller uses it to decide
    whether to emit a separate container args field); leader_command and
    worker_command are the final commands for the two pod templates; args is the
    vLLM arg list the caller sets on the container.
    """
    user_cmd = list(engine.command) if engine.command else None
    if user_cmd:
        return user_cmd, user_cmd, user_cmd, list(engine.args or [])
    args = _engine_args(engine, tensor, pipeline)
    args = base.apply_cache_args(args, replica, engine)
    if disagg and not any(a.startswith("--port=") for a in args):
        args = [*args, f"--port={base._DECODE_ENGINE_PORT}"]
    if disagg:
        args = _with_nixl_kv_transfer(args)
    leader_cmd = ["/bin/sh", "-c", _LEADER_BOOTSTRAP, "vllm", *args]
    worker_cmd = ["/bin/sh", "-c", _WORKER_BOOTSTRAP]
    return None, leader_cmd, worker_cmd, args


def _inference_pool_object(
    name: str,
    provider_config: str,
) -> k8sobjv1alpha1.Object:
    """Build a GAIE InferencePool for a disaggregated decode path.

    The pool selects both decode and prefill pods via the shared llm-d.ai labels
    (both roles carry app:<name> + llm-d.ai/inference-serving:"true").  The EPP
    partitions them by llm-d.ai/role and is referenced via endpointPickerRef.
    The HTTPRoute for the disagg path points at this pool instead of the Service,
    so the GAIE EPP can apply KV-aware routing.
    """
    manifest = {
        "apiVersion": "inference.networking.k8s.io/v1",
        "kind": "InferencePool",
        "metadata": {"name": f"{name}-pool", "namespace": base.REMOTE_NAMESPACE},
        "spec": {
            "selector": {
                "matchLabels": {
                    "app": name,
                    base.LABEL_LLMD_SERVING: "true",
                },
            },
            "targetPorts": [{"number": base.ENGINE_PORT}],
            "endpointPickerRef": {
                "name": f"{name}-epp",
                "port": {"number": 9002},
            },
            "failureMode": "FailOpen",
        },
    }
    return base.wrap_object(provider_config, manifest)


def _prefill_objects(
    replica: v1alpha1.ModelReplica,
    prefill_spec,
    name: str,
    provider_config: str,
    cache_volumes: list[dict],
    cache_volume_mounts: list[dict],
) -> dict[str, k8sobjv1alpha1.Object]:
    """Build the prefill pod set + ResourceClaimTemplate for a disaggregated replica.

    Returns the response entries for the internal prefill role: a LeaderWorkerSet
    (no Service/HTTPRoute — prefill is not an API entrypoint) and a per-role
    ResourceClaimTemplate. Pods carry pd-role:prefill, mount the model cache like
    decode, and get the NIXL side-channel env.
    """
    prefill_name = f"{name}-prefill"
    prefill_claim = base.claim_template_name_for(replica, "prefill")
    p_engine = next(c for c in prefill_spec.workers.template.spec.containers if c.name == "engine")
    p_tensor = int(prefill_spec.workers.topology.tensor)
    p_pipeline = int(prefill_spec.workers.topology.pipeline or 1)
    p_size = p_pipeline

    p_user_cmd = list(p_engine.command) if p_engine.command else None
    if p_user_cmd:
        p_leader_cmd = p_worker_cmd = p_user_cmd
        p_args = list(p_engine.args or [])
    else:
        p_args = _engine_args(p_engine, p_tensor, p_pipeline)
        p_args = base.apply_cache_args(p_args, replica, p_engine)
        p_args = _with_nixl_kv_transfer(p_args)
        p_leader_cmd = ["/bin/sh", "-c", _LEADER_BOOTSTRAP, "vllm", *p_args]
        p_worker_cmd = ["/bin/sh", "-c", _WORKER_BOOTSTRAP]

    p_env = [e.model_dump(exclude_none=True) for e in p_engine.env] if p_engine.env else None
    # Prefill pods also need the NIXL side-channel env (same reason as decode).
    p_env = (p_env or []) + [base.nixl_side_channel_env()]
    p_tmpl = prefill_spec.workers.template
    p_pull_secrets = (
        [s.model_dump(exclude_none=True) for s in p_tmpl.spec.imagePullSecrets]
        if p_tmpl.spec.imagePullSecrets
        else None
    )

    def p_container(command: list[str]) -> dict:
        c = {
            "name": "engine",
            "image": p_engine.image,
            "resources": base.engine_resources(),
            # Mounts the model cache like decode: prefill loads the same
            # weights to compute the KV it transfers to decode.
            "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}, *cache_volume_mounts],
            "command": command,
        }
        if p_user_cmd and p_args:
            c["args"] = p_args
        if p_env:
            c["env"] = p_env
        return c

    def p_pod_spec(c: dict) -> dict:
        spec = {
            "containers": [c],
            "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}, *cache_volumes],
        }
        base.place_pod_on(spec, prefill_spec.nodePoolName, prefill_claim)
        if p_pull_secrets:
            spec["imagePullSecrets"] = p_pull_secrets
        return spec

    prefill_labels = {
        base.LABEL_SERVING: prefill_name,
        base.LABEL_PD_ROLE: "prefill",
        base.LABEL_LLMD_ROLE: "prefill",
        base.LABEL_LLMD_SERVING: "true",
        "app": name,
    }
    p_leader_pod = {
        "metadata": {"labels": {**prefill_labels, _LABEL_ROLE: "leader"}},
        "spec": p_pod_spec(p_container(p_leader_cmd)),
    }
    p_worker_pod = {
        "metadata": {"labels": prefill_labels},
        "spec": p_pod_spec(p_container(p_worker_cmd)),
    }

    prefill_lws = {
        "apiVersion": "leaderworkerset.x-k8s.io/v1",
        "kind": "LeaderWorkerSet",
        "metadata": {"name": prefill_name, "namespace": base.REMOTE_NAMESPACE},
        "spec": {
            "replicas": int(prefill_spec.workers.count or 1),
            "leaderWorkerTemplate": {
                "size": p_size,
                "leaderTemplate": p_leader_pod,
                "workerTemplate": p_worker_pod,
            },
        },
    }

    return {
        "prefill-serving": base.wrap_object(provider_config, prefill_lws, cel_query=base.AVAILABLE_CEL),
        "prefill-resource-claim": base.resource_claim_template_for(
            replica, provider_config, "prefill", prefill_spec.deviceRequests
        ),
    }


# The EndpointPickerConfig YAML content for the disaggregated profile.
# This is the authoritative upstream config from deploy/config/pd-epp-config.yaml
# (llm-d/llm-d-inference-scheduler), using approx-prefix-cache-producer,
# prefix-based-pd-decider, disagg-profile-handler, prefill/decode profiles.
_PD_EPP_CONFIG_YAML = """\
apiVersion: inference.networking.x-k8s.io/v1alpha1
kind: EndpointPickerConfig
plugins:
- type: approx-prefix-cache-producer
  parameters:
    maxPrefixBlocksToMatch: 256
    lruCapacityPerServer: 31250
- type: prefix-cache-scorer
- type: queue-scorer
- type: prefill-filter
- type: decode-filter
- type: max-score-picker
- type: prefix-based-pd-decider
  parameters:
    nonCachedTokens: 16
- type: disagg-profile-handler
  parameters:
    deciders:
      prefill: prefix-based-pd-decider
schedulingProfiles:
- name: prefill
  plugins:
  - pluginRef: prefill-filter
  - pluginRef: max-score-picker
  - pluginRef: prefix-cache-scorer
    weight: 2
  - pluginRef: queue-scorer
    weight: 1
- name: decode
  plugins:
  - pluginRef: decode-filter
  - pluginRef: max-score-picker
  - pluginRef: prefix-cache-scorer
    weight: 2
  - pluginRef: queue-scorer
    weight: 1
"""

# Default EPP image when routing.template does not supply one.
_EPP_DEFAULT_IMAGE = "ghcr.io/llm-d/llm-d-inference-scheduler:v0.8.0"

# Name suffix for the EPP Role/RoleBinding (combines pod-watch + inference CRD watch).
_EPP_ROLE_SUFFIX = "epp-sa"

# ClusterRole/ClusterRoleBinding name suffix for metrics auth reviewer.
_EPP_AUTH_SUFFIX = "epp-auth-reviewer"


def _route_backend_refs(name: str, *, disagg: bool) -> list[dict]:
    """Return the HTTPRoute backendRefs for disaggregated (InferencePool) or unified (Service) paths."""
    if disagg:
        return [{"group": "inference.networking.k8s.io", "kind": "InferencePool", "name": f"{name}-pool"}]
    return [{"name": name, "port": 80}]


def _epp_container_from_routing(replica) -> dict | None:
    """Return the "epp" container from spec.routing.template, or None.

    routing.template.spec is dict[str, Any]; iterate containers to find name==epp.
    None means routing is absent or has no epp container, so the caller falls back
    to _EPP_DEFAULT_IMAGE and injects all args itself.
    """
    routing = getattr(replica.spec, "routing", None)
    if routing is None:
        return None
    tmpl = getattr(routing, "template", None)
    if tmpl is None:
        return None
    spec = getattr(tmpl, "spec", None) or {}
    for c in spec.get("containers", []):
        if c.get("name") == "epp":
            return c
    return None


def _epp_objects(
    replica,
    name: str,
    provider_config: str,
) -> dict[str, k8sobjv1alpha1.Object]:
    """Build all EPP-related Objects for a disaggregated replica.

    Emits 8 provider-kubernetes Objects (keyed as epp-serviceaccount, epp-role,
    epp-rolebinding, epp-clusterrole, epp-clusterrolebinding, epp-config, epp,
    epp-service). All namespace-scoped objects go into base.REMOTE_NAMESPACE.
    ClusterRole/ClusterRoleBinding are cluster-scoped (no namespace in metadata).

    The EPP container image comes from replica.spec.routing.template (the epp
    container in that list). Modelplane injects the required operational args;
    any user-supplied args in the template are prepended before the injected set.
    """
    ns = base.REMOTE_NAMESPACE
    epp_name = f"{name}-epp"
    role_name = f"{name}-{_EPP_ROLE_SUFFIX}"
    auth_name = f"{name}-{_EPP_AUTH_SUFFIX}"
    # The epp container (if any) supplies both the image and any user args.
    epp = _epp_container_from_routing(replica)
    image = epp.get("image", _EPP_DEFAULT_IMAGE) if epp else _EPP_DEFAULT_IMAGE
    user_epp_args = list(epp.get("args") or []) if epp else []

    # Modelplane-injected args (appended after any user args).
    injected_args = [
        f"--pool-name={name}-pool",
        f"--pool-namespace={ns}",
        "--pool-group=inference.networking.k8s.io",
        "--zap-encoder=json",
        "--config-file=/config/pd-epp-config.yaml",
        "--grpc-port=9002",
    ]
    epp_args = user_epp_args + injected_args

    sa = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": epp_name, "namespace": ns},
    }

    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": role_name, "namespace": ns},
        "rules": [
            {
                "apiGroups": [""],
                "resources": ["pods"],
                "verbs": ["get", "watch", "list"],
            },
            {
                "apiGroups": ["inference.networking.x-k8s.io"],
                "resources": ["inferenceobjectives", "inferencemodelrewrites"],
                "verbs": ["get", "watch", "list"],
            },
            {
                "apiGroups": ["llm-d.ai"],
                "resources": ["inferenceobjectives", "inferencemodelrewrites"],
                "verbs": ["get", "watch", "list"],
            },
            {
                "apiGroups": ["inference.networking.k8s.io"],
                "resources": ["inferencepools"],
                "verbs": ["get", "watch", "list"],
            },
        ],
    }

    rolebinding = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": role_name, "namespace": ns},
        "subjects": [
            {"kind": "ServiceAccount", "name": epp_name, "namespace": ns},
        ],
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": role_name,
        },
    }

    clusterrole = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRole",
        "metadata": {"name": auth_name},
        "rules": [
            {
                "apiGroups": ["authentication.k8s.io"],
                "resources": ["tokenreviews"],
                "verbs": ["create"],
            },
            {
                "apiGroups": ["authorization.k8s.io"],
                "resources": ["subjectaccessreviews"],
                "verbs": ["create"],
            },
        ],
    }

    clusterrolebinding = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {"name": auth_name},
        "subjects": [
            {"kind": "ServiceAccount", "name": epp_name, "namespace": ns},
        ],
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": auth_name,
        },
    }

    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": epp_name, "namespace": ns},
        "data": {"pd-epp-config.yaml": _PD_EPP_CONFIG_YAML},
    }

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": epp_name, "namespace": ns},
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {"app": epp_name}},
            "template": {
                "metadata": {"labels": {"app": epp_name}},
                "spec": {
                    "serviceAccountName": epp_name,
                    "terminationGracePeriodSeconds": 130,
                    "containers": [
                        {
                            "name": "epp",
                            "image": image,
                            "args": epp_args,
                            "env": [
                                {
                                    "name": "NAMESPACE",
                                    "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
                                },
                                {
                                    "name": "POD_NAME",
                                    "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}},
                                },
                            ],
                            "ports": [
                                {"name": "grpc", "containerPort": 9002},
                                {"name": "grpc-health", "containerPort": 9003},
                                {"name": "metrics", "containerPort": 9090},
                            ],
                            "livenessProbe": {
                                "grpc": {"port": 9003, "service": "inference-extension"},
                                "initialDelaySeconds": 5,
                                "periodSeconds": 10,
                            },
                            "readinessProbe": {
                                "grpc": {"port": 9003, "service": "inference-extension"},
                                "initialDelaySeconds": 5,
                                "periodSeconds": 10,
                            },
                            "volumeMounts": [
                                {"name": "plugins-config-volume", "mountPath": "/config"},
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "plugins-config-volume",
                            "configMap": {"name": epp_name},
                        }
                    ],
                },
            },
        },
    }

    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": epp_name, "namespace": ns},
        "spec": {
            "selector": {"app": epp_name},
            "type": "ClusterIP",
            "ports": [
                {
                    "name": "grpc-ext-proc",
                    "protocol": "TCP",
                    "port": 9002,
                    "targetPort": 9002,
                    "appProtocol": "http2",
                },
                {
                    "name": "http-metrics",
                    "protocol": "TCP",
                    "port": 9090,
                },
            ],
        },
    }

    return {
        "epp-serviceaccount": base.wrap_object(provider_config, sa),
        "epp-role": base.wrap_object(provider_config, role),
        "epp-rolebinding": base.wrap_object(provider_config, rolebinding),
        "epp-clusterrole": base.wrap_object(provider_config, clusterrole),
        "epp-clusterrolebinding": base.wrap_object(provider_config, clusterrolebinding),
        "epp-config": base.wrap_object(provider_config, configmap),
        "epp": base.wrap_object(provider_config, deployment),
        "epp-service": base.wrap_object(provider_config, service),
    }


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
        pipeline = int(replica.spec.workers.topology.pipeline or 1)

        cache_volumes, cache_volume_mounts = base.cache_mounts(replica)

        pull_secrets = (
            [s.model_dump(exclude_none=True) for s in replica.spec.workers.template.spec.imagePullSecrets]
            if replica.spec.workers.template.spec.imagePullSecrets
            else None
        )
        env = [e.model_dump(exclude_none=True) for e in engine.env] if engine.env else None

        # Disaggregated (prefill set): decode pods carry {pd-role: decode}.
        # Unified (no prefill): no pd-role label on decode pods (backward compat).
        prefill_spec = getattr(replica.spec, "prefill", None)
        disagg = prefill_spec is not None

        # Disaggregated pods need VLLM_NIXL_SIDE_CHANNEL_HOST set to their own
        # pod IP so NixlConnector can open the KV side-channel. It cannot come
        # from user args (it's pod-IP, not a static value), so the backend
        # injects it via the Kubernetes downward API. Only disaggregated replicas
        # get it; the unified path is left untouched.
        if disagg:
            env = (env or []) + [base.nixl_side_channel_env()]

        # Disaggregated decode: the pd-sidecar takes the external serving port
        # (ENGINE_PORT = 8000); vLLM moves to _DECODE_ENGINE_PORT (8001).
        # Unified / prefill paths stay on ENGINE_PORT.
        engine_serving_port = base._DECODE_ENGINE_PORT if disagg else base.ENGINE_PORT

        # Build leader/worker commands (and the args list for the container closure).
        # A user-supplied command owns cross-node coordination: inject neither the
        # Ray bootstrap nor vLLM-specific parallelism flags. It runs verbatim on
        # both templates (e.g. SGLang's symmetric launch against the LWS_* env).
        user_command, leader_command, worker_command, args = _build_commands(
            engine, tensor, pipeline, replica, disagg=disagg
        )

        decode_claim = base.claim_template_name(replica)
        decode_extra: dict = (
            {
                base.LABEL_PD_ROLE: "decode",
                base.LABEL_LLMD_ROLE: "decode",
                base.LABEL_LLMD_SERVING: "true",
                "app": name,
            }
            if disagg
            else {}
        )

        def container(command: list[str], *, serving: bool) -> dict:
            c = {
                "name": "engine",
                "image": engine.image,
                # GPUs PER POD (one tensor-parallel shard runs per pod in the
                # gang), bound via DRA through the pod-level claim.
                "resources": base.engine_resources(),
                # vLLM tensor parallelism needs a large /dev/shm.
                "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}, *cache_volume_mounts],
                "command": command,
            }
            # A user command takes args the normal way; an injected bootstrap
            # folds args into the command itself.
            if user_command and args:
                c["args"] = args
            if env:
                c["env"] = env
            if serving:
                # Disaggregated decode: engine moves to _DECODE_ENGINE_PORT (8001);
                # unified/prefill stay on ENGINE_PORT (8000).
                c["ports"] = [{"containerPort": engine_serving_port}]
                c["readinessProbe"] = {
                    "httpGet": {"path": "/health", "port": engine_serving_port},
                    "initialDelaySeconds": 30,
                    "periodSeconds": 10,
                }
            return c

        def pod_spec(c: dict) -> dict:
            spec = {
                "containers": [c],
                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}, *cache_volumes],
            }
            # Both the leader and worker pods pin to the scheduled pool and
            # claim GPUs via DRA.
            base.place_pod_on(spec, replica.spec.nodePoolName, decode_claim)
            if pull_secrets:
                spec["imagePullSecrets"] = pull_secrets
            return spec

        # Only the leader serves the OpenAI API → it carries the role label the
        # Service selects on, plus the serving port and readiness probe.
        leader_spec = pod_spec(container(leader_command, serving=True))
        # Disaggregated decode: append the pd-sidecar to the leader pod. The
        # sidecar takes ENGINE_PORT (8000) so the Service targetPort is unchanged;
        # the engine has already moved to _DECODE_ENGINE_PORT (8001) above.
        # Workers don't serve the API, so they get no sidecar.
        if disagg:
            leader_spec["containers"].append(base.pd_sidecar_container())
        leader_pod = {
            "metadata": {"labels": {base.LABEL_SERVING: name, _LABEL_ROLE: "leader", **decode_extra}},
            "spec": leader_spec,
        }
        worker_pod = {
            "metadata": {"labels": {base.LABEL_SERVING: name, **decode_extra}},
            "spec": pod_spec(container(worker_command, serving=False)),
        }

        # LeaderWorkerSet: spec.replicas gangs, each of `size` pods (leader+workers).
        leader_worker_set = {
            "apiVersion": "leaderworkerset.x-k8s.io/v1",
            "kind": "LeaderWorkerSet",
            "metadata": {"name": name, "namespace": base.REMOTE_NAMESPACE},
            "spec": {
                "replicas": int(replica.spec.workers.count or 1),
                "leaderWorkerTemplate": {
                    "size": base.nodes_per_worker(replica),
                    "leaderTemplate": leader_pod,
                    "workerTemplate": worker_pod,
                },
            },
        }

        # Service selector: always selects leader pods for this replica.
        # For a disagg replica also narrow to pd-role:decode so prefill leader
        # pods (which are not behind this Service) are never selected.
        svc_selector: dict = {base.LABEL_SERVING: name, _LABEL_ROLE: "leader"}
        if disagg:
            svc_selector[base.LABEL_PD_ROLE] = "decode"

        # Service selects the leader pods of every gang in this replica.
        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "namespace": base.REMOTE_NAMESPACE},
            "spec": {
                "selector": svc_selector,
                "ports": [{"port": 80, "targetPort": base.ENGINE_PORT}],
            },
        }

        # HTTPRoute -> InferencePool (disagg) or Service (unified).
        # Plain Gateway API; Traefik- and Envoy-compatible.
        http_route = {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "HTTPRoute",
            "metadata": {"name": name, "namespace": base.REMOTE_NAMESPACE},
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
                        "backendRefs": _route_backend_refs(name, disagg=disagg),
                    }
                ],
            },
        }

        out = {
            "model-serving": base.wrap_object(pc, leader_worker_set, cel_query=base.AVAILABLE_CEL),
            "model-service": base.wrap_object(pc, service),
            "model-route": base.wrap_object(pc, http_route),
        }
        out[base.RESOURCE_CLAIM_KEY] = base.resource_claim_template(replica, pc)

        # Disaggregated replica: emit the prefill pod set + its ResourceClaimTemplate.
        # No prefill Service or HTTPRoute — prefill is internal-only.
        # Also emit the InferencePool that the HTTPRoute now points to; it lets the
        # GAIE EPP perform KV-aware routing across the decode and prefill pods.
        # The EPP objects (ServiceAccount, Role/RoleBinding, ClusterRole/ClusterRoleBinding,
        # ConfigMap, Deployment, Service) are also emitted for the disaggregated path.
        if disagg:
            out["inference-pool"] = _inference_pool_object(name, pc)
            out.update(_prefill_objects(replica, prefill_spec, name, pc, cache_volumes, cache_volume_mounts))
            out.update(_epp_objects(replica, name, pc))

        return out
