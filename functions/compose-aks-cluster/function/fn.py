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

"""Compose an AKS cluster with networking and node pools.

This function provisions the Azure infrastructure for an inference
environment: a resource group, a virtual network and subnet, an AKS cluster
with system and GPU node pools, and ProviderConfigs for provider-kubernetes
and provider-helm to reach the cluster.

The AKS cluster's connection secret contains a kubeconfig with an embedded
client certificate for a cluster-admin local account, so consumers need no
separate cloud identity - the ProviderConfigs reference the kubeconfig Secret
alone.

Node pool autoscaling is served by AKS itself (each pool's autoscaling
fields), and the CNI and cluster DNS are bundled with the control plane, so
no in-cluster autoscaler or addons are composed. GPU pools currently can't
scale from zero: with no live node publishing ResourceSlices, the autoscaler
can't tell that a Pending pod's DRA ResourceClaim fits the pool, so GPU
pools should keep minNodeCount at 1 or higher.

ModelCache RWX storage is served by Azure Files. AKS bundles the Azure Files
CSI driver and provisions the backing storage account on demand, so only the
modelplane-rwx-fs StorageClass is composed, pinned to that driver with SMB
mount options that emulate the symlinks model caches rely on.

InfiniBand pools need no extra Azure resource: an AKS node pool's nodes share
a VM Scale Set placement group, which lands them on one physical InfiniBand
fabric when the VM size is InfiniBand-capable and the subscription has the
AKSInfinibandSupport feature flag registered. The NVIDIA network operator is
installed as a Helm release to load the host drivers and expose RDMA devices
to pods.
"""

from typing import Literal

import grpc
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.infrastructure.akscluster import v1alpha1
from models.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1
from models.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.crossplane.m.kubernetes.providerconfig import v1alpha1 as k8spcv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from models.io.upbound.m.azure.containerservice.kubernetescluster import (
    v1beta1 as clusterv1beta1,
)
from models.io.upbound.m.azure.containerservice.kubernetesclusternodepool import (
    v1beta1 as nodepoolv1beta1,
)
from models.io.upbound.m.azure.network.subnet import v1beta1 as subnetv1beta1
from models.io.upbound.m.azure.network.virtualnetwork import v1beta1 as vnetv1beta1
from models.io.upbound.m.azure.resourcegroup import v1beta1 as rgv1beta1

# System pool injected into every AKS cluster to host control-plane
# components (Envoy Gateway, LeaderWorkerSet, cert-manager, etc.). Not part of
# the user-facing API - compose-inference-cluster only passes GPU pools. AKS
# requires every cluster to carry a default node pool, so the system pool is
# it; user pools are composed as separate User-mode pools whose lifecycle
# doesn't touch the cluster resource.
_SYSTEM_POOL_NAME = "system"
_SYSTEM_POOL_VM_SIZE = "Standard_D4s_v5"
_SYSTEM_POOL_MIN_NODE_COUNT = 1
_SYSTEM_POOL_MAX_NODE_COUNT = 2
_SYSTEM_POOL_DISK_SIZE_GB = 100

# AKS rotates the default node pool through a temporary pool when a change
# requires recreating it. The name only has to differ from the pool's own.
_SYSTEM_POOL_ROTATION_NAME = "systemtmp"

# Labels written on node pools' nodes. compose-model-deployment reads these
# labels for GPU scheduling.
_LABEL_GPU = "modelplane.ai/gpu"
_LABEL_POOL = "modelplane.ai/pool"

# Secret types written to XR status. compose-inference-cluster reads these to
# wire the kubeconfig into ProviderConfigs.
_SECRET_TYPE_KUBECONFIG = "Kubeconfig"

# Key within the connection secret the KubernetesCluster writes. The provider
# copies the cluster's raw kubeconfig (kube_config_raw) under this key.
_SECRET_KEY_KUBECONFIG = "kubeconfig"

# The fabric value that opts a pool into InfiniBand.
_FABRIC_INFINIBAND = "InfiniBand"

