# Copyright 2026 The Modelplane Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Routing: front a replica's engine workloads with a serving surface.

The workload backends (native, llm-d, Grove) compose engines only; this layer
decorates them with the routing the replica's serving.mode selects. apply picks
the strategy:

- Unified fronts the serving pods with a GAIE InferencePool + endpoint picker,
  so requests route by prefix cache and load rather than round-robin. One path
  whether the replica serves from a single pod or many: the picker is vestigial
  for one pod, but the alternative is swapping a Service + HTTPRoute for a pool
  the moment a second pod appears, which would drop in-flight requests.
- PrefillDecode (disaggregated) role-labels the two phase engines and fronts
  both with a GAIE InferencePool + endpoint picker, routed to in place of a
  Service.

serving.router says who writes the router's behaviour. PrefillDecode requires
router: EndpointPicker and a picker config (the XRD enforces it), because a
wrong prefill/decode default reports Ready and rejects requests. The user
writes the picker's configuration and, as a container in the decode pod, the
disaggregation sidecar with the --kv-connector that matches their engines; this
layer injects nothing into their pods. Unified has a default: without a
router, the cluster routes the replica with Modelplane's picker configuration,
which reads --block-size from the engine's args (#179).

The backends build no routing of their own, so a strategy only adds resources
(and decorates the serving pods) - nothing is removed.
"""

import hashlib
import json
from typing import Any

from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

from function.backends import base


def _name(meta: metav1.ObjectMeta | None) -> str:
    """The object's name, always set on resources read from the API server."""
    if meta is None or meta.name is None:
        raise ValueError("metadata.name is unexpectedly absent")
    return meta.name


def _namespace(meta: metav1.ObjectMeta | None) -> str:
    """The object's namespace, always set on namespaced resources read from the API server."""
    if meta is None or meta.namespace is None:
        raise ValueError("metadata.namespace is unexpectedly absent")
    return meta.namespace


# The picker image a replica runs when its serving.endpointPicker sets none.
_EPP_IMAGE = "ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0"


# ConfigMap key and mount-path filename for the EPP config. One name for both
# strategies: the ConfigMap is per-replica and carries a single strategy's
# config, so the mount path never needs to encode which one.
_EPP_CONFIG_FILE = "epp-config.yaml"

# The picker's Prometheus endpoint, and what a series off it is attributed to.
# 9090 is the picker's own default; it is named here because the port has to be
# declared on the pod for the collector to discover it either way.
_EPP_METRICS_PORT = 9090
_EPP_ROLE = "picker"

# Labels llm-d's picker reads on a PrefillDecode replica's serving pods: the
# role its prefill and decode filters select on, and the inference-serving
# marker. The InferencePool selects on base.LABEL_SERVING, not on these.
_LABEL_ROLE = "llm-d.ai/role"
_LABEL_INFERENCE_SERVING = "llm-d.ai/inference-serving"

# EndpointPickerConfig for Unified serving: one profile scoring by prefix cache
# and queue depth, with no prefill/decode split. A single schedulingProfile makes
# the EPP fall back to its SingleProfileHandler, and prefix-cache-scorer still
# needs approx-prefix-cache-producer to populate the prefix-match attribute it
# scores on. The producer pins autoTune: false and blockSizeTokens, because its
# autoTune default leaves the block size 0 and never populates that attribute,
# and blockSizeTokens follows the engine's KV block size (#179), at least
# _MIN_BLOCK_SIZE_TOKENS. prefix-cache-scorer is weighted 2 to
# queue-scorer's 1: cache locality dominates placement until queue depths
# diverge, when the queue score breaks the tie. It's a starting default, not a
# tuned one.
_UNIFIED_EPP_CONFIG_TEMPLATE = """\
apiVersion: llm-d.ai/v1alpha1
kind: EndpointPickerConfig
plugins:
- type: approx-prefix-cache-producer
  parameters:
    autoTune: false
    blockSizeTokens: BLOCK_SIZE_TOKENS
    maxPrefixBlocksToMatch: 256
    lruCapacityPerServer: 31250
- type: prefix-cache-scorer
- type: queue-scorer
- type: max-score-picker
schedulingProfiles:
- name: default
  plugins:
  - pluginRef: max-score-picker
  - pluginRef: prefix-cache-scorer
    weight: 2
  - pluginRef: queue-scorer
    weight: 1
"""

