# Copyright 2026 The Modelplane Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Compose a GKE cluster with networking, node pools, and service accounts.

This function provisions the GCP infrastructure for an inference environment:
a VPC with a subnet, a GKE cluster with system and GPU node pools, a service
account with container.admin IAM, and ProviderConfigs for provider-kubernetes
and provider-helm to reach the cluster.
"""

from typing import Literal

import grpc
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.infrastructure.gkecluster import v1alpha1
from models.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.crossplane.m.kubernetes.providerconfig import v1alpha1 as k8spcv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from models.io.upbound.m.gcp.cloudplatform.projectiammember import v1beta1 as iamv1beta1
from models.io.upbound.m.gcp.cloudplatform.projectservice import (
    v1beta1 as projectsvcv1beta1,
)
from models.io.upbound.m.gcp.cloudplatform.serviceaccount import v1beta1 as sav1beta1
from models.io.upbound.m.gcp.cloudplatform.serviceaccountkey import (
    v1beta1 as sakeyv1beta1,
)
from models.io.upbound.m.gcp.compute.network import v1beta1 as networkv1beta1
from models.io.upbound.m.gcp.compute.subnetwork import v1beta1 as subnetv1beta1
from models.io.upbound.m.gcp.container.cluster import v1beta1 as clusterv1beta1
from models.io.upbound.m.gcp.container.nodepool import v1beta1 as nodepoolv1beta1

# Subnet secondary range names. These couple the subnet definition to
# the cluster's ipAllocationPolicy — both must use the same names.
_RANGE_PODS = "pods"
_RANGE_SERVICES = "services"

# System pool injected into every GKE cluster to host control-plane
# components (Envoy Gateway, LeaderWorkerSet, cert-manager, etc.). Not part of
# the user-facing API — compose-inference-cluster only passes GPU pools.
_SYSTEM_POOL_NAME = "system"
_SYSTEM_POOL_MACHINE_TYPE = "e2-standard-4"
_SYSTEM_POOL_NODE_COUNT = 1
_SYSTEM_POOL_MIN_NODE_COUNT = 1
_SYSTEM_POOL_MAX_NODE_COUNT = 2

# Labels written on GKE node pools. compose-model-deployment reads
# these labels for GPU scheduling.
_LABEL_GPU = "modelplane.ai/gpu"
_LABEL_POOL = "modelplane.ai/pool"

# Secret types written to XR status. compose-inference-cluster reads
# these to wire the kubeconfig and SA key into ProviderConfigs.
_SECRET_TYPE_KUBECONFIG = "Kubeconfig"
_SECRET_TYPE_GCP_SA_KEY = "GCPServiceAccountKey"

# Secret keys within the Kubernetes Secrets created by GCP providers.
_SECRET_KEY_KUBECONFIG = "kubeconfig"
_SECRET_KEY_GCP_SA = "private_key"

# Identity type for GCP service account credentials.
_IDENTITY_TYPE_GCP = "GoogleApplicationCredentials"

# Annotation the provider sets on a managed resource with its external name
# (the cloud-assigned name). Read from the Network to learn its VPC name.
_ANNOTATION_EXTERNAL_NAME = "crossplane.io/external-name"

# Name of the RWX StorageClass Modelplane composes for ModelCache when the
# user doesn't bring their own. Backed by Filestore Enterprise, pinned to the
# cluster's VPC (Filestore CSI defaults to the `default` VPC otherwise).
_MANAGED_STORAGE_CLASS = "modelplane-rwx"

# Management policies that exclude Delete, used for the RWX StorageClass Object
# installed on the workload cluster. It exists only to configure the cluster and
# is only ever deleted because the whole GKECluster - and the cluster itself - is
# being torn down. Deleting it then means asking provider-kubernetes to reach a
# cluster whose kubeconfig Secret has already been deleted, which wedges its
# finalizer and hangs the composite. Orphaning it sidesteps that: the in-cluster
# StorageClass dies with the cluster. Crossplane names composed resources
# deterministically from the owner XR's UID and the composition resource name, so
# if this MR is ever deleted out of band the recomposed MR takes the same name
# and provider-kubernetes adopts the existing StorageClass rather than erroring.
_ManagementPolicy = Literal["Observe", "Create", "Update", "Delete", "LateInitialize", "*"]
_ORPHAN_MANAGEMENT: list[_ManagementPolicy] = ["Observe", "Create", "Update"]

# GKE node configuration.
_GKE_IMAGE_TYPE = "COS_CONTAINERD"
_GKE_OAUTH_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def _kubeconfig_secret_name(xr):
    """Derive the kubeconfig secret name from the XR."""
    return resource.child_name(xr.metadata.name, "kubeconfig")


def _sa_key_secret_name(xr):
    """Derive the SA key secret name from the XR."""
    return resource.child_name(xr.metadata.name, "sa-key")


class FunctionRunner(grpcv1.FunctionRunnerServiceServicer):
    """A FunctionRunner handles gRPC RunFunctionRequests."""

    def __init__(self):
        """Create a new FunctionRunner."""
        self.log = logging.get_logger()

    async def RunFunction(
        self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext | None
    ) -> fnv1.RunFunctionResponse:  # ty: ignore[invalid-method-override]  # the generated grpc servicer base is untyped
        """Run the function."""
        log = self.log.bind(tag=req.meta.tag)
        log.info("Running function")

        rsp = response.to(req)
        c = Composer(req, rsp)
        c.compose()
        return rsp


class Composer:
    def __init__(self, req, rsp):
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.GKECluster(**resource.struct_to_dict(req.observed.composite.resource))

    def compose(self):
        self.compose_network()
        self.compose_filestore_api()
        self.compose_subnet()
        self.compose_cluster()
        self.compose_node_pools()
        self.compose_service_account()
        self.compose_iam_binding()
        self.compose_provider_configs()
        self.compose_storage_class()
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

    def compose_filestore_api(self):
        """Enable file.googleapis.com so the Filestore CSI addon can provision
        RWX volumes (fresh projects have it disabled → PVCs Pending with
        SERVICE_DISABLED)."""
        resource.update(
            self.rsp.desired.resources["projectservice-filestore"],
            projectsvcv1beta1.ProjectService(
                spec=projectsvcv1beta1.Spec(
                    forProvider=projectsvcv1beta1.ForProvider(
                        project=self.xr.spec.project,
                        service="file.googleapis.com",
                        disableOnDestroy=False,
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
                        # Enable the Filestore CSI driver addon so the
                        # modelplane-rwx StorageClass has a provisioner. Without
                        # it the ModelCache RWX PVC stays Pending forever (we
                        # enable file.googleapis.com and compose the
                        # StorageClass, but nothing runs the provisioner).
                        addonsConfig=clusterv1beta1.AddonsConfig(
                            gcpFilestoreCsiDriverConfig=clusterv1beta1.GcpFilestoreCsiDriverConfig(
                                enabled=True,
                            ),
                        ),
                    ),
                    writeConnectionSecretToRef=clusterv1beta1.WriteConnectionSecretToRef(
                        name=_kubeconfig_secret_name(self.xr),
                    ),
                ),
            ),
        )

    def compose_node_pools(self):
        self._compose_system_pool()
        for pool in self.xr.spec.nodePools:
            node_config = nodepoolv1beta1.NodeConfig(
                machineType=pool.machineType,
                diskSizeGb=pool.diskSizeGb,
                imageType=_GKE_IMAGE_TYPE,
                oauthScopes=[_GKE_OAUTH_SCOPE],
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
                    _LABEL_GPU: pool.gpu.acceleratorType,
                    _LABEL_POOL: pool.name,
                }
            else:
                node_config.labels = {
                    _LABEL_POOL: pool.name,
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
                np.spec.forProvider.nodeLocations = [zone.root for zone in pool.zones]

            resource.update(
                self.rsp.desired.resources[f"nodepool-{pool.name}"],
                np,
            )

    def _compose_system_pool(self):
        """Compose the system node pool for control-plane components."""
        resource.update(
            self.rsp.desired.resources[f"nodepool-{_SYSTEM_POOL_NAME}"],
            nodepoolv1beta1.NodePool(
                spec=nodepoolv1beta1.Spec(
                    forProvider=nodepoolv1beta1.ForProvider(
                        project=self.xr.spec.project,
                        location=self.xr.spec.region,
                        clusterSelector=nodepoolv1beta1.ClusterSelector(
                            matchControllerRef=True,
                        ),
                        initialNodeCount=_SYSTEM_POOL_NODE_COUNT,
                        autoscaling=nodepoolv1beta1.Autoscaling(
                            minNodeCount=_SYSTEM_POOL_MIN_NODE_COUNT,
                            maxNodeCount=_SYSTEM_POOL_MAX_NODE_COUNT,
                        ),
                        nodeConfig=nodepoolv1beta1.NodeConfig(
                            machineType=_SYSTEM_POOL_MACHINE_TYPE,
                            imageType=_GKE_IMAGE_TYPE,
                            oauthScopes=[_GKE_OAUTH_SCOPE],
                            labels={
                                _LABEL_POOL: _SYSTEM_POOL_NAME,
                            },
                        ),
                    ),
                ),
            ),
        )

    def compose_service_account(self):
        resource.update(
            self.rsp.desired.resources["service-account"],
            sav1beta1.ServiceAccount(
                spec=sav1beta1.Spec(
                    forProvider=sav1beta1.ForProvider(
                        project=self.xr.spec.project,
                        displayName=f"Crossplane GKECluster {self.xr.metadata.name}",  # ty: ignore[unresolved-attribute]  # metadata is always set on resources read from the API server
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
                            namespace=self.xr.metadata.namespace,  # ty: ignore[unresolved-attribute, invalid-argument-type]  # metadata is always set on resources read from the API server
                            key=_SECRET_KEY_KUBECONFIG,
                        ),
                    ),
                    identity=k8spcv1alpha1.Identity(
                        type=_IDENTITY_TYPE_GCP,
                        source="Secret",
                        secretRef=k8spcv1alpha1.SecretRef(
                            name=_sa_key_secret_name(self.xr),
                            namespace=self.xr.metadata.namespace,  # ty: ignore[unresolved-attribute, invalid-argument-type]  # metadata is always set on resources read from the API server
                            key=_SECRET_KEY_GCP_SA,
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
                            namespace=self.xr.metadata.namespace,  # ty: ignore[unresolved-attribute, invalid-argument-type]  # metadata is always set on resources read from the API server
                            key=_SECRET_KEY_KUBECONFIG,
                        ),
                    ),
                    identity=helmpcv1beta1.Identity(
                        type=_IDENTITY_TYPE_GCP,
                        source="Secret",
                        secretRef=helmpcv1beta1.SecretRef(
                            name=_sa_key_secret_name(self.xr),
                            namespace=self.xr.metadata.namespace,  # ty: ignore[unresolved-attribute, invalid-argument-type]  # metadata is always set on resources read from the API server
                            key=_SECRET_KEY_GCP_SA,
                        ),
                    ),
                ),
            ),
        )

    def compose_storage_class(self):
        """Compose the Filestore RWX StorageClass on the workload cluster.
        Gated on the network name: Filestore CSI defaults to the `default` VPC
        → PVCs hang, so pin parameters.network to our VPC; the VPC name carries
        a provider-generated suffix, known only once the Network is observed.
        The Object is applied through the cluster's own provider-kubernetes
        ProviderConfig. StorageClass has no Ready condition, so use
        SuccessfulCreate (DeriveFromObject would hang)."""
        network_name = self._observed_network_name()
        if not network_name:
            return
        manifest = {
            "apiVersion": "storage.k8s.io/v1",
            "kind": "StorageClass",
            "metadata": {"name": _MANAGED_STORAGE_CLASS},
            "provisioner": "filestore.csi.storage.gke.io",
            "parameters": {"tier": "enterprise", "network": network_name},
            "volumeBindingMode": "Immediate",
            "allowVolumeExpansion": True,
        }
        resource.update(
            self.rsp.desired.resources["storage-class-rwx"],
            k8sobjv1alpha1.Object(
                metadata=metav1.ObjectMeta(namespace=self.xr.metadata.namespace),  # ty: ignore[unresolved-attribute]  # metadata is always set on resources read from the API server
                spec=k8sobjv1alpha1.Spec(
                    managementPolicies=_ORPHAN_MANAGEMENT,
                    providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                        kind="ProviderConfig",
                        name=_kubeconfig_secret_name(self.xr),
                    ),
                    readiness=k8sobjv1alpha1.Readiness(policy="SuccessfulCreate"),
                    forProvider=k8sobjv1alpha1.ForProvider(manifest=manifest),
                ),
            ),
        )
        self.rsp.desired.resources["storage-class-rwx"].ready = fnv1.READY_TRUE

    def write_status(self):
        status = v1alpha1.Status(
            secrets=[
                v1alpha1.Secret(
                    type=_SECRET_TYPE_KUBECONFIG,
                    name=_kubeconfig_secret_name(self.xr),
                    key=_SECRET_KEY_KUBECONFIG,
                ),
                v1alpha1.Secret(
                    type=_SECRET_TYPE_GCP_SA_KEY,
                    name=_sa_key_secret_name(self.xr),
                    key=_SECRET_KEY_GCP_SA,
                ),
            ],
            # The RWX StorageClass Modelplane composes for ModelCache.
            # Published immediately so ModelCache can target it; the class may
            # still be materialising on the workload cluster.
            cache=v1alpha1.Cache(storageClassName=_MANAGED_STORAGE_CLASS),
        )
        resource.update_status(self.rsp.desired.composite, status)

    def _observed_network_name(self):
        """The composed VPC network's GCP name, from the observed Network MR's
        external-name annotation (set by the provider once the network exists).
        None on early reconciles before the network is created."""
        observed = self.req.observed.resources.get("network")
        if not observed:
            return None
        network = networkv1beta1.Network.model_validate(resource.struct_to_dict(observed.resource))
        if not network.metadata or not network.metadata.annotations:
            return None
        return network.metadata.annotations.get(_ANNOTATION_EXTERNAL_NAME) or None

    def mark_readiness(self):
        """Mark composed resources as ready based on their observed conditions."""
        managed_resources = [
            "network",
            "projectservice-filestore",
            "subnet",
            "cluster",
            "service-account",
            "service-account-key",
        ]
        managed_resources.append(f"nodepool-{_SYSTEM_POOL_NAME}")
        managed_resources += [f"nodepool-{pool.name}" for pool in self.xr.spec.nodePools]
        if self.observed_sa_email():
            managed_resources.append("iam-binding")

        for r in managed_resources:
            if resource.get_condition(self.req.observed.resources.get(r), "Ready").status == "True":
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
