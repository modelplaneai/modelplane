"""Deploy a model on a single InferenceEnvironment.

This function reads the referenced ClusterModel (or Model) and
InferenceEnvironment via required resources, computes GPU count from model
VRAM vs pool VRAM, and composes a provider-kubernetes Object wrapping an
LLMInferenceService on the remote cluster.
"""

import math

from crossplane.function import request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .lib import conditions, defaults, metadata, naming, quantities
from .lib import resource as libresource
from .model.ai.modelplane.clustermodel import v1alpha1 as cmv1alpha1
from .model.ai.modelplane.inferenceenvironment import v1alpha1 as iev1alpha1
from .model.ai.modelplane.model import v1alpha1 as mv1alpha1
from .model.ai.modelplane.modelplacement import v1alpha1
from .model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

# Condition types and reasons for the ModelPlacement XR.
CONDITION_TYPE_MODEL_ACCEPTED = "ModelAccepted"
CONDITION_TYPE_MODEL_READY = "ModelReady"

CONDITION_REASON_WAITING_FOR_REFERENCES = "WaitingForReferences"
CONDITION_REASON_WAITING_FOR_ENVIRONMENT = "WaitingForEnvironment"
CONDITION_REASON_WAITING_FOR_MODEL = "WaitingForModel"
CONDITION_REASON_WAITING_FOR_CLUSTER = "WaitingForCluster"
CONDITION_REASON_WAITING_FOR_GATEWAY = "WaitingForGateway"
CONDITION_REASON_DEPLOYING = "Deploying"
CONDITION_REASON_ACCEPTED = "Accepted"
CONDITION_REASON_SERVING = "Serving"
CONDITION_REASON_MODEL_STARTING = "ModelStarting"
CONDITION_REASON_BACKEND_CONFIGURED = "BackendConfigured"


