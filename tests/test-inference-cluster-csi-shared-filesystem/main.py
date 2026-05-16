"""Test InferenceCluster with SharedFilesystem CSI capability.

When the user opts into SharedFilesystem via spec.storage.csiDrivers,
two things should happen on a reconcile after the underlying GKE
cluster is Ready and has observed the network name:

1. The composed GKECluster XR's spec.addons.gcpFilestoreCsiDriver
   is True, so the GKE composition function enables the in-cluster
   Filestore CSI driver addon.
2. compose_gke_storage_classes composes an Object MR wrapping a
   StorageClass on the workload cluster with provisioner =
   filestore.csi.storage.gke.io and parameters.network pinned to
   the observed VPC name from GKECluster.status.network.name.
   Without parameters.network the CSI driver provisions on GCP's
   `default` VPC and the Filestore is unreachable from cluster
   nodes — which is what motivates this code path.

The composed Object MR must live in `modelplane-system` namespace.
Object is namespaced and the InferenceCluster XR is cluster-scoped,
so the namespace has to be set explicitly on the composed resource.
"""

from .lib import resource as libresource
from .model.ai.modelplane.inferenceclass import v1alpha1 as iclv1alpha1
from .model.ai.modelplane.infrastructure.gkecluster import v1alpha1 as gkev1alpha1
from .model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

_OBSERVED_VPC_NAME = "demo-us-central-12715c9eefc1"

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="inference-cluster-csi-shared-filesystem",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/inferenceclusters/composition.yaml",
        xrPath="tests/test-inference-cluster-csi-shared-filesystem/xr.yaml",
        xrdPath="apis/inferenceclusters/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        extraResources=[
            libresource.model_to_fixture(
                iclv1alpha1.InferenceClass(
                    metadata=metav1.ObjectMeta(name="gke-t4-1x-n1"),
                    spec=iclv1alpha1.Spec(
                        provisioning=iclv1alpha1.Provisioning(
                            provider="GKE",
                            gke=iclv1alpha1.Gke(
                                machineType="n1-standard-4",
                                diskSizeGb=100,
                                accelerator=iclv1alpha1.Accelerator(
                                    type="nvidia-tesla-t4",
                                    count=1,
                                ),
                            ),
                        ),
                        resources=iclv1alpha1.Resources(
                            gpu=iclv1alpha1.Gpu(
                                count=1,
                                memory="16Gi",
                            ),
                        ),
                    ),
                )
            ),
        ],
        # Simulate a second-pass reconcile where the underlying GKECluster
        # XR is Ready and has surfaced status.network.name. That status
        # field is what compose_gke_storage_classes reads to wire the
        # parameters.network field on the workload-cluster StorageClass.
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
                        network=gkev1alpha1.Network(name=_OBSERVED_VPC_NAME),
                    ),
                )
            ),
        ],
        assertResources=[
            # Assert the Object MR wrapping the workload-cluster
            # StorageClass is composed with namespace set and the
            # observed VPC pinned in parameters.network.
            libresource.model_to_dict(
                k8sobjv1alpha1.Object(
                    metadata=metav1.ObjectMeta(
                        namespace="modelplane-system",
                        annotations={
                            "crossplane.io/composition-resource-name": "storage-class-rwx",
                        },
                    ),
                    spec=k8sobjv1alpha1.Spec(
                        providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                            kind="ClusterProviderConfig",
                            name="demo-us-central-cluster-kubeconfig",
                        ),
                        readiness=k8sobjv1alpha1.Readiness(
                            policy="DeriveFromObject",
                        ),
                        forProvider=k8sobjv1alpha1.ForProvider(
                            manifest={
                                "apiVersion": "storage.k8s.io/v1",
                                "kind": "StorageClass",
                                "metadata": {"name": "modelplane-rwx"},
                                "provisioner": "filestore.csi.storage.gke.io",
                                "parameters": {
                                    "tier": "standard",
                                    "network": _OBSERVED_VPC_NAME,
                                },
                                "reclaimPolicy": "Delete",
                                "volumeBindingMode": "WaitForFirstConsumer",
                                "allowVolumeExpansion": True,
                            },
                        ),
                    ),
                )
            ),
        ],
    ),
)
