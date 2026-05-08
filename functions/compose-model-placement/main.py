"""Render a ModelReplica into a KServe LLMInferenceService on its target cluster.

This is the *renderer* half of the federation control plane (see also
`compose-model-deployment` for the composer half). It runs once per
ModelReplica reconcile and:

  1. Reads the ModelReplica — placement decision (cluster, pool[s]) +
     resolved decode / prefill role specs + engine + source.
  2. Reads the matched InferenceCluster + InferenceClass(es) — needed to
     build the DRA ResourceClaim from the class's typed capabilities.
  3. Composes a KServe LLMInferenceService (LLM-IS) on the target cluster
     via Crossplane's remote-cluster Object provider, plus a DRA
     ResourceClaim per role for device binding, plus any per-scheduler
     companion objects (PodGroup for KAI; nothing extra for Kueue — its
     webhook handles Workload creation).
  4. Reports observed status from the LLM-IS back to the ModelReplica
     (Ready / Pulling / LWSGangPending / EngineLoading conditions).

Sketch — assumes Nic's #64 protos. The renderer doesn't run the matcher
or look at other ModelReplicas; it's pure over (ModelReplica, IC, Class).
That's the whole point of the IR: the renderer is replaceable per backend
without re-implementing federation.

NOTE on the directory name: this function is named `compose-model-placement`
historically. With Nic's API rename (#64), the parent XR is `ModelReplica`.
The directory rename is a follow-up — Crossplane function package ids are
referenced from Compositions and aren't a simple `git mv`. The code in
this file targets the new ModelReplica shape.

Dependencies (what this function reads):

  Required resources (Crossplane "extra resources"):
    name=cluster   InferenceCluster matching MR.spec.target.cluster
    name=classes   InferenceClass × {decodePool's class, prefillPool's class}

  Composed (this function writes):
    KServe LLMInferenceService (on the remote target cluster)
    DRA ResourceClaim × roles (on the remote target cluster)

Use cases (each example exercises a path here):

  examples/workloads/gpt-oss-20b.yaml        Tensor → 1 pod, no LWS
  examples/workloads/kimi-k2.yaml            TensorPipeline → LWS gang
  examples/workloads/qwen3-coder.yaml        TensorPipeline + FP8 engine args
  examples/workloads/acme-vllm-fork.yaml     Engine fork — pass-through args
  Disaggregated LLM-IS (decode + prefill)    lands with #64

Why this seam matters for BYO-* (see ../../design/proposed-modelplane-api/design.md):

  - BYO KServe version v0.16 / v0.17 / v0.18 → swap the renderer in this
    file, MR shape unchanged, matcher unchanged, MD unchanged.
  - BYO Dynamo / raw-vllm → different file, same MR contract.
  - BYO scheduler (KAI vs Kueue) → scheduler.wrap() dispatches per
    IC.spec.scheduler.type. KAI: schedulerName + PodGroup. Kueue: queue
    label + suspend gate. MR unchanged. Matcher unchanged.
"""

# ---------------------------------------------------------------------------
# Imports — sketch. See compose-model-deployment/main.py for the same note.
# ---------------------------------------------------------------------------

from crossplane.function import request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from . import scheduler
from .lib import conditions, defaults, metadata, naming
from .lib import resource as libresource

# When #64 lands these become real:
#   from .model.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1
#   from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
#   from .model.ai.modelplane.inferenceclass import v1alpha1 as iclassv1alpha1
#   from .model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobj


# ---------------------------------------------------------------------------
# Conditions exposed on the ModelReplica XR.
# ---------------------------------------------------------------------------

CONDITION_TYPE_RENDERED = "Rendered"
CONDITION_TYPE_READY = "Ready"

# Granular cold-start states surfaced from the LLM-IS:
CONDITION_TYPE_PULLING = "Pulling"
CONDITION_TYPE_LWS_GANG_PENDING = "LWSGangPending"
CONDITION_TYPE_ENGINE_LOADING = "EngineLoading"

REASON_RENDERED = "RenderedToCluster"
REASON_RENDER_FAILED = "RenderFailed"
REASON_LLMIS_READY = "LLMInferenceServiceReady"
REASON_LLMIS_PROGRESSING = "LLMInferenceServiceProgressing"


# Composed resource keys — stable names on the response so subsequent
# reconciles update rather than recreate.
LLMIS_KEY = "llm-is"
RESOURCE_CLAIM_DECODE_KEY = "rc-decode"
RESOURCE_CLAIM_PREFILL_KEY = "rc-prefill"


