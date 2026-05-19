"""Compose a Gateway-API HTTPRoute from a ModelService.

ModelService selects ModelEndpoints by label and load-balances across
them. This function fetches the InferenceGateway (for the public
address and parentRef) and the matching ModelEndpoints (for their
backend names and rewrite paths), then composes a single HTTPRoute on
the control plane.

The match prefix is `/<service-ns>/<service-name>/`. Each backendRef
carries its own URLRewrite filter derived from the endpoint's
spec.rewritePath, so endpoints from different deployments or external
providers can coexist with different rewrite targets.
"""

from crossplane.function import request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .lib import conditions, defaults, metadata
from .lib import resource as libresource
from .model.ai.modelplane.inferencegateway import v1alpha1 as igwv1alpha1
from .model.ai.modelplane.modelendpoint import v1alpha1 as mev1alpha1
from .model.ai.modelplane.modelservice import v1alpha1

CONDITION_TYPE_ENDPOINTS_RESOLVED = "EndpointsResolved"
CONDITION_REASON_RESOLVED = "Resolved"
CONDITION_REASON_NO_ENDPOINTS = "NoEndpoints"
CONDITION_REASON_WAITING_FOR_GATEWAY = "WaitingForGateway"
CONDITION_REASON_ROUTE_CONFIGURED = "RouteConfigured"
CONDITION_REASON_CONFIGURING = "Configuring"


class Composer:
    def __init__(self, req, rsp):
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.ModelService(**resource.struct_to_dict(req.observed.composite.resource))
        self.gateway = None
        self.endpoints: list[mev1alpha1.ModelEndpoint] = []

    def compose(self):
        if not self.resolve_inputs():
            return
        self.compose_httproute()
        self.write_status()
        self.derive_conditions()

    def resolve_inputs(self) -> bool:
        """Fetch the InferenceGateway and matching ModelEndpoints."""
        response.require_resources(
            self.rsp,
            name="inference-gateway",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceGateway",
            match_name="default",
        )

        # One required-resources request per spec.endpoints[i] entry.
        for i, entry in enumerate(self.xr.spec.endpoints):
            response.require_resources(
                self.rsp,
                name=f"endpoints-{i}",
                api_version="modelplane.ai/v1alpha1",
                kind="ModelEndpoint",
                match_labels=entry.selector.matchLabels,
            )

        gw_dict = request.get_required_resource(self.req, "inference-gateway")
        self.gateway = (
            defaults.inference_gateway(igwv1alpha1.InferenceGateway.model_validate(gw_dict)) if gw_dict else None
        )

        # Gather matched endpoints from every selector entry.
        seen_names: set[str] = set()
        for i in range(len(self.xr.spec.endpoints)):
            for d in request.get_required_resources(self.req, f"endpoints-{i}") or []:
                ep = mev1alpha1.ModelEndpoint.model_validate(d)
                key = f"{ep.metadata.namespace}/{ep.metadata.name}"
                if key in seen_names:
                    continue
                seen_names.add(key)
                self.endpoints.append(ep)

        if not self.endpoints:
            conditions.set_condition(
                self.rsp,
                CONDITION_TYPE_ENDPOINTS_RESOLVED,
                False,
                CONDITION_REASON_NO_ENDPOINTS,
                "No ModelEndpoints matched the configured selectors",
            )
            response.warning(self.rsp, "No ModelEndpoints matched the configured selectors")
            return False

        ready = sum(1 for ep in self.endpoints if ep.status and ep.status.routing and ep.status.routing.backendName)
        waiting = len(self.endpoints) - ready
        msg = f"Matched {len(self.endpoints)} endpoint(s)"
        if waiting > 0:
            msg += f"; {waiting} waiting for Backend"

        conditions.set_condition(
            self.rsp,
            CONDITION_TYPE_ENDPOINTS_RESOLVED,
            True,
            CONDITION_REASON_RESOLVED,
            msg,
        )
        return True

    def compose_httproute(self):
        """Compose an HTTPRoute that load-balances across matched endpoints.

        Each backendRef carries its own URLRewrite filter so endpoints
        with different rewritePaths (e.g. composed replicas vs. external
        SaaS providers) are rewritten correctly per-backend.
        """
        backend_refs = []
        for ep in self.endpoints:
            if not ep.status or not ep.status.routing or not ep.status.routing.backendName:
                continue
            ref: dict = {
                "group": "gateway.envoyproxy.io",
                "kind": "Backend",
                "name": ep.status.routing.backendName,
                "port": 80,
                "weight": 1,
            }
            if ep.spec.rewritePath:
                ref["filters"] = [
                    {
                        "type": "URLRewrite",
                        "urlRewrite": {
                            "path": {
                                "type": "ReplacePrefixMatch",
                                "replacePrefixMatch": ep.spec.rewritePath,
                            },
                        },
                    }
                ]
            backend_refs.append(ref)

        match_prefix = f"/{self.xr.metadata.namespace}/{self.xr.metadata.name}/"

        rule: dict = {
            "matches": [
                {
                    "path": {
                        "type": "PathPrefix",
                        "value": match_prefix,
                    },
                }
            ],
        }
        if backend_refs:
            rule["backendRefs"] = backend_refs

        httproute_spec = {
            "parentRefs": [{"name": metadata.GATEWAY_NAME, "namespace": metadata.NAMESPACE_SYSTEM}],
            "rules": [rule],
        }

        resource.update(
            self.rsp.desired.resources["httproute"],
            {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "HTTPRoute",
                "metadata": {"namespace": self.xr.metadata.namespace},
                "spec": httproute_spec,
            },
        )

    def write_status(self):
        status = v1alpha1.Status()
        gateway_ip = self.gateway.status.address if self.gateway else None
        if gateway_ip:
            status.address = (
                f"{metadata.GATEWAY_SCHEME}://{gateway_ip}/{self.xr.metadata.namespace}/{self.xr.metadata.name}"
            )
        libresource.update_status(self.rsp.desired.composite, status)

    def derive_conditions(self):
        """RoutingReady: HTTPRoute is composed and Accepted with backends."""
        if "httproute" not in self.rsp.desired.resources:
            conditions.set_condition(
                self.rsp,
                conditions.CONDITION_TYPE_ROUTING_READY,
                False,
                CONDITION_REASON_WAITING_FOR_GATEWAY,
            )
            return

        backend_refs_observed = any(
            ep.status and ep.status.routing and ep.status.routing.backendName for ep in self.endpoints
        )
        route_ready = conditions.has_parent_condition(self.req, "httproute", "Accepted") and backend_refs_observed

        if route_ready:
            self.rsp.desired.resources["httproute"].ready = fnv1.READY_TRUE

        conditions.set_condition(
            self.rsp,
            conditions.CONDITION_TYPE_ROUTING_READY,
            route_ready,
            CONDITION_REASON_ROUTE_CONFIGURED if route_ready else CONDITION_REASON_CONFIGURING,
        )


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose an HTTPRoute from a ModelService."""
    Composer(req, rsp).compose()
