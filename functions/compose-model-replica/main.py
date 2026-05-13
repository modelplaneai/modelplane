"""Deploy a model on a single InferenceCluster.

This function reads the referenced InferenceCluster via required
resources, then composes a KServe LLMInferenceService on the remote
cluster and an Envoy Gateway Backend on the control plane for
ModelDeployment to route through.

GPU count comes from spec.workers.topology directly:
- Tensor:         1 pod, `tensor` GPUs.
- TensorPipeline: `pipeline` pods (1 leader + pipeline-1 workers),
                  `tensor` GPUs per pod.

Non-GPU resources (CPU, memory) come from spec.workers.resources.
"""

from crossplane.function import request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .lib import conditions, defaults, metadata, naming
from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from .model.ai.modelplane.modelreplica import v1alpha1
from .model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

# Condition types and reasons for the ModelReplica XR.
CONDITION_TYPE_MODEL_ACCEPTED = "ModelAccepted"
CONDITION_TYPE_MODEL_READY = "ModelReady"

CONDITION_REASON_WAITING_FOR_CLUSTER = "WaitingForCluster"
CONDITION_REASON_WAITING_FOR_MODEL = "WaitingForModel"
CONDITION_REASON_DEPLOYING = "Deploying"
CONDITION_REASON_ACCEPTED = "Accepted"
CONDITION_REASON_SERVING = "Serving"
CONDITION_REASON_MODEL_STARTING = "ModelStarting"

# Composed resource key for the model serving resource.
MODEL_RESOURCE_KEY = "model-serving"

# Topology strategy enum values.
STRATEGY_TENSOR = "Tensor"
STRATEGY_TENSOR_PIPELINE = "TensorPipeline"


class Composer:
    def __init__(self, req, rsp):
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.ModelReplica(**resource.struct_to_dict(req.observed.composite.resource))
        self.ic = None

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
            match_name=self.xr.spec.inferenceClusterRef.name,
        )

        ic_dict = request.get_required_resource(self.req, "cluster")
        if ic_dict is None:
            conditions.set_condition(
                self.rsp, CONDITION_TYPE_MODEL_ACCEPTED, False, CONDITION_REASON_WAITING_FOR_CLUSTER
            )
            conditions.set_condition(self.rsp, CONDITION_TYPE_MODEL_READY, False, CONDITION_REASON_WAITING_FOR_MODEL)
            response.normal(self.rsp, "Waiting for cluster to be resolved")
            return False

        self.ic = defaults.inference_cluster(icv1alpha1.InferenceCluster.model_validate(ic_dict))

        if not self.ic.status.providerConfigRef.name:
            conditions.set_condition(
                self.rsp, CONDITION_TYPE_MODEL_ACCEPTED, False, CONDITION_REASON_WAITING_FOR_CLUSTER
            )
            conditions.set_condition(self.rsp, CONDITION_TYPE_MODEL_READY, False, CONDITION_REASON_WAITING_FOR_MODEL)
            response.normal(self.rsp, "Waiting for cluster providerConfigRef")
            return False

        return True

    def compose_model_serving(self):
        """Compose the LLMInferenceService on the remote cluster."""
        topology = self.xr.spec.workers.topology
        resources = self.xr.spec.workers.resources
        engine = self.xr.spec.engine

        gpu_per_pod = int(topology.tensor)
        multi_node = topology.strategy == STRATEGY_TENSOR_PIPELINE

        # Build the container spec. Image and args come straight from
        # the engine block; env and imagePullSecrets pass through. The
        # GPU count is set via the device plugin; DRA support is a
        # future addition.
        container: dict = {
            "name": "main",
            "image": engine.image,
            "args": list(engine.args or []),
            "securityContext": {"runAsUser": 0, "runAsNonRoot": False},
            "resources": {
                "limits": {
                    "nvidia.com/gpu": str(gpu_per_pod),
                    "cpu": resources.cpu,
                    "memory": resources.memory,
                },
                "requests": {"cpu": "1", "memory": resources.memory},
            },
        }
        if engine.env:
            container["env"] = [e.model_dump(exclude_none=True) for e in engine.env]

        pod_spec: dict = {"containers": [container]}
        if engine.imagePullSecrets:
            pod_spec["imagePullSecrets"] = [s.model_dump(exclude_none=True) for s in engine.imagePullSecrets]

        llmis_spec: dict = {
            "replicas": 1,
            "template": pod_spec,
            "router": {"gateway": {}, "route": {}},
        }

        # Multi-node TensorPipeline: pipeline pods, each with `tensor` GPUs.
        # Total tensor parallelism = tensor * pipeline. Worker count is
        # pipeline - 1 (the leader is the "main" template).
        if multi_node:
            total_gpus = gpu_per_pod * int(topology.pipeline)
            llmis_spec["parallelism"] = {"tensor": total_gpus}
            llmis_spec["worker"] = {
                "size": int(topology.pipeline) - 1,
                "template": dict(pod_spec),
            }

        resource.update(
            self.rsp.desired.resources[MODEL_RESOURCE_KEY],
            k8sobjv1alpha1.Object(
                spec=k8sobjv1alpha1.Spec(
                    providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                        kind="ClusterProviderConfig",
                        name=self.ic.status.providerConfigRef.name,
                    ),
                    readiness=k8sobjv1alpha1.Readiness(
                        policy="DeriveFromObject",
                    ),
                    forProvider=k8sobjv1alpha1.ForProvider(
                        manifest={
                            "apiVersion": "serving.kserve.io/v1alpha1",
                            "kind": "LLMInferenceService",
                            "metadata": {
                                "name": self.llmis_name(),
                                "namespace": metadata.NAMESPACE_REMOTE,
                            },
                            "spec": llmis_spec,
                        },
                    ),
                ),
            ),
        )

    def llmis_name(self):
        """LLMInferenceService name on the remote cluster.

        Derived from the parent ModelDeployment name (the part of the
        replica name before the cluster suffix), so all replicas of the
        same deployment land at the same path on every remote gateway.
        """
        # The replica name is "<deployment>-<cluster>" by construction.
        cluster_suffix = f"-{self.xr.spec.inferenceClusterRef.name}"
        replica_name = self.xr.metadata.name
        deployment_name = (
            replica_name[: -len(cluster_suffix)] if replica_name.endswith(cluster_suffix) else replica_name
        )
        return naming.llmis_name(deployment_name)

    def derive_conditions(self):
        """Derive ModelAccepted and ModelReady conditions.

        Routing is no longer this function's concern - ModelEndpoint
        composes the control-plane Backend and ModelService composes
        the HTTPRoute.
        """

        # First-time transition: emit a normal event the first reconcile.
        if MODEL_RESOURCE_KEY not in self.req.observed.resources:
            response.normal(
                self.rsp,
                f"Composing {self.xr.spec.engine.image}"
                f" on {self.xr.spec.inferenceClusterRef.name}"
                f" ({self.xr.spec.workers.topology.strategy})",
            )

        # Check if the remote resource was created by reading the Object's
        # atProvider.manifest. provider-kubernetes populates this field after
        # successfully observing the remote resource at least once.
        serving_accepted = False
        serving_observed = self.req.observed.resources.get(MODEL_RESOURCE_KEY)
        if serving_observed:
            obj = k8sobjv1alpha1.Object.model_validate(resource.struct_to_dict(serving_observed.resource))
            serving_accepted = bool(obj.status and obj.status.atProvider and obj.status.atProvider.manifest)

        serving_ready = conditions.has_condition(self.req, MODEL_RESOURCE_KEY, "Ready")

        # ModelAccepted: the remote resource was created on the cluster.
        accepted_reason = CONDITION_REASON_ACCEPTED if serving_accepted else CONDITION_REASON_DEPLOYING
        conditions.set_condition(self.rsp, CONDITION_TYPE_MODEL_ACCEPTED, serving_accepted, accepted_reason)

        # ModelReady: the model is actually serving traffic.
        if serving_ready:
            ready_reason = CONDITION_REASON_SERVING
        elif serving_accepted:
            ready_reason = CONDITION_REASON_MODEL_STARTING
        else:
            ready_reason = CONDITION_REASON_WAITING_FOR_MODEL
        conditions.set_condition(self.rsp, CONDITION_TYPE_MODEL_READY, serving_ready, ready_reason)

        # Per-resource readiness.
        if MODEL_RESOURCE_KEY in self.rsp.desired.resources and serving_ready:
            self.rsp.desired.resources[MODEL_RESOURCE_KEY].ready = fnv1.READY_TRUE


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose model serving resources on the remote cluster."""
    Composer(req, rsp).compose()
