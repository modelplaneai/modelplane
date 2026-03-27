"""Condition helpers for composition functions."""

from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

# Condition types shared across multiple functions.
CONDITION_TYPE_ROUTING_READY = "RoutingReady"


def has_condition(req: fnv1.RunFunctionRequest, name: str, cond: str) -> bool:
    """Check if an observed composed resource has a condition set to True.

    Uses the SDK's resource.get_condition which reads status.conditions from
    the protobuf Struct representation of the resource.
    """
    observed = req.observed.resources.get(name)
    if observed is None:
        return False
    return resource.get_condition(observed.resource, cond).status == "True"


def has_parent_condition(req: fnv1.RunFunctionRequest, name: str, cond: str) -> bool:
    """Check a Gateway API condition nested under status.parents[].conditions.

    Gateway API resources (HTTPRoute, etc.) nest route status under
    status.parents[].conditions instead of top-level status.conditions.
    """
    observed = req.observed.resources.get(name)
    if observed is None:
        return False
    d = resource.struct_to_dict(observed.resource)
    for p in d.get("status", {}).get("parents", []):
        for c in p.get("conditions", []):
            if c.get("type") == cond and c.get("status") == "True":
                return True
    return False


def set_condition(
    rsp: fnv1.RunFunctionResponse,
    type: str,  # noqa: A002 - shadows builtin, but matches protobuf field name
    status: bool,
    reason: str,
    message: str = "",
) -> None:
    """Set a custom condition on the XR.

    A composable, single-condition setter. Call once per condition rather than
    bundling all conditions into one function with a large parameter list.
    """
    rsp.conditions.append(
        fnv1.Condition(
            type=type,
            status=(
                fnv1.STATUS_CONDITION_TRUE if status else fnv1.STATUS_CONDITION_FALSE
            ),
            reason=reason,
            message=message,
            target=fnv1.TARGET_COMPOSITE,
        )
    )
