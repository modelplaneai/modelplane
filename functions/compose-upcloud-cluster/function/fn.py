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

"""Compose an UpCloud Managed Kubernetes (UKS) cluster with node groups.

This function provisions a UKS cluster on a dedicated SDN network (UKS
requires a private network per cluster, with DHCP on and its default
route off), with a fixed system node group and a KubernetesNodeGroup
managed resource for each user-defined pool. UKS has no inline pools on
the cluster, so the system pool is a separate node group too. Node
groups gate on the cluster being Ready: UKS only accepts node group
operations on a running cluster. UpCloud honors only nodeCount changes
on an existing node group; the provider replaces the group for any
other change.

The cluster's kubeconfig embeds static client certificates, so the
kubeconfig alone is enough to reach the cluster. fetchKubeconfig must
be true or the provider publishes no kubeconfig to the connection
secret.

UKS has no server-side node group autoscaler, and the cluster
autoscaler's UpCloud cloud provider is not merged upstream
(kubernetes/autoscaler#10234); UpCloud publishes images of its fork at
ghcr.io/upcloudltd/autoscaler. Pools with maxNodeCount set are scaled
by that autoscaler, installed on the workload cluster through the
upstream chart with the image overridden. It authenticates to the
UpCloud API with a token from the Secret spec.autoscaler references,
which UpCloud provisions nowhere on the cluster, so an Object copies it
to kube-system. provider-kubernetes resolves the copy's patchesFrom
reference server-side; the token never passes through this function or
the Object's manifest. The newest fork image is built for Kubernetes
1.29, older than any UKS version this XR provisions; it talks to the
UpCloud API rather than version-skewed Kubernetes APIs, but the skew is
outside the autoscaler's support policy. The autoscaler enforces a
minimum of one node per group, so pools can't scale to zero.

UpCloud pre-installs the NVIDIA driver and container toolkit in its GPU
node images, so GPU pools need only the taint and pool labels here; the
serving stack installs NFD and the DRA driver (no GPU Operator), and
the DRA driver is the only GPU allocator. No GPU readiness gate is
composed: there is no pre-installed operator to observe.

No ModelCache RWX StorageClass is composed: UpCloud File Storage is
managed NFS but has no CSI driver, so PersistentVolumes can only be
provisioned statically, and Modelplane ships no cache storage of its
own on UpCloud.
"""

from typing import Literal

import grpc
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.infrastructure.upcloudcluster import v1alpha1
from models.com.upcloud.m.network import v1alpha1 as networkv1alpha1
from models.com.upcloud.m.uks.kubernetescluster import v1alpha1 as clusterv1alpha1
from models.com.upcloud.m.uks.kubernetesnodegroup import v1alpha1 as nodegroupv1alpha1
from models.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1
from models.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.crossplane.m.kubernetes.providerconfig import v1alpha1 as k8spcv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# System pool injected into every UKS cluster to host control-plane
# components (Envoy Gateway, LeaderWorkerSet, cert-manager, etc.). Not part
# of the user-facing API - compose-inference-cluster only passes GPU pools.
# The plan matches the Nebius, Vultr and Civo system pools' shape (16 GB
# memory); Premium is UpCloud's general-purpose family (Cloud Native plans
# unbundle storage, Starter tops out smaller). Fixed at two nodes: the
# autoscaler only scales user pools.
_SYSTEM_POOL_NAME = "system"
_SYSTEM_POOL_PLAN = "PREMIUM-4xCPU-16GB"
_SYSTEM_POOL_NODES = 2

# Labels written on UKS node groups' nodes. compose-model-deployment reads
# these labels for GPU scheduling.
_LABEL_GPU = "modelplane.ai/gpu"
_LABEL_POOL = "modelplane.ai/pool"

# Secret type written to XR status. compose-inference-cluster reads this to
# wire the kubeconfig into a ClusterProviderConfig.
_SECRET_TYPE_KUBECONFIG = "Kubeconfig"

# Key within the connection secret the KubernetesCluster resource writes.
# The provider publishes the decoded kubeconfig under this key once the
# cluster exists (spec.forProvider.fetchKubeconfig must be true for the
# provider to read it from the UpCloud API).
_SECRET_KEY_KUBECONFIG = "kubeconfig"

