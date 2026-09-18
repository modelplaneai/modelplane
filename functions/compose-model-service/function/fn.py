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

"""Compose a ModelRoute per InferenceGateway that serves a ModelService.

A ModelService is one model as a caller sees it. Which gateways serve it is the
gateway's choice, not the service's: an InferenceGateway's serviceSelector
matches the service's labels, and an absent selector matches every service. So
this function reads every InferenceGateway, works out which of them select this
service, and composes a ModelRoute per gateway, pinned to it.

The ModelRoute does the rest: it resolves the endpoints, their credentials and
CAs, and renders the routing objects onto its gateway's cluster. This function
composes onto no remote cluster and reads no ModelEndpoint. Its job is the
fan-out over gateways, and reporting how many of them carry the service.
"""

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.inferencegateway import v1alpha1 as igv1alpha1
from models.ai.modelplane.modelroute import v1alpha1 as mrtv1alpha1
from models.ai.modelplane.modelservice import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# Condition types this function sets on the ModelService.
CONDITION_TYPE_ROUTING_READY = "RoutingReady"

CONDITION_REASON_ROUTES_ACCEPTED = "RoutesAccepted"
CONDITION_REASON_WAITING_FOR_GATEWAYS = "WaitingForGateways"
CONDITION_REASON_NO_GATEWAY = "NoGatewayServesThisService"
CONDITION_REASON_WAITING_FOR_ROUTES = "WaitingForRoutes"

# Stamped on every composed ModelRoute so `kubectl get modelroutes -l
# modelplane.ai/service=<name>` is the per-gateway view of a service.
_LABEL_SERVICE = "modelplane.ai/service"
_LABEL_GATEWAY = "modelplane.ai/gateway"


def _name(meta: metav1.ObjectMeta | None) -> str:
    if meta is None or meta.name is None:
        raise ValueError("metadata.name is unexpectedly absent")
    return meta.name


def _namespace(meta: metav1.ObjectMeta | None) -> str:
    if meta is None or meta.namespace is None:
        raise ValueError("metadata.namespace is unexpectedly absent")
    return meta.namespace


def _labels(meta: metav1.ObjectMeta | None) -> dict[str, str]:
    return dict(meta.labels) if meta and meta.labels else {}


def _serves(gw: igv1alpha1.InferenceGateway, labels: dict[str, str]) -> bool:
    """Whether this gateway's serviceSelector matches a service's labels.

    An absent selector serves every service, which is the default and what a
    single-gateway Modelplane wants.
    """
    sel = gw.spec.serviceSelector
    if sel is None:
        return True
    return all(labels.get(k) == v for k, v in sel.matchLabels.items())


class FunctionRunner(grpcv1.FunctionRunnerServiceServicer):
    """A FunctionRunner handles gRPC RunFunctionRequests."""

    def __init__(self) -> None:
        """Create a new FunctionRunner."""
        self.log = logging.get_logger()

    async def RunFunction(
        self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext | None
    ) -> fnv1.RunFunctionResponse:  # ty: ignore[invalid-method-override]  # the generated grpc servicer base is untyped
        """Run the function."""
        log = self.log.bind(tag=req.meta.tag)
        log.info("Running function")

        rsp = response.to(req)
        Composer(req, rsp).compose()
        return rsp