# Taint applied to GPU node pools so only inference workloads that tolerate
# GPUs are scheduled on them. AKS takes taints as strings.
_GPU_TAINT = "nvidia.com/gpu=true:NoSchedule"
_GPU_TAINT_KEY = "nvidia.com/gpu"

# Label and taint on CPU inference pools. The label is what the serving
# stack's dra-driver-cpu selects its nodes by; the taint keeps system
# workloads off the pool as the GPU taint does on GPU pools, and engine
# pods tolerate it (compose-model-replica).
_LABEL_CPU = "modelplane.ai/cpu"
_CPU_TAINT = "modelplane.ai/cpu=true:NoSchedule"

# Node pool gpuDriver value asking AKS to install the NVIDIA driver on GPU
# nodes. The device plugin is not installed in this path - the serving
# stack's DRA driver binds GPUs to pods, as on EKS.
_GPU_DRIVER_INSTALL = "Install"

# The service and pod CIDRs for the cluster's overlay network. Pinned so they
# never overlap the VNet (whose default 10.0.0.0/16 contains Azure's own
# service CIDR default of 10.0.0.0/16).
_NETWORK_PLUGIN = "azure"
_NETWORK_PLUGIN_MODE = "overlay"
_POD_CIDR = "10.244.0.0/16"
_SERVICE_CIDR = "10.96.0.0/16"
_DNS_SERVICE_IP = "10.96.0.10"

# Node pool management policies that exclude LateInitialize, so the nodeCount
# we seed via initProvider is applied only at creation and then left to the
# AKS autoscaler. (initProvider is a beta feature gated on enumerating
# management policies - the provider rejects it alongside the default "*".)
_ManagementPolicy = Literal["Observe", "Create", "Update", "Delete", "LateInitialize", "*"]
_NODE_POOL_MANAGEMENT: list[_ManagementPolicy] = ["Observe", "Create", "Update", "Delete"]

# The upgrade surge declared on every node pool, the system pool included. AKS
# gives a pool created without one 10%, and nothing late-initializes that into
# the spec, so the provider would try to remove AKS's value on every reconcile
# while AKS puts it back: an endless agent pool update loop. Declaring AKS's
# own default ends the loop without changing how pools upgrade.
_MAX_SURGE = "10%"

# Management policies that exclude Delete, used for resources installed on
# the workload cluster (the RWX StorageClass Object and the network operator
# Helm Release). These exist only to configure the cluster and are only ever
# deleted because the whole AKSCluster - and the cluster itself - is being
# torn down. Deleting them then means asking provider-helm /
# provider-kubernetes to reach a cluster whose kubeconfig Secret has already
# been deleted, which wedges their finalizers and hangs the composite.
# Orphaning them sidesteps that: the in-cluster resources die with the
# cluster.
_ORPHAN_MANAGEMENT: list[_ManagementPolicy] = ["Observe", "Create", "Update"]

# The NVIDIA network operator, which loads the DOCA-OFED host drivers and
# exposes the InfiniBand devices of ND-series nodes to pods through the RDMA
# shared device plugin.
_NETWORK_OPERATOR_CHART_REPO = "https://helm.ngc.nvidia.com/nvidia"
_NETWORK_OPERATOR_CHART_NAME = "network-operator"
_NETWORK_OPERATOR_CHART_VERSION = "26.4.0"
_NETWORK_OPERATOR_NAMESPACE = "network-operator"

# Name of the RWX StorageClass Modelplane composes for ModelCache.
_MANAGED_STORAGE_CLASS = "modelplane-rwx-fs"
_AZURE_FILES_CSI_DRIVER = "file.csi.azure.com"
_AZURE_FILES_SKU = "Premium_LRS"
_AZURE_FILES_MOUNT_OPTIONS = [
    "dir_mode=0777",
    "file_mode=0777",
    "uid=0",
    "gid=0",
    "mfsymlinks",
    "cache=strict",
    "actimeo=30",
    "nosharesock",
]

# The provider sets an MR's external name - the name of the resource in Azure
# - through this annotation.
_ANNOTATION_EXTERNAL_NAME = "crossplane.io/external-name"

