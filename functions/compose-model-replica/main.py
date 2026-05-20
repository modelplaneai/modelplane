"""Deploy a model on a single InferenceCluster.

This function reads the referenced InferenceCluster via required
resources, then composes a KServe LLMInferenceService on the remote
cluster.

GPU count comes from spec.workers.topology directly:
- tensor:   GPUs per node.
- pipeline: nodes per worker (default 1). Values > 1 use
            LeaderWorkerSet for multi-node serving.

The worker template is a curated subset of PodTemplateSpec. The
container named "engine" is the inference engine; its image and args
are passed through to the LLMInferenceService.
"""

from crossplane.function import request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .lib import conditions, defaults, metadata, naming
from .lib.metadata import LABEL_KEY_DEPLOYMENT
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

# Composed resource key for the model serving resource.
MODEL_RESOURCE_KEY = "model-serving"


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

    def _engine_container(self):
        """Return the container named 'engine' from the worker template.

        The XRD enforces via CEL validation that exactly one container
        named 'engine' exists, so this always succeeds.
        """
        return next(c for c in self.xr.spec.workers.template.spec.containers if c.name == "engine")

    def compose_model_serving(self):
        """Compose the LLMInferenceService on the remote cluster."""
        topology = self.xr.spec.workers.topology
        template = self.xr.spec.workers.template
        self.engine = self._engine_container()
        engine = self.engine

        gpu_per_pod = int(topology.tensor)
        multi_node = int(topology.pipeline or 1) > 1

        # Extract the model name from engine args (e.g. --model=Qwen/...)
        # to build the HuggingFace URI that KServe requires. Strip the
        # --model= arg from the container args — KServe handles model
        # fetching via model.uri and invokes the engine with the local
        # model path.
        #
        # TODO(negz): Stop doing this when we drop KServe. It's a hack.
        model_name = ""
        container_args = []
        for arg in list(engine.args or []):
            if arg.startswith("--model="):
                model_name = arg.split("=", 1)[1]
            else:
                container_args.append(arg)

        container = self._build_container(engine, gpu_per_pod, container_args)
        pod_spec = self._build_pod_spec(template, container)
        llmis_template = self._build_llmis_template(template, pod_spec)

        llmis_spec: dict = {
            "model": {"uri": f"hf://{model_name}" if model_name else "hf://unknown"},
            "replicas": 1,
            "template": llmis_template,
            "router": {"gateway": {}, "route": {}},
        }

        # Multi-node: pipeline pods, each with `tensor` GPUs.
        # Total tensor parallelism = tensor * pipeline. Worker count is
        # pipeline - 1 (the leader is the "main" template).
        if multi_node:
            total_gpus = gpu_per_pod * int(topology.pipeline)
            llmis_spec["parallelism"] = {"tensor": total_gpus}
            llmis_spec["worker"] = {
                "size": int(topology.pipeline) - 1,
                "template": self._build_llmis_template(template, pod_spec),
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

    def _build_container(self, engine, gpu_per_pod: int, args: list[str]) -> dict:
        """Build the LLMInferenceService container dict from the engine container.

        GPU count is set via the device plugin. CPU and memory resource
        requirements are not set; DRA will handle device binding
        (including non-GPU resources) in a future version.
        """
        container: dict = {
            "name": "main",
            "image": engine.image,
            "args": args,
            "securityContext": {"runAsUser": 0, "runAsNonRoot": False},
            "resources": {
                "limits": {"nvidia.com/gpu": str(gpu_per_pod)},
            },
        }

        if engine.env:
            container["env"] = [e.model_dump(exclude_none=True) for e in engine.env]

        return container

    def _build_pod_spec(self, template, container: dict) -> dict:
        """Build the pod spec dict from the worker template."""
        pod_spec: dict = {"containers": [container]}
        if template.spec.imagePullSecrets:
            pod_spec["imagePullSecrets"] = [s.model_dump(exclude_none=True) for s in template.spec.imagePullSecrets]
        return pod_spec

    def _build_llmis_template(self, template, pod_spec: dict) -> dict:
        """Build the LLMInferenceService template, adding pod metadata if set."""
        if not template.metadata:
            return pod_spec
        meta = {}
        if template.metadata.labels:
            meta["labels"] = dict(template.metadata.labels)
        if template.metadata.annotations:
            meta["annotations"] = dict(template.metadata.annotations)
        return {"metadata": meta, **pod_spec} if meta else pod_spec

    def llmis_name(self):
        """LLMInferenceService name on the remote cluster.

        Read from the modelplane.ai/deployment label that
        compose-model-deployment sets on every replica. All replicas of
        the same deployment land at the same path on every remote gateway.
        """
        labels = self.xr.metadata.labels or {}
        deployment_name = labels.get(LABEL_KEY_DEPLOYMENT, self.xr.metadata.name)
        return naming.llmis_name(deployment_name)

    def derive_conditions(self):
        """Derive ModelAccepted and ModelReady conditions."""

        # First-time transition: emit a normal event the first reconcile.
        if MODEL_RESOURCE_KEY not in self.req.observed.resources:
            image = self.engine.image
            response.normal(
                self.rsp,
                f"Composing {image} on {self.xr.spec.inferenceClusterRef.name}",
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
            ready_reason = conditions.CONDITION_REASON_MODEL_STARTING
        else:
            ready_reason = CONDITION_REASON_WAITING_FOR_MODEL
        conditions.set_condition(self.rsp, CONDITION_TYPE_MODEL_READY, serving_ready, ready_reason)

        # Per-resource readiness.
        if MODEL_RESOURCE_KEY in self.rsp.desired.resources and serving_ready:
            self.rsp.desired.resources[MODEL_RESOURCE_KEY].ready = fnv1.READY_TRUE


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose model serving resources on the remote cluster."""
    Composer(req, rsp).compose()
