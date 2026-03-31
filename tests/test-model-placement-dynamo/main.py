from .lib import resource as libresource
from .model.ai.modelplane.clustermodel import v1alpha1 as cmv1alpha1
from .model.ai.modelplane.inferenceenvironment import v1alpha1 as iev1alpha1
from .model.ai.modelplane.modelplacement import v1alpha1 as mpv1alpha1
from .model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-placement-dynamo",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modelplacements/composition.yaml",
        xrPath="tests/test-model-placement-dynamo/xr.yaml",
        xrdPath="apis/modelplacements/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        extraResources=[
            # The ClusterModel referenced by the XR's spec.modelRef.
            libresource.model_to_fixture(
                cmv1alpha1.ClusterModel(
                    metadata=metav1.ObjectMeta(name="qwen-0.5b-vllm"),
                    spec=cmv1alpha1.Spec(
                        model=cmv1alpha1.Model(name="Qwen/Qwen2.5-0.5B-Instruct"),
                        source="HuggingFace",
                        huggingFace=cmv1alpha1.HuggingFace(
                            repo="Qwen/Qwen2.5-0.5B-Instruct",
                        ),
                        engine="vLLM",
                        vllm=cmv1alpha1.Vllm(
                            image="nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.0.0",
                        ),
                        resources=cmv1alpha1.Resources(
                            vram="2Gi",
                            cpu="3",
                            memory="10Gi",
                        ),
                    ),
                )
            ),
            # The InferenceEnvironment with Dynamo backend.
            libresource.model_to_fixture(
                iev1alpha1.InferenceEnvironment(
                    metadata=metav1.ObjectMeta(
                        name="dynamo-us-central",
                        labels={"modelplane.ai/environment": "true"},
                    ),
                    spec=iev1alpha1.Spec(backend="Dynamo"),
                    status=iev1alpha1.Status(
                        providerConfigRef=iev1alpha1.ProviderConfigRef(
                            name="dynamo-us-central-cluster",
                        ),
                        gateway=iev1alpha1.Gateway(address="34.55.100.20"),
                        capacity=iev1alpha1.Capacity(
                            backend="Dynamo",
                            gpuPools=[
                                iev1alpha1.GpuPool(
                                    acceleratorType="nvidia-h100-80gb",
                                    count=16,
                                    memory="80Gi",
                                )
                            ],
                        ),
                    ),
                )
            ),
        ],
        assertResources=[
            # Assert the XR has status populated from the model and env.
            libresource.model_to_dict(
                mpv1alpha1.ModelPlacement(
                    metadata=metav1.ObjectMeta(
                        name="qwen-dynamo-us-central",
                        namespace="ml-team",
                    ),
                    spec=mpv1alpha1.Spec(
                        modelRef=mpv1alpha1.ModelRef(
                            name="qwen-0.5b-vllm",
                        ),
                        inferenceEnvironmentRef=mpv1alpha1.InferenceEnvironmentRef(
                            name="dynamo-us-central",
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
                            url="http://34.55.100.20/default/model-qwen-0-5b-vllm/v1",
                        ),
                    ),
                )
            ),
            # Assert the DynamoGraphDeployment Object is composed on the remote
            # cluster with the correct vLLM worker spec.
            libresource.model_to_dict(
                k8sobjv1alpha1.Object(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "model-serving",
                        },
                    ),
                    spec=k8sobjv1alpha1.Spec(
                        providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                            kind="ClusterProviderConfig",
                            name="dynamo-us-central-cluster",
                        ),
                        readiness=k8sobjv1alpha1.Readiness(
                            policy="DeriveFromCelQuery",
                            celQuery='object.status.conditions.exists(c, c.type == "Ready" && c.status == "True")',
                        ),
                        forProvider=k8sobjv1alpha1.ForProvider(
                            manifest={
                                "apiVersion": "nvidia.com/v1alpha1",
                                "kind": "DynamoGraphDeployment",
                                "metadata": {
                                    "name": "model-qwen-0-5b-vllm",
                                    "namespace": "default",
                                },
                                "spec": {
                                    "backendFramework": "vllm",
                                    "envs": [
                                        {
                                            "name": "LD_LIBRARY_PATH",
                                            "value": (
                                                "/usr/local/nvidia/lib64"
                                                ":/usr/local/cuda/lib64"
                                                ":/opt/vllm/tools/ep_kernels/ep_kernels_workspace/nvshmem_install/lib"
                                                ":/opt/nvidia/nvda_nixl/lib/x86_64-linux-gnu"
                                                ":/opt/nvidia/nvda_nixl/lib/x86_64-linux-gnu/plugins"
                                                ":/usr/local/ucx/lib"
                                                ":/usr/local/ucx/lib/ucx"
                                            ),
                                        },
                                    ],
                                    "services": {
                                        "Frontend": {
                                            "componentType": "frontend",
                                            "replicas": 1,
                                            "extraPodSpec": {
                                                "mainContainer": {
                                                    "image": "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.0.0",
                                                },
                                            },
                                        },
                                        "Worker": {
                                            "componentType": "worker",
                                            "replicas": 1,
                                            "resources": {
                                                "limits": {
                                                    "gpu": "1",
                                                },
                                            },
                                            "extraPodSpec": {
                                                "mainContainer": {
                                                    "image": "nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.0.0",
                                                    "workingDir": "/workspace/examples/backends/vllm",
                                                    "command": ["python3", "-m", "dynamo.vllm"],
                                                    "args": [
                                                        "--model",
                                                        "Qwen/Qwen2.5-0.5B-Instruct",
                                                    ],
                                                },
                                            },
                                        },
                                    },
                                },
                            },
                        ),
                    ),
                )
            ),
            # Assert the HTTPRoute Object is composed to route traffic from
            # the remote Envoy Gateway to the Dynamo Frontend service.
            libresource.model_to_dict(
                k8sobjv1alpha1.Object(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "dynamo-httproute",
                        },
                    ),
                    spec=k8sobjv1alpha1.Spec(
                        providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                            kind="ClusterProviderConfig",
                            name="dynamo-us-central-cluster",
                        ),
                        forProvider=k8sobjv1alpha1.ForProvider(
                            manifest={
                                "apiVersion": "gateway.networking.k8s.io/v1",
                                "kind": "HTTPRoute",
                                "metadata": {
                                    "name": "model-qwen-0-5b-vllm",
                                    "namespace": "default",
                                },
                            },
                        ),
                    ),
                )
            ),
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
                    "endpoints": [
                        {
                            "ip": {"address": "34.55.100.20", "port": 80},
                        }
                    ],
                },
            },
        ],
    ),
)
