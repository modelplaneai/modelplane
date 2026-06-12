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

A replica is a set of worker groups co-scheduled onto one cluster. Each group
is placed on its own pool of that cluster (groups may share a pool or land on
disjoint ones), and a group's members may carry different nodeSelectors - a
Leader and its Workers can ask for different hardware. The scheduler therefore
asks, for each candidate cluster, whether every group can be assigned a pool
with enough free nodes, all on that one cluster.

Scheduling runs in two phases:

1. Retain. For each existing replica, keep its (cluster, index) if the cluster
   still exists and every group's pinned pool still satisfies the (possibly
   edited) nodeSelectors. Retention is otherwise unconditional: a healthy
   replica is never moved or dropped to improve the global picture. A degraded
   cluster (not Ready, or no gateway address) is still retained - transient
   outages surface via the deployment's conditions, not re-placement. This is
   what makes the scheduler stable: existing placements are inputs, not
   decisions.

2. Fill. If the deployment wants more replicas than were retained, place the
   shortfall one at a time. Each new replica goes to the eligible cluster
   hosting the fewest of this deployment's replicas (spread first, pack only
   when every eligible cluster already has its share), against a running ledger
   of free node capacity so we never overcommit a cluster. If the deployment
   wants fewer, drop the highest-index replicas first, consolidating off the
   clusters we packed onto last.

Capacity is gated on nodes, not on individual DRA devices. The per-node device
count is a device request's count; the only number the scheduler reads from a
group is its node cost, which it gates against a pool's available nodes.
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

# Member roles.
_ROLE_WORKER = "Worker"


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
class MemberPlacement:
    """The resolved DRA requests for one member of a placed group.

    A member runs its own pods (a Standalone, a Leader, or a Worker's
    followers); each pod binds GPUs through a ResourceClaim built from these
    requests. Always non-empty: a pool matches a member only when at least one
    of its requests resolves to a claim: DRA device.
    """

    device_requests: list[DeviceRequest] = field(default_factory=list)


@dataclass
class GroupPlacement:
    """A worker group's placement within a replica: its pool and resolved requests.

    name and the per-member device requests are stamped onto the ModelReplica's
    matching group; pool becomes the group's spec.nodePoolName. members is in
    the deployment's member order, so it lines up with the group's members.
    """

    name: str
    pool: str = ""
    members: list[MemberPlacement] = field(default_factory=list)


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
    # Per-group placement: the pool each of the replica's groups was assigned
    # and that group's resolved device requests. One entry per group in
    # deployment order. Always populated for a scheduled replica.
    groups: list[GroupPlacement] = field(default_factory=list)


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


@dataclass
class _CompiledMember:
    """A group member with its nodeSelector requests compiled."""

    requests: list[_CompiledRequest]


@dataclass
class _CompiledGroup:
    """A worker group reduced to what the scheduler needs.

    name identifies the group; nodes is its total node cost (pods x replicas);
    members carries each member's compiled nodeSelector requests, used to match
    a pool. A group lands on one pool that satisfies every member.
    """

    name: str
    nodes: int
    members: list[_CompiledMember]


def _member_pods(member) -> int:
    """Pods a single member contributes: a Worker's follower count, else 1.

    A Standalone or a Leader is always exactly one pod; only a Worker fans out
    to count follower pods (one per node).
    """
    if (member.role or "Standalone") == _ROLE_WORKER:
        return int(member.count or 1)
    return 1


def _group_nodes(group) -> int:
    """Total nodes one worker group consumes.

    A group is pods x replicas nodes, where pods is the sum of its members'
    pod counts: 1 for a Standalone, or 1 (Leader) + the Worker's count.
    """
    pods = sum(_member_pods(m) for m in group.members)
    return pods * int(group.replicas or 1)


def _compile_member(member) -> _CompiledMember:
    """Compile one member's nodeSelector device requests.

    Raises cel.CELCompileError on a malformed expression; the caller turns that
    into an InvalidNodeSelector condition.
    """
    requests = []
    for req in member.nodeSelector.devices:
        cel_selectors = [s.cel for s in req.selectors if s.cel]
        requests.append(
            _CompiledRequest(
                name=req.name,
                count=int(req.count or 1),
                cel_selectors=cel_selectors,
                programs=[cel.Program(c) for c in cel_selectors],
            )
        )
    return _CompiledMember(requests=requests)


