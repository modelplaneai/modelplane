"""Compose a ModelDeployment into ModelReplicas + ModelEndpoints.

This is the *composer* half of the federation control plane (see also
`compose-model-replica` for the renderer half). It runs once per
ModelDeployment reconcile and:

  1. Asks Crossplane for the substrate it needs (required-resources):
       - all InferenceClusters (cluster-scoped)
       - the InferenceClasses referenced by their nodePools
       - all ModelReplicas already owned by this MD (sticky placement)
  2. Calls `scheduling.match()` — the federation matcher — to pick
     (cluster, pool) per replica index 0..spec.replicas-1.
  3. Emits one ModelReplica per replica_index, carrying the resolved
     placement + the parent MD's resolved fields (so the renderer
     doesn't have to re-fetch them).
  4. Emits one ModelEndpoint per replica (per Nic's API in #64) — the
     reachable URL surface a ModelService can route to.
  5. Sets MD.status.conditions: Scheduled / FullyScheduled / Saturated
     and a per-cluster matchTrace surfaced when scheduling fails.

Sketch quality: the imports below assume Nic's #64 protos exist (they
don't yet). The control flow + condition / require_resources patterns
match the existing function shape; only the data plumbing is updated for
the new API.

Dependencies (what this function reads):

  Required resources (Crossplane "extra resources"):
    name=clusters         InferenceCluster (list, cluster-scoped)
    name=classes          InferenceClass   (list, by name from clusters)
    name=existing-replicas ModelReplica    (list, owner=this MD)

  Composed (this function writes):
    ModelReplica × spec.replicas (sticky by replicaIndex)
    ModelEndpoint × spec.replicas (one per replica)

Use cases (each example exercises a path here):

  examples/workloads/gpt-oss-20b.yaml        Tensor strategy, no disagg
  examples/workloads/kimi-k2.yaml            TensorPipeline, no disagg
  examples/workloads/qwen3-coder.yaml        Multi-node FP8, no disagg
  examples/workloads/kimi-k2-eu.yaml         Multi-region pattern
  spec.replicas: N                           Spread across multiple ICs

  Disaggregation (prefill + decode roles) lands with #64.

Lifecycle ops this enables (each is its own user story):

  Scale up   spec.replicas++  → composer creates new MR; matcher places it
                                where capacity allows (potentially a new IC).
  Scale down spec.replicas--  → composer GCs highest replicaIndex MR first
                                (oldest survives, gateway endpoints stable).
  IC degrades → eviction controller annotates affected MR; this composer
                drops it on next reconcile, matcher re-picks for new MR.
  IC added   → next scale-up event sees it as a candidate; existing MRs
                are sticky and don't move.

  See ../../design/proposed-modelplane-api/design.md for the full picture.
"""

# ---------------------------------------------------------------------------
# Imports — sketch. Real proto imports are gated by the new XRDs landing
# from #64; until then these are placeholders that document the contract.
# ---------------------------------------------------------------------------

from crossplane.function import request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from . import scheduling
from .lib import conditions, defaults, metadata, naming
from .lib import resource as libresource

# When #64 lands these become real:
#   from .model.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
#   from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
#   from .model.ai.modelplane.inferenceclass import v1alpha1 as iclassv1alpha1
#   from .model.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1
#   from .model.ai.modelplane.modelendpoint import v1alpha1 as mev1alpha1


# ---------------------------------------------------------------------------
# Conditions exposed on the ModelDeployment XR.
# ---------------------------------------------------------------------------

CONDITION_TYPE_SCHEDULED = "Scheduled"
CONDITION_TYPE_REPLICAS_READY = "ReplicasReady"

REASON_SCHEDULED_ALL = "AllReplicasScheduled"
REASON_SCHEDULED_PARTIAL = "PartiallyScheduled"
REASON_NO_CLUSTERS = "NoEligibleClusters"
REASON_SATURATED = "FleetSaturated"
REASON_NO_REPLICAS = "ZeroReplicas"
REASON_REPLICAS_READY = "AllReplicasReady"
REASON_REPLICAS_PROGRESSING = "ReplicasProgressing"


class Composer:
    """Composes a ModelDeployment into its child ModelReplicas + ModelEndpoints."""

    def __init__(self, req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
        self.req = req
        self.rsp = rsp
        # ModelDeployment XR (parent) — Nic's #64 shape:
        #   spec.{source, huggingFace, clusterSelector.matchLabels, replicas,
        #         nodeSelector.cel, topology, engine, prefill?}
        self.md = _load_md(req)

        # Substrate snapshots — populated by resolve_inputs().
        self.clusters: list[scheduling.InferenceCluster] = []
        self.existing: list[scheduling.ExistingPlacement] = []

    def compose(self) -> None:
        if not self.resolve_inputs():
            return  # required resources not all observed yet
        result = self.schedule()
        self.compose_replicas(result)
        self.compose_endpoints(result)
        self.write_conditions(result)

    # -----------------------------------------------------------------------
    # 1. Required resources
    # -----------------------------------------------------------------------

    def resolve_inputs(self) -> bool:
        """Declare the substrate this function depends on.

        Crossplane re-runs us until all of these are observed at least once.
        """
        response.require_resources(
            self.rsp,
            name="clusters",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceCluster",
            # Cluster-scoped; no namespace selector.
        )
        response.require_resources(
            self.rsp,
            name="classes",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceClass",
        )
        response.require_resources(
            self.rsp,
            name="existing-replicas",
            api_version="modelplane.ai/v1alpha1",
            kind="ModelReplica",
            match_labels={metadata.LABEL_KEY_DEPLOYMENT: self.md.name},
        )

        observed = self.req.extra_resources
        if not all(k in observed for k in ("clusters", "classes", "existing-replicas")):
            return False  # waiting on a refetch

        self.clusters = _resolve_clusters(observed["clusters"], observed["classes"])
        self.existing = _resolve_existing(observed["existing-replicas"])
        return True

    # -----------------------------------------------------------------------
    # 2. Federation match
    # -----------------------------------------------------------------------

    def schedule(self) -> scheduling.MatchResult:
        return scheduling.match(self.md, self.clusters, self.existing)

    # -----------------------------------------------------------------------
    # 3. Compose ModelReplicas
    # -----------------------------------------------------------------------

    def compose_replicas(self, result: scheduling.MatchResult) -> None:
        """Emit one ModelReplica per Placement.

        ModelReplica spec carries:
          - replicaIndex                       — stable identity 0..N-1
          - target.{cluster, decodePool, prefillPool?} — matcher's decision
          - resolved decode/prefill RoleSpec   — what the renderer renders
          - parentRef                          — back to the MD (owner)
        """
        for p in result.placements:
            mr_name = naming.replica_name(self.md.name, p.replica_index)
            mr_spec = {
                "replicaIndex": p.replica_index,
                "target": {
                    "cluster": p.cluster,
                    "decodePool": p.decode.pool,
                    "prefillPool": p.prefill.pool if p.prefill else None,
                },
                # The renderer reads these instead of re-fetching the MD —
                # makes the renderer pure over the MR + IC + class, no
                # parent lookup needed.
                "decode": _role_to_dict(self.md.decode, p.decode),
                "prefill": (
                    _role_to_dict(self.md.prefill, p.prefill)
                    if p.prefill and self.md.prefill
                    else None
                ),
                "engine": _engine_dict(self.md),
                "source": _source_dict(self.md),
            }
            libresource.add_composed(
                self.rsp,
                name=mr_name,
                api_version="modelplane.ai/v1alpha1",
                kind="ModelReplica",
                metadata={
                    "name": mr_name,
                    "namespace": self.md.namespace,
                    "labels": {
                        metadata.LABEL_KEY_DEPLOYMENT: self.md.name,
                    },
                    "ownerReferences": [_owner_ref(self.md)],
                },
                spec=mr_spec,
            )

    # -----------------------------------------------------------------------
    # 4. Compose ModelEndpoints
    # -----------------------------------------------------------------------

    def compose_endpoints(self, result: scheduling.MatchResult) -> None:
        """One ModelEndpoint per ModelReplica (Nic's #64 design).

        The endpoint's URL is set when the replica's gateway address is
        known — populated downstream by the renderer / status writer.
        ModelService selects across these endpoints by the deployment label.
        """
        for p in result.placements:
            ep_name = naming.endpoint_name(self.md.name, p.replica_index)
            libresource.add_composed(
                self.rsp,
                name=ep_name,
                api_version="modelplane.ai/v1alpha1",
                kind="ModelEndpoint",
                metadata={
                    "name": ep_name,
                    "namespace": self.md.namespace,
                    "labels": {
                        metadata.LABEL_KEY_DEPLOYMENT: self.md.name,
                    },
                    "ownerReferences": [_owner_ref(self.md)],
                },
                spec={
                    # Filled in by status reconcile once the replica's
                    # gateway address is known.
                    "url": "",
                    "api": "OpenAI",
                },
            )

    # -----------------------------------------------------------------------
    # 5. Conditions + matchTrace
    # -----------------------------------------------------------------------

    def write_conditions(self, result: scheduling.MatchResult) -> None:
        if self.md.replicas == 0:
            conditions.set_condition(
                self.rsp,
                type=CONDITION_TYPE_SCHEDULED,
                status="True",
                reason=REASON_NO_REPLICAS,
                message="spec.replicas is 0; nothing to schedule",
            )
            return

        if not result.placements:
            conditions.set_condition(
                self.rsp,
                type=CONDITION_TYPE_SCHEDULED,
                status="False",
                reason=_no_placement_reason(result),
                message=_trace_summary(result.trace),
            )
            return

        if len(result.placements) < self.md.replicas:
            conditions.set_condition(
                self.rsp,
                type=CONDITION_TYPE_SCHEDULED,
                status="False",
                reason=REASON_SCHEDULED_PARTIAL,
                message=(
                    f"{len(result.placements)}/{self.md.replicas} replicas scheduled. "
                    + _trace_summary(result.trace)
                ),
            )
            return

        conditions.set_condition(
            self.rsp,
            type=CONDITION_TYPE_SCHEDULED,
            status="True",
            reason=REASON_SCHEDULED_ALL,
            message=f"{self.md.replicas} replicas scheduled across the fleet",
        )


# ---------------------------------------------------------------------------
# Adapters between Crossplane structs and scheduling dataclasses.
# These are the boundaries; the matcher itself is plain Python.
# ---------------------------------------------------------------------------


def _load_md(req: fnv1.RunFunctionRequest) -> scheduling.ModelDeploymentSpec:
    """Build the matcher's view of the ModelDeployment.

    Sketch — real impl pulls from the generated ModelDeployment proto.
    Demonstrates which fields the matcher consumes (and only those).
    """
    raise NotImplementedError("wire to mdv1alpha1.ModelDeployment when #64 lands")


def _resolve_clusters(
    clusters_raw, classes_raw
) -> list[scheduling.InferenceCluster]:
    """Resolve InferenceClusters + the InferenceClasses they reference.

    Each pool's `class:` ref is replaced inline with the resolved
    InferenceClass.spec.capabilities. Pools whose class isn't observed yet
    are dropped (the matcher won't consider them). Crossplane re-runs us
    when classes appear.
    """
    raise NotImplementedError("walk extra resources when #64 lands")


def _resolve_existing(existing_raw) -> list[scheduling.ExistingPlacement]:
    """Project owned ModelReplicas into matcher form for sticky placement."""
    raise NotImplementedError("walk extra resources when #64 lands")


def _role_to_dict(role, placement: scheduling.RolePlacement) -> dict:
    """Build a ModelReplica.spec.{decode|prefill} block from MD + placement."""
    return {
        "topology": {
            "strategy": role.topology.strategy,
            "tensor": role.topology.tensor,
            "pipeline": role.topology.pipeline,
            "data": role.topology.data,
            "dataLocal": role.topology.data_local,
            "instances": role.topology.instances,
        },
        # nodeSelector.cel carried through verbatim — the renderer turns it
        # into a DRA ResourceClaim against the matched pool's capabilities.
        "nodeSelector": {"cel": role.node_selector_cel},
        "pool": placement.pool,
        "nodesUsed": placement.nodes_used,
        "gpusPerNode": placement.gpus_per_node,
        "instances": placement.instances,
    }


def _engine_dict(md: scheduling.ModelDeploymentSpec) -> dict:
    """Engine config the renderer needs. Pass-through from MD."""
    raise NotImplementedError("populate from md when wired")


def _source_dict(md: scheduling.ModelDeploymentSpec) -> dict:
    """Where to fetch model weights — HuggingFace repo / S3 / GCS / PVC."""
    raise NotImplementedError("populate from md when wired")


def _owner_ref(md: scheduling.ModelDeploymentSpec) -> dict:
    return {
        "apiVersion": "modelplane.ai/v1alpha1",
        "kind": "ModelDeployment",
        "name": md.name,
        # uid filled in by Crossplane when materialized
    }


def _no_placement_reason(result: scheduling.MatchResult) -> str:
    if not result.trace:
        return REASON_NO_CLUSTERS
    if all(t.reason == "capacity" for t in result.trace):
        return REASON_SATURATED
    return REASON_NO_CLUSTERS


def _trace_summary(trace: list[scheduling.MatchTrace]) -> str:
    """Compact summary of why scheduling failed, for the condition message.

    The full structured trace lives on MD.status.matchTrace (a separate
    field) — this is the human-readable headline.
    """
    if not trace:
        return "no eligible InferenceClusters"
    by_reason: dict[str, int] = {}
    for t in trace:
        by_reason[t.reason] = by_reason.get(t.reason, 0) + 1
    return "; ".join(f"{r}: {n}" for r, n in by_reason.items())


# ---------------------------------------------------------------------------
# Crossplane function entrypoint
# ---------------------------------------------------------------------------


def function(req: fnv1.RunFunctionRequest) -> fnv1.RunFunctionResponse:
    rsp = response.to(req)
    Composer(req, rsp).compose()
    return rsp
