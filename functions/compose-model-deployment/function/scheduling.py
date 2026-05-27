"""Schedule model replicas across inference clusters.

Replicas are pinned to clusters at creation time. On each reconcile the
scheduler runs in two phases:

1. Retain. For each existing replica of this deployment, keep its
   pinned cluster assignment if the cluster still exists. The cluster
   does not need to be Ready - a pinned replica stays pinned even if
   its cluster is temporarily unavailable, and the parent
   ModelDeployment surfaces the degraded state via its conditions.

2. Place. For any unfilled replicas (scale-up, or replicas whose
   pinned cluster was deleted entirely), pick from the remaining
   candidate clusters by filtering against capacity and ranking
   deterministically.

A merely degraded cluster (not Ready, or no gateway address) does not
trigger re-placement - the replica stays pinned and the deployment
reflects the degradation via conditions. Re-placement happens only
when the pinned cluster is gone from the cluster set entirely, or when
the underlying ModelReplica is deleted (e.g. by reducing replicas).
"""

from dataclasses import dataclass

from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1

# Label key written by compose-model-deployment. Used to find existing
# replicas of this deployment so we can preserve their cluster pins.
_LABEL_DEPLOYMENT = "modelplane.ai/deployment"


@dataclass
class Candidate:
    """A cluster selected to host a ModelReplica."""

    name: str
    # The cluster's gateway address. Empty if the cluster is pinned but
    # currently unavailable (no Ready condition or no gateway address).
    # Callers should not compose a ModelEndpoint when this is empty -
    # there is nothing to route traffic to.
    gateway_address: str


@dataclass
class Shape:
    """Physical shape derived from workers.topology and workers.count."""

    gpus_per_node: int  # GPUs per pod (= tensor).
    nodes_per_worker: int  # Pods per worker (= pipeline, default 1).
    total_gpus: int  # Total GPUs consumed by all workers in one replica.


def topology_shape(workers) -> Shape:
    """Derive the physical shape of one ModelReplica from workers."""
    topology = workers.topology
    count = int(workers.count or 1)
    gpus_per_node = int(topology.tensor)
    nodes_per_worker = int(topology.pipeline or 1)

    total_gpus = gpus_per_node * nodes_per_worker * count
    return Shape(
        gpus_per_node=gpus_per_node,
        nodes_per_worker=nodes_per_worker,
        total_gpus=total_gpus,
    )


def _cluster_ready(cluster: icv1alpha1.InferenceCluster) -> bool:
    """Check that the cluster is Ready and has a gateway address.

    A cluster without a Ready=True condition hasn't finished provisioning
    or has become unavailable. A cluster without a gateway address can't
    receive routed traffic. Both must be true for the cluster to be
    schedulable for new placements.
    """
    if not cluster.status.gateway or not cluster.status.gateway.address:
        return False
    return any(c.type == "Ready" and c.status == "True" for c in cluster.status.conditions or [])


def _pool_fits_shape(pool, shape: Shape) -> bool:
    """Check whether a pool can host one ModelReplica of this shape."""
    count_per_node = int(pool.countPerNode or 0)
    nodes = int(pool.nodes or 0)

    if count_per_node < shape.gpus_per_node:
        return False
    return nodes >= shape.nodes_per_worker


def _cluster_fits_shape(cluster: icv1alpha1.InferenceCluster, shape: Shape) -> tuple[bool, int]:
    """Return whether any pool on the cluster can host the shape, and
    the total eligible GPU capacity across fitting pools."""
    eligible_total = 0
    fit = False
    for pool in cluster.status.capacity.gpuPools:
        if not _pool_fits_shape(pool, shape):
            continue
        fit = True
        eligible_total += int(pool.countPerNode or 0) * int(pool.nodes or 0)
    return fit, eligible_total


