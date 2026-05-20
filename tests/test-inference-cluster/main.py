from .lib import resource as libresource
from .model.ai.modelplane.inferenceclass import v1alpha1 as iclv1alpha1
from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from .model.ai.modelplane.infrastructure.gkecluster import v1alpha1 as gkev1alpha1
from .model.ai.modelplane.infrastructure.kservebackend import v1alpha1 as kssv1alpha1
from .model.io.crossplane.m.kubernetes.clusterproviderconfig import (
    v1alpha1 as k8scpcv1alpha1,
)
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="inference-cluster-basic",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/inferenceclusters/composition.yaml",
        xrPath="tests/test-inference-cluster/xr.yaml",
        xrdPath="apis/inferenceclusters/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        extraResources=[
            # The InferenceClass referenced by spec.nodePools[].className.
            libresource.model_to_fixture(
                iclv1alpha1.InferenceClass(
                    metadata=metav1.ObjectMeta(name="gke-l4-1x-g2"),
                    spec=iclv1alpha1.Spec(
                        provisioning=iclv1alpha1.Provisioning(
                            provider="GKE",
                            gke=iclv1alpha1.Gke(
                                machineType="g2-standard-8",
                                diskSizeGb=100,
                                accelerator=iclv1alpha1.Accelerator(
                                    type="nvidia-l4",
                                    count=1,
                                ),
                            ),
                        ),
                        resources=iclv1alpha1.Resources(
                            gpu=iclv1alpha1.Gpu(
                                count=1,
                                memory="24Gi",
                            ),
                        ),
                    ),
                )
            ),
        ],
        # Simulate a second reconcile where the GKECluster is observed and
        # Ready with secrets. This triggers KServeBackend and
        # ClusterProviderConfig composition.
        observedResources=[
            libresource.model_to_fixture(
                gkev1alpha1.GKECluster(
                    metadata=metav1.ObjectMeta(
                        name="demo-us-central",
                        namespace="modelplane-system",
                        annotations={
                            "crossplane.io/composition-resource-name": "gke-cluster",
                        },
                    ),
                    spec=gkev1alpha1.Spec(
                        project="my-gcp-project",
                        region="us-central1",
                        nodePools=[
                            gkev1alpha1.NodePool(
                                name="system",
                                role="System",
                                machineType="e2-standard-4",
                            ),
                        ],
                    ),
                    status=gkev1alpha1.Status(
                        conditions=[
                            gkev1alpha1.Condition(
                                type="Ready",
                                status="True",
                                reason="Available",
                                lastTransitionTime="2025-01-01T00:00:00Z",
                            )
                        ],
                        secrets=[
                            gkev1alpha1.Secret(
                                type="Kubeconfig",
                                name="demo-us-central-kubeconfig-edfc0",
                                key="kubeconfig",
                            ),
                            gkev1alpha1.Secret(
                                type="GCPServiceAccountKey",
                                name="demo-us-central-sa-key-77d51",
                                key="credentials.json",
                            ),
                        ],
                    ),
                )
            ),
        ],
        assertResources=[
            # Assert the XR has status populated with providerConfigRef,
            # namespace, and GPU capacity derived from the class.
            libresource.model_to_dict(
                icv1alpha1.InferenceCluster(
                    metadata=metav1.ObjectMeta(
                        name="demo-us-central",
                    ),
                    spec=icv1alpha1.Spec(
                        cluster=icv1alpha1.Cluster(
                            source="GKE",
                            gke=icv1alpha1.Gke(
                                project="my-gcp-project",
                                region="us-central1",
                            ),
                        ),
                        nodePools=[
                            icv1alpha1.NodePool(
                                name="gpu-l4",
                                className="gke-l4-1x-g2",
                                maxNodeCount=2,
                                zones=["us-central1-a", "us-central1-c"],
                            ),
                        ],
                    ),
                    status=icv1alpha1.Status(
                        providerConfigRef=icv1alpha1.ProviderConfigRef(
                            name="demo-us-central-cluster-kubeconfig-65bed",
                        ),
                        namespace="modelplane-system",
                        capacity=icv1alpha1.Capacity(
                            gpuPools=[
                                icv1alpha1.GpuPool(
                                    acceleratorType="nvidia-l4",
                                    memory="24Gi",
                                    nodes=2,
                                    countPerNode=1,
                                ),
                            ],
                        ),
                    ),
                )
            ),
            # Assert GKECluster is composed with system pool + GPU pool
            # derived from the InferenceClass.
            libresource.model_to_dict(
                gkev1alpha1.GKECluster(
                    metadata=metav1.ObjectMeta(
                        name="demo-us-central",
                        namespace="modelplane-system",
                        annotations={
                            "crossplane.io/composition-resource-name": "gke-cluster",
                        },
                    ),
                    spec=gkev1alpha1.Spec(
                        project="my-gcp-project",
                        region="us-central1",
                        nodePools=[
                            gkev1alpha1.NodePool(
                                name="system",
                                role="System",
                                machineType="e2-standard-4",
                                nodeCount=1,
                                minNodeCount=1,
                                maxNodeCount=2,
                            ),
                            gkev1alpha1.NodePool(
                                name="gpu-l4",
                                role="GPU",
                                machineType="g2-standard-8",
                                diskSizeGb=100,
                                nodeCount=1,
                                minNodeCount=0,
                                maxNodeCount=2,
                                gpu=gkev1alpha1.Gpu(
                                    acceleratorType="nvidia-l4",
                                    acceleratorCount=1,
                                    memory="24Gi",
                                ),
                                zones=["us-central1-a", "us-central1-c"],
                            ),
                        ],
                    ),
                )
            ),
            # Assert KServeBackend is composed (gated on GKE being ready).
            libresource.model_to_dict(
                kssv1alpha1.KServeBackend(
                    metadata=metav1.ObjectMeta(
                        name="demo-us-central-kserve-1b3ff",
                        namespace="modelplane-system",
                        annotations={
                            "crossplane.io/composition-resource-name": "kserve-backend",
                        },
                    ),
                    spec=kssv1alpha1.Spec(
                        versions=kssv1alpha1.Versions(kserve="v0.16.0"),
                        secrets=[
                            kssv1alpha1.Secret(
                                type="Kubeconfig",
                                name="demo-us-central-kubeconfig-edfc0",
                                key="kubeconfig",
                            ),
                            kssv1alpha1.Secret(
                                type="GCPServiceAccountKey",
                                name="demo-us-central-sa-key-77d51",
                                key="credentials.json",
                            ),
                        ],
                    ),
                )
            ),
            # Assert ClusterProviderConfig is composed for cross-namespace
            # Object creation by ModelReplicas.
            libresource.model_to_dict(
                k8scpcv1alpha1.ClusterProviderConfig(
                    metadata=metav1.ObjectMeta(
                        name="demo-us-central-cluster-kubeconfig-65bed",
                        annotations={
                            "crossplane.io/composition-resource-name": "cluster-provider-config-kubernetes",
                        },
                    ),
                    spec=k8scpcv1alpha1.Spec(
                        credentials=k8scpcv1alpha1.Credentials(
                            source="Secret",
                            secretRef=k8scpcv1alpha1.SecretRef(
                                namespace="modelplane-system",
                                name="demo-us-central-kubeconfig-edfc0",
                                key="kubeconfig",
                            ),
                        ),
                        identity=k8scpcv1alpha1.Identity(
                            type="GoogleApplicationCredentials",
                            source="Secret",
                            secretRef=k8scpcv1alpha1.SecretRef(
                                namespace="modelplane-system",
                                name="demo-us-central-sa-key-77d51",
                                key="credentials.json",
                            ),
                        ),
                    ),
                )
            ),
        ],
    ),
)
