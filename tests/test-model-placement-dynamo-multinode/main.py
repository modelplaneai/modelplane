"""Test multi-node Dynamo placement.

Same 405B model (810GiB VRAM) on a 3-node H100 Dynamo cluster.
The DynamoGraphDeployment Worker should have multinode.nodeCount: 2
and gpu: "8" (per pod, not total).
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
        name="model-placement-dynamo-multinode",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modelplacements/composition.yaml",
        xrPath="tests/test-model-placement-dynamo-multinode/xr.yaml",
        xrdPath="apis/modelplacements/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        extraResources=[
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
                                name="vllm-dynamo",
                                backend="Dynamo",
                                engine=cmv1alpha1.Engine(
                                    name="vLLM",
                                    image="nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.0.0",
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
            libresource.model_to_fixture(
                iev1alpha1.InferenceEnvironment(
                    metadata=metav1.ObjectMeta(
                        name="dynamo-cluster",
                        labels={"modelplane.ai/environment": "true"},
                    ),
                    spec=iev1alpha1.Spec(backend="Dynamo"),
                    status=iev1alpha1.Status(
                        providerConfigRef=iev1alpha1.ProviderConfigRef(
                            name="dynamo-cluster-kubeconfig",
                        ),
                        gateway=iev1alpha1.Gateway(address="10.0.0.2"),
                        capacity=iev1alpha1.Capacity(
                            backend="Dynamo",
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
            libresource.model_to_dict(
                mpv1alpha1.ModelPlacement(
                    metadata=metav1.ObjectMeta(
                        name="llama405b-dynamo-cluster",
                        namespace="ml-team",
                    ),
                    spec=mpv1alpha1.Spec(
                        modelRef=mpv1alpha1.ModelRef(name="llama-405b"),
                        inferenceEnvironmentRef=mpv1alpha1.InferenceEnvironmentRef(
                            name="dynamo-cluster",
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
                            url="http://10.0.0.2/default/model-llama-405b/v1",
                        ),
                    ),
                )
            ),
            # Assert DGD Worker has multinode.nodeCount and per-pod GPU count.
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
                            name="dynamo-cluster-kubeconfig",
                        ),
                        readiness=k8sobjv1alpha1.Readiness(
                            policy="DeriveFromObject",
                        ),
                        forProvider=k8sobjv1alpha1.ForProvider(
                            manifest={
                                "apiVersion": "nvidia.com/v1alpha1",
                                "kind": "DynamoGraphDeployment",
                                "metadata": {
                                    "name": "model-llama-405b",
                                    "namespace": "default",
                                },
                                "spec": {
                                    "backendFramework": "vllm",
                                    "services": {
                                        "Worker": {
                                            "componentType": "worker",
                                            "replicas": 1,
                                            "resources": {
                                                "limits": {
                                                    "gpu": "8",
                                                },
                                            },
                                            "multinode": {"nodeCount": 2},
                                        },
                                    },
                                },
                            },
                        ),
                    ),
                )
            ),
        ],
    ),
)
