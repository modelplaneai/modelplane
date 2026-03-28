"""Compose a GKE cluster with networking, node pools, and service accounts.

This function provisions the GCP infrastructure for an inference environment:
a VPC with a subnet, a GKE cluster with system and GPU node pools, a service
account with container.admin IAM, and ProviderConfigs for provider-kubernetes
and provider-helm to reach the cluster.
"""

from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .lib import conditions, metadata, secrets
from .lib import resource as libresource
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

# Subnet secondary range names. These couple the subnet definition to
# the cluster's ipAllocationPolicy — both must use the same names.
_RANGE_PODS = "pods"
_RANGE_SERVICES = "services"


def _kubeconfig_secret_name(xr):
    """Derive the kubeconfig secret name from the XR."""
    return f"{xr.metadata.name}-kubeconfig"


def _sa_key_secret_name(xr):
    """Derive the SA key secret name from the XR."""
    return f"{xr.metadata.name}-sa-key"


class Composer:
    def __init__(self, req, rsp):
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.GKECluster(**resource.struct_to_dict(req.observed.composite.resource))

    def compose(self):
        self.compose_network()
        self.compose_subnet()
        self.compose_cluster()
        self.compose_node_pools()
        self.compose_service_account()
        self.compose_iam_binding()
        self.compose_provider_configs()
        self.write_status()
        self.mark_readiness()

    def compose_network(self):
        resource.update(
            self.rsp.desired.resources["network"],
            networkv1beta1.Network(
                spec=networkv1beta1.Spec(
                    forProvider=networkv1beta1.ForProvider(
                        project=self.xr.spec.project,
                        autoCreateSubnetworks=False,
                    ),
                ),
            ),
        )

    def compose_subnet(self):
        networking = self.xr.spec.networking or v1alpha1.Networking()

        resource.update(
            self.rsp.desired.resources["subnet"],
            subnetv1beta1.Subnetwork(
                spec=subnetv1beta1.Spec(
                    forProvider=subnetv1beta1.ForProvider(
                        project=self.xr.spec.project,
                        region=self.xr.spec.region,
                        networkSelector=subnetv1beta1.NetworkSelector(
                            matchControllerRef=True,
                        ),
                        ipCidrRange=networking.nodeCidr,
                        secondaryIpRange=[
                            subnetv1beta1.SecondaryIpRangeItem(
                                rangeName=_RANGE_PODS,
                                ipCidrRange=networking.podCidr,
                            ),
                            subnetv1beta1.SecondaryIpRangeItem(
                                rangeName=_RANGE_SERVICES,
                                ipCidrRange=networking.serviceCidr,
                            ),
                        ],
                    ),
                ),
            ),
        )

    def compose_cluster(self):
        resource.update(
            self.rsp.desired.resources["cluster"],
            clusterv1beta1.Cluster(
                spec=clusterv1beta1.Spec(
                    forProvider=clusterv1beta1.ForProvider(
                        project=self.xr.spec.project,
                        location=self.xr.spec.region,
                        deletionProtection=False,
                        removeDefaultNodePool=True,
                        initialNodeCount=1,
                        minMasterVersion=self.xr.spec.kubernetesVersion,
                        networkSelector=clusterv1beta1.NetworkSelector(
                            matchControllerRef=True,
                        ),
                        subnetworkSelector=clusterv1beta1.SubnetworkSelector(
                            matchControllerRef=True,
                        ),
                        ipAllocationPolicy=clusterv1beta1.IpAllocationPolicy(
                            clusterSecondaryRangeName=_RANGE_PODS,
                            servicesSecondaryRangeName=_RANGE_SERVICES,
                        ),
                        releaseChannel=clusterv1beta1.ReleaseChannel(
                            channel="REGULAR",
                        ),
                        workloadIdentityConfig=clusterv1beta1.WorkloadIdentityConfig(
                            workloadPool=f"{self.xr.spec.project}.svc.id.goog",
                        ),
                    ),
                    writeConnectionSecretToRef=clusterv1beta1.WriteConnectionSecretToRef(
                        name=_kubeconfig_secret_name(self.xr),
                        namespace=self.xr.metadata.namespace,
                    ),
                ),
            ),
        )

    def compose_node_pools(self):
        for pool in self.xr.spec.nodePools:
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
                        project=self.xr.spec.project,
                        location=self.xr.spec.region,
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
                self.rsp.desired.resources[f"nodepool-{pool.name}"],
                np,
            )

    def compose_service_account(self):
        resource.update(
            self.rsp.desired.resources["service-account"],
            sav1beta1.ServiceAccount(
                spec=sav1beta1.Spec(
                    forProvider=sav1beta1.ForProvider(
                        project=self.xr.spec.project,
                        displayName=f"Crossplane GKECluster {self.xr.metadata.name}",
                    ),
                ),
            ),
        )

        resource.update(
            self.rsp.desired.resources["service-account-key"],
            sakeyv1beta1.ServiceAccountKey(
                spec=sakeyv1beta1.Spec(
                    forProvider=sakeyv1beta1.ForProvider(
                        serviceAccountIdSelector=sakeyv1beta1.ServiceAccountIdSelector(
                            matchControllerRef=True,
                        ),
                    ),
                    writeConnectionSecretToRef=sakeyv1beta1.WriteConnectionSecretToRef(
                        name=_sa_key_secret_name(self.xr),
                        namespace=self.xr.metadata.namespace,
                    ),
                ),
            ),
        )

    def compose_iam_binding(self):
        """Compose the IAM binding for the service account. Gated on the SA
        email being available in observed state."""
        sa_email = self.observed_sa_email()
        if not sa_email:
            return

        resource.update(
            self.rsp.desired.resources["iam-binding"],
            iamv1beta1.ProjectIAMMember(
                spec=iamv1beta1.Spec(
                    forProvider=iamv1beta1.ForProvider(
                        project=self.xr.spec.project,
                        role="roles/container.admin",
                        member=f"serviceAccount:{sa_email}",
                    ),
                ),
            ),
        )

    def compose_provider_configs(self):
        resource.update(
            self.rsp.desired.resources["provider-config-kubernetes"],
            k8spcv1alpha1.ProviderConfig(
                metadata=metav1.ObjectMeta(name=_kubeconfig_secret_name(self.xr)),
                spec=k8spcv1alpha1.Spec(
                    credentials=k8spcv1alpha1.Credentials(
                        source="Secret",
                        secretRef=k8spcv1alpha1.SecretRef(
                            name=_kubeconfig_secret_name(self.xr),
                            namespace=self.xr.metadata.namespace,
                            key=secrets.SECRET_KEY_KUBECONFIG,
                        ),
                    ),
                    identity=k8spcv1alpha1.Identity(
                        type="GoogleApplicationCredentials",
                        source="Secret",
                        secretRef=k8spcv1alpha1.SecretRef(
                            name=_sa_key_secret_name(self.xr),
                            namespace=self.xr.metadata.namespace,
                            key=secrets.SECRET_KEY_GCP_SA,
                        ),
                    ),
                ),
            ),
        )

        resource.update(
            self.rsp.desired.resources["provider-config-helm"],
            helmpcv1beta1.ProviderConfig(
                metadata=metav1.ObjectMeta(name=_kubeconfig_secret_name(self.xr)),
                spec=helmpcv1beta1.Spec(
                    credentials=helmpcv1beta1.Credentials(
                        source="Secret",
                        secretRef=helmpcv1beta1.SecretRef(
                            name=_kubeconfig_secret_name(self.xr),
                            namespace=self.xr.metadata.namespace,
                            key=secrets.SECRET_KEY_KUBECONFIG,
                        ),
                    ),
                    identity=helmpcv1beta1.Identity(
                        type="GoogleApplicationCredentials",
                        source="Secret",
                        secretRef=helmpcv1beta1.SecretRef(
                            name=_sa_key_secret_name(self.xr),
                            namespace=self.xr.metadata.namespace,
                            key=secrets.SECRET_KEY_GCP_SA,
                        ),
                    ),
                ),
            ),
        )

    def write_status(self):
        libresource.update_status(
            self.rsp.desired.composite,
            v1alpha1.Status(
                secrets=[
                    v1alpha1.Secret(
                        type=secrets.SECRET_TYPE_KUBECONFIG,
                        name=_kubeconfig_secret_name(self.xr),
                        key=secrets.SECRET_KEY_KUBECONFIG,
                    ),
                    v1alpha1.Secret(
                        type=secrets.SECRET_TYPE_GCP_SA_KEY,
                        name=_sa_key_secret_name(self.xr),
                        key=secrets.SECRET_KEY_GCP_SA,
                    ),
                ],
            ),
        )

    def mark_readiness(self):
        """Mark composed resources as ready based on their observed conditions."""
        managed_resources = [
            "network",
            "subnet",
            "cluster",
            "service-account",
            "service-account-key",
        ]
        managed_resources += [f"nodepool-{pool.name}" for pool in self.xr.spec.nodePools]
        if self.observed_sa_email():
            managed_resources.append("iam-binding")

        for r in managed_resources:
            if conditions.has_condition(self.req, r, "Ready"):
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE

        self.rsp.desired.resources["provider-config-kubernetes"].ready = fnv1.READY_TRUE
        self.rsp.desired.resources["provider-config-helm"].ready = fnv1.READY_TRUE

    def observed_sa_email(self):
        """Read the service account email from observed state."""
        observed_sa = self.req.observed.resources.get("service-account")
        if not observed_sa:
            return None
        sa = sav1beta1.ServiceAccount.model_validate(resource.struct_to_dict(observed_sa.resource))
        if not sa.status or not sa.status.atProvider:
            return None
        return sa.status.atProvider.email


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose a GKE cluster and all supporting GCP resources."""
    Composer(req, rsp).compose()
