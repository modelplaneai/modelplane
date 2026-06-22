# Copyright 2026 The Modelplane Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

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

A replica is a set of engines co-scheduled onto one cluster. Every member of an
engine is placed on one pool of that cluster: each member carries its own
nodeSelector, but the scheduler requires a single pool that satisfies them all.
A gang's members talk over their pool's fabric, and the scheduler can't reason
about fabric - pool identity is the finest grain it has - so it never splits an
engine's members across pools, where a collective could be scattered across
fabrics and silently hang. An engine that no single pool satisfies is not
scheduled on that cluster (see #149 for the same-fabric multi-pool case). A
member with no nodeSelector claims no devices - it matches the engine's pool at
zero node cost, packed onto the gang's nodes by the cluster's scheduler. The
scheduler therefore asks, for each candidate cluster, whether every engine can
be assigned a single pool with enough free nodes, all on that one cluster.

Scheduling runs in two phases:

1. Retain. For each existing replica, keep its (cluster, index) if the cluster
   still exists and every member's pinned pool still satisfies the (possibly
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
member is its node cost, which it gates against a pool's available nodes. A
member's pods occupy nodes only when they claim devices, so a member that
resolves no claim: DRA device - it carried no nodeSelector, or matched only
synthetic devices - costs zero nodes. Device-count contention BETWEEN
deployments is left to DRA admission on the workload cluster, which is
authoritative: it rejects a Pod whose ResourceClaim can't be satisfied, and the
next reconcile sees the updated observed state. The control-plane scheduler
stays deliberately coarse - "could this cluster plausibly host this replica" -
rather than duplicating the real DRA scheduler.
"""

from dataclasses import dataclass, field

from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

from function import cel


def _name(meta: metav1.ObjectMeta | None) -> str:
    """The object's name, always set on resources read from the API server."""
    if meta is None or meta.name is None:
        raise ValueError("metadata.name is unexpectedly absent")
    return meta.name


def _labels(meta: metav1.ObjectMeta | None) -> dict[str, str]:
    """The object's labels, empty when it has none."""
    if meta is None or meta.labels is None:
        return {}
    return meta.labels


# A deployment Member and a replica Member are distinct generated classes with
# the same shape (a deployment fans out to identically-shaped replicas).
# _member_pods reads the fields common to both - role and worker.nodes - off
# whichever it's handed, so it accepts either.
_Member = mdv1alpha1.Member | mrv1alpha1.Member

# Labels written by compose-model-deployment, read back here to reconstruct a
# replica's (cluster, index) identity from observed state.
_LABEL_DEPLOYMENT = "modelplane.ai/deployment"
_LABEL_CLUSTER = "modelplane.ai/cluster"
_LABEL_INDEX = "modelplane.ai/replica-index"

# claim discriminator values on an InferenceClass device.
_CLAIM_DRA = "DRA"

# Member roles.
_ROLE_STANDALONE = "Standalone"
_ROLE_WORKER = "Worker"


def _published_count(value: int | None) -> int:
    """A cluster-published device/node count, floored at zero.

    Cluster status counts (a pool's node count, a device's per-node count) are
    unconstrained on the wire: the InferenceCluster XRD puts no minimum on
    status.gpuPools[].nodes or [].devices[].count, so a published value can be 0
    (an autoscaled-to-zero pool's node count), None, or - in a malformed status -
    negative. All must read as "none available". The load-bearing case is a
    device count of 0: it differs from a deployment-side count, which the XRD
    floors at 1, so `x or 1` is a safe default-for-None there but would wrongly
    turn a published 0 into 1 here. Flooring None and negatives to 0 is defensive
    hygiene - both already fail the capacity gate as a non-positive count.
    """
    if value is None or value < 0:
        return 0
    return value


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
    """A member's placement within a replica: its pool and resolved requests.

    pool becomes the member's spec.nodePoolName on the ModelReplica; the
    resolved device requests become its deviceRequests, from which every one of
    the member's pods builds its ResourceClaim. device_requests is empty for a
    member that claims nothing - it carried no nodeSelector, or its requests
    resolved only synthetic devices - in which case only the pool pin places
    its pods. At least one member of an engine always resolves a claimable
    device: the scheduler rejects placements where none does.
    """

    role: str
    pool: str = ""
    device_requests: list[DeviceRequest] = field(default_factory=list)


@dataclass
class EnginePlacement:
    """An engine's placement within a replica: one placement per member.

    members is in the deployment's member order. Every member shares one pool:
    the scheduler places a whole engine on a single pool or not at all.
    """

    name: str
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
    # Per-engine placement: the pool each member of the replica's engines was
    # assigned and that member's resolved device requests. One entry per engine
    # in deployment order. Always populated for a scheduled replica.
    engines: list[EnginePlacement] = field(default_factory=list)


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
    """A member reduced to what the scheduler needs.

    nodes is the member's total node cost across the engine's copies (its pod
    count x copies), charged only when the member resolves a claimable device.
    requests carries the member's compiled nodeSelector requests, used to match
    a pool; empty when the member carries no nodeSelector and so matches every
    pool.
    """

    role: str
    nodes: int
    requests: list[_CompiledRequest]


@dataclass
class _CompiledEngine:
    """An engine reduced to what the scheduler needs.

    name identifies the engine; members are its compiled members in deployment
    order. Every member of an engine is placed on a single shared pool.
    """

    name: str
    members: list[_CompiledMember]


def _member_pods(member: _Member) -> int:
    """Pods a single member contributes: a Worker's node span, else 1.

    A Standalone or a Leader is always exactly one pod; only a Worker fans out
    to worker.nodes follower pods (one per node).
    """
    if (member.role or _ROLE_STANDALONE) == _ROLE_WORKER and member.worker:
        return int(member.worker.nodes)
    return 1


def _compile_requests(node_selector: mdv1alpha1.NodeSelector) -> list[_CompiledRequest]:
    """Compile a member's nodeSelector device requests.

    Raises cel.CELCompileError on a malformed expression; the caller turns that
    into an InvalidNodeSelector condition.
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


def compile_engines(deployment: mdv1alpha1.ModelDeployment) -> list[_CompiledEngine]:
    """Compile every member's nodeSelector selectors once.

    A member's nodeSelector is optional - a member without one claims no
    devices and compiles to no requests - but at least one member per engine
    carries one (the XRD enforces it). Raises cel.CELCompileError on a
    malformed expression.
    """
    engines = []
    for engine in deployment.spec.engines:
        copies = int(engine.copies or 1)
        members = [
            _CompiledMember(
                role=member.role or _ROLE_STANDALONE,
                nodes=_member_pods(member) * copies,
                requests=_compile_requests(member.nodeSelector) if member.nodeSelector else [],
            )
            for member in engine.members
        ]
        engines.append(_CompiledEngine(name=engine.name, members=members))
    return engines


def _cluster_ready(cluster: icv1alpha1.InferenceCluster) -> bool:
    """Check that the cluster is Ready and has a gateway address.

    A cluster without a Ready=True condition hasn't finished provisioning
    or has become unavailable. A cluster without a gateway address can't
    receive routed traffic. Both must be true for the cluster to be
    schedulable for new placements.
    """
    if not cluster.status or not cluster.status.gateway or not cluster.status.gateway.address:
        return False
    return any(c.type == "Ready" and c.status == "True" for c in cluster.status.conditions or [])


def _device_satisfies(device: icv1alpha1.Device, programs: list[cel.Program]) -> bool:
    """Whether a pool device satisfies every selector (all ANDed)."""
    # by_alias keeps the DRA wire names (bool/int, not the generated bool_/int_
    # Python attribute names) so the CEL activation sees device.attributes the
    # way DRA selectors expect.
    raw = device.model_dump(by_alias=True, exclude_none=True)
    return all(p.matches(raw) for p in programs)


def _match_member(pool: icv1alpha1.GpuPool, member: _CompiledMember) -> list[DeviceRequest] | None:
    """Match a member's requests against a pool.

    Returns the resolved claim: DRA DeviceRequests when the pool satisfies
    every request, or None when the member fails any request. The requests
    describe what ONE of the member's pods needs from its node; every pod of
    the member runs on its own node of the matched pool and claims the same
    devices. A member with no requests matches every pool, resolving nothing.
    The resolved list may also be empty when every matched device is
    claim: Synthetic (matched for fleet scheduling but never claimed); the
    caller rejects an engine NONE of whose members resolve a claimable device,
    since its pods would have no ResourceClaim to bind GPUs through.

    Assignment is greedy in request order: each request takes the first device
    that satisfies it and has count left, with no backtracking. Greedy is exact
    when requests don't overlap on a device (the practical shape: one GPU
    request, maybe a NIC request); when they do overlap it can only
    false-reject, never overcommit, so it fails safe. Members are matched
    independently of each other - their pods run on different nodes, so two
    members' requests never contend for one node's devices.
    """
    devices = pool.devices or []
    remaining = [_published_count(d.count) for d in devices]
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
    return resolved


def _member_cost(member: _CompiledMember, resolved: list[DeviceRequest]) -> int:
    """The nodes a member consumes on its pool, given its resolved requests.

    A member that claims devices occupies its nodes exclusively (the coarse
    pod-per-node model). One that claims nothing - no nodeSelector, or only
    synthetic devices matched - shares the pool's nodes with the pods that do
    claim, so it costs zero NODES. The same rule charges observed replicas in
    the ledger (keyed on their stamped deviceRequests), so fill-time and
    rebuild-time accounting agree.

    "Zero nodes" is the fleet scheduler's whole-node accounting, not zero
    resources: a claimless member (e.g. a gang's coordinator-only leader) is
    still pinned to its gang's GPU pool and its pod still consumes CPU and memory
    on whatever GPU node the cluster's scheduler packs it onto. We model only
    nodes here and leave that to the cluster scheduler, on the assumption a GPU
    node has slack to host a coordinator beside its worker pods. If it doesn't
    the pod stays Pending - real-resource contention the fleet scheduler doesn't
    see. Pinning to the gang's pool is deliberate even so: a coordinator on
    another pool could be in a different zone or interconnect fabric from the
    workers it coordinates.
    """
    return member.nodes if resolved else 0


def _gpu_pools(cluster: icv1alpha1.InferenceCluster) -> list[icv1alpha1.GpuPool]:
    """The cluster's published GPU pools, empty if it has none yet."""
    if not cluster.status:
        return []
    return cluster.status.gpuPools or []


def _pool_by_name(cluster: icv1alpha1.InferenceCluster, pool_name: str) -> icv1alpha1.GpuPool | None:
    """The cluster's published pool with this name, or None."""
    for pool in _gpu_pools(cluster):
        if (pool.name or "") == pool_name:
            return pool
    return None


def _is_ours(replica: mrv1alpha1.ModelReplica, deployment: mdv1alpha1.ModelDeployment) -> bool:
    """Whether a replica belongs to this deployment."""
    return _labels(replica.metadata).get(_LABEL_DEPLOYMENT) == _name(deployment.metadata)


def _replica_index(replica: mrv1alpha1.ModelReplica) -> int:
    """The per-cluster-local index recorded on a replica, defaulting to 0.

    Read from the modelplane.ai/replica-index label. A replica from before this
    label existed (or with a malformed value) is treated as index 0; that's the
    natural single-replica-per-cluster case those replicas came from.
    """
    raw = _labels(replica.metadata).get(_LABEL_INDEX)
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


@dataclass
class _Ledger:
    """Free node capacity per (cluster, pool).

    Built by _build_ledger from published capacity minus the replicas already
    committed to each pool (see there for exactly which replicas count). The
    fill phase then decrements it via consume() as it places each new replica's
    members, which is what stops a single scheduling pass overcommitting a pool.
    """

    free: dict[tuple[str, str], int]

    def available(self, cluster: str, pool: str) -> int:
        return self.free.get((cluster, pool), 0)

    def consume(self, cluster: str, pool: str, nodes: int) -> None:
        self.free[(cluster, pool)] = self.available(cluster, pool) - nodes


def _observed_member_cost(engine: mrv1alpha1.Engine, member: mrv1alpha1.Member) -> int:
    """An observed replica member's node cost, from its stamped deviceRequests.

    Mirrors _member_cost: a member that claims devices occupies its nodes; one
    with no deviceRequests shares its gang's nodes and costs nothing.

    This reads the cost the member was STAMPED with, not what it would resolve
    to now. For a retained member whose pinned pool drifted across the
    claimless/claimable boundary (e.g. a synthetic-only match that now resolves a
    claim: DRA device, or vice versa) the stamped cost and the re-resolved cost
    disagree for one reconcile: the ledger charges the stamped cost here while
    the composed ModelReplica carries the re-resolved one. It reconverges on the
    next reconcile once the new spec is observed, and never durably overcommits
    (DRA admission on the workload cluster is the backstop), so we accept the
    one-pass skew rather than couple ledger accounting to retain re-resolution.
    """
    if not member.deviceRequests:
        return 0
    return _member_pods(member) * int(engine.copies or 1)


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

    Every counted replica is charged per member at its OWN observed node cost
    (derived from its spec.engines), to the pool that member is pinned to. A
    member pinned to a pool the cluster still publishes is subtracted from that
    pool. One pinned to a pool that's since been deleted is charged nothing - its
    pods are unschedulable (Pending) and occupy no node (see `charge`).
    """
    free: dict[tuple[str, str], int] = {}
    for cluster in clusters:
        name = _name(cluster.metadata)
        for pool in _gpu_pools(cluster):
            free[(name, pool.name or "")] = _published_count(pool.nodes)

    def charge(cluster_name: str, pool_name: str, nodes: int) -> None:
        # Charge the member's nodes to the pool it's pinned to. A member whose
        # pool isn't among the cluster's published pools is charged nothing: its
        # pods are hard-pinned (modelplane.ai/pool nodeSelector) to a node label
        # no node carries, so they're unschedulable and occupy no node. (A
        # member's nodePoolName is XRD-required and always names a pool that was
        # published when the replica was placed, so this is the deleted-pool
        # case, not a missing pin.)
        if (cluster_name, pool_name) in free:
            free[(cluster_name, pool_name)] -= nodes

    # Identities (cluster, index) of the replicas we're keeping.
    retained_ids = {(c.name, c.index) for c in retained}

    for r in all_replicas:
        if not r.spec.engines:
            continue
        ours = _is_ours(r, deployment)
        # Skip our own replicas that aren't being retained: dropped (re-placed)
        # ones are freeing their nodes, and scaled-down ones are going away.
        if ours and (r.spec.clusterName, _replica_index(r)) not in retained_ids:
            continue
        for engine in r.spec.engines:
            for member in engine.members or []:
                nodes = _observed_member_cost(engine, member)
                if nodes:
                    charge(r.spec.clusterName, member.nodePoolName or "", nodes)

    return _Ledger(free=free)


def _retain(
    deployment: mdv1alpha1.ModelDeployment,
    clusters_by_name: dict[str, icv1alpha1.InferenceCluster],
    all_replicas: list[mrv1alpha1.ModelReplica],
    engines: list[_CompiledEngine],
) -> list[Candidate]:
    """Keep existing replicas whose cluster exists and pools still match.

    Returns one Candidate per retained replica, carrying its (cluster, index)
    identity and the per-member placement re-resolved against the current
    nodeSelectors. A replica is dropped from the retained set (and so re-placed
    by the fill phase) when its cluster is gone, or when any member's pinned pool
    no longer satisfies that member's nodeSelector - the Kubernetes "template
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
        placements = _retained_placements(r, cluster, engines)
        if placements is None:
            continue
        seen.add(identity)
        retained.append(
            Candidate(
                name=cluster_name,
                index=identity[1],
                gateway_address=_gateway_address(cluster),
                engines=placements,
            )
        )
    return retained


def _retained_placements(
    replica: mrv1alpha1.ModelReplica,
    cluster: icv1alpha1.InferenceCluster,
    engines: list[_CompiledEngine],
) -> list[EnginePlacement] | None:
    """Re-resolve a retained replica's members against their pinned pools.

    Modelplane follows Kubernetes here. A change to the deployment's
    nodeSelectors is a change to the deployment "template", so - like editing a
    Deployment's Pod template - a replica whose pinned pool no longer matches is
    re-placed (we drop the pin and let the fill phase pick a matching pool). This
    is distinct from a pool's own device attributes drifting under a
    still-matching replica, which we leave pinned (Kubernetes'
    IgnoredDuringExecution: node-label drift does not evict a bound Pod).

    Each member is re-matched against the pool it's currently pinned to, using
    the deployment's current member definition (engines matched by name, members
    by position and role). Returns one EnginePlacement per engine when every
    member's pinned pool still matches, or None (re-place the whole replica)
    when:
      * a member carries no pool pin, or
      * its pinned pool no longer exists on the cluster, or
      * its pinned pool no longer satisfies the member's nodeSelector, or
      * no member of an engine resolves a claimable device, or
      * the deployment no longer defines an engine of that name, or its member
        shape (count or roles) changed.

    A retained replica keeps its EXISTING members' pins; the deployment's engine
    set is matched by name so an edit that adds, removes, or renames an engine
    re-places the replica (its observed engines no longer line up).
    """
    engines_by_name = {g.name: g for g in engines}
    if len(replica.spec.engines) != len(engines):
        return None
    placements: list[EnginePlacement] = []
    for observed in replica.spec.engines:
        engine = engines_by_name.get(observed.name)
        if engine is None:
            return None
        members = _retained_members(observed, cluster, engine)
        if members is None:
            return None
        placements.append(EnginePlacement(name=engine.name, members=members))
    return placements


def _retained_members(
    observed: mrv1alpha1.Engine,
    cluster: icv1alpha1.InferenceCluster,
    engine: _CompiledEngine,
) -> list[MemberPlacement] | None:
    """Re-resolve one observed engine's members against their pinned pools.

    Returns one MemberPlacement per member when every pinned pool still exists
    and matches, and at least one member resolves a claimable device, or None
    to re-place the replica.
    """
    observed_members = observed.members or []
    if len(observed_members) != len(engine.members):
        return None
    members: list[MemberPlacement] = []
    for got, member in zip(observed_members, engine.members, strict=True):
        placement = _retained_member(got, cluster, member)
        if placement is None:
            return None
        members.append(placement)
    if not any(m.device_requests for m in members):
        return None
    return members


def _retained_member(
    observed: mrv1alpha1.Member,
    cluster: icv1alpha1.InferenceCluster,
    member: _CompiledMember,
) -> MemberPlacement | None:
    """Re-resolve one observed member against its pinned pool.

    Returns its placement when its role still lines up with the deployment's
    member and its pinned pool still exists and satisfies the member's
    nodeSelector, or None to re-place the replica.
    """
    if (observed.role or _ROLE_STANDALONE) != member.role:
        return None
    pool_name = observed.nodePoolName
    if not pool_name:
        return None
    pool = _pool_by_name(cluster, pool_name)
    if pool is None:
        return None
    resolved = _match_member(pool, member)
    if resolved is None:
        return None
    return MemberPlacement(role=member.role, pool=pool_name, device_requests=resolved)


def _place_engines(
    cluster: icv1alpha1.InferenceCluster,
    engines: list[_CompiledEngine],
    ledger: _Ledger,
) -> list[EnginePlacement] | None:
    """Assign every member of one replica's engines to a pool on this cluster.

    Members are placed against a TRIAL copy of this cluster's free capacity so
    two members of the same replica don't double-book one pool. Returns one
    EnginePlacement per engine (in deployment order) when every member fits, or
    None when any member has no eligible pool - the replica can't be
    co-scheduled here.

    Engines are placed in deployment order, each greedily taking the first
    eligible pool(s). Like device matching within a pool, this is greedy without
    backtracking, so it is not complete: when an earlier engine's selector
    overlaps a pool a later engine also needs and capacity is tight, greedy can
    consume that pool early and fail to place the later engine even though a
    different assignment would have fit them both. It stays greedy because the
    failure is safe - a false reject surfaces as InsufficientCapacity, never an
    overcommit - and the common shapes don't hit it: a gang's members repeat one
    nodeSelector (one pool), and disaggregated phases target disjoint hardware
    (disjoint pools). Overlapping selectors across engines of one replica are the
    case that can false-reject.
    """
    cluster_name = _name(cluster.metadata)
    # Trial free counts for this cluster's pools, decremented as we place each
    # member so a later member sees capacity an earlier one took. Discarded
    # wholesale when any member fails to place, so partial placement never
    # leaks into the real ledger.
    trial = {(pool.name or ""): ledger.available(cluster_name, pool.name or "") for pool in _gpu_pools(cluster)}
    placements: list[EnginePlacement] = []
    for engine in engines:
        members = _place_engine(cluster, engine, trial)
        if members is None:
            return None
        placements.append(EnginePlacement(name=engine.name, members=members))
    return placements


def _place_engine(
    cluster: icv1alpha1.InferenceCluster,
    engine: _CompiledEngine,
    trial: dict[str, int],
) -> list[MemberPlacement] | None:
    """Place a whole engine on a single pool of this cluster, or reject it.

    Every member of an engine lands on one pool. A gang's members talk over
    their pool's fabric (NVLink within a node, InfiniBand within a pool), and
    the scheduler can't reason about fabric - pool identity is the finest grain
    it has, so it can't tell whether two pools share an interconnect. Placing a
    gang's members on different pools would risk scattering a collective across
    fabrics, where it never forms and the gang hangs NotReady with no signal. So
    the scheduler never splits an engine across pools: it places every member on
    the first single pool that satisfies them all with room to spare, or rejects
    the engine on this cluster. Rejection is safe and recoverable - the caller
    surfaces InsufficientCapacity and the fill phase tries another cluster -
    whereas a cross-fabric placement is a silent hang. (Co-scheduling a gang
    across pools that genuinely share a fabric is tracked in #149.)

    A pool hosts the engine when every member matches it, no member that could
    claim a real device elsewhere is stranded on a synthetic-only match, the
    engine has at least one member that resolves a claim: DRA device (or its
    pods would have no ResourceClaim to bind GPUs through), and the pool has free
    nodes for the engine's whole cost. A member with no nodeSelector claims
    nothing, matches every pool, and rides along on the engine's pool at zero
    node cost. Returns one MemberPlacement per member, or None when no single
    pool can host the engine.

    Decrements trial as it places; on failure the caller discards the whole
    trial, so partial decrements don't leak.
    """
    pools = _gpu_pools(cluster)
    # Whether each member's requests resolve a claimable device on any pool of
    # this cluster, ignoring capacity. Used to refuse a placement that would
    # strand a claim-capable member on a pool where it resolves only synthetic
    # devices.
    claimable = [any(_match_member(pool, member) for pool in pools) for member in engine.members]
    # No member can claim anywhere on this cluster, so any placement would
    # leave the engine's pods with no ResourceClaim to bind GPUs through.
    if not any(claimable):
        return None
    for pool in pools:
        pool_name = pool.name or ""
        members = _match_members(pool, engine, claimable)
        if members is None:
            continue
        cost = sum(_member_cost(m, p.device_requests) for m, p in zip(engine.members, members, strict=True))
        if trial[pool_name] < cost:
            continue
        trial[pool_name] -= cost
        return members
    return None


def _match_members(
    pool: icv1alpha1.GpuPool, engine: _CompiledEngine, claimable: list[bool]
) -> list[MemberPlacement] | None:
    """Match every member of an engine against one pool.

    Returns one MemberPlacement per member when the pool satisfies every
    member's requests without stranding a member that could claim elsewhere
    claimless, or None.
    """
    members: list[MemberPlacement] = []
    for member, can_claim in zip(engine.members, claimable, strict=True):
        resolved = _match_member(pool, member)
        if resolved is None or (can_claim and not resolved):
            return None
        members.append(MemberPlacement(role=member.role, pool=pool.name or "", device_requests=resolved))
    return members


def _fill(
    engines: list[_CompiledEngine],
    clusters: list[icv1alpha1.InferenceCluster],
    retained: list[Candidate],
    ledger: _Ledger,
    n: int,
) -> list[Candidate]:
    """Place n new replicas, spreading across clusters and packing when forced.

    Places one replica at a time. For each, the eligible clusters are those that
    are Ready and can co-schedule every member on their pools given the ledger.
    Among them we pick the cluster hosting the fewest of this deployment's
    replicas so far (spread), breaking ties by cluster name for determinism. A
    second replica lands on a cluster only once every other eligible cluster
    already has its share; when capacity forces it, replicas pack onto fewer
    clusters. Each placement decrements the ledger (per member, on the pools the
    members took) and takes the lowest free index on its chosen cluster, so the
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
        choice = _pick_cluster(engines, clusters, load, ledger)
        if choice is None:
            break
        cluster, placements = choice
        name = _name(cluster.metadata)
        index = _lowest_free_index(used_indices.setdefault(name, set()))

        placed.append(
            Candidate(
                name=name,
                index=index,
                gateway_address=_gateway_address(cluster),
                engines=placements,
            )
        )

        load[name] = load.get(name, 0) + 1
        used_indices[name].add(index)
        for ep in placements:
            engine = _engine_by_name(engines, ep.name)
            for member, mp in zip(engine.members, ep.members, strict=True):
                ledger.consume(name, mp.pool, _member_cost(member, mp.device_requests))

    return placed


def _engine_by_name(engines: list[_CompiledEngine], name: str) -> _CompiledEngine:
    """The compiled engine with this name. Always present for a placement we built."""
    return next(g for g in engines if g.name == name)


def _pick_cluster(
    engines: list[_CompiledEngine],
    clusters: list[icv1alpha1.InferenceCluster],
    load: dict[str, int],
    ledger: _Ledger,
) -> tuple[icv1alpha1.InferenceCluster, list[EnginePlacement]] | None:
    """Pick the eligible cluster hosting the fewest of this deployment's replicas.

    Eligible means Ready and able to co-schedule every member on its pools given
    the ledger. The chosen key is (load on the cluster, cluster name): fewest
    replicas first for spread, name for a deterministic tiebreak. load already
    counts only this deployment's replicas (seeded from retained plus those
    placed earlier in the pass). Returns (cluster, engine placements) or None
    when no cluster is eligible.
    """
    best = None
    best_key = None
    for cluster in clusters:
        if not _cluster_ready(cluster):
            continue
        placements = _place_engines(cluster, engines, ledger)
        if placements is None:
            continue
        key = (load.get(_name(cluster.metadata), 0), _name(cluster.metadata))
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
    if not cluster.status or not cluster.status.gateway:
        return ""
    return cluster.status.gateway.address or ""


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
    *,
    fill: bool = True,
) -> list[Candidate]:
    """Pick clusters for a deployment's ModelReplicas.

    Retains existing replicas on their pinned (cluster, index), then fills any
    shortfall by spreading new replicas across clusters (packing onto fewer only
    when capacity forces it). Returns up to deployment.spec.replicas candidates,
    fewer if not enough capacity exists.

    Existing replicas are always retained. fill gates only the placement of NEW
    replicas: with fill=False the schedule is retain-only, holding steady at the
    current placement and adding nothing. A caller passes fill=False when it
    can't yet trust the candidate set - for instance when an input that narrows
    the candidate clusters is unresolved, so placing new replicas now risks
    landing them on clusters that input would have excluded. Retain never
    consults fill: a narrowing input only ever shrinks the candidate set, so its
    absence can't evict a pinned replica.
    """
    desired = int(deployment.spec.replicas)
    clusters_by_name = {_name(c.metadata): c for c in clusters}

    # Sort observed replicas by name (unique per object) so the schedule is a
    # deterministic function of state, not of the order Crossplane happened to
    # deliver required resources in. Both consumers below are order-sensitive:
    # _retain keeps the first replica seen for a colliding (cluster, index), and
    # _build_ledger charges replicas in iteration order. An unsorted input could
    # otherwise place two equal states differently across reconciles.
    all_replicas = sorted(all_replicas, key=lambda r: r.metadata.name or "")

    # Compile every member's nodeSelector selectors once and reuse them
    # across every pool of every cluster. Raises CELCompileError on a malformed
    # expression - the caller turns that into a condition.
    engines = compile_engines(deployment)

    retained = _retain(deployment, clusters_by_name, all_replicas, engines)

    if len(retained) > desired:
        retained = _scale_down(retained, desired)

    placed: list[Candidate] = []
    if fill and len(retained) < desired:
        # Build the ledger AFTER retain and scale-down: it charges the replicas
        # in the final `retained` set (plus other deployments' replicas), and
        # must not charge our dropped or scaled-down replicas, whose nodes are
        # freeing up. Fill then decrements it only as it places NEW replicas.
        ledger = _build_ledger(deployment, clusters, retained, all_replicas)
        placed = _fill(engines, clusters, retained, ledger, desired - len(retained))

    result = retained + placed
    result.sort(key=lambda c: (c.name, c.index))
    return result