def compile_groups(deployment: mdv1alpha1.ModelDeployment) -> list[_CompiledGroup]:
    """Compile every group's members' nodeSelector selectors once.

    Each member's nodeSelector is required (the XRD enforces at least one device
    request), so GPUs always bind through a DRA ResourceClaim derived from these
    requests. Raises cel.CELCompileError on a malformed expression.
    """
    groups = []
    for group in deployment.spec.workers:
        groups.append(
            _CompiledGroup(
                name=group.name,
                nodes=_group_nodes(group),
                members=[_compile_member(m) for m in group.members],
            )
        )
    return groups


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


def _device_satisfies(device, programs: list[cel.Program]) -> bool:
    """Whether a pool device satisfies every selector (all ANDed)."""
    # by_alias keeps the DRA wire names (bool/int, not the generated bool_/int_
    # Python attribute names) so the CEL activation sees device.attributes the
    # way DRA selectors expect.
    raw = device.model_dump(by_alias=True, exclude_none=True)
    return all(p.matches(raw) for p in programs)


def _match_member(remaining: list[int], devices: list, member: _CompiledMember) -> list[DeviceRequest] | None:
    """Match one member's requests against a pool's remaining device counts.

    Returns the resolved claim: DRA DeviceRequests when the pool satisfies every
    request AND at least one matched device is claim: DRA, or None when the
    member fails any request or matches only synthetic devices. Decrements
    `remaining` in place as requests consume device count.

    A member's pods run on their own nodes, so a member's requests are matched
    against the pool's per-node devices independently of other members. Within a
    member, assignment is greedy in request order: each request takes the first
    device that satisfies it and has count left, with no backtracking. (See the
    module docstring on why greedy is exact for the request shapes that occur in
    practice, and fails safe otherwise.)

    A member that resolves only synthetic devices (claim: Synthetic, matched for
    fleet scheduling but never claimed) has nothing to claim, so we reject the
    pool for that member: its pods would have no ResourceClaim to bind GPUs
    through.
    """
    resolved: list[DeviceRequest] = []
    for req in member.requests:
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
    if not resolved:
        return None
    return resolved


def _match_group(pool, group: _CompiledGroup) -> list[MemberPlacement] | None:
    """Match a whole group against a pool.

    Returns one MemberPlacement per member (in member order) when every member's
    requests resolve against the pool, or None when any member fails. Each
    member's pods occupy their own nodes, so members don't contend for one
    node's devices: every member is matched against a fresh per-pool device
    budget. (Two members CAN both ask for the pool's GPUs - they run on
    different nodes of the same pool.)
    """
    devices = pool.devices or []
    placements: list[MemberPlacement] = []
    for member in group.members:
        remaining = [int(d.count or 1) for d in devices]
        resolved = _match_member(remaining, devices, member)
        if resolved is None:
            return None
        placements.append(MemberPlacement(device_requests=resolved))
    return placements


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
    fill phase then decrements it via consume() as it places each new replica's
    groups, which is what stops a single scheduling pass overcommitting a pool.
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
    were dropped from the retained set (cluster gone, or a pinned pool no longer
    matches the nodeSelectors). Those are being deleted, so their nodes are
    freeing up and must be available to the fill phase that re-places them -
    otherwise re-placement (delete-old + create-new) could never converge.

    Every counted replica is charged per group at its OWN observed node cost
    (derived from its spec.workers), to the pool that group is pinned to. A
    group pinned to a known pool is subtracted from that pool. One with no pool
    pin (or naming a pool no longer published) can't be attributed to a specific
    pool, so it's charged to EVERY pool on its cluster - deliberately
    conservative: it can only make the gate decline to pack where it technically
    could, never overcommit.
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
        # dict membership, keeps an unpinned group from ever colliding with a
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
        for group in r.spec.workers:
            charge(r.spec.clusterName, group.nodePoolName or "", _group_nodes(group))

    return _Ledger(free=free)


