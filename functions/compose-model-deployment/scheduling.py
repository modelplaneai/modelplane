"""Federation matcher — pick (InferenceCluster, pool) per ModelReplica.

Stage 1 of two-stage scheduling. Operates on declared substrate state only:

  - InferenceCluster.metadata.labels     — cluster-level matching
  - InferenceCluster.spec.nodePools[]    — list of pools, each with a class ref
  - InferenceClass.spec.capabilities     — typed capabilities for CEL matching
  - InferenceCluster.spec.nodePools[].maxNodes — capacity ceiling
  - existing ModelReplicas               — for sticky placement + capacity used

Federation never reads runtime DRA ResourceSlices. DRA grounding happens at
stage 2 (in the renderer) when the pod actually lands.

This is a sketch — assumes Nic's API shape from #64. The proto types aren't
generated yet; we use plain dicts / dataclasses so the algorithm reads on its
own. Real implementation slots into the existing crossplane.function pattern.

Use cases this exercises (each row in the test plan should hit one):

  Single-node, single-GPU      examples/workloads/gpt-oss-20b.yaml
  Multi-node TP+PP             examples/workloads/kimi-k2.yaml
  Multi-node FP8               examples/workloads/qwen3-coder.yaml
  Multi-region (regional MDs)  examples/workloads/kimi-k2-eu.yaml
  Disaggregated P/D            (will be added with #64 — top-level decode +
                                spec.prefill block)
  Multi-replica spread         spec.replicas=N → N placements; capacity
                               drives the spread across the fleet
"""

import math
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Types — plain dataclasses standing in for the generated protos.
# Each maps to a field on the new XRDs (Nic's #64 sketch).
# ---------------------------------------------------------------------------


@dataclass
class Topology:
    """ModelDeployment.spec.topology (or .prefill.topology).

    Discriminated by `strategy`. The matcher reads this to derive how many
    nodes and GPUs-per-node a replica needs from a pool.
    """

    strategy: str  # "Tensor" | "TensorPipeline" | "DataExpert"
    tensor: int = 0
    pipeline: int = 0  # required when strategy == "TensorPipeline"
    data: int = 0  # required when strategy == "DataExpert"
    data_local: int = 0  # required when strategy == "DataExpert"
    instances: int = 1

    def shape(self) -> tuple[int, int]:
        """Return (nodes_per_instance, gpus_per_node) for one role-instance.

        Multiplied by instances to get the role's total footprint.
        """
        if self.strategy == "Tensor":
            return 1, self.tensor
        if self.strategy == "TensorPipeline":
            return self.pipeline, self.tensor
        if self.strategy == "DataExpert":
            return self.data // self.data_local, self.data_local * self.tensor
        raise ValueError(f"unknown topology strategy: {self.strategy}")


@dataclass
class RoleSpec:
    """One role's compute requirements — decode (top-level) or prefill block."""

    node_selector_cel: str  # ModelDeployment.spec[.prefill].nodeSelector.cel
    topology: Topology


@dataclass
class ModelDeploymentSpec:
    name: str
    namespace: str
    cluster_selector: dict[str, str]  # matchLabels
    replicas: int
    decode: RoleSpec
    prefill: RoleSpec | None = None  # presence ⇒ disaggregated

    @property
    def disaggregated(self) -> bool:
        return self.prefill is not None


@dataclass
class InferenceClass:
    """Resolved hardware bundle. Capabilities is an open key/value map.

    Values may be strings, numbers, lists, or {type, value} for decorated
    types (e.g. version). The CEL evaluator reads this exact shape.
    """

    name: str
    capabilities: dict[str, Any]
    gpu_count: int  # convenience: capabilities["gpu.count"], used for sizing


@dataclass
class Pool:
    """InferenceCluster.spec.nodePools[] resolved against its class."""

    name: str
    cls: InferenceClass
    max_nodes: int


@dataclass
class InferenceCluster:
    name: str
    labels: dict[str, str]  # InferenceCluster.metadata.labels
    pools: list[Pool]


@dataclass
class ExistingPlacement:
    """An existing ModelReplica's footprint for capacity accounting."""

    md_name: str
    md_namespace: str
    replica_index: int
    cluster: str
    decode_pool: str
    decode_nodes: int
    prefill_pool: str | None
    prefill_nodes: int


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------


@dataclass
class RolePlacement:
    pool: str
    nodes_used: int
    gpus_per_node: int
    instances: int


@dataclass
class Placement:
    replica_index: int
    cluster: str
    decode: RolePlacement
    prefill: RolePlacement | None  # None for unified deployments


@dataclass
class MatchTrace:
    """Per-cluster reason a candidate was rejected. Surfaced on MD.status."""

    cluster: str
    pool: str | None
    reason: str
    detail: str = ""


@dataclass
class MatchResult:
    placements: list[Placement] = field(default_factory=list)
    trace: list[MatchTrace] = field(default_factory=list)

    @property
    def fully_scheduled(self) -> bool:
        return len(self.placements) > 0


# ---------------------------------------------------------------------------
# CEL evaluation — placeholder.
# Real implementation: cel-python or a Go shim. The expression is a single
# boolean predicate over `capabilities[<key>]` access patterns.
# ---------------------------------------------------------------------------


def eval_cel(expr: str, capabilities: dict[str, Any]) -> bool:
    """Stand-in for CEL evaluation against a pool's capability map.

    Real impl: bind `capabilities` as the variable, parse + check + eval the
    expression with cel-python. Empty / missing expr ⇒ True (no constraint).
    """
    # Sketch: production code lives in a real CEL evaluator.
    raise NotImplementedError("CEL evaluator wiring is out of scope for this sketch")


# ---------------------------------------------------------------------------
# Capacity accounting
# ---------------------------------------------------------------------------


def role_footprint(role: RoleSpec) -> tuple[int, int, int]:
    """Return (nodes_per_instance, gpus_per_node, total_instances) for a role."""
    nodes_per_inst, gpus_per_node = role.topology.shape()
    return nodes_per_inst, gpus_per_node, role.topology.instances


def role_nodes_required(role: RoleSpec) -> int:
    """Total nodes a single ModelReplica needs from a pool for this role."""
    nodes_per_inst, _, instances = role_footprint(role)
    return nodes_per_inst * instances


def pool_used_nodes(pool: Pool, cluster: str, existing: list[ExistingPlacement]) -> int:
    """Nodes already consumed in this pool by other ModelReplicas."""
    used = 0
    for p in existing:
        if p.cluster != cluster:
            continue
        if p.decode_pool == pool.name:
            used += p.decode_nodes
        if p.prefill_pool == pool.name:
            used += p.prefill_nodes
    return used


def pool_fits_role(pool: Pool, role: RoleSpec) -> bool:
    """The pool's class has enough GPUs per node for the role's per-node demand.

    Capacity check (free vs used) happens separately; this is the static
    feasibility check (is the per-node shape even possible here?).
    """
    nodes_per_inst, gpus_per_node = role.topology.shape()
    return pool.cls.gpu_count >= gpus_per_node


# ---------------------------------------------------------------------------
# Matcher entry point
# ---------------------------------------------------------------------------


def match(
    md: ModelDeploymentSpec,
    clusters: list[InferenceCluster],
    existing: list[ExistingPlacement],
) -> MatchResult:
    """Run federation matching for a ModelDeployment.

    Algorithm:
      1. Reuse existing placements (sticky) for replica indices that already
         have a ModelReplica. The matcher never moves a placement once made;
         re-placement is handled out-of-band by the eviction controller.
      2. For each remaining replica index 0..replicas-1, walk all (cluster,
         pool) pairs, filter by:
           A) clusterSelector.matchLabels ⊆ IC.metadata.labels
           B) decode role's CEL passes against pool.cls.capabilities
           C) pool fits decode role's per-node shape (gpu count check)
           D) capacity headroom: pool.max_nodes − used ≥ nodes_required
           E) for disagg: a second pool in the SAME cluster passes the
              prefill role's CEL + capacity. Decode and prefill must
              co-locate (KV cache transfer).
      3. Score remaining (cluster, decode_pool[, prefill_pool]) candidates
         by capacity headroom + spread bonus (prefer ICs not yet hosting
         this MD). Stable hash tie-break.
      4. Reserve the chosen capacity in our local view so subsequent
         replicas don't double-count.
    """
    result = MatchResult()
    md_existing = [
        e for e in existing if e.md_name == md.name and e.md_namespace == md.namespace
    ]

    # Step 1: sticky placements for replicas that already exist.
    by_index = {e.replica_index: e for e in md_existing}
    placed_indices: set[int] = set()
    for idx, e in by_index.items():
        if idx >= md.replicas:
            continue  # scaled down; will be GC'd by the composer
        result.placements.append(_sticky_placement(md, e))
        placed_indices.add(idx)

    # Step 2-4: schedule the rest.
    # Local view of capacity that includes both real existing + decisions
    # we make in this pass.
    working = list(existing)

    for idx in range(md.replicas):
        if idx in placed_indices:
            continue

        candidates = _candidates_for_replica(md, clusters, working, result.trace)
        if not candidates:
            continue  # trace already populated

        winner = _pick(candidates, md, clusters, result.placements)
        result.placements.append(_to_placement(md, idx, winner))

        # Reserve in our working set so later replicas see this consumed.
        working.append(_reservation(md, idx, winner))

    return result


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


@dataclass
class _Candidate:
    cluster: InferenceCluster
    decode_pool: Pool
    prefill_pool: Pool | None
    decode_nodes_free: int
    prefill_nodes_free: int


def _candidates_for_replica(
    md: ModelDeploymentSpec,
    clusters: list[InferenceCluster],
    existing: list[ExistingPlacement],
    trace: list[MatchTrace],
) -> list[_Candidate]:
    out: list[_Candidate] = []

    decode_nodes_needed = role_nodes_required(md.decode)
    prefill_nodes_needed = role_nodes_required(md.prefill) if md.disaggregated else 0

    for ic in clusters:
        # A) Cluster-level label match.
        if not _matches_labels(md.cluster_selector, ic.labels):
            trace.append(MatchTrace(ic.name, None, "clusterSelector"))
            continue

        # B+C+D) Find decode pools.
        decode_pools = _eligible_pools(
            ic, md.decode, decode_nodes_needed, existing, trace
        )
        if not decode_pools:
            continue  # trace already populated

        if not md.disaggregated:
            for dp, free in decode_pools:
                out.append(_Candidate(ic, dp, None, free, 0))
            continue

        # E) For disagg: prefill must land on a pool in the SAME cluster.
        prefill_pools = _eligible_pools(
            ic, md.prefill, prefill_nodes_needed, existing, trace
        )
        if not prefill_pools:
            continue

        # Pick the (decode, prefill) pair that maximizes the min-headroom.
        # Decode and prefill can share a pool only if its free capacity
        # covers both. Most workloads use different classes for the two
        # roles, so distinct pools is the common case.
        for dp, dfree in decode_pools:
            for pp, pfree in prefill_pools:
                if dp.name == pp.name and dfree < decode_nodes_needed + prefill_nodes_needed:
                    continue
                out.append(_Candidate(ic, dp, pp, dfree, pfree))

    return out