# Taint applied to GPU node groups so only inference workloads that
# tolerate GPUs are scheduled on them. UpCloud taints GPU nodes with
# nothing itself.
_GPU_TAINT_KEY = "nvidia.com/gpu"
_GPU_TAINT_VALUE = "true"
_GPU_TAINT_EFFECT = "NoSchedule"

# Label and taint on CPU inference pools. The label is what the serving
# stack's dra-driver-cpu selects its nodes by; the taint keeps system
# workloads off the pool as the GPU taint does on GPU pools, and engine
# pods tolerate it (compose-model-replica).
_LABEL_CPU = "modelplane.ai/cpu"
_CPU_TAINT_KEY = "modelplane.ai/cpu"
_CPU_TAINT_VALUE = "true"
_CPU_TAINT_EFFECT = "NoSchedule"

# Storage tier for node group disks on plans whose storage UpCloud
# unbundles (GPU and Cloud Native plans). maxiops is UpCloud's SSD tier,
# matching the other clouds' SSD boot disks.
_NODE_STORAGE_TIER = "maxiops"

# Control plane plan for every cluster: three API servers and a
# three-member etcd on separate infrastructure, up to 120 nodes. The
# dev-md plan has no redundancy, and the plan is create-time only, so
# every cluster gets the HA control plane (matching Vultr's
# haControlplanes).
_CLUSTER_PLAN = "prod-md-ha"

# UKS requires controlPlaneIpFilter; an empty list denies everyone. Open
# access matches UKS's own default (null means no filtering) and the other
# clouds' public endpoints: the Modelplane control plane and anything
# consuming the kubeconfig reach the API server over the Internet.
_CONTROL_PLANE_IP_FILTER = ["0.0.0.0/0"]

# The cluster's dedicated SDN network. UKS requires DHCP on and the DHCP
# default route off (nodes route through their public/utility interfaces),
# and a network can serve only one cluster. The CIDR is fixed: the network
# is never peered, so overlap across clusters doesn't matter.
_NETWORK_CIDR = "10.0.1.0/24"
_NETWORK_FAMILY = "IPv4"

# The Kubernetes cluster autoscaler, installed on the workload cluster for
# pools with maxNodeCount set. Chart pin matches compose-eks-cluster's and
# compose-civo-cluster's. The image is UpCloud's fork build (see the module
# docstring); the upstream chart renders cloudProvider: upcloud
# mechanically even though its documented provider list omits it.
_AUTOSCALER_NAMESPACE = "kube-system"
_AUTOSCALER_CHART_REPO = "https://kubernetes.github.io/autoscaler"
_AUTOSCALER_CHART_NAME = "cluster-autoscaler"
_AUTOSCALER_CHART_VERSION = "9.57.0"
_AUTOSCALER_IMAGE_REPO = "ghcr.io/upcloudltd/autoscaler"
_AUTOSCALER_IMAGE_TAG = "v1.29.5"

# Secret the autoscaler reads the UpCloud API token from, copied into
# kube-system on the workload cluster from the Secret spec.autoscaler
# references. The chart's extraEnvSecrets renders a secretKeyRef without
# optional, so the autoscaler pod restarts until the copy lands.
_AUTOSCALER_SECRET_NAME = "upcloud-autoscaler"
_AUTOSCALER_SECRET_KEY = "token"

# Annotation the provider sets on a managed resource with its external
# name. On the cluster that's the UpCloud UUID the autoscaler's
# UPCLOUD_CLUSTER_ID env wants; on node groups this function sets it to
# the pool name, since KubernetesNodeGroup has no name in forProvider -
# the external name is the UKS node group name.
_ANNOTATION_EXTERNAL_NAME = "crossplane.io/external-name"

# Compose the in-cluster resources (the autoscaler Helm release and the
# token Secret's Object) with Observe, Create and Update but not Delete.
# Deleting them with the XR would mean asking their providers to reach a
# cluster whose kubeconfig Secret has already been deleted, wedging their
# finalizers; orphaning them sidesteps that - the in-cluster resources die
# with the cluster.
_ManagementPolicy = Literal["Observe", "Create", "Update", "Delete", "LateInitialize", "*"]
_ORPHAN_MANAGEMENT: list[_ManagementPolicy] = ["Observe", "Create", "Update"]

