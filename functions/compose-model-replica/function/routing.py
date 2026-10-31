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
router: EndpointPicker and a picker (the XRD enforces it), because a wrong
prefill/decode default reports Ready and rejects requests. The user writes the
picker's container, its configuration and, as a container in the decode pod,
the disaggregation sidecar with the --kv-connector that matches their engines;
this layer injects nothing into their pods. Unified has a default: without a
router, the cluster routes the replica with llm-d's picker and Modelplane's
configuration, which reads --block-size from the engine's args (#179).

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


# The picker image a replica runs when it doesn't set serving.router.
_EPP_IMAGE = "ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0"

# The picker's gRPC ports: ext-proc for the gateway, and the health service its
# readiness probe calls. Every picker serves these, whatever it's called with.
_EPP_GRPC_PORT = 9002
_EPP_GRPC_HEALTH_PORT = 9003

# What only Modelplane knows about a picker, set in its environment for its
# command line to pass on. The user writes the command line, because each
# picker spells it differently.
_EPP_ENV_POOL_NAME = "MODELPLANE_POOL_NAME"
_EPP_ENV_POOL_NAMESPACE = "MODELPLANE_POOL_NAMESPACE"
_EPP_ENV_CONFIG = "MODELPLANE_PICKER_CONFIG"

# The ports Modelplane expects the picker to serve on, set in its environment
# too, so its command line names them rather than repeating the numbers.
_EPP_ENV_GRPC_PORT = "MODELPLANE_GRPC_PORT"
_EPP_ENV_GRPC_HEALTH_PORT = "MODELPLANE_GRPC_HEALTH_PORT"
_EPP_ENV_METRICS_PORT = "MODELPLANE_METRICS_PORT"

# The picker counts as available while its rollout progresses. A rollout past
# its progress deadline, such as a new picker that rejects its configuration
# and never becomes ready while the old one keeps serving, makes it not.
_EPP_AVAILABLE_CEL = (
    base.AVAILABLE_CEL + ' && !object.status.conditions.exists(c, c.type == "Progressing" && c.status == "False")'
)


# ConfigMap key and mount-path filename for the EPP config. One name for both
# strategies: the ConfigMap is per-replica and carries a single strategy's
# config, so the mount path never needs to encode which one.
_EPP_CONFIG_FILE = "epp-config.yaml"

# The picker's Prometheus endpoint, and what a series off it is attributed to.
# 9090 is the picker's own default; it is named here because the port has to be
# declared on the pod for the collector to discover it either way.
_EPP_METRICS_PORT = 9090
_EPP_ROLE = "picker"

# Two pickers, both serving, so a drained node leaves one routing. llm-d's
# leader election would keep a single prefix-cache view, but only the leader
# passes its readiness probe. A disruption budget would then never allow its
# eviction, and a rolling update could never replace it (llm-d's own chart
# uses the Recreate strategy for that reason). Without leader election each
# picker learns prefixes only from the requests it routes, which matters only
# when a replica runs more than one copy of an engine.
_EPP_REPLICAS = 2

# The longest a label value can be.
_LABEL_VALUE_MAX = 63

# The label llm-d's prefill and decode filters select on, on a PrefillDecode
# replica's serving pods. The InferencePool selects on base.LABEL_SERVING.
_LABEL_ROLE = "llm-d.ai/role"

# EndpointPickerConfig for Unified serving: one profile scoring by prefix cache
# and queue depth, with no prefill/decode split. A single schedulingProfile makes
# the EPP fall back to its SingleProfileHandler, and prefix-cache-scorer still
# needs approx-prefix-cache-producer to populate the prefix-match attribute it
# scores on. The producer pins autoTune: false and blockSizeTokens, because its
# autoTune default leaves the block size 0 and never populates that attribute,
# and blockSizeTokens follows the engine's KV block size (#179), at least
# _MIN_BLOCK_SIZE_TOKENS. maxPrefixTokensToMatch is the producer's only match
# cap: llm-d-router v0.9.0 defaults it to 131072 and, while it's above 0, ignores
# maxPrefixBlocksToMatch (approximateprefix/types.go L133-L153, plugin.go
# L178-L181). Rendering the default explicitly keeps the config the picker
# reports equal to the one it runs. The producer hashes only that many leading
# tokens of a prompt, so two prompts that agree that far look the same to it.
# prefix-cache-scorer is weighted 2 to queue-scorer's 1: cache locality
# dominates placement until queue depths diverge, when the queue score breaks
# the tie. It's a starting default, not a tuned one.
_UNIFIED_EPP_CONFIG_TEMPLATE = """\
apiVersion: llm-d.ai/v1alpha1
kind: EndpointPickerConfig
plugins:
- type: approx-prefix-cache-producer
  parameters:
    autoTune: false
    blockSizeTokens: BLOCK_SIZE_TOKENS
    maxPrefixTokensToMatch: 131072
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

    Otherwise Modelplane routes the replica with its default picker
    configuration, which the XRD allows only under Unified.
    """
    serving = replica.spec.serving
    return serving is not None and serving.router == "EndpointPicker"


