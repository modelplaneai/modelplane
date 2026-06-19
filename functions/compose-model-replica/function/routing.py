"""Routing: front a replica's engine workloads with a serving surface.

The workload backends (native, llm-d, dynamo) compose engines only; this layer
decorates them with the routing the replica's serving.mode selects. apply picks
the strategy:

- Unified fronts every engine's serving pods with one Service + HTTPRoute.
- PrefillDecode (disaggregated) role-labels the two phase engines, injects the
  pd-sidecar on decode, and fronts both with a GAIE InferencePool + endpoint
  picker, routed to in place of a Service.

The backends build no routing of their own, so a strategy only adds resources
(and, for disaggregated, decorates the decode pod) - nothing is removed.

Engine flags, including --kv-transfer-config for the NixlConnector, are the
user's; this layer injects none.
"""

from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

from function.backends import base

_EPP_IMAGE = "ghcr.io/llm-d/llm-d-inference-scheduler:v0.8.0"
_SIDECAR_IMAGE = "ghcr.io/llm-d/llm-d-routing-sidecar:v0.8.0"

# The pd-sidecar takes ENGINE_PORT (8000), so the decode engine listens here.
_DECODE_ENGINE_PORT = 8001

# NIXL KV-transfer plumbing injected onto every disaggregated engine.
_NIXL_SHM_VOLUME = "nixl-shm"
_NIXL_SIDE_CHANNEL_PORT = "5557"

# Selector labels shared by both engines' serving pods (the InferencePool
# matchLabels) and the per-role label the picker partitions on.
_LABEL_ROLE = "llm-d.ai/role"
_LABEL_INFERENCE_SERVING = "llm-d.ai/inference-serving"

# EndpointPickerConfig for the disaggregated profile. The apiVersion is the GIE
# group the EPP binary registers (inference.networking.x-k8s.io/v1alpha1).
#
# The decider in disagg-profile-handler is what makes a request disaggregate: it
# runs the prefill profile, picks a prefill endpoint, and the handler sets the
# x-prefiller-host-port header the routing sidecar uses to send the prefill phase
# there (KV then flows prefill->decode over NIXL). The selective
# prefix-based-pd-decider disaggregates only when a request's uncached suffix is
# at least nonCachedTokens long, so short or cache-hot prompts skip the prefill
# hop (and its KV-transfer cost) and serve decode-only.
#
# Three things must line up or it silently never disaggregates, and the EPP
# image we run (llm-d-inference-scheduler v0.8.0, embedding
# gateway-api-inference-extension v1.5.0) makes the defaults wrong on every one:
#   1. nonCachedTokens defaults to 0, which the decider treats as "disabled"
#      (always decode-only). Set it explicitly.
#   2. The decider reads a PrefixCacheMatchInfo attribute that prefix-cache-scorer
#      no longer produces (GIE v1.5.0 split production into a separate plugin and
#      made prepare-data default-on, so the old `prepareDataPlugins` feature gate
#      the v0.8.0 docs still mention is *unregistered* and crashes the EPP). The
#      producer is now an explicit plugin: approx-prefix-cache-producer.
#   3. That producer defaults to autoTune: true, which leaves its block size 0
#      and never populates the attribute. Pin autoTune: false + blockSizeTokens.
# (Verified live: with this config the prefill engine's request_prefill_time
# counter increments for long prompts and stays flat for short ones; with the
# defaults it stayed at zero for everything.)
#
# blockSizeTokens MUST match the engine's KV block size or prefix-cache routing
# silently degrades (#179). It's derived best-effort from the engine flags via
# _kv_block_size() (BLOCK_SIZE_TOKENS placeholder), defaulting to vLLM's 16.
_EPP_CONFIG_TEMPLATE = """\
apiVersion: inference.networking.x-k8s.io/v1alpha1
kind: EndpointPickerConfig
plugins:
- type: approx-prefix-cache-producer
  parameters:
    autoTune: false
    blockSizeTokens: BLOCK_SIZE_TOKENS
    maxPrefixBlocksToMatch: 256
    lruCapacityPerServer: 31250
- type: prefix-cache-scorer
- type: disagg-headers-handler
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

_DEFAULT_KV_BLOCK_SIZE = 16


def _epp_config_yaml(block_size: int) -> str:
    """Render the EPP config with the engine's KV block size."""
    return _EPP_CONFIG_TEMPLATE.replace("BLOCK_SIZE_TOKENS", str(block_size))


