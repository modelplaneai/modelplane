"""Deploy a model on a single InferenceEnvironment.

This function reads the referenced ClusterModel (or Model) and
InferenceEnvironment via required resources, resolves the matching serving
profile, computes GPU count from model VRAM vs pool VRAM, and composes
resources on the remote cluster.

It creates a KServe LLMInferenceService on the remote cluster and an Envoy
Gateway Backend on the control plane for ModelDeployment to route through.
"""

import math
from dataclasses import dataclass

from crossplane.function import request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .lib import conditions, defaults, metadata, naming, prometheus, quantities, serving
from .lib import resource as libresource
from .model.ai.modelplane.clustermodel import v1alpha1 as cmv1alpha1
from .model.ai.modelplane.inferenceenvironment import v1alpha1 as iev1alpha1
from .model.ai.modelplane.model import v1alpha1 as mv1alpha1
from .model.ai.modelplane.modelplacement import v1alpha1
from .model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

# Condition types and reasons for the ModelPlacement XR.
CONDITION_TYPE_MODEL_ACCEPTED = "ModelAccepted"
CONDITION_TYPE_MODEL_READY = "ModelReady"
CONDITION_TYPE_PROFILE_MATCHED = "ProfileMatched"

CONDITION_REASON_WAITING_FOR_REFERENCES = "WaitingForReferences"
CONDITION_REASON_WAITING_FOR_ENVIRONMENT = "WaitingForEnvironment"
CONDITION_REASON_WAITING_FOR_MODEL = "WaitingForModel"
CONDITION_REASON_WAITING_FOR_CLUSTER = "WaitingForCluster"
CONDITION_REASON_WAITING_FOR_GATEWAY = "WaitingForGateway"
CONDITION_REASON_NO_MATCHING_PROFILE = "NoMatchingProfile"
CONDITION_REASON_DEPLOYING = "Deploying"
CONDITION_REASON_ACCEPTED = "Accepted"
CONDITION_REASON_SERVING = "Serving"
CONDITION_REASON_MODEL_STARTING = "ModelStarting"
CONDITION_REASON_BACKEND_CONFIGURED = "BackendConfigured"
CONDITION_REASON_PROFILE_RESOLVED = "ProfileResolved"

# Composed resource key for the model serving resource, regardless of backend.
MODEL_RESOURCE_KEY = "model-serving"


@dataclass
class GpuAllocation:
    """Result of compute_gpus() — describes how GPUs should be distributed."""

    total: int  # Total GPUs needed (ceil(model_vram / gpu_vram))
    per_node: int  # GPUs available per node in the selected pool
    node_count: int  # Nodes needed (ceil(total / per_node))

    @property
    def multi_node(self) -> bool:
        """Whether the model requires more GPUs than a single node has."""
        return self.total > self.per_node


