"""Compose a Gateway-API HTTPRoute from a ModelService.

ModelService selects ModelEndpoints by label and load-balances across
them. This function fetches the InferenceGateway (for the public
address and parentRef) and the matching ModelEndpoints (for their
backend service names and rewrite paths), then composes a single
HTTPRoute on the control plane.

The match prefix is `/<service-ns>/<service-name>/`. Each endpoint's
rewritePath is attached as a per-backendRef URLRewrite filter so that
endpoints with different path conventions (e.g. composed replicas at
/v1/ alongside external providers at /openai/v1/) each get the correct
path rewrite. This is a Gateway API Extended feature (per-backendRef
filters) supported by Traefik Proxy.
"""

import urllib.parse

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.inferencegateway import v1alpha1 as igwv1alpha1
from models.ai.modelplane.modelendpoint import v1alpha1 as mev1alpha1
from models.ai.modelplane.modelservice import v1alpha1

CONDITION_TYPE_ENDPOINTS_RESOLVED = "EndpointsResolved"
CONDITION_REASON_RESOLVED = "Resolved"
CONDITION_REASON_NO_ENDPOINTS = "NoEndpoints"
CONDITION_REASON_WAITING_FOR_GATEWAY = "WaitingForGateway"
CONDITION_REASON_ROUTE_CONFIGURED = "RouteConfigured"
CONDITION_REASON_CONFIGURING = "Configuring"
CONDITION_TYPE_ROUTING_READY = "RoutingReady"

# The control plane gateway name and namespace. ModelService composes
# HTTPRoutes that reference this gateway as a parentRef.
_GATEWAY_NAME = "modelplane"
_NAMESPACE_SYSTEM = "modelplane-system"

# Scheme for user-facing service URLs.
_GATEWAY_SCHEME = "http"


def _inference_gateway(
    gw: igwv1alpha1.InferenceGateway,
) -> igwv1alpha1.InferenceGateway:
    """Return a copy with status fields defaulted."""
    gw = gw.model_copy(deep=True)
    gw.status = gw.status or igwv1alpha1.Status()
    return gw


def _port_from_url(url: str) -> int:
    """Parse the backend port from a ModelEndpoint URL.

    Defaults to 443 for https and 80 for http when not explicit, matching
    what compose-model-endpoint uses when creating the backend Service.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.port:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def _has_parent_condition(req: fnv1.RunFunctionRequest, name: str, cond: str) -> bool:
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


class FunctionRunner(grpcv1.FunctionRunnerService):
    """A FunctionRunner handles gRPC RunFunctionRequests."""

    def __init__(self):
        """Create a new FunctionRunner."""
        self.log = logging.get_logger()

    async def RunFunction(self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext) -> fnv1.RunFunctionResponse:
        """Run the function."""
        log = self.log.bind(tag=req.meta.tag)
        log.info("Running function")

        rsp = response.to(req)
        c = Composer(req, rsp)
        c.compose()
        return rsp


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
        self.gateway = _inference_gateway(igwv1alpha1.InferenceGateway.model_validate(gw_dict)) if gw_dict else None

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
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_ENDPOINTS_RESOLVED,
                    status="False",
                    reason=CONDITION_REASON_NO_ENDPOINTS,
                    message="No ModelEndpoints matched the configured selectors",
                ),
            )
            response.warning(self.rsp, "No ModelEndpoints matched the configured selectors")
            return False

        ready = sum(1 for ep in self.endpoints if ep.status and ep.status.routing and ep.status.routing.backendName)
        waiting = len(self.endpoints) - ready
        msg = f"Matched {len(self.endpoints)} endpoint(s)"
        if waiting > 0:
            msg += f"; {waiting} waiting for Backend"

        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_ENDPOINTS_RESOLVED,
                status="True",
                reason=CONDITION_REASON_RESOLVED,
                message=msg,
            ),
        )
        return True

    def compose_httproute(self):
        """Compose an HTTPRoute that load-balances across matched endpoints.

        A single rule matches the service prefix and fans out to all
        ready endpoints via weighted backendRefs. Each backendRef carries
        its own URLRewrite filter derived from the endpoint's rewritePath,
        so endpoints with different path conventions are rewritten
        correctly per-backend. This is a Gateway API Extended feature
        supported by Traefik Proxy.
        """
        match_prefix = f"/{self.xr.metadata.namespace}/{self.xr.metadata.name}/"
        match = {"path": {"type": "PathPrefix", "value": match_prefix}}

        backend_refs = []
        for ep in self.endpoints:
            if not ep.status or not ep.status.routing or not ep.status.routing.backendName:
                continue
            # Derive the backend Service port from the endpoint's URL.
            # compose-model-endpoint creates a Service with this port.
            port = _port_from_url(ep.spec.url)
            ref: dict = {
                "name": ep.status.routing.backendName,
                "port": port,
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

        rule: dict = {"matches": [match]}
        if backend_refs:
            rule["backendRefs"] = backend_refs

        resource.update(
            self.rsp.desired.resources["httproute"],
            {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "HTTPRoute",
                "metadata": {"namespace": self.xr.metadata.namespace},
                "spec": {
                    "parentRefs": [{"name": _GATEWAY_NAME, "namespace": _NAMESPACE_SYSTEM}],
                    "rules": [rule],
                },
            },
        )

    def write_status(self):
        status = v1alpha1.Status()
        gateway_ip = self.gateway.status.address if self.gateway else None
        if gateway_ip:
            status.address = f"{_GATEWAY_SCHEME}://{gateway_ip}/{self.xr.metadata.namespace}/{self.xr.metadata.name}"
        resource.update_status(self.rsp.desired.composite, status)

    def derive_conditions(self):
        """RoutingReady: HTTPRoute is composed and Accepted with backends."""
        if "httproute" not in self.rsp.desired.resources:
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_ROUTING_READY,
                    status="False",
                    reason=CONDITION_REASON_WAITING_FOR_GATEWAY,
                ),
            )
            return

        backend_refs_observed = any(
            ep.status and ep.status.routing and ep.status.routing.backendName for ep in self.endpoints
        )
        route_ready = _has_parent_condition(self.req, "httproute", "Accepted") and backend_refs_observed

        if route_ready:
            self.rsp.desired.resources["httproute"].ready = fnv1.READY_TRUE

        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_ROUTING_READY,
                status="True" if route_ready else "False",
                reason=CONDITION_REASON_ROUTE_CONFIGURED if route_ready else CONDITION_REASON_CONFIGURING,
            ),
        )
