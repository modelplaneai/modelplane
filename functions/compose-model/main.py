"""Validate a ClusterModel or Model.

This function composes no resources. Both ClusterModel and Model are data
records — catalog entries that describe how a model should be served. The
function validates the spec. Crossplane automatically marks the XR as
Ready since there are no composed resources to wait for.
"""

from crossplane.function import resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .model.ai.modelplane.clustermodel import v1alpha1 as cmv1alpha1
from .model.ai.modelplane.model import v1alpha1 as mv1alpha1


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Validate the model spec."""
    d = resource.struct_to_dict(req.observed.composite.resource)
    if d.get("kind") == "Model":
        xr = mv1alpha1.ModelModel(**d)
    else:
        xr = cmv1alpha1.ClusterModel(**d)

    if xr.spec.engine == "vLLM" and not xr.spec.vllm:
        response.warning(rsp, "engine is vLLM but spec.vllm is not set; using defaults")
