"""Deploy a model on a single InferenceEnvironment.

This function reads the referenced ClusterModel (or Model) and
InferenceEnvironment via required resources, computes GPU count from model
VRAM vs pool VRAM, and composes a provider-kubernetes Object wrapping an
LLMInferenceService on the remote cluster.
"""

import math

from crossplane.function import request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .lib import conditions
from .lib import defaults
from .lib import naming
from .lib import quantities
from .lib import resource as libresource
from .model.ai.modelplane.clustermodel import v1alpha1 as cmv1alpha1
from .model.ai.modelplane.inferenceenvironment import v1alpha1 as iev1alpha1
from .model.ai.modelplane.modelplacement import v1alpha1
from .model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose an LLMInferenceService on the remote cluster."""
    xr = v1alpha1.ModelPlacement(
        **resource.struct_to_dict(req.observed.composite.resource)
    )

    model_kind = xr.spec.modelRef.kind or "ClusterModel"
    model_name = xr.spec.modelRef.name
    ie_name = xr.spec.inferenceEnvironmentRef.name

    # Declare required resources on every reconcile. Crossplane resolves
    # them and makes them available via request.get_required_resource.
    response.require_resources(
        rsp,
        name="model",
        api_version="modelplane.ai/v1alpha1",
        kind=model_kind,
        match_name=model_name,
    )
    response.require_resources(
        rsp,
        name="environment",
        api_version="modelplane.ai/v1alpha1",
        kind="InferenceEnvironment",
        match_name=ie_name,
    )


    model_dict = request.get_required_resource(req, "model")
    ie_dict = request.get_required_resource(req, "environment")
    if model_dict is None or ie_dict is None:
        conditions.set_condition(rsp, "ModelAccepted", False, "WaitingForReferences")
        conditions.set_condition(rsp, "ModelReady", False, "WaitingForModel")
        conditions.set_condition(rsp, "RoutingReady", False, "WaitingForModel")
        response.normal(rsp, "Waiting for model and environment to be resolved")
        return

    ie = defaults.inference_environment(
        iev1alpha1.InferenceEnvironment.model_validate(ie_dict)
    )

    if not ie.status.providerConfigRef.name:
        conditions.set_condition(rsp, "ModelAccepted", False, "WaitingForEnvironment")
        conditions.set_condition(rsp, "ModelReady", False, "WaitingForModel")
        conditions.set_condition(rsp, "RoutingReady", False, "WaitingForModel")
        response.normal(rsp, "Waiting for environment providerConfigRef")
        return

    model = defaults.cluster_model(
        cmv1alpha1.ClusterModel.model_validate(model_dict)
    )

    # Compute how many GPUs the model needs by dividing model VRAM by the
    # per-GPU VRAM of the first eligible pool in the environment.
    gpus_per_replica = 1
    for pool in ie.status.capacity.gpuPools:
        pool_memory = quantities.parse_quantity(pool.memory or "0Gi")
        if pool_memory > 0:
            gpus_per_replica = max(1, math.ceil(
                quantities.parse_quantity(model.spec.resources.vram) / pool_memory
            ))
            break

    # Use the ClusterModel name (sanitized to DNS-1035) as the LLMIS name on
    # all remote clusters. This means the remote path is the same regardless
    # of which environment, fixing multi-environment routing.
    llmis_name = naming.to_dns_label(model_name)
    llmis_namespace = "default"

    # Build the container spec for the vLLM model server. Always set
    # --served-model-name so vLLM registers the model under the name
    # from the ClusterModel spec, not the local path (/mnt/models).
    args = [f"--served-model-name={model.spec.model.name}"]
    if model.spec.vllm.extraArgs:
        args.extend(model.spec.vllm.extraArgs)

    container: dict = {
        "name": "main",
        "image": model.spec.vllm.image,
        "args": args,
        "securityContext": {"runAsUser": 0, "runAsNonRoot": False},
        "resources": {
            "limits": {
                "nvidia.com/gpu": str(gpus_per_replica),
                "cpu": model.spec.resources.cpu,
                "memory": model.spec.resources.memory,
            },
            "requests": {"cpu": "1", "memory": model.spec.resources.memory},
        },
    }

    # Compose a provider-kubernetes Object wrapping an LLMInferenceService
    # on the remote cluster. Use DeriveFromCelQuery so the Object's Ready
    # condition reflects the LLMIS actually serving traffic, not just being
    # created. Without this, SuccessfulCreate reports Ready in seconds while
    # the model spends minutes downloading weights and starting up.
    resource.update(
        rsp.desired.resources["llm-inference-service"],
        k8sobjv1alpha1.Object(
            spec=k8sobjv1alpha1.Spec(
                providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                    kind="ClusterProviderConfig",
                    name=ie.status.providerConfigRef.name,
                ),
                readiness=k8sobjv1alpha1.Readiness(
                    policy="DeriveFromCelQuery",
                    celQuery=(
                        'object.status.conditions.exists('
                        'c, c.type == "Ready" && c.status == "True")'
                    ),
                ),
                forProvider=k8sobjv1alpha1.ForProvider(
                    manifest={
                        "apiVersion": "serving.kserve.io/v1alpha1",
                        "kind": "LLMInferenceService",
                        "metadata": {
                            "name": llmis_name,
                            "namespace": llmis_namespace,
                        },
                        "spec": {
                            "model": {
                                "uri": f"hf://{model.spec.huggingFace.repo}" if model.spec.huggingFace else "",
                                "name": model.spec.model.name,
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

    # Compose a Backend on the control plane pointing to the remote cluster's
    # KServe gateway. ModelDeployment aggregates these into an HTTPRoute.
    if ie.status.gateway.address:
        resource.update(rsp.desired.resources["backend"], {
            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
            "kind": "Backend",
            "metadata": {"namespace": xr.metadata.namespace},
            "spec": {
                "endpoints": [{"ip": {"address": ie.status.gateway.address, "port": 80}}],
            },
        })

    # Read the Backend's Crossplane-generated name from observed state and
    # surface it in status so ModelDeployment can reference it in the HTTPRoute.
    backend_name = None
    backend_observed = req.observed.resources.get("backend")
    if backend_observed:
        backend_name = (
            resource.struct_to_dict(backend_observed.resource)
            .get("metadata", {}).get("name")
        )

    # Write status fields for consumption by compose-model-deployment.
    status = v1alpha1.Status(
        model=v1alpha1.Model(name=model.spec.model.name),
        resources=v1alpha1.Resources(gpu=v1alpha1.Gpu(count=gpus_per_replica)),
    )
    if ie.status.gateway.address:
        status.endpoint = v1alpha1.Endpoint(
            url=f"http://{ie.status.gateway.address}/{llmis_namespace}/{llmis_name}/v1",
        )
    if backend_name:
        status.routing = v1alpha1.Routing(backendName=backend_name)
    libresource.update_status(rsp.desired.composite, status)

    # Transition: first time composing the LLMInferenceService.
    llmis_exists = "llm-inference-service" in req.observed.resources
    if not llmis_exists:
        response.normal(
            rsp,
            f"Composing LLMInferenceService for {model.spec.model.name} "
            f"on {ie_name}, GPUs: {gpus_per_replica}",
        )

    # ModelAccepted requires the Object to be both observed AND synced.
    # An Object that exists but can't reach the remote cluster (e.g. because
    # the cluster is still provisioning) has Synced=False — it hasn't
    # actually been accepted by the backend.
    llmis_synced = conditions.has_condition(req, "llm-inference-service", "Synced")
    llmis_accepted = llmis_exists and llmis_synced

    # ModelReady: the LLMIS is actually serving traffic. With
    # DeriveFromCelQuery, the Object reports Ready only when the remote
    # LLMIS's Ready condition is True.
    llmis_ready = conditions.has_condition(req, "llm-inference-service", "Ready")
    backend_exists = "backend" in req.observed.resources
    # ModelAccepted: the backend accepted the model workload (Object synced).
    # ModelReady: the LLMIS is actually serving traffic.
    # RoutingReady: the Backend resource exists on the control plane.
    # Ready: all of the above (via rsp.desired.composite.ready).
    if not llmis_exists:
        accepted_reason = "Deploying"
    elif llmis_accepted:
        accepted_reason = "Accepted"
    else:
        accepted_reason = "WaitingForCluster"

    conditions.set_condition(rsp, "ModelAccepted", llmis_accepted, accepted_reason)
    conditions.set_condition(
        rsp,
        "ModelReady",
        llmis_ready,
        "Serving" if llmis_ready else "ModelStarting" if llmis_accepted else "WaitingForModel",
    )
    conditions.set_condition(
        rsp,
        "RoutingReady",
        backend_exists,
        "BackendConfigured" if backend_exists else "WaitingForGateway",
    )

    if llmis_ready:
        rsp.desired.resources["llm-inference-service"].ready = fnv1.READY_TRUE
        rsp.desired.composite.ready = fnv1.READY_TRUE
        if not conditions.was_ready(req):
            endpoint = f"http://{ie.status.gateway.address}/{llmis_namespace}/{llmis_name}/v1" if ie.status.gateway.address else "pending"
            response.normal(rsp, f"Ready, endpoint: {endpoint}")
    else:
        response.normal(rsp, "Waiting for: llm-inference-service")