def prefill_decode_routing_missing(replica: v1alpha1.ModelReplica) -> bool:
    """Whether a PrefillDecode replica lacks the routing the XRD requires.

    The XRD rejects a PrefillDecode replica without router: EndpointPicker and
    a picker, but CEL rules don't apply to an object stored before them
    and left unchanged, so such a replica can still reach the function.
    Composing it would leave its decode pods without a sidecar, a pair that
    reports Ready and serves decode alone, so the caller stops instead and
    leaves what's running in place.
    """
    serving = replica.spec.serving
    if not serving or serving.mode != "PrefillDecode":
        return False
    return not (_user_routed(replica) and serving.endpointPicker is not None)


def decode_without_sidecar(replica: v1alpha1.ModelReplica) -> bool:
    """Whether a PrefillDecode replica's decode serving pod runs only its engine.

    llm-d's picker sends a request's prefill through a sidecar in the decode
    pod, so a decode pod with no other container serves every request from
    decode alone. Modelplane doesn't know which container is the sidecar, so
    any container beside the engine counts.
    """
    serving = replica.spec.serving
    if not serving or serving.mode != "PrefillDecode":
        return False
    decode = next((e for e in replica.spec.engines if e.phase == "Decode"), None)
    if decode is None:
        return False
    member = base.engine_member(decode, base.ROLE_LEADER) or base.engine_member(decode, base.ROLE_STANDALONE)
    if member is None or member.template.spec is None:
        return False
    return all(c.name == "engine" for c in member.template.spec.containers)


def _default_unified_picker() -> dict[str, Any]:
    """The picker container a replica runs when it doesn't set serving.router: llm-d's."""
    return {
        "name": "epp",
        "image": _EPP_IMAGE,
        "args": [
            f"--pool-name=$({_EPP_ENV_POOL_NAME})",
            f"--pool-namespace=$({_EPP_ENV_POOL_NAMESPACE})",
            "--pool-group=inference.networking.k8s.io",
            f"--config-file=$({_EPP_ENV_CONFIG})",
            f"--grpc-port=$({_EPP_ENV_GRPC_PORT})",
            f"--grpc-health-port=$({_EPP_ENV_GRPC_HEALTH_PORT})",
            f"--metrics-port=$({_EPP_ENV_METRICS_PORT})",
            # llm-d's picker authenticates scrapes by TokenReview by default,
            # which needs the system:auth-delegator ClusterRole its
            # ServiceAccount can't be given namespace-scoped, so every scrape
            # would be rejected. Off, the endpoint is plain HTTP on the pod
            # network, as every engine's /metrics is.
            "--metrics-endpoint-auth=false",
        ],
    }


