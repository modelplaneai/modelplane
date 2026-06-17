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

# Selector labels shared by both engines' serving pods (the InferencePool
# matchLabels) and the per-role label the picker partitions on.
_LABEL_ROLE = "llm-d.ai/role"
_LABEL_INFERENCE_SERVING = "llm-d.ai/inference-serving"

# EndpointPickerConfig for the disaggregated profile. The apiVersion is the GIE
# group the EPP binary registers (inference.networking.x-k8s.io/v1alpha1).
_EPP_CONFIG_YAML = """\
apiVersion: inference.networking.x-k8s.io/v1alpha1
kind: EndpointPickerConfig
plugins:
- type: prefill-filter
- type: decode-filter
- type: max-score-picker
- type: prefix-cache-scorer
- type: queue-scorer
- type: prefix-based-pd-decider
- type: disagg-profile-handler
  parameters:
    deciders:
      prefill: prefix-based-pd-decider
schedulingProfiles:
- name: prefill
  plugins:
  - pluginRef: prefill-filter
  - pluginRef: max-score-picker
- name: decode
  plugins:
  - pluginRef: decode-filter
  - pluginRef: max-score-picker
"""


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
    """
    name = replica.metadata.name
    prefill = next(e for e in replica.spec.engines if e.phase == "Prefill")
    decode = next(e for e in replica.spec.engines if e.phase == "Decode")

    out = dict(composed)
    decode_key = base.workload_key(decode)
    _label_role(out[base.workload_key(prefill)], role="prefill", app=name)
    _label_role(out[decode_key], role="decode", app=name)
    _add_sidecar_to_decode(out[decode_key])

    out["inference-pool"] = base.wrap_object(provider_config, _inference_pool(name))
    out[base.ROUTE_KEY] = base.wrap_object(provider_config, _http_route(replica, name))
    out.update(_epp_objects(name, provider_config))
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
    args = engine.get("args", [])
    for i, a in enumerate(args):
        if a.startswith("--port="):
            return int(a.split("=", 1)[1])
        if a == "--port" and i + 1 < len(args):
            return int(args[i + 1])
    return _DECODE_ENGINE_PORT


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
        }
        containers.append(
            {
                "name": "pd-sidecar",
                "image": _SIDECAR_IMAGE,
                "args": ["--secure-proxy=false", "--kv-connector=nixlv2", f"--vllm-port={port}"],
                "ports": [{"containerPort": base.ENGINE_PORT}],
                "readinessProbe": {
                    "httpGet": {"path": "/health", "port": base.ENGINE_PORT},
                    "initialDelaySeconds": 30,
                    "periodSeconds": 10,
                },
            }
        )


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


def _epp_objects(name: str, provider_config: str) -> dict[str, k8sobjv1alpha1.Object]:
    """The hardcoded endpoint picker: ServiceAccount, RBAC, ConfigMap, Deployment, Service."""
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
        "data": {"pd-epp-config.yaml": _EPP_CONFIG_YAML},
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