_DEFAULT_KV_BLOCK_SIZE = 16

# The smallest blockSizeTokens the picker uses. llm-d-router raises a smaller
# value to 64 at request time and logs a warning at startup, because a small
# block size grows the prefix index until the picker runs out of memory
# (llm-d/llm-d-router#1158). Rendering it here keeps the config the picker
# reports equal to the one it runs.
_MIN_BLOCK_SIZE_TOKENS = 64


def _unified_epp_config_yaml(block_size: int) -> str:
    """Render the unified EPP config with the engine's KV block size, at least _MIN_BLOCK_SIZE_TOKENS."""
    return _UNIFIED_EPP_CONFIG_TEMPLATE.replace("BLOCK_SIZE_TOKENS", str(max(block_size, _MIN_BLOCK_SIZE_TOKENS)))


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
    A deployment that needs another value writes serving.endpointPicker.config.
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


def _user_routed(replica: v1alpha1.ModelReplica) -> bool:
    """Whether the user writes the router (serving.router: EndpointPicker).

    Otherwise the cluster routes the replica, which the XRD allows only under
    Unified.
    """
    serving = replica.spec.serving
    return serving is not None and serving.router == "EndpointPicker"


def prefill_decode_routing_missing(replica: v1alpha1.ModelReplica) -> bool:
    """Whether a PrefillDecode replica lacks the routing the XRD requires.

    The XRD rejects a PrefillDecode replica without router: EndpointPicker and
    a picker config, but CEL rules don't apply to an object stored before them
    and left unchanged, so such a replica can still reach the function.
    Composing it would leave its decode pods without a sidecar, a pair that
    reports Ready and serves decode alone, so the caller stops instead and
    leaves what's running in place.
    """
    serving = replica.spec.serving
    if not serving or serving.mode != "PrefillDecode":
        return False
    picker = serving.endpointPicker
    return not (_user_routed(replica) and picker is not None and picker.config is not None)


def _picker(replica: v1alpha1.ModelReplica, default_config_yaml: str) -> tuple[str, str]:
    """The picker image and configuration the replica runs.

    With router: EndpointPicker, serving.endpointPicker overrides either (the
    XRD allows it only then). A user config replaces the default whole rather
    than merging with it, and the XRD requires an image alongside it, since a
    picker config is written for one picker release.
    """
    serving = replica.spec.serving
    picker = serving.endpointPicker if serving and _user_routed(replica) else None
    image = picker.image if picker and picker.image else _EPP_IMAGE
    if picker and picker.config is not None:
        return image, _render_picker_config(picker.config)
    return image, default_config_yaml


def _render_picker_config(config: dict[str, Any]) -> str:
    """Render a user's picker config as JSON, which is YAML.

    The config reaches the function through a protobuf Struct, which keeps no key
    order and stores every number as a float. Sorted keys keep the rendering, and
    so the checksum that rolls the picker, the same from one run to the next.
    Whole floats go back to ints, since the picker decodes integer parameters
    strictly and rejects 64.0 where it wants 64.
    """

    def ints(value: object) -> object:
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, dict):
            return {k: ints(v) for k, v in value.items()}
        if isinstance(value, list):
            return [ints(v) for v in value]
        return value

    return json.dumps(ints(config), indent=2, sort_keys=True)