def schedule(
    deployment: mdv1alpha1.ModelDeployment,
    clusters: list[icv1alpha1.InferenceCluster],
    all_replicas: list[mrv1alpha1.ModelReplica],
) -> list[Candidate]:
    """Pick clusters for a deployment's ModelReplicas.

    Existing replicas keep their pinned cluster. Any remaining replica
    slots are filled by picking deterministically from the remaining
    candidate clusters.

    Returns up to deployment.spec.replicas candidates. Returns fewer
    if not enough viable clusters exist.
    """
    desired_replicas = int(deployment.spec.replicas)
    shape = topology_shape(deployment.spec.workers)
    clusters_by_name = {c.metadata.name: c for c in clusters}

    # Phase 1: retain. Each existing replica stays on its pinned
    # cluster, as long as that cluster still exists in the candidate
    # set. We keep degraded clusters (not Ready, no gateway address) so
    # transient outages don't trigger re-placement.
    retained: list[Candidate] = []
    retained_names: set[str] = set()
    for r in all_replicas:
        if (r.metadata.labels or {}).get(_LABEL_DEPLOYMENT) != deployment.metadata.name:
            continue
        cluster_name = r.spec.clusterName
        # Replicas without a pin (shouldn't happen given the XRD requires
        # clusterName) or pinned to a cluster that no longer exists are
        # dropped from the retained set - the scheduler will pick a
        # replacement in phase 2.
        if not cluster_name or cluster_name not in clusters_by_name:
            continue
        if cluster_name in retained_names:
            continue
        cluster = clusters_by_name[cluster_name]
        retained.append(
            Candidate(
                name=cluster_name,
                # Empty when the cluster is degraded. Callers must check
                # this before composing routing resources.
                gateway_address=(cluster.status.gateway.address if cluster.status.gateway else "") or "",
            )
        )
        retained_names.add(cluster_name)

    # Trim retained to desired replica count. Scale-down keeps the
    # lexicographically earliest pinned clusters so the choice is
    # deterministic and stable across reconciles.
    retained.sort(key=lambda c: c.name)
    retained = retained[:desired_replicas]
    retained_names = {c.name for c in retained}

    # Phase 2: place. Fill any remaining slots from clusters that don't
    # already host one of this deployment's replicas. Only clusters that
    # are Ready and have free capacity are eligible.
    remaining = desired_replicas - len(retained)
    placed: list[Candidate] = []
    if remaining > 0:
        placed = _place_new(deployment, shape, clusters, retained_names, all_replicas, remaining)

    return retained + placed


def _place_new(
    deployment: mdv1alpha1.ModelDeployment,
    shape: Shape,
    clusters: list[icv1alpha1.InferenceCluster],
    skip: set[str],
    all_replicas: list[mrv1alpha1.ModelReplica],
    n: int,
) -> list[Candidate]:
    """Pick up to n clusters for new replicas.

    Skips clusters in the skip set (already retained). Filters by
    readiness, topology fit, and free capacity. Returns at most n
    candidates sorted alphabetically.
    """
    candidates: list[Candidate] = []
    for cluster in clusters:
        if cluster.metadata.name in skip:
            continue
        if not _cluster_ready(cluster):
            continue

        fit, eligible_total = _cluster_fits_shape(cluster, shape)
        if not fit:
            continue

        # Subtract GPUs consumed by other deployments' replicas on this
        # cluster. Our own replicas can't be on this cluster (we skipped
        # those above).
        used_gpus = _used_gpus(deployment, cluster, all_replicas)

        if eligible_total - used_gpus < shape.total_gpus:
            continue

        candidates.append(
            Candidate(
                name=cluster.metadata.name,
                gateway_address=cluster.status.gateway.address,
            )
        )

    candidates.sort(key=lambda c: c.name)
    return candidates[:n]


def _used_gpus(deployment, cluster, all_replicas) -> int:
    """Sum GPUs consumed by other deployments' replicas on this cluster.

    Other deployments' replicas are read from observed state. Each
    replica reports its topology, from which we derive total GPUs.
    Our own replicas are excluded - the scheduler treats this
    deployment's own demand separately.
    """
    used = 0
    for r in all_replicas:
        if (r.metadata.labels or {}).get(_LABEL_DEPLOYMENT) == deployment.metadata.name:
            continue
        if r.spec.clusterName != cluster.metadata.name:
            continue
        if not r.spec.workers:
            continue
        used += topology_shape(r.spec.workers).total_gpus
    return used
