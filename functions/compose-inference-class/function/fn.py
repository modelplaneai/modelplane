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

"""Compose an InferenceClass.

InferenceClass is a data resource: it describes hardware (devices) and
optionally how to provision it (provisioning). It has no composed
children. This function marks the XR Ready, rejecting a class whose
devices name an accelerator vendor its provisioning provider's serving
stack cannot install - the earliest point the contradiction is visible,
before any cluster references the class. Classes without a provisioning
block are BYO: the cluster's operator manages the accelerator stack, so
any vendor is accepted.
"""

from typing import Final

import grpc
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.inferenceclass import v1alpha1

# The accelerator vendors each provider's serving stack can install.
# Kept in sync with ACCELERATOR_VENDORS in compose-serving-stack's
# stacks package, which is the authoritative table next to the
# component lists themselves. compose-inference-cluster carries the
# same table for the cluster-side pairing check.
_PROVIDER_ACCELERATOR_VENDORS: Final[dict[str, frozenset[str]]] = {
    "GKE": frozenset({"NVIDIA"}),
    "EKS": frozenset({"NVIDIA"}),
    "AKS": frozenset({"NVIDIA"}),
    "Nebius": frozenset({"NVIDIA"}),
    "Vultr": frozenset({"NVIDIA"}),
    "VultrBaremetal": frozenset({"AMD", "NVIDIA"}),
}

CONDITION_REASON_UNSUPPORTED_DEVICES = "UnsupportedDevices"


def _accelerator_vendors(xr: v1alpha1.InferenceClass) -> set[str]:
    """The accelerator vendors the class's devices name, from each
    device's DRA driver (gpu.amd.com, gpu.nvidia.com)."""
    vendors: set[str] = set()
    for device in xr.spec.devices or []:
        if "amd" in device.driver:
            vendors.add("AMD")
        elif "nvidia" in device.driver:
            vendors.add("NVIDIA")
    return vendors


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

        xr = v1alpha1.InferenceClass(**resource.struct_to_dict(req.observed.composite.resource))

        resource.update_status(rsp.desired.composite, v1alpha1.Status())

        provider = xr.spec.provisioning.provider if xr.spec.provisioning else None
        supported = _PROVIDER_ACCELERATOR_VENDORS.get(provider) if provider else None
        unsupported = sorted(_accelerator_vendors(xr) - supported) if supported is not None else []
        if unsupported:
            msg = (
                f"{', '.join(unsupported)} devices are not supported on {provider}: "
                f"its serving stack installs only {', '.join(sorted(supported or []))} accelerator stacks"
            )
            response.set_conditions(
                rsp,
                resource.Condition(
                    typ="Accepted",
                    status="False",
                    reason=CONDITION_REASON_UNSUPPORTED_DEVICES,
                    message=msg,
                ),
            )
            response.warning(rsp, msg)
            return rsp

        response.set_conditions(rsp, resource.Condition(typ="Accepted", status="True", reason="Available"))
        rsp.desired.composite.ready = fnv1.READY_TRUE

        return rsp
