from .lib import resource as libresource
from .model.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
from .model.ai.modelplane.modelplacement import v1alpha1 as mpv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-deployment-incompatible-env",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modeldeployments/composition.yaml",
        xrPath="tests/test-model-deployment-incompatible-env/xr.yaml",
        xrdPath="apis/modeldeployments/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        # extraResources is the up CLI's name for required resources.
        # These are resources the function reads but doesn't own, resolved
        # by Crossplane at runtime via response.require_resources().
        extraResources=[
            # Compatible: KServe backend supports vLLM.
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "InferenceEnvironment",
                "metadata": {
                    "name": "compatible-env",
                    "labels": {"modelplane.ai/environment": "true"},
                },
                "spec": {"backend": "KServe"},
                "status": {
                    "providerConfigRef": {"name": "compatible-cluster"},
                    "gateway": {"address": "10.0.0.1"},
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
            # Incompatible: no backend set — engine compatibility check
            # should filter this out.
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "InferenceEnvironment",
                "metadata": {
                    "name": "incompatible-env",
                    "labels": {"modelplane.ai/environment": "true"},
                },
                "spec": {"backend": "KServe"},
                "status": {
                    "providerConfigRef": {"name": "incompatible-cluster"},
                    "gateway": {"address": "10.0.0.2"},
                    "capacity": {
                        "backend": "SomeOtherBackend",
                        "gpuPools": [{
                            "acceleratorType": "nvidia-l4",
                            "count": 2,
                            "memory": "24Gi",
                        }],
                    },
                },
            },
            # The ClusterModel requesting vLLM engine.
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "ClusterModel",
                "metadata": {"name": "qwen-0.5b-vllm"},
                "spec": {
                    "model": {"name": "Qwen/Qwen2.5-0.5B-Instruct"},
                    "source": "HuggingFace",
                    "huggingFace": {"repo": "Qwen/Qwen2.5-0.5B-Instruct"},
                    "engine": "vLLM",
                    "resources": {"vram": "2Gi"},
                },
            },
            # The InferenceGateway.
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "InferenceGateway",
                "metadata": {"name": "default"},
                "spec": {"backend": "EnvoyGateway"},
                "status": {"address": "10.0.0.100"},
            },
        ],
        assertResources=[
            # Assert only the compatible environment gets a placement.
            libresource.model_to_dict(mpv1alpha1.ModelPlacement(
                metadata=metav1.ObjectMeta(
                    annotations={
                        "crossplane.io/composition-resource-name": "placement-compatible-env",
                    },
                    name="qwen-demo-compatible-env",
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
                        name="compatible-env",
                    ),
                ),
            )),
            # Assert the XR status shows 1 placement, not 2.
            libresource.model_to_dict(mdv1alpha1.ModelDeployment(
                metadata=metav1.ObjectMeta(
                    name="qwen-demo",
                    namespace="ml-team",
                ),
                spec=mdv1alpha1.Spec(
                    modelRef=mdv1alpha1.ModelRef(name="qwen-0.5b-vllm"),
                    environments=1,
                ),
                status=mdv1alpha1.Status(
                    model=mdv1alpha1.Model(name="Qwen/Qwen2.5-0.5B-Instruct"),
                    placements=mdv1alpha1.Placements(total=1, ready=0),
                    endpoint=mdv1alpha1.Endpoint(
                        url="http://10.0.0.100/ml-team/qwen-demo/v1/chat/completions",
                    ),
                ),
            )),
        ],
    ),
)
