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

"""Compose a Civo Kubernetes (k3s) cluster with node pools.

This function provisions a Civo Kubernetes cluster on a dedicated network
and firewall, with a fixed system node pool and separate NodePool managed
resources for each user-defined pool. The system pool is inline on the
cluster resource (create-only, never changes). User pools are separate
managed resources that gate on the cluster being Ready, giving them an
independent lifecycle - they can be updated or removed without recreating
the cluster.

Civo's default Traefik ingress is removed from the cluster (the serving
stack installs Envoy Gateway as the ingress); the built-in metrics-server
stays.

Civo has no server-side node pool autoscaler, so pools with maxNodeCount
set are scaled by the Kubernetes cluster autoscaler installed on the
workload cluster as a Helm release. Its Civo cloud provider addresses node
groups by pool ID and authenticates to the Civo API with the
civo-api-access Secret that Civo provisions in kube-system on every
cluster, so no credentials are composed here.

Unlike Vultr - whose managed GPU Operator add-on this function's Vultr
counterpart gates cluster readiness on - Civo pre-installs no GPU
operator. Modelplane's serving stack installs it (driver included), and
that stack only starts composing after this XR is Ready, so no GPU
observer is composed here: gating readiness on a component the serving
stack installs would deadlock. GPU stack health is enforced downstream by
the serving stack's gpu-operator chart (wait=True) and the DRA driver
that depends on it.

No ModelCache RWX StorageClass is composed: Civo's civo-volume class is
ReadWriteOnce only, and Modelplane ships no cache storage of its own on
Civo.
"""

from typing import Literal

import grpc
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.infrastructure.civocluster import v1alpha1
from models.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1
from models.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from models.io.upbound.m.civo.kubernetes.cluster import v1beta1 as clusterv1beta1
from models.io.upbound.m.civo.kubernetes.nodepool import v1beta1 as nodepoolv1beta1
from models.io.upbound.m.civo.vpc.firewall import v1beta1 as firewallv1beta1
from models.io.upbound.m.civo.vpc.network import v1beta1 as networkv1beta1

# System pool injected into every Civo cluster to host control-plane
# components (Envoy Gateway, LeaderWorkerSet, cert-manager, etc.). Not part
# of the user-facing API - compose-inference-cluster only passes GPU pools.
# The size matches the Nebius and Vultr system pools' shape (16 GB memory);
# g4p.kube.small is the Performance tier's 4 vCPU / 16 GB Kubernetes size
# (verified against /v2/sizes - the Standard g4s tier tops out at 8 GB).
# The pool is inline on the cluster resource and create-time only, and the
# cluster autoscaler only scales user pools, so both nodes are provisioned
# up front.
_SYSTEM_POOL_NAME = "system"
_SYSTEM_POOL_SIZE = "g4p.kube.small"
_SYSTEM_POOL_NODES = 2

# Labels written on Civo node pools' nodes. compose-model-deployment reads
# these labels for GPU scheduling.
_LABEL_GPU = "modelplane.ai/gpu"
_LABEL_POOL = "modelplane.ai/pool"

# Secret type written to XR status. compose-inference-cluster reads this to
# wire the kubeconfig into a ClusterProviderConfig.
_SECRET_TYPE_KUBECONFIG = "Kubeconfig"

# Key within the connection secret the Kubernetes cluster resource writes.
# The provider publishes the decoded kubeconfig under this key once the
# cluster exists (spec.forProvider.writeKubeconfig must be true for the
# provider to read it from the Civo API).
_SECRET_KEY_KUBECONFIG = "kubeconfig"

# Taint applied to GPU node pools so only inference workloads that
# tolerate GPUs are scheduled on them.
_GPU_TAINT_KEY = "nvidia.com/gpu"
_GPU_TAINT_VALUE = "true"
_GPU_TAINT_EFFECT = "NoSchedule"

# Civo's default marketplace applications include the Traefik ingress
# controller, which would compete with the serving stack's Envoy Gateway
# for LoadBalancer traffic. A "-" prefix removes a default application at
# cluster creation. The built-in metrics-server default is kept.
_REMOVE_DEFAULT_APPS = "-traefik2-nodeport"

# CNI for the cluster, set at creation time only. Pinned to cilium: Civo
# is deprecating flannel (still its default), so new clusters start on
# the CNI that will remain supported.
_CNI = "cilium"

# The Kubernetes cluster autoscaler, installed on the workload cluster for
# pools with maxNodeCount set. Chart pin matches compose-eks-cluster's.
_AUTOSCALER_NAMESPACE = "kube-system"
_AUTOSCALER_CHART_REPO = "https://kubernetes.github.io/autoscaler"
_AUTOSCALER_CHART_NAME = "cluster-autoscaler"
_AUTOSCALER_CHART_VERSION = "9.57.0"
_AUTOSCALER_RELEASE_NAME = "mp-cluster-autoscaler"

# Secret the autoscaler's Civo cloud provider reads its API access from:
# civo-api-access in kube-system with api-key, api-url, cluster-id, and
# region keys. Civo provisions it on every cluster (its CCM and node-agent
# read it too), so it is referenced, never composed.
_API_ACCESS_SECRET_NAME = "civo-api-access"

# Annotation the provider sets on a managed resource with its external
# name - the resource's ID in Civo. The autoscaler addresses node groups
# by pool ID, so the Helm release is templated from these.
_ANNOTATION_EXTERNAL_NAME = "crossplane.io/external-name"

# Compose the autoscaler Helm release with Observe, Create and Update but
# not Delete. Deleting it with the XR would mean asking provider-helm to
# reach a cluster whose kubeconfig Secret has already been deleted,
# wedging its finalizer; orphaning it sidesteps that - the in-cluster
# resources die with the cluster.
_ManagementPolicy = Literal["Observe", "Create", "Update", "Delete", "LateInitialize", "*"]
_ORPHAN_MANAGEMENT: list[_ManagementPolicy] = ["Observe", "Create", "Update"]

# Node pool management policies that exclude LateInitialize, used for
# autoscaled pools: nodeCount is seeded via initProvider so it is applied
# only at creation and then left alone. The cluster autoscaler drives the
# pool's node count; without this Crossplane would keep reverting it to
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


# provider-helm reads the external-name annotation as the Helm release name,
# which otherwise defaults to the Release's generated metadata.name: a long XR
# name pushes that past Helm's 53-character limit. So Releases are named
# mp-<chart>, like compose-serving-stack's: each runs on a cluster of its own,
# so a fixed name can't collide.
_HELM_RELEASE_NAME_MAX_LENGTH = 53


def _release_name(installed: str | None, fixed: str) -> str:
    """The Helm release name for a Release: fixed, unless it was installed under another.

    installed is the Release's observed external name. Renaming an installed
    release would install its chart a second time, so that name is kept, unless
    it's longer than Helm allows: a release with that name was never installed.
    """
    if installed and len(installed) <= _HELM_RELEASE_NAME_MAX_LENGTH:
        return installed
    return fixed


