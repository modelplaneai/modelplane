from .lib import resource as libresource
from .model.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
from .model.ai.modelplane.modelplacement import v1alpha1 as mpv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-deployment-basic",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modeldeployments/composition.yaml",
        xrPath="tests/test-model-deployment/xr.yaml",
        xrdPath="apis/modeldeployments/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        # extraResources is the up CLI's name for required resources.
        # These are resources the function reads but doesn't own, resolved
        # by Crossplane at runtime via response.require_resources().
        extraResources=[
            # A ready InferenceEnvironment with KServe backend and one L4 pool.
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
            # The ClusterModel referenced by spec.modelRef.
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
            # The InferenceGateway for the control plane routing endpoint.
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "InferenceGateway",
                "metadata": {"name": "default"},
                "spec": {"backend": "EnvoyGateway"},
                "status": {
                    "address": "10.0.0.1",
                },
            },
        ],
        assertResources=[
            # Assert the XR has status populated with model name and placement
            # count, plus the unified endpoint URL from the inference gateway.
            libresource.model_to_dict(mdv1alpha1.ModelDeployment(
                metadata=metav1.ObjectMeta(
                    name="qwen-demo",
                    namespace="ml-team",
                ),
                spec=mdv1alpha1.Spec(
                    modelRef=mdv1alpha1.ModelRef(
                        name="qwen-0.5b-vllm",
                    ),
                    environments=1,
                ),
                status=mdv1alpha1.Status(
                    model=mdv1alpha1.Model(
                        name="Qwen/Qwen2.5-0.5B-Instruct",
                    ),
                    placements=mdv1alpha1.Placements(
                        total=1,
                        ready=0,
                    ),
                    endpoint=mdv1alpha1.Endpoint(
                        url="http://10.0.0.1/ml-team/qwen-demo/v1/chat/completions",
                    ),
                ),
            )),
            # Assert a ModelPlacement is composed for the matched environment.
            libresource.model_to_dict(mpv1alpha1.ModelPlacement(
                metadata=metav1.ObjectMeta(
                    annotations={
                        "crossplane.io/composition-resource-name": "placement-demo-us-central",
                    },
                    name="qwen-demo-demo-us-central",
                    namespace="ml-team",
                    labels={
                        "modelplane.ai/placement": "true",
                        "modelplane.ai/deployment": "qwen-demo",
                    },
                ),
                spec=mpv1alpha1.Spec(
                    modelRef=mpv1alpha1.ModelRef(
                        kind="ClusterModel",
                        name="qwen-0.5b-vllm",
                    ),
                    inferenceEnvironmentRef=mpv1alpha1.InferenceEnvironmentRef(
                        name="demo-us-central",
                    ),
                ),
            )),
            # Assert an HTTPRoute is composed with the correct path rewrite.
            {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "HTTPRoute",
                "metadata": {
                    "namespace": "ml-team",
                    "annotations": {
                        "crossplane.io/composition-resource-name": "httproute",
                    },
                },
                "spec": {
                    "parentRefs": [{
                        "name": "modelplane",
                        "namespace": "modelplane-system",
                    }],
                    "rules": [{
                        "matches": [{
                            "path": {
                                "type": "PathPrefix",
                                "value": "/ml-team/qwen-demo/",
                            },
                        }],
                        "filters": [{
                            "type": "URLRewrite",
                            "urlRewrite": {
                                "path": {
                                    "type": "ReplacePrefixMatch",
                                    "replacePrefixMatch": "/default/model-qwen-0-5b-vllm/",
                                },
                            },
                        }],
                    }],
                },
            },
        ],
    ),
)
