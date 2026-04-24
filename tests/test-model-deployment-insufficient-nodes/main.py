"""Test that the scheduler rejects environments with insufficient nodes.

A 405B model (810GiB VRAM) needs ceil(810/80) = 11 H100 GPUs. The
environment has only 1 node with 8 GPUs (countPerNode=8, count=8).
Multi-node would require 2 nodes but only 1 is available. The
scheduler should produce 0 placements.
"""

from .lib import resource as libresource
from .model.ai.modelplane.clustermodel import v1alpha1 as cmv1alpha1
from .model.ai.modelplane.inferenceenvironment import v1alpha1 as iev1alpha1
from .model.ai.modelplane.inferencegateway import v1alpha1 as igwv1alpha1
from .model.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-deployment-insufficient-nodes",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modeldeployments/composition.yaml",
        xrPath="tests/test-model-deployment-insufficient-nodes/xr.yaml",
        xrdPath="apis/modeldeployments/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        extraResources=[
            # 1-node H100 cluster: 8 GPUs total, 8 per node. Not enough
            # nodes for a model that needs 11 GPUs (requires 2 nodes).
            libresource.model_to_fixture(
                iev1alpha1.InferenceEnvironment(
                    metadata=metav1.ObjectMeta(
                        name="small-h100",
                        labels={"modelplane.ai/environment": "true"},
                    ),
                    spec=iev1alpha1.Spec(backend="KServe"),
                    status=iev1alpha1.Status(
                        providerConfigRef=iev1alpha1.ProviderConfigRef(
                            name="small-h100-cluster",
                        ),
                        gateway=iev1alpha1.Gateway(address="10.0.0.1"),
                        capacity=iev1alpha1.Capacity(
                            backend="KServe",
                            gpuPools=[
                                iev1alpha1.GpuPool(
                                    acceleratorType="nvidia-h100-80gb",
                                    countPerNode=8,
                                    nodes=1,
                                    memory="80Gi",
                                )
                            ],
                        ),
                    ),
                )
            ),
            libresource.model_to_fixture(
                cmv1alpha1.ClusterModel(
                    metadata=metav1.ObjectMeta(name="llama-405b"),
                    spec=cmv1alpha1.Spec(
                        model=cmv1alpha1.Model(name="meta-llama/Llama-3.1-405B"),
                        source="HuggingFace",
                        huggingFace=cmv1alpha1.HuggingFace(
                            repo="meta-llama/Llama-3.1-405B",
                        ),
                        resources=cmv1alpha1.Resources(vram="810Gi"),
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
                    ),
                )
            ),
            libresource.model_to_fixture(
                igwv1alpha1.InferenceGateway(
                    metadata=metav1.ObjectMeta(name="default"),
                    spec=igwv1alpha1.Spec(backend="EnvoyGateway"),
                    status=igwv1alpha1.Status(address="10.0.0.100"),
                )
            ),
        ],
        assertResources=[
            # Assert no placements — the model needs 2 nodes but only 1
            # is available.
            libresource.model_to_dict(
                mdv1alpha1.ModelDeployment(
                    metadata=metav1.ObjectMeta(
                        name="llama405b-demo",
                        namespace="ml-team",
                    ),
                    spec=mdv1alpha1.Spec(
                        modelRef=mdv1alpha1.ModelRef(name="llama-405b"),
                        environments=1,
                    ),
                    status=mdv1alpha1.Status(
                        model=mdv1alpha1.Model(name="meta-llama/Llama-3.1-405B"),
                        placements=mdv1alpha1.Placements(total=0, ready=0),
                    ),
                )
            ),
        ],
    ),
)
