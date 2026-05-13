from .lib import resource as libresource
from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from .model.ai.modelplane.inferencegateway import v1alpha1 as igwv1alpha1
from .model.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
from .model.ai.modelplane.modelendpoint import v1alpha1 as mev1alpha1
from .model.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1
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
        extraResources=[
            # A ready InferenceCluster with KServe backend and one L4 pool.
            libresource.model_to_fixture(
                icv1alpha1.InferenceCluster(
                    metadata=metav1.ObjectMeta(
                        name="demo-us-central",
                        labels={"modelplane.ai/cluster": "true"},
                    ),
                    spec=icv1alpha1.Spec(cluster=icv1alpha1.Cluster(source="Existing")),
                    status=icv1alpha1.Status(
                        providerConfigRef=icv1alpha1.ProviderConfigRef(
                            name="demo-us-central-cluster",
                        ),
                        gateway=icv1alpha1.Gateway(address="34.55.100.10"),
                        capacity=icv1alpha1.Capacity(
                            gpuPools=[
                                icv1alpha1.GpuPool(
                                    acceleratorType="nvidia-l4",
                                    countPerNode=1,
                                    nodes=2,
                                    memory="24Gi",
                                )
                            ],
                        ),
                    ),
                )
            ),
            # The InferenceGateway. ModelDeployment doesn't currently read
            # its address (that's ModelService's job), but it's still a
            # required resource for environments where future composition
            # steps may need it.
            libresource.model_to_fixture(
                igwv1alpha1.InferenceGateway(
                    metadata=metav1.ObjectMeta(name="default"),
                    spec=igwv1alpha1.Spec(backend="EnvoyGateway"),
                    status=igwv1alpha1.Status(address="10.0.0.1"),
                )
            ),
        ],
        assertResources=[
            # Assert the XR has status populated with the replica count.
            libresource.model_to_dict(
                mdv1alpha1.ModelDeployment(
                    metadata=metav1.ObjectMeta(
                        name="qwen-demo",
                        namespace="ml-team",
                    ),
                    spec=mdv1alpha1.Spec(
                        replicas=1,
                        workers=mdv1alpha1.Workers(
                            topology=mdv1alpha1.Topology(
                                strategy="Tensor",
                                tensor=1,
                            ),
                            resources=mdv1alpha1.Resources(
                                cpu="3",
                                memory="10Gi",
                            ),
                        ),
                        engine=mdv1alpha1.Engine(
                            image="vllm/vllm-openai:v0.7.3",
                            args=["--model=Qwen/Qwen2.5-0.5B-Instruct"],
                        ),
                    ),
                    status=mdv1alpha1.Status(
                        replicas=mdv1alpha1.Replicas(
                            total=1,
                            ready=0,
                        ),
                    ),
                )
            ),
            # Assert a ModelReplica is composed for the matched cluster.
            libresource.model_to_dict(
                mrv1alpha1.ModelReplica(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "replica-demo-us-central",
                        },
                        name="qwen-demo-demo-us-central",
                        namespace="ml-team",
                        labels={
                            "modelplane.ai/replica": "true",
                            "modelplane.ai/deployment": "qwen-demo",
                        },
                    ),
                    spec=mrv1alpha1.Spec(
                        inferenceClusterRef=mrv1alpha1.InferenceClusterRef(
                            name="demo-us-central",
                        ),
                        workers=mrv1alpha1.Workers(
                            topology=mrv1alpha1.Topology(
                                strategy="Tensor",
                                tensor=1,
                            ),
                            resources=mrv1alpha1.Resources(
                                cpu="3",
                                memory="10Gi",
                            ),
                        ),
                        engine=mrv1alpha1.Engine(
                            image="vllm/vllm-openai:v0.7.3",
                            args=["--model=Qwen/Qwen2.5-0.5B-Instruct"],
                        ),
                    ),
                )
            ),
            # Assert a ModelEndpoint is composed for the matched cluster.
            libresource.model_to_dict(
                mev1alpha1.ModelEndpoint(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "endpoint-demo-us-central",
                        },
                        name="qwen-demo-demo-us-central",
                        namespace="ml-team",
                        labels={
                            "modelplane.ai/deployment": "qwen-demo",
                        },
                    ),
                    spec=mev1alpha1.Spec(
                        url="http://34.55.100.10/default/qwen-demo/v1",
                        api="OpenAI",
                        rewritePath="/default/qwen-demo/",
                    ),
                )
            ),
        ],
    ),
)
