"""Validate a ClusterModel or Model and set Ready.

This function composes no resources. Both ClusterModel and Model are data
records — catalog entries that describe how a model should be served. The
function validates the spec and sets Ready.
"""

from crossplane.function import resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

# Both ClusterModel and Model share the same schema. We use ClusterModel's
# Pydantic model for both since the spec fields are identical.
from .model.ai.modelplane.clustermodel import v1alpha1


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Validate the model spec and set the XR as ready."""
    xr = v1alpha1.ClusterModel(
        **resource.struct_to_dict(req.observed.composite.resource)
    )

    if xr.spec.engine == "vLLM" and not xr.spec.vllm:
        response.warning(rsp, "engine is vLLM but spec.vllm is not set; using defaults")

    rsp.desired.composite.ready = fnv1.READY_TRUE
