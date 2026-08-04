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

"""Compose a Vultr Kubernetes Engine (VKE) cluster with node pools.

This function provisions a VKE cluster with a fixed system node pool and
separate VkeNodePool managed resources for each user-defined pool. The
system pool is inline on the VkeCluster (create-only, never changes). User
pools are separate managed resources that gate on the cluster being Ready,
giving them an independent lifecycle - they can be updated or removed
without recreating the cluster.

Once node pools are composed, a provider-kubernetes ProviderConfig and a
GPU-observer Object are added. The Object reads the nvidia-operator-validator
DaemonSet on the workload cluster using a CEL readiness query. VultrCluster
is only marked Ready when this DaemonSet is fully ready, meaning the GPU
stack is validated and nvidia.com/gpu resources are advertised before the
serving stack starts scheduling inference workloads.

Node pool autoscaling is served by VKE itself (the pool's autoScaler block),
so no in-cluster autoscaler is composed. Storage is: VKE ships the
vultr-vfs-storage ReadWriteMany StorageClass backed by Vultr File System,
but it is not usable on GPU nodes, so ModelCache RWX storage is served by
Longhorn instead - a Helm release installs Longhorn (tolerating the GPU
taint so its node components run everywhere), an NFS-client installer
DaemonSet satisfies Longhorn's RWX prerequisite on VKE's node image, and
the modelplane-rwx-longhorn StorageClass pins ModelCache PVCs to
driver.longhorn.io.
"""

from typing import Literal

import grpc
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.infrastructure.vultrcluster import v1alpha1
from models.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1
from models.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.crossplane.m.kubernetes.providerconfig import v1alpha1 as k8spcv1alpha1
from models.io.crossplane.m.vultr.vkecluster import v1alpha1 as vkev1alpha1
from models.io.crossplane.m.vultr.vkenodepool import v1alpha1 as vkenodepoolv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# System pool injected into every VKE cluster to host control-plane
# components (Envoy Gateway, LeaderWorkerSet, cert-manager, etc.). Not part of
# the user-facing API - compose-inference-cluster only passes GPU pools. The
# plan matches the Nebius system pool's shape (16 GB memory).
_SYSTEM_POOL_NAME = "system"
_SYSTEM_POOL_PLAN = "vc2-6c-16gb"
_SYSTEM_POOL_MIN_NODES = 1
_SYSTEM_POOL_MAX_NODES = 2

# Labels written on VKE node pools' nodes. compose-model-deployment reads
# these labels for GPU scheduling.
_LABEL_GPU = "modelplane.ai/gpu"
_LABEL_POOL = "modelplane.ai/pool"

# Secret type written to XR status. compose-inference-cluster reads this to
# wire the kubeconfig into a ClusterProviderConfig.
_SECRET_TYPE_KUBECONFIG = "Kubeconfig"

# Key within the connection secret the VkeCluster writes. The provider
# publishes the decoded kubeconfig under this key once the cluster exists.
_SECRET_KEY_KUBECONFIG = "kubeconfig"

# Taint applied to GPU node pools so only inference workloads that
# tolerate GPUs are scheduled on them.
_GPU_TAINT_KEY = "nvidia.com/gpu"
_GPU_TAINT_VALUE = "true"
_GPU_TAINT_EFFECT = "NoSchedule"

# Management policies that exclude Delete, used for resources installed on the
# workload cluster (the Longhorn Helm Release, the NFS installer, and the RWX
# StorageClass Object). These exist only to configure the cluster and are only
# ever deleted because the whole VultrCluster - and the cluster itself - is
# being torn down. Deleting them then means asking provider-helm /
# provider-kubernetes to reach a cluster whose kubeconfig Secret has already
# been deleted, which wedges their finalizers and hangs the composite.
# Orphaning them sidesteps that: the in-cluster resources die with the
# cluster.
_ManagementPolicy = Literal["Observe", "Create", "Update", "Delete", "LateInitialize", "*"]
_ORPHAN_MANAGEMENT: list[_ManagementPolicy] = ["Observe", "Create", "Update"]

# Longhorn serves ModelCache RWX storage. VKE's built-in vultr-vfs-storage
# class (Vultr File System) is not usable on GPU nodes, so Longhorn is
# installed via its Helm chart to serve RWX volumes from node-local disks
# over NFS (each RWX volume is exported by a share-manager pod).
_LONGHORN_CHART_REPO = "https://charts.longhorn.io"
_LONGHORN_CHART_NAME = "longhorn"
_LONGHORN_CHART_VERSION = "1.12.0"
_LONGHORN_NAMESPACE = "longhorn-system"
_LONGHORN_PROVISIONER = "driver.longhorn.io"

# Name of the RWX StorageClass Modelplane composes for ModelCache, mirroring
# modelplane-rwx-fs on Nebius.
_MANAGED_STORAGE_CLASS = "modelplane-rwx-longhorn"

# The GPU taint as a Kubernetes toleration (chart values, DaemonSets) and in
# Longhorn's taint-toleration string form, which is the only way to give
# Longhorn's system-managed components (instance-manager, share-manager, the
# CSI daemonsets) a toleration.
_GPU_TOLERATION = {"key": _GPU_TAINT_KEY, "operator": "Exists", "effect": _GPU_TAINT_EFFECT}
_LONGHORN_TAINT_TOLERATION = f"{_GPU_TAINT_KEY}:{_GPU_TAINT_EFFECT}"

# NFS client installer DaemonSet. Longhorn RWX volumes are mounted over
# NFSv4; VKE's node image lacks nfs-common (Vultr's own Longhorn guide
# installs it).
_NFS_INSTALLER_NAME = "longhorn-nfs-installation"
_NFS_INSTALLER_NAMESPACE = "kube-system"

# Vultr pre-installs the NVIDIA GPU Operator on every VKE GPU cluster. The
# operator's validator DaemonSet runs a four-stage init sequence (driver →
# CUDA → device-plugin → full validation). When all pods in the DaemonSet
# are ready the GPU stack is validated and nvidia.com/gpu resources are
# advertised on the node. VultrCluster gates its own readiness on this
# signal so the serving stack never starts before GPUs are available.
_GPU_OPERATOR_NAMESPACE = "gpu-operator"
_GPU_OPERATOR_VALIDATOR_DS = "nvidia-operator-validator"
# CEL DaemonSet readiness: at least one pod scheduled and all pods ready.
# has() guards against missing status fields on early reconciles. Shared by
# the gpu-observer and the NFS installer.
_DS_READY_CEL = (
    "has(object.status.numberReady)"
    " && object.status.desiredNumberScheduled >= 1"
    " && object.status.numberReady == object.status.desiredNumberScheduled"
)


def _name(meta: metav1.ObjectMeta | None) -> str:
    """The object's name, always set on resources read from the API server."""
    if meta is None or meta.name is None:
        raise ValueError("metadata.name is unexpectedly absent")
    return meta.name


def _namespace(meta: metav1.ObjectMeta | None) -> str:
    """The object's namespace, always set on resources read from the API server."""
    if meta is None or meta.namespace is None:
        raise ValueError("metadata.namespace is unexpectedly absent")
    return meta.namespace