def _flag_value(args: list, *flags: str) -> str | None:
    """Best-effort value of a `--flag value` or `--flag=value` engine arg.

    Engine flags belong to the user (per #137); callers only peek. Returns the
    first match's raw string value, or None if no flag is present.
    """
    for i, a in enumerate(args or []):
        for flag in flags:
            if a == flag and i + 1 < len(args):
                return args[i + 1]
            if a.startswith(flag + "="):
                return a.split("=", 1)[1]
    return None


def _kv_block_size(engine_args: list) -> int:
    """HACK: best-effort read the engine's KV block size from its flags so the
    EPP prefix-cache producer chunks prefixes the same way the engine does.

    We peek for the common flags — vLLM's --block-size and SGLang's --page-size
    — and fall back to vLLM's default of 16. A mismatch silently degrades
    prefix-cache routing with no error (#179), so deriving it beats hardcoding.
    The durable fix is a typed/overridable knob on the serving block (#179);
    until then, this peek.
    """
    raw = _flag_value(engine_args, "--block-size", "--page-size")
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            pass
    return _DEFAULT_KV_BLOCK_SIZE


def _engine_args(obj: k8sobjv1alpha1.Object) -> list:
    """The engine container's args from a workload Object (best-effort)."""
    for tmpl in _serving_pod_templates(obj.spec.forProvider.manifest):
        for c in tmpl["spec"]["containers"]:
            if c.get("name") == "engine":
                return c.get("args", [])
    return []


def apply(
    composed: dict[str, k8sobjv1alpha1.Object],
    replica: v1alpha1.ModelReplica,
    provider_config: str,
) -> dict[str, k8sobjv1alpha1.Object]:
    """Decorate the engine workloads with the replica's routing surface.

    PrefillDecode serving picks the disaggregated stack; everything else (Unified
    or no serving block) picks the plain Service.
    """
    serving = replica.spec.serving
    if serving and serving.mode == "PrefillDecode":
        return _disaggregated(composed, replica, provider_config)
    return _unified(composed, replica, provider_config)


def _unified(
    composed: dict[str, k8sobjv1alpha1.Object],
    replica: v1alpha1.ModelReplica,
    provider_config: str,
) -> dict[str, k8sobjv1alpha1.Object]:
    """Front every engine's serving pods with one Service + HTTPRoute."""
    return {**composed, **base.serving_resources(replica, provider_config)}


def _disaggregated(
    composed: dict[str, k8sobjv1alpha1.Object],
    replica: v1alpha1.ModelReplica,
    provider_config: str,
) -> dict[str, k8sobjv1alpha1.Object]:
    """Front the prefill and decode engines with an InferencePool + endpoint picker.

    Role-labels the two engines marking phase Prefill and Decode, adds the
    pd-sidecar to decode, and adds the InferencePool, endpoint picker, and an
    HTTPRoute pointing at the pool. The engine workloads are reused as-is apart
    from the label/sidecar decoration.

    Engine-image prerequisite: PrefillDecode needs the engine image to ship the
    NIXL runtime. vLLM's NixlConnector (and SGLang's PD transfer) import the
    `nixl` package, so an image without it crashloops at startup with "NIXL is
    not available". Recent vanilla vllm/vllm-openai images ship NIXL, so pin a
    current tag. Engine images are the user's (#137), so Modelplane can't bundle
    this; it is a deployment prerequisite, not something the composition provides.
    """
    name = replica.metadata.name
    prefill = next(e for e in replica.spec.engines if e.phase == "Prefill")
    decode = next(e for e in replica.spec.engines if e.phase == "Decode")

    out = dict(composed)
    prefill_key = base.workload_key(prefill)
    decode_key = base.workload_key(decode)
    _label_role(out[prefill_key], role="prefill", app=name)
    _label_role(out[decode_key], role="decode", app=name)
    _add_sidecar_to_decode(out[decode_key])

    # Both engines need NIXL KV-transfer plumbing the ModelDeployment schema
    # can't express (no fieldRef env, no volumes). Inject it for them.
    _inject_nixl_plumbing(out[prefill_key])
    _inject_nixl_plumbing(out[decode_key])

    # The EPP's prefix-cache producer must chunk prefixes at the decode engine's
    # KV block size; derive it from the decode engine's flags (HACK, #179).
    block_size = _kv_block_size(_engine_args(out[decode_key]))
    out["inference-pool"] = base.wrap_object(provider_config, _inference_pool(name))
    out[base.ROUTE_KEY] = base.wrap_object(provider_config, _http_route(replica, name))
    out.update(_epp_objects(name, provider_config, block_size))
    return out


