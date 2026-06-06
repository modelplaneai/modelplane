"""Compose an InferenceClass.

InferenceClass is a data resource: it describes hardware (devices) and
optionally how to provision it (provisioning). It has no composed
children. This function just marks the XR Ready.
"""

import grpc
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.inferenceclass import v1alpha1


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

        _ = v1alpha1.InferenceClass(**resource.struct_to_dict(req.observed.composite.resource))

        resource.update_status(rsp.desired.composite, v1alpha1.Status())
        response.set_conditions(rsp, resource.Condition(typ="Accepted", status="True", reason="Available"))
        rsp.desired.composite.ready = fnv1.READY_TRUE

        return rsp