def _kubeconfig_secret_name(xr: v1alpha1.VultrCluster) -> str:
    """Derive the kubeconfig secret name from the XR."""
    return resource.child_name(_name(xr.metadata), "kubeconfig")


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
        self.xr = v1alpha1.VultrCluster(**resource.struct_to_dict(req.observed.composite.resource))

    def _cred_kind(self) -> str:
        creds = self.xr.spec.credentials
        return creds.type if creds and creds.type else "ClusterProviderConfig"

    def _cred_name(self) -> str:
        creds = self.xr.spec.credentials
        return creds.name if creds and creds.name else "default"

    def compose(self) -> None:
        self.compose_cluster()
        if self._cluster_ready() or self._dependents_observed():
            self.compose_node_pools()
            self.compose_provider_configs()
            self.compose_gpu_observer()
            self.compose_nfs_installer()
            self.compose_longhorn()
            self.compose_storage_class()
        self.write_status()
        self.mark_readiness()

    def _cluster_ready(self) -> bool:
        return resource.get_condition(self.req.observed.resources.get("cluster"), "Ready").status == "True"

    def _dependents_observed(self) -> bool:
        """Whether the Ready-gated dependents were composed on a previous
        reconcile. The gate delays their first composition until the cluster
        is Ready, but must not drop them from desired state when the Ready
        condition transiently regresses - that would delete them, and the
        VkeNodePools are not orphaned, so their nodes would be deprovisioned
        with them. The dependents are composed as one block, so any observed
        member means the block was composed before; the ProviderConfig is the
        sentinel, with the node pools covering partially-applied states."""
        observed = self.req.observed.resources
        return "provider-config-kubernetes" in observed or any(name.startswith("node-pool-") for name in observed)

    def compose_cluster(self) -> None:
        """Compose the VKE cluster with the fixed system node pool.
        User-defined pools are separate VkeNodePool resources composed after
        the cluster is Ready, giving them an independent lifecycle."""
        cluster = vkev1alpha1.VkeCluster(
            spec=vkev1alpha1.Spec(
                providerConfigRef=vkev1alpha1.ProviderConfigRef(
                    kind=self._cred_kind(),
                    name=self._cred_name(),
                ),
                forProvider=vkev1alpha1.ForProvider(
                    label=_name(self.xr.metadata),
                    region=self.xr.spec.region,
                    version=self.xr.spec.kubernetesVersion,  # ty: ignore[invalid-argument-type]  # the XRD defaults kubernetesVersion
                    haControlplanes=True,
                    nodePools=[self._system_pool()],
                ),
                writeConnectionSecretToRef=vkev1alpha1.WriteConnectionSecretToRef(
                    name=_kubeconfig_secret_name(self.xr),
                ),
            ),
        )
        resource.update(self.rsp.desired.resources["cluster"], cluster)

    def compose_node_pools(self) -> None:
        """Compose a VkeNodePool for each user-defined pool. Gated on the
        cluster being Ready so the cluster ID is available for the selector."""
        for pool in self.xr.spec.nodePools:
            resource.update(
                self.rsp.desired.resources[f"node-pool-{pool.name}"],
                self._node_pool(pool),
            )

    def compose_provider_configs(self) -> None:
        """Compose ProviderConfigs for provider-kubernetes and provider-helm
        pointing at the VKE kubeconfig secret. The kubeconfig embeds client
        certificates, so no identity block is needed. provider-kubernetes
        serves the gpu-observer, NFS installer and StorageClass Objects;
        provider-helm serves the Longhorn release."""
        kubeconfig_name = _kubeconfig_secret_name(self.xr)
        resource.update(
            self.rsp.desired.resources["provider-config-kubernetes"],
            k8spcv1alpha1.ProviderConfig(
                metadata=metav1.ObjectMeta(
                    name=kubeconfig_name,
                    namespace=_namespace(self.xr.metadata),
                ),
                spec=k8spcv1alpha1.Spec(
                    credentials=k8spcv1alpha1.Credentials(
                        source="Secret",
                        secretRef=k8spcv1alpha1.SecretRef(
                            namespace=_namespace(self.xr.metadata),
                            name=kubeconfig_name,
                            key=_SECRET_KEY_KUBECONFIG,
                        ),
                    ),
                ),
            ),
        )

        resource.update(
            self.rsp.desired.resources["provider-config-helm"],
            helmpcv1beta1.ProviderConfig(
                metadata=metav1.ObjectMeta(
                    name=kubeconfig_name,
                    namespace=_namespace(self.xr.metadata),
                ),
                spec=helmpcv1beta1.Spec(
                    credentials=helmpcv1beta1.Credentials(
                        source="Secret",
                        secretRef=helmpcv1beta1.SecretRef(
                            namespace=_namespace(self.xr.metadata),
                            name=kubeconfig_name,
                            key=_SECRET_KEY_KUBECONFIG,
                        ),
                    ),
                ),
            ),
        )

    def compose_gpu_observer(self) -> None:
        """Observe the nvidia-operator-validator DaemonSet on the workload
        cluster. The CEL readiness query keeps this Object un-Ready until all
        DS pods pass validation, so VultrCluster itself stays un-Ready until
        the GPU stack is fully initialised."""
        resource.update(
            self.rsp.desired.resources["gpu-observer"],
            k8sobjv1alpha1.Object(
                metadata=metav1.ObjectMeta(namespace=_namespace(self.xr.metadata)),
                spec=k8sobjv1alpha1.Spec(
                    managementPolicies=["Observe"],
                    providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                        kind="ProviderConfig",
                        name=_kubeconfig_secret_name(self.xr),
                    ),
                    readiness=k8sobjv1alpha1.Readiness(
                        policy="DeriveFromCelQuery",
                        celQuery=_DS_READY_CEL,
                    ),
                    forProvider=k8sobjv1alpha1.ForProvider(
                        manifest={
                            "apiVersion": "apps/v1",
                            "kind": "DaemonSet",
                            "metadata": {
                                "name": _GPU_OPERATOR_VALIDATOR_DS,
                                "namespace": _GPU_OPERATOR_NAMESPACE,
                            },
                        },
                    ),
                ),
            ),
        )

    def compose_nfs_installer(self) -> None:
        """Compose a DaemonSet that installs the NFS client on every node.
        Longhorn RWX volumes are mounted over NFSv4 and VKE's node image
        ships without nfs-common, so without this every RWX mount fails.
        The CEL readiness keeps the XR un-Ready until every node - including
        autoscaled GPU nodes - has the client installed."""
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {
                "name": _NFS_INSTALLER_NAME,
                "namespace": _NFS_INSTALLER_NAMESPACE,
                "labels": {"app": _NFS_INSTALLER_NAME},
            },
            "spec": {
                "selector": {"matchLabels": {"app": _NFS_INSTALLER_NAME}},
                "updateStrategy": {"type": "RollingUpdate"},
                "template": {
                    "metadata": {"labels": {"app": _NFS_INSTALLER_NAME}},
                    "spec": {
                        "hostNetwork": True,
                        "hostPID": True,
                        "tolerations": [_GPU_TOLERATION],
                        # nsenter into the host's mount namespace to install
                        # nfs-common on the node itself; the pause container
                        # then just keeps the pod Ready as the installed
                        # signal.
                        "initContainers": [
                            {
                                "name": "nfs-installation",
                                "image": "alpine:3.22",
                                "command": [
                                    "nsenter",
                                    "--mount=/proc/1/ns/mnt",
                                    "--",
                                    "bash",
                                    "-c",
                                    "apt-get update -qq && apt-get install -y nfs-common && modprobe nfs",
                                ],
                                "securityContext": {"privileged": True},
                            },
                        ],
                        "containers": [
                            {"name": "sleep", "image": "registry.k8s.io/pause:3.10"},
                        ],
                    },
                },
            },
        }
        resource.update(
            self.rsp.desired.resources["nfs-installer"],
            k8sobjv1alpha1.Object(
                metadata=metav1.ObjectMeta(namespace=_namespace(self.xr.metadata)),
                spec=k8sobjv1alpha1.Spec(
                    managementPolicies=_ORPHAN_MANAGEMENT,
                    providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                        kind="ProviderConfig",
                        name=_kubeconfig_secret_name(self.xr),
                    ),
                    readiness=k8sobjv1alpha1.Readiness(
                        policy="DeriveFromCelQuery",
                        celQuery=_DS_READY_CEL,
                    ),
                    forProvider=k8sobjv1alpha1.ForProvider(manifest=manifest),
                ),
            ),
        )

    def compose_longhorn(self) -> None:
        """Compose Longhorn as a Helm release on the cluster's own helm
        ProviderConfig. It serves RWX PersistentVolumes from node-local
        disks, exported over NFS by per-volume share-manager pods."""
        resource.update(
            self.rsp.desired.resources["release-longhorn"],
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
                            name=_LONGHORN_CHART_NAME,
                            repository=_LONGHORN_CHART_REPO,
                            version=_LONGHORN_CHART_VERSION,
                        ),
                        namespace=_LONGHORN_NAMESPACE,
                        values={
                            # Don't make the chart's own "longhorn" class the
                            # cluster default; the composed
                            # modelplane-rwx-longhorn class is what
                            # ModelCache targets.
                            "persistence": {"defaultClass": False},
                            # Manager, driver deployer and UI must also run
                            # on tainted GPU nodes.
                            "global": {"tolerations": [_GPU_TOLERATION]},
                            "defaultSettings": {
                                # System-managed components - instance-manager,
                                # share-manager, the CSI daemonsets - take
                                # tolerations from this Longhorn setting, not
                                # from chart tolerations. Without it the CSI
                                # node plugin never registers on GPU nodes and
                                # engine pods there fail to mount cache PVCs.
                                "taintToleration": _LONGHORN_TAINT_TOLERATION,
                                # The smallest cluster is one system node plus
                                # one GPU node; hard replica anti-affinity
                                # would wedge provisioning there. The cache
                                # holds re-downloadable model weights, so
                                # degraded placement beats no placement.
                                "replicaSoftAntiAffinity": "true",
                            },
                        },
                    ),
                ),
            ),
        )

    def compose_storage_class(self) -> None:
        """Compose the RWX StorageClass on the workload cluster, pinned to
        the Longhorn CSI driver. Applied through the cluster's own
        provider-kubernetes ProviderConfig. StorageClass has no Ready
        condition, so use SuccessfulCreate (DeriveFromObject would hang)."""
        manifest = {
            "apiVersion": "storage.k8s.io/v1",
            "kind": "StorageClass",
            "metadata": {"name": _MANAGED_STORAGE_CLASS},
            "provisioner": _LONGHORN_PROVISIONER,
            "allowVolumeExpansion": True,
            "parameters": {
                # The v2 data engine needs hugepages and NVMe kernel modules;
                # v1 is the right fit for a weights cache.
                "dataEngine": "v1",
                # Two replicas fit the smallest cluster; the cache is
                # re-downloadable so durability beyond that buys little.
                "numberOfReplicas": "2",
                "staleReplicaTimeout": "30",
                # Longhorn's documented RWX mount options; the string fully
                # replaces Longhorn's defaults.
                "nfsOptions": "vers=4.2,noresvport,softerr,timeo=600,retrans=5",
            },
        }
        resource.update(
            self.rsp.desired.resources["storage-class-rwx-longhorn"],
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
        self.rsp.desired.resources["storage-class-rwx-longhorn"].ready = fnv1.READY_TRUE

    def _system_pool(self) -> vkev1alpha1.NodePool:
        """The system node pool for control-plane components."""
        return vkev1alpha1.NodePool(
            label=_SYSTEM_POOL_NAME,
            plan=_SYSTEM_POOL_PLAN,
            nodeQuantity=_SYSTEM_POOL_MIN_NODES,
            autoScaler=True,
            minNodes=_SYSTEM_POOL_MIN_NODES,
            maxNodes=_SYSTEM_POOL_MAX_NODES,
            labels={_LABEL_POOL: _SYSTEM_POOL_NAME},
        )

    def _node_pool(self, pool: v1alpha1.NodePool) -> vkenodepoolv1alpha1.VkeNodePool:
        """Map an XR node pool to a VkeNodePool managed resource."""
        labels = {_LABEL_POOL: pool.name}
        if pool.role == "GPU" and pool.gpu:
            labels[_LABEL_GPU] = pool.gpu.acceleratorType

        fp = vkenodepoolv1alpha1.ForProvider(
            label=pool.name,
            plan=pool.plan,
            nodeQuantity=pool.nodeCount,  # ty: ignore[invalid-argument-type]  # the XRD defaults nodeCount
            labels=labels,
            vkeClusterIDSelector=vkenodepoolv1alpha1.VkeClusterIDSelector(
                matchControllerRef=True,
            ),
        )

        if pool.role == "GPU":
            fp.taints = [
                vkenodepoolv1alpha1.Taint(
                    key=_GPU_TAINT_KEY,
                    value=_GPU_TAINT_VALUE,
                    effect=_GPU_TAINT_EFFECT,
                ),
            ]

        # maxNodeCount opts into VKE's server-side autoscaling.
        if pool.maxNodeCount is not None:
            fp.autoScaler = True
            fp.minNodes = pool.minNodeCount if pool.minNodeCount is not None else pool.nodeCount
            fp.maxNodes = pool.maxNodeCount

        return vkenodepoolv1alpha1.VkeNodePool(
            spec=vkenodepoolv1alpha1.Spec(
                providerConfigRef=vkenodepoolv1alpha1.ProviderConfigRef(
                    kind=self._cred_kind(),
                    name=self._cred_name(),
                ),
                forProvider=fp,
            ),
        )

    def write_status(self) -> None:
        status = v1alpha1.Status(
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
        """Mark composed resources as ready based on their observed conditions.

        The ProviderConfigs have no meaningful Ready condition and are always
        marked ready. All other resources (cluster, node pools, gpu-observer,
        NFS installer, Longhorn release) are marked ready only once their
        observed Ready condition is True. The gpu-observer and nfs-installer
        Objects use DeriveFromCelQuery, so the XR only becomes Ready once the
        nvidia-operator-validator and NFS installer DaemonSets are fully
        rolled out - and provider-helm reports the Longhorn release deployed.
        """
        for r in self.rsp.desired.resources:
            if r in ("provider-config-kubernetes", "provider-config-helm"):
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE
                continue
            if resource.get_condition(self.req.observed.resources.get(r), "Ready").status == "True":
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE
