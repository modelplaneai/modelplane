"""Deploy a model on a single InferenceEnvironment.

This function reads the referenced ClusterModel (or Model) and
InferenceEnvironment via required resources, computes GPU count from model
VRAM vs pool VRAM, and composes a provider-kubernetes Object wrapping an
LLMInferenceService on the remote cluster.
"""

import math

from crossplane.function import request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .model.ai.modelplane.modelplacement import v1alpha1
from .model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1


def _has_condition(req: fnv1.RunFunctionRequest, name: str, cond: str) -> bool:
    """Check if an observed composed resource has a condition set to True.

    Uses the SDK's resource.get_condition which reads status.conditions from
    the protobuf Struct representation of the resource.
    """
    observed = req.observed.resources.get(name)
    if observed is None:
        return False
    return resource.get_condition(observed.resource, cond).status == "True"


def _parse_quantity(q: str) -> int:
    """Parse a Kubernetes resource quantity string to bytes.

    Supports Gi, Mi, and Ti suffixes. Returns 0 for unparseable values.
    """
    if not q:
        return 0
    q = q.strip()
    if q.endswith("Gi"):
        return int(q[:-2]) * 1024 * 1024 * 1024
    if q.endswith("Mi"):
        return int(q[:-2]) * 1024 * 1024
    if q.endswith("Ti"):
        return int(q[:-2]) * 1024 * 1024 * 1024 * 1024
    try:
        return int(q)
    except ValueError:
        return 0


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
        response.warning(rsp, "Waiting for model and environment to be resolved")
        return

    ie_status = ie.get("status", {})
    pc_name = ie_status.get("providerConfigRef", {}).get("name")
    gateway_address = ie_status.get("gateway", {}).get("address")

    if not pc_name:
        response.warning(rsp, "Waiting for environment providerConfigRef")
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
        pool_memory = _parse_quantity(pool.get("memory", "0Gi"))
        if pool_memory > 0:
            gpus_per_replica = max(1, math.ceil(
                _parse_quantity(model_vram) / pool_memory
            ))
            break

    llmis_name = xr.metadata.name
    llmis_namespace = "default"

    # Build the container spec for the vLLM model server.
    container: dict = {
        "name": "main",
        "image": image,
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
    if extra_args:
        container["args"] = extra_args

    # Compose a provider-kubernetes Object wrapping an LLMInferenceService
    # on the remote cluster.
    resource.update(
        rsp.desired.resources["llm-inference-service"],
        k8sobjv1alpha1.Object(
            spec=k8sobjv1alpha1.Spec(
                providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                    kind="ClusterProviderConfig",
                    name=pc_name,
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

    # Write status fields for consumption by compose-model-deployment.
    status: dict = {
        "model": {"name": resolved_model_name},
        "resources": {"gpu": {"count": gpus_per_replica}},
    }
    if gateway_address:
        status["endpoint"] = {
            "url": f"http://{gateway_address}/{llmis_namespace}/{llmis_name}/v1",
        }
    resource.update(rsp.desired.composite, {"status": status})

    # Set readiness based on the LLMInferenceService Object's Ready condition.
    if _has_condition(req, "llm-inference-service", "Ready"):
        rsp.desired.resources["llm-inference-service"].ready = fnv1.READY_TRUE
        rsp.desired.composite.ready = fnv1.READY_TRUE
    else:
        response.warning(rsp, "Waiting for: llm-inference-service")
