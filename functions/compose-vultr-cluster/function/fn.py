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
separate KubernetesNodePool managed resources for each user-defined pool.
The system pool is inline on the Kubernetes cluster resource (create-only,
never changes). User pools are separate managed resources that gate on the
cluster being Ready, giving them an independent lifecycle - they can be
updated or removed without recreating the cluster.

Once node pools are composed, a provider-kubernetes ProviderConfig and a
GPU-observer Object are added. The Object reads the nvidia-operator-validator
DaemonSet on the workload cluster using a CEL readiness query. VultrCluster
is only marked Ready when this DaemonSet is fully ready, meaning the GPU
stack is validated and nvidia.com/gpu resources are advertised before the
serving stack starts scheduling inference workloads.

Node pool autoscaling is served by VKE itself (the pool's autoScaler block),
so no in-cluster autoscaler is composed. No ModelCache RWX StorageClass is
composed either: VKE's built-in vultr-vfs-storage class (Vultr File System)
is not usable on GPU nodes, and Modelplane ships no cache storage of its own
on Vultr.
"""

import grpc
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.infrastructure.vultrcluster import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.crossplane.m.kubernetes.providerconfig import v1alpha1 as k8spcv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from models.io.upbound.m.vultr.vke.kubernetes import v1beta1 as vkev1beta1
from models.io.upbound.m.vultr.vke.kubernetesnodepool import v1beta1 as vkenodepoolv1beta1

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

# Silences the device plugin of VKE's managed GPU Operator add-on on
# Modelplane's GPU nodes: the operator respects a pre-set false label
# and won't run the plugin there, leaving the DRA driver's
# ResourceSlices as the sole GPU allocator. Without it, the plugin's
# nvidia.com/gpu ledger and the ResourceSlices would book the same
# physical devices independently. The add-on's DCGM metrics and GPU
# feature discovery keep running; only the competing allocator goes.
_LABEL_DEVICE_PLUGIN_KEY = "nvidia.com/gpu.deploy.device-plugin"
_LABEL_DEVICE_PLUGIN_VALUE = "false"

# Secret type written to XR status. compose-inference-cluster reads this to
# wire the kubeconfig into a ClusterProviderConfig.
_SECRET_TYPE_KUBECONFIG = "Kubeconfig"

# Key within the connection secret the Kubernetes cluster resource writes.
# The provider publishes the decoded kubeconfig under this key once the
# cluster exists.
_SECRET_KEY_KUBECONFIG = "kubeconfig"

# Taint applied to GPU node pools so only inference workloads that
# tolerate GPUs are scheduled on them.
_GPU_TAINT_KEY = "nvidia.com/gpu"
_GPU_TAINT_VALUE = "true"
_GPU_TAINT_EFFECT = "NoSchedule"

# Vultr pre-installs the NVIDIA GPU Operator on every VKE GPU cluster. The
# operator's validator DaemonSet runs a four-stage init sequence (driver →
# CUDA → device-plugin → full validation). When all pods in the DaemonSet
# are ready the GPU stack is validated and nvidia.com/gpu resources are
# advertised on the node. VultrCluster gates its own readiness on this
# signal so the serving stack never starts before GPUs are available.
_GPU_OPERATOR_NAMESPACE = "gpu-operator"
_GPU_OPERATOR_VALIDATOR_DS = "nvidia-operator-validator"
# CEL DaemonSet readiness: at least one pod scheduled and all pods ready.
# has() guards against missing status fields on early reconciles.
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
            self.compose_provider_config()
            self.compose_gpu_observer()
        self.write_status()
        self.mark_readiness()

    def _cluster_ready(self) -> bool:
        return resource.get_condition(self.req.observed.resources.get("cluster"), "Ready").status == "True"

    def _dependents_observed(self) -> bool:
        """Whether the Ready-gated dependents were composed on a previous
        reconcile. The gate delays their first composition until the cluster
        is Ready, but must not drop them from desired state when the Ready
        condition transiently regresses - that would delete them, and the
        KubernetesNodePools are not orphaned, so their nodes would be
        deprovisioned with them. The dependents are composed as one block, so
        any observed member means the block was composed before; the
        ProviderConfig is the sentinel, with the node pools covering
        partially-applied states."""
        observed = self.req.observed.resources
        return "provider-config-kubernetes" in observed or any(name.startswith("node-pool-") for name in observed)

    def compose_cluster(self) -> None:
        """Compose the VKE cluster with the fixed system node pool.
        User-defined pools are separate KubernetesNodePool resources composed
        after the cluster is Ready, giving them an independent lifecycle."""
        cluster = vkev1beta1.Kubernetes(
            spec=vkev1beta1.Spec(
                providerConfigRef=vkev1beta1.ProviderConfigRef(
                    kind=self._cred_kind(),
                    name=self._cred_name(),
                ),
                forProvider=vkev1beta1.ForProvider(
                    label=_name(self.xr.metadata),
                    region=self.xr.spec.region,
                    version=self.xr.spec.kubernetesVersion,
                    haControlplanes=True,
                    nodePools=self._system_pool(),
                ),
                writeConnectionSecretToRef=vkev1beta1.WriteConnectionSecretToRef(
                    name=_kubeconfig_secret_name(self.xr),
                ),
            ),
        )
        resource.update(self.rsp.desired.resources["cluster"], cluster)

    def compose_node_pools(self) -> None:
        """Compose a KubernetesNodePool for each user-defined pool. Gated on
        the cluster being Ready so the cluster ID is available for the
        selector."""
        for pool in self.xr.spec.nodePools:
            resource.update(
                self.rsp.desired.resources[f"node-pool-{pool.name}"],
                self._node_pool(pool),
            )

    def compose_provider_config(self) -> None:
        """Compose a provider-kubernetes ProviderConfig pointing at the VKE
        kubeconfig secret. The kubeconfig embeds client certificates, so no
        identity block is needed. It serves the gpu-observer Object."""
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

    def _system_pool(self) -> vkev1beta1.NodePools:
        """The system node pool for control-plane components."""
        return vkev1beta1.NodePools(
            label=_SYSTEM_POOL_NAME,
            plan=_SYSTEM_POOL_PLAN,
            nodeQuantity=_SYSTEM_POOL_MIN_NODES,
            autoScaler=True,
            minNodes=_SYSTEM_POOL_MIN_NODES,
            maxNodes=_SYSTEM_POOL_MAX_NODES,
            labels=[vkev1beta1.Label(key=_LABEL_POOL, value=_SYSTEM_POOL_NAME)],
        )

    def _node_pool(self, pool: v1alpha1.NodePool) -> vkenodepoolv1beta1.KubernetesNodePool:
        """Map an XR node pool to a KubernetesNodePool managed resource."""
        labels = [vkenodepoolv1beta1.Label(key=_LABEL_POOL, value=pool.name)]
        if pool.role == "GPU" and pool.gpu:
            labels.append(vkenodepoolv1beta1.Label(key=_LABEL_GPU, value=pool.gpu.acceleratorType))
            labels.append(vkenodepoolv1beta1.Label(key=_LABEL_DEVICE_PLUGIN_KEY, value=_LABEL_DEVICE_PLUGIN_VALUE))

        fp = vkenodepoolv1beta1.ForProvider(
            label=pool.name,
            plan=pool.plan,
            nodeQuantity=pool.nodeCount,
            labels=labels,
            clusterIdSelector=vkenodepoolv1beta1.ClusterIdSelector(
                matchControllerRef=True,
            ),
        )

        if pool.role == "GPU":
            fp.taints = [
                vkenodepoolv1beta1.Taint(
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

        return vkenodepoolv1beta1.KubernetesNodePool(
            spec=vkenodepoolv1beta1.Spec(
                providerConfigRef=vkenodepoolv1beta1.ProviderConfigRef(
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
        )
        resource.update_status(self.rsp.desired.composite, status)

    def mark_readiness(self) -> None:
        """Mark composed resources as ready based on their observed conditions.

        The ProviderConfig has no meaningful Ready condition and is always
        marked ready. All other resources (cluster, node pools, gpu-observer)
        are marked ready only once their observed Ready condition is True.
        The gpu-observer Object uses DeriveFromCelQuery, so the XR only
        becomes Ready once the nvidia-operator-validator DaemonSet is fully
        rolled out.
        """
        for r in self.rsp.desired.resources:
            if r == "provider-config-kubernetes":
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE
                continue
            if resource.get_condition(self.req.observed.resources.get(r), "Ready").status == "True":
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE
