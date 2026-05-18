"""Test that scheduling is stable.

Two compatible clusters exist; cluster-b already has a replica from a
previous reconcile. With replicas=1, the scheduler should keep cluster-b
rather than rescheduling to cluster-a (which sorts first alphabetically).
"""

from datetime import UTC, datetime

from .lib import resource as libresource
from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from .model.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

REPLICA_SPEC = mrv1alpha1.SpecModel(
    inferenceClusterRef=mrv1alpha1.InferenceClusterRef(
        name="cluster-b",
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
)

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-deployment-stable-scheduling",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modeldeployments/composition.yaml",
        xrPath="tests/test-model-deployment-stable-scheduling/xr.yaml",
        xrdPath="apis/modeldeployments/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        extraResources=[
            # cluster-a: a new cluster that just came online. Sorts first
            # lexicographically, so without the stability sort it would
            # displace cluster-b's existing replica.
            libresource.model_to_fixture(
                icv1alpha1.InferenceCluster(
                    metadata=metav1.ObjectMeta(
                        name="cluster-a",
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
                            name="cluster-a-cluster",
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
            # cluster-b: already has a replica (simulated via observedResources).
            # With replicas=1 the scheduler should prefer cluster-b because
            # it already has an observed replica - even though cluster-a
            # sorts first lexicographically.
            libresource.model_to_fixture(
                icv1alpha1.InferenceCluster(
                    metadata=metav1.ObjectMeta(
                        name="cluster-b",
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
                            name="cluster-b-cluster",
                        ),
                        gateway=icv1alpha1.Gateway(address="10.0.0.2"),
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
            # The existing replica on cluster-b, also visible as a required
            # resource so the scheduler knows cluster-b already has a replica.
            libresource.model_to_fixture(
                mrv1alpha1.ModelReplica(
                    metadata=metav1.ObjectMeta(
                        name="qwen-demo-cluster-b",
                        namespace="ml-team",
                        labels={
                            "modelplane.ai/replica": "true",
                            "modelplane.ai/deployment": "qwen-demo",
                            "modelplane.ai/cluster": "cluster-b",
                        },
                    ),
                    spec=REPLICA_SPEC,
                )
            ),
        ],
        # Simulate a second reconcile where replica-cluster-b already exists.
        # The scheduler should keep cluster-b, not reschedule to cluster-a.
        observedResources=[
            libresource.model_to_dict(
                mrv1alpha1.ModelReplica(
                    metadata=metav1.ObjectMeta(
                        name="qwen-demo-cluster-b",
                        namespace="ml-team",
                        annotations={
                            "crossplane.io/composition-resource-name": "replica-cluster-b",
                        },
                        labels={
                            "modelplane.ai/replica": "true",
                            "modelplane.ai/deployment": "qwen-demo",
                            "modelplane.ai/cluster": "cluster-b",
                        },
                    ),
                    spec=REPLICA_SPEC,
                )
            ),
        ],
        assertResources=[
            # Assert the replica stays on cluster-b, not rescheduled to cluster-a.
            libresource.model_to_dict(
                mrv1alpha1.ModelReplica(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "replica-cluster-b",
                        },
                        name="qwen-demo-cluster-b",
                        namespace="ml-team",
                        labels={
                            "modelplane.ai/replica": "true",
                            "modelplane.ai/deployment": "qwen-demo",
                            "modelplane.ai/cluster": "cluster-b",
                        },
                    ),
                    spec=REPLICA_SPEC,
                )
            ),
        ],
    ),
)
