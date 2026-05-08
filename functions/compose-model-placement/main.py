"""Render a ModelReplica into a KServe LLMInferenceService on its target cluster.

Reads the ModelReplica's placement decision plus the matched InferenceCluster
+ InferenceClass(es), and composes a KServe LLMInferenceService + DRA
ResourceClaim(s) on the target cluster via the kubeconfig provider, plus any
per-scheduler companion objects (PodGroup for KAI, none for Kueue).

Pure logic in rendering.py (LLM-IS shape, CEL derivation) and scheduler.py
(per-scheduler wrap dispatch). This file is the Crossplane glue.
"""

from crossplane.function import request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from . import adapters, rendering, scheduler
from .lib import conditions, defaults, naming
from .lib import resource as libresource

# When #64 lands these become real:
#   from .model.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1
#   from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
#   from .model.ai.modelplane.inferenceclass import v1alpha1 as iclassv1alpha1


# Condition types and reasons for the ModelReplica XR.
CONDITION_TYPE_RENDERED = "Rendered"
CONDITION_TYPE_READY = "Ready"

CONDITION_REASON_RENDERED = "RenderedToCluster"
CONDITION_REASON_LLMIS_READY = "LLMInferenceServiceReady"
CONDITION_REASON_PROGRESSING = "Progressing"
CONDITION_REASON_CONFIG_INVALID = "ConfigInvalid"

# Cold-start sub-states lifted from the LLM-IS:
CONDITION_REASON_PULLING = "Pulling"
CONDITION_REASON_LWS_GANG_PENDING = "LWSGangPending"
CONDITION_REASON_ENGINE_LOADING = "EngineLoading"

# Composed resource keys — stable so subsequent reconciles update.
LLMIS_KEY = "llm-is"
RC_DECODE_KEY = "rc-decode"
RC_PREFILL_KEY = "rc-prefill"


