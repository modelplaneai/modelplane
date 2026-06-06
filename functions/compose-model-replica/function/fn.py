"""Deploy a model on a single InferenceCluster.

This function reads the referenced InferenceCluster via required
resources, then dispatches to the backend that matches the replica's
topology to compose the cluster-level serving resources.

GPU count comes from spec.workers.topology directly:
- tensor:   GPUs per node.
- pipeline: nodes per worker (default 1). Values > 1 select a
            multi-node backend (llm-d / LeaderWorkerSet).

The worker template is a curated subset of PodTemplateSpec. The
container named "engine" is the inference engine; its image and args
are passed through to the composed workload.
"""

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

from function.backends import base, dynamo, llmd, native

# Condition types and reasons for the ModelReplica XR.
CONDITION_TYPE_MODEL_ACCEPTED = "ModelAccepted"
CONDITION_TYPE_MODEL_READY = "ModelReady"

CONDITION_REASON_WAITING_FOR_CLUSTER = "WaitingForCluster"
CONDITION_REASON_WAITING_FOR_MODEL = "WaitingForModel"
CONDITION_REASON_DEPLOYING = "Deploying"
CONDITION_REASON_ACCEPTED = "Accepted"
CONDITION_REASON_SERVING = "Serving"
CONDITION_REASON_MODEL_STARTING = "ModelStarting"

# Composed resource key for the primary model serving resource. The
# native and llm-d backends both emit this key, and derive_conditions
# reads it to track acceptance/readiness.
MODEL_RESOURCE_KEY = "model-serving"

# Backend registry: topology selects which one composes the workload.
_BACKENDS: dict[str, type[base.Backend]] = {
    base.NATIVE: native.NativeBackend,
    base.LLMD: llmd.LLMDBackend,
    base.DYNAMO: dynamo.DynamoBackend,
}


def _inference_cluster(
    ic: icv1alpha1.InferenceCluster,
) -> icv1alpha1.InferenceCluster:
    """Return a copy with status fields defaulted."""
    ic = ic.model_copy(deep=True)
    ic.status = ic.status or icv1alpha1.Status()
    ic.status.providerConfigRef = ic.status.providerConfigRef or icv1alpha1.ProviderConfigRef()
    ic.status.gateway = ic.status.gateway or icv1alpha1.Gateway()
    ic.status.capacity = ic.status.capacity or icv1alpha1.CapacityModel()
    ic.status.capacity.gpuPools = ic.status.capacity.gpuPools or []
    return ic


class FunctionRunner(grpcv1.FunctionRunnerService):
    """A FunctionRunner handles gRPC RunFunctionRequests."""

    def __init__(self):
        """Create a new FunctionRunner."""
        self.log = logging.get_logger()

    async def RunFunction(self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext) -> fnv1.RunFunctionResponse:
        """Run the function."""
        log = self.log.bind(tag=req.meta.tag)
        log.info("Running function")

        rsp = response.to(req)
        c = Composer(req, rsp)
        c.compose()
        return rsp


class Composer:
    def __init__(self, req, rsp):
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.ModelReplica(**resource.struct_to_dict(req.observed.composite.resource))
        self.ic = None
        self.engine = None  # Cached engine container; set in compose_model_serving.

    def compose(self):
        if not self.resolve_inputs():
            return
        self.compose_model_serving()
        self.derive_conditions()

    def resolve_inputs(self):
        """Declare and fetch the referenced InferenceCluster."""
        response.require_resources(
            self.rsp,
            name="cluster",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceCluster",
            match_name=self.xr.spec.clusterName,
        )

        ic_dict = request.get_required_resource(self.req, "cluster")
        if ic_dict is None:
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_MODEL_ACCEPTED, status="False", reason=CONDITION_REASON_WAITING_FOR_CLUSTER
                ),
            )
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_MODEL_READY, status="False", reason=CONDITION_REASON_WAITING_FOR_MODEL
                ),
            )
            response.normal(self.rsp, "Waiting for cluster to be resolved")
            return False

        self.ic = _inference_cluster(icv1alpha1.InferenceCluster.model_validate(ic_dict))

        if not self.ic.status.providerConfigRef.name:
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_MODEL_ACCEPTED, status="False", reason=CONDITION_REASON_WAITING_FOR_CLUSTER
                ),
            )
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_MODEL_READY, status="False", reason=CONDITION_REASON_WAITING_FOR_MODEL
                ),
            )
            response.normal(self.rsp, "Waiting for cluster providerConfigRef")
            return False

        return True

    def compose_model_serving(self):
        """Dispatch to the backend that matches the replica's topology."""
        self.engine = base.engine_container(self.xr)
        backend = _BACKENDS[base.select_backend(self.xr)]()
        for key, composed in backend.build(self.xr, self.ic).items():
            resource.update(self.rsp.desired.resources[key], composed)

    def derive_conditions(self):
        """Derive ModelAccepted and ModelReady conditions."""

        # First-time transition: emit a normal event the first reconcile.
        if MODEL_RESOURCE_KEY not in self.req.observed.resources:
            image = self.engine.image
            response.normal(
                self.rsp,
                f"Composing {image} on {self.xr.spec.clusterName}",
            )

        # Check if the remote resource was created by reading the Object's
        # atProvider.manifest. provider-kubernetes populates this field after
        # successfully observing the remote resource at least once.
        serving_accepted = False
        serving_observed = self.req.observed.resources.get(MODEL_RESOURCE_KEY)
        if serving_observed:
            obj = k8sobjv1alpha1.Object.model_validate(resource.struct_to_dict(serving_observed.resource))
            serving_accepted = bool(obj.status and obj.status.atProvider and obj.status.atProvider.manifest)

        serving_ready = (
            resource.get_condition(self.req.observed.resources.get(MODEL_RESOURCE_KEY), "Ready").status == "True"
        )

        # ModelAccepted: the remote resource was created on the cluster.
        accepted_reason = CONDITION_REASON_ACCEPTED if serving_accepted else CONDITION_REASON_DEPLOYING
        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_MODEL_ACCEPTED,
                status="True" if serving_accepted else "False",
                reason=accepted_reason,
            ),
        )

        # ModelReady: the model is actually serving traffic.
        if serving_ready:
            ready_reason = CONDITION_REASON_SERVING
        elif serving_accepted:
            ready_reason = CONDITION_REASON_MODEL_STARTING
        else:
            ready_reason = CONDITION_REASON_WAITING_FOR_MODEL
        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_MODEL_READY, status="True" if serving_ready else "False", reason=ready_reason
            ),
        )

        # Per-resource readiness. Only the workload (model-serving) gates XR
        # readiness; the Service/HTTPRoute (and llm-d's pool/EPP) Objects derive
        # their own readiness via provider-kubernetes DeriveFromObject.
        if MODEL_RESOURCE_KEY in self.rsp.desired.resources and serving_ready:
            self.rsp.desired.resources[MODEL_RESOURCE_KEY].ready = fnv1.READY_TRUE
