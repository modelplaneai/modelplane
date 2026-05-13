"""Compose an InferenceClass.

InferenceClass is a data resource: it describes hardware (resources)
and optionally how to provision it (provisioning). It has no composed
children. This function just marks the XR Ready.
"""

from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .lib import conditions
from .lib import resource as libresource
from .model.ai.modelplane.inferenceclass import v1alpha1

CONDITION_TYPE_ACCEPTED = "Accepted"
CONDITION_REASON_AVAILABLE = "Available"


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Mark the InferenceClass Ready. No resources are composed."""
    xr = v1alpha1.InferenceClass(**resource.struct_to_dict(req.observed.composite.resource))

    libresource.update_status(rsp.desired.composite, v1alpha1.Status())
    conditions.set_condition(rsp, CONDITION_TYPE_ACCEPTED, True, CONDITION_REASON_AVAILABLE)
    rsp.desired.composite.ready = fnv1.READY_TRUE

    # Silence unused-variable lint; we read xr for parsing-side validation.
    _ = xr