class Renderer:
    """ModelReplica → KServe LLMInferenceService + DRA ResourceClaims."""

    def __init__(self, req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
        self.req = req
        self.rsp = rsp
        self.mr = _load_mr(req)

        # Matched substrate — populated by resolve_inputs().
        self.cluster = None  # InferenceCluster
        self.classes: dict[str, "InferenceClass"] = {}  # pool name → class

    def render(self) -> None:
        if not self.resolve_inputs():
            return
        self.compose_llmis()
        self.compose_resource_claims()
        self.observe_status()

    # -----------------------------------------------------------------------
    # 1. Required resources
    # -----------------------------------------------------------------------

    def resolve_inputs(self) -> bool:
        """We only need the IC we landed on + the class(es) referenced by
        our decode / prefill pools. Crossplane re-runs until both are
        observed."""
        target = self.mr.target
        response.require_resources(
            self.rsp,
            name="cluster",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceCluster",
            match_name=target.cluster,
        )
        # Class names come from the IC; we discover them after the cluster
        # is observed. Two-phase: cluster first, then classes.
        observed = self.req.extra_resources
        if "cluster" not in observed:
            return False
        self.cluster = _resolve_cluster(observed["cluster"])

        class_names = {self.cluster.pool_class(target.decode_pool)}
        if target.prefill_pool:
            class_names.add(self.cluster.pool_class(target.prefill_pool))

        for cn in class_names:
            response.require_resources(
                self.rsp,
                name=f"class-{cn}",
                api_version="modelplane.ai/v1alpha1",
                kind="InferenceClass",
                match_name=cn,
            )

        if not all(f"class-{cn}" in observed for cn in class_names):
            return False

        for cn in class_names:
            self.classes[cn] = _resolve_class(observed[f"class-{cn}"])

        return True

    # -----------------------------------------------------------------------
    # 2. KServe LLMInferenceService
    # -----------------------------------------------------------------------

    def compose_llmis(self) -> None:
        """Render the LLM-IS for the target KServe version, wrapped per the
        cluster's in-cluster scheduler (KAI / Kueue / none).

        Single KServe render path here; switch on cluster.backend.version
        once we ship per-version adapters (v0.16 / v0.17 / v0.18 differ on
        the worker pod spec — `size`/`template` wrapper vs flat `containers`,
        args vs command, storage migration).

        Scheduler integration is a separate dispatch (scheduler.wrap) that
        post-processes the LLM-IS spec and emits any extra objects the
        scheduler needs (PodGroup for KAI, none for Kueue — Kueue's webhook
        creates Workload from the queue label).
        """
        base_spec = self._llmis_spec()

        # Stage-2 wrap: mutate the LLM-IS spec + emit extra objects per
        # scheduler. See scheduler.py for the per-scheduler logic.
        wrapped = scheduler.wrap(
            self.cluster.scheduler_type,
            base_spec,
            mr_name=self.mr.parent_name,
            namespace=self.mr.parent_namespace,
            replica_index=self.mr.replica_index,
        )

        # The LLM-IS itself.
        libresource.add_remote_object(
            self.rsp,
            name=LLMIS_KEY,
            target_cluster_secret=self.cluster.kubeconfig_secret_ref,
            api_version="serving.kserve.io/v1alpha1",
            kind="LLMInferenceService",
            metadata={
                "name": naming.llmis_name(self.mr.parent_name, self.mr.replica_index),
                "namespace": self.mr.parent_namespace,
                "labels": {metadata.LABEL_KEY_REPLICA: str(self.mr.replica_index)},
            },
            spec=wrapped.llmis_spec,
        )

        # Scheduler-side companion objects (PodGroup for KAI, etc.). Lands
        # on the same target cluster via the same kubeconfig.
        for i, obj in enumerate(wrapped.extra_objects):
            libresource.add_remote_object(
                self.rsp,
                name=f"sched-{i}",
                target_cluster_secret=self.cluster.kubeconfig_secret_ref,
                api_version=obj["apiVersion"],
                kind=obj["kind"],
                metadata=obj["metadata"],
                spec=obj["spec"],
            )

    def _llmis_spec(self) -> dict:
        """Build the KServe LLM-IS spec from the MR's resolved roles.

        Topology mapping:

          Tensor          → workerSpec sized to gpus_per_node, 1 pod
          TensorPipeline  → LWS group: pipeline pods × tensor GPUs each
          DataExpert      → DP+EP across nodes; LWS group sized accordingly

        Disaggregation: decode is the top-level workerSpec; prefill is
        rendered as `spec.prefill` with its own workerSpec. KV transfer
        config is plumbed through engine.args (Nic's design — opaque
        pass-through).
        """
        decode = self.mr.decode  # always present
        spec = {
            "model": {
                "name": f"{self.mr.parent_namespace}/{self.mr.parent_name}",
                "source": _source(self.mr.source),
            },
            "replicas": 1,  # one LLM-IS per ModelReplica — sticky 1
            "engine": _engine_block(self.mr.engine, decode),
            "workerSpec": _worker_spec(
                decode,
                self.classes[self.cluster.pool_class(self.mr.target.decode_pool)],
            ),
        }
        if self.mr.prefill is not None:
            spec["prefill"] = {
                "engine": _engine_block(self.mr.engine, self.mr.prefill),
                "workerSpec": _worker_spec(
                    self.mr.prefill,
                    self.classes[self.cluster.pool_class(self.mr.target.prefill_pool)],
                ),
            }
        return spec

    # -----------------------------------------------------------------------
    # 3. DRA ResourceClaims
    # -----------------------------------------------------------------------

    def compose_resource_claims(self) -> None:
        """Emit a DRA ResourceClaim per role.

        DRA is required on every InferenceCluster (Nic's #64 design — no
        device-plugin fallback). The claim's selector is derived from the
        class's typed capabilities:

          requirements:
            - selector:
                cel: |
                  device.attributes["gpu.nvidia.com/product"].string ==
                    capabilities["gpu.product"]
                  && device.attributes["gpu.nvidia.com/memory.gib"].quantity >=
                    capabilities["gpu.vramGiB"]

        The claim count is gpus_per_node (per-pod). Multiple pods in a LWS
        gang each get their own claim. The DRA driver matches them against
        runtime ResourceSlices at admission — that's where drift / typos /
        misconfig get caught (the federation match already evaluated the
        same predicate against declared attrs at stage 1).
        """
        decode_class = self.classes[self.cluster.pool_class(self.mr.target.decode_pool)]
        libresource.add_remote_object(
            self.rsp,
            name=RESOURCE_CLAIM_DECODE_KEY,
            target_cluster_secret=self.cluster.kubeconfig_secret_ref,
            api_version="resource.k8s.io/v1beta1",
            kind="ResourceClaim",
            metadata={
                "name": naming.claim_name(
                    self.mr.parent_name, self.mr.replica_index, "decode"
                ),
                "namespace": self.mr.parent_namespace,
            },
            spec=_resource_claim_spec(self.mr.decode, decode_class),
        )
        if self.mr.prefill is not None:
            prefill_class = self.classes[
                self.cluster.pool_class(self.mr.target.prefill_pool)
            ]
            libresource.add_remote_object(
                self.rsp,
                name=RESOURCE_CLAIM_PREFILL_KEY,
                target_cluster_secret=self.cluster.kubeconfig_secret_ref,
                api_version="resource.k8s.io/v1beta1",
                kind="ResourceClaim",
                metadata={
                    "name": naming.claim_name(
                        self.mr.parent_name, self.mr.replica_index, "prefill"
                    ),
                    "namespace": self.mr.parent_namespace,
                },
                spec=_resource_claim_spec(self.mr.prefill, prefill_class),
            )

    # -----------------------------------------------------------------------
    # 4. Observe status from the in-cluster LLM-IS
    # -----------------------------------------------------------------------

    def observe_status(self) -> None:
        """Translate the LLM-IS's observed status into MR conditions.

        Granular cold-start signals are the UX investment: the MR surfaces
        which stage of cold-start it's in so users see "pulling 70GB image"
        vs "weights still loading" vs "gang scheduling stuck".
        """
        observed = self.req.observed.resources.get(LLMIS_KEY)
        if observed is None:
            conditions.set_condition(
                self.rsp,
                type=CONDITION_TYPE_RENDERED,
                status="True",
                reason=REASON_RENDERED,
            )
            return

        # Real impl reads observed.resource (Struct) and lifts conditions.
        # Sketch: just signal "ready when the LLM-IS is ready".
        if conditions.has_condition(self.req, LLMIS_KEY, "Ready"):
            conditions.set_condition(
                self.rsp,
                type=CONDITION_TYPE_READY,
                status="True",
                reason=REASON_LLMIS_READY,
            )
            return

        # When not Ready, lift the most informative cold-start condition.
        for stage_cond in (
            CONDITION_TYPE_PULLING,
            CONDITION_TYPE_LWS_GANG_PENDING,
            CONDITION_TYPE_ENGINE_LOADING,
        ):
            if conditions.has_condition(self.req, LLMIS_KEY, stage_cond):
                conditions.set_condition(
                    self.rsp,
                    type=CONDITION_TYPE_READY,
                    status="False",
                    reason=stage_cond,
                )
                return
        conditions.set_condition(
            self.rsp,
            type=CONDITION_TYPE_READY,
            status="False",
            reason=REASON_LLMIS_PROGRESSING,
        )


# ---------------------------------------------------------------------------
# Render helpers — pure functions over MR + class.
# These are the per-KServe-version dispatch points.
# ---------------------------------------------------------------------------


def _worker_spec(role, cls) -> dict:
    """Build the LLM-IS workerSpec for a role.

    Targets KServe v0.18 schema today (flat `containers`, no size/template
    wrapper). v0.17 / v0.16 dispatch is a follow-up — add a switch on
    cluster.backend.version when the per-version adapters land.
    """
    nodes_per_inst = role["topology"]["pipeline"] or 1
    gpus_per_node = role["gpusPerNode"]
    return {
        "replicas": role["instances"],
        # LWS group size: pipeline depth (>1 for multi-node).
        "leaderWorkerSet": {"size": nodes_per_inst} if nodes_per_inst > 1 else None,
        "containers": [
            {
                "name": "engine",
                "image": role.get("image"),
                "command": role.get("command", []),
                "args": role.get("args", []),
                "resources": {
                    "claims": [{"name": "gpus"}],  # bound to a ResourceClaim
                    "limits": {"nvidia.com/gpu": gpus_per_node},
                },
            }
        ],
    }


def _engine_block(engine, role) -> dict:
    """Pass-through engine config for one role.

    Nic's API: engine.{name, image, args}. No structured `quantization` /
    `speculation` / `optimizations` — engine args is the opaque seam.
    """
    return {
        "name": engine.get("name"),
        "image": engine.get("image"),
        "args": engine.get("args", []),
    }


def _resource_claim_spec(role, cls) -> dict:
    """Derive a DRA ResourceClaim from the class's typed capabilities.

    Builds a CEL expression over device.attributes that mirrors the
    capability constraints. The DRA driver evaluates this against runtime
    ResourceSlices at pod admission.
    """
    return {
        "devices": {
            "requests": [
                {
                    "name": "gpus",
                    "deviceClassName": _device_class_for(cls),
                    "selectors": [
                        {"cel": _cel_from_capabilities(cls.capabilities)},
                    ],
                    "count": role["gpusPerNode"],
                }
            ],
        }
    }


def _cel_from_capabilities(capabilities: dict) -> str:
    """Map declared capabilities → DRA selector CEL.

    Real impl walks the capability map and emits the equivalent
    device.attributes predicates. Sketch shows the shape — production
    would template per well-known key (gpu.product, gpu.vramGiB,
    gpu.features, ...).
    """
    raise NotImplementedError("template per well-known capability key when wired")


def _device_class_for(cls) -> str:
    """Pick the DRA DeviceClass name from the InferenceClass's vendor."""
    vendor = cls.capabilities.get("gpu.vendor", "nvidia")
    return {"nvidia": "gpu.nvidia.com", "amd": "gpu.amd.com"}.get(vendor, "generic-gpu")


def _source(source) -> dict:
    return source  # MR carries the resolved source dict already


# ---------------------------------------------------------------------------
# Adapters between Crossplane structs and renderer types.
# ---------------------------------------------------------------------------


def _load_mr(req):
    raise NotImplementedError("wire to mrv1alpha1.ModelReplica when #64 lands")


def _resolve_cluster(observed):
    raise NotImplementedError("walk extra resources when #64 lands")


def _resolve_class(observed):
    raise NotImplementedError("walk extra resources when #64 lands")


# ---------------------------------------------------------------------------
# Crossplane function entrypoint
# ---------------------------------------------------------------------------


def function(req: fnv1.RunFunctionRequest) -> fnv1.RunFunctionResponse:
    rsp = response.to(req)
    Renderer(req, rsp).render()
    return rsp