def _retain(
    deployment: mdv1alpha1.ModelDeployment,
    clusters_by_name: dict[str, icv1alpha1.InferenceCluster],
    all_replicas: list[mrv1alpha1.ModelReplica],
    groups: list[_CompiledGroup],
) -> list[Candidate]:
    """Keep existing replicas whose cluster exists and pools still match.

    Returns one Candidate per retained replica, carrying its (cluster, index)
    identity and the per-group placement re-resolved against the current
    nodeSelectors. A replica is dropped from the retained set (and so re-placed
    by the fill phase) when its cluster is gone, or when any group's pinned pool
    no longer satisfies that group's nodeSelectors - the Kubernetes "template
    changed, roll the replica" behavior. A degraded-but-present cluster is
    retained.
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
        placements = _retained_placements(r, cluster, groups)
        if placements is None:
            continue
        seen.add(identity)
        retained.append(
            Candidate(
                name=cluster_name,
                index=identity[1],
                gateway_address=_gateway_address(cluster),
                groups=placements,
            )
        )
    return retained


def _retained_placements(
    replica: mrv1alpha1.ModelReplica,
    cluster: icv1alpha1.InferenceCluster,
    groups: list[_CompiledGroup],
) -> list[GroupPlacement] | None:
    """Re-resolve a retained replica's groups against their pinned pools.

    Modelplane follows Kubernetes here. A change to the deployment's
    nodeSelectors is a change to the deployment "template", so - like editing a
    Deployment's Pod template - a replica whose pinned pool no longer matches is
    re-placed (we drop the pin and let the fill phase pick a matching pool). This
    is distinct from a pool's own device attributes drifting under a
    still-matching replica, which we leave pinned (Kubernetes'
    IgnoredDuringExecution: node-label drift does not evict a bound Pod).

    Each group is re-matched against the pool it's currently pinned to, using
    the deployment's current group definition (matched by group name). Returns
    one GroupPlacement per group when every group's pinned pool still matches,
    or None (re-place the whole replica) when:
      * a group carries no pool pin, or
      * its pinned pool no longer exists on the cluster, or
      * its pinned pool no longer satisfies the group's nodeSelectors, or
      * the deployment no longer defines a group of that name.

    A retained replica keeps its EXISTING groups' pins; the deployment's group
    set is matched by name so an edit that adds, removes, or renames a group
    re-places the replica (its observed groups no longer line up).
    """
    groups_by_name = {g.name: g for g in groups}
    if len(replica.spec.workers) != len(groups):
        return None
    placements: list[GroupPlacement] = []
    for observed in replica.spec.workers:
        group = groups_by_name.get(observed.name)
        if group is None:
            return None
        pool_name = observed.nodePoolName
        if not pool_name:
            return None
        pool = _pool_by_name(cluster, pool_name)
        if pool is None:
            return None
        members = _match_group(pool, group)
        if members is None:
            return None
        placements.append(GroupPlacement(name=group.name, pool=pool_name, members=members))
    return placements


def _place_groups(
    cluster: icv1alpha1.InferenceCluster,
    groups: list[_CompiledGroup],
    ledger: _Ledger,
) -> list[GroupPlacement] | None:
    """Assign every group of one replica to a pool on this cluster.

    Each group is placed on the first pool that satisfies all its members and
    has enough free nodes, against a TRIAL copy of this cluster's free capacity
    so two groups of the same replica don't double-book one pool. Returns one
    GroupPlacement per group (in deployment order) when every group fits, or
    None when any group has no eligible pool - the replica can't be co-scheduled
    here.

    Groups are placed in deployment order, each greedily taking the first
    eligible pool. Like device matching within a pool, this is greedy without
    backtracking; in practice a replica's groups either share one large pool or
    target disjoint pools (different hardware), so order doesn't starve a later
    group. It fails safe: a false reject surfaces as InsufficientCapacity, never
    an overcommit.
    """
    cluster_name = cluster.metadata.name
    # Trial free counts for this cluster's pools, decremented as we place each
    # group so a later group sees capacity an earlier one took.
    trial = {
        (cluster_name, pool.name or ""): ledger.available(cluster_name, pool.name or "")
        for pool in cluster.status.gpuPools or []
    }
    placements: list[GroupPlacement] = []
    for group in groups:
        placed = None
        for pool in cluster.status.gpuPools or []:
            pool_name = pool.name or ""
            if trial[(cluster_name, pool_name)] < group.nodes:
                continue
            members = _match_group(pool, group)
            if members is None:
                continue
            trial[(cluster_name, pool_name)] -= group.nodes
            placed = GroupPlacement(name=group.name, pool=pool_name, members=members)
            break
        if placed is None:
            return None
        placements.append(placed)
    return placements


def _fill(
    groups: list[_CompiledGroup],
    clusters: list[icv1alpha1.InferenceCluster],
    retained: list[Candidate],
    ledger: _Ledger,
    n: int,
) -> list[Candidate]:
    """Place n new replicas, spreading across clusters and packing when forced.

    Places one replica at a time. For each, the eligible clusters are those that
    are Ready and can co-schedule every group on their pools given the ledger.
    Among them we pick the cluster hosting the fewest of this deployment's
    replicas so far (spread), breaking ties by cluster name for determinism. A
    second replica lands on a cluster only once every other eligible cluster
    already has its share; when capacity forces it, replicas pack onto fewer
    clusters. Each placement decrements the ledger (per group, on the pools that
    group took) and takes the lowest free index on its chosen cluster, so the
    next iteration sees the updated load. Stops early (placing fewer than n)
    when no cluster can host another replica - the caller surfaces that as
    InsufficientCapacity.
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
        choice = _pick_cluster(groups, clusters, load, ledger)
        if choice is None:
            break
        cluster, placements = choice
        name = cluster.metadata.name
        index = _lowest_free_index(used_indices.setdefault(name, set()))

        placed.append(
            Candidate(
                name=name,
                index=index,
                gateway_address=cluster.status.gateway.address,
                groups=placements,
            )
        )

        load[name] = load.get(name, 0) + 1
        used_indices[name].add(index)
        for gp in placements:
            ledger.consume(name, gp.pool, _group_by_name(groups, gp.name).nodes)

    return placed


