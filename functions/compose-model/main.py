from crossplane.function import resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Validate a ClusterModel or Model and set Ready.

    This function composes no resources. Both ClusterModel and Model are data
    records — catalog entries that describe how a model should be served. The
    function validates the spec and sets Ready=True.
    """
    xr = resource.struct_to_dict(req.observed.composite.resource)
    spec = xr.get("spec", {})

    engine = spec.get("engine", "")
    if engine == "vLLM" and not spec.get("vllm"):
        response.warning(rsp, "engine is vLLM but spec.vllm is not set; using defaults")

    rsp.conditions.append(fnv1.Condition(
        type="Ready",
        status=fnv1.STATUS_CONDITION_TRUE,
        reason="Available",
        target=fnv1.TARGET_COMPOSITE_AND_CLAIM,
    ))
