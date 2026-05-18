"""Schedule model replicas across inference clusters.

For each candidate cluster, checks whether the cluster's pools can host
the workers.topology shape (GPUs per node, nodes per worker). Accounts
for GPUs already consumed by other deployments' replicas. Returns a
stable list of candidates that prefers clusters with existing replicas
for this deployment.
"""

from dataclasses import dataclass

from .lib import metadata
from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from .model.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
from .model.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1

STRATEGY_TENSOR = "Tensor"
STRATEGY_TENSOR_PIPELINE = "TensorPipeline"


@dataclass
class Candidate:
    """A cluster that matched scheduling criteria."""

    name: str


@dataclass
class Shape:
    """Physical shape derived from workers.topology and workers.count."""

    gpus_per_node: int  # GPUs per pod.
    nodes_per_worker: int  # Pods per worker (1 for Tensor, pipeline for TP).
    total_gpus: int  # Total GPUs consumed by all workers in one replica.


def topology_shape(workers) -> Shape:
    """Derive the physical shape of one ModelReplica from workers."""
    topology = workers.topology
    count = int(workers.count or 1)
    gpus_per_node = int(topology.tensor)

    nodes_per_worker = int(topology.pipeline) if topology.strategy == STRATEGY_TENSOR_PIPELINE else 1

    total_gpus = gpus_per_node * nodes_per_worker * count
    return Shape(
        gpus_per_node=gpus_per_node,
        nodes_per_worker=nodes_per_worker,
        total_gpus=total_gpus,
    )


def _cluster_ready(cluster: icv1alpha1.InferenceCluster) -> bool:
    """Check that the cluster is Ready and has a gateway address.

    A cluster without a Ready=True condition hasn't finished provisioning.
    A cluster without a gateway address can't receive routed traffic.
    Both must be true for the cluster to be schedulable.
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


def schedule(
    deployment: mdv1alpha1.ModelDeployment,
    clusters: list[icv1alpha1.InferenceCluster],
    all_replicas: list[mrv1alpha1.ModelReplica],
) -> list[Candidate]:
    """Select clusters for model replicas.

    For each candidate cluster, checks that at least one pool can host
    the workers.topology shape and that the cluster has enough free GPUs
    after subtracting GPUs consumed by other deployments' replicas.

    Sorts to prefer clusters that already have a replica for this
    deployment (stability), then alphabetically (determinism). Returns
    at most deployment.spec.replicas candidates.
    """
    shape = topology_shape(deployment.spec.workers)

    # Clusters that already have a replica for this deployment.
    existing_clusters = {
        r.spec.inferenceClusterRef.name
        for r in all_replicas
        if (r.metadata.labels or {}).get(metadata.LABEL_KEY_DEPLOYMENT) == deployment.metadata.name
    }

    candidates = []
    for cluster in clusters:
        if not _cluster_ready(cluster):
            continue

        # Find pools that can host the shape, accumulating total eligible
        # GPU capacity.
        eligible_total = 0
        fit = False
        for pool in cluster.status.capacity.gpuPools:
            if not _pool_fits_shape(pool, shape):
                continue
            fit = True
            eligible_total += int(pool.countPerNode or 0) * int(pool.nodes or 0)

        if not fit:
            continue

        # Subtract GPUs consumed by other deployments' replicas on this cluster.
        used_gpus = _used_gpus(deployment, cluster, all_replicas)

        if eligible_total - used_gpus < shape.total_gpus:
            continue

        candidates.append(Candidate(name=cluster.metadata.name))

    # Prefer clusters that already have a replica for this deployment.
    # Within each group (existing vs new), sort by name for determinism.
    candidates.sort(
        key=lambda c: (
            0 if c.name in existing_clusters else 1,
            c.name,
        )
    )
    return candidates[: int(deployment.spec.replicas)]


def _used_gpus(deployment, cluster, all_replicas) -> int:
    """Sum GPUs consumed by other deployments' replicas on this cluster.

    Other deployments' replicas are read from observed state. Each
    replica reports its topology, from which we derive total GPUs.
    Our own replicas are excluded - the scheduler treats this
    deployment's own demand separately.
    """
    used = 0
    for r in all_replicas:
        if (r.metadata.labels or {}).get(metadata.LABEL_KEY_DEPLOYMENT) == deployment.metadata.name:
            continue
        if r.spec.inferenceClusterRef.name != cluster.metadata.name:
            continue
        if not r.spec.workers:
            continue
        used += topology_shape(r.spec.workers).total_gpus
    return used
