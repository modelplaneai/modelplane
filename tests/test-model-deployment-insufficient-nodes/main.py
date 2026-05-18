"""Test that the scheduler rejects clusters with insufficient nodes.

The deployment uses tensor=8, pipeline=2 topology, meaning each replica
needs 2 nodes with 8 GPUs each. The cluster only has 1
node. The scheduler should produce 0 replicas.
"""

from datetime import UTC, datetime

from .lib import resource as libresource
from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
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
            # 1-node H100 cluster: 8 GPUs per node. The replica's topology
            # asks for pipeline=2, which requires 2 nodes. Only 1 is
            # available, so no replica should be scheduled.
            libresource.model_to_fixture(
                icv1alpha1.InferenceCluster(
                    metadata=metav1.ObjectMeta(
                        name="small-h100",
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
                            name="small-h100-cluster",
                        ),
                        gateway=icv1alpha1.Gateway(address="10.0.0.1"),
                        capacity=icv1alpha1.Capacity(
                            gpuPools=[
                                icv1alpha1.GpuPool(
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
        ],
        assertResources=[
            # Assert no replicas - the topology doesn't fit.
            libresource.model_to_dict(
                mdv1alpha1.ModelDeployment(
                    metadata=metav1.ObjectMeta(
                        name="llama405b-demo",
                        namespace="ml-team",
                    ),
                    spec=mdv1alpha1.SpecModel(
                        replicas=1,
                        workers=mdv1alpha1.Workers(
                            topology=mdv1alpha1.Topology(tensor=8, pipeline=2),
                            template=mdv1alpha1.Template(
                                spec=mdv1alpha1.Spec(
                                    containers=[
                                        mdv1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:v0.7.3",
                                            args=["--model=meta-llama/Llama-3.1-405B"],
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
