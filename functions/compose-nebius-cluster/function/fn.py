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

"""Compose a Nebius mk8s cluster with networking and node groups.

This function provisions the Nebius infrastructure for an inference
environment: a VPC network and subnet, an mk8s cluster with system and GPU
node groups, GPU clusters for InfiniBand fabrics, and ProviderConfigs for
provider-kubernetes and provider-helm to reach the cluster.

The mk8s cluster's connection secret contains a kubeconfig with only the
cluster endpoint and CA certificate - Nebius authenticates every client
through Nebius IAM, and offers no way to mint service account keys
server-side. Consumers authenticate with service account credentials layered on
the kubeconfig as the ProviderConfigs' NebiusServiceAccountCredentials
identity. The credentials Secret is read off the Nebius ClusterProviderConfig
named default - the same identity that provisions the cluster.

Node group autoscaling is served by mk8s itself (the NodeGroup's autoscaling
block), and the CNI and cluster DNS are bundled with the control plane, so no
in-cluster autoscaler or addons are composed.

ModelCache RWX storage is served by a Nebius shared filesystem.
Nebius has no elastic RWX filesystem and no managed CSI integration,
so the pieces are composed individually: the filesystem is
attached to every node group and mounted by cloud-init, Nebius's
csi-mounted-fs-path CSI driver is installed as a Helm release to serve RWX
PersistentVolumes from paths on the mount, and the modelplane-rwx-fs
StorageClass pins ModelCache PVCs to that driver.
"""

import dataclasses
from typing import Literal

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.infrastructure.nebiuscluster import v1alpha1
from models.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1
from models.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.crossplane.m.kubernetes.providerconfig import v1alpha1 as k8spcv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from models.io.upbound.m.nebius.clusterproviderconfig import v1beta1 as nebiuscpcv1beta1
from models.io.upbound.m.nebius.compute.filesystem import v1beta1 as fsv1beta1
from models.io.upbound.m.nebius.compute.gpucluster import v1beta1 as gpuclusterv1beta1
from models.io.upbound.m.nebius.mk8s.cluster import v1beta1 as clusterv1beta1
from models.io.upbound.m.nebius.mk8s.nodegroup import v1beta1 as nodegroupv1beta1
from models.io.upbound.m.nebius.providerconfig import v1beta1 as nebiuspcv1beta1
from models.io.upbound.m.nebius.vpc.network import v1beta1 as networkv1beta1
from models.io.upbound.m.nebius.vpc.subnet import v1beta1 as subnetv1beta1

# System pool injected into an mk8s cluster to host control-plane
# components (Envoy Gateway, LeaderWorkerSet, cert-manager, etc.) when the
# spec declares no System pool of its own. Declared System pools compose
# like any other pool and replace this default.
_SYSTEM_POOL_NAME = "system"
_SYSTEM_POOL_PLATFORM = "cpu-d3"
_SYSTEM_POOL_PRESET = "4vcpu-16gb"
_SYSTEM_POOL_MIN_NODE_COUNT = 1
_SYSTEM_POOL_MAX_NODE_COUNT = 2

# Labels written on mk8s node groups' nodes. compose-model-deployment reads
# these labels for GPU scheduling.
_LABEL_GPU = "modelplane.ai/gpu"
_LABEL_POOL = "modelplane.ai/pool"

# Label written on composed GPU clusters so node groups can select the GPU
# cluster backing their InfiniBand fabric.
_LABEL_FABRIC = "modelplane.ai/fabric"

# Secret types written to XR status. compose-inference-cluster reads these to
# wire the kubeconfig and service account credentials into ProviderConfigs.
# The credentials' type is the provider identity it authenticates as.
_SECRET_TYPE_KUBECONFIG = "Kubeconfig"

# Key within the connection secret the mk8s Cluster writes. The provider
# renders a kubeconfig targeting the public endpoint when one exists.
_SECRET_KEY_KUBECONFIG = "kubeconfig"

# Identity type for Nebius service account credentials.
_IDENTITY_TYPE_NEBIUS = "NebiusServiceAccountCredentials"

# Boot disk type for node groups. Network SSDs are available on all
# platforms; local disks would tie the group to specific presets.
_BOOT_DISK_TYPE = "NETWORK_SSD"

# Taint applied to GPU node groups so only inference workloads that
# tolerate GPUs are scheduled on them.
_GPU_TAINT_KEY = "nvidia.com/gpu"
_GPU_TAINT_VALUE = "true"
_GPU_TAINT_EFFECT = "NO_SCHEDULE"

# Management policies that exclude Delete, used for resources installed on the
# workload cluster (the RWX StorageClass Object and the CSI driver Helm
# Release). These exist only to configure the cluster and are only ever
# deleted because the whole NebiusCluster - and the cluster itself - is being
# torn down. Deleting them then means asking provider-helm /
# provider-kubernetes to reach a cluster whose kubeconfig Secret has already
# been deleted, which wedges their finalizers and hangs the composite.
# Orphaning them sidesteps that: the in-cluster resources die with the
# cluster.
_ManagementPolicy = Literal["Observe", "Create", "Update", "Delete", "LateInitialize", "*"]
_ORPHAN_MANAGEMENT: list[_ManagementPolicy] = ["Observe", "Create", "Update"]

# The shared filesystem backing ModelCache RWX storage. Nebius shared
# filesystems are fixed size - there is no elastic option like EFS - so the
# filesystem is created at a fixed size that fits several large models'
# weights.
_FS_TYPE = "NETWORK_SSD"
_FS_SIZE_GIB = 1024

# The tag node groups attach the filesystem under, and the host path their
# cloud-init mounts it on. The CSI driver serves volumes from dataDir, a
# directory on that mount.
_FS_MOUNT_TAG = "modelplane-cache"
_FS_MOUNT_POINT = "/mnt/data"

# Key within the composed cloud-init Secret that holds the user data.
_SECRET_KEY_USER_DATA = "userData"

# Cloud-init user data mounting the shared filesystem on every node, per
# https://docs.nebius.com/kubernetes/storage/filesystem-over-csi. runcmd runs
# once at first boot; the fstab entry keeps the mount across reboots.
_FS_CLOUD_INIT = f"""#cloud-config
runcmd:
  - mkdir -p {_FS_MOUNT_POINT}
  - mount -t virtiofs {_FS_MOUNT_TAG} {_FS_MOUNT_POINT}
  - printf "{_FS_MOUNT_TAG} {_FS_MOUNT_POINT} virtiofs defaults,nofail 0 2\\n" >> /etc/fstab
"""

# Nebius's csi-mounted-fs-path CSI driver, which serves RWX
# PersistentVolumes from paths on the pre-mounted shared filesystem.
_CSI_CHART_REPO = "oci://cr.eu-north1.nebius.cloud/mk8s/helm"
_CSI_CHART_NAME = "csi-mounted-fs-path"
_CSI_CHART_VERSION = "0.1.6"
_CSI_NAMESPACE = "kube-system"

# The driver name the chart registers (its values.yaml driverName default).
# The composed StorageClass pins to it as its provisioner.
_CSI_DRIVER_NAME = "mounted-fs-path.csi.nebius.ai"

# Name of the RWX StorageClass Modelplane composes for ModelCache, mirroring
# modelplane-rwx-efs on EKS.
_MANAGED_STORAGE_CLASS = "modelplane-rwx-fs"


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


def _kubeconfig_secret_name(xr: v1alpha1.NebiusCluster) -> str:
    """Derive the kubeconfig secret name from the XR."""
    return resource.child_name(_name(xr.metadata), "kubeconfig")


def _cloud_init_secret_name(xr: v1alpha1.NebiusCluster) -> str:
    """Derive the cloud-init user data secret name from the XR."""
    return resource.child_name(_name(xr.metadata), "cloud-init")