def apply(
    composed: dict[str, k8sobjv1alpha1.Object],
    replica: v1alpha1.ModelReplica,
    provider_config: str,
) -> dict[str, k8sobjv1alpha1.Object]:
    """Decorate the engine workloads with the replica's routing surface.

    PrefillDecode serving picks the disaggregated stack; Unified (or no serving
    block) fronts the serving pods with an InferencePool + endpoint picker.
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
    """Front the replica's serving pods with an InferencePool + endpoint picker.

    Selects the pods by the serving label they already carry, and routes the
    replica's path at the pool. The picker scores by prefix cache and queue depth
    (see _UNIFIED_EPP_CONFIG_TEMPLATE) rather than spraying requests round-robin.

    One pod or many, the surface is the same. A single pod has nothing to pick
    between, so the picker is vestigial there; but always fronting with a pool
    avoids swapping a Service for one the moment a second pod appears - a swap
    that would drop in-flight requests.
    """
    name = _name(replica.metadata)
    out = dict(composed)
    # The prefix-cache producer must chunk prefixes at the engine's KV block
    # size; derive it from the first engine's flags (HACK, #179).
    block_size = _kv_block_size(_engine_args(out[base.workload_key(replica.spec.engines[0])]))
    image, config_yaml = _picker(replica, _unified_epp_config_yaml(block_size))
    selector = {base.LABEL_SERVING: base.serving_label(replica)}
    ns = base.remote_namespace(replica)
    out["inference-pool"] = base.wrap_object(provider_config, _inference_pool(name, ns, selector))
    out[base.ROUTE_KEY] = base.wrap_object(provider_config, _http_route(replica, name))
    out.update(_epp_objects(name, ns, provider_config, image, config_yaml, replica))
    return out


def _disaggregated(
    composed: dict[str, k8sobjv1alpha1.Object],
    replica: v1alpha1.ModelReplica,
    provider_config: str,
) -> dict[str, k8sobjv1alpha1.Object]:
    """Front the prefill and decode engines with an InferencePool + endpoint picker.

    Role-labels the two engines marking phase Prefill and Decode, and adds the
    InferencePool, endpoint picker, and an HTTPRoute pointing at the pool. The
    engine workloads are reused as-is apart from the labels. The decode pod's
    sidecar is the user's container; a decode pod without one sends every
    request to decode alone.

    The picker runs the user's config: the XRD requires router: EndpointPicker
    and serving.endpointPicker.config under PrefillDecode, and fn.py stops before
    composing a replica that lacks them (prefill_decode_routing_missing). The
    engine image must ship its KV connector's runtime, which is the user's.
    """
    name = _name(replica.metadata)
    prefill = next(e for e in replica.spec.engines if e.phase == "Prefill")
    decode = next(e for e in replica.spec.engines if e.phase == "Decode")

    out = dict(composed)
    prefill_key = base.workload_key(prefill)
    decode_key = base.workload_key(decode)
    _label_role(out[prefill_key], role="prefill", app=name)
    _label_role(out[decode_key], role="decode", app=name)

    if prefill_decode_routing_missing(replica):
        raise ValueError("PrefillDecode routing needs serving.endpointPicker.config")
    image, config_yaml = _picker(replica, "")
    # Select the replica's serving pods by the serving label they already carry,
    # the same as unified. The phase (role) labels _label_role adds are for the
    # EPP's prefill/decode filters, not for pool membership.
    selector = {base.LABEL_SERVING: base.serving_label(replica)}
    ns = base.remote_namespace(replica)
    out["inference-pool"] = base.wrap_object(provider_config, _inference_pool(name, ns, selector))
    out[base.ROUTE_KEY] = base.wrap_object(provider_config, _http_route(replica, name))
    out.update(_epp_objects(name, ns, provider_config, image, config_yaml, replica))
    return out


def _serving_pod_templates(manifest: dict) -> list[dict]:
    """The serving pod target(s) of a workload that actually serve the API.

    Normalizes the three workload shapes to {"labels": ..., "spec": ...}, a
    mutable reference to the pod's labels and a mutable reference to its
    corev1.PodSpec, so callers don't need to know which workload kind they're
    decorating. A Deployment carries a PodTemplateSpec at spec.template and a
    LeaderWorkerSet one at spec.leaderWorkerTemplate.leaderTemplate (its
    followers never serve) - both with labels under metadata. A Grove
    PodCliqueSet's leader clique (the only clique that serves) is different: its
    podSpec is a bare corev1.PodSpec with no metadata of its own, so its labels
    live one level up, on the clique itself.
    """
    spec = manifest["spec"]
    kind = manifest["kind"]
    if kind == "Deployment":
        tmpl = spec["template"]
        return [{"labels": tmpl.setdefault("metadata", {}).setdefault("labels", {}), "spec": tmpl["spec"]}]
    if kind == "LeaderWorkerSet":
        tmpl = spec["leaderWorkerTemplate"]["leaderTemplate"]
        return [{"labels": tmpl.setdefault("metadata", {}).setdefault("labels", {}), "spec": tmpl["spec"]}]
    for clique in spec["template"]["cliques"]:
        if clique["name"] == base.GROVE_LEADER_CLIQUE:
            return [{"labels": clique.setdefault("labels", {}), "spec": clique["spec"]["podSpec"]}]
    raise ValueError("PodCliqueSet manifest has no leader clique")


def _label_role(obj: k8sobjv1alpha1.Object, *, role: str, app: str) -> None:
    """Stamp the labels llm-d's picker reads on an engine's serving pods."""
    manifest = obj.spec.forProvider.manifest
    for tmpl in _serving_pod_templates(manifest):
        labels = tmpl["labels"]
        labels[_LABEL_ROLE] = role
        labels[_LABEL_INFERENCE_SERVING] = "true"
        labels["app"] = app


def _inference_pool(name: str, namespace: str, selector: dict[str, str]) -> dict:
    return {
        "apiVersion": "inference.networking.k8s.io/v1",
        "kind": "InferencePool",
        "metadata": {"name": f"{name}-pool", "namespace": namespace},
        "spec": {
            "selector": {"matchLabels": selector},
            "targetPorts": [{"number": base.ENGINE_PORT}],
            "endpointPickerRef": {"name": f"{name}-epp", "port": {"number": 9002}, "failureMode": "FailOpen"},
        },
    }


def _http_route(replica: v1alpha1.ModelReplica, name: str) -> dict:
    return {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {"name": name, "namespace": base.remote_namespace(replica)},
        "spec": {
            "parentRefs": [{"name": "cluster-gateway", "namespace": "modelplane-system"}],
            "rules": [
                {
                    "matches": [{"path": {"type": "PathPrefix", "value": f"/{_namespace(replica.metadata)}/{name}/"}}],
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


def _epp_objects(
    name: str,
    ns: str,
    provider_config: str,
    image: str,
    config_yaml: str,
    replica: v1alpha1.ModelReplica,
) -> dict[str, k8sobjv1alpha1.Object]:
    """The endpoint picker: ServiceAccount, RBAC, ConfigMap, Deployment, Service.

    image and config_yaml are the picker image and the configuration it runs
    with: the user's serving.endpointPicker, or the default for the serving
    strategy.
    """
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
        "data": {_EPP_CONFIG_FILE: config_yaml},
    }
    # The EPP reads its config once at startup and a mounted ConfigMap update
    # doesn't re-trigger that read, so a config change alone would leave the
    # picker on the old config. Stamp a hash of the config on the pod template so
    # a change rolls the Deployment.
    config_checksum = hashlib.sha256(config_yaml.encode()).hexdigest()
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": epp, "namespace": ns},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": epp}},
            "template": {
                "metadata": {
                    # The identity labels beside the selector label: they are
                    # how the collector attributes a series off this pod to the
                    # deployment and replica it routes for. Nothing selects on
                    # them, so they can't collide with the selector above.
                    "labels": {"app": epp, **base.fleet_labels(replica, _EPP_ROLE)},
                    "annotations": {"modelplane.ai/epp-config-checksum": config_checksum},
                },
                "spec": {
                    "serviceAccountName": epp,
                    "containers": [
                        {
                            "name": "epp",
                            "image": image,
                            "args": [
                                f"--pool-name={name}-pool",
                                f"--pool-namespace={ns}",
                                "--pool-group=inference.networking.k8s.io",
                                f"--config-file=/config/{_EPP_CONFIG_FILE}",
                                "--grpc-port=9002",
                                f"--metrics-port={_EPP_METRICS_PORT}",
                                # The picker's metrics endpoint authenticates
                                # its callers by TokenReview by default, which
                                # needs the system:auth-delegator ClusterRole
                                # its ServiceAccount does not hold and cannot
                                # be given namespace-scoped. Left on, every
                                # scrape is rejected. Off, the endpoint is
                                # plain HTTP on the pod network, which is how
                                # every engine already serves /metrics.
                                #
                                # --secure-serving is a different thing and
                                # stays on: it is the TLS of the ext-proc gRPC
                                # server Envoy calls, not of this.
                                "--metrics-endpoint-auth=false",
                            ],
                            "ports": [
                                {"name": "grpc", "containerPort": 9002},
                                {"name": "grpc-health", "containerPort": 9003},
                                # Named for the collector's engine scrape job,
                                # which keeps a pod on this name.
                                {"name": base.ENGINE_PORT_NAME, "containerPort": _EPP_METRICS_PORT},
                            ],
                            # Ready once the picker has synced its pool. Without
                            # it a rollout replaces the old picker before the
                            # new one serves, and the gateway, which fails
                            # closed, rejects requests in between. No service
                            # name: the overall check, which every picker on
                            # this command line answers.
                            "readinessProbe": {"grpc": {"port": 9003}, "periodSeconds": 2},
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
