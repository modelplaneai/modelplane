from .lib import resource as libresource
from .model.ai.modelplane.infrastructure.gkecluster import v1alpha1 as gkev1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest
from .model.io.upbound.m.gcp.cloudplatform.serviceaccount import v1beta1 as sav1beta1
from .model.io.upbound.m.gcp.cloudplatform.serviceaccountkey import (
    v1beta1 as sakeyv1beta1,
)
from .model.io.upbound.m.gcp.compute.network import v1beta1 as networkv1beta1
from .model.io.upbound.m.gcp.container.cluster import v1beta1 as clusterv1beta1
from .model.io.upbound.m.gcp.container.nodepool import v1beta1 as nodepoolv1beta1

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="gkecluster-basic",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/gkeclusters/composition.yaml",
        xrPath="tests/test-gkecluster/xr.yaml",
        xrdPath="apis/gkeclusters/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        assertResources=[
            libresource.model_to_dict(
                gkev1alpha1.GKECluster(
                    metadata=metav1.ObjectMeta(
                        name="gpu-us-central1",
                        namespace="gpu-us-central1",
                    ),
                    spec=gkev1alpha1.Spec(
                        project="acme-ml-platform",
                        region="us-central1",
                        nodePools=[
                            gkev1alpha1.NodePool(
                                name="system",
                                role="System",
                                machineType="e2-standard-4",
                                diskSizeGb=100,
                                nodeCount=2,
                                minNodeCount=1,
                                maxNodeCount=4,
                            ),
                            gkev1alpha1.NodePool(
                                name="gpu-l4",
                                role="GPU",
                                machineType="g2-standard-4",
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
                    status=gkev1alpha1.Status(
                        secrets=[
                            gkev1alpha1.Secret(
                                type="Kubeconfig",
                                name="gpu-us-central1-kubeconfig",
                                key="kubeconfig",
                            ),
                            gkev1alpha1.Secret(
                                type="GCPServiceAccountKey",
                                name="gpu-us-central1-sa-key",
                                key="private_key",
                            ),
                        ],
                    ),
                )
            ),
            libresource.model_to_dict(
                networkv1beta1.Network(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "network",
                        },
                    ),
                    spec=networkv1beta1.Spec(
                        forProvider=networkv1beta1.ForProvider(
                            project="acme-ml-platform",
                            autoCreateSubnetworks=False,
                        ),
                    ),
                )
            ),
            libresource.model_to_dict(
                clusterv1beta1.Cluster(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "cluster",
                        },
                    ),
                    spec=clusterv1beta1.Spec(
                        forProvider=clusterv1beta1.ForProvider(
                            location="us-central1",
                            project="acme-ml-platform",
                            deletionProtection=False,
                            removeDefaultNodePool=True,
                            initialNodeCount=1,
                            minMasterVersion="1.35",
                            networkSelector=clusterv1beta1.NetworkSelector(
                                matchControllerRef=True,
                            ),
                            subnetworkSelector=clusterv1beta1.SubnetworkSelector(
                                matchControllerRef=True,
                            ),
                            ipAllocationPolicy=clusterv1beta1.IpAllocationPolicy(
                                clusterSecondaryRangeName="pods",
                                servicesSecondaryRangeName="services",
                            ),
                            releaseChannel=clusterv1beta1.ReleaseChannel(
                                channel="REGULAR",
                            ),
                            workloadIdentityConfig=clusterv1beta1.WorkloadIdentityConfig(
                                workloadPool="acme-ml-platform.svc.id.goog",
                            ),
                        ),
                        writeConnectionSecretToRef=clusterv1beta1.WriteConnectionSecretToRef(
                            name="gpu-us-central1-kubeconfig",
                            namespace="gpu-us-central1",
                        ),
                    ),
                )
            ),
            libresource.model_to_dict(
                nodepoolv1beta1.NodePool(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "nodepool-system",
                        },
                    ),
                    spec=nodepoolv1beta1.Spec(
                        forProvider=nodepoolv1beta1.ForProvider(
                            location="us-central1",
                            project="acme-ml-platform",
                            clusterSelector=nodepoolv1beta1.ClusterSelector(
                                matchControllerRef=True,
                            ),
                            initialNodeCount=2,
                            autoscaling=nodepoolv1beta1.Autoscaling(
                                minNodeCount=1,
                                maxNodeCount=4,
                            ),
                            nodeConfig=nodepoolv1beta1.NodeConfig(
                                machineType="e2-standard-4",
                                diskSizeGb=100,
                                imageType="COS_CONTAINERD",
                                oauthScopes=[
                                    "https://www.googleapis.com/auth/cloud-platform",
                                ],
                                labels={
                                    "modelplane.ai/pool": "system",
                                },
                            ),
                        ),
                    ),
                )
            ),
            libresource.model_to_dict(
                nodepoolv1beta1.NodePool(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "nodepool-gpu-l4",
                        },
                    ),
                    spec=nodepoolv1beta1.Spec(
                        forProvider=nodepoolv1beta1.ForProvider(
                            location="us-central1",
                            project="acme-ml-platform",
                            clusterSelector=nodepoolv1beta1.ClusterSelector(
                                matchControllerRef=True,
                            ),
                            initialNodeCount=1,
                            autoscaling=nodepoolv1beta1.Autoscaling(
                                minNodeCount=0,
                                maxNodeCount=2,
                            ),
                            nodeLocations=["us-central1-a", "us-central1-c"],
                            nodeConfig=nodepoolv1beta1.NodeConfig(
                                machineType="g2-standard-4",
                                diskSizeGb=100,
                                imageType="COS_CONTAINERD",
                                oauthScopes=[
                                    "https://www.googleapis.com/auth/cloud-platform",
                                ],
                                labels={
                                    "modelplane.ai/gpu": "nvidia-l4",
                                    "modelplane.ai/pool": "gpu-l4",
                                },
                                guestAccelerator=[
                                    nodepoolv1beta1.GuestAcceleratorItem(
                                        type="nvidia-l4",
                                        count=1,
                                        gpuDriverInstallationConfig=nodepoolv1beta1.GpuDriverInstallationConfig(
                                            gpuDriverVersion="DEFAULT",
                                        ),
                                    ),
                                ],
                            ),
                        ),
                    ),
                )
            ),
            libresource.model_to_dict(
                sav1beta1.ServiceAccount(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "service-account",
                        },
                    ),
                    spec=sav1beta1.Spec(
                        forProvider=sav1beta1.ForProvider(
                            project="acme-ml-platform",
                            displayName="Crossplane GKECluster gpu-us-central1",
                        ),
                    ),
                )
            ),
            libresource.model_to_dict(
                sakeyv1beta1.ServiceAccountKey(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "service-account-key",
                        },
                    ),
                    spec=sakeyv1beta1.Spec(
                        forProvider=sakeyv1beta1.ForProvider(
                            serviceAccountIdSelector=sakeyv1beta1.ServiceAccountIdSelector(
                                matchControllerRef=True,
                            ),
                        ),
                        writeConnectionSecretToRef=sakeyv1beta1.WriteConnectionSecretToRef(
                            name="gpu-us-central1-sa-key",
                            namespace="gpu-us-central1",
                        ),
                    ),
                )
            ),
        ],
    ),
)
