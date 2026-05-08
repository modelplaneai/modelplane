"""Crossplane composition function: ModelReplica → KServe LLM-IS + DRA + scheduler.

═══════════════════════════════════════════════════════════════════════════
  THIS MODULE IS THE ORCHESTRATOR.  Crossplane glue, status / conditions,
  error handling. Pure logic lives elsewhere:

    rendering.py    pure: build LLM-IS / ResourceClaim dicts from MR + class
    scheduler.py    pure: per-scheduler wrap (KAI / Kueue / none)
    adapters.py     proto ⇄ rendering types (boundary)
    main.py         THIS FILE — orchestrates the phases below

  Sketch — adapters.* raise NotImplementedError until #64's protos exist.
═══════════════════════════════════════════════════════════════════════════

Lifecycle (one reconcile = one call):

  ┌─────────────────────────────────────────────────────────────────┐
  │ Phase 1: REQUIRE (cluster)                                      │
  │   require IC matching MR.spec.target.cluster. If not observed   │
  │   yet → return waiting.                                         │
  ├─────────────────────────────────────────────────────────────────┤
  │ Phase 2: REQUIRE (classes)                                      │
  │   IC tells us which classes our pools reference. require those, │
  │   wait for them to be observed.                                 │
  ├─────────────────────────────────────────────────────────────────┤
  │ Phase 3: LOAD                                                   │
  │   adapters.load_mr / load_cluster / load_classes — translate to │
  │   pure types. Errors → ConfigInvalid (Ready=False, no emission).│
  ├─────────────────────────────────────────────────────────────────┤
  │ Phase 4: RENDER (pure)                                          │
  │   rendering.build_llmis_spec → base LLM-IS dict.                │
  │   rendering.build_resource_claim_spec × roles → DRA claim dicts.│
  ├─────────────────────────────────────────────────────────────────┤
  │ Phase 5: WRAP (pure)                                            │
  │   scheduler.wrap(IC.scheduler_type, ...) → mutated LLM-IS spec  │
  │   + extra companion objects (PodGroup for KAI, none for Kueue). │
  ├─────────────────────────────────────────────────────────────────┤
  │ Phase 6: EMIT                                                   │
  │   resource.update for the LLM-IS, DRA claims, scheduler         │
  │   companion objects. All wrapped as Crossplane k8s.Object       │
  │   resources targeting the cluster's kubeconfig provider.        │
  ├─────────────────────────────────────────────────────────────────┤
  │ Phase 7: STATUS                                                 │
  │   Lift observed LLM-IS conditions into MR conditions:           │
  │   Ready / Pulling / LWSGangPending / EngineLoading.             │
  └─────────────────────────────────────────────────────────────────┘

State machine for MR.status.conditions[Ready]:

  Unknown              Phase 1/2 still gathering required resources
  False ConfigInvalid  Phase 3 raised
  False Pulling        observed LLM-IS reports Pulling
  False LWSGangPending observed LLM-IS reports LWS gang admission pending
  False EngineLoading  observed LLM-IS reports engine loading weights
  False Progressing    none of the above; LLM-IS exists but not Ready
  True                 LLM-IS Ready=True

Error handling:

  Required-resource missing (Phase 1/2) → return waiting (no errors)
  Adapter failure (Phase 3)            → ConfigInvalid + log
  Render failure (Phase 4)             → InternalError + log
  Wrap failure (Phase 5)               → InternalError + log (defensive)
  Emit failure (Phase 6)               → InternalError + log
"""

# ═══ Crossplane SDK ════════════════════════════════════════════════════════
from crossplane.function import resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

# ═══ Local pure modules ═══════════════════════════════════════════════════
from . import adapters, rendering, scheduler

# ═══ Shared lib helpers ═══════════════════════════════════════════════════
from .lib import conditions

# When #64 lands:
#   from .model.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1


# ---------------------------------------------------------------------------
# Conditions exposed on the MR XR.
# ---------------------------------------------------------------------------

CONDITION_TYPE_RENDERED = "Rendered"
CONDITION_TYPE_READY = "Ready"

REASON_RENDERED = "RenderedToCluster"
REASON_LLMIS_READY = "LLMInferenceServiceReady"
REASON_PROGRESSING = "Progressing"
REASON_CONFIG_INVALID = "ConfigInvalid"
REASON_INTERNAL_ERROR = "InternalError"

# Cold-start reasons lifted from the LLM-IS:
LLMIS_COND_PULLING = "Pulling"
LLMIS_COND_LWS_GANG_PENDING = "LWSGangPending"
LLMIS_COND_ENGINE_LOADING = "EngineLoading"

# Composed resource keys — stable so subsequent reconciles update.
LLMIS_KEY = "llm-is"
RESOURCE_CLAIM_DECODE_KEY = "rc-decode"
RESOURCE_CLAIM_PREFILL_KEY = "rc-prefill"


def function(req: fnv1.RunFunctionRequest) -> fnv1.RunFunctionResponse:
    """Crossplane composition function entry point."""
    rsp = response.to(req)
    Renderer(req, rsp).run()
    return rsp


