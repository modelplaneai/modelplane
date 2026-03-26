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
from .lib import naming
from .lib import quantities
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


    # Required resources are dicts — they're external resources resolved by
    # Crossplane, not composed resources with generated Pydantic models.
    model = request.get_required_resource(req, "model")
    ie = request.get_required_resource(req, "environment")
    if model is None or ie is None:
        conditions.set_condition(rsp, "ModelAccepted", False, "WaitingForReferences")
        conditions.set_condition(rsp, "ModelReady", False, "WaitingForModel")
        conditions.set_condition(rsp, "RoutingReady", False, "WaitingForModel")
        response.normal(rsp, "Waiting for model and environment to be resolved")
        return

    ie_status = ie.get("status", {})
    pc_name = ie_status.get("providerConfigRef", {}).get("name")
    gateway_address = ie_status.get("gateway", {}).get("address")

    if not pc_name:
        conditions.set_condition(rsp, "ModelAccepted", False, "WaitingForEnvironment")
        conditions.set_condition(rsp, "ModelReady", False, "WaitingForModel")
        conditions.set_condition(rsp, "RoutingReady", False, "WaitingForModel")
        response.normal(rsp, "Waiting for environment providerConfigRef")
        return

    # Extract model configuration from the ClusterModel (or Model) spec.
    model_spec = model.get("spec", {})
    resolved_model_name = model_spec.get("model", {}).get("name", "")
    hf = model_spec.get("huggingFace", {})
    model_repo = hf.get("repo", "")
    model_uri = f"hf://{model_repo}" if model_repo else ""
    vllm_config = model_spec.get("vllm", {})
    image = vllm_config.get("image", "vllm/vllm-openai:v0.7.3")
    extra_args = vllm_config.get("extraArgs", [])
    model_vram = model_spec.get("resources", {}).get("vram", "0Gi")
    cpu = model_spec.get("resources", {}).get("cpu", "4")
    memory = model_spec.get("resources", {}).get("memory", "16Gi")

    # Compute how many GPUs the model needs by dividing model VRAM by the
    # per-GPU VRAM of the first eligible pool in the environment.
    gpu_pools = ie_status.get("capacity", {}).get("gpuPools", [])
    gpus_per_replica = 1
    for pool in gpu_pools:
        pool_memory = quantities.parse_quantity(pool.get("memory", "0Gi"))
        if pool_memory > 0:
            gpus_per_replica = max(1, math.ceil(
                quantities.parse_quantity(model_vram) / pool_memory
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
    args = [f"--served-model-name={resolved_model_name}"]
    if extra_args:
        args.extend(extra_args)

    container: dict = {
        "name": "main",
        "image": image,
        "args": args,
        "securityContext": {"runAsUser": 0, "runAsNonRoot": False},
        "resources": {
            "limits": {
                "nvidia.com/gpu": str(gpus_per_replica),
                "cpu": cpu,
                "memory": memory,
            },
            "requests": {"cpu": "1", "memory": memory},
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
                    name=pc_name,
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
                            "model": {"uri": model_uri, "name": resolved_model_name},
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
    if gateway_address:
        resource.update(rsp.desired.resources["backend"], {
            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
            "kind": "Backend",
            "metadata": {"namespace": xr.metadata.namespace},
            "spec": {
                "endpoints": [{"ip": {"address": gateway_address, "port": 80}}],
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
    status: dict = {
        "model": {"name": resolved_model_name},
        "resources": {"gpu": {"count": gpus_per_replica}},
    }
    if gateway_address:
        status["endpoint"] = {
            "url": f"http://{gateway_address}/{llmis_namespace}/{llmis_name}/v1",
        }
    if backend_name:
        status["routing"] = {"backendName": backend_name}
    resource.update(rsp.desired.composite, {"status": status})

    # Transition: first time composing the LLMInferenceService.
    llmis_exists = "llm-inference-service" in req.observed.resources
    if not llmis_exists:
        response.normal(
            rsp,
            f"Composing LLMInferenceService for {resolved_model_name} "
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
            endpoint = f"http://{gateway_address}/{llmis_namespace}/{llmis_name}/v1" if gateway_address else "pending"
            response.normal(rsp, f"Ready, endpoint: {endpoint}")
    else:
        response.normal(rsp, "Waiting for: llm-inference-service")
