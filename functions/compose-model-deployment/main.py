"""Fan out a ModelDeployment to ModelPlacements and configure routing.

This function discovers InferenceEnvironments, matches model requirements
against available capacity, creates a ModelPlacement per matched environment,
and composes Envoy Gateway Backend + HTTPRoute resources on the control plane
for unified endpoint routing.
"""

from crossplane.function import request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from . import scheduling
from .lib import conditions, defaults, metadata, naming
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


class Composer:
    def __init__(self, req, rsp):
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.ModelDeployment(**resource.struct_to_dict(req.observed.composite.resource))

        # Required resources — set by _resolve_inputs.
        self.model = None
        self.envs = []
        self.gateway = None
        self.all_placements = []

    def compose(self):
        if not self.resolve_inputs():
            return
        matched = self.schedule()
        self.compose_placements(matched)
        self.compose_httproute(matched)
        self.write_status(matched)
        self.derive_conditions(matched)

    def resolve_inputs(self):
        """Declare and fetch required resources. Returns False if critical
        inputs are missing."""
        model_kind = self.xr.spec.modelRef.kind
        model_name = self.xr.spec.modelRef.name

        # InferenceEnvironments are matched by the modelplane.ai/environment=true
        # label — a workaround for the empty match_labels protobuf bug.
        env_match_labels: dict[str, str] = {
            metadata.LABEL_KEY_ENVIRONMENT: metadata.LABEL_VALUE_ENVIRONMENT,
        }
        if self.xr.spec.environmentSelector and self.xr.spec.environmentSelector.matchLabels:
            env_match_labels.update(self.xr.spec.environmentSelector.matchLabels)

        response.require_resources(
            self.rsp,
            name="environments",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceEnvironment",
            match_labels=env_match_labels,
        )
        response.require_resources(
            self.rsp,
            name="model",
            api_version="modelplane.ai/v1alpha1",
            kind=model_kind,
            match_name=model_name,
        )
        response.require_resources(
            self.rsp,
            name="inference-gateway",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceGateway",
            match_name="default",
        )
        response.require_resources(
            self.rsp,
            name="all-placements",
            api_version="modelplane.ai/v1alpha1",
            kind="ModelPlacement",
            match_labels={metadata.LABEL_KEY_PLACEMENT: metadata.LABEL_VALUE_PLACEMENT},
        )

        env_dicts = request.get_required_resources(self.req, "environments")
        model_dict = request.get_required_resource(self.req, "model")
        gw_dict = request.get_required_resource(self.req, "inference-gateway")
        placement_dicts = request.get_required_resources(self.req, "all-placements")

        if not env_dicts:
            conditions.set_condition(
                self.rsp,
                CONDITION_TYPE_PLACEMENTS_SCHEDULED,
                False,
                CONDITION_REASON_NO_ENVIRONMENTS,
            )
            response.warning(self.rsp, "No InferenceEnvironments found")
            return False

        if model_dict is None:
            conditions.set_condition(
                self.rsp,
                CONDITION_TYPE_PLACEMENTS_SCHEDULED,
                False,
                CONDITION_REASON_MODEL_NOT_FOUND,
            )
            response.warning(self.rsp, f"Model {model_name} not found")
            return False

        self.envs = [
            defaults.inference_environment(iev1alpha1.InferenceEnvironment.model_validate(e)) for e in env_dicts
        ]
        if model_kind == "Model":
            self.model = defaults.cluster_model(mv1alpha1.ModelModel.model_validate(model_dict))
        else:
            self.model = defaults.cluster_model(cmv1alpha1.ClusterModel.model_validate(model_dict))
        self.gateway = (
            defaults.inference_gateway(igwv1alpha1.InferenceGateway.model_validate(gw_dict)) if gw_dict else None
        )
        self.all_placements = [
            defaults.model_placement(mpv1alpha1.ModelPlacement.model_validate(p)) for p in placement_dicts
        ]

        return True

    def schedule(self):
        """Match model requirements against available environments. Returns the
        list of matched candidates."""
        matched = scheduling.schedule(self.xr, self.model, self.envs, self.all_placements)

        # Transition: emit which environments were matched (first time only).
        if not matched:
            return matched
        prev_placement_count = sum(1 for c in matched if f"placement-{c.name}" in self.req.observed.resources)
        if prev_placement_count == 0:
            matched_names = [c.name for c in matched]
            response.normal(self.rsp, f"Matched {len(matched)} environments: {', '.join(matched_names)}")

        return matched

    def compose_placements(self, matched):
        """Compose a ModelPlacement per matched environment."""
        for env_info in matched:
            placement_key = f"placement-{env_info.name}"

            mp_spec = mpv1alpha1.Spec(
                modelRef=mpv1alpha1.ModelRef(
                    kind=self.xr.spec.modelRef.kind,
                    name=self.xr.spec.modelRef.name,
                ),
                inferenceEnvironmentRef=mpv1alpha1.InferenceEnvironmentRef(
                    name=env_info.name,
                ),
                scaling=self.xr.spec.scaling,
            )

            resource.update(
                self.rsp.desired.resources[placement_key],
                mpv1alpha1.ModelPlacement(
                    metadata=metav1.ObjectMeta(
                        name=naming.placement_name(self.xr.metadata.name, env_info.name),
                        namespace=self.xr.metadata.namespace,
                        labels={
                            metadata.LABEL_KEY_PLACEMENT: metadata.LABEL_VALUE_PLACEMENT,
                            metadata.LABEL_KEY_DEPLOYMENT: self.xr.metadata.name,
                        },
                    ),
                    spec=mp_spec,
                ),
            )

    def compose_httproute(self, matched):
        """Compose an HTTPRoute that load-balances across all placements'
        backends. Backend resources are composed by ModelPlacement — we read
        their names from observed ModelPlacement status."""
        if not matched:
            return

        backend_refs = self.backend_refs(matched)

        # Rewrite /{ns}/{deployment}/ to /{remote-ns}/{model-name}/.
        # The LLMIS name is the ClusterModel name on all remote clusters,
        # so the rewrite is the same for every backend.
        rewrite_prefix = f"/{metadata.NAMESPACE_REMOTE}/{naming.to_dns_label(self.xr.spec.modelRef.name)}/"

        # Gateway parentRef — defaults for Envoy Gateway, could be read
        # from InferenceGateway status in future.
        httproute_spec: dict = {
            "parentRefs": [{"name": metadata.GATEWAY_NAME, "namespace": metadata.NAMESPACE_SYSTEM}],
            "rules": [
                {
                    "matches": [
                        {
                            "path": {
                                "type": "PathPrefix",
                                "value": f"/{self.xr.metadata.namespace}/{self.xr.metadata.name}/",
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
            self.rsp.desired.resources["httproute"],
            {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "HTTPRoute",
                "metadata": {"namespace": self.xr.metadata.namespace},
                "spec": httproute_spec,
            },
        )

    def write_status(self, matched):
        """Write deployment status: model name, placement counts, endpoint."""
        gateway_ip = self.gateway.status.address if self.gateway else None

        placements_ready = sum(1 for c in matched if conditions.has_condition(self.req, f"placement-{c.name}", "Ready"))

        status = v1alpha1.Status(
            model=v1alpha1.Model(name=self.model.spec.model.name),
            placements=v1alpha1.Placements(total=len(matched), ready=placements_ready),
        )
        if gateway_ip:
            status.endpoint = v1alpha1.Endpoint(
                url=f"http://{gateway_ip}/{self.xr.metadata.namespace}/{self.xr.metadata.name}/v1/chat/completions",
            )
        libresource.update_status(self.rsp.desired.composite, status)

    def derive_conditions(self, matched):
        """Derive PlacementsScheduled, PlacementsReady, and RoutingReady
        conditions. Also marks per-placement and httproute readiness."""
        self.derive_placements_scheduled(matched)
        self.derive_placements_ready(matched)
        self.derive_routing_ready(matched)

        # When no placements are scheduled, explicitly mark not ready. Without
        # this, an XR with no composed resources would be trivially ready.
        if not matched:
            self.rsp.desired.composite.ready = fnv1.READY_FALSE

    def derive_placements_scheduled(self, matched):
        """PlacementsScheduled: environments matched and placements created."""
        any_observed = any(f"placement-{c.name}" in self.req.observed.resources for c in matched)
        scheduled = len(matched) > 0 and any_observed

        if not matched:
            reason = CONDITION_REASON_INSUFFICIENT_CAPACITY
            msg = f"0 of {int(self.xr.spec.environments)} environments matched (checked {len(self.envs)})"
        elif scheduled:
            reason = CONDITION_REASON_PLACEMENTS_CREATED
            msg = f"Matched {len(matched)} environments"
        else:
            reason = CONDITION_REASON_SCHEDULING
            msg = ""

        conditions.set_condition(self.rsp, CONDITION_TYPE_PLACEMENTS_SCHEDULED, scheduled, reason, msg)

    def derive_placements_ready(self, matched):
        """PlacementsReady: all placements are serving traffic."""
        placements_ready = 0
        for c in matched:
            placement_key = f"placement-{c.name}"
            if conditions.has_condition(self.req, placement_key, "Ready"):
                self.rsp.desired.resources[placement_key].ready = fnv1.READY_TRUE
                placements_ready += 1

        all_ready = len(matched) > 0 and placements_ready == len(matched)

        if not matched:
            reason = CONDITION_REASON_NO_PLACEMENTS_SCHEDULED
            msg = ""
        elif all_ready:
            reason = CONDITION_REASON_ALL_PLACEMENTS_READY
            msg = f"{placements_ready} of {len(matched)} ready"
        else:
            reason = CONDITION_REASON_MODEL_STARTING
            msg = f"{placements_ready} of {len(matched)} ready"

        conditions.set_condition(self.rsp, CONDITION_TYPE_PLACEMENTS_READY, all_ready, reason, msg)

    def derive_routing_ready(self, matched):
        """RoutingReady: the control plane HTTPRoute is configured and has
        backends."""
        if "httproute" not in self.rsp.desired.resources:
            if not matched:
                reason = CONDITION_REASON_NO_PLACEMENTS_SCHEDULED
            else:
                reason = CONDITION_REASON_WAITING_FOR_PLACEMENTS
            conditions.set_condition(self.rsp, conditions.CONDITION_TYPE_ROUTING_READY, False, reason)
            return

        # The HTTPRoute is only truly ready when it has backendRefs (not just
        # Accepted). An empty-backendRefs HTTPRoute returns 404.
        backend_refs = self.backend_refs(matched)
        route_ready = conditions.has_parent_condition(self.req, "httproute", "Accepted") and bool(backend_refs)

        if route_ready:
            self.rsp.desired.resources["httproute"].ready = fnv1.READY_TRUE

        if not matched:
            reason = CONDITION_REASON_NO_PLACEMENTS_SCHEDULED
        elif route_ready:
            reason = CONDITION_REASON_ROUTE_CONFIGURED
        else:
            reason = CONDITION_REASON_CONFIGURING

        conditions.set_condition(self.rsp, conditions.CONDITION_TYPE_ROUTING_READY, route_ready, reason)

    def backend_refs(self, matched):
        """Read backend names from observed ModelPlacement status."""
        refs = []
        for env_info in matched:
            observed = self.req.observed.resources.get(f"placement-{env_info.name}")
            if not observed:
                continue
            p = mpv1alpha1.ModelPlacement.model_validate(resource.struct_to_dict(observed.resource))
            if not p.status or not p.status.routing:
                continue
            if not p.status.routing.backendName:
                continue
            refs.append(
                {
                    "group": "gateway.envoyproxy.io",
                    "kind": "Backend",
                    "name": p.status.routing.backendName,
                    "port": 80,
                    "weight": 1,
                }
            )
        return refs


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose ModelPlacements and control plane routing resources."""
    Composer(req, rsp).compose()
