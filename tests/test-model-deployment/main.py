from datetime import UTC, datetime

from .lib import resource as libresource
from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
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
                        conditions=[
                            icv1alpha1.Condition(
                                type="Ready",
                                status="True",
                                reason="Available",
                                lastTransitionTime=datetime(2025, 1, 1, tzinfo=UTC),
                            ),
                        ],
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
        ],
        assertResources=[
            # Assert the XR has status populated with the replica count.
            libresource.model_to_dict(
                mdv1alpha1.ModelDeployment(
                    metadata=metav1.ObjectMeta(
                        name="qwen-demo",
                        namespace="ml-team",
                    ),
                    spec=mdv1alpha1.SpecModel(
                        replicas=1,
                        workers=mdv1alpha1.Workers(
                            topology=mdv1alpha1.Topology(tensor=1),
                            template=mdv1alpha1.Template(
                                spec=mdv1alpha1.Spec(
                                    containers=[
                                        mdv1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:v0.7.3",
                                            args=["--model=Qwen/Qwen2.5-0.5B-Instruct"],
                                        ),
                                    ],
                                ),
                            ),
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
                        name="qwen-demo-demo-us-central-7078d",
                        namespace="ml-team",
                        labels={
                            "modelplane.ai/replica": "true",
                            "modelplane.ai/deployment": "qwen-demo",
                            "modelplane.ai/cluster": "demo-us-central",
                        },
                    ),
                    spec=mrv1alpha1.SpecModel(
                        inferenceClusterRef=mrv1alpha1.InferenceClusterRef(
                            name="demo-us-central",
                        ),
                        workers=mrv1alpha1.Workers(
                            topology=mrv1alpha1.Topology(tensor=1),
                            template=mrv1alpha1.Template(
                                spec=mrv1alpha1.Spec(
                                    containers=[
                                        mrv1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:v0.7.3",
                                            args=["--model=Qwen/Qwen2.5-0.5B-Instruct"],
                                        ),
                                    ],
                                ),
                            ),
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
                        name="qwen-demo-demo-us-central-7078d",
                        namespace="ml-team",
                        labels={
                            "modelplane.ai/deployment": "qwen-demo",
                            "modelplane.ai/cluster": "demo-us-central",
                        },
                    ),
                    spec=mev1alpha1.Spec(
                        url="http://34.55.100.10/default/qwen-demo-86093/v1",
                        rewritePath="/default/qwen-demo-86093/",
                    ),
                )
            ),
        ],
    ),
)
