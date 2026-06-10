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
    # The node pool the scheduler matched on this cluster, propagated to the
    # ModelReplica as spec.nodePoolName. Always set: a candidate exists only
    # because a named pool matched (the pool name is XRD-required).
    pool: str = ""
    # Resolved claim: DRA device requests for the matched pool, in nodeSelector
    # order. Stamped onto the ModelReplica as spec.deviceRequests. Always
    # non-empty: a pool matches only when at least one claim: DRA device
    # resolves (see _match_pool), so every scheduled replica has a claim.
    device_requests: list[DeviceRequest] = field(default_factory=list)
    # Prefill role placement, for disaggregated replicas. Empty for unified.
    # Asymmetric with the decode fields above on purpose: decode is required and
    # owns the ModelEndpoint; prefill is optional and internal.
    prefill_pool: str = ""
    prefill_device_requests: list[DeviceRequest] = field(default_factory=list)


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


def prefill_shape(deployment) -> Shape | None:
    """Nodes-per-replica for the prefill role, or None when unified."""
    prefill = getattr(deployment.spec, "prefill", None)
    if not prefill:
        return None
    return topology_shape(prefill.workers)


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
    return _compile_node_selector_requests(deployment.spec.nodeSelector)


def _compile_node_selector_requests(node_selector) -> list[_CompiledRequest]:
    """Compile device requests from an arbitrary nodeSelector object.

    Used to compile the prefill role's nodeSelector without a full
    ModelDeployment. Raises cel.CELCompileError on a malformed expression.
    """
    requests = []
    for req in node_selector.devices:
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


def _match_pool(pool, requests: list[_CompiledRequest]) -> list[DeviceRequest] | None:
    """Match a pool against the device requests.

    Returns the resolved claim: DRA DeviceRequests when the pool satisfies every
    request AND at least one matched device is claim: DRA, or None when the pool
    fails any request or matches only synthetic devices.

    A request matches a pool device when the device has enough UNCONSUMED count
    to cover the request and every selector evaluates true against that device.
    Each resolved DRA request becomes a distinct DeviceRequest in one
    ResourceClaim, and DRA allocates distinct devices per request, so a device's
    count is consumed as requests claim it: two requests cannot both be satisfied
    by the same single-count device, and N requests against one device must fit
    within that device's count. This accounting keeps us from accepting a pool
    DRA can't actually satisfy.

    A replica's serving workload binds its GPUs through this ResourceClaim, so a
    pool that matches only synthetic devices (claim: Synthetic, matched for fleet
    scheduling but never claimed) yields nothing to claim and is not a viable
    host. Synthetic devices are co-selectors that refine placement alongside a
    claimable device; a selector that resolves to synthetic devices alone leaves
    the workload with no claim, so we reject the pool. The deployment then finds
    no eligible pool and surfaces InsufficientCapacity. The ModelDeployment XRD
    documents that a nodeSelector must match at least one claimable device.

    Assignment is GREEDY in request order: each request takes the first device
    that satisfies it and has count left, with no backtracking. Greedy is exact
    when no device satisfies two different requests, and that holds for both
    patterns that occur in practice. First, a workload asking for N of one device
    is a single request, so nothing contends. Second, a workload asking for
    different device DOMAINS (e.g. a GPU and a NIC) writes selectors that read
    different attribute domains, so again no device satisfies two requests and
    order can't starve either.

    Greedy can falsely reject only when two requests' match sets OVERLAP on a
    shared device kind - e.g. a broad request (memory >= 80Gi, matches an H100
    and an H200) and a narrow one (an H200 specifically) against a pool holding
    one of each. If the broad request takes the H200 first, the narrow one finds
    nothing and we reject the pool, though broad->H100, narrow->H200 would have
    fit. This needs one deployment to ask for multiple GPUs of deliberately
    different specificity from one mixed-GPU pool, written as overlapping rather
    than disjoint selectors - a shape no real workload writes (you'd name both
    GPUs, or use one request with a count). It also fails SAFE: a false reject
    surfaces as InsufficientCapacity, never an overcommit or a bad placement, and
    the user can resolve it by making the selectors disjoint.
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
    # Every request matched, but if none resolved to a claim: DRA device the
    # replica would have no ResourceClaim to bind its GPUs through. Reject the
    # pool rather than place a claimless workload.
    if not resolved:
        return None
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
        pf = getattr(r.spec, "prefill", None)
        if pf and pf.workers:
            charge(r.spec.clusterName, pf.nodePoolName or "", topology_shape(pf.workers).nodes_per_replica)

    return _Ledger(free=free)


def _retain(
    deployment: mdv1alpha1.ModelDeployment,
    clusters_by_name: dict[str, icv1alpha1.InferenceCluster],
    all_replicas: list[mrv1alpha1.ModelReplica],
    requests: list[_CompiledRequest],
    prefill_requests: list[_CompiledRequest] | None = None,
) -> list[Candidate]:
    """Keep existing replicas whose cluster exists and pool still matches.

    Returns one Candidate per retained replica, carrying its (cluster, index)
    identity. A replica is dropped from the retained set (and so re-placed by
    the fill phase) when its cluster is gone, or when its pinned pool no longer
    satisfies the nodeSelector - the Kubernetes "template changed, roll the
    replica" behavior. A degraded-but-present cluster is retained.

    For disaggregated deployments (prefill_requests is not None) BOTH the decode
    pool and the prefill pool are checked. A replica whose prefill pinned pool no
    longer satisfies the prefill nodeSelector is re-placed, mirroring the same
    rolling-replacement semantics applied to the decode pool.
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
        if not _pinned_pool_still_matches(r, cluster, requests, prefill_requests=prefill_requests):
            continue
        seen.add(identity)
        candidate = Candidate(
            name=cluster_name,
            index=identity[1],
            gateway_address=_gateway_address(cluster),
            pool=r.spec.nodePoolName or "",
            device_requests=_retained_requests(r, cluster, requests),
        )
        # For disagg replicas, resolve and populate the retained prefill placement.
        if prefill_requests is not None:
            pf = getattr(r.spec, "prefill", None)
            if pf and pf.nodePoolName:
                pf_pool = _pool_by_name(cluster, pf.nodePoolName)
                if pf_pool is not None:
                    candidate.prefill_pool = pf.nodePoolName
                    candidate.prefill_device_requests = _match_pool(pf_pool, prefill_requests) or []
        retained.append(candidate)
    return retained