def _pool_fabric(pool: v1alpha1.NodePool) -> str | None:
    """The InfiniBand fabric the pool joins, or None for standard VPC
    networking. The XRD guarantees fabric.infiniband is set when fabric.type
    is InfiniBand."""
    if pool.fabric and pool.fabric.type == "InfiniBand" and pool.fabric.infiniband:
        return pool.fabric.infiniband.fabric
    return None


@dataclasses.dataclass
class Credentials:
    """A reference to the Nebius service account credentials Secret."""

    namespace: str
    name: str
    key: str


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
        self.xr = v1alpha1.NebiusCluster(**resource.struct_to_dict(req.observed.composite.resource))

    def _cred_kind(self) -> str:
        creds = self.xr.spec.credentials
        return creds.type if creds and creds.type else "ClusterProviderConfig"

    def _cred_name(self) -> str:
        creds = self.xr.spec.credentials
        return creds.name if creds and creds.name else "default"

    def _endpoint_access(self) -> str:
        return self.xr.spec.endpointAccess or "PublicAndPrivate"

    def _has_user_system_pool(self) -> bool:
        """Whether the spec declares its own System pool, replacing the
        injected default."""
        return any(pool.role == "System" for pool in self.xr.spec.nodePools)

    def compose(self) -> None:
        # Credentials gate only their consumers: the ProviderConfigs, the CSI
        # driver and RWX StorageClass installed through them, and the status
        # credential entry. The infrastructure is composed unconditionally so
        # that a transiently unresolvable Nebius ClusterProviderConfig never
        # drops provisioned cloud resources from desired state - the gated
        # pieces just recompose on a later reconcile once credentials resolve
        # again.
        creds = self.resolve_credentials()
        self.compose_network()
        self.compose_cluster()
        self.compose_filesystem()
        self.compose_gpu_clusters()
        self.compose_node_groups()
        if creds:
            self.compose_provider_configs(creds)
            self.compose_csi_driver()
            self.compose_storage_class()
        self.write_status(creds)
        self.mark_readiness(provider_configs_composed=creds is not None)

    def resolve_credentials(self) -> Credentials | None:
        """Resolve the Nebius service account credentials Secret consumers
        authenticate to the cluster with.

        The credentials Secret is read off the Nebius ClusterProviderConfig
        the composed resources use - the cluster identity is the identity
        that provisioned it. None while the ProviderConfig or
        ClusterProviderConfig hasn't been fetched yet, or when it doesn't
        source credentials from a Secret.

        When the referenced config is gone but a composed ProviderConfig is
        observed, falls back to the credentials that ProviderConfig was last
        written with: a mistakenly deleted config shouldn't tear the
        credential consumers out of desired state, and a recreated one takes
        over again on a later reconcile."""
        cred_kind = self._cred_kind()
        cred_name = self._cred_name()
        response.require_resources(
            self.rsp,
            name="nebius-provider-config",
            api_version="nebius.m.upbound.io/v1beta1",
            kind=cred_kind,
            match_name=cred_name,
            namespace=_namespace(self.xr.metadata) if cred_kind == "ProviderConfig" else None,
        )
        d = request.get_required_resource(self.req, "nebius-provider-config")
        if d is None:
            creds = self._observed_credentials()
            if creds:
                response.normal(
                    self.rsp,
                    f"Nebius {cred_kind} {cred_name} not found; keeping the "
                    "credentials the composed ProviderConfig already carries",
                )
                return creds
            response.normal(self.rsp, f"Waiting for Nebius {cred_kind} {cred_name}")
            return None

        if cred_kind == "ClusterProviderConfig":
            pc = nebiuscpcv1beta1.ClusterProviderConfig.model_validate(d)
        else:
            pc = nebiuspcv1beta1.ProviderConfig.model_validate(d)
        secret_ref = pc.spec.credentials.secretRef
        if pc.spec.credentials.source != "Secret" or not secret_ref:
            response.warning(
                self.rsp,
                f"Nebius {cred_kind} {cred_name} doesn't source credentials "
                "from a Secret; consumers can't authenticate to the cluster without one",
            )
            return None
        # ClusterProviderConfig.SecretRef carries an explicit namespace; the
        # namespaced ProviderConfig omits it because the secret lives in the
        # ProviderConfig's own namespace.
        ns = getattr(secret_ref, "namespace", None) or (pc.metadata.namespace if pc.metadata else None)
        if not ns:
            response.warning(
                self.rsp,
                f"Nebius {cred_kind} {cred_name} has no namespace; cannot locate credentials Secret",
            )
            return None
        return Credentials(namespace=ns, name=secret_ref.name, key=secret_ref.key)

    def _observed_credentials(self) -> Credentials | None:
        """The credentials the composed kubernetes ProviderConfig was last
        written with, None before one is observed."""
        observed = self.req.observed.resources.get("provider-config-kubernetes")
        if not observed:
            return None
        pc = k8spcv1alpha1.ProviderConfig.model_validate(resource.struct_to_dict(observed.resource))
        ref = pc.spec.identity.secretRef if pc.spec.identity else None
        if not ref:
            return None
        return Credentials(namespace=ref.namespace, name=ref.name, key=ref.key)

    def compose_network(self) -> None:
        """Compose a dedicated VPC network and subnet for the cluster. Nebius
        allocates subnet CIDRs from the network's pools, so there is nothing
        to configure beyond opting into them.

        Every Nebius resource carries an explicit forProvider.name: the Nebius
        API requires a non-empty name, and the provider doesn't derive one
        from the Kubernetes object name."""
        network = networkv1beta1.Network(
            spec=networkv1beta1.Spec(
                providerConfigRef=networkv1beta1.ProviderConfigRef(
                    kind=self._cred_kind(),
                    name=self._cred_name(),
                ),
                forProvider=networkv1beta1.ForProvider(
                    name=_name(self.xr.metadata),
                ),
            ),
        )
        resource.update(self.rsp.desired.resources["network"], network)

        subnet = subnetv1beta1.Subnet(
            spec=subnetv1beta1.Spec(
                providerConfigRef=subnetv1beta1.ProviderConfigRef(
                    kind=self._cred_kind(),
                    name=self._cred_name(),
                ),
                forProvider=subnetv1beta1.ForProvider(
                    name=_name(self.xr.metadata),
                    networkIdSelector=subnetv1beta1.NetworkIdSelector(
                        matchControllerRef=True,
                    ),
                    ipv4PrivatePools=subnetv1beta1.Ipv4PrivatePools(
                        useNetworkPools=True,
                    ),
                ),
            ),
        )
        resource.update(self.rsp.desired.resources["subnet"], subnet)

    def compose_cluster(self) -> None:
        cluster = clusterv1beta1.Cluster(
            spec=clusterv1beta1.Spec(
                providerConfigRef=clusterv1beta1.ProviderConfigRef(
                    kind=self._cred_kind(),
                    name=self._cred_name(),
                ),
                forProvider=clusterv1beta1.ForProvider(
                    name=_name(self.xr.metadata),
                    controlPlane=clusterv1beta1.ControlPlane(
                        version=self.xr.spec.kubernetesVersion,
                        subnetIdSelector=clusterv1beta1.SubnetIdSelector(
                            matchControllerRef=True,
                        ),
                        # The private in-VPC endpoint always exists; a public
                        # endpoint is created alongside it unless the spec
                        # asks for Private.
                        endpoints=(
                            clusterv1beta1.Endpoints(
                                publicEndpoint=clusterv1beta1.PublicEndpoint(),
                            )
                            if self._endpoint_access() != "Private"
                            else clusterv1beta1.Endpoints()
                        ),
                    ),
                ),
                writeConnectionSecretToRef=clusterv1beta1.WriteConnectionSecretToRef(
                    name=_kubeconfig_secret_name(self.xr),
                ),
            ),
        )
        resource.update(self.rsp.desired.resources["cluster"], cluster)

    def compose_filesystem(self) -> None:
        """Provision the shared filesystem backing ModelCache RWX storage,
        and the cloud-init Secret node groups mount it with.

        Every node group attaches the filesystem under _FS_MOUNT_TAG
        (compose_node_groups) and mounts it via this cloud-init;
        compose_csi_driver installs the CSI driver that serves RWX volumes
        from the mount, and compose_storage_class pins the modelplane-rwx-fs
        StorageClass to that driver."""
        resource.update(
            self.rsp.desired.resources["filesystem"],
            fsv1beta1.Filesystem(
                spec=fsv1beta1.Spec(
                    providerConfigRef=fsv1beta1.ProviderConfigRef(
                        kind=self._cred_kind(),
                        name=self._cred_name(),
                    ),
                    forProvider=fsv1beta1.ForProvider(
                        name=f"{_name(self.xr.metadata)}-cache",
                        type=_FS_TYPE,
                        sizeGibibytes=_FS_SIZE_GIB,
                    ),
                ),
            ),
        )

        # The NodeGroup API only takes cloud-init user data by Secret
        # reference, read from the node group's own namespace, so the static
        # mount config is composed as a plain Secret next to the node groups.
        # Secrets have no Ready condition, so mark readiness here.
        resource.update(
            self.rsp.desired.resources["cloud-init"],
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": _cloud_init_secret_name(self.xr),
                    "namespace": _namespace(self.xr.metadata),
                },
                "type": "Opaque",
                "stringData": {_SECRET_KEY_USER_DATA: _FS_CLOUD_INIT},
            },
        )
        self.rsp.desired.resources["cloud-init"].ready = fnv1.READY_TRUE

    def compose_csi_driver(self) -> None:
        """Compose Nebius's csi-mounted-fs-path CSI driver as a Helm release
        on the cluster's own helm ProviderConfig. It serves RWX
        PersistentVolumes from dataDir, a directory on the shared filesystem
        mount every node carries. Gate it on the cluster being observed:
        until it exists, the ProviderConfig can't reach a cluster and the
        release would just error."""
        cluster_observed = "cluster" in self.req.observed.resources
        release_exists = "release-csi-mounted-fs-path" in self.req.observed.resources
        if not (cluster_observed or release_exists):
            return

        resource.update(
            self.rsp.desired.resources["release-csi-mounted-fs-path"],
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
                            name=_CSI_CHART_NAME,
                            repository=_CSI_CHART_REPO,
                            version=_CSI_CHART_VERSION,
                        ),
                        namespace=_CSI_NAMESPACE,
                        values={
                            "dataDir": f"{_FS_MOUNT_POINT}/csi-mounted-fs-path-data/",
                            # The node plugin must run on every node whose pods
                            # mount cache PVCs - including the GPU nodes, which
                            # carry the nvidia.com/gpu taint. The chart's
                            # DaemonSet tolerates nothing by default, so
                            # without this the driver never registers with the
                            # GPU nodes' kubelet and engine pods fail to mount
                            # ("driver name mounted-fs-path.csi.nebius.ai not
                            # found in the list of registered CSI drivers").
                            "tolerations": [
                                {
                                    "key": _GPU_TAINT_KEY,
                                    "operator": "Exists",
                                    "effect": "NoSchedule",
                                },
                            ],
                        },
                    ),
                ),
            ),
        )

    def compose_storage_class(self) -> None:
        """Compose the RWX StorageClass on the workload cluster, pinned to
        the csi-mounted-fs-path driver. Gated like the CSI driver release:
        until the cluster is observed, the ProviderConfig can't reach it. The
        Object is applied through the cluster's own provider-kubernetes
        ProviderConfig. StorageClass has no Ready condition, so use
        SuccessfulCreate (DeriveFromObject would hang)."""
        cluster_observed = "cluster" in self.req.observed.resources
        storage_class_exists = "storage-class-rwx-fs" in self.req.observed.resources
        if not (cluster_observed or storage_class_exists):
            return

        manifest = {
            "apiVersion": "storage.k8s.io/v1",
            "kind": "StorageClass",
            "metadata": {"name": _MANAGED_STORAGE_CLASS},
            "provisioner": _CSI_DRIVER_NAME,
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

    def compose_gpu_clusters(self) -> None:
        """Compose one GPU cluster per distinct InfiniBand fabric used by the
        node pools. Node groups on a fabric select their GPU cluster by its
        fabric label."""
        for fabric in self._fabrics():
            gpu_cluster = gpuclusterv1beta1.GpuCluster(
                metadata=metav1.ObjectMeta(labels={_LABEL_FABRIC: fabric}),
                spec=gpuclusterv1beta1.Spec(
                    providerConfigRef=gpuclusterv1beta1.ProviderConfigRef(
                        kind=self._cred_kind(),
                        name=self._cred_name(),
                    ),
                    forProvider=gpuclusterv1beta1.ForProvider(
                        name=f"{_name(self.xr.metadata)}-{fabric}",
                        infinibandFabric=fabric,
                    ),
                ),
            )
            resource.update(self.rsp.desired.resources[f"gpu-cluster-{fabric}"], gpu_cluster)

    def compose_node_groups(self) -> None:
        if not self._has_user_system_pool():
            self._compose_system_group()
        for pool in self.xr.spec.nodePools:
            labels = {_LABEL_POOL: pool.name}
            if pool.role == "GPU" and pool.gpu:
                labels[_LABEL_GPU] = pool.gpu.acceleratorType

            template = nodegroupv1beta1.Template(
                resources=nodegroupv1beta1.Resources(
                    platform=pool.platform,
                    preset=pool.preset,
                ),
                bootDisk=nodegroupv1beta1.BootDisk(
                    sizeGibibytes=pool.diskSizeGb,
                    type=_BOOT_DISK_TYPE,
                ),
                networkInterfaces=[
                    nodegroupv1beta1.NetworkInterface(
                        subnetIdSelector=nodegroupv1beta1.SubnetIdSelector(
                            matchControllerRef=True,
                        ),
                    ),
                ],
                filesystems=self._cache_filesystems(),
                cloudInitUserDataSecretRef=self._cloud_init_ref(),
                metadata=nodegroupv1beta1.Metadata(labels=labels),
            )

            if pool.role == "GPU" and pool.gpu:
                template.gpuSettings = nodegroupv1beta1.GpuSettings(
                    driversPreset=pool.gpu.driversPreset,
                )
                template.taints = [
                    nodegroupv1beta1.Taint(
                        key=_GPU_TAINT_KEY,
                        value=_GPU_TAINT_VALUE,
                        effect=_GPU_TAINT_EFFECT,
                    ),
                ]

            fabric = _pool_fabric(pool)
            if fabric:
                template.gpuCluster = nodegroupv1beta1.GpuCluster(
                    idSelector=nodegroupv1beta1.IdSelector(
                        matchControllerRef=True,
                        matchLabels={_LABEL_FABRIC: fabric},
                    ),
                )

            ng = nodegroupv1beta1.NodeGroup(
                spec=nodegroupv1beta1.Spec(
                    providerConfigRef=nodegroupv1beta1.ProviderConfigRef(
                        kind=self._cred_kind(),
                        name=self._cred_name(),
                    ),
                    forProvider=nodegroupv1beta1.ForProvider(
                        name=f"{_name(self.xr.metadata)}-{pool.name}",
                        parentIdSelector=nodegroupv1beta1.ParentIdSelector(
                            matchControllerRef=True,
                        ),
                        version=self.xr.spec.kubernetesVersion,
                        template=template,
                    ),
                ),
            )

            # mk8s node groups are either fixed size or autoscaled, never
            # both. maxNodeCount opts into server-side autoscaling.
            if pool.maxNodeCount is not None:
                ng.spec.forProvider.autoscaling = nodegroupv1beta1.Autoscaling(
                    minNodeCount=(pool.minNodeCount if pool.minNodeCount is not None else pool.nodeCount),
                    maxNodeCount=pool.maxNodeCount,
                )
            else:
                ng.spec.forProvider.fixedNodeCount = pool.nodeCount

            resource.update(
                self.rsp.desired.resources[f"nodegroup-{pool.name}"],
                ng,
            )

    def _compose_system_group(self) -> None:
        """Compose the system node group for control-plane components."""
        resource.update(
            self.rsp.desired.resources[f"nodegroup-{_SYSTEM_POOL_NAME}"],
            nodegroupv1beta1.NodeGroup(
                spec=nodegroupv1beta1.Spec(
                    providerConfigRef=nodegroupv1beta1.ProviderConfigRef(
                        kind=self._cred_kind(),
                        name=self._cred_name(),
                    ),
                    forProvider=nodegroupv1beta1.ForProvider(
                        name=f"{_name(self.xr.metadata)}-{_SYSTEM_POOL_NAME}",
                        parentIdSelector=nodegroupv1beta1.ParentIdSelector(
                            matchControllerRef=True,
                        ),
                        version=self.xr.spec.kubernetesVersion,
                        autoscaling=nodegroupv1beta1.Autoscaling(
                            minNodeCount=_SYSTEM_POOL_MIN_NODE_COUNT,
                            maxNodeCount=_SYSTEM_POOL_MAX_NODE_COUNT,
                        ),
                        template=nodegroupv1beta1.Template(
                            resources=nodegroupv1beta1.Resources(
                                platform=_SYSTEM_POOL_PLATFORM,
                                preset=_SYSTEM_POOL_PRESET,
                            ),
                            bootDisk=nodegroupv1beta1.BootDisk(
                                sizeGibibytes=100,
                                type=_BOOT_DISK_TYPE,
                            ),
                            networkInterfaces=[
                                nodegroupv1beta1.NetworkInterface(
                                    subnetIdSelector=nodegroupv1beta1.SubnetIdSelector(
                                        matchControllerRef=True,
                                    ),
                                ),
                            ],
                            filesystems=self._cache_filesystems(),
                            cloudInitUserDataSecretRef=self._cloud_init_ref(),
                            metadata=nodegroupv1beta1.Metadata(
                                labels={
                                    _LABEL_POOL: _SYSTEM_POOL_NAME,
                                },
                            ),
                        ),
                    ),
                ),
            ),
        )

    def _cache_filesystems(self) -> list[nodegroupv1beta1.Filesystem]:
        """The cache filesystem attachment every node group carries. The
        selector resolves to the one composed Filesystem."""
        return [
            nodegroupv1beta1.Filesystem(
                attachMode="READ_WRITE",
                mountTag=_FS_MOUNT_TAG,
                existingFilesystem=nodegroupv1beta1.ExistingFilesystem(
                    idSelector=nodegroupv1beta1.IdSelector(matchControllerRef=True),
                ),
            ),
        ]

    def _cloud_init_ref(self) -> nodegroupv1beta1.CloudInitUserDataSecretRef:
        """A reference to the composed cloud-init Secret that mounts the
        cache filesystem."""
        return nodegroupv1beta1.CloudInitUserDataSecretRef(
            name=_cloud_init_secret_name(self.xr),
            key=_SECRET_KEY_USER_DATA,
        )

    def compose_provider_configs(self, creds: Credentials) -> None:
        """Compose ProviderConfigs for provider-kubernetes and provider-helm
        targeting the cluster. The kubeconfig from the cluster's connection
        secret has no credentials, so both authenticate as the Nebius service
        account identity."""
        resource.update(
            self.rsp.desired.resources["provider-config-kubernetes"],
            k8spcv1alpha1.ProviderConfig(
                metadata=metav1.ObjectMeta(name=_kubeconfig_secret_name(self.xr)),
                spec=k8spcv1alpha1.Spec(
                    credentials=k8spcv1alpha1.Credentials(
                        source="Secret",
                        secretRef=k8spcv1alpha1.SecretRef(
                            name=_kubeconfig_secret_name(self.xr),
                            namespace=_namespace(self.xr.metadata),
                            key=_SECRET_KEY_KUBECONFIG,
                        ),
                    ),
                    identity=k8spcv1alpha1.Identity(
                        type=_IDENTITY_TYPE_NEBIUS,
                        source="Secret",
                        secretRef=k8spcv1alpha1.SecretRef(
                            name=creds.name,
                            namespace=creds.namespace,
                            key=creds.key,
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
                            namespace=_namespace(self.xr.metadata),
                            key=_SECRET_KEY_KUBECONFIG,
                        ),
                    ),
                    identity=helmpcv1beta1.Identity(
                        type=_IDENTITY_TYPE_NEBIUS,
                        source="Secret",
                        secretRef=helmpcv1beta1.SecretRef(
                            name=creds.name,
                            namespace=creds.namespace,
                            key=creds.key,
                        ),
                    ),
                ),
            ),
        )

    def write_status(self, creds: Credentials | None) -> None:
        secrets = [
            v1alpha1.Secret(
                type=_SECRET_TYPE_KUBECONFIG,
                name=_kubeconfig_secret_name(self.xr),
                key=_SECRET_KEY_KUBECONFIG,
            ),
        ]
        if creds:
            credential = v1alpha1.Secret(
                type=_IDENTITY_TYPE_NEBIUS,
                name=creds.name,
                key=creds.key,
            )
            # Only name a namespace when it differs from the XR's own, the
            # namespace consumers assume by default.
            if creds.namespace != _namespace(self.xr.metadata):
                credential.namespace = creds.namespace
            secrets.append(credential)
        status = v1alpha1.Status(
            secrets=secrets,
            # The RWX StorageClass Modelplane composes for ModelCache.
            # Published immediately so ModelCache can target it; the class may
            # still be materialising on the workload cluster.
            cache=v1alpha1.Cache(storageClassName=_MANAGED_STORAGE_CLASS),
        )
        resource.update_status(self.rsp.desired.composite, status)

    def mark_readiness(self, *, provider_configs_composed: bool) -> None:
        """Mark composed resources as ready based on their observed conditions."""
        managed_resources = [
            "network",
            "subnet",
            "cluster",
            "filesystem",
        ]
        managed_resources += [f"gpu-cluster-{fabric}" for fabric in self._fabrics()]
        if not self._has_user_system_pool():
            managed_resources.append(f"nodegroup-{_SYSTEM_POOL_NAME}")
        managed_resources += [f"nodegroup-{pool.name}" for pool in self.xr.spec.nodePools]
        # The CSI driver Helm release is only composed once the cluster is
        # observed, so only mark it ready when it's actually in desired state -
        # touching it here otherwise would re-add a resource we gated out.
        if "release-csi-mounted-fs-path" in self.rsp.desired.resources:
            managed_resources.append("release-csi-mounted-fs-path")

        for r in managed_resources:
            if resource.get_condition(self.req.observed.resources.get(r), "Ready").status == "True":
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE

        if provider_configs_composed:
            self.rsp.desired.resources["provider-config-kubernetes"].ready = fnv1.READY_TRUE
            self.rsp.desired.resources["provider-config-helm"].ready = fnv1.READY_TRUE

    def _fabrics(self) -> list[str]:
        """The distinct InfiniBand fabrics used by the node pools, in
        first-use order."""
        fabrics: list[str] = []
        for pool in self.xr.spec.nodePools:
            fabric = _pool_fabric(pool)
            if fabric and fabric not in fabrics:
                fabrics.append(fabric)
        return fabrics
