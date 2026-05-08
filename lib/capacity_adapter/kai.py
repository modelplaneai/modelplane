"""KAI Scheduler capacity adapter.

Reads KAI's status CRDs:

  Queue                 — per-tenant queue (modelplane creates one per
                          namespace under a per-IC Project).
  ResourcePool          — KAI's view of a node pool, with per-flavor
                          capacity numbers.

Maps both into the common CapacitySnapshot shape the matcher consumes
via InferenceCluster.status.capacity. KAI's status is granular enough
to surface pending gang counts — useful for fleet-level observability,
not load-bearing for the matcher.

Sketch — `_list_*` functions stand in for the K8s client calls. The
control flow + projection are honest.
"""

from datetime import UTC, datetime

from .common import CapacitySnapshot, PoolCapacity, ResourceCount

# KAI uses these GVKs.
KAI_QUEUE = ("scheduling.run.ai/v2", "Queue")
KAI_RESOURCE_POOL = ("scheduling.run.ai/v2alpha2", "ResourcePool")


def snapshot(cluster_name: str, k8s_client) -> CapacitySnapshot:
    """Build a CapacitySnapshot from a cluster running KAI.

    Algorithm:
      1. List all ResourcePools (cluster-scoped under KAI's namespace).
      2. For each pool, read .status.{capacity, allocated, allocatable}
         per resource flavor (e.g. "nvidia-h200").
      3. Cross-reference with the InferenceCluster's nodePools[] to map
         KAI's resource-flavor name back to our pool name. The IC
         onboarding controller seeds this mapping when KAI is detected.

    KAI also exposes Queue.status with pending gang counts. We don't
    surface those into status.capacity (it's not capacity); they go to a
    separate observability output for fleet operators (out of scope).
    """
    snap = CapacitySnapshot(cluster=cluster_name, last_observed=datetime.now(UTC))

    pools_raw = _list_resource_pools(k8s_client)
    for raw in pools_raw:
        modelplane_pool_name = _kai_pool_to_modelplane(raw)
        if modelplane_pool_name is None:
            continue  # not a Modelplane-managed pool; skip
        snap.pools.append(_project_pool(modelplane_pool_name, raw))

    return snap


# ---------------------------------------------------------------------------
# Per-pool projection
# ---------------------------------------------------------------------------


def _project_pool(name: str, kai_pool_raw: dict) -> PoolCapacity:
    """Project one KAI ResourcePool into a PoolCapacity.

    KAI's status structure (sketch):
      status.resources:
        - name: "nvidia.com/gpu"
          quota: <total>
          allocated: <used>
        - name: "cpu"
          quota: ...
          allocated: ...
    """
    p = PoolCapacity(name=name)
    for res in kai_pool_raw.get("status", {}).get("resources", []):
        p.resources.append(
            ResourceCount(
                name=res["name"],
                total=int(res.get("quota", 0)),
                used=int(res.get("allocated", 0)),
            )
        )
    return p


def _kai_pool_to_modelplane(kai_pool_raw: dict) -> str | None:
    """Map a KAI ResourcePool name to the matching IC.spec.nodePools[].name.

    The IC onboarding controller stamps a label on KAI's ResourcePool
    when it creates / discovers it:
      modelplane.ai/pool: <node-pool-name>

    For BYO KAI pools we may need to add this label as part of
    onboarding (or read a config-map mapping). The label is the cleanest.
    """
    return (
        kai_pool_raw.get("metadata", {})
        .get("labels", {})
        .get("modelplane.ai/pool")
    )


# ---------------------------------------------------------------------------
# K8s client placeholders — wired to real client (controller-runtime / kr8s
# / kubernetes.io) in production.
# ---------------------------------------------------------------------------


def _list_resource_pools(k8s_client) -> list[dict]:
    raise NotImplementedError("k8s.list(KAI_RESOURCE_POOL) when wired")
