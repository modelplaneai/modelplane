"""Compose a GKE cluster with networking, node pools, and service accounts.

This function provisions the GCP infrastructure for an inference environment:
a VPC with a subnet, a GKE cluster with system and GPU node pools, a service
account with container.admin IAM, and ProviderConfigs for provider-kubernetes
and provider-helm to reach the cluster.
"""

from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .lib import conditions
from .lib import metadata
from .lib import resource as libresource
from .lib import secrets
from .model.ai.modelplane.infrastructure.gkecluster import v1alpha1
from .model.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1
from .model.io.crossplane.m.kubernetes.providerconfig import v1alpha1 as k8spcv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.m.gcp.cloudplatform.projectiammember import v1beta1 as iamv1beta1
from .model.io.upbound.m.gcp.cloudplatform.serviceaccount import v1beta1 as sav1beta1
from .model.io.upbound.m.gcp.cloudplatform.serviceaccountkey import (
    v1beta1 as sakeyv1beta1,
)
from .model.io.upbound.m.gcp.compute.network import v1beta1 as networkv1beta1
from .model.io.upbound.m.gcp.compute.subnetwork import v1beta1 as subnetv1beta1
from .model.io.upbound.m.gcp.container.cluster import v1beta1 as clusterv1beta1
from .model.io.upbound.m.gcp.container.nodepool import v1beta1 as nodepoolv1beta1


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose a GKE cluster and all supporting GCP resources."""
    xr = v1alpha1.GKECluster(**resource.struct_to_dict(req.observed.composite.resource))
    name = xr.metadata.name
    ns = xr.metadata.namespace
    networking = xr.spec.networking or v1alpha1.Networking()

    # Subnet secondary range names. These couple the subnet definition to
    # the cluster's ipAllocationPolicy — both must use the same names.
    range_pods = "pods"
    range_services = "services"

    resource.update(
        rsp.desired.resources["network"],
        networkv1beta1.Network(
            spec=networkv1beta1.Spec(
                forProvider=networkv1beta1.ForProvider(
                    project=xr.spec.project,
                    autoCreateSubnetworks=False,
                ),
            ),
        ),
    )

    resource.update(
        rsp.desired.resources["subnet"],
        subnetv1beta1.Subnetwork(
            spec=subnetv1beta1.Spec(
                forProvider=subnetv1beta1.ForProvider(
                    project=xr.spec.project,
                    region=xr.spec.region,
                    networkSelector=subnetv1beta1.NetworkSelector(
                        matchControllerRef=True,
                    ),
                    ipCidrRange=networking.nodeCidr,
                    secondaryIpRange=[
                        subnetv1beta1.SecondaryIpRangeItem(
                            rangeName=range_pods,
                            ipCidrRange=networking.podCidr,
                        ),
                        subnetv1beta1.SecondaryIpRangeItem(
                            rangeName=range_services,
                            ipCidrRange=networking.serviceCidr,
                        ),
                    ],
                ),
            ),
        ),
    )

    kubeconfig_secret_name = f"{name}-kubeconfig"

    resource.update(
        rsp.desired.resources["cluster"],
        clusterv1beta1.Cluster(
            spec=clusterv1beta1.Spec(
                forProvider=clusterv1beta1.ForProvider(
                    project=xr.spec.project,
                    location=xr.spec.region,
                    deletionProtection=False,
                    removeDefaultNodePool=True,
                    initialNodeCount=1,
                    minMasterVersion=xr.spec.kubernetesVersion,
                    networkSelector=clusterv1beta1.NetworkSelector(
                        matchControllerRef=True,
                    ),
                    subnetworkSelector=clusterv1beta1.SubnetworkSelector(
                        matchControllerRef=True,
                    ),
                    ipAllocationPolicy=clusterv1beta1.IpAllocationPolicy(
                        clusterSecondaryRangeName=range_pods,
                        servicesSecondaryRangeName=range_services,
                    ),
                    releaseChannel=clusterv1beta1.ReleaseChannel(
                        channel="REGULAR",
                    ),
                    workloadIdentityConfig=clusterv1beta1.WorkloadIdentityConfig(
                        workloadPool=f"{xr.spec.project}.svc.id.goog",
                    ),
                ),
                writeConnectionSecretToRef=clusterv1beta1.WriteConnectionSecretToRef(
                    name=kubeconfig_secret_name,
                    namespace=ns,
                ),
            ),
        ),
    )

    for pool in xr.spec.nodePools:
        node_config = nodepoolv1beta1.NodeConfig(
            machineType=pool.machineType,
            diskSizeGb=pool.diskSizeGb,
            imageType="COS_CONTAINERD",
            oauthScopes=[
                "https://www.googleapis.com/auth/cloud-platform",
            ],
        )

        if pool.role == "GPU" and pool.gpu:
            node_config.guestAccelerator = [
                nodepoolv1beta1.GuestAcceleratorItem(
                    type=pool.gpu.acceleratorType,
                    count=pool.gpu.acceleratorCount,
                    gpuDriverInstallationConfig=nodepoolv1beta1.GpuDriverInstallationConfig(
                        gpuDriverVersion="DEFAULT",
                    ),
                ),
            ]
            node_config.labels = {
                metadata.LABEL_KEY_GPU: pool.gpu.acceleratorType,
                metadata.LABEL_KEY_POOL: pool.name,
            }
        else:
            node_config.labels = {
                metadata.LABEL_KEY_POOL: pool.name,
            }

        np = nodepoolv1beta1.NodePool(
            spec=nodepoolv1beta1.Spec(
                forProvider=nodepoolv1beta1.ForProvider(
                    project=xr.spec.project,
                    location=xr.spec.region,
                    clusterSelector=nodepoolv1beta1.ClusterSelector(
                        matchControllerRef=True,
                    ),
                    initialNodeCount=pool.nodeCount,
                    autoscaling=nodepoolv1beta1.Autoscaling(
                        minNodeCount=pool.minNodeCount,
                        maxNodeCount=pool.maxNodeCount,
                    ),
                    nodeConfig=node_config,
                ),
            ),
        )

        if pool.zones:
            np.spec.forProvider.nodeLocations = pool.zones

        resource.update(
            rsp.desired.resources[f"nodepool-{pool.name}"],
            np,
        )

    sa_key_secret_name = f"{name}-sa-key"

    resource.update(
        rsp.desired.resources["service-account"],
        sav1beta1.ServiceAccount(
            spec=sav1beta1.Spec(
                forProvider=sav1beta1.ForProvider(
                    project=xr.spec.project,
                    displayName=f"Crossplane GKECluster {name}",
                ),
            ),
        ),
    )

    resource.update(
        rsp.desired.resources["service-account-key"],
        sakeyv1beta1.ServiceAccountKey(
            spec=sakeyv1beta1.Spec(
                forProvider=sakeyv1beta1.ForProvider(
                    serviceAccountIdSelector=sakeyv1beta1.ServiceAccountIdSelector(
                        matchControllerRef=True,
                    ),
                ),
                writeConnectionSecretToRef=sakeyv1beta1.WriteConnectionSecretToRef(
                    name=sa_key_secret_name,
                    namespace=ns,
                ),
            ),
        ),
    )

    sa_email = None
    observed_sa = req.observed.resources.get("service-account")
    if observed_sa:
        sa = sav1beta1.ServiceAccount.model_validate(
            resource.struct_to_dict(observed_sa.resource)
        )
        if sa.status and sa.status.atProvider:
            sa_email = sa.status.atProvider.email

    if sa_email:
        resource.update(
            rsp.desired.resources["iam-binding"],
            iamv1beta1.ProjectIAMMember(
                spec=iamv1beta1.Spec(
                    forProvider=iamv1beta1.ForProvider(
                        project=xr.spec.project,
                        role="roles/container.admin",
                        member=f"serviceAccount:{sa_email}",
                    ),
                ),
            ),
        )

    resource.update(
        rsp.desired.resources["provider-config-kubernetes"],
        k8spcv1alpha1.ProviderConfig(
            metadata=metav1.ObjectMeta(name=kubeconfig_secret_name),
            spec=k8spcv1alpha1.Spec(
                credentials=k8spcv1alpha1.Credentials(
                    source="Secret",
                    secretRef=k8spcv1alpha1.SecretRef(
                        name=kubeconfig_secret_name,
                        namespace=ns,
                        key=secrets.SECRET_KEY_KUBECONFIG,
                    ),
                ),
                identity=k8spcv1alpha1.Identity(
                    type="GoogleApplicationCredentials",
                    source="Secret",
                    secretRef=k8spcv1alpha1.SecretRef(
                        name=sa_key_secret_name,
                        namespace=ns,
                        key=secrets.SECRET_KEY_GCP_SA,
                    ),
                ),
            ),
        ),
    )

    resource.update(
        rsp.desired.resources["provider-config-helm"],
        helmpcv1beta1.ProviderConfig(
            metadata=metav1.ObjectMeta(name=kubeconfig_secret_name),
            spec=helmpcv1beta1.Spec(
                credentials=helmpcv1beta1.Credentials(
                    source="Secret",
                    secretRef=helmpcv1beta1.SecretRef(
                        name=kubeconfig_secret_name,
                        namespace=ns,
                        key=secrets.SECRET_KEY_KUBECONFIG,
                    ),
                ),
                identity=helmpcv1beta1.Identity(
                    type="GoogleApplicationCredentials",
                    source="Secret",
                    secretRef=helmpcv1beta1.SecretRef(
                        name=sa_key_secret_name,
                        namespace=ns,
                        key=secrets.SECRET_KEY_GCP_SA,
                    ),
                ),
            ),
        ),
    )

    libresource.update_status(
        rsp.desired.composite,
        v1alpha1.Status(
            secrets=[
                v1alpha1.Secret(
                    type=secrets.SECRET_TYPE_KUBECONFIG,
                    name=kubeconfig_secret_name,
                    key=secrets.SECRET_KEY_KUBECONFIG,
                ),
                v1alpha1.Secret(
                    type=secrets.SECRET_TYPE_GCP_SA_KEY,
                    name=sa_key_secret_name,
                    key=secrets.SECRET_KEY_GCP_SA,
                ),
            ],
        ),
    )

    managed_resources = [
        "network",
        "subnet",
        "cluster",
        "service-account",
        "service-account-key",
    ]
    managed_resources += [f"nodepool-{pool.name}" for pool in xr.spec.nodePools]
    if sa_email:
        managed_resources.append("iam-binding")

    for r in managed_resources:
        if conditions.has_condition(req, r, "Ready"):
            rsp.desired.resources[r].ready = fnv1.READY_TRUE

    rsp.desired.resources["provider-config-kubernetes"].ready = fnv1.READY_TRUE
    rsp.desired.resources["provider-config-helm"].ready = fnv1.READY_TRUE