# Node group management policies that exclude LateInitialize, used for
# autoscaled pools: nodeCount is seeded via initProvider so it is applied
# only at creation and then left alone. The cluster autoscaler drives the
# group's node count; without this Crossplane would keep reverting it to
# nodeCount and fight the autoscaler. (initProvider is gated on
# enumerating management policies - the default "*" still late-initializes
# nodeCount into forProvider, defeating the purpose.)
_AUTOSCALED_POOL_MANAGEMENT: list[_ManagementPolicy] = ["Observe", "Create", "Update", "Delete"]


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


def _kubeconfig_secret_name(xr: v1alpha1.UpCloudCluster) -> str:
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
        self.xr = v1alpha1.UpCloudCluster(**resource.struct_to_dict(req.observed.composite.resource))

    def _cred_kind(self) -> str:
        creds = self.xr.spec.credentials
        return creds.type if creds and creds.type else "ClusterProviderConfig"

    def _cred_name(self) -> str:
        creds = self.xr.spec.credentials
        return creds.name if creds and creds.name else "default"

    def compose(self) -> None:
        self.compose_network()
        self.compose_cluster()
        if self._cluster_ready() or self._dependents_observed():
            self.compose_node_pools()
            # Only the autoscaler reaches into the workload cluster, so
            # without credentials for it nothing composes the
            # ProviderConfigs, the token Secret or the release. The XRD
            # requires spec.autoscaler when any pool sets maxNodeCount.
            # Once the release is observed it stays composed even after
            # spec.autoscaler is removed: the release excludes Delete, so
            # dropping it would orphan a running autoscaler that keeps
            # scaling its old node groups against the nodeCount Crossplane
            # enforces on the now-fixed pools. With no autoscaled pools the
            # group list is empty and the chart renders no Deployment. The
            # token Secret Object is composed only while spec.autoscaler
            # names its source Secret; dropped, the copy stays on the
            # cluster because the Object excludes Delete too.
            if self.xr.spec.autoscaler or "release-cluster-autoscaler" in self.req.observed.resources:
                self.compose_provider_configs()
                if self.xr.spec.autoscaler:
                    self.compose_autoscaler_secret()
                self.compose_cluster_autoscaler()
        self.write_status()
        self.mark_readiness()

    def _cluster_ready(self) -> bool:
        return resource.get_condition(self.req.observed.resources.get("cluster"), "Ready").status == "True"

    def _dependents_observed(self) -> bool:
        """Whether the Ready-gated dependents were composed on a previous
        reconcile. The gate delays their first composition until the cluster
        is Ready, but must not drop them from desired state when the Ready
        condition transiently regresses - that would delete them, and the
        KubernetesNodeGroups are not orphaned, so their nodes would be
        deprovisioned with them. The dependents are composed as one block,
        so any observed member means the block was composed before; the
        node groups are the sentinel (the system group is always among
        them), with the ProviderConfig covering partially-applied states of
        the autoscaler sub-block."""
        observed = self.req.observed.resources
        return "provider-config-helm" in observed or any(name.startswith("node-pool-") for name in observed)

    def _observed_cluster_id(self) -> str | None:
        """The UpCloud UUID of the observed cluster, from the external-name
        annotation the provider sets once the cluster exists."""
        observed = self.req.observed.resources.get("cluster")
        if observed is None:
            return None
        d = resource.struct_to_dict(observed.resource)
        return d.get("metadata", {}).get("annotations", {}).get(_ANNOTATION_EXTERNAL_NAME) or None

    def compose_network(self) -> None:
        """Compose the cluster's dedicated SDN network. UKS requires a
        private network per cluster with DHCP on and its default route
        off."""
        resource.update(
            self.rsp.desired.resources["network"],
            networkv1alpha1.Network(
                spec=networkv1alpha1.Spec(
                    providerConfigRef=networkv1alpha1.ProviderConfigRef(
                        kind=self._cred_kind(),
                        name=self._cred_name(),
                    ),
                    forProvider=networkv1alpha1.ForProvider(
                        name=_name(self.xr.metadata),
                        zone=self.xr.spec.zone,
                        ipNetwork=networkv1alpha1.IpNetwork(
                            address=_NETWORK_CIDR,
                            dhcp=True,
                            dhcpDefaultRoute=False,
                            family=_NETWORK_FAMILY,
                        ),
                    ),
                ),
            ),
        )

    def compose_cluster(self) -> None:
        """Compose the UKS cluster. All node groups, the fixed system one
        included, are separate KubernetesNodeGroup resources composed after
        the cluster is Ready - UKS has no inline pools."""
        cluster = clusterv1alpha1.KubernetesCluster(
            spec=clusterv1alpha1.Spec(
                providerConfigRef=clusterv1alpha1.ProviderConfigRef(
                    kind=self._cred_kind(),
                    name=self._cred_name(),
                ),
                forProvider=clusterv1alpha1.ForProvider(
                    name=_name(self.xr.metadata),
                    zone=self.xr.spec.zone,
                    version=self.xr.spec.kubernetesVersion,
                    plan=_CLUSTER_PLAN,
                    controlPlaneIpFilter=_CONTROL_PLANE_IP_FILTER,
                    fetchKubeconfig=True,
                    networkSelector=clusterv1alpha1.NetworkSelector(
                        matchControllerRef=True,
                    ),
                ),
                writeConnectionSecretToRef=clusterv1alpha1.WriteConnectionSecretToRef(
                    name=_kubeconfig_secret_name(self.xr),
                ),
            ),
        )
        resource.update(self.rsp.desired.resources["cluster"], cluster)

    def compose_node_pools(self) -> None:
        """Compose a KubernetesNodeGroup for the injected system pool and
        each user-defined pool. Gated on the cluster being Ready: UKS only
        accepts node group operations on a running cluster, and the cluster
        UUID must be available for the selector."""
        resource.update(
            self.rsp.desired.resources[f"node-pool-{_SYSTEM_POOL_NAME}"],
            self._system_pool(),
        )
        for pool in self.xr.spec.nodePools:
            resource.update(
                self.rsp.desired.resources[f"node-pool-{pool.name}"],
                self._node_pool(pool),
            )

    def compose_provider_configs(self) -> None:
        """Compose provider-helm and provider-kubernetes ProviderConfigs
        pointing at the cluster's kubeconfig secret. The kubeconfig embeds
        client certificates, so no identity block is needed. They serve the
        cluster autoscaler's Helm release and the token Secret's Object."""
        kubeconfig_name = _kubeconfig_secret_name(self.xr)
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

    def compose_autoscaler_secret(self) -> None:
        """Copy the UpCloud API token to kube-system on the workload
        cluster for the autoscaler. UpCloud provisions no API credentials
        on UKS clusters, so unlike Civo there is nothing to reference.
        provider-kubernetes resolves the patchesFrom reference server-side
        at apply time, so the token appears in neither this function nor
        the Object's stored manifest."""
        autoscaler = self.xr.spec.autoscaler
        if autoscaler is None:  # compose() only calls this when set; keeps the type checker happy.
            return
        key = autoscaler.secretRef.key or _AUTOSCALER_SECRET_KEY
        resource.update(
            self.rsp.desired.resources["autoscaler-secret"],
            k8sobjv1alpha1.Object(
                metadata=metav1.ObjectMeta(namespace=_namespace(self.xr.metadata)),
                spec=k8sobjv1alpha1.Spec(
                    managementPolicies=_ORPHAN_MANAGEMENT,
                    providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                        kind="ProviderConfig",
                        name=_kubeconfig_secret_name(self.xr),
                    ),
                    references=[
                        k8sobjv1alpha1.Reference(
                            patchesFrom=k8sobjv1alpha1.PatchesFrom(
                                apiVersion="v1",
                                kind="Secret",
                                name=autoscaler.secretRef.name,
                                namespace=_namespace(self.xr.metadata),
                                fieldPath=f"data.{key}",
                            ),
                            toFieldPath=f"data.{_AUTOSCALER_SECRET_KEY}",
                        ),
                    ],
                    forProvider=k8sobjv1alpha1.ForProvider(
                        manifest={
                            "apiVersion": "v1",
                            "kind": "Secret",
                            "metadata": {
                                "name": _AUTOSCALER_SECRET_NAME,
                                "namespace": _AUTOSCALER_NAMESPACE,
                            },
                            "type": "Opaque",
                        },
                    ),
                ),
            ),
        )

    def _autoscaled_pools(self) -> list[v1alpha1.NodePool]:
        return [p for p in self.xr.spec.nodePools if p.maxNodeCount is not None]

    def compose_cluster_autoscaler(self) -> None:
        """Provision the Kubernetes cluster autoscaler so pools with
        maxNodeCount scale within their min/max. Node groups are addressed
        by name, which this function fixes to the pool name through the
        external-name annotation, so the group list needs no observed
        state; only UPCLOUD_CLUSTER_ID does, so the release waits for the
        cluster's UUID to be observed.

        The release is composed whenever the autoscaler sub-block is, even
        with no autoscaled pools: its management policies exclude Delete
        (see _ORPHAN_MANAGEMENT), so dropping it from desired state when
        the last pool goes fixed-size would orphan a running autoscaler
        that keeps scaling with its old node groups. Updating it to an
        empty group list instead makes the chart render no Deployment,
        removing the stale autoscaler."""
        cluster_id = self._observed_cluster_id()
        if cluster_id is None:
            return

        groups = [
            {
                "name": pool.name,
                "minSize": pool.minNodeCount if pool.minNodeCount is not None else pool.nodeCount,
                "maxSize": pool.maxNodeCount,
            }
            for pool in self._autoscaled_pools()
        ]

        resource.update(
            self.rsp.desired.resources["release-cluster-autoscaler"],
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
                            name=_AUTOSCALER_CHART_NAME,
                            repository=_AUTOSCALER_CHART_REPO,
                            version=_AUTOSCALER_CHART_VERSION,
                        ),
                        namespace=_AUTOSCALER_NAMESPACE,
                        values={
                            "cloudProvider": "upcloud",
                            # The upstream chart ships no UpCloud build;
                            # this is UpCloud's fork image (see the module
                            # docstring for the version skew).
                            "image": {
                                "repository": _AUTOSCALER_IMAGE_REPO,
                                "tag": _AUTOSCALER_IMAGE_TAG,
                            },
                            "autoscalingGroups": groups,
                            # The fork reads the cluster UUID and API token
                            # from the environment. The token comes from
                            # the Secret compose_autoscaler_secret copies;
                            # until that copy lands the pod restarts on the
                            # missing secretKeyRef and recovers by itself.
                            "extraEnv": {"UPCLOUD_CLUSTER_ID": cluster_id},
                            "extraEnvSecrets": {
                                "UPCLOUD_TOKEN": {
                                    "name": _AUTOSCALER_SECRET_NAME,
                                    "key": _AUTOSCALER_SECRET_KEY,
                                },
                            },
                        },
                    ),
                ),
            ),
        )

    def _system_pool(self) -> nodegroupv1alpha1.KubernetesNodeGroup:
        """The system node group for control-plane components."""
        return nodegroupv1alpha1.KubernetesNodeGroup(
            metadata=metav1.ObjectMeta(
                annotations={_ANNOTATION_EXTERNAL_NAME: _SYSTEM_POOL_NAME},
            ),
            spec=nodegroupv1alpha1.Spec(
                providerConfigRef=nodegroupv1alpha1.ProviderConfigRef(
                    kind=self._cred_kind(),
                    name=self._cred_name(),
                ),
                forProvider=nodegroupv1alpha1.ForProvider(
                    plan=_SYSTEM_POOL_PLAN,
                    nodeCount=_SYSTEM_POOL_NODES,
                    labels={_LABEL_POOL: _SYSTEM_POOL_NAME},
                    clusterSelector=nodegroupv1alpha1.ClusterSelector(
                        matchControllerRef=True,
                    ),
                ),
            ),
        )

    def _node_pool(self, pool: v1alpha1.NodePool) -> nodegroupv1alpha1.KubernetesNodeGroup:
        """Map an XR node pool to a KubernetesNodeGroup managed resource.

        KubernetesNodeGroup has no name in forProvider; the external name
        is the UKS node group name, so it is pinned to the pool name. That
        keeps the name deterministic for the autoscaler's node group list
        and the nodes it creates (the XRD restricts pool names to UKS's
        charset)."""
        labels = {_LABEL_POOL: pool.name}
        if pool.role == "GPU" and pool.gpu:
            labels[_LABEL_GPU] = pool.gpu.acceleratorType
        elif pool.role == "CPU":
            labels[_LABEL_CPU] = "true"

        fp = nodegroupv1alpha1.ForProvider(
            plan=pool.plan,
            labels=labels,
            clusterSelector=nodegroupv1alpha1.ClusterSelector(
                matchControllerRef=True,
            ),
        )

        # UKS unbundles storage from GPU and Cloud Native plans, so the
        # pool's disk size lands in the matching plan block. Starter and
        # Premium plans bundle a fixed disk, leaving it nowhere to go.
        # Sizing GPU disks matters: UKS's default GPU plan disk is too
        # small to hold an inference serving image, and a full disk evicts
        # every pod on the node on ephemeral-storage pressure.
        if pool.plan.startswith("GPU-"):
            fp.gpuPlan = nodegroupv1alpha1.GpuPlan(
                storageSize=pool.diskSizeGb,
                storageTier=_NODE_STORAGE_TIER,
            )
        elif pool.plan.startswith("CLOUDNATIVE-"):
            fp.cloudNativePlan = nodegroupv1alpha1.CloudNativePlan(
                storageSize=pool.diskSizeGb,
                storageTier=_NODE_STORAGE_TIER,
            )

        if pool.role == "GPU":
            fp.taint = [
                nodegroupv1alpha1.TaintItem(
                    key=_GPU_TAINT_KEY,
                    value=_GPU_TAINT_VALUE,
                    effect=_GPU_TAINT_EFFECT,
                ),
            ]
        elif pool.role == "CPU":
            fp.taint = [
                nodegroupv1alpha1.TaintItem(
                    key=_CPU_TAINT_KEY,
                    value=_CPU_TAINT_VALUE,
                    effect=_CPU_TAINT_EFFECT,
                ),
            ]

        spec = nodegroupv1alpha1.Spec(
            providerConfigRef=nodegroupv1alpha1.ProviderConfigRef(
                kind=self._cred_kind(),
                name=self._cred_name(),
            ),
            forProvider=fp,
        )

        # The autoscaler owns an autoscaled pool's node count, so nodeCount
        # only seeds it at creation (see _AUTOSCALED_POOL_MANAGEMENT). Fixed
        # pools keep it in forProvider so drift is corrected.
        if pool.maxNodeCount is not None:
            spec.managementPolicies = _AUTOSCALED_POOL_MANAGEMENT
            spec.initProvider = nodegroupv1alpha1.InitProvider(nodeCount=pool.nodeCount)
        else:
            fp.nodeCount = pool.nodeCount

        return nodegroupv1alpha1.KubernetesNodeGroup(
            metadata=metav1.ObjectMeta(
                annotations={_ANNOTATION_EXTERNAL_NAME: pool.name},
            ),
            spec=spec,
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
        )
        # The cluster's UpCloud UUID. compose-inference-cluster builds
        # account-unique gateway load balancer names from it. Assigned only
        # when observed: an explicit None would serialize as a null
        # tombstone under exclude_unset.
        cluster_id = self._observed_cluster_id()
        if cluster_id is not None:
            status.clusterId = cluster_id
        resource.update_status(self.rsp.desired.composite, status)

    def mark_readiness(self) -> None:
        """Mark composed resources as ready based on their observed
        conditions.

        The ProviderConfigs have no meaningful Ready condition and are
        always marked ready. All other resources (network, cluster, node
        groups, token Secret Object, autoscaler) are marked ready only once
        their observed Ready condition is True.
        """
        for r in self.rsp.desired.resources:
            if r in ("provider-config-helm", "provider-config-kubernetes"):
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE
                continue
            if resource.get_condition(self.req.observed.resources.get(r), "Ready").status == "True":
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE
