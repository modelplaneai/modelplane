from .lib import resource as libresource
from .model.ai.modelplane.clustermodel import v1alpha1 as cmv1alpha1
from .model.ai.modelplane.inferenceenvironment import v1alpha1 as iev1alpha1
from .model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-placement-dynamo-fixed",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modelplacements/composition.yaml",
        xrPath="tests/test-model-placement-dynamo-fixed/xr.yaml",
        xrdPath="apis/modelplacements/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        extraResources=[
            libresource.model_to_fixture(
                cmv1alpha1.ClusterModel(
                    metadata=metav1.ObjectMeta(name="qwen-0.5b"),
                    spec=cmv1alpha1.Spec(
                        model=cmv1alpha1.Model(name="Qwen/Qwen2.5-0.5B-Instruct"),
                        source="HuggingFace",
                        huggingFace=cmv1alpha1.HuggingFace(
                            repo="Qwen/Qwen2.5-0.5B-Instruct",
                        ),
                        serving=[
                            cmv1alpha1.ServingItem(
                                name="vllm-dynamo",
                                backend="Dynamo",
                                engine=cmv1alpha1.Engine(
                                    name="vLLM",
                                    image="nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.0.0",
                                    args=["--model", "Qwen/Qwen2.5-0.5B-Instruct"],
                                ),
                            ),
                        ],
                        resources=cmv1alpha1.Resources(
                            vram="2Gi",
                            cpu="3",
                            memory="10Gi",
                        ),
                    ),
                )
            ),
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
                                    countPerNode=8,
                                    nodes=2,
                                    memory="80Gi",
                                )
                            ],
                        ),
                    ),
                )
            ),
        ],
        assertResources=[
            # Assert the DGD Worker has 3 replicas and no scalingAdapter.
            libresource.model_to_dict(
                k8sobjv1alpha1.Object(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "model-serving",
                        },
                    ),
                    spec=k8sobjv1alpha1.Spec(
                        forProvider=k8sobjv1alpha1.ForProvider(
                            manifest={
                                "apiVersion": "nvidia.com/v1alpha1",
                                "kind": "DynamoGraphDeployment",
                                "metadata": {
                                    "name": "model-qwen-0-5b",
                                    "namespace": "default",
                                },
                                "spec": {
                                    "services": {
                                        "Worker": {
                                            "componentType": "worker",
                                            "replicas": 3,
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