def _pinned_pool_still_matches(
    replica: mrv1alpha1.ModelReplica,
    cluster: icv1alpha1.InferenceCluster,
    requests: list[_CompiledRequest],
    prefill_requests: list[_CompiledRequest] | None = None,
) -> bool:
    """Whether a retained replica's pinned pool(s) still satisfy the requests.

    Modelplane follows Kubernetes here. A change to the deployment's nodeSelector
    is a change to the deployment "template", so - like editing a Deployment's
    Pod template - replicas that no longer match are re-placed (Kubernetes does a
    rolling replacement; we drop the pin and let the fill phase pick a matching
    pool). This is distinct from a pool's own device attributes drifting under a
    still-matching replica, which we leave pinned (Kubernetes'
    IgnoredDuringExecution: node-label drift does not evict a bound Pod).

    Returns False (re-place) when:
      * the replica carries no decode pool pin (it needs a real pool pin), or
      * the pinned decode pool no longer exists on the cluster, or
      * the pinned decode pool no longer satisfies the decode requests.

    For disaggregated deployments (prefill_requests is not None), also returns
    False when:
      * the replica carries no prefill pool pin, or
      * the pinned prefill pool no longer exists on the cluster, or
      * the pinned prefill pool no longer satisfies the prefill requests.
    """
    pool_name = replica.spec.nodePoolName
    if not pool_name:
        return False
    pool = _pool_by_name(cluster, pool_name)
    if pool is None:
        # Pinned decode pool is gone from the cluster's published capacity.
        return False
    if _match_pool(pool, requests) is None:
        return False
    if prefill_requests is not None:
        pf = getattr(replica.spec, "prefill", None)
        if not pf or not pf.nodePoolName:
            return False
        pf_pool = _pool_by_name(cluster, pf.nodePoolName)
        if pf_pool is None or _match_pool(pf_pool, prefill_requests) is None:
            return False
    return True


