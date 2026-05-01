from .lib import resource as libresource
from .model.ai.modelplane.clustermodel import v1alpha1 as cmv1alpha1
from .model.ai.modelplane.inferenceenvironment import v1alpha1 as iev1alpha1
from .model.ai.modelplane.inferencegateway import v1alpha1 as igwv1alpha1
from .model.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-deployment-insufficient-capacity",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modeldeployments/composition.yaml",
        xrPath="tests/test-model-deployment-insufficient-capacity/xr.yaml",
        xrdPath="apis/modeldeployments/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        # extraResources is the up CLI's name for required resources.
        # These are resources the function reads but doesn't own, resolved
        # by Crossplane at runtime via response.require_resources().
        extraResources=[
            # An environment with a single 24Gi L4 GPU — not enough for
            # a 140Gi model that needs 6+ GPUs but only 1 is available.
            libresource.model_to_fixture(
                iev1alpha1.InferenceEnvironment(
                    metadata=metav1.ObjectMeta(
                        name="small-env",
                        labels={"modelplane.ai/environment": "true"},
                    ),
                    spec=iev1alpha1.Spec(cluster=iev1alpha1.Cluster(source="Existing")),
                    status=iev1alpha1.Status(
                        conditions=[
                            iev1alpha1.Condition(
                                type="Ready",
                                status="True",
                                reason="Available",
                                lastTransitionTime="2025-01-01T00:00:00Z",
                            )
                        ],
                        providerConfigRef=iev1alpha1.ProviderConfigRef(
                            name="small-cluster",
                        ),
                        gateway=iev1alpha1.Gateway(address="10.0.0.1"),
                        capacity=iev1alpha1.Capacity(
                            gpuPools=[
                                iev1alpha1.GpuPool(
                                    acceleratorType="nvidia-l4",
                                    countPerNode=1,
                                    nodes=1,
                                    memory="24Gi",
                                )
                            ],
                        ),
                    ),
                )
            ),
            # A 140Gi model — needs ceil(140/24) = 6 GPUs, but only 1
            # is available. Should not be scheduled.
            libresource.model_to_fixture(
                cmv1alpha1.ClusterModel(
                    metadata=metav1.ObjectMeta(name="llama-70b"),
                    spec=cmv1alpha1.Spec(
                        model=cmv1alpha1.Model(name="meta-llama/Llama-3-70B"),
                        source="HuggingFace",
                        huggingFace=cmv1alpha1.HuggingFace(
                            repo="meta-llama/Llama-3-70B",
                        ),
                        resources=cmv1alpha1.Resources(vram="140Gi"),
                        serving=[
                            cmv1alpha1.ServingItem(
                                name="vllm-kserve",
                                engine=cmv1alpha1.Engine(
                                    name="vLLM",
                                    image="vllm/vllm-openai:v0.7.3",
                                ),
                            ),
                        ],
                    ),
                )
            ),
            # The InferenceGateway.
            libresource.model_to_fixture(
                igwv1alpha1.InferenceGateway(
                    metadata=metav1.ObjectMeta(name="default"),
                    spec=igwv1alpha1.Spec(backend="EnvoyGateway"),
                    status=igwv1alpha1.Status(address="10.0.0.100"),
                )
            ),
        ],
        assertResources=[
            # Assert no placements — the model doesn't fit.
            libresource.model_to_dict(
                mdv1alpha1.ModelDeployment(
                    metadata=metav1.ObjectMeta(
                        name="llama-demo",
                        namespace="ml-team",
                    ),
                    spec=mdv1alpha1.Spec(
                        modelRef=mdv1alpha1.ModelRef(name="llama-70b"),
                        environments=1,
                    ),
                    status=mdv1alpha1.Status(
                        model=mdv1alpha1.Model(name="meta-llama/Llama-3-70B"),
                        placements=mdv1alpha1.Placements(total=0, ready=0),
                    ),
                )
            ),
        ],
    ),
)