class Composer:
    def __init__(self, req, rsp):
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.ModelPlacement(**resource.struct_to_dict(req.observed.composite.resource))

        # Required resources and resolved profile — set by resolve_inputs.
        self.model = None
        self.ie = None
        self.profile = None

    def compose(self):
        if not self.resolve_inputs():
            return
        gpus = self.compute_gpus()
        self.compose_model_serving(gpus)
        self.compose_backend()
        self.write_status(gpus)
        self.derive_conditions()

    def resolve_inputs(self):
        """Declare and fetch required resources, then resolve the serving
        profile. Returns False if critical inputs are missing or no profile
        matches."""
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

        # Resolve the serving profile by walking the model's serving[] array.
        self.profile = serving.match_profile(self.model, self.ie)
        if not self.profile:
            conditions.set_condition(
                self.rsp, CONDITION_TYPE_PROFILE_MATCHED, False, CONDITION_REASON_NO_MATCHING_PROFILE
            )
            response.warning(
                self.rsp,
                f"No serving profile matches environment {self.xr.spec.inferenceEnvironmentRef.name}",
            )
            return False

        return True

    def compute_gpus(self):
        """Compute how many GPUs the model needs and whether multi-node
        inference is required.

        Returns a GpuAllocation with total GPUs needed, per-node GPU count
        from the pool, and the number of nodes required.
        """
        for pool in self.ie.status.capacity.gpuPools:
            pool_memory = quantities.parse_quantity(pool.memory or "0Gi")
            if pool_memory > 0:
                total = max(
                    1,
                    math.ceil(quantities.parse_quantity(self.model.spec.resources.vram) / pool_memory),
                )
                per_node = int(pool.countPerNode)
                node_count = max(1, math.ceil(total / per_node)) if per_node > 0 else 1
                return GpuAllocation(
                    total=total,
                    per_node=min(per_node, total),
                    node_count=node_count,
                )
        return GpuAllocation(total=1, per_node=1, node_count=1)

    def compose_model_serving(self, gpus):
        """Compose the model serving resource on the remote cluster."""
        self.compose_kserve_llmis(gpus)
        if self.has_autoscaling():
            self.compose_kserve_keda_scaledobject()

    def compose_kserve_llmis(self, gpus):
        """Compose a provider-kubernetes Object wrapping an LLMInferenceService
        on the remote cluster.

        For single-node models, each pod gets all required GPUs. For multi-node
        models (VRAM exceeds a single node), the LLMInferenceService is composed
        with a parallelism section and a worker section that uses
        LeaderWorkerSet for cross-node tensor parallelism.
        """
        llmis_name = naming.to_dns_label(self.xr.spec.modelRef.name)

        args = list(self.profile.engine.args or [])

        # Each pod gets per_node GPUs (or total if single-node).
        gpu_per_pod = gpus.per_node if gpus.multi_node else gpus.total

        container: dict = {
            "name": "main",
            "image": self.profile.engine.image,
            "args": args,
            "securityContext": {"runAsUser": 0, "runAsNonRoot": False},
            "resources": {
                "limits": {
                    "nvidia.com/gpu": str(gpu_per_pod),
                    "cpu": self.model.spec.resources.cpu,
                    "memory": self.model.spec.resources.memory,
                },
                "requests": {"cpu": "1", "memory": self.model.spec.resources.memory},
            },
        }

        llmis_spec: dict = {
            "model": {
                "uri": f"hf://{self.model.spec.huggingFace.repo}" if self.model.spec.huggingFace else "",
                "name": self.model.spec.model.name,
            },
            "replicas": self.worker_replicas(),
            "template": {"containers": [container]},
            "router": {"gateway": {}, "route": {}},
        }

        if gpus.multi_node:
            llmis_spec["parallelism"] = {"tensor": gpus.total}
            # Worker pods use the same container spec as the leader.
            worker_container = dict(container)
            llmis_spec["worker"] = {
                "size": gpus.node_count - 1,
                "template": {"containers": [worker_container]},
            }

        resource.update(
            self.rsp.desired.resources[MODEL_RESOURCE_KEY],
            k8sobjv1alpha1.Object(
                spec=k8sobjv1alpha1.Spec(
                    providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                        kind="ClusterProviderConfig",
                        name=self.ie.status.providerConfigRef.name,
                    ),
                    readiness=k8sobjv1alpha1.Readiness(
                        policy="DeriveFromObject",
                    ),
                    forProvider=k8sobjv1alpha1.ForProvider(
                        manifest={
                            "apiVersion": "serving.kserve.io/v1alpha1",
                            "kind": "LLMInferenceService",
                            "metadata": {
                                "name": llmis_name,
                                "namespace": metadata.NAMESPACE_REMOTE,
                            },
                            "spec": llmis_spec,
                        },
                    ),
                ),
            ),
        )

    def compose_kserve_keda_scaledobject(self):
        """Compose a KEDA ScaledObject on the remote cluster that targets the
        KServe Deployment for autoscaling.

        KServe's LLMInferenceService (v1alpha1) creates a Deployment named
        {llmis-name}-kserve. The ScaledObject uses Envoy Gateway's
        envoy_cluster_upstream_rq_active metric to measure in-flight
        requests at the proxy level. KServe creates an HTTPRoute named
        {llmis-name}-kserve-route, and Envoy uses that as the cluster
        name in its metrics."""
        llmis_name = naming.to_dns_label(self.xr.spec.modelRef.name)
        scaling = self.xr.spec.scaling
        concurrency = scaling.concurrency

        target = concurrency.target if concurrency.target is not None else 1
        utilization = concurrency.utilization if concurrency.utilization is not None else 70
        threshold = str(max(1, target * utilization // 100))

        # Envoy names its upstream clusters after the HTTPRoute:
        # httproute/{namespace}/{route-name}/rule/{index}. KServe's
        # LLMInferenceService creates a route named {name}-kserve-route.
        envoy_cluster = f"httproute/{metadata.NAMESPACE_REMOTE}/{llmis_name}-kserve-route/rule/0"

        min_replicas = concurrency.minReplicas if concurrency.minReplicas is not None else 1
        max_replicas = concurrency.maxReplicas if concurrency.maxReplicas is not None else 1
        cooldown = concurrency.scaleDownDelay if concurrency.scaleDownDelay is not None else 300

        if max_replicas < min_replicas:
            response.warning(
                self.rsp,
                f"maxReplicas ({max_replicas}) is less than minReplicas ({min_replicas}), skipping ScaledObject",
            )
            return

        scaledobject_manifest = {
            "apiVersion": "keda.sh/v1alpha1",
            "kind": "ScaledObject",
            "metadata": {
                "name": f"{llmis_name}-scaler",
                "namespace": metadata.NAMESPACE_REMOTE,
            },
            "spec": {
                "scaleTargetRef": {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": f"{llmis_name}-kserve",
                },
                "minReplicaCount": min_replicas,
                "maxReplicaCount": max_replicas,
                "pollingInterval": 15,
                "cooldownPeriod": cooldown,
                "triggers": [
                    {
                        "type": "prometheus",
                        "metadata": {
                            "serverAddress": prometheus.URL,
                            "metricName": "envoy_cluster_upstream_rq_active",
                            "query": (f'sum(envoy_cluster_upstream_rq_active{{envoy_cluster_name="{envoy_cluster}"}})'),
                            "threshold": threshold,
                        },
                    },
                ],
            },
        }

        resource.update(
            self.rsp.desired.resources["keda-scaledobject"],
            k8sobjv1alpha1.Object(
                spec=k8sobjv1alpha1.Spec(
                    providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                        kind="ClusterProviderConfig",
                        name=self.ie.status.providerConfigRef.name,
                    ),
                    readiness=k8sobjv1alpha1.Readiness(
                        policy="DeriveFromObject",
                    ),
                    forProvider=k8sobjv1alpha1.ForProvider(
                        manifest=scaledobject_manifest,
                    ),
                ),
            ),
        )

    def has_autoscaling(self):
        """Check if the placement has autoscaling (not fixed) configured."""
        return self.xr.spec.scaling.signal != "Fixed"

    def worker_replicas(self):
        """Return the desired worker replica count from scaling config.

        For Concurrency scaling, this returns minReplicas (which may be 0
        for scale-to-zero). KEDA manages scaling from there."""
        scaling = self.xr.spec.scaling
        if scaling.signal == "Fixed" and scaling.fixed:
            return scaling.fixed.replicas if scaling.fixed.replicas is not None else 1
        if scaling.signal == "Concurrency" and scaling.concurrency:
            return scaling.concurrency.minReplicas if scaling.concurrency.minReplicas is not None else 1
        return 1

    def compose_backend(self):
        """Compose a Backend on the control plane pointing to the remote
        cluster's gateway. ModelDeployment aggregates these into an HTTPRoute."""
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
        model_name = naming.to_dns_label(self.xr.spec.modelRef.name)

        status = v1alpha1.Status(
            model=v1alpha1.Model(name=self.model.spec.model.name),
            resources=v1alpha1.Resources(gpu=v1alpha1.Gpu(count=gpus.total)),
            servingProfile=v1alpha1.ServingProfile(
                name=self.profile.name,
                engine=v1alpha1.Engine(
                    name=self.profile.engine.name,
                    image=self.profile.engine.image,
                ),
            ),
        )
        if self.ie.status.gateway.address:
            status.endpoint = v1alpha1.Endpoint(
                url=f"http://{self.ie.status.gateway.address}/{metadata.NAMESPACE_REMOTE}/{model_name}/v1",
            )

        # Read the Backend's Crossplane-generated name from observed state so
        # ModelDeployment can reference it in the HTTPRoute.
        backend_observed = self.req.observed.resources.get("backend")
        if backend_observed:
            backend_name = resource.struct_to_dict(backend_observed.resource).get("metadata", {}).get("name")
            if backend_name:
                status.routing = v1alpha1.Routing(backendName=backend_name)

        libresource.update_status(self.rsp.desired.composite, status)

        # Transition: first time composing the model serving resource.
        if MODEL_RESOURCE_KEY not in self.req.observed.resources:
            response.normal(
                self.rsp,
                f"Composing deployment for {self.model.spec.model.name}"
                f" on {self.xr.spec.inferenceEnvironmentRef.name}"
                f" (profile: {self.profile.name}, engine: {self.profile.engine.name}, GPUs: {gpus})",
            )

    def derive_conditions(self):
        """Derive ModelAccepted, ModelReady, and RoutingReady conditions."""

        # Check if the remote resource was created by reading the Object's
        # atProvider.manifest. provider-kubernetes populates this field after
        # successfully observing the remote resource at least once.
        serving_accepted = False
        serving_observed = self.req.observed.resources.get(MODEL_RESOURCE_KEY)
        if serving_observed:
            obj = k8sobjv1alpha1.Object.model_validate(resource.struct_to_dict(serving_observed.resource))
            serving_accepted = bool(obj.status and obj.status.atProvider and obj.status.atProvider.manifest)

        serving_ready = conditions.has_condition(self.req, MODEL_RESOURCE_KEY, "Ready")
        backend_exists = "backend" in self.req.observed.resources

        # ProfileMatched: a serving profile was resolved for this environment.
        conditions.set_condition(self.rsp, CONDITION_TYPE_PROFILE_MATCHED, True, CONDITION_REASON_PROFILE_RESOLVED)

        # ModelAccepted: the remote resource was created on the cluster.
        if serving_accepted:
            accepted_reason = CONDITION_REASON_ACCEPTED
        elif serving_observed:
            accepted_reason = CONDITION_REASON_DEPLOYING
        else:
            accepted_reason = CONDITION_REASON_DEPLOYING
        conditions.set_condition(self.rsp, CONDITION_TYPE_MODEL_ACCEPTED, serving_accepted, accepted_reason)

        # ModelReady: the model is actually serving traffic.
        if serving_ready:
            ready_reason = CONDITION_REASON_SERVING
        elif serving_accepted:
            ready_reason = CONDITION_REASON_MODEL_STARTING
        else:
            ready_reason = CONDITION_REASON_WAITING_FOR_MODEL
        conditions.set_condition(self.rsp, CONDITION_TYPE_MODEL_READY, serving_ready, ready_reason)

        # RoutingReady: the Backend resource exists on the control plane.
        conditions.set_condition(
            self.rsp,
            conditions.CONDITION_TYPE_ROUTING_READY,
            backend_exists,
            CONDITION_REASON_BACKEND_CONFIGURED if backend_exists else CONDITION_REASON_WAITING_FOR_GATEWAY,
        )

        # Per-resource readiness.
        if MODEL_RESOURCE_KEY in self.rsp.desired.resources and serving_ready:
            self.rsp.desired.resources[MODEL_RESOURCE_KEY].ready = fnv1.READY_TRUE
        if backend_exists:
            self.rsp.desired.resources["backend"].ready = fnv1.READY_TRUE
        keda_ready = conditions.has_condition(self.req, "keda-scaledobject", "Ready")
        if "keda-scaledobject" in self.rsp.desired.resources and keda_ready:
            self.rsp.desired.resources["keda-scaledobject"].ready = fnv1.READY_TRUE


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose model serving resources on the remote cluster."""
    Composer(req, rsp).compose()