def _kubeconfig_secret_name(xr: v1alpha1.CivoCluster) -> str:
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
        self.xr = v1alpha1.CivoCluster(**resource.struct_to_dict(req.observed.composite.resource))

    def _cred_kind(self) -> str:
        creds = self.xr.spec.credentials
        return creds.type if creds and creds.type else "ClusterProviderConfig"

    def _cred_name(self) -> str:
        creds = self.xr.spec.credentials
        return creds.name if creds and creds.name else "default"

    def compose(self) -> None:
        self.compose_network()
        self.compose_firewall()
        self.compose_cluster()
        if self._cluster_ready() or self._dependents_observed():
            self.compose_node_pools()
            self.compose_provider_configs()
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
        NodePools are not orphaned, so their nodes would be deprovisioned
        with them. The dependents are composed as one block, so any observed
        member means the block was composed before; the ProviderConfig is
        the sentinel, with the node pools covering partially-applied
        states."""
        observed = self.req.observed.resources
        return "provider-config-helm" in observed or any(name.startswith("node-pool-") for name in observed)

    def _observed_external_name(self, name: str) -> str | None:
        """The external name of an observed composed resource, from the
        external-name annotation its provider sets: a Civo resource's ID,
        or a Release's Helm release name."""
        observed = self.req.observed.resources.get(name)
        if observed is None:
            return None
        d = resource.struct_to_dict(observed.resource)
        return d.get("metadata", {}).get("annotations", {}).get(_ANNOTATION_EXTERNAL_NAME) or None

    def compose_network(self) -> None:
        """Compose a dedicated network for the cluster, for per-cluster
        isolation and clean teardown parity with the other clouds."""
        resource.update(
            self.rsp.desired.resources["network"],
            networkv1beta1.Network(
                spec=networkv1beta1.Spec(
                    providerConfigRef=networkv1beta1.ProviderConfigRef(
                        kind=self._cred_kind(),
                        name=self._cred_name(),
                    ),
                    forProvider=networkv1beta1.ForProvider(
                        label=_name(self.xr.metadata),
                        region=self.xr.spec.region,
                    ),
                ),
            ),
        )

    def compose_firewall(self) -> None:
        """Compose a firewall on the cluster's network. Civo's default rules
        allow the API server (6443) and web traffic (80/443) in, which is
        what the cluster and the serving stack's gateway load balancer
        need."""
        resource.update(
            self.rsp.desired.resources["firewall"],
            firewallv1beta1.Firewall(
                spec=firewallv1beta1.Spec(
                    providerConfigRef=firewallv1beta1.ProviderConfigRef(
                        kind=self._cred_kind(),
                        name=self._cred_name(),
                    ),
                    forProvider=firewallv1beta1.ForProvider(
                        name=_name(self.xr.metadata),
                        region=self.xr.spec.region,
                        createDefaultRules=True,
                        networkIdSelector=firewallv1beta1.NetworkIdSelector(
                            matchControllerRef=True,
                        ),
                    ),
                ),
            ),
        )

    def compose_cluster(self) -> None:
        """Compose the Civo cluster with the fixed system node pool.
        User-defined pools are separate NodePool resources composed after
        the cluster is Ready, giving them an independent lifecycle."""
        fp = clusterv1beta1.ForProvider(
            name=_name(self.xr.metadata),
            region=self.xr.spec.region,
            cni=_CNI,
            applications=_REMOVE_DEFAULT_APPS,
            writeKubeconfig=True,
            networkIdSelector=clusterv1beta1.NetworkIdSelector(
                matchControllerRef=True,
            ),
            firewallIdSelector=clusterv1beta1.FirewallIdSelector(
                matchControllerRef=True,
            ),
            pools=self._system_pool(),
        )
        # Left unset unless specified: the provider then picks Civo's
        # current default version.
        if self.xr.spec.kubernetesVersion is not None:
            fp.kubernetesVersion = self.xr.spec.kubernetesVersion
        cluster = clusterv1beta1.Cluster(
            spec=clusterv1beta1.Spec(
                providerConfigRef=clusterv1beta1.ProviderConfigRef(
                    kind=self._cred_kind(),
                    name=self._cred_name(),
                ),
                forProvider=fp,
                writeConnectionSecretToRef=clusterv1beta1.WriteConnectionSecretToRef(
                    name=_kubeconfig_secret_name(self.xr),
                ),
            ),
        )
        resource.update(self.rsp.desired.resources["cluster"], cluster)

    def compose_node_pools(self) -> None:
        """Compose a NodePool for each user-defined pool. Gated on the
        cluster being Ready so the cluster ID is available for the
        selector."""
        for pool in self.xr.spec.nodePools:
            resource.update(
                self.rsp.desired.resources[f"node-pool-{pool.name}"],
                self._node_pool(pool),
            )

    def compose_provider_configs(self) -> None:
        """Compose a provider-helm ProviderConfig pointing at the cluster's
        kubeconfig secret. The kubeconfig embeds client certificates, so no
        identity block is needed. It serves the cluster autoscaler's Helm
        release."""
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

    def _autoscaled_pools(self) -> list[v1alpha1.NodePool]:
        return [p for p in self.xr.spec.nodePools if p.maxNodeCount is not None]

    def compose_cluster_autoscaler(self) -> None:
        """Provision the Kubernetes cluster autoscaler so pools with
        maxNodeCount scale within their min/max, the way VKE's server-side
        autoscaler does on Vultr. Its Civo cloud provider addresses node
        groups by pool ID and reads API access from the civo-api-access
        Secret Civo provisions in kube-system, so only the Helm release is
        composed, templated from the observed pool IDs.

        The release is composed whenever the Ready-gated block is, even
        with no autoscaled pools: its management policies exclude Delete
        (see _ORPHAN_MANAGEMENT), so dropping it from desired state when
        the last pool goes fixed-size would orphan a running autoscaler
        that keeps scaling with its old node groups. Updating it to an
        empty group list instead makes the chart render no Deployment,
        removing the stale autoscaler."""
        # Node groups are addressed by pool ID, known only once each
        # NodePool exists. Pools whose ID is not yet observed are left out;
        # the next reconcile picks them up.
        groups = []
        for pool in self._autoscaled_pools():
            pool_id = self._observed_external_name(f"node-pool-{pool.name}")
            if pool_id is None:
                continue
            groups.append(
                {
                    "name": pool_id,
                    "minSize": pool.minNodeCount if pool.minNodeCount is not None else pool.nodeCount,
                    "maxSize": pool.maxNodeCount,
                },
            )

        resource.update(
            self.rsp.desired.resources["release-cluster-autoscaler"],
            helmv1beta1.Release(
                metadata=metav1.ObjectMeta(
                    namespace=_namespace(self.xr.metadata),
                    annotations={
                        _ANNOTATION_EXTERNAL_NAME: _release_name(
                            self._observed_external_name("release-cluster-autoscaler"), _AUTOSCALER_RELEASE_NAME
                        )
                    },
                ),
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
                            "cloudProvider": "civo",
                            "autoscalingGroups": groups,
                            # The chart renders the CIVO_* env vars itself
                            # for cloudProvider civo (adding them again via
                            # extraEnvSecrets duplicates them and the
                            # server-side apply rejects the Deployment);
                            # this points its built-in secretKeyRefs at the
                            # Secret composed above.
                            "secretKeyRefNameOverride": _API_ACCESS_SECRET_NAME,
                        },
                    ),
                ),
            ),
        )

    def _system_pool(self) -> clusterv1beta1.Pools:
        """The system node pool for control-plane components."""
        return clusterv1beta1.Pools(
            label=_SYSTEM_POOL_NAME,
            size=_SYSTEM_POOL_SIZE,
            nodeCount=_SYSTEM_POOL_NODES,
            labels={_LABEL_POOL: _SYSTEM_POOL_NAME},
        )

    def _node_pool(self, pool: v1alpha1.NodePool) -> nodepoolv1beta1.NodePool:
        """Map an XR node pool to a NodePool managed resource."""
        labels = {_LABEL_POOL: pool.name}
        if pool.role == "GPU" and pool.gpu:
            labels[_LABEL_GPU] = pool.gpu.acceleratorType

        fp = nodepoolv1beta1.ForProvider(
            label=pool.name,
            size=pool.size,
            # Region scopes every API call this resource makes; without it
            # the provider falls back to the account's default region.
            region=self.xr.spec.region,
            labels=labels,
            clusterIdSelector=nodepoolv1beta1.ClusterIdSelector(
                matchControllerRef=True,
            ),
        )

        if pool.role == "GPU":
            fp.taint = [
                nodepoolv1beta1.TaintItem(
                    key=_GPU_TAINT_KEY,
                    value=_GPU_TAINT_VALUE,
                    effect=_GPU_TAINT_EFFECT,
                ),
            ]

        spec = nodepoolv1beta1.Spec(
            providerConfigRef=nodepoolv1beta1.ProviderConfigRef(
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
            spec.initProvider = nodepoolv1beta1.InitProvider(nodeCount=pool.nodeCount)
        else:
            fp.nodeCount = pool.nodeCount

        return nodepoolv1beta1.NodePool(spec=spec)

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
        """Mark composed resources as ready based on their observed
        conditions.

        The ProviderConfig has no meaningful Ready condition and is
        always marked ready. All other resources (network, firewall,
        cluster, node pools, autoscaler) are marked ready only once their
        observed Ready condition is True.
        """
        for r in self.rsp.desired.resources:
            if r == "provider-config-helm":
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE
                continue
            if resource.get_condition(self.req.observed.resources.get(r), "Ready").status == "True":
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE
