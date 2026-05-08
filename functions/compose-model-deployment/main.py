"""Crossplane composition function: ModelDeployment → ModelReplicas + ModelEndpoints.

═══════════════════════════════════════════════════════════════════════════
  THIS MODULE IS THE ORCHESTRATOR.  Crossplane glue, status / conditions,
  error handling. The actual matcher logic lives in scheduling.py;
  proto translation in adapters.py; composed-resource builders in
  emitters.py. Read order, top to bottom:

    scheduling.py    pure algorithm, no Crossplane
    adapters.py      proto ⇄ scheduling types (boundary)
    emitters.py      pure: scheduling.Placement → composed-resource dicts
    main.py          THIS FILE — orchestrates the phases below

  Sketch — adapters.* raise NotImplementedError until #64's protos exist.
═══════════════════════════════════════════════════════════════════════════

Lifecycle (one reconcile = one call):

  ┌─────────────────────────────────────────────────────────────────┐
  │ Phase 1: REQUIRE                                                │
  │   Declare extra-resources we need (clusters, classes, owned     │
  │   replicas). If not all observed → return "waiting" (no error,  │
  │   no composed resources, no Ready condition update).            │
  ├─────────────────────────────────────────────────────────────────┤
  │ Phase 2: LOAD                                                   │
  │   adapters.load_md / load_clusters / load_existing — translate  │
  │   observed structs to scheduling types. Errors surface as       │
  │   ConfigInvalid (Ready=False) — we don't proceed.               │
  ├─────────────────────────────────────────────────────────────────┤
  │ Phase 3: SCHEDULE (pure)                                        │
  │   scheduling.schedule() — Filter → Score → Bind. Always         │
  │   returns; never raises. Partial / no-match shows up in result. │
  ├─────────────────────────────────────────────────────────────────┤
  │ Phase 4: BUILD (pure)                                           │
  │   emitters.build_replica / build_endpoint per Placement.        │
  │   Pure dict builders.                                           │
  ├─────────────────────────────────────────────────────────────────┤
  │ Phase 5: EMIT                                                   │
  │   resource.update(rsp.desired.resources[...], built_dict)       │
  │   — the Crossplane SDK call to add a composed resource.         │
  ├─────────────────────────────────────────────────────────────────┤
  │ Phase 6: STATUS                                                 │
  │   Set MD.status.conditions (Scheduled / ReplicasReady) and      │
  │   matchTrace from Phase 3's result.                             │
  └─────────────────────────────────────────────────────────────────┘

State machine for MD.status.conditions[Scheduled]:

  Unknown    initial; Phase 1 still gathering required resources
  True       Phase 3 placed all spec.replicas successfully
  False      Phase 3 placed 0 (NoEligibleClusters / FleetSaturated)
             OR Phase 3 placed N < spec.replicas (PartiallyScheduled)
             OR Phase 2 raised (ConfigInvalid)

State machine for MD.status.conditions[ReplicasReady]:

  Unknown    no observed ModelReplicas yet
  True       all child MRs report Ready=True (from their own renderer)
  False      ≥1 child MR reports Ready=False (with reason from the MR's
             renderer — e.g. Pulling, LWSGangPending, EngineLoading)

Error handling:

  Required-resource missing (Phase 1) → return waiting (no errors)
  Adapter failure (Phase 2)           → ConfigInvalid + log
  Scheduler failure (Phase 3)         → can't happen — schedule() is total
  Builder / emit failure (Phase 5)    → InternalError + log
"""

# ---------------------------------------------------------------------------
# Imports
# ═══ Crossplane SDK ════════════════════════════════════════════════════════
from crossplane.function import resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

# ═══ Local pure modules ═══════════════════════════════════════════════════
from . import adapters, emitters, scheduling

# ═══ Shared lib helpers ═══════════════════════════════════════════════════
from .lib import conditions

# When #64 lands:
#   from .model.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1


# ---------------------------------------------------------------------------
# Conditions exposed on the MD XR.
# ---------------------------------------------------------------------------

CONDITION_TYPE_SCHEDULED = "Scheduled"
CONDITION_TYPE_REPLICAS_READY = "ReplicasReady"

REASON_SCHEDULED_ALL = "AllReplicasScheduled"
REASON_SCHEDULED_PARTIAL = "PartiallyScheduled"
REASON_NO_CLUSTERS = "NoEligibleClusters"
REASON_SATURATED = "FleetSaturated"
REASON_NO_REPLICAS = "ZeroReplicas"
REASON_CONFIG_INVALID = "ConfigInvalid"