def _retained_requests(replica, cluster, requests: list[_CompiledRequest]) -> list[DeviceRequest]:
    """Resolve the claim: DRA requests for a retained replica's pinned pool.

    Only called for a replica _pinned_pool_still_matches already accepted, so the
    pinned pool exists and yields at least one DRA request: _match_pool returns a
    non-empty list here, never None. If that contract were ever broken the empty
    result would surface as an XRD validation error in compose_replicas (which
    requires deviceRequests), not as a silently claimless replica.
    """
    pool = _pool_by_name(cluster, replica.spec.nodePoolName)
    return _match_pool(pool, requests) or []


def _eligible_placement(
    cluster: icv1alpha1.InferenceCluster,
    roles: list[tuple[Shape, list[_CompiledRequest]]],
    ledger: _Ledger,
) -> list[tuple[str, list[DeviceRequest]]] | None:
    """Choose a pool per role on this cluster that jointly fits the ledger.

    roles: list of (shape, requests) - one entry for unified, two for disagg
    (decode then prefill). Picks pool assignments jointly so a shared pool's
    capacity is never double-committed. Returns a list of (pool_name,
    resolved_requests) aligned with roles, or None if no assignment fits.

    The pool count per cluster is small, so an exhaustive search is fine. Each
    assignment is evaluated cumulatively: two roles assigned to the same pool
    must together fit within that pool's available nodes.
    """
    pools = cluster.status.gpuPools or []
    cname = cluster.metadata.name

    # For each role, collect the pools that match its selector (ignoring ledger
    # here; the search step applies the ledger cumulatively).
    per_role_candidates: list[list[tuple[str, list[DeviceRequest]]]] = []
    for _shape, requests in roles:
        role_cands: list[tuple[str, list[DeviceRequest]]] = []
        for pool in pools:
            resolved = _match_pool(pool, requests)
            if resolved is not None:
                role_cands.append((pool.name or "", resolved))
        if not role_cands:
            return None  # No pool satisfies this role at all.
        per_role_candidates.append(role_cands)

    def search(k: int, charged: dict[str, int]) -> list[tuple[str, list[DeviceRequest]]] | None:
        """Recursively assign pools to roles k..len(roles)-1.

        charged tracks extra nodes already committed to each pool name during
        this replica's joint assignment (above whatever the ledger shows), so
        two roles sharing one pool see their costs sum against its capacity.
        """
        if k == len(roles):
            return []
        shape, _ = roles[k]
        for name, resolved in per_role_candidates[k]:
            already_charged = charged.get(name, 0)
            if ledger.available(cname, name) - already_charged >= shape.nodes_per_replica:
                charged2 = dict(charged)
                charged2[name] = already_charged + shape.nodes_per_replica
                rest = search(k + 1, charged2)
                if rest is not None:
                    return [(name, resolved), *rest]
        return None

    return search(0, {})