def _eligible_pools(
    ic: InferenceCluster,
    role: RoleSpec,
    nodes_needed: int,
    existing: list[ExistingPlacement],
    trace: list[MatchTrace],
) -> list[tuple[Pool, int]]:
    """Return (pool, free_nodes) for pools that pass CEL + fit + capacity."""
    out: list[tuple[Pool, int]] = []
    for pool in ic.pools:
        try:
            ok = eval_cel(role.node_selector_cel, pool.cls.capabilities) if role.node_selector_cel else True
        except NotImplementedError:
            ok = True  # sketch — see CEL placeholder
        if not ok:
            trace.append(MatchTrace(ic.name, pool.name, "nodeSelector.cel"))
            continue
        if not pool_fits_role(pool, role):
            trace.append(MatchTrace(ic.name, pool.name, "shape"))
            continue
        used = pool_used_nodes(pool, ic.name, existing)
        free = pool.max_nodes - used
        if free < nodes_needed:
            trace.append(
                MatchTrace(ic.name, pool.name, "capacity", f"{free}/{pool.max_nodes} free")
            )
            continue
        out.append((pool, free))
    return out


def _pick(
    candidates: list[_Candidate],
    md: ModelDeploymentSpec,
    clusters: list[InferenceCluster],
    chosen_so_far: list[Placement],
) -> _Candidate:
    """Score and pick a winner.

    score = headroom — primary
          + spread_bonus(cluster, this MD's chosen so far)
          + stable hash tie-break
    """
    chosen_clusters = {p.cluster for p in chosen_so_far}

    def score(c: _Candidate) -> tuple[int, int, int]:
        head = min(c.decode_nodes_free, c.prefill_nodes_free or c.decode_nodes_free)
        spread = 0 if c.cluster.name in chosen_clusters else 1
        tie = abs(hash((md.name, c.cluster.name, c.decode_pool.name))) % 100
        return (head, spread, tie)

    return max(candidates, key=score)


def _to_placement(md: ModelDeploymentSpec, idx: int, c: _Candidate) -> Placement:
    return Placement(
        replica_index=idx,
        cluster=c.cluster.name,
        decode=RolePlacement(
            pool=c.decode_pool.name,
            nodes_used=role_nodes_required(md.decode),
            gpus_per_node=md.decode.topology.shape()[1],
            instances=md.decode.topology.instances,
        ),
        prefill=(
            RolePlacement(
                pool=c.prefill_pool.name,  # type: ignore[union-attr]
                nodes_used=role_nodes_required(md.prefill),  # type: ignore[arg-type]
                gpus_per_node=md.prefill.topology.shape()[1],  # type: ignore[union-attr]
                instances=md.prefill.topology.instances,  # type: ignore[union-attr]
            )
            if md.disaggregated
            else None
        ),
    )


def _sticky_placement(md: ModelDeploymentSpec, e: ExistingPlacement) -> Placement:
    """Reconstruct a Placement from an existing ModelReplica's recorded fields.

    The matcher doesn't recompute pools or scoring for already-placed
    replicas; the renderer reads them off the ModelReplica.
    """
    return Placement(
        replica_index=e.replica_index,
        cluster=e.cluster,
        decode=RolePlacement(
            pool=e.decode_pool,
            nodes_used=e.decode_nodes,
            gpus_per_node=md.decode.topology.shape()[1],
            instances=md.decode.topology.instances,
        ),
        prefill=(
            RolePlacement(
                pool=e.prefill_pool,  # type: ignore[arg-type]
                nodes_used=e.prefill_nodes,
                gpus_per_node=md.prefill.topology.shape()[1] if md.prefill else 0,
                instances=md.prefill.topology.instances if md.prefill else 0,
            )
            if md.prefill is not None and e.prefill_pool
            else None
        ),
    )


def _reservation(md: ModelDeploymentSpec, idx: int, c: _Candidate) -> ExistingPlacement:
    return ExistingPlacement(
        md_name=md.name,
        md_namespace=md.namespace,
        replica_index=idx,
        cluster=c.cluster.name,
        decode_pool=c.decode_pool.name,
        decode_nodes=role_nodes_required(md.decode),
        prefill_pool=c.prefill_pool.name if c.prefill_pool else None,
        prefill_nodes=role_nodes_required(md.prefill) if md.disaggregated else 0,
    )


def _matches_labels(selector: dict[str, str], labels: dict[str, str]) -> bool:
    return all(labels.get(k) == v for k, v in selector.items())