REASON_REPLICAS_READY = "AllReplicasReady"
REASON_REPLICAS_PROGRESSING = "ReplicasProgressing"


def function(req: fnv1.RunFunctionRequest) -> fnv1.RunFunctionResponse:
    """Crossplane composition function entry point."""
    rsp = response.to(req)
    Composer(req, rsp).run()
    return rsp


class Composer:
    """Phase orchestrator for one reconcile.

    All phase methods return a bool: True if the phase succeeded and we
    should continue, False if we should stop (the phase has already
    written conditions / emitted warnings / required more resources).
    """

    def __init__(self, req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
        self.req = req
        self.rsp = rsp

        # Filled in by the phases below.
        self.md: scheduling.ModelDeploymentSpec | None = None
        self.md_uid: str = ""
        self.engine: dict = {}
        self.source: dict = {}
        self.clusters: list[scheduling.InferenceCluster] = []
        self.existing: list[scheduling.ExistingPlacement] = []
        self.result: scheduling.ScheduleResult | None = None

    # =======================================================================
    # ENTRY
    # =======================================================================

    def run(self) -> None:
        if not self.phase_require():
            return
        if not self.phase_load():
            return
        self.phase_schedule()
        self.phase_build_and_emit()
        self.phase_status()

    # =======================================================================
    # ═══ Phase 1: REQUIRE ═════════════════════════════════════════════════
    # Crossplane composition glue.
    # =======================================================================

    def phase_require(self) -> bool:
        """Declare extra resources we need. Returns False if we're still
        waiting on Crossplane to fetch them (next reconcile will retry)."""
        response.require_resources(
            self.rsp,
            name="clusters",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceCluster",
        )
        response.require_resources(
            self.rsp,
            name="classes",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceClass",
        )
        # Only fetch ModelReplicas owned by THIS MD — label set by emitters.
        # Read the MD name out of the observed XR before we do full adapter
        # parsing (adapter parsing is Phase 2; this is just the name).
        md_name = _peek_md_name(self.req)
        response.require_resources(
            self.rsp,
            name="existing-replicas",
            api_version="modelplane.ai/v1alpha1",
            kind="ModelReplica",
            match_labels={emitters.LABEL_DEPLOYMENT: md_name},
        )

        observed = self.req.extra_resources
        return all(k in observed for k in ("clusters", "classes", "existing-replicas"))

    # =======================================================================
    # ═══ Phase 2: LOAD ═════════════════════════════════════════════════════
    # Boundary between protos and pure types. Errors here are config-level.
    # =======================================================================

    def phase_load(self) -> bool:
        try:
            self.md = adapters.load_md(self.req)
            self.engine = _peek_engine(self.req)
            self.source = _peek_source(self.req)
            self.md_uid = _peek_md_uid(self.req)
            self.clusters = adapters.load_clusters(self.req)
            self.existing = adapters.load_existing(self.req)
        except (ValueError, KeyError, NotImplementedError) as e:
            # NotImplementedError: sketch-only — the function is not wired
            # yet. Surface as ConfigInvalid for now; once #64 lands and
            # adapters become real this branch is just (ValueError, KeyError).
            conditions.set_condition(
                self.rsp,
                type=CONDITION_TYPE_SCHEDULED,
                status="False",
                reason=REASON_CONFIG_INVALID,
                message=str(e),
            )
            return False
        return True

    # =======================================================================
    # ═══ Phase 3: SCHEDULE ════════════════════════════════════════════════
    # Pure call into scheduling.schedule() — Filter → Score → Bind. Total
    # function; cannot fail. Partial scheduling surfaces as fewer placements
    # in the result than spec.replicas.
    # =======================================================================

    def phase_schedule(self) -> None:
        assert self.md is not None
        self.result = scheduling.schedule(self.md, self.clusters, self.existing)

    # =======================================================================
    # ═══ Phase 4+5: BUILD + EMIT ═══════════════════════════════════════════
    # emitters.* are pure; resource.update is the Crossplane SDK call.
    # =======================================================================

    def phase_build_and_emit(self) -> None:
        assert self.md is not None and self.result is not None
        for placement in self.result.placements:
            mr_dict = emitters.build_replica(
                self.md, placement, self.md_uid, self.engine, self.source
            )
            ep_dict = emitters.build_endpoint(self.md, placement, self.md_uid)
            self._emit(f"replica-{placement.replica_index}", mr_dict)
            self._emit(f"endpoint-{placement.replica_index}", ep_dict)

    def _emit(self, key: str, obj: dict) -> None:
        """Crossplane SDK call to register a composed resource."""
        resource.update(self.rsp.desired.resources[key], obj)

    # =======================================================================
    # ═══ Phase 6: STATUS ═══════════════════════════════════════════════════
    # Translate matcher result → MD.status.conditions + matchTrace.
    # =======================================================================

    def phase_status(self) -> None:
        assert self.md is not None and self.result is not None

        # Condition: Scheduled
        if self.md.replicas == 0:
            self._set(CONDITION_TYPE_SCHEDULED, "True", REASON_NO_REPLICAS,
                      "spec.replicas is 0; nothing to schedule")
        elif not self.result.placements:
            self._set(CONDITION_TYPE_SCHEDULED, "False",
                      _no_placement_reason(self.result),
                      _trace_summary(self.result.trace))
        elif len(self.result.placements) < self.md.replicas:
            self._set(
                CONDITION_TYPE_SCHEDULED,
                "False",
                REASON_SCHEDULED_PARTIAL,
                f"{len(self.result.placements)}/{self.md.replicas} replicas scheduled. "
                + _trace_summary(self.result.trace),
            )
        else:
            self._set(
                CONDITION_TYPE_SCHEDULED,
                "True",
                REASON_SCHEDULED_ALL,
                f"{self.md.replicas} replicas scheduled across the fleet",
            )

        # Condition: ReplicasReady — derived from observed MR.status conditions.
        # The replicas we composed in Phase 5 might not exist yet on this
        # reconcile; on subsequent reconciles their status flows back.
        # Sketched here for clarity; real impl walks observed.resources.
        # _set(CONDITION_TYPE_REPLICAS_READY, ...)

    def _set(self, type_: str, status: str, reason: str, message: str = "") -> None:
        conditions.set_condition(
            self.rsp, type=type_, status=status, reason=reason, message=message
        )


# ---------------------------------------------------------------------------
# Helpers — peek at XR struct fields without going through the full adapter.
# Phase 1 needs the MD name *before* phase 2 has run.
# ---------------------------------------------------------------------------


def _peek_md_name(req: fnv1.RunFunctionRequest) -> str:
    """Read the MD's metadata.name out of the observed composite struct."""
    return resource.struct_to_dict(req.observed.composite.resource).get(
        "metadata", {}
    ).get("name", "")


def _peek_md_uid(req: fnv1.RunFunctionRequest) -> str:
    return resource.struct_to_dict(req.observed.composite.resource).get(
        "metadata", {}
    ).get("uid", "")


def _peek_engine(req: fnv1.RunFunctionRequest) -> dict:
    return (
        resource.struct_to_dict(req.observed.composite.resource)
        .get("spec", {})
        .get("engine", {})
    )


def _peek_source(req: fnv1.RunFunctionRequest) -> dict:
    """Source spec dict — pass-through into ModelReplica.spec.source."""
    s = resource.struct_to_dict(req.observed.composite.resource).get("spec", {})
    out = {"type": s.get("source")}
    for k in ("huggingFace", "s3", "gcs", "pvc"):
        if k in s:
            out[k] = s[k]
    return out


# ---------------------------------------------------------------------------
# Status reasons / message helpers.
# ---------------------------------------------------------------------------


def _no_placement_reason(result: scheduling.ScheduleResult) -> str:
    if not result.trace:
        return REASON_NO_CLUSTERS
    if all(t.reason == "capacity" for t in result.trace):
        return REASON_SATURATED
    return REASON_NO_CLUSTERS


def _trace_summary(trace: list[scheduling.MatchTrace]) -> str:
    """Compact summary of why scheduling failed, for the condition message.

    Full structured trace lands on MD.status.matchTrace — see
    emitters.build_match_trace.
    """
    if not trace:
        return "no eligible InferenceClusters"
    by_reason: dict[str, int] = {}
    for t in trace:
        by_reason[t.reason] = by_reason.get(t.reason, 0) + 1
    return "; ".join(f"{r}: {n}" for r, n in by_reason.items())