def _serving_pod_templates(manifest: dict) -> list[dict]:
    """The pod template(s) of a workload that actually serve the API.

    A Deployment serves from spec.template; a LeaderWorkerSet serves from its
    leaderTemplate (followers never serve). Returns the list to mutate.
    """
    spec = manifest["spec"]
    if manifest["kind"] == "Deployment":
        return [spec["template"]]
    return [spec["leaderWorkerTemplate"]["leaderTemplate"]]


def _label_role(obj: k8sobjv1alpha1.Object, *, role: str, app: str) -> None:
    """Stamp the role + InferencePool selector labels on an engine's serving pods."""
    manifest = obj.spec.forProvider.manifest
    for tmpl in _serving_pod_templates(manifest):
        labels = tmpl.setdefault("metadata", {}).setdefault("labels", {})
        labels[_LABEL_ROLE] = role
        labels[_LABEL_INFERENCE_SERVING] = "true"
        labels["app"] = app


def _decode_port(engine: dict) -> int:
    """The port the decode engine serves on.

    The pd-sidecar takes the external ENGINE_PORT (8000) and forwards to the
    decode engine, so the engine must listen on a different port: Modelplane
    expects _DECODE_ENGINE_PORT. The user owns the engine flags (per #137), so we
    also best-effort honor an explicit --port override rather than assume one.
    """
    raw = _flag_value(engine.get("args", []), "--port")
    return int(raw) if raw is not None else _DECODE_ENGINE_PORT


def _add_sidecar_to_decode(obj: k8sobjv1alpha1.Object) -> None:
    """Front the decode engine with the pd-sidecar on ENGINE_PORT.

    The sidecar takes the external serving port (8000) and forwards to the engine
    on its own --port (read from the user's args); the engine's containerPort and
    probe follow that port so they match what vLLM actually listens on.
    """
    for tmpl in _serving_pod_templates(obj.spec.forProvider.manifest):
        containers = tmpl["spec"]["containers"]
        engine = next(c for c in containers if c["name"] == "engine")
        port = _decode_port(engine)
        engine["ports"] = [{"containerPort": port}]
        engine["readinessProbe"] = {
            "httpGet": {"path": "/health", "port": port},
            "initialDelaySeconds": 30,
            "periodSeconds": 10,
            # Match the timeout the backends set on the standalone probe: a slow
            # but healthy /health (SGLang's sits at ~1s) flaps the 1s default.
            "timeoutSeconds": 5,
        }
        containers.append(
            {
                "name": "pd-sidecar",
                "image": _SIDECAR_IMAGE,
                "args": ["--secure-proxy=false", "--kv-connector=nixlv2", f"--vllm-port={port}"],
                "ports": [{"containerPort": base.ENGINE_PORT}],
                "readinessProbe": {
                    # The sidecar proxies /health to the same engine, so it's
                    # just as slow; give it the same 5s timeout.
                    "httpGet": {"path": "/health", "port": base.ENGINE_PORT},
                    "initialDelaySeconds": 30,
                    "periodSeconds": 10,
                    "timeoutSeconds": 5,
                },
            }
        )


def _inject_nixl_plumbing(obj: k8sobjv1alpha1.Object) -> None:
    """Add the NIXL KV-transfer plumbing every disaggregated engine needs but
    that the ModelDeployment schema can't express (no fieldRef env, no volumes).

    Two pieces, both infra-level and always-correct for PrefillDecode, so we
    inject them the same way we inject the sidecar rather than asking the user:
      - a Memory-backed /dev/shm: vLLM's NixlConnector uses shared memory, and
        the container default (64Mi) is far too small.
      - VLLM_NIXL_SIDE_CHANNEL_HOST set to the pod IP (+ a fixed port) so peer
        engines can reach this one's NIXL metadata channel. Without it the
        engine advertises an unreachable address and cross-pod KV transfer
        fails — requests get a 500 with no error in the engine logs.
    """
    for tmpl in _serving_pod_templates(obj.spec.forProvider.manifest):
        spec = tmpl["spec"]
        volumes = spec.setdefault("volumes", [])
        if not any(v.get("name") == _NIXL_SHM_VOLUME for v in volumes):
            volumes.append({"name": _NIXL_SHM_VOLUME, "emptyDir": {"medium": "Memory"}})
        engine = next(c for c in spec["containers"] if c["name"] == "engine")
        mounts = engine.setdefault("volumeMounts", [])
        if not any(m.get("mountPath") == "/dev/shm" for m in mounts):
            mounts.append({"name": _NIXL_SHM_VOLUME, "mountPath": "/dev/shm"})
        env = engine.setdefault("env", [])
        existing = {e.get("name") for e in env}
        if "VLLM_NIXL_SIDE_CHANNEL_HOST" not in existing:
            env.append(
                {"name": "VLLM_NIXL_SIDE_CHANNEL_HOST", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}}
            )
        if "VLLM_NIXL_SIDE_CHANNEL_PORT" not in existing:
            env.append({"name": "VLLM_NIXL_SIDE_CHANNEL_PORT", "value": _NIXL_SIDE_CHANNEL_PORT})