class Renderer:
    def __init__(self, req, rsp):
        self.req = req
        self.rsp = rsp
        self.xr = adapters.load_mr(req)

        # Required resources — set by resolve_inputs.
        self.cluster: rendering.ClusterView | None = None
        self.classes: dict[str, rendering.ClassView] = {}

    def render(self):
        if not self.resolve_inputs():
            return
        self.compose_llmis()
        self.compose_resource_claims()
        self.derive_conditions()

    def resolve_inputs(self) -> bool:
        """Declare and fetch the cluster + class(es) we need. Returns False
        while waiting on Crossplane (or on a config error)."""
        if not self.xr.target_cluster:
            return False  # MR has no placement yet — composer hasn't run

        response.require_resources(
            self.rsp,
            name="cluster",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceCluster",
            match_name=self.xr.target_cluster,
        )

        cluster_dict = request.get_required_resource(self.req, "cluster")
        if cluster_dict is None:
            return False  # waiting on the cluster

        try:
            self.cluster = adapters.load_cluster(cluster_dict)
        except (ValueError, KeyError) as e:
            self._fail(CONDITION_REASON_CONFIG_INVALID, str(e))
            response.warning(self.rsp, f"InferenceCluster invalid: {e}")
            return False

        # Now request the classes our pools reference.
        class_names = self._referenced_classes()
        for cn in class_names:
            response.require_resources(
                self.rsp,
                name=f"class-{cn}",
                api_version="modelplane.ai/v1alpha1",
                kind="InferenceClass",
                match_name=cn,
            )

        class_dicts = {cn: request.get_required_resource(self.req, f"class-{cn}") for cn in class_names}
        if any(d is None for d in class_dicts.values()):
            return False  # waiting on classes

        try:
            self.classes = adapters.load_classes(class_dicts)
        except (ValueError, KeyError) as e:
            self._fail(CONDITION_REASON_CONFIG_INVALID, str(e))
            response.warning(self.rsp, f"InferenceClass invalid: {e}")
            return False

        return True

    def compose_llmis(self):
        """Compose a KServe LLMInferenceService on the target cluster, wrapped
        per the cluster's in-cluster scheduler (KAI / Kueue / none)."""
        base = rendering.build_llmis_spec(self.xr, self.classes)

        wrapped = scheduler.wrap(
            self.cluster.scheduler_type,
            base,
            mr_name=self.xr.parent_name,
            namespace=self.xr.parent_namespace,
            replica_index=self.xr.replica_index,
        )

        self._compose_remote(
            LLMIS_KEY,
            api_version="serving.kserve.io/v1alpha1",
            kind="LLMInferenceService",
            name=naming.llmis_name(self.xr.parent_name, self.xr.replica_index),
            namespace=self.xr.parent_namespace,
            spec=wrapped.llmis_spec,
        )

        # Scheduler companion objects (PodGroup for KAI; none for Kueue).
        for i, obj in enumerate(wrapped.extra_objects):
            self._compose_remote(
                f"sched-{i}",
                api_version=obj["apiVersion"],
                kind=obj["kind"],
                name=obj["metadata"]["name"],
                namespace=obj["metadata"]["namespace"],
                spec=obj["spec"],
            )

    def compose_resource_claims(self):
        """Compose a DRA ResourceClaim per role. The DRA driver matches
        these against runtime ResourceSlices at pod admission."""
        decode_class = self.classes[self.cluster.pool_to_class[self.xr.target_decode_pool]]
        self._compose_remote(
            RC_DECODE_KEY,
            api_version="resource.k8s.io/v1beta1",
            kind="ResourceClaim",
            name=naming.claim_name(self.xr.parent_name, self.xr.replica_index, "decode"),
            namespace=self.xr.parent_namespace,
            spec=rendering.build_resource_claim_spec(self.xr.decode, decode_class),
        )
        if self.xr.prefill is not None and self.xr.target_prefill_pool is not None:
            prefill_class = self.classes[self.cluster.pool_to_class[self.xr.target_prefill_pool]]
            self._compose_remote(
                RC_PREFILL_KEY,
                api_version="resource.k8s.io/v1beta1",
                kind="ResourceClaim",
                name=naming.claim_name(self.xr.parent_name, self.xr.replica_index, "prefill"),
                namespace=self.xr.parent_namespace,
                spec=rendering.build_resource_claim_spec(self.xr.prefill, prefill_class),
            )

    def derive_conditions(self):
        """Translate observed LLM-IS conditions into MR conditions. Granular
        cold-start sub-states surface which stage the replica is in."""
        if LLMIS_KEY not in self.req.observed.resources:
            conditions.set_condition(
                self.rsp, CONDITION_TYPE_RENDERED, True, CONDITION_REASON_RENDERED
            )
            return

        if conditions.has_condition(self.req, LLMIS_KEY, "Ready"):
            conditions.set_condition(
                self.rsp, CONDITION_TYPE_READY, True, CONDITION_REASON_LLMIS_READY
            )
            return

        # Lift the most informative cold-start sub-state if present.
        for stage in (CONDITION_REASON_PULLING, CONDITION_REASON_LWS_GANG_PENDING, CONDITION_REASON_ENGINE_LOADING):
            if conditions.has_condition(self.req, LLMIS_KEY, stage):
                conditions.set_condition(self.rsp, CONDITION_TYPE_READY, False, stage)
                return

        conditions.set_condition(
            self.rsp, CONDITION_TYPE_READY, False, CONDITION_REASON_PROGRESSING
        )

    # ---- helpers ----------------------------------------------------------

    def _referenced_classes(self) -> set[str]:
        """Class names referenced by the pools we landed on. Computed after
        the cluster resolves, so we know the pool→class mapping."""
        names = {self.cluster.pool_to_class[self.xr.target_decode_pool]}
        if self.xr.target_prefill_pool:
            names.add(self.cluster.pool_to_class[self.xr.target_prefill_pool])
        return names

    def _compose_remote(
        self,
        key: str,
        api_version: str,
        kind: str,
        name: str,
        namespace: str,
        spec: dict,
    ):
        """Wrap an in-cluster object as a Crossplane provider-kubernetes Object
        so it lands on the target cluster via the cluster's kubeconfig."""
        resource.update(
            self.rsp.desired.resources[key],
            {
                "apiVersion": "kubernetes.crossplane.io/v1alpha2",
                "kind": "Object",
                "metadata": {"name": f"{namespace}-{name}-{key}"},
                "spec": {
                    "providerConfigRef": {"name": self.cluster.kubeconfig_secret_ref["name"]},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": api_version,
                            "kind": kind,
                            "metadata": {"name": name, "namespace": namespace},
                            "spec": spec,
                        }
                    },
                },
            },
        )

    def _fail(self, reason: str, message: str):
        conditions.set_condition(
            self.rsp, CONDITION_TYPE_READY, False, reason, message
        )


def function(req: fnv1.RunFunctionRequest) -> fnv1.RunFunctionResponse:
    rsp = response.to(req)
    Renderer(req, rsp).render()
    return rsp


# Silence unused-import warnings for lib symbols that real code will use.
_ = (defaults, libresource)
