from .lib import resource as libresource
from .model.ai.modelplane.modelplacement import v1alpha1 as mpv1alpha1
from .model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-placement-basic",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modelplacements/composition.yaml",
        xrPath="tests/test-model-placement/xr.yaml",
        xrdPath="apis/modelplacements/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        # extraResources is the up CLI's name for required resources.
        # These are resources the function reads but doesn't own, resolved
        # by Crossplane at runtime via response.require_resources().
        extraResources=[
            # The ClusterModel referenced by the XR's spec.modelRef.
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "ClusterModel",
                "metadata": {"name": "qwen-0.5b-vllm"},
                "spec": {
                    "model": {"name": "Qwen/Qwen2.5-0.5B-Instruct"},
                    "source": "HuggingFace",
                    "huggingFace": {"repo": "Qwen/Qwen2.5-0.5B-Instruct"},
                    "engine": "vLLM",
                    "vllm": {"image": "vllm/vllm-openai:v0.7.3"},
                    "resources": {
                        "vram": "2Gi",
                        "cpu": "3",
                        "memory": "10Gi",
                    },
                },
            },
            # The InferenceEnvironment referenced by spec.inferenceEnvironmentRef.
            # Status fields are populated as if the environment is fully ready.
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "InferenceEnvironment",
                "metadata": {
                    "name": "demo-us-central",
                    "labels": {"modelplane.ai/environment": "true"},
                },
                "spec": {"backend": "KServe"},
                "status": {
                    "providerConfigRef": {"name": "demo-us-central-cluster"},
                    "gateway": {"address": "34.55.100.10"},
                    "capacity": {
                        "backend": "KServe",
                        "gpuPools": [{
                            "acceleratorType": "nvidia-l4",
                            "count": 2,
                            "memory": "24Gi",
                        }],
                    },
                },
            },
        ],
        assertResources=[
            # Assert the XR has status populated from the model and env.
            libresource.model_to_dict(mpv1alpha1.ModelPlacement(
                metadata=metav1.ObjectMeta(
                    name="qwen-demo-us-central",
                    namespace="ml-team",
                ),
                spec=mpv1alpha1.Spec(
                    modelRef=mpv1alpha1.ModelRef(
                        name="qwen-0.5b-vllm",
                    ),
                    inferenceEnvironmentRef=mpv1alpha1.InferenceEnvironmentRef(
                        name="demo-us-central",
                    ),
                ),
                status=mpv1alpha1.Status(
                    model=mpv1alpha1.Model(
                        name="Qwen/Qwen2.5-0.5B-Instruct",
                    ),
                    resources=mpv1alpha1.Resources(
                        gpu=mpv1alpha1.Gpu(count=1),
                    ),
                    endpoint=mpv1alpha1.Endpoint(
                        url="http://34.55.100.10/default/model-qwen-0-5b-vllm/v1",
                    ),
                ),
            )),
            # Assert the LLMInferenceService Object is composed on the remote
            # cluster with the correct vLLM container spec and GPU count.
            libresource.model_to_dict(k8sobjv1alpha1.Object(
                metadata=metav1.ObjectMeta(
                    annotations={
                        "crossplane.io/composition-resource-name": "llm-inference-service",
                    },
                ),
                spec=k8sobjv1alpha1.Spec(
                    providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                        kind="ClusterProviderConfig",
                        name="demo-us-central-cluster",
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
                                "name": "model-qwen-0-5b-vllm",
                                "namespace": "default",
                            },
                            "spec": {
                                "model": {
                                    "uri": "hf://Qwen/Qwen2.5-0.5B-Instruct",
                                    "name": "Qwen/Qwen2.5-0.5B-Instruct",
                                },
                                "replicas": 1,
                                "template": {
                                    "containers": [{
                                        "name": "main",
                                        "image": "vllm/vllm-openai:v0.7.3",
                                        "args": [
                                            "--served-model-name=Qwen/Qwen2.5-0.5B-Instruct",
                                        ],
                                        "securityContext": {
                                            "runAsUser": 0,
                                            "runAsNonRoot": False,
                                        },
                                        "resources": {
                                            "limits": {
                                                "nvidia.com/gpu": "1",
                                                "cpu": "3",
                                                "memory": "10Gi",
                                            },
                                            "requests": {
                                                "cpu": "1",
                                                "memory": "10Gi",
                                            },
                                        },
                                    }],
                                },
                                "router": {"gateway": {}, "route": {}},
                            },
                        },
                    ),
                ),
            )),
            # Assert the Backend is composed pointing to the remote gateway.
            {
                "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                "kind": "Backend",
                "metadata": {
                    "namespace": "ml-team",
                    "annotations": {
                        "crossplane.io/composition-resource-name": "backend",
                    },
                },
                "spec": {
                    "endpoints": [{
                        "ip": {"address": "34.55.100.10", "port": 80},
                    }],
                },
            },
        ],
    ),
)