def _fill(
    shape: Shape,
    clusters: list[icv1alpha1.InferenceCluster],
    retained: list[Candidate],
    ledger: _Ledger,
    requests: list[_CompiledRequest],
    n: int,
    prefill_requests: list[_CompiledRequest] | None = None,
    pf_shape: Shape | None = None,
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

    For disaggregated deployments, prefill_requests and pf_shape are set. The
    decode and prefill pools are then chosen jointly (see _pick_cluster) so a
    shared pool's capacity is never double-committed.
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
        choice = _pick_cluster(
            shape,
            clusters,
            load,
            ledger,
            requests,
            prefill_requests=prefill_requests,
            pf_shape=pf_shape,
        )
        if choice is None:
            break
        cluster, placement = choice
        name = cluster.metadata.name
        index = _lowest_free_index(used_indices.setdefault(name, set()))

        decode_pool, decode_resolved = placement[0]
        candidate = Candidate(
            name=name,
            index=index,
            gateway_address=cluster.status.gateway.address,
            pool=decode_pool,
            device_requests=decode_resolved,
        )
        if len(placement) > 1:
            candidate.prefill_pool, candidate.prefill_device_requests = placement[1]
        placed.append(candidate)

        load[name] = load.get(name, 0) + 1
        used_indices[name].add(index)
        # Consume ledger for decode pool.
        ledger.consume(name, decode_pool, shape.nodes_per_replica)
        # Consume ledger for prefill pool (may be the same pool as decode).
        if len(placement) > 1 and pf_shape is not None:
            ledger.consume(name, candidate.prefill_pool, pf_shape.nodes_per_replica)

    return placed


def _pick_cluster(
    shape: Shape,
    clusters: list[icv1alpha1.InferenceCluster],
    load: dict[str, int],
    ledger: _Ledger,
    requests: list[_CompiledRequest],
    prefill_requests: list[_CompiledRequest] | None = None,
    pf_shape: Shape | None = None,
) -> tuple[icv1alpha1.InferenceCluster, list[tuple[str, list[DeviceRequest]]]] | None:
    """Pick the eligible cluster hosting the fewest of this deployment's replicas.

    Eligible means Ready, with pool(s) jointly satisfying the decode (and
    optionally prefill) roles with enough free capacity in the ledger. The
    chosen key is (load on the cluster, cluster name): fewest replicas first
    for spread, name for a deterministic tiebreak. load already counts only
    this deployment's replicas (seeded from retained plus those placed earlier
    in the pass). Returns (cluster, placement) where placement is a list of
    (pool_name, resolved_requests) aligned with roles, or None when no cluster
    is eligible.

    For unified deployments (prefill_requests=None), behaviour is identical to
    the old single-role _eligible_pool path: one role, one pool.
    """
    roles: list[tuple[Shape, list[_CompiledRequest]]] = [(shape, requests)]
    if prefill_requests is not None and pf_shape is not None:
        roles.append((pf_shape, prefill_requests))

    best: tuple[icv1alpha1.InferenceCluster, list[tuple[str, list[DeviceRequest]]]] | None = None
    best_key = None
    for cluster in clusters:
        if not _cluster_ready(cluster):
            continue
        placement = _eligible_placement(cluster, roles, ledger)
        if placement is None:
            continue
        key = (load.get(cluster.metadata.name, 0), cluster.metadata.name)
        if best_key is None or key < best_key:
            best_key = key
            best = (cluster, placement)
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

    For disaggregated deployments (spec.prefill is set), each replica requires
    BOTH a decode pool and a prefill pool on the same cluster. The pools are
    chosen jointly from one capacity ledger so a shared pool is never
    double-committed. Unified deployments (spec.prefill absent) are unaffected:
    the single-role path is identical to before.
    """
    desired = int(deployment.spec.replicas)
    shape = topology_shape(deployment.spec.workers)
    clusters_by_name = {c.metadata.name: c for c in clusters}

    # Compile every nodeSelector request's selectors once and reuse them across
    # every pool of every cluster. Raises CELCompileError on a malformed
    # expression - the caller turns that into a condition.
    requests = compile_requests(deployment)

    # Compile prefill nodeSelector requests when the deployment is disaggregated.
    pf_shape = prefill_shape(deployment)
    pf_requests: list[_CompiledRequest] | None = None
    prefill = getattr(deployment.spec, "prefill", None)
    if prefill is not None:
        pf_requests = _compile_node_selector_requests(prefill.nodeSelector)

    retained = _retain(deployment, clusters_by_name, all_replicas, requests, prefill_requests=pf_requests)

    if len(retained) > desired:
        retained = _scale_down(retained, desired)

    # Build the ledger AFTER retain and scale-down: it charges the replicas in
    # the final `retained` set (plus other deployments' replicas), and must not
    # charge our dropped or scaled-down replicas, whose nodes are freeing up.
    # Fill then decrements it only as it places NEW replicas.
    ledger = _build_ledger(deployment, clusters, retained, all_replicas)

    placed: list[Candidate] = []
    if len(retained) < desired:
        placed = _fill(
            shape,
            clusters,
            retained,
            ledger,
            requests,
            desired - len(retained),
            prefill_requests=pf_requests,
            pf_shape=pf_shape,
        )

    result = retained + placed
    result.sort(key=lambda c: (c.name, c.index))
    return result
