"""Compose an Envoy Backend from a ModelEndpoint.

ModelEndpoint is a reachable inference endpoint. This function parses
spec.url and composes an Envoy Gateway Backend on the control plane
pointing at the URL's host:port. ModelService reads the resulting
backend name from status.routing.backendName to build its HTTPRoute.

External / SaaS endpoints (fqdn-style Backends) are deferred. For now,
spec.url is expected to be an http://<ip>:<port>/... shape.
"""

import urllib.parse

from crossplane.function import resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .lib import conditions
from .lib import resource as libresource
from .model.ai.modelplane.modelendpoint import v1alpha1

CONDITION_REASON_AVAILABLE = "Available"
CONDITION_REASON_INVALID_URL = "InvalidURL"
CONDITION_REASON_BACKEND_CONFIGURED = "BackendConfigured"
CONDITION_REASON_WAITING_FOR_BACKEND = "WaitingForBackend"

BACKEND_RESOURCE_KEY = "backend"


class Composer:
    def __init__(self, req, rsp):
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.ModelEndpoint(**resource.struct_to_dict(req.observed.composite.resource))

    def compose(self):
        host, port = self.parse_url()
        if host is None:
            return

        self.compose_backend(host, port)
        self.write_status()
        self.derive_conditions()

    def parse_url(self):
        """Parse spec.url into (host, port). Returns (None, None) and
        marks the XR not-ready if the URL is invalid."""
        parsed = urllib.parse.urlparse(self.xr.spec.url)
        if not parsed.hostname:
            conditions.set_condition(
                self.rsp,
                conditions.CONDITION_TYPE_ROUTING_READY,
                False,
                CONDITION_REASON_INVALID_URL,
                f"spec.url has no host: {self.xr.spec.url}",
            )
            response.warning(self.rsp, f"Invalid spec.url: {self.xr.spec.url}")
            return None, None

        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.hostname, port

    def compose_backend(self, host: str, port: int):
        """Compose an Envoy Gateway Backend on the control plane."""
        resource.update(
            self.rsp.desired.resources[BACKEND_RESOURCE_KEY],
            {
                "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                "kind": "Backend",
                "metadata": {"namespace": self.xr.metadata.namespace},
                "spec": {
                    "endpoints": [{"ip": {"address": host, "port": port}}],
                },
            },
        )

    def write_status(self):
        """Surface the composed Backend's name in status."""
        status = v1alpha1.Status()

        backend_observed = self.req.observed.resources.get(BACKEND_RESOURCE_KEY)
        if backend_observed:
            backend_name = resource.struct_to_dict(backend_observed.resource).get("metadata", {}).get("name")
            if backend_name:
                status.routing = v1alpha1.Routing(backendName=backend_name)

        libresource.update_status(self.rsp.desired.composite, status)

    def derive_conditions(self):
        """RoutingReady: the Backend has been observed on the control plane."""
        backend_exists = BACKEND_RESOURCE_KEY in self.req.observed.resources
        conditions.set_condition(
            self.rsp,
            conditions.CONDITION_TYPE_ROUTING_READY,
            backend_exists,
            CONDITION_REASON_BACKEND_CONFIGURED if backend_exists else CONDITION_REASON_WAITING_FOR_BACKEND,
        )
        if backend_exists:
            self.rsp.desired.resources[BACKEND_RESOURCE_KEY].ready = fnv1.READY_TRUE


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose a Backend from a ModelEndpoint."""
    Composer(req, rsp).compose()
