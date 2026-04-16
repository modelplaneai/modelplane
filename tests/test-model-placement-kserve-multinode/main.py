"""Test multi-node KServe placement.

A 405B model at FP16 needs ~810GiB of VRAM. An 8xH100 node has 640GiB
(8 * 80GiB). compute_gpus() should return total=11 GPUs, per_node=8,
node_count=2, multi_node=True.

The LLMInferenceService should have:
- parallelism.tensor: 11
- worker.size: 1 (2 nodes - 1 leader)
- 8 GPUs per pod (not 11)
"""

from .lib import resource as libresource
from .model.ai.modelplane.clustermodel import v1alpha1 as cmv1alpha1
from .model.ai.modelplane.inferenceenvironment import v1alpha1 as iev1alpha1
from .model.ai.modelplane.modelplacement import v1alpha1 as mpv1alpha1
from .model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-placement-kserve-multinode",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modelplacements/composition.yaml",
        xrPath="tests/test-model-placement-kserve-multinode/xr.yaml",
        xrdPath="apis/modelplacements/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        extraResources=[
            # Llama 405B: 810GiB VRAM, KServe serving profile.
            libresource.model_to_fixture(
                cmv1alpha1.ClusterModel(
                    metadata=metav1.ObjectMeta(name="llama-405b"),
                    spec=cmv1alpha1.Spec(
                        model=cmv1alpha1.Model(name="meta-llama/Llama-3.1-405B"),
                        source="HuggingFace",
                        huggingFace=cmv1alpha1.HuggingFace(
                            repo="meta-llama/Llama-3.1-405B",
                        ),
                        serving=[
                            cmv1alpha1.ServingItem(
                                name="vllm-kserve",
                                backend="KServe",
                                engine=cmv1alpha1.Engine(
                                    name="vLLM",
                                    image="vllm/vllm-openai:v0.7.3",
                                ),
                            ),
                        ],
                        resources=cmv1alpha1.Resources(
                            vram="810Gi",
                            cpu="8",
                            memory="128Gi",
                        ),
                    ),
                )
            ),
            # 3-node H100 cluster: 8 GPUs per node = 24 total.
            libresource.model_to_fixture(
                iev1alpha1.InferenceEnvironment(
                    metadata=metav1.ObjectMeta(
                        name="h100-cluster",
                        labels={"modelplane.ai/environment": "true"},
                    ),
                    spec=iev1alpha1.Spec(backend="KServe"),
                    status=iev1alpha1.Status(
                        providerConfigRef=iev1alpha1.ProviderConfigRef(
                            name="h100-cluster-kubeconfig",
                        ),
                        gateway=iev1alpha1.Gateway(address="10.0.0.1"),
                        capacity=iev1alpha1.Capacity(
                            backend="KServe",
                            gpuPools=[
                                iev1alpha1.GpuPool(
                                    acceleratorType="nvidia-h100-80gb",
                                    count=24,
                                    countPerNode=8,
                                    memory="80Gi",
                                )
                            ],
                        ),
                    ),
                )
            ),
        ],
        assertResources=[
            # Assert status shows 11 total GPUs.
            libresource.model_to_dict(
                mpv1alpha1.ModelPlacement(
                    metadata=metav1.ObjectMeta(
                        name="llama405b-h100-cluster",
                        namespace="ml-team",
                    ),
                    spec=mpv1alpha1.Spec(
                        modelRef=mpv1alpha1.ModelRef(name="llama-405b"),
                        inferenceEnvironmentRef=mpv1alpha1.InferenceEnvironmentRef(
                            name="h100-cluster",
                        ),
                    ),
                    status=mpv1alpha1.Status(
                        model=mpv1alpha1.Model(
                            name="meta-llama/Llama-3.1-405B",
                        ),
                        resources=mpv1alpha1.Resources(
                            gpu=mpv1alpha1.Gpu(count=11),
                        ),
                        endpoint=mpv1alpha1.Endpoint(
                            url="http://10.0.0.1/default/model-llama-405b/v1",
                        ),
                    ),
                )
            ),
            # Assert LLMInferenceService has multi-node configuration:
            # - parallelism.tensor = 11
            # - worker.size = 1 (2 nodes - 1 leader)
            # - 8 GPUs per pod (not 11)
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
                            name="h100-cluster-kubeconfig",
                        ),
                        readiness=k8sobjv1alpha1.Readiness(
                            policy="DeriveFromObject",
                        ),
                        forProvider=k8sobjv1alpha1.ForProvider(
                            manifest={
                                "apiVersion": "serving.kserve.io/v1alpha1",
                                "kind": "LLMInferenceService",
                                "metadata": {
                                    "name": "model-llama-405b",
                                    "namespace": "default",
                                },
                                "spec": {
                                    "model": {
                                        "uri": "hf://meta-llama/Llama-3.1-405B",
                                        "name": "meta-llama/Llama-3.1-405B",
                                    },
                                    "replicas": 1,
                                    "parallelism": {"tensor": 11},
                                    "template": {
                                        "containers": [
                                            {
                                                "name": "main",
                                                "image": "vllm/vllm-openai:v0.7.3",
                                                "args": [],
                                                "securityContext": {
                                                    "runAsUser": 0,
                                                    "runAsNonRoot": False,
                                                },
                                                "resources": {
                                                    "limits": {
                                                        "nvidia.com/gpu": "8",
                                                        "cpu": "8",
                                                        "memory": "128Gi",
                                                    },
                                                    "requests": {
                                                        "cpu": "1",
                                                        "memory": "128Gi",
                                                    },
                                                },
                                            }
                                        ],
                                    },
                                    "worker": {
                                        "size": 1,
                                        "template": {
                                            "containers": [
                                                {
                                                    "name": "main",
                                                    "image": "vllm/vllm-openai:v0.7.3",
                                                    "args": [],
                                                    "securityContext": {
                                                        "runAsUser": 0,
                                                        "runAsNonRoot": False,
                                                    },
                                                    "resources": {
                                                        "limits": {
                                                            "nvidia.com/gpu": "8",
                                                            "cpu": "8",
                                                            "memory": "128Gi",
                                                        },
                                                        "requests": {
                                                            "cpu": "1",
                                                            "memory": "128Gi",
                                                        },
                                                    },
                                                }
                                            ],
                                        },
                                    },
                                    "router": {"gateway": {}, "route": {}},
                                },
                            },
                        ),
                    ),
                )
            ),
        ],
    ),
)
