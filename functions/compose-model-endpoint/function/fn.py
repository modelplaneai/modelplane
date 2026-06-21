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

"""Compose a Kubernetes Service and EndpointSlice from a ModelEndpoint.

ModelEndpoint is a reachable inference endpoint. This function parses
spec.url and composes a selectorless Service plus a manually-managed
EndpointSlice on the control plane pointing at the URL's host:port.
ModelService reads the resulting service name from
status.routing.backendName to build its HTTPRoute.

For IPv4 URLs (e.g. workload cluster gateways) the EndpointSlice uses
addressType IPv4; for IPv6 URLs, IPv6; for FQDN URLs (e.g. external
SaaS providers like Together or Groq), FQDN.
"""

import ipaddress
import urllib.parse

import grpc
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.modelendpoint import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

SERVICE_RESOURCE_KEY = "service"
ENDPOINTSLICE_RESOURCE_KEY = "endpointslice"

# Condition type shared with compose-model-service. Both functions write
# RoutingReady to signal whether traffic can reach the endpoint.
CONDITION_TYPE_ROUTING_READY = "RoutingReady"
CONDITION_REASON_BACKEND_CONFIGURED = "BackendConfigured"
CONDITION_REASON_WAITING_FOR_BACKEND = "WaitingForBackend"
CONDITION_REASON_INVALID_URL = "InvalidURL"


def _namespace(meta: metav1.ObjectMeta | None) -> str:
    """The object's namespace, always set on namespaced resources read from the API server."""
    if meta is None or meta.namespace is None:
        raise ValueError("metadata.namespace is unexpectedly absent")
    return meta.namespace


def _address_type(host: str) -> str:
    """Return the EndpointSlice addressType for a host: IPv4, IPv6, or FQDN."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return "FQDN"
    return "IPv6" if isinstance(addr, ipaddress.IPv6Address) else "IPv4"


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
        c = Composer(req, rsp)
        c.compose()
        return rsp


class Composer:
    def __init__(self, req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse) -> None:
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.ModelEndpoint(**resource.struct_to_dict(req.observed.composite.resource))

    def compose(self) -> None:
        parsed = self.parse_url()
        if parsed is None:
            return
        host, port = parsed

        self.compose_backend(host, port, _address_type(host))
        self.write_status()
        self.derive_conditions()

    def parse_url(self) -> tuple[str, int] | None:
        """Parse spec.url into (host, port), or None (marking the XR not-ready)
        if the URL is invalid."""
        parsed = urllib.parse.urlparse(self.xr.spec.url)
        if not parsed.hostname:
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_ROUTING_READY,
                    status="False",
                    reason=CONDITION_REASON_INVALID_URL,
                    message=f"spec.url has no host: {self.xr.spec.url}",
                ),
            )
            response.warning(self.rsp, f"Invalid spec.url: {self.xr.spec.url}")
            return None

        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.hostname, port

    def compose_backend(self, host: str, port: int, address_type: str) -> None:
        """Compose a selectorless Service and EndpointSlice for the endpoint.

        The Service has no selector (Kubernetes will not auto-populate
        EndpointSlices for it) so we compose the EndpointSlice ourselves.
        The kubernetes.io/service-name label associates the slice with
        the Service. The slice is gated on the Service being observed
        because Crossplane generates the Service's name.

        ExternalName Services aren't an option for FQDN endpoints:
        Traefik's Gateway API provider explicitly rejects them. See
        https://github.com/traefik/traefik/blob/fa49e2bcad7ffd8a80accdf1fae1ae480913d93d/pkg/provider/kubernetes/gateway/kubernetes.go#L890.
        """
        ns = _namespace(self.xr.metadata)

        resource.update(
            self.rsp.desired.resources[SERVICE_RESOURCE_KEY],
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"namespace": ns},
                "spec": {
                    "ports": [{"port": port, "protocol": "TCP"}],
                },
            },
        )

        svc_observed = self.req.observed.resources.get(SERVICE_RESOURCE_KEY)
        svc_name = (
            resource.struct_to_dict(svc_observed.resource).get("metadata", {}).get("name") if svc_observed else None
        )
        if svc_name:
            resource.update(
                self.rsp.desired.resources[ENDPOINTSLICE_RESOURCE_KEY],
                {
                    "apiVersion": "discovery.k8s.io/v1",
                    "kind": "EndpointSlice",
                    "metadata": {
                        "namespace": ns,
                        "labels": {"kubernetes.io/service-name": svc_name},
                    },
                    "addressType": address_type,
                    "ports": [{"name": "", "port": port, "protocol": "TCP"}],
                    # Traefik's Gateway API provider skips endpoints
                    # whose ready condition is nil, contradicting the
                    # Kubernetes spec which says nil should be
                    # interpreted as true. See
                    # https://github.com/traefik/traefik/blob/fa49e2bcad7ffd8a80accdf1fae1ae480913d93d/pkg/provider/kubernetes/gateway/kubernetes.go#L948.
                    "endpoints": [
                        {
                            "addresses": [host],
                            "conditions": {"ready": True},
                        }
                    ],
                },
            )

    def write_status(self) -> None:
        """Surface the composed Service's name in status, but only once the
        EndpointSlice is observed too. ModelService treats backendName as
        routable, so we must not advertise it until the backing endpoint
        exists or Traefik will report ResolvedRefs=False until the next
        reconcile catches up."""
        status = v1alpha1.Status()

        svc_observed = self.req.observed.resources.get(SERVICE_RESOURCE_KEY)
        slice_observed = ENDPOINTSLICE_RESOURCE_KEY in self.req.observed.resources
        if svc_observed and slice_observed:
            svc_name = resource.struct_to_dict(svc_observed.resource).get("metadata", {}).get("name")
            if svc_name:
                status.routing = v1alpha1.Routing(backendName=svc_name)

        resource.update_status(self.rsp.desired.composite, status)

    def derive_conditions(self) -> None:
        """RoutingReady: both the Service and the EndpointSlice have been
        observed on the control plane."""
        svc_exists = SERVICE_RESOURCE_KEY in self.req.observed.resources
        slice_exists = ENDPOINTSLICE_RESOURCE_KEY in self.req.observed.resources
        ready = svc_exists and slice_exists
        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_ROUTING_READY,
                status="True" if ready else "False",
                reason=CONDITION_REASON_BACKEND_CONFIGURED if ready else CONDITION_REASON_WAITING_FOR_BACKEND,
            ),
        )
        if svc_exists:
            self.rsp.desired.resources[SERVICE_RESOURCE_KEY].ready = fnv1.READY_TRUE
        if ready:
            self.rsp.desired.resources[ENDPOINTSLICE_RESOURCE_KEY].ready = fnv1.READY_TRUE
