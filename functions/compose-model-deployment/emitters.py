"""Pure builders: matcher output → composed-resource dicts.

═══════════════════════════════════════════════════════════════════════════
  THIS MODULE IS PURE.  Same shape as scheduling.py — no Crossplane / k8s
  imports. Builds the dicts the orchestrator (main.py) hands to
  resource.update(rsp.desired.resources[...]).

  Test target: tests/unit/test_emitters.py
═══════════════════════════════════════════════════════════════════════════

Two functions:

  build_replica(md, placement)  → dict (a ModelReplica spec)
  build_endpoint(md, placement) → dict (a ModelEndpoint spec)

Both return Kubernetes-shaped dicts (apiVersion/kind/metadata/spec). The
main.py orchestrator wraps the result in resource.update().
"""

from . import scheduling

API_VERSION = "modelplane.ai/v1alpha1"
LABEL_DEPLOYMENT = "modelplane.ai/deployment"


# ---------------------------------------------------------------------------
# ModelReplica — one per Placement.
# Spec carries everything the renderer needs: target, resolved decode/prefill
# role specs, engine, source. The renderer doesn't re-fetch the parent MD.
# ---------------------------------------------------------------------------


def build_replica(
    md: scheduling.ModelDeploymentSpec,
    placement: scheduling.Placement,
    md_uid: str,
    engine: dict,
    source: dict,
) -> dict:
    """Build a ModelReplica dict from MD + matcher placement decision.

    Args:
      md         — the parent ModelDeployment's matcher view.
      placement  — the placement decision for this replica index.
      md_uid     — UID for ownerReferences, propagated by main.py from the
                   observed MD struct.
      engine     — engine config dict carried through from MD (image, args,
                   etc.). Pass-through.
      source     — source spec dict (HuggingFace repo, S3 URI, etc.).
                   Pass-through.

    Returns: dict suitable for resource.update().
    """
    name = _replica_name(md.name, placement.replica_index)
    spec: dict = {
        "replicaIndex": placement.replica_index,
        "target": {
            "cluster": placement.cluster,
            "decodePool": placement.decode.pool,
            "prefillPool": placement.prefill.pool if placement.prefill else None,
        },
        "decode": _role_dict(md.decode, placement.decode),
        "prefill": (
            _role_dict(md.prefill, placement.prefill)
            if md.disaggregated and placement.prefill and md.prefill
            else None
        ),
        "engine": engine,
        "source": source,
    }
    return {
        "apiVersion": API_VERSION,
        "kind": "ModelReplica",
        "metadata": {
            "name": name,
            "namespace": md.namespace,
            "labels": {LABEL_DEPLOYMENT: md.name},
            "ownerReferences": [_owner_ref(md, md_uid)],
        },
        "spec": spec,
    }


# ---------------------------------------------------------------------------
# ModelEndpoint — one per replica (per Nic's #64).
# URL is filled in later by status reconcile when the gateway address is
# known; we set api: OpenAI as the default.
# ---------------------------------------------------------------------------


def build_endpoint(
    md: scheduling.ModelDeploymentSpec,
    placement: scheduling.Placement,
    md_uid: str,
) -> dict:
    name = _endpoint_name(md.name, placement.replica_index)
    return {
        "apiVersion": API_VERSION,
        "kind": "ModelEndpoint",
        "metadata": {
            "name": name,
            "namespace": md.namespace,
            "labels": {LABEL_DEPLOYMENT: md.name},
            "ownerReferences": [_owner_ref(md, md_uid)],
        },
        "spec": {
            "url": "",  # filled in by status reconcile
            "api": "OpenAI",
        },
    }


# ---------------------------------------------------------------------------
# Helpers — pure.
# ---------------------------------------------------------------------------


def _role_dict(
    role: scheduling.RoleSpec | None,
    placement: scheduling.RolePlacement,
) -> dict:
    """Build a ModelReplica.spec.{decode|prefill} block.

    The renderer reads this instead of re-fetching the parent MD — pure
    over (MR, IC, Class).
    """
    assert role is not None, "build_replica should only call _role_dict for present roles"
    return {
        "topology": {
            "strategy": role.topology.strategy,
            "tensor": role.topology.tensor,
            "pipeline": role.topology.pipeline,
            "data": role.topology.data,
            "dataLocal": role.topology.data_local,
            "instances": role.topology.instances,
        },
        # The CEL string is carried verbatim. Renderer re-uses it to build
        # a DRA ResourceClaim selector.
        "nodeSelector": {"cel": role.node_selector_cel},
        "pool": placement.pool,
        "nodesUsed": placement.nodes_used,
        "gpusPerNode": placement.gpus_per_node,
        "instances": placement.instances,
    }


def _owner_ref(md: scheduling.ModelDeploymentSpec, md_uid: str) -> dict:
    return {
        "apiVersion": API_VERSION,
        "kind": "ModelDeployment",
        "name": md.name,
        "uid": md_uid,
        "controller": True,
        "blockOwnerDeletion": True,
    }


def _replica_name(md_name: str, idx: int) -> str:
    return f"{md_name}-{idx}"


def _endpoint_name(md_name: str, idx: int) -> str:
    return f"{md_name}-{idx}"


# ---------------------------------------------------------------------------
# matchTrace projection — for MD.status.matchTrace.
# ---------------------------------------------------------------------------


def build_match_trace(trace: list[scheduling.MatchTrace]) -> list[dict]:
    """Group the matcher's per-(IC,pool) trace lines for status output.

    The shape lands on MD.status.matchTrace. The user reads this when
    Ready=False with reason NotScheduled — surfaces *why* each candidate
    was rejected.
    """
    return [
        {"cluster": t.cluster, "pool": t.pool or "", "reason": t.reason, "detail": t.detail}
        for t in trace
    ]