# Azure caps an AKS cluster's DNS prefix at 54 characters.
_DNS_PREFIX_MAX_LEN = 54

# Conditions reporting which provisioning stage the cluster is in. The cluster
# is everything this XR composes except the user's node pools: resource group,
# network, the AKS cluster with its system pool as the default node pool, and
# the in-cluster network operator and StorageClass. compose-inference-cluster
# relays both conditions onto the InferenceCluster.
CONDITION_TYPE_CLUSTER_READY = "ClusterReady"
CONDITION_TYPE_NODE_POOLS_READY = "NodePoolsReady"

CONDITION_REASON_AVAILABLE = "Available"
CONDITION_REASON_PENDING = "Pending"
CONDITION_REASON_PROVISIONING = "Provisioning"
CONDITION_REASON_FAILED = "Failed"

# status.nodePools[].state values.
_POOL_STATE_PENDING = "Pending"
_POOL_STATE_PROVISIONING = "Provisioning"
_POOL_STATE_READY = "Ready"
_POOL_STATE_FAILED = "Failed"

# upjet sets this condition False, with the provider's error as its message,
# when its last create, update or delete of the external resource failed. It's
# the only failure signal read: a managed resource is also Synced=False while it
# waits for a reference to resolve, which is routine during provisioning.
_CONDITION_LAST_ASYNC_OPERATION = "LastAsyncOperation"


def _first(failures: list[str]) -> str:
    """The first failure, noting how many more there are."""
    if len(failures) == 1:
        return failures[0]
    return f"{failures[0]} (and {len(failures) - 1} more)"


def _name(meta: metav1.ObjectMeta | None) -> str:
    """The object's name, always set on resources read from the API server."""
    if meta is None or meta.name is None:
        raise ValueError("metadata.name is unexpectedly absent")
    return meta.name


def _namespace(meta: metav1.ObjectMeta | None) -> str:
    """The object's namespace, always set on namespaced resources read from the API server."""
    if meta is None or meta.namespace is None:
        raise ValueError("metadata.namespace is unexpectedly absent")
    return meta.namespace


def _kubeconfig_secret_name(xr: v1alpha1.AKSCluster) -> str:
    """Derive the kubeconfig secret name from the XR."""
    return resource.child_name(_name(xr.metadata), "kubeconfig")


def _cluster_name(xr: v1alpha1.AKSCluster) -> str:
    """The AKS cluster's name in Azure, also used for its resource group.

    The name is derived from the XR's namespace and name. AKSCluster is
    namespaced but a resource group name is subscription-global, so the XR
    name alone would let two clusters in different namespaces collide on one
    Azure resource group. child_name folds both in and appends a hash for
    uniqueness.
    """
    return resource.child_name(_namespace(xr.metadata), _name(xr.metadata), "aks")


def _dns_prefix(xr: v1alpha1.AKSCluster) -> str:
    """The AKS cluster's DNS prefix, which is its name cut to fit Azure's limit.

    child_name caps the name at 63 characters, but Azure allows a DNS prefix
    of at most 54. A name that fits is used as is. A longer name keeps its
    hash suffix and loses characters before it, so the prefix stays unique
    and ends in an alphanumeric, as Azure requires.
    """
    name = _cluster_name(xr)
    if len(name) <= _DNS_PREFIX_MAX_LEN:
        return name
    prefix, suffix = name.rsplit("-", 1)
    return f"{prefix[: _DNS_PREFIX_MAX_LEN - len(suffix) - 1].rstrip('-')}-{suffix}"


