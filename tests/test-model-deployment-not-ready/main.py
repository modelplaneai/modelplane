"""Test that the scheduler skips clusters that aren't Ready.

The cluster has matching labels and sufficient GPU capacity, but its
Ready condition is False. The scheduler should not schedule to it.
"""

from datetime import UTC, datetime

from .lib import resource as libresource
from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from .model.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-deployment-not-ready",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modeldeployments/composition.yaml",
        xrPath="tests/test-model-deployment-not-ready/xr.yaml",
        xrdPath="apis/modeldeployments/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        extraResources=[
            # A cluster with capacity but Ready=False. The scheduler
            # should skip it entirely.
            libresource.model_to_fixture(
                icv1alpha1.InferenceCluster(
                    metadata=metav1.ObjectMeta(
                        name="not-ready-cluster",
                        labels={"modelplane.ai/cluster": "true"},
                    ),
                    spec=icv1alpha1.Spec(cluster=icv1alpha1.Cluster(source="GKE")),
                    status=icv1alpha1.Status(
                        conditions=[
                            icv1alpha1.Condition(
                                type="Ready",
                                status="False",
                                reason="BackendNotReady",
                                lastTransitionTime=datetime(2025, 1, 1, tzinfo=UTC),
                            ),
                        ],
                        providerConfigRef=icv1alpha1.ProviderConfigRef(
                            name="not-ready-cluster-config",
                        ),
                        gateway=icv1alpha1.Gateway(address="10.0.0.1"),
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
            # Assert no replicas scheduled.
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
                        replicas=mdv1alpha1.Replicas(total=0, ready=0),
                    ),
                )
            ),
        ],
    ),
)
