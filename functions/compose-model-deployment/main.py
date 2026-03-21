import math

from crossplane.function import request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .model.ai.modelplane.modeldeployment import v1alpha1

# Engine/backend compatibility map.
_COMPAT = {
    "KServe": ["vLLM"],
}

# The namespace used for LLMInferenceService on all remote clusters.
_REMOTE_NAMESPACE = "default"


def _has_condition(req: fnv1.RunFunctionRequest, name: str, cond: str) -> bool:
    """Check if an observed composed resource has the given condition True."""
    observed = req.observed.resources.get(name)
    if observed is None:
        return False
    return resource.get_condition(observed.resource, cond).status == "True"


def _has_parent_condition(
    req: fnv1.RunFunctionRequest, name: str, cond: str,
) -> bool:
    """Check a Gateway API condition nested under status.parents[].conditions."""
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
    """Parse a Kubernetes resource quantity to bytes."""
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
    """Derive a deterministic ModelPlacement name from the deployment and env."""
    return f"{deployment_name}-{ie_name}"[:63]


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    xr = v1alpha1.ModelDeployment(
        **resource.struct_to_dict(req.observed.composite.resource)
    )

    model_kind = xr.spec.modelRef.kind or "ClusterModel"
    model_name = xr.spec.modelRef.name
    # Protobuf Struct delivers all numbers as float.
    desired_envs = int(xr.spec.environments)
    env_selector = xr.spec.environmentSelector
    xr_name = xr.metadata.name
    xr_ns = xr.metadata.namespace or ""

    # Always declare required resources.
    # Match on modelplane.ai/environment=true to discover IEs. This label must
    # be applied by the platform team when creating InferenceEnvironments. We
    # can't use an empty match_labels selector (Crossplane protobuf bug — see
    # build log) or function-set labels (Crossplane doesn't apply metadata
    # changes from functions to the XR).
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

    # Read required resources. These are dicts (not Pydantic models) because
    # they're external resources resolved by Crossplane, not the XR itself.
    envs = request.get_required_resources(req, "environments")
    model = request.get_required_resource(req, "model")
    inference_gw = request.get_required_resource(req, "inference-gateway")
    all_placements: list = []  # Capacity tracking deferred — MVP has one env

    if not envs:
        rsp.conditions.append(fnv1.Condition(
            type="Ready",
            status=fnv1.STATUS_CONDITION_FALSE,
            reason="NoEnvironments",
            message="No InferenceEnvironments found",
            target=fnv1.TARGET_COMPOSITE_AND_CLAIM,
        ))
        return

    if model is None:
        rsp.conditions.append(fnv1.Condition(
            type="Ready",
            status=fnv1.STATUS_CONDITION_FALSE,
            reason="ModelNotFound",
            message=f"Model {model_name} not found",
            target=fnv1.TARGET_COMPOSITE_AND_CLAIM,
        ))
        return

    model_spec = model.get("spec", {})
    resolved_model_name = model_spec.get("model", {}).get("name", "")
    engine = model_spec.get("engine", "")
    model_vram = model_spec.get("resources", {}).get("vram", "0Gi")
    model_vram_bytes = _parse_quantity(model_vram)

    # Schedule: filter and rank environments.
    candidates = []
    for env in envs:
        env_name = env.get("metadata", {}).get("name", "")
        env_status = env.get("status", {})
        capacity = env_status.get("capacity", {})
        backend = capacity.get("backend", "")

        # Engine compatibility.
        if engine not in _COMPAT.get(backend, []):
            continue

        # VRAM and capacity.
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

        # Check available capacity from existing placements.
        used_gpus = 0
        for p in all_placements:
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

    # Sort by name for determinism and take first N.
    candidates.sort(key=lambda c: c["name"])
    matched = candidates[:desired_envs]

    # Compose a ModelPlacement for each matched environment.
    for env_info in matched:
        ie_name = env_info["name"]
        placement_key = f"placement-{ie_name}"
        pname = _placement_name(xr_name, ie_name)

        resource.update(rsp.desired.resources[placement_key], {
            "apiVersion": "modelplane.ai/v1alpha1",
            "kind": "ModelPlacement",
            "metadata": {
                "name": pname,
                "namespace": xr_ns,
                "labels": {"modelplane.ai/deployment": xr_name},
            },
            "spec": {
                "modelRef": {"kind": model_kind, "name": model_name},
                "inferenceEnvironmentRef": {"name": ie_name},
            },
        })

    # Compose routing resources on the control plane.
    backend_refs = []
    for env_info in matched:
        ie_name = env_info["name"]
        gw_addr = env_info.get("gateway_address")
        if not gw_addr:
            continue

        backend_key = f"backend-{ie_name}"
        resource.update(rsp.desired.resources[backend_key], {
            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
            "kind": "Backend",
            "metadata": {"namespace": xr_ns},
            "spec": {
                "endpoints": [{"ip": {"address": gw_addr, "port": 80}}],
            },
        })

        # Read the Crossplane-generated name from an observed Backend.
        backend_observed = req.observed.resources.get(backend_key)
        if backend_observed:
            d = resource.struct_to_dict(backend_observed.resource)
            backend_name = d.get("metadata", {}).get("name")
            if backend_name:
                backend_refs.append({
                    "group": "gateway.envoyproxy.io",
                    "kind": "Backend",
                    "name": backend_name,
                    "port": 80,
                    "weight": 1,
                })

    # Compose an HTTPRoute.
    if matched:
        first_pname = _placement_name(xr_name, matched[0]["name"])
        rewrite_prefix = f"/{_REMOTE_NAMESPACE}/{first_pname}/"

        # Read gateway name/namespace from InferenceGateway status.
        gw_name = "modelplane"
        gw_ns = "modelplane-system"
        if inference_gw:
            gw_status = inference_gw.get("status", {}).get("gateway", {})
            gw_name = gw_status.get("name", gw_name)
            gw_ns = gw_status.get("namespace", gw_ns)

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

    # Read the control plane gateway address from InferenceGateway status.
    gateway_ip = None
    if inference_gw:
        gateway_ip = (
            inference_gw.get("status", {}).get("gateway", {}).get("address")
        )

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

    for env_info in matched:
        backend_key = f"backend-{env_info['name']}"
        if backend_key in rsp.desired.resources:
            if _has_condition(req, backend_key, "Accepted"):
                rsp.desired.resources[backend_key].ready = fnv1.READY_TRUE
            elif _has_condition(req, backend_key, "Invalid"):
                not_ready.append(backend_key)
            elif backend_key not in req.observed.resources:
                not_ready.append(backend_key)
            else:
                rsp.desired.resources[backend_key].ready = fnv1.READY_TRUE

    if "httproute" in rsp.desired.resources:
        if _has_parent_condition(req, "httproute", "Accepted"):
            rsp.desired.resources["httproute"].ready = fnv1.READY_TRUE
        else:
            not_ready.append("httproute")

    # Write status.
    status: dict = {
        "model": {"name": resolved_model_name},
        "placements": {"total": len(matched), "ready": placements_ready},
    }
    if gateway_ip:
        status["endpoint"] = {
            "url": f"http://{gateway_ip}/{xr_ns}/{xr_name}/v1/chat/completions",
        }
    resource.update(rsp.desired.composite, {"status": status})

    # Set Ready condition.
    if placements_ready > 0 and not not_ready:
        rsp.conditions.append(fnv1.Condition(
            type="Ready",
            status=fnv1.STATUS_CONDITION_TRUE,
            reason="PlacementsAvailable",
            target=fnv1.TARGET_COMPOSITE_AND_CLAIM,
        ))
    else:
        msg_parts = []
        if not_ready:
            msg_parts.append(f"Unready: {', '.join(not_ready)}")
        if len(matched) < desired_envs:
            msg_parts.append(
                f"{len(matched)} of {desired_envs} environments matched"
            )
        rsp.conditions.append(fnv1.Condition(
            type="Ready",
            status=fnv1.STATUS_CONDITION_FALSE,
            reason="Creating",
            message="; ".join(msg_parts) if msg_parts else "Waiting",
            target=fnv1.TARGET_COMPOSITE_AND_CLAIM,
        ))