class Renderer:
    """Phase orchestrator for one MR reconcile."""

    def __init__(self, req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
        self.req = req
        self.rsp = rsp

        # Filled in across phases.
        self.mr: rendering.ModelReplicaView | None = None
        self.cluster: rendering.ClusterView | None = None
        self.classes: dict[str, rendering.ClassView] = {}
        self.llmis_spec: dict = {}
        self.wrapped: scheduler.WrappedRender | None = None

    # =======================================================================
    # ENTRY
    # =======================================================================

    def run(self) -> None:
        if not self.phase_require_cluster():
            return
        if not self.phase_require_classes():
            return
        if not self.phase_load():
            return
        if not self.phase_render():
            return
        if not self.phase_wrap():
            return
        self.phase_emit()
        self.phase_status()

    # =======================================================================
    # ═══ Phase 1: REQUIRE cluster ═════════════════════════════════════════
    # =======================================================================

    def phase_require_cluster(self) -> bool:
        target_cluster = _peek_target_cluster(self.req)
        if not target_cluster:
            # MR has no target yet — composer hasn't run. Nothing to do.
            return False
        response.require_resources(
            self.rsp,
            name="cluster",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceCluster",
            match_name=target_cluster,
        )
        return "cluster" in self.req.extra_resources

    # =======================================================================
    # ═══ Phase 2: REQUIRE classes ═════════════════════════════════════════
    # IC tells us which classes are referenced by the pools we landed on.
    # =======================================================================

    def phase_require_classes(self) -> bool:
        # Resolve cluster first so we know which class names to require.
        try:
            self.cluster = adapters.load_cluster(self.req)
        except (ValueError, KeyError, NotImplementedError) as e:
            self._fail(REASON_CONFIG_INVALID, str(e))
            return False

        decode_pool = _peek_target_decode_pool(self.req)
        prefill_pool = _peek_target_prefill_pool(self.req)
        class_names = {self.cluster.pool_to_class[decode_pool]}
        if prefill_pool:
            class_names.add(self.cluster.pool_to_class[prefill_pool])

        for cn in class_names:
            response.require_resources(
                self.rsp,
                name=f"class-{cn}",
                api_version="modelplane.ai/v1alpha1",
                kind="InferenceClass",
                match_name=cn,
            )
        return all(f"class-{cn}" in self.req.extra_resources for cn in class_names)

    # =======================================================================
    # ═══ Phase 3: LOAD ════════════════════════════════════════════════════
    # =======================================================================

    def phase_load(self) -> bool:
        try:
            self.mr = adapters.load_mr(self.req)
            class_names = list(set(self.cluster.pool_to_class.values())) if self.cluster else []
            self.classes = adapters.load_classes(self.req, class_names)
        except (ValueError, KeyError, NotImplementedError) as e:
            self._fail(REASON_CONFIG_INVALID, str(e))
            return False
        return True

    # =======================================================================
    # ═══ Phase 4: RENDER (pure) ═══════════════════════════════════════════
    # =======================================================================

    def phase_render(self) -> bool:
        assert self.mr is not None
        try:
            self.llmis_spec = rendering.build_llmis_spec(self.mr, self.classes)
        except Exception as e:  # noqa: BLE001 — defensive; render shouldn't raise
            self._fail(REASON_INTERNAL_ERROR, f"render failed: {e}")
            return False
        return True

    # =======================================================================
    # ═══ Phase 5: WRAP (pure) ═════════════════════════════════════════════
    # Per-scheduler dispatch. KAI: schedulerName + PodGroup. Kueue: queue
    # label + suspend. none: pass-through.
    # =======================================================================

    def phase_wrap(self) -> bool:
        assert self.mr is not None and self.cluster is not None
        try:
            self.wrapped = scheduler.wrap(
                self.cluster.scheduler_type,
                self.llmis_spec,
                mr_name=self.mr.parent_name,
                namespace=self.mr.parent_namespace,
                replica_index=self.mr.replica_index,
            )
        except Exception as e:  # noqa: BLE001
            self._fail(REASON_INTERNAL_ERROR, f"scheduler wrap failed: {e}")
            return False
        return True

    # =======================================================================
    # ═══ Phase 6: EMIT ════════════════════════════════════════════════════
    # Wrap each in-cluster object as a Crossplane k8s.Object so the
    # remote-cluster provider applies it via the cluster's kubeconfig.
    # =======================================================================

    def phase_emit(self) -> None:
        assert self.mr is not None and self.cluster is not None and self.wrapped is not None

        # The LLM-IS itself.
        self._emit_remote(
            LLMIS_KEY,
            api_version="serving.kserve.io/v1alpha1",
            kind="LLMInferenceService",
            metadata={
                "name": _llmis_name(self.mr.parent_name, self.mr.replica_index),
                "namespace": self.mr.parent_namespace,
            },
            spec=self.wrapped.llmis_spec,
        )

        # DRA ResourceClaims — one per role.
        decode_class = self.classes[self.cluster.pool_to_class[self.mr.target_decode_pool]]
        self._emit_remote(
            RESOURCE_CLAIM_DECODE_KEY,
            api_version="resource.k8s.io/v1beta1",
            kind="ResourceClaim",
            metadata={
                "name": _claim_name(self.mr.parent_name, self.mr.replica_index, "decode"),
                "namespace": self.mr.parent_namespace,
            },
            spec=rendering.build_resource_claim_spec(self.mr.decode, decode_class),
        )
        if self.mr.prefill is not None and self.mr.target_prefill_pool is not None:
            prefill_class = self.classes[self.cluster.pool_to_class[self.mr.target_prefill_pool]]
            self._emit_remote(
                RESOURCE_CLAIM_PREFILL_KEY,
                api_version="resource.k8s.io/v1beta1",
                kind="ResourceClaim",
                metadata={
                    "name": _claim_name(self.mr.parent_name, self.mr.replica_index, "prefill"),
                    "namespace": self.mr.parent_namespace,
                },
                spec=rendering.build_resource_claim_spec(self.mr.prefill, prefill_class),
            )

        # Scheduler companion objects (PodGroup for KAI; none for Kueue).
        for i, obj in enumerate(self.wrapped.extra_objects):
            self._emit_remote(
                f"sched-{i}",
                api_version=obj["apiVersion"],
                kind=obj["kind"],
                metadata=obj["metadata"],
                spec=obj["spec"],
            )

    def _emit_remote(
        self,
        key: str,
        api_version: str,
        kind: str,
        metadata: dict,
        spec: dict,
    ) -> None:
        """Wrap a remote-cluster object as a Crossplane k8s.Object.

        The provider-kubernetes Object resource takes a `forProvider.manifest`
        with the embedded object + a `providerConfigRef` pointing at the
        cluster's kubeconfig.
        """
        assert self.cluster is not None
        resource.update(
            self.rsp.desired.resources[key],
            {
                "apiVersion": "kubernetes.crossplane.io/v1alpha2",
                "kind": "Object",
                "metadata": {
                    "name": f"{metadata['namespace']}-{metadata['name']}-{key}",
                },
                "spec": {
                    "providerConfigRef": {"name": self.cluster.kubeconfig_secret_ref["name"]},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": api_version,
                            "kind": kind,
                            "metadata": metadata,
                            "spec": spec,
                        }
                    },
                },
            },
        )

    # =======================================================================
    # ═══ Phase 7: STATUS ══════════════════════════════════════════════════
    # Translate observed LLM-IS conditions → MR conditions.
    # =======================================================================

    def phase_status(self) -> None:
        observed = self.req.observed.resources.get(LLMIS_KEY)
        if observed is None:
            self._set(CONDITION_TYPE_RENDERED, "True", REASON_RENDERED)
            return

        if conditions.has_condition(self.req, LLMIS_KEY, "Ready"):
            self._set(CONDITION_TYPE_READY, "True", REASON_LLMIS_READY)
            return

        # Lift the most informative cold-start condition first.
        for stage in (LLMIS_COND_PULLING, LLMIS_COND_LWS_GANG_PENDING, LLMIS_COND_ENGINE_LOADING):
            if conditions.has_condition(self.req, LLMIS_KEY, stage):
                self._set(CONDITION_TYPE_READY, "False", stage)
                return

        self._set(CONDITION_TYPE_READY, "False", REASON_PROGRESSING)

    # =======================================================================
    # Helpers
    # =======================================================================

    def _set(self, type_: str, status: str, reason: str, message: str = "") -> None:
        conditions.set_condition(
            self.rsp, type=type_, status=status, reason=reason, message=message
        )

    def _fail(self, reason: str, message: str) -> None:
        conditions.set_condition(
            self.rsp, type=CONDITION_TYPE_READY, status="False", reason=reason, message=message
        )


# ---------------------------------------------------------------------------
# Peek helpers — read MR.spec.target.* before adapters.load_mr() runs.
# Phase 1 needs the cluster name; Phase 2 needs the pool names.
# ---------------------------------------------------------------------------


def _peek_target_cluster(req: fnv1.RunFunctionRequest) -> str:
    return _peek_spec(req).get("target", {}).get("cluster", "")


def _peek_target_decode_pool(req: fnv1.RunFunctionRequest) -> str:
    return _peek_spec(req).get("target", {}).get("decodePool", "")


def _peek_target_prefill_pool(req: fnv1.RunFunctionRequest) -> str | None:
    return _peek_spec(req).get("target", {}).get("prefillPool") or None


def _peek_spec(req: fnv1.RunFunctionRequest) -> dict:
    return resource.struct_to_dict(req.observed.composite.resource).get("spec", {})


# ---------------------------------------------------------------------------
# Naming.
# ---------------------------------------------------------------------------


def _llmis_name(parent_name: str, idx: int) -> str:
    return f"{parent_name}-{idx}"


def _claim_name(parent_name: str, idx: int, role: str) -> str:
    return f"{parent_name}-{idx}-{role}-claim"