def _inference_pool(name: str) -> dict:
    return {
        "apiVersion": "inference.networking.k8s.io/v1",
        "kind": "InferencePool",
        "metadata": {"name": f"{name}-pool", "namespace": base.REMOTE_NAMESPACE},
        "spec": {
            "selector": {"matchLabels": {"app": name, _LABEL_INFERENCE_SERVING: "true"}},
            "targetPorts": [{"number": base.ENGINE_PORT}],
            "endpointPickerRef": {"name": f"{name}-epp", "port": {"number": 9002}, "failureMode": "FailOpen"},
        },
    }


def _http_route(replica: v1alpha1.ModelReplica, name: str) -> dict:
    return {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {"name": name, "namespace": base.REMOTE_NAMESPACE},
        "spec": {
            "parentRefs": [{"name": "inference-gateway", "namespace": "modelplane-system"}],
            "rules": [
                {
                    "matches": [{"path": {"type": "PathPrefix", "value": f"/{replica.metadata.namespace}/{name}/"}}],
                    "timeouts": {"request": base.REQUEST_TIMEOUT},
                    "filters": [
                        {
                            "type": "URLRewrite",
                            "urlRewrite": {"path": {"type": "ReplacePrefixMatch", "replacePrefixMatch": "/"}},
                        }
                    ],
                    "backendRefs": [
                        {"group": "inference.networking.k8s.io", "kind": "InferencePool", "name": f"{name}-pool"}
                    ],
                }
            ],
        },
    }


def _epp_objects(name: str, provider_config: str, block_size: int) -> dict[str, k8sobjv1alpha1.Object]:
    """The endpoint picker: ServiceAccount, RBAC, ConfigMap, Deployment, Service.

    block_size is the engine's KV block size, rendered into the prefix-cache
    producer so its prefix chunking matches the engine.
    """
    ns = base.REMOTE_NAMESPACE
    epp = f"{name}-epp"
    sa = {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": epp, "namespace": ns}}
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": epp, "namespace": ns},
        "rules": [
            {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "watch", "list"]},
            {
                "apiGroups": ["inference.networking.k8s.io"],
                "resources": ["inferencepools"],
                "verbs": ["get", "watch", "list"],
            },
            # The picker also watches InferenceObjectives (GIE x-k8s.io group);
            # without this rule it logs a forbidden error.
            {
                "apiGroups": ["inference.networking.x-k8s.io"],
                "resources": ["inferenceobjectives"],
                "verbs": ["get", "watch", "list"],
            },
        ],
    }
    binding = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": epp, "namespace": ns},
        "subjects": [{"kind": "ServiceAccount", "name": epp, "namespace": ns}],
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": epp},
    }
    config = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": epp, "namespace": ns},
        "data": {"pd-epp-config.yaml": _epp_config_yaml(block_size)},
    }
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": epp, "namespace": ns},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": epp}},
            "template": {
                "metadata": {"labels": {"app": epp}},
                "spec": {
                    "serviceAccountName": epp,
                    "containers": [
                        {
                            "name": "epp",
                            "image": _EPP_IMAGE,
                            "args": [
                                f"--pool-name={name}-pool",
                                f"--pool-namespace={ns}",
                                "--pool-group=inference.networking.k8s.io",
                                "--config-file=/config/pd-epp-config.yaml",
                                "--grpc-port=9002",
                            ],
                            "ports": [
                                {"name": "grpc", "containerPort": 9002},
                                {"name": "grpc-health", "containerPort": 9003},
                            ],
                            "volumeMounts": [{"name": "config", "mountPath": "/config"}],
                        }
                    ],
                    "volumes": [{"name": "config", "configMap": {"name": epp}}],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": epp, "namespace": ns},
        "spec": {
            "selector": {"app": epp},
            "ports": [{"name": "grpc-ext-proc", "port": 9002, "targetPort": 9002, "appProtocol": "http2"}],
        },
    }
    return {
        "epp-serviceaccount": base.wrap_object(provider_config, sa),
        "epp-role": base.wrap_object(provider_config, role),
        "epp-rolebinding": base.wrap_object(provider_config, binding),
        "epp-config": base.wrap_object(provider_config, config),
        "epp": base.wrap_object(provider_config, deployment, cel_query=base.AVAILABLE_CEL),
        "epp-service": base.wrap_object(provider_config, service),
    }