def _group_by_name(groups: list[_CompiledGroup], name: str) -> _CompiledGroup:
    """The compiled group with this name. Always present for a placement we built."""
    return next(g for g in groups if g.name == name)


def _pick_cluster(
    groups: list[_CompiledGroup],
    clusters: list[icv1alpha1.InferenceCluster],
    load: dict[str, int],
    ledger: _Ledger,
) -> tuple[icv1alpha1.InferenceCluster, list[GroupPlacement]] | None:
    """Pick the eligible cluster hosting the fewest of this deployment's replicas.

    Eligible means Ready and able to co-schedule every group on its pools given
    the ledger. The chosen key is (load on the cluster, cluster name): fewest
    replicas first for spread, name for a deterministic tiebreak. load already
    counts only this deployment's replicas (seeded from retained plus those
    placed earlier in the pass). Returns (cluster, group placements) or None
    when no cluster is eligible.
    """
    best = None
    best_key = None
    for cluster in clusters:
        if not _cluster_ready(cluster):
            continue
        placements = _place_groups(cluster, groups, ledger)
        if placements is None:
            continue
        key = (load.get(cluster.metadata.name, 0), cluster.metadata.name)
        if best_key is None or key < best_key:
            best_key = key
            best = (cluster, placements)
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
    clusters_by_name = {c.metadata.name: c for c in clusters}

    # Compile every group member's nodeSelector selectors once and reuse them
    # across every pool of every cluster. Raises CELCompileError on a malformed
    # expression - the caller turns that into a condition.
    groups = compile_groups(deployment)

    retained = _retain(deployment, clusters_by_name, all_replicas, groups)

    if len(retained) > desired:
        retained = _scale_down(retained, desired)

    # Build the ledger AFTER retain and scale-down: it charges the replicas in
    # the final `retained` set (plus other deployments' replicas), and must not
    # charge our dropped or scaled-down replicas, whose nodes are freeing up.
    # Fill then decrements it only as it places NEW replicas.
    ledger = _build_ledger(deployment, clusters, retained, all_replicas)

    placed: list[Candidate] = []
    if len(retained) < desired:
        placed = _fill(groups, clusters, retained, ledger, desired - len(retained))

    result = retained + placed
    result.sort(key=lambda c: (c.name, c.index))
    return result
