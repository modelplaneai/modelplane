"""Fan out a ModelDeployment to ModelPlacements and configure routing.

This function discovers InferenceEnvironments, matches model requirements
against available capacity, creates a ModelPlacement per matched environment,
and composes Envoy Gateway Backend + HTTPRoute resources on the control plane
for unified endpoint routing.
"""

import math
import re

from crossplane.function import request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .model.ai.modelplane.modeldeployment import v1alpha1
from .model.ai.modelplane.modelplacement import v1alpha1 as mpv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# Maps InferenceEnvironment backends to the engines they support.
_COMPAT = {
    "KServe": ["vLLM"],
}

# The namespace used for LLMInferenceService on all remote clusters.
_REMOTE_NAMESPACE = "default"


def _to_dns_label(s: str) -> str:
    """Sanitize a string to a valid DNS-1035 label.

    DNS-1035 labels must be lowercase, start with a letter, end with an
    alphanumeric, contain only [a-z0-9-], and be at most 63 characters.
    """
    s = s.lower()
    s = re.sub(r"[^a-z0-9-]", "-", s)
    s = re.sub(r"-+", "-", s)
    s = s.strip("-")
    s = f"model-{s}"
    return s[:63]


def _has_condition(req: fnv1.RunFunctionRequest, name: str, cond: str) -> bool:
    """Check if an observed composed resource has a condition set to True.

    Uses the SDK's resource.get_condition which reads status.conditions from
    the protobuf Struct representation of the resource.
    """
    observed = req.observed.resources.get(name)
    if observed is None:
        return False
    return resource.get_condition(observed.resource, cond).status == "True"


def _has_parent_condition(
    req: fnv1.RunFunctionRequest, name: str, cond: str,
) -> bool:
    """Check a Gateway API condition nested under status.parents[].conditions.

    Gateway API resources (HTTPRoute, etc.) nest route status under
    status.parents[].conditions instead of top-level status.conditions.
    """
    observed = req.observed.resources.get(name)
    if observed is None:
        return False
    d = resource.struct_to_dict(observed.resource)
    for p in d.get("status", {}).get("parents", []):
        for c in p.get("conditions", []):
            if c.get("type") == cond and c.get("status") == "True":
                return True
    return False


def _parse_quantity(q: str) -> int:
    """Parse a Kubernetes resource quantity string to bytes.

    Supports Gi, Mi, and Ti suffixes. Returns 0 for unparseable values.
    """
    if not q:
        return 0
    q = q.strip()
    if q.endswith("Gi"):
        return int(q[:-2]) * 1024 * 1024 * 1024
    if q.endswith("Mi"):
        return int(q[:-2]) * 1024 * 1024
    if q.endswith("Ti"):
        return int(q[:-2]) * 1024 * 1024 * 1024 * 1024
    try:
        return int(q)
    except ValueError:
        return 0


