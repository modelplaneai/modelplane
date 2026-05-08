"""Kueue capacity adapter.

Reads Kueue's `ClusterQueue.status.flavorsUsage[]`:

  status:
    flavorsUsage:
    - name: nvidia-h200          # ResourceFlavor name → maps to a pool
      resources:
      - name: nvidia.com/gpu
        total: "32"              # nominalQuota of this flavor
        usage: "12"              # admitted Workloads consumed
      - name: cpu
        total: "256"
        usage: "120"

Modelplane uses one ClusterQueue per InferenceCluster, with one
ResourceFlavor per node pool. The mapping flavor → pool comes from the
IC onboarding controller (stamps a label on the ResourceFlavor).

Kueue v0.8+ has native LeaderWorkerSet integration: the LWS owner is
admitted as a Workload, capacity counted against the right flavor based
on the LWS pod template's tolerations / nodeSelector.

Sketch — same shape as kai.py; only the per-vendor projection differs.
"""

from datetime import UTC, datetime

from .common import CapacitySnapshot, PoolCapacity, ResourceCount

KUEUE_CLUSTER_QUEUE = ("kueue.x-k8s.io/v1beta1", "ClusterQueue")
KUEUE_RESOURCE_FLAVOR = ("kueue.x-k8s.io/v1beta1", "ResourceFlavor")


def snapshot(cluster_name: str, k8s_client) -> CapacitySnapshot:
    """Build a CapacitySnapshot from a cluster running Kueue.

    Algorithm:
      1. Find the Modelplane-owned ClusterQueue
         (label: modelplane.ai/inference-cluster=<cluster_name>).
      2. Walk its .status.flavorsUsage[] entries.
      3. For each flavor, look up the corresponding ResourceFlavor and
         resolve its modelplane.ai/pool label → pool name.
      4. Project flavor.resources[] → ResourceCount[].
    """
    snap = CapacitySnapshot(cluster=cluster_name, last_observed=datetime.now(UTC))

    cq = _find_cluster_queue(k8s_client, cluster_name)
    if cq is None:
        return snap  # not yet onboarded by the install controller

    flavor_to_pool = _build_flavor_pool_map(k8s_client)

    for flavor_usage in cq.get("status", {}).get("flavorsUsage", []):
        flavor_name = flavor_usage["name"]
        pool_name = flavor_to_pool.get(flavor_name)
        if pool_name is None:
            continue  # flavor not mapped to a Modelplane pool; skip
        snap.pools.append(_project_pool(pool_name, flavor_usage))

    return snap


# ---------------------------------------------------------------------------
# Per-pool projection
# ---------------------------------------------------------------------------


def _project_pool(name: str, flavor_usage: dict) -> PoolCapacity:
    """Project one ClusterQueue flavorsUsage entry into a PoolCapacity."""
    p = PoolCapacity(name=name)
    for res in flavor_usage.get("resources", []):
        p.resources.append(
            ResourceCount(
                name=res["name"],
                # Kueue uses K8s Quantity strings ("32", "1Gi"); the
                # production helper would parse these properly.
                total=int(_parse_quantity(res.get("total", 0))),
                used=int(_parse_quantity(res.get("usage", 0))),
            )
        )
    return p


def _build_flavor_pool_map(k8s_client) -> dict[str, str]:
    """ResourceFlavor.metadata.name → InferenceCluster.spec.nodePools[].name.

    The IC onboarding controller stamps the modelplane.ai/pool label on
    each ResourceFlavor when it creates them (one per pool).
    """
    out: dict[str, str] = {}
    for rf in _list_resource_flavors(k8s_client):
        labels = rf.get("metadata", {}).get("labels", {})
        if "modelplane.ai/pool" in labels:
            out[rf["metadata"]["name"]] = labels["modelplane.ai/pool"]
    return out


# ---------------------------------------------------------------------------
# K8s client placeholders
# ---------------------------------------------------------------------------


def _find_cluster_queue(k8s_client, cluster_name: str) -> dict | None:
    raise NotImplementedError(
        "k8s.list(KUEUE_CLUSTER_QUEUE, label=modelplane.ai/inference-cluster) when wired"
    )


def _list_resource_flavors(k8s_client) -> list[dict]:
    raise NotImplementedError("k8s.list(KUEUE_RESOURCE_FLAVOR) when wired")


def _parse_quantity(q) -> int:
    """K8s Quantity → int. Sketch — production uses kubernetes.utils.parse_quantity."""
    if isinstance(q, int):
        return q
    return int(str(q))
