"""Fan out a ModelDeployment to ModelPlacements and configure routing.

This function discovers InferenceEnvironments, matches model requirements
against available capacity, creates a ModelPlacement per matched environment,
and composes Envoy Gateway Backend + HTTPRoute resources on the control plane
for unified endpoint routing.
"""

from crossplane.function import request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from . import scheduling
from .lib import conditions
from .lib import defaults
from .lib import metadata
from .lib import naming
from .lib import resource as libresource
from .model.ai.modelplane.clustermodel import v1alpha1 as cmv1alpha1
from .model.ai.modelplane.inferenceenvironment import v1alpha1 as iev1alpha1
from .model.ai.modelplane.inferencegateway import v1alpha1 as igwv1alpha1
from .model.ai.modelplane.model import v1alpha1 as mv1alpha1
from .model.ai.modelplane.modeldeployment import v1alpha1
from .model.ai.modelplane.modelplacement import v1alpha1 as mpv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# Condition types and reasons for the ModelDeployment XR.
CONDITION_TYPE_PLACEMENTS_SCHEDULED = "PlacementsScheduled"
CONDITION_TYPE_PLACEMENTS_READY = "PlacementsReady"

CONDITION_REASON_NO_ENVIRONMENTS = "NoEnvironments"
CONDITION_REASON_MODEL_NOT_FOUND = "ModelNotFound"
CONDITION_REASON_INSUFFICIENT_CAPACITY = "InsufficientCapacity"
CONDITION_REASON_PLACEMENTS_CREATED = "PlacementsCreated"
CONDITION_REASON_SCHEDULING = "Scheduling"
CONDITION_REASON_NO_PLACEMENTS_SCHEDULED = "NoPlacementsScheduled"
CONDITION_REASON_ALL_PLACEMENTS_READY = "AllPlacementsReady"
CONDITION_REASON_MODEL_STARTING = "ModelStarting"
CONDITION_REASON_ROUTE_CONFIGURED = "RouteConfigured"
CONDITION_REASON_CONFIGURING = "Configuring"
CONDITION_REASON_WAITING_FOR_PLACEMENTS = "WaitingForPlacements"


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose ModelPlacements and control plane routing resources."""
    xr = v1alpha1.ModelDeployment(
        **resource.struct_to_dict(req.observed.composite.resource)
    )

    model_kind = xr.spec.modelRef.kind
    model_name = xr.spec.modelRef.name
    xr_name = xr.metadata.name
    xr_ns = xr.metadata.namespace

    # Declare required resources. InferenceEnvironments are matched by the
    # modelplane.ai/environment=true label — a workaround for the empty
    # match_labels protobuf bug (see build log).
    env_match_labels: dict[str, str] = {
        metadata.LABEL_KEY_ENVIRONMENT: metadata.LABEL_VALUE_ENVIRONMENT,
    }
    if xr.spec.environmentSelector and xr.spec.environmentSelector.matchLabels:
        env_match_labels.update(xr.spec.environmentSelector.matchLabels)

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
        match_labels={metadata.LABEL_KEY_PLACEMENT: metadata.LABEL_VALUE_PLACEMENT},
    )

    env_dicts = request.get_required_resources(req, "environments")
    model_dict = request.get_required_resource(req, "model")
    gw_dict = request.get_required_resource(req, "inference-gateway")
    placement_dicts = request.get_required_resources(req, "all-placements")

    if not env_dicts:
        conditions.set_condition(
            rsp,
            CONDITION_TYPE_PLACEMENTS_SCHEDULED,
            False,
            CONDITION_REASON_NO_ENVIRONMENTS,
        )
        response.warning(rsp, "No InferenceEnvironments found")
        return

    if model_dict is None:
        conditions.set_condition(
            rsp,
            CONDITION_TYPE_PLACEMENTS_SCHEDULED,
            False,
            CONDITION_REASON_MODEL_NOT_FOUND,
        )
        response.warning(rsp, f"Model {model_name} not found")
        return

    envs = [
        defaults.inference_environment(
            iev1alpha1.InferenceEnvironment.model_validate(e)
        )
        for e in env_dicts
    ]
    if model_kind == "Model":
        model_resource = defaults.cluster_model(
            mv1alpha1.ModelModel.model_validate(model_dict)
        )
    else:
        model_resource = defaults.cluster_model(
            cmv1alpha1.ClusterModel.model_validate(model_dict)
        )
    inference_gw = (
        defaults.inference_gateway(igwv1alpha1.InferenceGateway.model_validate(gw_dict))
        if gw_dict
        else None
    )
    all_placements = [
        defaults.model_placement(mpv1alpha1.ModelPlacement.model_validate(p))
        for p in placement_dicts
    ]

    matched = scheduling.schedule(xr, model_resource, envs, all_placements)

    # Transition: emit which environments were matched (first time only).
    matched_names = [c.name for c in matched]
    prev_placement_count = sum(
        1 for c in matched if f"placement-{c.name}" in req.observed.resources
    )
    if matched and prev_placement_count == 0:
        response.normal(
            rsp, f"Matched {len(matched)} environments: {', '.join(matched_names)}"
        )

    # Compose a ModelPlacement per matched environment.
    for env_info in matched:
        ie_name = env_info.name
        placement_key = f"placement-{ie_name}"
        pname = naming.placement_name(xr_name, ie_name)

        resource.update(
            rsp.desired.resources[placement_key],
            mpv1alpha1.ModelPlacement(
                metadata=metav1.ObjectMeta(
                    name=pname,
                    namespace=xr_ns,
                    labels={
                        metadata.LABEL_KEY_PLACEMENT: metadata.LABEL_VALUE_PLACEMENT,
                        metadata.LABEL_KEY_DEPLOYMENT: xr_name,
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
        f"placement-{c.name}" in req.observed.resources for c in matched
    )
    scheduled = len(matched) > 0 and any_placements_observed

    if not matched:
        sched_reason = CONDITION_REASON_INSUFFICIENT_CAPACITY
        sched_msg = f"0 of {int(xr.spec.environments)} environments matched (checked {len(envs)})"
    elif scheduled:
        sched_reason = CONDITION_REASON_PLACEMENTS_CREATED
        sched_msg = f"Matched {len(matched)} environments"
    else:
        sched_reason = CONDITION_REASON_SCHEDULING
        sched_msg = ""

    conditions.set_condition(
        rsp, CONDITION_TYPE_PLACEMENTS_SCHEDULED, scheduled, sched_reason, sched_msg
    )

    # Compose an HTTPRoute that aggregates all placements' backends.
    # Backends are composed by ModelPlacement — we read their names from
    # observed ModelPlacement status.
    backend_refs = []
    for env_info in matched:
        observed = req.observed.resources.get(f"placement-{env_info.name}")
        if observed:
            p = mpv1alpha1.ModelPlacement.model_validate(
                resource.struct_to_dict(observed.resource)
            )
            backend_name = (
                p.status.routing.backendName if p.status and p.status.routing else None
            )
            if backend_name:
                backend_refs.append(
                    {
                        "group": "gateway.envoyproxy.io",
                        "kind": "Backend",
                        "name": backend_name,
                        "port": 80,
                        "weight": 1,
                    }
                )

    if matched:
        # Rewrite /{ns}/{deployment}/ to /{remote-ns}/{model-name}/.
        # The LLMIS name is the ClusterModel name on all remote clusters,
        # so the rewrite is the same for every backend.
        rewrite_prefix = (
            f"/{metadata.NAMESPACE_REMOTE}/{naming.to_dns_label(model_name)}/"
        )

        # Gateway parentRef — defaults for Envoy Gateway, could be read
        # from InferenceGateway status in future.
        gw_name = metadata.GATEWAY_NAME
        gw_ns = metadata.NAMESPACE_SYSTEM

        httproute_spec: dict = {
            "parentRefs": [{"name": gw_name, "namespace": gw_ns}],
            "rules": [
                {
                    "matches": [
                        {
                            "path": {
                                "type": "PathPrefix",
                                "value": f"/{xr_ns}/{xr_name}/",
                            },
                        }
                    ],
                    "filters": [
                        {
                            "type": "URLRewrite",
                            "urlRewrite": {
                                "path": {
                                    "type": "ReplacePrefixMatch",
                                    "replacePrefixMatch": rewrite_prefix,
                                },
                            },
                        }
                    ],
                }
            ],
        }
        if backend_refs:
            httproute_spec["rules"][0]["backendRefs"] = backend_refs

        resource.update(
            rsp.desired.resources["httproute"],
            {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "HTTPRoute",
                "metadata": {"namespace": xr_ns},
                "spec": httproute_spec,
            },
        )

    # Read the control plane gateway address for the unified endpoint URL.
    gateway_ip = inference_gw.status.address if inference_gw else None

    # Track per-resource readiness. Crossplane derives the XR's Ready
    # condition automatically from composed resource readiness.
    placements_ready = 0
    for env_info in matched:
        placement_key = f"placement-{env_info.name}"
        if conditions.has_condition(req, placement_key, "Ready"):
            rsp.desired.resources[placement_key].ready = fnv1.READY_TRUE
            placements_ready += 1

    route_ready = False
    if "httproute" in rsp.desired.resources:
        # The HTTPRoute is only truly ready when it has backendRefs (not
        # just Accepted). An empty-backendRefs HTTPRoute returns 404.
        route_ready = conditions.has_parent_condition(
            req, "httproute", "Accepted"
        ) and bool(backend_refs)
        if route_ready:
            rsp.desired.resources["httproute"].ready = fnv1.READY_TRUE

    # PlacementsReady: all placements are serving traffic.
    all_placements_ready = len(matched) > 0 and placements_ready == len(matched)
    if not matched:
        pr_reason = CONDITION_REASON_NO_PLACEMENTS_SCHEDULED
        pr_msg = ""
    elif all_placements_ready:
        pr_reason = CONDITION_REASON_ALL_PLACEMENTS_READY
        pr_msg = f"{placements_ready} of {len(matched)} ready"
    else:
        pr_reason = CONDITION_REASON_MODEL_STARTING
        pr_msg = f"{placements_ready} of {len(matched)} ready"

    conditions.set_condition(
        rsp, CONDITION_TYPE_PLACEMENTS_READY, all_placements_ready, pr_reason, pr_msg
    )

    # RoutingReady: the control plane HTTPRoute is configured.
    if not matched:
        rr_reason = CONDITION_REASON_NO_PLACEMENTS_SCHEDULED
    elif route_ready:
        rr_reason = CONDITION_REASON_ROUTE_CONFIGURED
    elif "httproute" in rsp.desired.resources:
        rr_reason = CONDITION_REASON_CONFIGURING
    else:
        rr_reason = CONDITION_REASON_WAITING_FOR_PLACEMENTS

    conditions.set_condition(
        rsp, conditions.CONDITION_TYPE_ROUTING_READY, route_ready, rr_reason
    )

    # Write status for the user.
    status = v1alpha1.Status(
        model=v1alpha1.Model(name=model_resource.spec.model.name),
        placements=v1alpha1.Placements(total=len(matched), ready=placements_ready),
    )
    if gateway_ip:
        status.endpoint = v1alpha1.Endpoint(
            url=f"http://{gateway_ip}/{xr_ns}/{xr_name}/v1/chat/completions",
        )
    libresource.update_status(rsp.desired.composite, status)

    # When no placements are scheduled, explicitly mark not ready. Without
    # this, an XR with no composed resources would be trivially ready.
    if not matched:
        rsp.desired.composite.ready = fnv1.READY_FALSE
