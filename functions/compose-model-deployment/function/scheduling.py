"""Schedule a ModelDeployment's replicas across inference clusters.

The scheduler is a pure function of observed state. Every reconcile it is
handed the deployment, every InferenceCluster with its published capacity, and
every existing ModelReplica, and it recomputes the whole placement from
scratch. Given the same observed state it returns the same placement, so it is
safe to run on every reconcile.

A replica's identity is the pair (cluster, index): the cluster it runs on and a
per-cluster-local index that distinguishes co-located replicas of the same
deployment. The index is a collision breaker, not an ordering - replicas are
fungible. A replica never moves cluster. If its cluster is deleted (or, in
future, drained) the replica's desired entry stops being emitted, Crossplane
garbage-collects it, and the fill phase mints a fresh replica elsewhere to
refill the deployment's replica count. Moving is always delete-plus-create,
mirroring how Kubernetes treats a Pod whose node is gone.

Scheduling runs in two phases:

1. Retain. For each existing replica, keep its (cluster, index) if the cluster
   still exists and its pinned pool still satisfies the (possibly edited)
   nodeSelector. Retention is otherwise unconditional: a healthy replica is
   never moved or dropped to improve the global picture. A degraded cluster
   (not Ready, or no gateway address) is still retained - transient outages
   surface via the deployment's conditions, not re-placement. This is what
   makes the scheduler stable: existing placements are inputs, not decisions.

2. Fill. If the deployment wants more replicas than were retained, place the
   shortfall one at a time. Each new replica goes to the eligible cluster
   hosting the fewest of this deployment's replicas (spread first, pack only
   when every eligible cluster already has its share), against a running ledger
   of free node capacity so we never overcommit a cluster. If the deployment
   wants fewer, drop the highest-index replicas first, consolidating off the
   clusters we packed onto last.

Capacity is gated on nodes, not on individual DRA devices. The per-node device
count is a device request's count; the only number the scheduler reads from
topology is nodes-per-replica, which it gates against a pool's available nodes.
Device-count contention BETWEEN deployments is left to DRA admission on the
workload cluster, which is authoritative: it rejects a Pod whose ResourceClaim
can't be satisfied, and the next reconcile sees the updated observed state. The
control-plane scheduler stays deliberately coarse - "could this cluster
plausibly host this replica" - rather than duplicating the real DRA scheduler.
"""

from dataclasses import dataclass, field

from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1

from function import cel

# Labels written by compose-model-deployment, read back here to reconstruct a
# replica's (cluster, index) identity from observed state.
_LABEL_DEPLOYMENT = "modelplane.ai/deployment"
_LABEL_CLUSTER = "modelplane.ai/cluster"
_LABEL_INDEX = "modelplane.ai/replica-index"

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
    """A ModelReplica placement: one replica on one cluster.

    A deployment's placement is a list of these, one per desired replica that
    could be retained or placed. Each is identified by (name, index): the
    cluster name and a per-cluster-local index distinguishing co-located
    replicas. The index is meaningless beyond breaking name collisions.
    """

    name: str
    # Per-cluster-local index distinguishing this replica from others of the
    # same deployment on the same cluster. Stable across reconciles for a
    # retained replica.
    index: int
    # The cluster's gateway address. Empty if the cluster is pinned but
    # currently unavailable (no Ready condition or no gateway address).
    # Callers should not compose a ModelEndpoint when this is empty -
    # there is nothing to route traffic to.
    gateway_address: str = ""
    # The node pool the scheduler matched on this cluster. Empty when the pool of
    # a retained replica can't be re-derived (e.g. the cluster is degraded).
    # Propagated to the ModelReplica as spec.nodePoolName.
    pool: str = ""
    # Resolved claim: DRA device requests for the matched pool, in nodeSelector
    # order. Stamped onto the ModelReplica as spec.deviceRequests. Empty when the
    # cluster is degraded (pool not re-derived) or only synthetic devices matched.
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