class Composer:
    def __init__(self, req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse) -> None:
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.ModelService(**resource.struct_to_dict(req.observed.composite.resource))
        self.gateways: list[igv1alpha1.InferenceGateway] = []

    def compose(self) -> None:
        if not self.resolve_gateways():
            self.write_status()
            return
        self.compose_routes()
        self.write_status()
        self.mark_ready()
        self.derive_conditions()

    def mark_ready(self) -> None:
        """Mark each composed ModelRoute ready once its observed counterpart is.

        Nothing else does this. The composition pipeline has no auto-ready
        function, so a desired resource's readiness is whatever the function
        says, and a function that says nothing leaves the XR permanently
        not-Ready however healthy everything under it is.
        """
        for key, res in self.rsp.desired.resources.items():
            if resource.get_condition(self.req.observed.resources.get(key), "Ready").status == "True":
                res.ready = fnv1.READY_TRUE

    def resolve_gateways(self) -> bool:
        """The gateways whose serviceSelector matches this service's labels.

        Returns False, having set conditions, when none does and so no caller can
        reach the service.
        """
        response.require_resources(
            self.rsp,
            name="gateways",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceGateway",
        )
        if "gateways" not in self.req.required_resources:
            self.not_ready(CONDITION_REASON_WAITING_FOR_GATEWAYS, "Waiting for the gateways to resolve")
            return False

        labels = _labels(self.xr.metadata)
        for g in request.get_required_resources(self.req, "gateways"):
            gw = igv1alpha1.InferenceGateway.model_validate(g)
            if _serves(gw, labels):
                self.gateways.append(gw)
        self.gateways.sort(key=lambda gw: _name(gw.metadata))

        if not self.gateways:
            self.not_ready(
                CONDITION_REASON_NO_GATEWAY,
                "No InferenceGateway's serviceSelector matches this service's labels, so no caller can reach it",
            )
            return False
        return True

    def compose_routes(self) -> None:
        """One ModelRoute per serving gateway, pinned to it and carrying a copy
        of this service's endpoint selectors."""
        ns = _namespace(self.xr.metadata)
        svc = _name(self.xr.metadata)
        for gw in self.gateways:
            gateway = _name(gw.metadata)
            route = mrtv1alpha1.ModelRoute(
                metadata=metav1.ObjectMeta(
                    name=resource.child_name(svc, gateway),
                    namespace=ns,
                    labels={_LABEL_SERVICE: svc, _LABEL_GATEWAY: gateway},
                ),
                spec=mrtv1alpha1.Spec(
                    gatewayName=gateway,
                    serviceName=svc,
                    endpoints=[
                        mrtv1alpha1.Endpoint.model_validate(e.model_dump(exclude_unset=True))
                        for e in self.xr.spec.endpoints
                    ],
                ),
            )
            resource.update(self.rsp.desired.resources[f"route-{gateway}"], route)

    def route_ready(self, gw: igv1alpha1.InferenceGateway) -> bool:
        """Whether this gateway's composed ModelRoute reports Ready."""
        key = f"route-{_name(gw.metadata)}"
        return resource.get_condition(self.req.observed.resources.get(key), "Ready").status == "True"

    def gateway_ready(self, gw: igv1alpha1.InferenceGateway) -> bool:
        """Whether the gateway itself is up, i.e. has an address to serve on.

        A gateway still provisioning is excluded from the readiness aggregation
        so it can't fail the service; compose-inference-gateway reports its
        progress on the gateway.
        """
        return bool(gw.status and gw.status.address)

    def write_status(self) -> None:
        """Publish the model name and the ModelRoute counts.

        Per-gateway detail is on the ModelRoutes, not here: `kubectl get
        modelroutes -l modelplane.ai/service=<name>`.
        """
        status = v1alpha1.Status(
            model=f"{_namespace(self.xr.metadata)}/{_name(self.xr.metadata)}",
            routes=v1alpha1.Routes(
                total=len(self.gateways),
                ready=sum(1 for gw in self.gateways if self.route_ready(gw)),
            ),
        )
        resource.update_status(self.rsp.desired.composite, status)

    def not_ready(self, reason: str, message: str) -> None:
        # Mark the composite not-ready explicitly. With no gateway serving the
        # service there are no composed ModelRoutes, and an XR with no composed
        # resources would otherwise aggregate to trivially ready.
        self.rsp.desired.composite.ready = fnv1.READY_FALSE
        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_ROUTING_READY,
                status="False",
                reason=reason,
                message=message,
            ),
        )
        response.normal(self.rsp, message)

    def derive_conditions(self) -> None:
        """RoutingReady once every ready gateway's route has been accepted.

        Every ready gateway, not any: a service reachable through only some of
        the gateways serving it is a residency or capacity problem worth
        surfacing. A gateway that isn't itself ready is left out, so it doesn't
        fail the service while it's still coming up.
        """
        pending = [_name(gw.metadata) for gw in self.gateways if self.gateway_ready(gw) and not self.route_ready(gw)]
        if pending:
            self.not_ready(
                CONDITION_REASON_WAITING_FOR_ROUTES,
                f"Waiting for routes on gateways: {', '.join(pending)}",
            )
            return
        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_ROUTING_READY,
                status="True",
                reason=CONDITION_REASON_ROUTES_ACCEPTED,
            ),
        )