class Composer:
    def __init__(self, req, rsp):
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.ModelPlacement(**resource.struct_to_dict(req.observed.composite.resource))

        # Required resources — set by _resolve_inputs.
        self.model = None
        self.ie = None

    def compose(self):
        if not self.resolve_inputs():
            return
        gpus = self.compute_gpus()
        self.compose_llmis(gpus)
        self.compose_backend()
        self.write_status(gpus)
        self.derive_conditions()

    def resolve_inputs(self):
        """Declare and fetch required resources. Returns False if critical
        inputs are missing."""
        response.require_resources(
            self.rsp,
            name="model",
            api_version="modelplane.ai/v1alpha1",
            kind=self.xr.spec.modelRef.kind,
            match_name=self.xr.spec.modelRef.name,
        )
        response.require_resources(
            self.rsp,
            name="environment",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceEnvironment",
            match_name=self.xr.spec.inferenceEnvironmentRef.name,
        )

        model_dict = request.get_required_resource(self.req, "model")
        ie_dict = request.get_required_resource(self.req, "environment")
        if model_dict is None or ie_dict is None:
            conditions.set_condition(
                self.rsp, CONDITION_TYPE_MODEL_ACCEPTED, False, CONDITION_REASON_WAITING_FOR_REFERENCES
            )
            conditions.set_condition(self.rsp, CONDITION_TYPE_MODEL_READY, False, CONDITION_REASON_WAITING_FOR_MODEL)
            conditions.set_condition(
                self.rsp, conditions.CONDITION_TYPE_ROUTING_READY, False, CONDITION_REASON_WAITING_FOR_MODEL
            )
            response.normal(self.rsp, "Waiting for model and environment to be resolved")
            return False

        self.ie = defaults.inference_environment(iev1alpha1.InferenceEnvironment.model_validate(ie_dict))

        if not self.ie.status.providerConfigRef.name:
            conditions.set_condition(
                self.rsp, CONDITION_TYPE_MODEL_ACCEPTED, False, CONDITION_REASON_WAITING_FOR_ENVIRONMENT
            )
            conditions.set_condition(self.rsp, CONDITION_TYPE_MODEL_READY, False, CONDITION_REASON_WAITING_FOR_MODEL)
            conditions.set_condition(
                self.rsp, conditions.CONDITION_TYPE_ROUTING_READY, False, CONDITION_REASON_WAITING_FOR_MODEL
            )
            response.normal(self.rsp, "Waiting for environment providerConfigRef")
            return False

        if self.xr.spec.modelRef.kind == "Model":
            self.model = defaults.cluster_model(mv1alpha1.ModelModel.model_validate(model_dict))
        else:
            self.model = defaults.cluster_model(cmv1alpha1.ClusterModel.model_validate(model_dict))

        return True

    def compute_gpus(self):
        """Compute how many GPUs the model needs by dividing model VRAM by
        the per-GPU VRAM of the first eligible pool in the environment."""
        for pool in self.ie.status.capacity.gpuPools:
            pool_memory = quantities.parse_quantity(pool.memory or "0Gi")
            if pool_memory > 0:
                return max(
                    1,
                    math.ceil(quantities.parse_quantity(self.model.spec.resources.vram) / pool_memory),
                )
        return 1

    def compose_llmis(self, gpus):
        """Compose a provider-kubernetes Object wrapping an LLMInferenceService
        on the remote cluster."""
        # Use the ClusterModel name (sanitized to DNS-1035) as the LLMIS name
        # on all remote clusters. This means the remote path is the same
        # regardless of which environment, fixing multi-environment routing.
        llmis_name = naming.to_dns_label(self.xr.spec.modelRef.name)

        # Build the container spec for the vLLM model server. Always set
        # --served-model-name so vLLM registers the model under the name
        # from the ClusterModel spec, not the local path (/mnt/models).
        args = [f"--served-model-name={self.model.spec.model.name}"]
        if self.model.spec.vllm.extraArgs:
            args.extend(self.model.spec.vllm.extraArgs)

        container: dict = {
            "name": "main",
            "image": self.model.spec.vllm.image,
            "args": args,
            "securityContext": {"runAsUser": 0, "runAsNonRoot": False},
            "resources": {
                "limits": {
                    "nvidia.com/gpu": str(gpus),
                    "cpu": self.model.spec.resources.cpu,
                    "memory": self.model.spec.resources.memory,
                },
                "requests": {"cpu": "1", "memory": self.model.spec.resources.memory},
            },
        }

        # Use DeriveFromCelQuery so the Object's Ready condition reflects the
        # LLMIS actually serving traffic, not just being created.
        resource.update(
            self.rsp.desired.resources["llm-inference-service"],
            k8sobjv1alpha1.Object(
                spec=k8sobjv1alpha1.Spec(
                    providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                        kind="ClusterProviderConfig",
                        name=self.ie.status.providerConfigRef.name,
                    ),
                    readiness=k8sobjv1alpha1.Readiness(
                        policy="DeriveFromCelQuery",
                        celQuery='object.status.conditions.exists(c, c.type == "Ready" && c.status == "True")',
                    ),
                    forProvider=k8sobjv1alpha1.ForProvider(
                        manifest={
                            "apiVersion": "serving.kserve.io/v1alpha1",
                            "kind": "LLMInferenceService",
                            "metadata": {
                                "name": llmis_name,
                                "namespace": metadata.NAMESPACE_REMOTE,
                            },
                            "spec": {
                                "model": {
                                    "uri": f"hf://{self.model.spec.huggingFace.repo}"
                                    if self.model.spec.huggingFace
                                    else "",
                                    "name": self.model.spec.model.name,
                                },
                                "replicas": 1,
                                "template": {"containers": [container]},
                                "router": {"gateway": {}, "route": {}},
                            },
                        },
                    ),
                ),
            ),
        )

    def compose_backend(self):
        """Compose a Backend on the control plane pointing to the remote
        cluster's KServe gateway. ModelDeployment aggregates these into an
        HTTPRoute."""
        if not self.ie.status.gateway.address:
            return

        resource.update(
            self.rsp.desired.resources["backend"],
            {
                "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                "kind": "Backend",
                "metadata": {"namespace": self.xr.metadata.namespace},
                "spec": {
                    "endpoints": [{"ip": {"address": self.ie.status.gateway.address, "port": 80}}],
                },
            },
        )

    def write_status(self, gpus):
        """Write status fields for consumption by compose-model-deployment."""
        llmis_name = naming.to_dns_label(self.xr.spec.modelRef.name)

        status = v1alpha1.Status(
            model=v1alpha1.Model(name=self.model.spec.model.name),
            resources=v1alpha1.Resources(gpu=v1alpha1.Gpu(count=gpus)),
        )
        if self.ie.status.gateway.address:
            status.endpoint = v1alpha1.Endpoint(
                url=f"http://{self.ie.status.gateway.address}/{metadata.NAMESPACE_REMOTE}/{llmis_name}/v1",
            )

        # Read the Backend's Crossplane-generated name from observed state so
        # ModelDeployment can reference it in the HTTPRoute.
        backend_observed = self.req.observed.resources.get("backend")
        if backend_observed:
            backend_name = resource.struct_to_dict(backend_observed.resource).get("metadata", {}).get("name")
            if backend_name:
                status.routing = v1alpha1.Routing(backendName=backend_name)

        libresource.update_status(self.rsp.desired.composite, status)

        # Transition: first time composing the LLMInferenceService.
        if "llm-inference-service" not in self.req.observed.resources:
            response.normal(
                self.rsp,
                f"Composing LLMInferenceService for {self.model.spec.model.name}"
                f" on {self.xr.spec.inferenceEnvironmentRef.name}, GPUs: {gpus}",
            )

    def derive_conditions(self):
        """Derive ModelAccepted, ModelReady, and RoutingReady conditions."""
        llmis_exists = "llm-inference-service" in self.req.observed.resources
        llmis_synced = conditions.has_condition(self.req, "llm-inference-service", "Synced")
        llmis_accepted = llmis_exists and llmis_synced
        llmis_ready = conditions.has_condition(self.req, "llm-inference-service", "Ready")
        backend_exists = "backend" in self.req.observed.resources

        # ModelAccepted: the backend accepted the model workload (Object synced).
        if not llmis_exists:
            accepted_reason = CONDITION_REASON_DEPLOYING
        elif llmis_accepted:
            accepted_reason = CONDITION_REASON_ACCEPTED
        else:
            accepted_reason = CONDITION_REASON_WAITING_FOR_CLUSTER
        conditions.set_condition(self.rsp, CONDITION_TYPE_MODEL_ACCEPTED, llmis_accepted, accepted_reason)

        # ModelReady: the LLMIS is actually serving traffic.
        if llmis_ready:
            ready_reason = CONDITION_REASON_SERVING
        elif llmis_accepted:
            ready_reason = CONDITION_REASON_MODEL_STARTING
        else:
            ready_reason = CONDITION_REASON_WAITING_FOR_MODEL
        conditions.set_condition(self.rsp, CONDITION_TYPE_MODEL_READY, llmis_ready, ready_reason)

        # RoutingReady: the Backend resource exists on the control plane.
        conditions.set_condition(
            self.rsp,
            conditions.CONDITION_TYPE_ROUTING_READY,
            backend_exists,
            CONDITION_REASON_BACKEND_CONFIGURED if backend_exists else CONDITION_REASON_WAITING_FOR_GATEWAY,
        )

        # Per-resource readiness.
        if llmis_ready:
            self.rsp.desired.resources["llm-inference-service"].ready = fnv1.READY_TRUE
        if backend_exists:
            self.rsp.desired.resources["backend"].ready = fnv1.READY_TRUE


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose an LLMInferenceService on the remote cluster."""
    Composer(req, rsp).compose()
