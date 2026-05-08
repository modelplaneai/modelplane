"""Pure builders: scheduler output → composed-resource dicts.

No Crossplane / Kubernetes imports. Builds the dicts main.py hands to
resource.update(rsp.desired.resources[...]). When #64's protos exist,
these will return pydantic models instead of dicts (resource.update accepts
either) and the build functions can use the generated v1alpha1.* shapes.

Crossplane sets ownerReferences from the XR (composite) onto composed
resources automatically — emitters do not set them manually.
"""

from . import scheduling

API_VERSION = "modelplane.ai/v1alpha1"


# ---- ModelReplica ---------------------------------------------------------


def build_replica(md: scheduling.ModelDeploymentSpec, p: scheduling.Placement) -> dict:
    """One ModelReplica per Placement. Spec carries the placement decision +
    resolved decode/prefill role specs so the renderer doesn't refetch the MD.
    """
    return {
        "apiVersion": API_VERSION,
        "kind": "ModelReplica",
        "metadata": {
            "name": _replica_name(md.name, p.replica_index),
            "namespace": md.namespace,
            "labels": {"modelplane.ai/deployment": md.name},
        },
        "spec": {
            "replicaIndex": p.replica_index,
            "target": {
                "cluster": p.cluster,
                "decodePool": p.decode.pool,
                "prefillPool": p.prefill.pool if p.prefill else None,
            },
            "decode": _role(md.decode, p.decode),
            "prefill": _role(md.prefill, p.prefill) if p.prefill and md.prefill else None,
        },
    }


# ---- ModelEndpoint --------------------------------------------------------


def build_endpoint(md: scheduling.ModelDeploymentSpec, p: scheduling.Placement) -> dict:
    """One ModelEndpoint per ModelReplica (per Nic's #64). URL is filled
    in later by status reconcile when the gateway address is known.
    """
    return {
        "apiVersion": API_VERSION,
        "kind": "ModelEndpoint",
        "metadata": {
            "name": _endpoint_name(md.name, p.replica_index),
            "namespace": md.namespace,
            "labels": {"modelplane.ai/deployment": md.name},
        },
        "spec": {
            "url": "",
            "api": "OpenAI",
        },
    }


# ---- matchTrace projection ------------------------------------------------


def build_match_trace(trace: list[scheduling.MatchTrace]) -> list[dict]:
    """Per-(cluster, pool, reason, detail) trace for MD.status.matchTrace.

    Surfaces *why* every candidate was rejected — load-bearing for the user
    when no placement is possible.
    """
    return [
        {"cluster": t.cluster, "pool": t.pool or "", "reason": t.reason, "detail": t.detail}
        for t in trace
    ]


# ---- helpers --------------------------------------------------------------


def _role(role: scheduling.RoleSpec | None, placement: scheduling.RolePlacement) -> dict:
    """Build a ModelReplica.spec.{decode|prefill} block. Carries everything
    the renderer needs — pool, GPU shape, CEL — so the renderer is pure
    over (MR, IC, Class) without reading the parent MD.
    """
    assert role is not None
    return {
        "topology": {
            "strategy": role.topology.strategy,
            "tensor": role.topology.tensor,
            "pipeline": role.topology.pipeline,
            "data": role.topology.data,
            "dataLocal": role.topology.data_local,
            "instances": role.topology.instances,
        },
        "nodeSelector": {"cel": role.node_selector_cel},
        "pool": placement.pool,
        "nodesUsed": placement.nodes_used,
        "gpusPerNode": placement.gpus_per_node,
        "instances": placement.instances,
    }


def _replica_name(md_name: str, idx: int) -> str:
    return f"{md_name}-{idx}"


def _endpoint_name(md_name: str, idx: int) -> str:
    return f"{md_name}-{idx}"
