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

"""Report whether a ModelEndpoint can carry traffic.

A ModelEndpoint describes a backend. compose-model-route composes the objects a
gateway needs to reach it, once per gateway serving a ModelService that selects
it.

This function checks that the endpoint's credential Secret exists and holds its
key, and reports the result as EndpointReady. That puts the reason on the
endpoint, and lets compose-model-route leave a broken endpoint out of every
route.
"""

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.modelendpoint import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# EndpointReady says whether a gateway could serve a request from this endpoint.
# compose-model-route reads it, and leaves an endpoint out of a route until
# it's True, so a broken endpoint carries no traffic rather than failing
# requests that reach it.
CONDITION_TYPE_ENDPOINT_READY = "EndpointReady"

CONDITION_REASON_ENDPOINT_USABLE = "EndpointUsable"
CONDITION_REASON_CREDENTIAL_MISSING = "CredentialMissing"
CONDITION_REASON_WAITING_FOR_CREDENTIAL = "WaitingForCredential"


def _namespace(meta: metav1.ObjectMeta | None) -> str:
    """The endpoint's namespace, always set on a namespaced resource."""
    if meta is None or meta.namespace is None:
        raise ValueError("metadata.namespace is unexpectedly absent")
    return meta.namespace


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
        self.xr = v1alpha1.ModelEndpoint(**resource.struct_to_dict(req.observed.composite.resource))

    def compose(self) -> None:
        self.derive_conditions()

    def derive_conditions(self) -> None:
        """Set EndpointReady, having resolved the credential Secret if any.

        An endpoint with no credential is usable as soon as it exists: the
        XRD's validation has already established that its origin is a scheme and
        a host, and whether the backend actually answers is a question only a
        request can settle, which the gateway's outlier detection then acts on.
        """
        api_key = self.xr.spec.credential.apiKey if self.xr.spec.credential else None
        if api_key is None:
            self.ready()
            return
        ref = api_key.secretRef

        response.require_resources(
            self.rsp,
            name="credential",
            api_version="v1",
            kind="Secret",
            match_name=ref.name,
            namespace=_namespace(self.xr.metadata),
        )
        # A requirement key is absent until it resolves, which is how the SDK
        # distinguishes unresolved from resolved-empty.
        if "credential" not in self.req.required_resources:
            self.not_ready(
                CONDITION_REASON_WAITING_FOR_CREDENTIAL,
                f"Waiting for Secret {ref.name} to resolve",
            )
            return

        secrets = request.get_required_resources(self.req, "credential")
        if not secrets:
            self.not_ready(
                CONDITION_REASON_CREDENTIAL_MISSING,
                f"Secret {ref.name} does not exist",
            )
            return

        key = ref.key or "apiKey"
        if key not in secrets[0].get("data", {}):
            self.not_ready(
                CONDITION_REASON_CREDENTIAL_MISSING,
                f"Secret {ref.name} has no key {key}",
            )
            return

        self.ready()

    def ready(self) -> None:
        # This XR composes nothing, so Crossplane has no composed resource to
        # derive the composite's Ready from; set it here so it mirrors
        # EndpointReady rather than reading vacuously true.
        self.rsp.desired.composite.ready = fnv1.READY_TRUE
        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_ENDPOINT_READY,
                status="True",
                reason=CONDITION_REASON_ENDPOINT_USABLE,
            ),
        )

    def not_ready(self, reason: str, message: str) -> None:
        self.rsp.desired.composite.ready = fnv1.READY_FALSE
        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_ENDPOINT_READY,
                status="False",
                reason=reason,
                message=message,
            ),
        )
        response.normal(self.rsp, message)