def _placement_name(deployment_name: str, ie_name: str) -> str:
    """Derive a deterministic ModelPlacement name.

    Deterministic names are needed so the deployment function knows the
    LLMInferenceService name on the remote cluster for URL rewriting.
    """
    return f"{deployment_name}-{ie_name}"[:63]


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose ModelPlacements and control plane routing resources."""
    xr = v1alpha1.ModelDeployment(
        **resource.struct_to_dict(req.observed.composite.resource)
    )

    model_kind = xr.spec.modelRef.kind or "ClusterModel"
    model_name = xr.spec.modelRef.name
    desired_envs = int(xr.spec.environments)  # protobuf delivers as float
    env_selector = xr.spec.environmentSelector
    xr_name = xr.metadata.name
    xr_ns = xr.metadata.namespace or ""

    # Declare required resources. InferenceEnvironments are matched by the
    # modelplane.ai/environment=true label — a workaround for the empty
    # match_labels protobuf bug (see build log).
    env_match_labels: dict[str, str] = {
        "modelplane.ai/environment": "true",
    }
    if env_selector and env_selector.matchLabels:
        env_match_labels.update(env_selector.matchLabels)

    response.require_resources(
        rsp,
        name="environments",
        api_version="modelplane.ai/v1alpha1",
        kind="InferenceEnvironment",
        match_labels=env_match_labels,
    )
    response.require_resources(
        rsp,
        name="model",
        api_version="modelplane.ai/v1alpha1",
        kind=model_kind,
        match_name=model_name,
    )
    response.require_resources(
        rsp,
        name="inference-gateway",
        api_version="modelplane.ai/v1alpha1",
        kind="InferenceGateway",
        match_name="default",
    )
    response.require_resources(
        rsp,
        name="all-placements",
        api_version="modelplane.ai/v1alpha1",
        kind="ModelPlacement",
        match_labels={"modelplane.ai/placement": "true"},
    )

    # Required resources are dicts resolved by Crossplane.
    envs = request.get_required_resources(req, "environments")
    model_resource = request.get_required_resource(req, "model")
    inference_gw = request.get_required_resource(req, "inference-gateway")
    all_placements = request.get_required_resources(req, "all-placements")

    if not envs:
        rsp.conditions.append(fnv1.Condition(
            type="PlacementsScheduled",
            status=fnv1.STATUS_CONDITION_FALSE,
            reason="NoEnvironments",
            target=fnv1.TARGET_COMPOSITE,
        ))
        response.warning(rsp, "No InferenceEnvironments found")
        return

    if model_resource is None:
        rsp.conditions.append(fnv1.Condition(
            type="PlacementsScheduled",
            status=fnv1.STATUS_CONDITION_FALSE,
            reason="ModelNotFound",
            target=fnv1.TARGET_COMPOSITE,
        ))
        response.warning(rsp, f"Model {model_name} not found")
        return

    model_spec = model_resource.get("spec", {})
    resolved_model_name = model_spec.get("model", {}).get("name", "")
    engine = model_spec.get("engine", "")
    model_vram_bytes = _parse_quantity(
        model_spec.get("resources", {}).get("vram", "0Gi")
    )

    # Schedule: filter environments by engine compatibility and VRAM capacity.
    candidates = []
    for env in envs:
        env_name = env.get("metadata", {}).get("name", "")
        env_status = env.get("status", {})
        capacity = env_status.get("capacity", {})
        backend = capacity.get("backend", "")

        if engine not in _COMPAT.get(backend, []):
            continue

        # Find the pool that needs the fewest GPUs for this model.
        gpu_pools = capacity.get("gpuPools", [])
        best_gpus_needed = None
        eligible_total = 0
        for pool in gpu_pools:
            pool_mem = _parse_quantity(pool.get("memory", "0Gi"))
            if pool_mem <= 0:
                continue
            gpus_needed = max(1, math.ceil(model_vram_bytes / pool_mem))
            eligible_total += pool.get("count", 0)
            if best_gpus_needed is None or gpus_needed < best_gpus_needed:
                best_gpus_needed = gpus_needed

        if best_gpus_needed is None:
            continue

        # Subtract GPUs used by other deployments' placements on this IE.
        used_gpus = 0
        for p in all_placements:
            p_deployment = (
                p.get("metadata", {})
                .get("labels", {})
                .get("modelplane.ai/deployment", "")
            )
            if p_deployment == xr_name:
                continue  # Don't count our own placements against us.
            p_ie = (
                p.get("spec", {})
                .get("inferenceEnvironmentRef", {})
                .get("name", "")
            )
            if p_ie == env_name:
                used_gpus += (
                    p.get("status", {})
                    .get("resources", {})
                    .get("gpu", {})
                    .get("count", 0)
                )

        if eligible_total - used_gpus < best_gpus_needed:
            continue

        candidates.append({
            "name": env_name,
            "gateway_address": env_status.get("gateway", {}).get("address"),
        })

    # Sort by name for deterministic scheduling and take first N.
    candidates.sort(key=lambda c: c["name"])
    matched = candidates[:desired_envs]

    # Transition: emit which environments were matched (first time only).
    matched_names = [c["name"] for c in matched]
    prev_placement_count = sum(
        1 for c in matched
        if f"placement-{c['name']}" in req.observed.resources
    )
    if matched and prev_placement_count == 0:
        response.normal(
            rsp, f"Matched {len(matched)} environments: {', '.join(matched_names)}"
        )

    # Compose a ModelPlacement per matched environment.
    for env_info in matched:
        ie_name = env_info["name"]
        placement_key = f"placement-{ie_name}"
        pname = _placement_name(xr_name, ie_name)

        resource.update(
            rsp.desired.resources[placement_key],
            mpv1alpha1.ModelPlacement(
                metadata=metav1.ObjectMeta(
                    name=pname,
                    namespace=xr_ns,
                    labels={
                        "modelplane.ai/placement": "true",
                        "modelplane.ai/deployment": xr_name,
                    },
                ),
                spec=mpv1alpha1.Spec(
                    modelRef=mpv1alpha1.ModelRef(kind=model_kind, name=model_name),
                    inferenceEnvironmentRef=mpv1alpha1.InferenceEnvironmentRef(
                        name=ie_name,
                    ),
                ),
            ),
        )

    # PlacementsScheduled: environments matched and placements created.
    any_placements_observed = any(
        f"placement-{c['name']}" in req.observed.resources for c in matched
    )
    scheduled = len(matched) > 0 and any_placements_observed

    if not matched:
        sched_reason = "InsufficientCapacity"
        sched_msg = f"0 of {desired_envs} environments matched (checked {len(envs)})"
    elif scheduled:
        sched_reason = "PlacementsCreated"
        sched_msg = f"Matched {len(matched)} environments"
    else:
        sched_reason = "Scheduling"
        sched_msg = ""

    rsp.conditions.append(fnv1.Condition(
        type="PlacementsScheduled",
        status=fnv1.STATUS_CONDITION_TRUE if scheduled else fnv1.STATUS_CONDITION_FALSE,
        reason=sched_reason,
        message=sched_msg,
        target=fnv1.TARGET_COMPOSITE,
    ))

    # Compose an HTTPRoute that aggregates all placements' backends.
    # Backends are composed by ModelPlacement — we read their names from
    # observed ModelPlacement status.
    backend_refs = []
    for env_info in matched:
        placement_key = f"placement-{env_info['name']}"
        observed = req.observed.resources.get(placement_key)
        if observed:
            p_status = resource.struct_to_dict(observed.resource).get("status", {})
            backend_name = p_status.get("routing", {}).get("backendName")
            if backend_name:
                backend_refs.append({
                    "group": "gateway.envoyproxy.io",
                    "kind": "Backend",
                    "name": backend_name,
                    "port": 80,
                    "weight": 1,
                })

    if matched:
        # Rewrite /{ns}/{deployment}/ to /{remote-ns}/{model-name}/.
        # The LLMIS name is the ClusterModel name on all remote clusters,
        # so the rewrite is the same for every backend.
        rewrite_prefix = f"/{_REMOTE_NAMESPACE}/{_to_dns_label(model_name)}/"

        # Gateway parentRef — defaults for Envoy Gateway, could be read
        # from InferenceGateway status in future.
        gw_name = "modelplane"
        gw_ns = "modelplane-system"

        httproute_spec: dict = {
            "parentRefs": [{"name": gw_name, "namespace": gw_ns}],
            "rules": [{
                "matches": [{
                    "path": {
                        "type": "PathPrefix",
                        "value": f"/{xr_ns}/{xr_name}/",
                    },
                }],
                "filters": [{
                    "type": "URLRewrite",
                    "urlRewrite": {
                        "path": {
                            "type": "ReplacePrefixMatch",
                            "replacePrefixMatch": rewrite_prefix,
                        },
                    },
                }],
            }],
        }
        if backend_refs:
            httproute_spec["rules"][0]["backendRefs"] = backend_refs

        resource.update(rsp.desired.resources["httproute"], {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "HTTPRoute",
            "metadata": {"namespace": xr_ns},
            "spec": httproute_spec,
        })

    # Read the control plane gateway address for the unified endpoint URL.
    gateway_ip = None
    if inference_gw:
        gateway_ip = inference_gw.get("status", {}).get("address")

    # Track readiness of composed resources.
    not_ready = []

    placements_ready = 0
    for env_info in matched:
        placement_key = f"placement-{env_info['name']}"
        if _has_condition(req, placement_key, "Ready"):
            rsp.desired.resources[placement_key].ready = fnv1.READY_TRUE
            placements_ready += 1
        else:
            not_ready.append(placement_key)

    route_ready = False
    if "httproute" in rsp.desired.resources:
        # The HTTPRoute is only truly ready when it has backendRefs (not
        # just Accepted). An empty-backendRefs HTTPRoute returns 404.
        route_ready = _has_parent_condition(req, "httproute", "Accepted") and bool(backend_refs)
        if route_ready:
            rsp.desired.resources["httproute"].ready = fnv1.READY_TRUE
        else:
            not_ready.append("httproute")

    # PlacementsReady: all placements are serving traffic.
    all_placements_ready = len(matched) > 0 and placements_ready == len(matched)
    if not matched:
        pr_reason = "NoPlacementsScheduled"
        pr_msg = ""
    elif all_placements_ready:
        pr_reason = "AllPlacementsReady"
        pr_msg = f"{placements_ready} of {len(matched)} ready"
    else:
        pr_reason = "ModelStarting"
        pr_msg = f"{placements_ready} of {len(matched)} ready"

    rsp.conditions.append(fnv1.Condition(
        type="PlacementsReady",
        status=fnv1.STATUS_CONDITION_TRUE if all_placements_ready else fnv1.STATUS_CONDITION_FALSE,
        reason=pr_reason,
        message=pr_msg,
        target=fnv1.TARGET_COMPOSITE,
    ))

    # RoutingReady: the control plane HTTPRoute is configured.
    if not matched:
        rr_reason = "NoPlacementsScheduled"
    elif route_ready:
        rr_reason = "RouteConfigured"
    elif "httproute" in rsp.desired.resources:
        rr_reason = "Configuring"
    else:
        rr_reason = "WaitingForPlacements"

    rsp.conditions.append(fnv1.Condition(
        type="RoutingReady",
        status=fnv1.STATUS_CONDITION_TRUE if route_ready else fnv1.STATUS_CONDITION_FALSE,
        reason=rr_reason,
        target=fnv1.TARGET_COMPOSITE,
    ))

    # Write status for the user.
    status: dict = {
        "model": {"name": resolved_model_name},
        "placements": {"total": len(matched), "ready": placements_ready},
    }
    if gateway_ip:
        status["endpoint"] = {
            "url": f"http://{gateway_ip}/{xr_ns}/{xr_name}/v1/chat/completions",
        }
    resource.update(rsp.desired.composite, {"status": status})

    was_ready = resource.get_condition(
        req.observed.composite.resource, "Ready"
    ).status == "True"

    # Track previous placement count for transition detection.
    prev_ready = int(
        resource.struct_to_dict(req.observed.composite.resource)
        .get("status", {}).get("placements", {}).get("ready", 0)
    )

    if placements_ready > 0 and not not_ready:
        rsp.desired.composite.ready = fnv1.READY_TRUE
        if not was_ready:
            endpoint = status.get("endpoint", {}).get("url", "pending")
            response.normal(
                rsp,
                f"{placements_ready} placements ready, endpoint: {endpoint}",
            )
    else:
        # Explicitly not ready. Without this, an XR with no composed
        # resources (e.g. no environments matched) would be trivially ready.
        rsp.desired.composite.ready = fnv1.READY_FALSE

        # Emit progress transition when placement count changes.
        if placements_ready > prev_ready:
            response.normal(
                rsp, f"{placements_ready} of {len(matched)} placements ready"
            )
        msg_parts = []
        if not_ready:
            msg_parts.append(f"Unready: {', '.join(not_ready)}")
        if len(matched) < desired_envs:
            msg_parts.append(
                f"{len(matched)} of {desired_envs} environments matched"
            )
        response.normal(rsp, "; ".join(msg_parts) if msg_parts else "Waiting")
