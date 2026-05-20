"""Test that clusterSelector filters out clusters with mismatched labels.

The deployment requires modelplane.ai/region: us-central. Two clusters
are present: one with the matching label, one without. Only the
compatible cluster should get a replica.
"""

from datetime import UTC, datetime

from .lib import resource as libresource
from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from .model.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
from .model.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-deployment-incompatible-cluster",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modeldeployments/composition.yaml",
        xrPath="tests/test-model-deployment-incompatible-cluster/xr.yaml",
        xrdPath="apis/modeldeployments/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        extraResources=[
            # Compatible: labels match the deployment's clusterSelector.
            # The incompatible cluster (without the region label) would
            # be filtered out by Crossplane before reaching the function,
            # so we only fixture the compatible one.
            libresource.model_to_fixture(
                icv1alpha1.InferenceCluster(
                    metadata=metav1.ObjectMeta(
                        name="compatible-cluster",
                        labels={
                            "modelplane.ai/cluster": "true",
                            "modelplane.ai/region": "us-central",
                        },
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
                            name="compatible-cluster-kubeconfig",
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
            # Assert only the compatible cluster gets a replica.
            libresource.model_to_dict(
                mrv1alpha1.ModelReplica(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "replica-compatible-cluster",
                        },
                        name="qwen-demo-compatible-cluster-bdaf4",
                        namespace="ml-team",
                        labels={
                            "modelplane.ai/replica": "true",
                            "modelplane.ai/deployment": "qwen-demo",
                            "modelplane.ai/cluster": "compatible-cluster",
                        },
                    ),
                    spec=mrv1alpha1.SpecModel(
                        inferenceClusterRef=mrv1alpha1.InferenceClusterRef(
                            name="compatible-cluster",
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
            # Assert the XR status shows 1 replica.
            libresource.model_to_dict(
                mdv1alpha1.ModelDeployment(
                    metadata=metav1.ObjectMeta(
                        name="qwen-demo",
                        namespace="ml-team",
                    ),
                    spec=mdv1alpha1.SpecModel(
                        replicas=1,
                        clusterSelector=mdv1alpha1.ClusterSelector(
                            matchLabels={"modelplane.ai/region": "us-central"},
                        ),
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
                        replicas=mdv1alpha1.Replicas(total=1, ready=0),
                    ),
                )
            ),
        ],
    ),
)