class FunctionRunner(grpcv1.FunctionRunnerServiceServicer):
    """A FunctionRunner handles gRPC RunFunctionRequests."""

    def __init__(self) -> None:
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
    def __init__(self, req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse) -> None:
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.AKSCluster(**resource.struct_to_dict(req.observed.composite.resource))

    def compose(self) -> None:
        self.compose_resource_group()
        self.compose_network()
        self.compose_cluster()
        self.compose_node_pools()
        self.compose_network_operator()
        self.compose_provider_configs()
        self.compose_storage_class()
        self.mark_readiness()
        self.write_status(self.derive_conditions())

    def _cred_kind(self) -> str:
        creds = self.xr.spec.credentials
        return creds.type if creds and creds.type else "ClusterProviderConfig"

    def _cred_name(self) -> str:
        creds = self.xr.spec.credentials
        return creds.name if creds and creds.name else "default"

    def compose_resource_group(self) -> None:
        """Compose a dedicated resource group holding every Azure resource of
        the cluster, so tearing down the AKSCluster leaves nothing behind."""
        resource.update(
            self.rsp.desired.resources["resource-group"],
            rgv1beta1.ResourceGroup(
                metadata=metav1.ObjectMeta(name=_cluster_name(self.xr)),
                spec=rgv1beta1.Spec(
                    providerConfigRef=rgv1beta1.ProviderConfigRef(
                        kind=self._cred_kind(),
                        name=self._cred_name(),
                    ),
                    forProvider=rgv1beta1.ForProvider(
                        location=self.xr.spec.location,
                    ),
                ),
            ),
        )

    def compose_network(self) -> None:
        """Compose a dedicated virtual network and node subnet for the
        cluster. Azure subnets span all Availability Zones in a region, so
        one subnet serves every node pool."""
        networking = self._networking()
        # The XRD (and the model) default both CIDRs, so they're always set;
        # the guards only narrow the optional model types.
        if networking.vnetCidr is None or networking.subnetCidr is None:
            raise ValueError("spec.networking CIDRs are unexpectedly absent")
        resource.update(
            self.rsp.desired.resources["virtual-network"],
            vnetv1beta1.VirtualNetwork(
                spec=vnetv1beta1.Spec(
                    providerConfigRef=vnetv1beta1.ProviderConfigRef(
                        kind=self._cred_kind(),
                        name=self._cred_name(),
                    ),
                    forProvider=vnetv1beta1.ForProvider(
                        location=self.xr.spec.location,
                        addressSpace=[networking.vnetCidr],
                        resourceGroupNameSelector=vnetv1beta1.ResourceGroupNameSelector(
                            matchControllerRef=True,
                        ),
                    ),
                ),
            ),
        )

        resource.update(
            self.rsp.desired.resources["subnet"],
            subnetv1beta1.Subnet(
                spec=subnetv1beta1.Spec(
                    providerConfigRef=subnetv1beta1.ProviderConfigRef(
                        kind=self._cred_kind(),
                        name=self._cred_name(),
                    ),
                    forProvider=subnetv1beta1.ForProvider(
                        addressPrefixes=[networking.subnetCidr],
                        resourceGroupNameSelector=subnetv1beta1.ResourceGroupNameSelector(
                            matchControllerRef=True,
                        ),
                        virtualNetworkNameSelector=subnetv1beta1.VirtualNetworkNameSelector(
                            matchControllerRef=True,
                        ),
                    ),
                ),
            ),
        )

    def compose_cluster(self) -> None:
        """Compose the AKS cluster with the injected system pool as its
        required default node pool.

        Local accounts stay enabled: the connection secret's kubeconfig
        embeds a client certificate for the clusterAdmin local account, and
        disabling them would leave consumers no way to authenticate without
        Azure AD."""
        cluster = clusterv1beta1.KubernetesCluster(
            metadata=metav1.ObjectMeta(name=_cluster_name(self.xr)),
            spec=clusterv1beta1.Spec(
                providerConfigRef=clusterv1beta1.ProviderConfigRef(
                    kind=self._cred_kind(),
                    name=self._cred_name(),
                ),
                forProvider=clusterv1beta1.ForProvider(
                    location=self.xr.spec.location,
                    kubernetesVersion=self.xr.spec.kubernetesVersion,
                    dnsPrefix=_dns_prefix(self.xr),
                    # The resource group AKS creates for the cluster's own
                    # resources (VM Scale Sets, load balancer, disks). Named
                    # explicitly because the default MC_<rg>_<cluster>_<region>
                    # exceeds Azure's 80-character resource group limit with
                    # our derived names.
                    nodeResourceGroup=f"{_cluster_name(self.xr)}-nodes",
                    resourceGroupNameSelector=clusterv1beta1.ResourceGroupNameSelector(
                        matchControllerRef=True,
                    ),
                    identity=clusterv1beta1.Identity(type="SystemAssigned"),
                    defaultNodePool=clusterv1beta1.DefaultNodePool(
                        name=_SYSTEM_POOL_NAME,
                        vmSize=_SYSTEM_POOL_VM_SIZE,
                        autoScalingEnabled=True,
                        minCount=_SYSTEM_POOL_MIN_NODE_COUNT,
                        maxCount=_SYSTEM_POOL_MAX_NODE_COUNT,
                        osDiskSizeGb=_SYSTEM_POOL_DISK_SIZE_GB,
                        temporaryNameForRotation=_SYSTEM_POOL_ROTATION_NAME,
                        nodeLabels={_LABEL_POOL: _SYSTEM_POOL_NAME},
                        upgradeSettings=clusterv1beta1.UpgradeSettings(maxSurge=_MAX_SURGE),
                        vnetSubnetIdSelector=clusterv1beta1.VnetSubnetIdSelector(
                            matchControllerRef=True,
                        ),
                    ),
                    networkProfile=clusterv1beta1.NetworkProfile(
                        networkPlugin=_NETWORK_PLUGIN,
                        networkPluginMode=_NETWORK_PLUGIN_MODE,
                        podCidr=_POD_CIDR,
                        serviceCidr=_SERVICE_CIDR,
                        dnsServiceIp=_DNS_SERVICE_IP,
                    ),
                ),
                writeConnectionSecretToRef=clusterv1beta1.WriteConnectionSecretToRef(
                    name=_kubeconfig_secret_name(self.xr),
                ),
            ),
        )
        resource.update(self.rsp.desired.resources["cluster"], cluster)

    def compose_node_pools(self) -> None:
        """Compose one User-mode node pool per XR pool.

        The pool's name in AKS comes from the external-name annotation: agent
        pool names are capped at 12 lowercase alphanumerics (the XRD enforces
        the pattern), which the longer generated MR name would never fit.
        """
        for pool in self.xr.spec.nodePools:
            fp = nodepoolv1beta1.ForProvider(
                kubernetesClusterIdSelector=nodepoolv1beta1.KubernetesClusterIdSelector(
                    matchControllerRef=True,
                ),
                vnetSubnetIdSelector=nodepoolv1beta1.VnetSubnetIdSelector(
                    matchControllerRef=True,
                ),
                mode="User",
                vmSize=pool.vmSize,
                osDiskSizeGb=pool.diskSizeGb,
                orchestratorVersion=self.xr.spec.kubernetesVersion,
                # min/max are ours to enforce; nodeCount is seeded via
                # initProvider below so the autoscaler can move it freely.
                autoScalingEnabled=True,
                minCount=pool.minNodeCount,
                maxCount=pool.maxNodeCount,
                nodeLabels={_LABEL_POOL: pool.name},
                upgradeSettings=nodepoolv1beta1.UpgradeSettings(maxSurge=_MAX_SURGE),
            )

            if pool.zones:
                fp.zones = [z.root for z in pool.zones]

            if pool.role == "GPU" and pool.gpu:
                fp.gpuDriver = _GPU_DRIVER_INSTALL
                fp.nodeLabels = {
                    _LABEL_GPU: pool.gpu.acceleratorType,
                    _LABEL_POOL: pool.name,
                }
                fp.nodeTaints = [_GPU_TAINT]
            elif pool.role == "CPU":
                fp.nodeLabels = {
                    _LABEL_POOL: pool.name,
                    _LABEL_CPU: "true",
                }
                fp.nodeTaints = [_CPU_TAINT]

            resource.update(
                self.rsp.desired.resources[f"nodepool-{pool.name}"],
                nodepoolv1beta1.KubernetesClusterNodePool(
                    metadata=metav1.ObjectMeta(
                        annotations={_ANNOTATION_EXTERNAL_NAME: pool.name},
                    ),
                    spec=nodepoolv1beta1.Spec(
                        providerConfigRef=nodepoolv1beta1.ProviderConfigRef(
                            kind=self._cred_kind(),
                            name=self._cred_name(),
                        ),
                        managementPolicies=_NODE_POOL_MANAGEMENT,
                        initProvider=nodepoolv1beta1.InitProvider(nodeCount=pool.nodeCount),
                        forProvider=fp,
                    ),
                ),
            )

    def compose_network_operator(self) -> None:
        """Compose the NVIDIA network operator as a Helm release when any
        pool joins the InfiniBand fabric. It loads the DOCA-OFED drivers on
        the ND-series hosts and runs the RDMA shared device plugin that
        exposes their InfiniBand devices to pods. Gate it on the cluster
        being observed: until it exists, the ProviderConfig can't reach a
        cluster and the release would just error."""
        if not any(pool.fabric == _FABRIC_INFINIBAND for pool in self.xr.spec.nodePools):
            return
        cluster_observed = "cluster" in self.req.observed.resources
        release_exists = "release-network-operator" in self.req.observed.resources
        if not (cluster_observed or release_exists):
            return

        resource.update(
            self.rsp.desired.resources["release-network-operator"],
            helmv1beta1.Release(
                metadata=metav1.ObjectMeta(namespace=_namespace(self.xr.metadata)),
                spec=helmv1beta1.Spec(
                    managementPolicies=_ORPHAN_MANAGEMENT,
                    providerConfigRef=helmv1beta1.ProviderConfigRef(
                        kind="ProviderConfig",
                        name=_kubeconfig_secret_name(self.xr),
                    ),
                    forProvider=helmv1beta1.ForProvider(
                        chart=helmv1beta1.Chart(
                            name=_NETWORK_OPERATOR_CHART_NAME,
                            repository=_NETWORK_OPERATOR_CHART_REPO,
                            version=_NETWORK_OPERATOR_CHART_VERSION,
                        ),
                        namespace=_NETWORK_OPERATOR_NAMESPACE,
                        values={
                            # Deploy a NicClusterPolicy alongside the operator
                            # with the host driver and the RDMA device plugin.
                            "deployCR": True,
                            "ofedDriver": {"deploy": True},
                            "rdmaSharedDevicePlugin": {"deploy": True},
                            # The driver and device plugin DaemonSets must run
                            # on the InfiniBand nodes, which carry the
                            # nvidia.com/gpu taint.
                            "daemonsets": {
                                "tolerations": [
                                    {
                                        "key": _GPU_TAINT_KEY,
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    },
                                ],
                            },
                        },
                    ),
                ),
            ),
        )

    def compose_storage_class(self) -> None:
        """Compose the RWX StorageClass on the workload cluster, pinned to
        the Azure Files CSI driver AKS bundles.
        Gated on the cluster being observed: until it exists, the
        ProviderConfig can't reach it. StorageClass has no Ready condition,
        so use SuccessfulCreate (DeriveFromObject would hang)."""
        cluster_observed = "cluster" in self.req.observed.resources
        storage_class_exists = "storage-class-rwx-fs" in self.req.observed.resources
        if not (cluster_observed or storage_class_exists):
            return

        manifest = {
            "apiVersion": "storage.k8s.io/v1",
            "kind": "StorageClass",
            "metadata": {"name": _MANAGED_STORAGE_CLASS},
            "provisioner": _AZURE_FILES_CSI_DRIVER,
            "parameters": {"skuName": _AZURE_FILES_SKU},
            "mountOptions": _AZURE_FILES_MOUNT_OPTIONS,
            "reclaimPolicy": "Delete",
            "allowVolumeExpansion": True,
            "volumeBindingMode": "WaitForFirstConsumer",
        }
        resource.update(
            self.rsp.desired.resources["storage-class-rwx-fs"],
            k8sobjv1alpha1.Object(
                metadata=metav1.ObjectMeta(namespace=_namespace(self.xr.metadata)),
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
        self.rsp.desired.resources["storage-class-rwx-fs"].ready = fnv1.READY_TRUE

    def compose_provider_configs(self) -> None:
        """Compose ProviderConfigs for provider-kubernetes and provider-helm
        targeting the cluster. The kubeconfig from the cluster's connection
        secret embeds a client certificate, so no separate identity is
        layered on."""
        kubeconfig_secret = _kubeconfig_secret_name(self.xr)
        resource.update(
            self.rsp.desired.resources["provider-config-kubernetes"],
            k8spcv1alpha1.ProviderConfig(
                metadata=metav1.ObjectMeta(name=kubeconfig_secret),
                spec=k8spcv1alpha1.Spec(
                    credentials=k8spcv1alpha1.Credentials(
                        source="Secret",
                        secretRef=k8spcv1alpha1.SecretRef(
                            name=kubeconfig_secret,
                            namespace=_namespace(self.xr.metadata),
                            key=_SECRET_KEY_KUBECONFIG,
                        ),
                    ),
                ),
            ),
        )

        resource.update(
            self.rsp.desired.resources["provider-config-helm"],
            helmpcv1beta1.ProviderConfig(
                metadata=metav1.ObjectMeta(name=kubeconfig_secret),
                spec=helmpcv1beta1.Spec(
                    credentials=helmpcv1beta1.Credentials(
                        source="Secret",
                        secretRef=helmpcv1beta1.SecretRef(
                            name=kubeconfig_secret,
                            namespace=_namespace(self.xr.metadata),
                            key=_SECRET_KEY_KUBECONFIG,
                        ),
                    ),
                ),
            ),
        )

    def write_status(self, node_pools: list[v1alpha1.NodePoolModel]) -> None:
        status = v1alpha1.Status(
            nodePools=node_pools,
            secrets=[
                v1alpha1.Secret(
                    type=_SECRET_TYPE_KUBECONFIG,
                    name=_kubeconfig_secret_name(self.xr),
                    key=_SECRET_KEY_KUBECONFIG,
                ),
            ],
            # The RWX StorageClass Modelplane composes for ModelCache.
            # Published immediately so ModelCache can target it; the class may
            # still be materialising on the workload cluster.
            cache=v1alpha1.Cache(storageClassName=_MANAGED_STORAGE_CLASS),
        )
        resource.update_status(self.rsp.desired.composite, status)

    def mark_readiness(self) -> None:
        """Mark composed resources ready based on their observed conditions."""
        managed_resources = [
            "resource-group",
            "virtual-network",
            "subnet",
            "cluster",
        ]
        managed_resources += [f"nodepool-{pool.name}" for pool in self.xr.spec.nodePools]
        # The network operator Helm release is only composed once the cluster
        # is observed, so only mark it ready when it's actually in desired
        # state - touching it here otherwise would re-add a resource we gated
        # out.
        if "release-network-operator" in self.rsp.desired.resources:
            managed_resources.append("release-network-operator")

        for r in managed_resources:
            if resource.get_condition(self.req.observed.resources.get(r), "Ready").status == "True":
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE

        self.rsp.desired.resources["provider-config-kubernetes"].ready = fnv1.READY_TRUE
        self.rsp.desired.resources["provider-config-helm"].ready = fnv1.READY_TRUE

    def pool_resources(self, pool: v1alpha1.NodePool) -> list[str]:
        """The composed resources that build pool. The network operator Helm
        release serves every InfiniBand pool, so it's part of the cluster
        instead."""
        return [f"nodepool-{pool.name}"]

    def _failures(self, resources: list[str]) -> list[str]:
        """Each composed resource whose provider reports its last operation
        failed, as its name and the provider's error."""
        failures = []
        for r in resources:
            c = resource.get_condition(self.req.observed.resources.get(r), _CONDITION_LAST_ASYNC_OPERATION)
            if c.status == "False":
                failures.append(f"{r}: {c.message}")
        return failures

    def _unready(self, resources: list[str]) -> list[str]:
        """Each of resources that isn't composed and marked ready."""
        desired = self.rsp.desired.resources
        return [r for r in resources if r not in desired or desired[r].ready != fnv1.READY_TRUE]

    def _node_pool_statuses(self) -> list[v1alpha1.NodePoolModel]:
        """The provisioning state of each user node pool, from its resources."""
        statuses = []
        for pool in self.xr.spec.nodePools:
            resources = self.pool_resources(pool)
            failures = self._failures(resources)
            if failures:
                statuses.append(
                    v1alpha1.NodePoolModel(name=pool.name, state=_POOL_STATE_FAILED, message=_first(failures))
                )
            elif not any(r in self.rsp.desired.resources for r in resources):
                statuses.append(v1alpha1.NodePoolModel(name=pool.name, state=_POOL_STATE_PENDING))
            elif self._unready(resources):
                statuses.append(v1alpha1.NodePoolModel(name=pool.name, state=_POOL_STATE_PROVISIONING))
            else:
                statuses.append(v1alpha1.NodePoolModel(name=pool.name, state=_POOL_STATE_READY))
        return statuses

    def derive_conditions(self) -> list[v1alpha1.NodePoolModel]:
        """Derive ClusterReady and NodePoolsReady from the readiness
        mark_readiness set, and return each node pool's state for
        status.nodePools.

        NodePoolsReady is True only once ClusterReady is, so the first False
        condition names the stage provisioning is in. A failed pool is reported
        Failed even while the cluster is still provisioning.
        """
        node_pools = self._node_pool_statuses()
        in_pools = {r for pool in self.xr.spec.nodePools for r in self.pool_resources(pool)}
        cluster = sorted(r for r in self.rsp.desired.resources if r not in in_pools)
        failures = self._failures(cluster)
        unready = self._unready(cluster)
        if failures:
            cluster_ready = resource.Condition(
                typ=CONDITION_TYPE_CLUSTER_READY,
                status="False",
                reason=CONDITION_REASON_FAILED,
                message=_first(failures),
            )
        elif unready:
            cluster_ready = resource.Condition(
                typ=CONDITION_TYPE_CLUSTER_READY,
                status="False",
                reason=CONDITION_REASON_PROVISIONING,
                message=f"{len(cluster) - len(unready)} of {len(cluster)} resources ready",
            )
        else:
            cluster_ready = resource.Condition(
                typ=CONDITION_TYPE_CLUSTER_READY, status="True", reason=CONDITION_REASON_AVAILABLE
            )

        failed = [p.name for p in node_pools if p.state == _POOL_STATE_FAILED]
        ready = [p.name for p in node_pools if p.state == _POOL_STATE_READY]
        if failed:
            pools_ready = resource.Condition(
                typ=CONDITION_TYPE_NODE_POOLS_READY,
                status="False",
                reason=CONDITION_REASON_FAILED,
                message=f"{len(failed)} of {len(node_pools)} node pools failed: {', '.join(failed)}",
            )
        elif cluster_ready.status != "True":
            pools_ready = resource.Condition(
                typ=CONDITION_TYPE_NODE_POOLS_READY,
                status="False",
                reason=CONDITION_REASON_PENDING,
                message=f"Waiting for {CONDITION_TYPE_CLUSTER_READY}",
            )
        elif len(ready) < len(node_pools):
            pools_ready = resource.Condition(
                typ=CONDITION_TYPE_NODE_POOLS_READY,
                status="False",
                reason=CONDITION_REASON_PROVISIONING,
                message=f"{len(ready)} of {len(node_pools)} node pools ready",
            )
        else:
            pools_ready = resource.Condition(
                typ=CONDITION_TYPE_NODE_POOLS_READY, status="True", reason=CONDITION_REASON_AVAILABLE
            )
        response.set_conditions(self.rsp, cluster_ready, pools_ready)
        return node_pools

    def _networking(self) -> v1alpha1.Networking:
        """Return the (defaulted) networking config from the XR."""
        return self.xr.spec.networking or v1alpha1.Networking()