def _picker(
    replica: v1alpha1.ModelReplica, default_config_yaml: str | None
) -> tuple[dict[str, Any], str | None, list[dict]]:
    """The picker container, its configuration and the pod's image pull secrets.

    With router: EndpointPicker, the replica's serving.endpointPicker sets all
    three, and its config replaces the default whole rather than merging with
    it. It may set no config, for a picker that takes none. Otherwise the
    replica runs llm-d's picker with default_config_yaml, which only a Unified
    replica has.
    """
    serving = replica.spec.serving
    picker = serving.endpointPicker if serving and _user_routed(replica) else None
    if picker is not None:
        spec = picker.template.spec
        container = spec.containers[0].model_dump(exclude_none=True)
        secrets = [s.model_dump(exclude_none=True) for s in spec.imagePullSecrets or []]
        config_yaml = _render_picker_config(picker.config) if picker.config else None
        return container, config_yaml, secrets
    assert default_config_yaml is not None, "only a Unified replica falls back to the default picker configuration"
    return _default_unified_picker(), default_config_yaml, []


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
    container, config_yaml, secrets = _picker(replica, _unified_epp_config_yaml(block_size))
    selector = {base.LABEL_SERVING: base.serving_label(replica)}
    ns = base.remote_namespace(replica)
    out["inference-pool"] = base.wrap_object(provider_config, _inference_pool(name, ns, selector))
    out[base.ROUTE_KEY] = base.wrap_object(provider_config, _http_route(replica, name))
    out.update(_epp_objects(name, ns, provider_config, container, config_yaml, secrets, replica))
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

    The picker is the user's: the XRD requires router: EndpointPicker and
    serving.endpointPicker under PrefillDecode, and fn.py stops before
    composing a replica that lacks them (prefill_decode_routing_missing). The
    engine image must ship its KV connector's runtime, which is the user's.
    """
    name = _name(replica.metadata)
    prefill = next(e for e in replica.spec.engines if e.phase == "Prefill")
    decode = next(e for e in replica.spec.engines if e.phase == "Decode")

    out = dict(composed)
    prefill_key = base.workload_key(prefill)
    decode_key = base.workload_key(decode)
    _label_role(out[prefill_key], role="prefill")
    _label_role(out[decode_key], role="decode")

    container, config_yaml, secrets = _picker(replica, None)
    # Select the replica's serving pods by the serving label they already carry,
    # the same as unified. The phase (role) labels _label_role adds are for the
    # EPP's prefill/decode filters, not for pool membership.
    selector = {base.LABEL_SERVING: base.serving_label(replica)}
    ns = base.remote_namespace(replica)
    out["inference-pool"] = base.wrap_object(provider_config, _inference_pool(name, ns, selector))
    out[base.ROUTE_KEY] = base.wrap_object(provider_config, _http_route(replica, name))
    out.update(_epp_objects(name, ns, provider_config, container, config_yaml, secrets, replica))
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


def _label_role(obj: k8sobjv1alpha1.Object, *, role: str) -> None:
    """Stamp the role llm-d's prefill and decode filters select on onto an engine's serving pods."""
    manifest = obj.spec.forProvider.manifest
    for tmpl in _serving_pod_templates(manifest):
        tmpl["labels"][_LABEL_ROLE] = role


def _inference_pool(name: str, namespace: str, selector: dict[str, str]) -> dict:
    return {
        "apiVersion": "inference.networking.k8s.io/v1",
        "kind": "InferencePool",
        "metadata": {"name": f"{name}-pool", "namespace": namespace},
        "spec": {
            "selector": {"matchLabels": selector},
            "targetPorts": [{"number": base.ENGINE_PORT}],
            # Envoy AI Gateway fails closed without a picker whatever the
            # failure mode, so the pool leaves it at its default, FailClose.
            "endpointPickerRef": {
                "name": base.dns_1035_label(f"{name}-epp", "epp"),
                "port": {"number": _EPP_GRPC_PORT},
            },
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
    container: dict[str, Any],
    config_yaml: str | None,
    image_pull_secrets: list[dict],
    replica: v1alpha1.ModelReplica,
) -> dict[str, k8sobjv1alpha1.Object]:
    """The endpoint picker: ServiceAccount, RBAC, ConfigMap, Deployment, Service, PDB.

    container, config_yaml and image_pull_secrets are the picker as the user's
    serving.endpointPicker writes it, or the default for the serving strategy.
    Modelplane adds what only it knows to the container as environment: the
    pool, the ports it expects the picker to serve on, and where the
    configuration is mounted. It also declares those ports, and adds the
    readiness probe. A picker with no
    configuration gets no ConfigMap.
    """
    epp = f"{name}-epp"
    # Only the Service needs a DNS-1035 label, and the app label a value of at
    # most 63 characters; the other objects keep {name}-epp, which any
    # replica name makes a valid name for them, so none is renamed.
    #
    # NOTE(negz): Kubernetes accepts a Service name that starts with a digit
    # from 1.36, so there a replica named that way already had a {name}-epp
    # Service, which a derived name leaves behind. It selects the same pods, so
    # it's harmless.
    service_name = base.dns_1035_label(epp, "epp")
    app = epp if len(epp) <= _LABEL_VALUE_MAX else service_name
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
    pool = f"{name}-pool"
    env = [
        {"name": _EPP_ENV_POOL_NAME, "value": pool},
        {"name": _EPP_ENV_POOL_NAMESPACE, "value": ns},
        {"name": _EPP_ENV_GRPC_PORT, "value": str(_EPP_GRPC_PORT)},
        {"name": _EPP_ENV_GRPC_HEALTH_PORT, "value": str(_EPP_GRPC_HEALTH_PORT)},
        {"name": _EPP_ENV_METRICS_PORT, "value": str(_EPP_METRICS_PORT)},
    ]
    mounts: list[dict] = []
    volumes: list[dict] = []
    annotations: dict[str, str] = {}
    objects: dict[str, k8sobjv1alpha1.Object] = {}
    if config_yaml is not None:
        env.append({"name": _EPP_ENV_CONFIG, "value": f"/config/{_EPP_CONFIG_FILE}"})
        mounts.append({"name": "config", "mountPath": "/config"})
        volumes.append({"name": "config", "configMap": {"name": epp}})
        config = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": epp, "namespace": ns},
            "data": {_EPP_CONFIG_FILE: config_yaml},
        }
        objects["epp-config"] = base.wrap_object(provider_config, config)
        # The picker reads its config once at startup, and a mounted ConfigMap
        # update doesn't re-trigger that read, so a config change alone would
        # leave the picker on the old config. A hash of the config on the pod
        # template rolls the Deployment instead.
        annotations["modelplane.ai/epp-config-checksum"] = hashlib.sha256(config_yaml.encode()).hexdigest()
    picker = {
        **container,
        # Modelplane's first, so the user's own environment can refer to it.
        "env": [*env, *container.get("env", [])],
        "ports": [
            {"name": "grpc", "containerPort": _EPP_GRPC_PORT},
            {"name": "grpc-health", "containerPort": _EPP_GRPC_HEALTH_PORT},
            # Named for the collector's engine scrape job, which keeps a pod on
            # this name.
            {"name": base.ENGINE_PORT_NAME, "containerPort": _EPP_METRICS_PORT},
        ],
        # Ready once the picker has synced its pool. Without it a rollout
        # replaces the old picker before the new one serves, and the gateway,
        # which fails closed, rejects requests in between. No service name:
        # the overall check, which any gRPC health server answers.
        "readinessProbe": {"grpc": {"port": _EPP_GRPC_HEALTH_PORT}, "periodSeconds": 2},
    }
    if mounts:
        picker["volumeMounts"] = mounts
    pod_spec: dict[str, Any] = {
        "serviceAccountName": epp,
        "containers": [picker],
        # Spread the pickers across nodes and zones where there's room, and
        # share a node where there isn't, as on a single system node.
        "topologySpreadConstraints": [
            {
                "maxSkew": 1,
                "topologyKey": key,
                "whenUnsatisfiable": "ScheduleAnyway",
                "labelSelector": {"matchLabels": {"app": app}},
            }
            for key in ("kubernetes.io/hostname", "topology.kubernetes.io/zone")
        ],
    }
    if volumes:
        pod_spec["volumes"] = volumes
    if image_pull_secrets:
        pod_spec["imagePullSecrets"] = image_pull_secrets
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": epp, "namespace": ns},
        "spec": {
            "replicas": _EPP_REPLICAS,
            # Available while one picker is, which is all serving needs. The
            # default, 25%, rounds down to none for two pickers, so a picker
            # left pending by a drain would report the replica as not serving.
            "strategy": {"rollingUpdate": {"maxUnavailable": 1}},
            "selector": {"matchLabels": {"app": app}},
            "template": {
                "metadata": {
                    # The identity labels beside the selector label: they are
                    # how the collector attributes a series off this pod to the
                    # deployment and replica it routes for. Nothing selects on
                    # them, so they can't collide with the selector above.
                    "labels": {"app": app, **base.fleet_labels(replica, _EPP_ROLE)},
                    **({"annotations": annotations} if annotations else {}),
                },
                "spec": pod_spec,
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": service_name, "namespace": ns},
        "spec": {
            "selector": {"app": app},
            "ports": [
                {"name": "grpc-ext-proc", "port": _EPP_GRPC_PORT, "targetPort": _EPP_GRPC_PORT, "appProtocol": "http2"}
            ],
        },
    }
    # Lets a drain evict one picker at a time, so the other keeps routing until
    # its peer is ready elsewhere. A picker that isn't ready can always be
    # evicted, so one that never starts doesn't hold up a drain.
    budget = {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {"name": epp, "namespace": ns},
        "spec": {
            "maxUnavailable": 1,
            "selector": {"matchLabels": {"app": app}},
            "unhealthyPodEvictionPolicy": "AlwaysAllow",
        },
    }
    return {
        "epp-serviceaccount": base.wrap_object(provider_config, sa),
        "epp-role": base.wrap_object(provider_config, role),
        "epp-rolebinding": base.wrap_object(provider_config, binding),
        **objects,
        # Watched, so a stalled rollout reaches the replica when it happens
        # rather than at provider-kubernetes' next resync.
        "epp": base.wrap_object(provider_config, deployment, cel_query=_EPP_AVAILABLE_CEL, watch=True),
        "epp-service": base.wrap_object(provider_config, service),
        "epp-disruption-budget": base.wrap_object(provider_config, budget),
    }