def compile_requests(deployment: mdv1alpha1.ModelDeployment) -> list[_CompiledRequest]:
    """Compile every nodeSelector device request's selectors once.

    nodeSelector is required (the XRD enforces at least one device request), so
    GPUs always bind through a DRA ResourceClaim derived from these requests.
    Raises cel.CELCompileError on a malformed expression; the caller turns that
    into an InvalidNodeSelector condition.
    """
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
    # by_alias keeps the DRA wire names (bool/int, not the generated bool_/int_
    # Python attribute names) so the CEL activation sees device.attributes the
    # way DRA selectors expect.
    raw = device.model_dump(by_alias=True, exclude_none=True)
    return all(p.matches(raw) for p in programs)


def _match_pool(pool, requests: list[_CompiledRequest]) -> "list[DeviceRequest] | None":
    """Match a pool against the device requests.

    Returns the resolved claim: DRA DeviceRequests (possibly empty if only
    synthetic devices matched) when the pool satisfies every request, or None
    when the pool fails any request.

    A request matches a pool device when the device has enough UNCONSUMED count
    to cover the request and every selector evaluates true against that device.
    Each resolved DRA request becomes a distinct DeviceRequest in one
    ResourceClaim, and DRA allocates distinct devices per request, so a device's
    count is consumed as requests claim it: two requests cannot both be satisfied
    by the same single-count device, and N requests against one device must fit
    within that device's count. Without this accounting the scheduler would place
    a replica onto a node DRA can't actually satisfy.
    """
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
    for pool in cluster.status.gpuPools or []:
        if (pool.name or "") == pool_name:
            return pool
    return None


def _is_ours(replica: mrv1alpha1.ModelReplica, deployment: mdv1alpha1.ModelDeployment) -> bool:
    """Whether a replica belongs to this deployment."""
    return (replica.metadata.labels or {}).get(_LABEL_DEPLOYMENT) == deployment.metadata.name


def _replica_index(replica: mrv1alpha1.ModelReplica) -> int:
    """The per-cluster-local index recorded on a replica, defaulting to 0.

    Read from the modelplane.ai/replica-index label. A replica from before this
    label existed (or with a malformed value) is treated as index 0; that's the
    natural single-replica-per-cluster case those replicas came from.
    """
    raw = (replica.metadata.labels or {}).get(_LABEL_INDEX)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


@dataclass
class _Ledger:
    """Free node capacity per (cluster, pool).

    Built by _build_ledger from published capacity minus the replicas already
    committed to each pool (see there for exactly which replicas count). The
    fill phase then decrements it via consume() as it places each new replica,
    which is what stops a single scheduling pass overcommitting one cluster.
    """

    free: dict[tuple[str, str], int]

    def available(self, cluster: str, pool: str) -> int:
        return self.free.get((cluster, pool), 0)

    def consume(self, cluster: str, pool: str, nodes: int) -> None:
        self.free[(cluster, pool)] = self.available(cluster, pool) - nodes


def _build_ledger(
    deployment: mdv1alpha1.ModelDeployment,
    clusters: list[icv1alpha1.InferenceCluster],
    retained: list[Candidate],
    all_replicas: list[mrv1alpha1.ModelReplica],
) -> _Ledger:
    """Compute free node capacity per (cluster, pool).

    Starts from each pool's published node count and subtracts the nodes already
    committed to it. A replica counts when it is either:

      * another deployment's replica - capacity we don't control, or
      * one of THIS deployment's RETAINED replicas - a placement we're keeping.

    It deliberately does NOT subtract this deployment's observed replicas that
    were dropped from the retained set (cluster gone, or pinned pool no longer
    matches the nodeSelector). Those are being deleted, so their nodes are
    freeing up and must be available to the fill phase that re-places them -
    otherwise re-placement (delete-old + create-new) could never converge.

    Every counted replica is charged at its OWN observed node cost (derived from
    its spec.workers), not the deployment's current shape. A replica still
    physically consumes whatever it was created with until it's rolled, and
    editing workers without editing the nodeSelector doesn't re-roll it.

    A replica pinned to a known pool is subtracted from that pool. One with no
    pool pin (or naming a pool no longer published) can't be attributed to a
    specific pool, so it's charged to EVERY pool on its cluster. That's
    deliberately conservative: it can only make the gate decline to pack where
    it technically could, never overcommit. In practice every replica this
    function creates records its pool, so unattributed consumption is limited to
    legacy replicas predating the pool pin.
    """
    free: dict[tuple[str, str], int] = {}
    pools_by_cluster: dict[str, list[str]] = {}
    for cluster in clusters:
        name = cluster.metadata.name
        pools_by_cluster[name] = []
        for pool in cluster.status.gpuPools or []:
            free[(name, pool.name or "")] = int(pool.nodes or 0)
            pools_by_cluster[name].append(pool.name or "")

    def charge(cluster_name: str, pool_name: str, nodes: int) -> None:
        # A real pool pin is charged to that pool; anything else (no pin, or a
        # pool no longer published) is unattributable and charged to every pool
        # on the cluster (conservative). Keying on pool_name's truthiness, not on
        # dict membership, keeps an unpinned replica from ever colliding with a
        # published pool.
        if pool_name and (cluster_name, pool_name) in free:
            free[(cluster_name, pool_name)] -= nodes
            return
        for p in pools_by_cluster.get(cluster_name, []):
            free[(cluster_name, p)] -= nodes

    # Identities (cluster, index) of the replicas we're keeping.
    retained_ids = {(c.name, c.index) for c in retained}

    for r in all_replicas:
        if not r.spec.workers:
            continue
        ours = _is_ours(r, deployment)
        # Skip our own replicas that aren't being retained: dropped (re-placed)
        # ones are freeing their nodes, and scaled-down ones are going away.
        if ours and (r.spec.clusterName, _replica_index(r)) not in retained_ids:
            continue
        charge(r.spec.clusterName, r.spec.nodePoolName or "", topology_shape(r.spec.workers).nodes_per_replica)

    return _Ledger(free=free)


def _retain(
    deployment: mdv1alpha1.ModelDeployment,
    clusters_by_name: dict[str, icv1alpha1.InferenceCluster],
    all_replicas: list[mrv1alpha1.ModelReplica],
    requests: list[_CompiledRequest],
) -> list[Candidate]:
    """Keep existing replicas whose cluster exists and pool still matches.

    Returns one Candidate per retained replica, carrying its (cluster, index)
    identity. A replica is dropped from the retained set (and so re-placed by
    the fill phase) when its cluster is gone, or when its pinned pool no longer
    satisfies the nodeSelector - the Kubernetes "template changed, roll the
    replica" behavior. A degraded-but-present cluster is retained.
    """
    retained: list[Candidate] = []
    seen: set[tuple[str, int]] = set()
    for r in all_replicas:
        if not _is_ours(r, deployment):
            continue
        cluster_name = r.spec.clusterName
        if not cluster_name or cluster_name not in clusters_by_name:
            continue
        identity = (cluster_name, _replica_index(r))
        if identity in seen:
            continue
        cluster = clusters_by_name[cluster_name]
        if not _pinned_pool_still_matches(r, cluster, requests):
            continue
        seen.add(identity)
        retained.append(
            Candidate(
                name=cluster_name,
                index=identity[1],
                gateway_address=_gateway_address(cluster),
                pool=r.spec.nodePoolName or "",
                device_requests=_retained_requests(r, cluster, requests),
            )
        )
    return retained


def _pinned_pool_still_matches(
    replica: mrv1alpha1.ModelReplica,
    cluster: icv1alpha1.InferenceCluster,
    requests: list[_CompiledRequest],
) -> bool:
    """Whether a retained replica's pinned pool still satisfies the requests.

    Modelplane follows Kubernetes here. A change to the deployment's nodeSelector
    is a change to the deployment "template", so - like editing a Deployment's
    Pod template - replicas that no longer match are re-placed (Kubernetes does a
    rolling replacement; we drop the pin and let the fill phase pick a matching
    pool). This is distinct from a pool's own device attributes drifting under a
    still-matching replica, which we leave pinned (Kubernetes'
    IgnoredDuringExecution: node-label drift does not evict a bound Pod).

    Returns False (re-place) when:
      * the replica carries no pool pin (it needs a real pool pin), or
      * the pinned pool no longer exists on the cluster, or
      * the pinned pool no longer satisfies the requests.
    """
    pool_name = replica.spec.nodePoolName
    if not pool_name:
        return False
    pool = _pool_by_name(cluster, pool_name)
    if pool is None:
        # Pinned pool is gone from the cluster's published capacity.
        return False
    return _match_pool(pool, requests) is not None


def _retained_requests(replica, cluster, requests: list[_CompiledRequest]) -> list[DeviceRequest]:
    """Resolve device requests for a retained replica's pinned pool.

    Returns the claim: DRA requests for the pinned pool, or empty if the pool
    can't be found (degraded cluster). The pin itself was already validated by
    _pinned_pool_still_matches.
    """
    pool_name = replica.spec.nodePoolName
    if not pool_name:
        return []
    pool = _pool_by_name(cluster, pool_name)
    if pool is None:
        return []
    return _match_pool(pool, requests) or []


def _eligible_pool(
    cluster: icv1alpha1.InferenceCluster,
    shape: Shape,
    requests: list[_CompiledRequest],
    ledger: _Ledger,
) -> "tuple[str, list[DeviceRequest]] | None":
    """Pick the first pool on a cluster that can host one more replica.

    A pool is eligible when it satisfies the nodeSelector requests AND has at
    least nodes-per-replica free in the ledger (which already accounts for
    replicas placed earlier in this pass). Pools are considered in published
    order, which is deterministic. Returns (pool_name, resolved_requests) or
    None if no pool on the cluster is eligible.
    """
    for pool in cluster.status.gpuPools or []:
        name = pool.name or ""
        if ledger.available(cluster.metadata.name, name) < shape.nodes_per_replica:
            continue
        resolved = _match_pool(pool, requests)
        if resolved is None:
            continue
        return name, resolved
    return None


def _fill(
    shape: Shape,
    clusters: list[icv1alpha1.InferenceCluster],
    retained: list[Candidate],
    ledger: _Ledger,
    requests: list[_CompiledRequest],
    n: int,
) -> list[Candidate]:
    """Place n new replicas, spreading across clusters and packing when forced.

    Places one replica at a time. For each, the eligible clusters are those that
    are Ready, have a nodeSelector-matching pool, and have free capacity in the
    ledger. Among them we pick the cluster hosting the fewest of this
    deployment's replicas so far (spread), breaking ties by cluster name for
    determinism. A second replica lands on a cluster only once every other
    eligible cluster already has its share; when capacity forces it, replicas
    pack onto fewer clusters. Each placement decrements the ledger and takes the
    lowest free index on its chosen cluster, so the next iteration sees the
    updated load. Stops early (placing fewer than n) when no cluster can host
    another replica - the caller surfaces that as InsufficientCapacity.
    """
    # Per-cluster load and used indices seeded from retained replicas, so spread
    # accounts for what's already there and new indices don't collide.
    load: dict[str, int] = {}
    used_indices: dict[str, set[int]] = {}
    for c in retained:
        load[c.name] = load.get(c.name, 0) + 1
        used_indices.setdefault(c.name, set()).add(c.index)

    placed: list[Candidate] = []
    for _ in range(n):
        choice = _pick_cluster(shape, clusters, load, ledger, requests)
        if choice is None:
            break
        cluster, pool_name, resolved = choice
        name = cluster.metadata.name
        index = _lowest_free_index(used_indices.setdefault(name, set()))

        placed.append(
            Candidate(
                name=name,
                index=index,
                gateway_address=cluster.status.gateway.address,
                pool=pool_name,
                device_requests=resolved,
            )
        )

        load[name] = load.get(name, 0) + 1
        used_indices[name].add(index)
        ledger.consume(name, pool_name, shape.nodes_per_replica)

    return placed


def _pick_cluster(
    shape: Shape,
    clusters: list[icv1alpha1.InferenceCluster],
    load: dict[str, int],
    ledger: _Ledger,
    requests: list[_CompiledRequest],
) -> "tuple[icv1alpha1.InferenceCluster, str, list[DeviceRequest]] | None":
    """Pick the eligible cluster hosting the fewest of this deployment's replicas.

    Eligible means Ready, with a nodeSelector-matching pool that has free
    capacity in the ledger. The chosen key is (load on the cluster, cluster
    name): fewest replicas first for spread, name for a deterministic tiebreak.
    load already counts only this deployment's replicas (seeded from retained
    plus those placed earlier in the pass). Returns (cluster, pool_name,
    resolved_requests) or None when no cluster is eligible.
    """
    best = None
    best_key = None
    for cluster in clusters:
        if not _cluster_ready(cluster):
            continue
        eligible = _eligible_pool(cluster, shape, requests, ledger)
        if eligible is None:
            continue
        pool_name, resolved = eligible
        key = (load.get(cluster.metadata.name, 0), cluster.metadata.name)
        if best_key is None or key < best_key:
            best_key = key
            best = (cluster, pool_name, resolved)
    return best


def _lowest_free_index(used: set[int]) -> int:
    """The smallest non-negative integer not in used."""
    i = 0
    while i in used:
        i += 1
    return i


def _gateway_address(cluster: icv1alpha1.InferenceCluster) -> str:
    """The cluster's gateway address, or empty when degraded/unset."""
    return (cluster.status.gateway.address if cluster.status.gateway else "") or ""


def _scale_down(retained: list[Candidate], desired: int) -> list[Candidate]:
    """Drop replicas to reach desired, consolidating off the most-packed clusters.

    Each victim is the highest-index replica on whichever cluster currently
    hosts the most of this deployment's replicas. Removing from the most-loaded
    cluster first preserves spread (a cluster never loses its sole replica while
    another still has two), and taking the highest index there keeps the
    survivors' indices dense and stable. Ties between equally-loaded clusters
    break by cluster name for determinism. We only remove at the margin; the
    survivors are never reshuffled.
    """
    survivors = list(retained)
    while len(survivors) > desired:
        load: dict[str, int] = {}
        for c in survivors:
            load[c.name] = load.get(c.name, 0) + 1
        # Victim: the most-loaded cluster, then the lexicographically later
        # cluster name, then the highest index on it. max() picks the largest of
        # each in turn, so later names and higher indices are dropped first.
        victim = max(survivors, key=lambda c: (load[c.name], c.name, c.index))
        survivors.remove(victim)
    return survivors


def schedule(
    deployment: mdv1alpha1.ModelDeployment,
    clusters: list[icv1alpha1.InferenceCluster],
    all_replicas: list[mrv1alpha1.ModelReplica],
) -> list[Candidate]:
    """Pick clusters for a deployment's ModelReplicas.

    Retains existing replicas on their pinned (cluster, index), then fills any
    shortfall by spreading new replicas across clusters (packing onto fewer only
    when capacity forces it). Returns up to deployment.spec.replicas candidates,
    fewer if not enough capacity exists.
    """
    desired = int(deployment.spec.replicas)
    shape = topology_shape(deployment.spec.workers)
    clusters_by_name = {c.metadata.name: c for c in clusters}

    # Compile every nodeSelector request's selectors once and reuse them across
    # every pool of every cluster. Raises CELCompileError on a malformed
    # expression - the caller turns that into a condition.
    requests = compile_requests(deployment)

    retained = _retain(deployment, clusters_by_name, all_replicas, requests)

    if len(retained) > desired:
        retained = _scale_down(retained, desired)

    # Build the ledger AFTER retain and scale-down: it charges the replicas in
    # the final `retained` set (plus other deployments' replicas), and must not
    # charge our dropped or scaled-down replicas, whose nodes are freeing up.
    # Fill then decrements it only as it places NEW replicas.
    ledger = _build_ledger(deployment, clusters, retained, all_replicas)

    placed: list[Candidate] = []
    if len(retained) < desired:
        placed = _fill(shape, clusters, retained, ledger, requests, desired - len(retained))

    result = retained + placed
    result.sort(key=lambda c: (c.name, c.index))
    return result
