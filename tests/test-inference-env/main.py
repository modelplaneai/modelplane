from .lib import resource as libresource
from .model.ai.modelplane.inferenceenvironment import v1alpha1 as iev1alpha1
from .model.ai.modelplane.infrastructure.gkecluster import v1alpha1 as gkev1alpha1
from .model.ai.modelplane.infrastructure.kservebackend import v1alpha1 as kssv1alpha1
from .model.io.crossplane.m.kubernetes.clusterproviderconfig import (
    v1alpha1 as k8scpcv1alpha1,
)
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="inference-env-basic",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/inferenceenvironments/composition.yaml",
        xrPath="tests/test-inference-env/xr.yaml",
        xrdPath="apis/inferenceenvironments/definition.yaml",
        timeoutSeconds=120,
        validate=False,
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
                                name="demo-us-central-kubeconfig",
                                key="kubeconfig",
                            ),
                            gkev1alpha1.Secret(
                                type="GCPServiceAccountKey",
                                name="demo-us-central-sa-key",
                                key="credentials.json",
                            ),
                        ],
                    ),
                )
            ),
        ],
        assertResources=[
            # Assert the XR has status populated with providerConfigRef,
            # namespace, and GPU capacity.
            libresource.model_to_dict(
                iev1alpha1.InferenceEnvironment(
                    metadata=metav1.ObjectMeta(
                        name="demo-us-central",
                    ),
                    spec=iev1alpha1.Spec(
                        backend="KServe",
                    ),
                    status=iev1alpha1.Status(
                        providerConfigRef=iev1alpha1.ProviderConfigRef(
                            name="demo-us-central-cluster-kubeconfig",
                        ),
                        namespace="modelplane-system",
                        capacity=iev1alpha1.Capacity(
                            backend="KServe",
                            gpuPools=[
                                iev1alpha1.GpuPool(
                                    acceleratorType="nvidia-l4",
                                    memory="24Gi",
                                    count=2,
                                ),
                            ],
                        ),
                    ),
                )
            ),
            # Assert GKECluster is composed in modelplane-system.
            {
                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                "kind": "GKECluster",
                "metadata": {
                    "name": "demo-us-central",
                    "namespace": "modelplane-system",
                    "annotations": {
                        "crossplane.io/composition-resource-name": "gke-cluster",
                    },
                },
                "spec": {
                    "project": "my-gcp-project",
                    "region": "us-central1",
                },
            },
            # Assert KServeBackend is composed (gated on GKE being ready).
            libresource.model_to_dict(
                kssv1alpha1.KServeBackend(
                    metadata=metav1.ObjectMeta(
                        name="demo-us-central-kserve",
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
                                name="demo-us-central-kubeconfig",
                                key="kubeconfig",
                            ),
                            kssv1alpha1.Secret(
                                type="GCPServiceAccountKey",
                                name="demo-us-central-sa-key",
                                key="credentials.json",
                            ),
                        ],
                    ),
                )
            ),
            # Assert ClusterProviderConfig is composed for cross-namespace
            # Object creation by ModelPlacements.
            libresource.model_to_dict(
                k8scpcv1alpha1.ClusterProviderConfig(
                    metadata=metav1.ObjectMeta(
                        name="demo-us-central-cluster-kubeconfig",
                        annotations={
                            "crossplane.io/composition-resource-name": "cluster-provider-config-kubernetes",
                        },
                    ),
                    spec=k8scpcv1alpha1.Spec(
                        credentials=k8scpcv1alpha1.Credentials(
                            source="Secret",
                            secretRef=k8scpcv1alpha1.SecretRef(
                                namespace="modelplane-system",
                                name="demo-us-central-kubeconfig",
                                key="kubeconfig",
                            ),
                        ),
                        identity=k8scpcv1alpha1.Identity(
                            type="GoogleApplicationCredentials",
                            source="Secret",
                            secretRef=k8scpcv1alpha1.SecretRef(
                                namespace="modelplane-system",
                                name="demo-us-central-sa-key",
                                key="credentials.json",
                            ),
                        ),
                    ),
                )
            ),
        ],
    ),
)
