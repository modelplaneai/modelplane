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

Pool selection is driven entirely by nodeSelector device requests (DRA CEL
matched against a pool's devices) plus available-node capacity. Topology is a
provisioning concern, not a scheduling input: the per-node GPU count is expressed
as a device request's count, and the only number the scheduler reads from
topology is nodes-per-replica, which gates against the pool's available nodes.
"""

from dataclasses import dataclass, field

from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1

from function import cel

# Label key written by compose-model-deployment. Used to find existing
# replicas of this deployment so we can preserve their cluster pins.
_LABEL_DEPLOYMENT = "modelplane.ai/deployment"

# claim discriminator values on an InferenceClass device.
_CLAIM_DRA = "DRA"


@dataclass
class DeviceRequest:
    """A resolved DRA device request for a matched pool device.

    Carries everything compose-model-replica needs to emit one DeviceRequest in
    a ResourceClaim: the request name, the DeviceClass to claim through (from the
    matched InferenceClass device), the count, and the CEL selectors. Only
    claim: DRA devices produce one of these; synthetic devices are matched for
    scheduling but never claimed.
    """

    name: str
    device_class_name: str
    count: int
    cel_selectors: list[str]


@dataclass
class Candidate:
    """A cluster selected to host a ModelReplica."""

    name: str
    # The cluster's gateway address. Empty if the cluster is pinned but
    # currently unavailable (no Ready condition or no gateway address).
    # Callers should not compose a ModelEndpoint when this is empty -
    # there is nothing to route traffic to.
    gateway_address: str
    # The node pool the scheduler matched on this cluster. Empty when there is
    # no nodeSelector (any pool is acceptable) or when the pool of a retained
    # replica can't be re-derived (e.g. the cluster is degraded). Propagated to
    # the ModelReplica as spec.nodePoolName.
    pool: str = ""
    # Resolved claim: DRA device requests for the matched pool, in nodeSelector
    # order. Stamped onto the ModelReplica as spec.deviceRequests. Empty when
    # there is no nodeSelector, the cluster is degraded (pool not re-derived), or
    # only synthetic devices matched.
    device_requests: list[DeviceRequest] = field(default_factory=list)


@dataclass
class Shape:
    """Physical shape derived from workers.topology and workers.count.

    Only nodes_per_replica is a scheduling input (the available-node gate).
    Topology otherwise drives provisioning, not pool selection.
    """

    nodes_per_replica: int  # Total nodes consumed by one ModelReplica.


def topology_shape(workers) -> Shape:
    """Derive nodes-per-replica from workers.

    Nodes per worker is pipeline (the only multi-node axis in v0.1); a replica
    has workers.count workers, so nodes-per-replica is pipeline * count.
    """
    topology = workers.topology
    count = int(workers.count or 1)
    nodes_per_worker = int(topology.pipeline or 1)
    return Shape(nodes_per_replica=nodes_per_worker * count)


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


@dataclass
class _CompiledRequest:
    """One nodeSelector device request with its CEL selectors compiled.

    cel_selectors are the raw expressions (carried through to the DeviceRequest);
    programs are the compiled forms used to match a pool device.
    """

    name: str
    count: int
    cel_selectors: list[str]
    programs: list[cel.Program]


def compile_requests(deployment: mdv1alpha1.ModelDeployment) -> "list[_CompiledRequest] | None":
    """Compile every nodeSelector device request's selectors once.

    Returns None when the deployment has no nodeSelector. Raises
    cel.CELCompileError on a malformed expression; the caller turns that into an
    InvalidNodeSelector condition.
    """
    if not deployment.spec.nodeSelector:
        return None
    requests = []
    for req in deployment.spec.nodeSelector.devices:
        cel_selectors = [s.cel for s in req.selectors if s.cel]
        requests.append(
            _CompiledRequest(
                name=req.name,
                count=int(req.count or 1),
                cel_selectors=cel_selectors,
                programs=[cel.Program(c) for c in cel_selectors],
            )
        )
    return requests


def _device_satisfies(device, programs: list[cel.Program]) -> bool:
    """Whether a pool device satisfies every selector (all ANDed)."""
    raw = device.model_dump(exclude_none=True)
    return all(p.matches(raw) for p in programs)


def _match_pool(pool, requests: "list[_CompiledRequest] | None") -> "list[DeviceRequest] | None":
    """Match a pool against the device requests.

    Returns the resolved claim: DRA DeviceRequests (possibly empty if only
    synthetic devices matched) when the pool satisfies every request, or None
    when the pool fails any request. A pool with no requests trivially matches
    with no resolved requests.

    A request matches a pool device when the device has enough UNCONSUMED count
    to cover the request and every selector evaluates true against that device.
    Each resolved DRA request becomes a distinct DeviceRequest in one
    ResourceClaim, and DRA allocates distinct devices per request, so a device's
    count is consumed as requests claim it: two requests cannot both be satisfied
    by the same single-count device, and N requests against one device must fit
    within that device's count. Without this accounting the scheduler would place
    a replica onto a node DRA can't actually satisfy.
    """
    if requests is None:
        return []

    devices = pool.devices or []
    # Track remaining count per device by its index in the pool, so capacity
    # consumed by an earlier request isn't offered again to a later one.
    remaining = [int(d.count or 1) for d in devices]
    resolved: list[DeviceRequest] = []
    for req in requests:
        match = None
        for i, device in enumerate(devices):
            if remaining[i] < req.count:
                continue
            if not _device_satisfies(device, req.programs):
                continue
            match = device
            remaining[i] -= req.count
            break
        if match is None:
            return None
        if (match.claim or _CLAIM_DRA) == _CLAIM_DRA:
            resolved.append(
                DeviceRequest(
                    name=req.name,
                    device_class_name=match.deviceClassName or "",
                    count=req.count,
                    cel_selectors=req.cel_selectors,
                )
            )
    return resolved


def _pool_by_name(cluster: icv1alpha1.InferenceCluster, pool_name: str):
    """The cluster's published pool with this name, or None."""
    for pool in cluster.status.capacity.gpuPools or []:
        if (pool.name or "") == pool_name:
            return pool
    return None


def _pinned_pool_still_matches(
    replica: mrv1alpha1.ModelReplica,
    cluster: icv1alpha1.InferenceCluster,
    requests: "list[_CompiledRequest] | None",
) -> bool:
    """Whether a retained replica's pinned pool still satisfies the requests.

    Modelplane follows Kubernetes here. A change to the deployment's nodeSelector
    is a change to the deployment "template", so - like editing a Deployment's
    Pod template - replicas that no longer match are re-placed (Kubernetes does a
    rolling replacement; we drop the pin and let phase 2 pick a matching pool).
    This is distinct from a pool's own device attributes drifting under a
    still-matching replica, which we leave pinned (Kubernetes'
    IgnoredDuringExecution: node-label drift does not evict a bound Pod).

    Returns True (keep the pin) when there is no nodeSelector. Returns False
    (re-place) when:
      * the replica carries no pool pin but the deployment now has a
        nodeSelector (we can't confirm a match, and the replica needs a real
        pool pin), or
      * the pinned pool no longer exists on the cluster, or
      * the pinned pool no longer satisfies the requests.
    """
    if requests is None:
        return True
    pool_name = replica.spec.nodePoolName
    if not pool_name:
        return False
    pool = _pool_by_name(cluster, pool_name)
    if pool is None:
        # Pinned pool is gone from the cluster's published capacity.
        return False
    return _match_pool(pool, requests) is not None


def _first_matching_pool(
    cluster: icv1alpha1.InferenceCluster,
    shape: Shape,
    requests: "list[_CompiledRequest] | None",
) -> "tuple[str, list[DeviceRequest]] | None":
    """Pick the first pool that matches the requests AND has enough nodes.

    Returns (pool_name, resolved_device_requests) for the first eligible pool,
    or None if no pool is eligible. A pool must both satisfy the nodeSelector
    requests and have at least nodes-per-replica nodes. Pools are considered in
    published order, which is deterministic (compose-inference-cluster emits them
    in spec.nodePools order).
    """
    for pool in cluster.status.capacity.gpuPools or []:
        if int(pool.nodes or 0) < shape.nodes_per_replica:
            continue
        resolved = _match_pool(pool, requests)
        if resolved is None:
            continue
        return (pool.name or ""), resolved
    return None


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

    # Compile every nodeSelector request's selectors once and reuse them across
    # every pool of every cluster. Raises CELCompileError on a malformed
    # expression - the caller turns that into a condition.
    requests = compile_requests(deployment)

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
        # Re-validate the pinned pool against the CURRENT nodeSelector. If the
        # ML team tightened nodeSelector so the pinned pool no longer matches,
        # drop the pin and let phase 2 re-place the replica - the Kubernetes
        # "template changed -> roll the replica" behavior. A pool whose own
        # device attributes drifted but still matches stays pinned.
        if not _pinned_pool_still_matches(r, cluster, requests):
            continue
        retained.append(
            Candidate(
                name=cluster_name,
                # Empty when the cluster is degraded. Callers must check
                # this before composing routing resources.
                gateway_address=(cluster.status.gateway.address if cluster.status.gateway else "") or "",
                # Keep the replica's existing pool pin. The scheduler retains
                # pool assignments across reconciles just like cluster pins.
                pool=r.spec.nodePoolName or "",
                # Re-resolve device requests for the retained pool so the
                # ModelReplica's spec.deviceRequests stays current with the
                # deployment's nodeSelector.
                device_requests=_retained_requests(r, cluster, requests),
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
        placed = _place_new(deployment, shape, clusters, retained_names, all_replicas, remaining, requests)

    return retained + placed


def _retained_requests(replica, cluster, requests: "list[_CompiledRequest] | None") -> list[DeviceRequest]:
    """Resolve device requests for a retained replica's pinned pool.

    Returns the claim: DRA requests for the pinned pool, or empty if there's no
    nodeSelector or the pool can't be found (degraded cluster). The pin itself
    was already validated by _pinned_pool_still_matches.
    """
    if requests is None:
        return []
    pool_name = replica.spec.nodePoolName
    if not pool_name:
        return []
    pool = _pool_by_name(cluster, pool_name)
    if pool is None:
        return []
    return _match_pool(pool, requests) or []


def _place_new(
    deployment: mdv1alpha1.ModelDeployment,
    shape: Shape,
    clusters: list[icv1alpha1.InferenceCluster],
    skip: set[str],
    all_replicas: list[mrv1alpha1.ModelReplica],
    n: int,
    requests: "list[_CompiledRequest] | None",
) -> list[Candidate]:
    """Pick up to n clusters for new replicas.

    Skips clusters in the skip set (already retained). Filters by
    readiness, nodeSelector match, and free node capacity. Returns at
    most n candidates sorted alphabetically.
    """
    candidates: list[Candidate] = []
    for cluster in clusters:
        if cluster.metadata.name in skip:
            continue
        if not _cluster_ready(cluster):
            continue

        match = _first_matching_pool(cluster, shape, requests)
        if match is None:
            continue
        pool_name, resolved = match

        # Subtract nodes consumed by other deployments' replicas on this
        # cluster. Our own replicas can't be on this cluster (we skipped
        # those above). The available-node gate is the only cross-deployment
        # capacity check: device-count contention BETWEEN deployments (two
        # replicas wanting all of a node's GPUs) is left to DRA admission on the
        # workload cluster, which rejects a pod whose ResourceClaim can't be
        # satisfied. We track per-device count only WITHIN a placement (see
        # _match_pool), to avoid pinning a single replica to a pool DRA can't
        # satisfy at all.
        used_nodes = _used_nodes(deployment, cluster, all_replicas)
        available = _cluster_nodes(cluster, pool_name) - used_nodes
        if available < shape.nodes_per_replica:
            continue

        candidates.append(
            Candidate(
                name=cluster.metadata.name,
                gateway_address=cluster.status.gateway.address,
                pool=pool_name,
                device_requests=resolved,
            )
        )

    candidates.sort(key=lambda c: c.name)
    return candidates[:n]


def _cluster_nodes(cluster: icv1alpha1.InferenceCluster, pool_name: str) -> int:
    """Total nodes the matched pool has."""
    pool = _pool_by_name(cluster, pool_name)
    return int(pool.nodes or 0) if pool is not None else 0


def _used_nodes(deployment, cluster, all_replicas) -> int:
    """Sum nodes consumed by other deployments' replicas on this cluster.

    Other deployments' replicas are read from observed state. Each replica
    reports its topology, from which we derive nodes-per-replica. Our own
    replicas are excluded - the scheduler treats this deployment's own demand
    separately.
    """
    used = 0
    for r in all_replicas:
        if (r.metadata.labels or {}).get(_LABEL_DEPLOYMENT) == deployment.metadata.name:
            continue
        if r.spec.clusterName != cluster.metadata.name:
            continue
        if not r.spec.workers:
            continue
        used += topology_shape(r.spec.workers).nodes_per_replica
    return used
