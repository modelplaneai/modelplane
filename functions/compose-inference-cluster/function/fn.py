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

"""Compose an InferenceCluster.

This function orchestrates the internal XRs that make up an inference
cluster. It dispatches on the cluster source (GKE, Existing) to determine
how the cluster is obtained, then composes a ServingStack on it.

GPU node pools reference InferenceClasses. For provisioned (GKE)
clusters the class's provisioning block describes how to build the pool;
for BYO (Existing) clusters the class is a pure description of pools
that already exist. Either way, the class's resources block populates
status.gpuPools so the scheduler can match models.

For provisioned clusters, a system node pool is injected automatically
to host control-plane components (Envoy Gateway, Prometheus, etc.).
The system pool is not exposed in the user-facing API.
"""

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.inferenceclass import v1alpha1 as iclv1alpha1
from models.ai.modelplane.inferencecluster import v1alpha1
from models.ai.modelplane.infrastructure.ekscluster import v1alpha1 as eksv1alpha1
from models.ai.modelplane.infrastructure.gkecluster import v1alpha1 as gkev1alpha1
from models.ai.modelplane.infrastructure.servingstack import v1alpha1 as ssv1alpha1
from models.io.crossplane.m.kubernetes.clusterproviderconfig import (
    v1alpha1 as k8scpcv1alpha1,
)
from models.io.crossplane.protection.clusterusage import v1beta1 as clusterusagev1beta1
from models.io.crossplane.protection.usage import v1beta1 as usagev1beta1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# Cluster source discriminator values from the XRD enum.
CLUSTER_SOURCE_GKE = "GKE"
CLUSTER_SOURCE_EKS = "EKS"
CLUSTER_SOURCE_EXISTING = "Existing"

# GKE installs the NVIDIA driver here rather than at the default / root; the
# ServingStack passes it to the DRA driver so its kubelet plugin starts.
_GKE_NVIDIA_DRIVER_ROOT = "/home/kubernetes/bin/nvidia"

# Condition types and reasons for the InferenceCluster XR.
CONDITION_TYPE_CLUSTER_READY = "ClusterReady"
CONDITION_TYPE_BACKEND_READY = "BackendReady"

CONDITION_REASON_CLUSTER_RUNNING = "ClusterRunning"
CONDITION_REASON_PROVISIONING = "Provisioning"
CONDITION_REASON_WAITING_FOR_CLUSTER = "WaitingForCluster"
CONDITION_REASON_WAITING_FOR_CLASSES = "WaitingForClasses"
CONDITION_REASON_BACKEND_HEALTHY = "BackendHealthy"
CONDITION_REASON_INSTALLING = "Installing"
CONDITION_REASON_INVALID_NODE_POOL = "InvalidNodePool"

# Composed resource key for the backend XR.
BACKEND_RESOURCE_KEY = "serving-stack"

# Composed resource key for the ClusterUsage that blocks the InferenceCluster's
# deletion while ModelReplicas are scheduled to it.
_REPLICA_GUARD_RESOURCE_KEY = "usage-replicas"

# Label stamped on ModelReplicas by compose-model-deployment, carrying the name
# of the InferenceCluster the replica is scheduled to. Kept in sync with that
# function's _LABEL_CLUSTER.
_LABEL_CLUSTER = "modelplane.ai/cluster"

# Secret types that couple compose-gke-cluster (writer) to this function
# (reader) and compose-serving-stack (reader).
_SECRET_TYPE_KUBECONFIG = "Kubeconfig"
_SECRET_TYPE_GCP_SA_KEY = "GCPServiceAccountKey"

# The modelplane-system namespace. Used for the ServingStack XR,
# ClusterProviderConfig secretRefs, and status.namespace.
_NAMESPACE_SYSTEM = "modelplane-system"

# Identity type for GCP service account credentials.
_IDENTITY_TYPE_GCP = "GoogleApplicationCredentials"


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
        self.xr = v1alpha1.InferenceCluster(**resource.struct_to_dict(req.observed.composite.resource))
        # Resolved InferenceClasses, keyed by class name. Populated by
        # resolve_classes().
        self.classes: dict[str, iclv1alpha1.InferenceClass] = {}

    def compose(self):
        # The replica guard runs first, before any early return. It only
        # depends on which ModelReplicas reference this cluster, not on the
        # cluster's source or whether its classes resolve. Gating it behind
        # those would drop the guard on a reconcile where classes are
        # transiently unresolved, deleting the ClusterUsage and letting the
        # cluster be deleted while replicas still use it.
        self.compose_replica_guard()

        cluster = self.xr.spec.cluster
        if not cluster:
            response.warning(self.rsp, "spec.cluster is required")
            return

        if not self.resolve_classes():
            return

        source = cluster.source
        if source == CLUSTER_SOURCE_GKE:
            self.compose_gke(cluster.gke)
        elif source == CLUSTER_SOURCE_EKS:
            self.compose_eks(cluster.eks)
        elif source == CLUSTER_SOURCE_EXISTING:
            self.compose_existing(cluster.existing)
        else:
            response.warning(self.rsp, f"unsupported cluster source: {source}")

    def compose_replica_guard(self):
        """Block deletion of the InferenceCluster while ModelReplicas use it.

        Deleting an InferenceCluster out from under running ModelReplicas
        strands them: their workloads' provider-kubernetes Objects lose the
        ClusterProviderConfig (and the cluster) they need to finalize, and they
        wedge until their finalizers are removed by hand.

        ModelReplicas are namespaced and the InferenceCluster is cluster scoped,
        so a Usage can't reference the cluster from a replica: a namespaced
        Usage's `by` can't reach a namespaced replica from the cluster's scope,
        and a ClusterUsage's `by` can't reach a namespaced replica at all. So
        instead of protecting the cluster *by* the replicas, this protects it
        with a ClusterUsage that has no `by` at all. A reason-only Usage blocks
        deletion of its `of` resource until the Usage itself is gone.

        The guard is gated on observing ModelReplicas labelled for this cluster,
        across all namespaces. While any exist the ClusterUsage is composed and
        the cluster can't be deleted. When the last replica goes the function
        stops composing it; if a delete was already attempted, replayDeletion
        re-issues it once the ClusterUsage is gone.
        """
        response.require_resources(
            self.rsp,
            name="model-replicas",
            api_version="modelplane.ai/v1alpha1",
            kind="ModelReplica",
            match_labels={_LABEL_CLUSTER: self.xr.metadata.name},  # ty: ignore[unresolved-attribute, invalid-argument-type]  # metadata is always set on resources read from the API server
        )

        replicas = request.get_required_resources(self.req, "model-replicas")
        if not replicas:
            return

        resource.update(
            self.rsp.desired.resources[_REPLICA_GUARD_RESOURCE_KEY],
            clusterusagev1beta1.ClusterUsage(
                spec=clusterusagev1beta1.Spec(
                    of=clusterusagev1beta1.Of(
                        apiVersion="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        resourceRef=clusterusagev1beta1.ResourceRef(name=self.xr.metadata.name),  # ty: ignore[unresolved-attribute, invalid-argument-type]  # metadata is always set on resources read from the API server
                    ),
                    reason="ModelReplicas are scheduled to this InferenceCluster",
                    replayDeletion=True,
                ),
            ),
        )
        self.rsp.desired.resources[_REPLICA_GUARD_RESOURCE_KEY].ready = fnv1.READY_TRUE

    def resolve_classes(self) -> bool:
        """Declare and fetch every InferenceClass referenced by
        spec.nodePools[].className. Returns False if any is missing,
        in which case the function gates and waits."""
        pools = self.xr.spec.nodePools or []
        class_names = sorted({p.className for p in pools})

        for name in class_names:
            response.require_resources(
                self.rsp,
                name=f"class-{name}",
                api_version="modelplane.ai/v1alpha1",
                kind="InferenceClass",
                match_name=name,
            )

        missing: list[str] = []
        for name in class_names:
            d = request.get_required_resource(self.req, f"class-{name}")
            if d is None:
                missing.append(name)
                continue
            self.classes[name] = iclv1alpha1.InferenceClass.model_validate(d)

        if missing:
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_CLUSTER_READY,
                    status="False",
                    reason=CONDITION_REASON_WAITING_FOR_CLASSES,
                    message=f"Waiting for InferenceClasses: {', '.join(missing)}",
                ),
            )
            response.normal(self.rsp, f"Waiting for InferenceClasses: {', '.join(missing)}")
            return False

        return True

    def compose_gke(self, gke):
        """Compose an InferenceCluster backed by a Modelplane-provisioned
        GKE cluster. Composes the GKECluster XR, waits for it to be ready,
        then wires its secrets into the backend."""
        if not gke:
            response.warning(self.rsp, "GKE configuration is required when source is GKE")
            return

        self.compose_gke_cluster(gke)

        gke_ready = resource.get_condition(self.req.observed.resources.get("gke-cluster"), "Ready").status == "True"
        kubeconfig_secret = self.observed_gke_secret(_SECRET_TYPE_KUBECONFIG)
        sa_key = self.observed_gke_secret(_SECRET_TYPE_GCP_SA_KEY)
        backend_exists = BACKEND_RESOURCE_KEY in self.req.observed.resources

        if gke_ready and kubeconfig_secret:
            self.compose_cluster_provider_config(kubeconfig_secret.name, kubeconfig_secret.key, sa_key)

        backend_secrets = self.resolve_gke_backend_secrets(gke_ready, backend_exists)
        if backend_secrets or backend_exists:
            if backend_secrets:
                self.compose_serving_stack(backend_secrets, nvidia_driver_root=_GKE_NVIDIA_DRIVER_ROOT)
            self.compose_gke_usage()

        if gke_ready:
            self.rsp.desired.resources["gke-cluster"].ready = fnv1.READY_TRUE
            if not backend_exists:
                response.normal(self.rsp, "GKE cluster ready, composing backend")

        self.write_status(self.gpu_pools())
        self.derive_conditions(cluster_ready=gke_ready)

    def compose_eks(self, eks):
        """Compose an InferenceCluster backed by a Modelplane-provisioned
        EKS cluster. Composes the EKSCluster XR, waits for it to be ready,
        then wires its kubeconfig into the backend.

        The kubeconfig from ClusterAuth contains a static bearer token that
        the AWS provider refreshes periodically, and the cluster grants the
        AWS provider's principal cluster-admin via
        bootstrapClusterCreatorAdminPermissions. So the kubeconfig alone is
        enough to reach the cluster.
        """
        if not eks:
            response.warning(self.rsp, "EKS configuration is required when source is EKS")
            return

        self.compose_eks_cluster(eks)

        eks_ready = resource.get_condition(self.req.observed.resources.get("eks-cluster"), "Ready").status == "True"
        kubeconfig = self.observed_eks_secret(_SECRET_TYPE_KUBECONFIG)
        backend_exists = BACKEND_RESOURCE_KEY in self.req.observed.resources

        if eks_ready and kubeconfig:
            self.compose_cluster_provider_config(kubeconfig.name, kubeconfig.key)

        backend_secrets = self.resolve_eks_backend_secrets(eks_ready, backend_exists)
        if backend_secrets or backend_exists:
            if backend_secrets:
                self.compose_serving_stack(backend_secrets)
            self.compose_eks_usage()

        if eks_ready:
            self.rsp.desired.resources["eks-cluster"].ready = fnv1.READY_TRUE
            if not backend_exists:
                response.normal(self.rsp, "EKS cluster ready, composing backend")

        self.write_status(self.gpu_pools())
        self.derive_conditions(cluster_ready=eks_ready)

    def compose_existing(self, existing):
        """Compose an InferenceCluster backed by a user-supplied cluster.
        No gating needed — the kubeconfig secret is provided by the user."""
        if not existing:
            response.warning(self.rsp, "Existing cluster configuration is required when source is Existing")
            return

        identity = existing.identitySecretRef

        self.compose_cluster_provider_config(existing.secretRef.name, existing.secretRef.key, sa_key=identity)

        backend_secrets = [
            ssv1alpha1.Secret(type=_SECRET_TYPE_KUBECONFIG, name=existing.secretRef.name, key=existing.secretRef.key),
        ]
        if identity:
            backend_secrets.append(
                ssv1alpha1.Secret(type=_SECRET_TYPE_GCP_SA_KEY, name=identity.name, key=identity.key),
            )
        self.compose_serving_stack(backend_secrets)

        self.write_status(self.gpu_pools())
        self.derive_conditions(cluster_ready=True)

    def compose_serving_stack(
        self,
        backend_secrets: list[ssv1alpha1.Secret],
        nvidia_driver_root: str | None = None,
    ):
        """Compose a ServingStack XR with the given secrets.

        nvidia_driver_root is set for provisioned GKE clusters, where the NVIDIA
        driver lives off the default / path; the serving stack consumes it
        without inspecting its own cloud. Left None for EKS / existing clusters,
        which keep the ServingStack's default root.
        """
        spec = ssv1alpha1.Spec(secrets=backend_secrets)
        if nvidia_driver_root is not None:
            spec.nvidiaDriverRoot = nvidia_driver_root
        resource.update(
            self.rsp.desired.resources[BACKEND_RESOURCE_KEY],
            ssv1alpha1.ServingStack(
                metadata=metav1.ObjectMeta(
                    name=resource.child_name(self.xr.metadata.name, "serving-stack"),  # ty: ignore[unresolved-attribute, invalid-argument-type]  # metadata is always set on resources read from the API server
                    namespace=_NAMESPACE_SYSTEM,
                ),
                spec=spec,
            ),
        )

    def compose_cluster_provider_config(self, kubeconfig_name, kubeconfig_key, sa_key=None):
        """Compose a ClusterProviderConfig for provider-kubernetes so that
        ModelReplicas can create Objects on the remote cluster."""
        cpc = k8scpcv1alpha1.ClusterProviderConfig(
            metadata=metav1.ObjectMeta(name=resource.child_name(self.xr.metadata.name, "cluster-kubeconfig")),  # ty: ignore[unresolved-attribute, invalid-argument-type]  # metadata is always set on resources read from the API server
            spec=k8scpcv1alpha1.Spec(
                credentials=k8scpcv1alpha1.Credentials(
                    source="Secret",
                    secretRef=k8scpcv1alpha1.SecretRef(
                        namespace=_NAMESPACE_SYSTEM,
                        name=kubeconfig_name,
                        key=kubeconfig_key,
                    ),
                ),
            ),
        )
        if sa_key:
            cpc.spec.identity = k8scpcv1alpha1.Identity(
                type=_IDENTITY_TYPE_GCP,
                source="Secret",
                secretRef=k8scpcv1alpha1.SecretRef(
                    namespace=_NAMESPACE_SYSTEM,
                    name=sa_key.name,
                    key=sa_key.key,
                ),
            )
        resource.update(
            self.rsp.desired.resources["cluster-provider-config-kubernetes"],
            cpc,
        )
        self.rsp.desired.resources["cluster-provider-config-kubernetes"].ready = fnv1.READY_TRUE

    def write_status(self, gpu_pools):
        """Write the InferenceCluster status."""
        status = v1alpha1.Status(
            providerConfigRef=v1alpha1.ProviderConfigRef(
                name=resource.child_name(self.xr.metadata.name, "cluster-kubeconfig"),  # ty: ignore[unresolved-attribute, invalid-argument-type]  # metadata is always set on resources read from the API server
            ),
            namespace=_NAMESPACE_SYSTEM,
            gpuPools=gpu_pools,
        )
        cache_storage_class = self.observed_cache_storage_class()
        if cache_storage_class:
            status.cache = v1alpha1.CacheModel(storageClassName=cache_storage_class)
        gateway_address = self.observed_gateway_address()
        if gateway_address:
            status.gateway = v1alpha1.Gateway(address=gateway_address)
        resource.update_status(self.rsp.desired.composite, status)

    def derive_conditions(self, cluster_ready):
        """Derive ClusterReady and BackendReady conditions."""
        backend_ready = (
            resource.get_condition(self.req.observed.resources.get(BACKEND_RESOURCE_KEY), "Ready").status == "True"
        )
        if BACKEND_RESOURCE_KEY in self.rsp.desired.resources and backend_ready:
            self.rsp.desired.resources[BACKEND_RESOURCE_KEY].ready = fnv1.READY_TRUE

        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_CLUSTER_READY,
                status="True" if cluster_ready else "False",
                reason=CONDITION_REASON_CLUSTER_RUNNING if cluster_ready else CONDITION_REASON_PROVISIONING,
            ),
        )

        if not cluster_ready:
            backend_reason = CONDITION_REASON_WAITING_FOR_CLUSTER
        elif backend_ready:
            backend_reason = CONDITION_REASON_BACKEND_HEALTHY
        else:
            backend_reason = CONDITION_REASON_INSTALLING

        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_BACKEND_READY,
                status="True" if backend_ready else "False",
                reason=backend_reason,
            ),
        )

    def compose_gke_cluster(self, gke):
        """Compose a GKECluster XR.

        Combines the cluster-level config (project, region) with the
        GPU pools derived from the user's node pools + referenced classes.
        The system pool is injected by compose-gke-cluster.
        """
        gke_node_pools: list[gkev1alpha1.NodePool] = []

        for pool in self.xr.spec.nodePools or []:
            cls = self.classes.get(pool.className)
            if not cls or not cls.spec.provisioning or not cls.spec.provisioning.gke:
                msg = f"InferenceClass {pool.className} has no GKE provisioning block"
                response.set_conditions(
                    self.rsp,
                    resource.Condition(
                        typ=CONDITION_TYPE_CLUSTER_READY,
                        status="False",
                        reason=CONDITION_REASON_INVALID_NODE_POOL,
                        message=msg,
                    ),
                )
                response.warning(self.rsp, msg)
                return
            prov = cls.spec.provisioning.gke
            gke_node_pools.append(
                gkev1alpha1.NodePool(
                    name=pool.name,
                    role="GPU",
                    machineType=prov.machineType,
                    diskSizeGb=prov.diskSizeGb,
                    nodeCount=pool.nodeCount,
                    minNodeCount=pool.minNodeCount,
                    maxNodeCount=pool.maxNodeCount,
                    gpu=gkev1alpha1.Gpu(
                        acceleratorType=prov.accelerator.type,
                        acceleratorCount=prov.accelerator.count,
                    ),
                    zones=[gkev1alpha1.Zone(z) for z in pool.zones or []],
                )
            )

        resource.update(
            self.rsp.desired.resources["gke-cluster"],
            gkev1alpha1.GKECluster(
                metadata=metav1.ObjectMeta(
                    name=self.xr.metadata.name,  # ty: ignore[unresolved-attribute]  # metadata is always set on resources read from the API server
                    namespace=_NAMESPACE_SYSTEM,
                ),
                spec=gkev1alpha1.Spec(
                    project=gke.project,
                    region=gke.region,
                    kubernetesVersion=gke.kubernetesVersion,
                    nodePools=gke_node_pools,
                ),
            ),
        )

    def compose_eks_cluster(self, eks):
        """Compose an EKSCluster XR.

        Combines the cluster-level config (region) with GPU node pools
        derived from the user's node pools + referenced classes. The
        system pool is injected by compose-eks-cluster.
        """
        eks_node_pools: list[eksv1alpha1.NodePool] = []

        for pool in self.xr.spec.nodePools or []:
            cls = self.classes.get(pool.className)
            if not cls or not cls.spec.provisioning or not cls.spec.provisioning.eks:
                msg = f"InferenceClass {pool.className} has no EKS provisioning block"
                response.set_conditions(
                    self.rsp,
                    resource.Condition(
                        typ=CONDITION_TYPE_CLUSTER_READY,
                        status="False",
                        reason=CONDITION_REASON_INVALID_NODE_POOL,
                        message=msg,
                    ),
                )
                response.warning(self.rsp, msg)
                return
            prov = cls.spec.provisioning.eks
            node_pool = eksv1alpha1.NodePool(
                name=pool.name,
                role="GPU",
                instanceType=prov.instanceType,
                diskSizeGb=prov.diskSizeGb,
                nodeCount=pool.nodeCount,
                minNodeCount=pool.minNodeCount,
                maxNodeCount=pool.maxNodeCount,
                gpu=eksv1alpha1.Gpu(
                    acceleratorType=prov.accelerator.type,
                ),
                zones=[eksv1alpha1.Zone(z) for z in pool.zones or []],
            )
            # Only set capacityBlock when the pool has one. resource.update
            # serializes with exclude_unset, so leaving it unset keeps it out
            # of the EKSCluster spec rather than emitting capacityBlock: null.
            if pool.capacityBlock:
                node_pool.capacityBlock = eksv1alpha1.CapacityBlock(
                    capacityReservationId=pool.capacityBlock.capacityReservationId,
                )
            # Likewise only set fabric when the pool opts into one, so an
            # on-demand pool's EKSCluster spec stays free of fabric: None.
            if pool.fabric and pool.fabric != "None":
                node_pool.fabric = pool.fabric
            eks_node_pools.append(node_pool)

        resource.update(
            self.rsp.desired.resources["eks-cluster"],
            eksv1alpha1.EKSCluster(
                metadata=metav1.ObjectMeta(
                    name=self.xr.metadata.name,  # ty: ignore[unresolved-attribute]  # metadata is always set on resources read from the API server
                    namespace=_NAMESPACE_SYSTEM,
                ),
                spec=eksv1alpha1.Spec(
                    region=eks.region,
                    kubernetesVersion=eks.kubernetesVersion,
                    nodePools=eks_node_pools,
                ),
            ),
        )

    def compose_eks_usage(self):
        """Block EKSCluster deletion until the backend is deleted."""
        resource.update(
            self.rsp.desired.resources["usage-eks-by-backend"],
            usagev1beta1.Usage(
                metadata=metav1.ObjectMeta(namespace=_NAMESPACE_SYSTEM),
                spec=usagev1beta1.Spec(
                    of=usagev1beta1.Of(
                        apiVersion="infrastructure.modelplane.ai/v1alpha1",
                        kind="EKSCluster",
                        resourceSelector=usagev1beta1.ResourceSelectorModel(matchControllerRef=True),
                    ),
                    by=usagev1beta1.By(
                        apiVersion="infrastructure.modelplane.ai/v1alpha1",
                        kind="ServingStack",
                        resourceSelector=usagev1beta1.ResourceSelector(matchControllerRef=True),
                    ),
                    replayDeletion=True,
                ),
            ),
        )
        self.rsp.desired.resources["usage-eks-by-backend"].ready = fnv1.READY_TRUE

    def resolve_eks_backend_secrets(self, eks_ready, backend_exists) -> list[ssv1alpha1.Secret] | None:
        """Resolve secrets for the backend from EKSCluster status. Falls
        back to the observed backend's spec.secrets if EKSCluster secrets
        aren't available but the backend already exists."""
        eks_secrets = self.observed_eks_secrets()

        if eks_ready and eks_secrets:
            return [ssv1alpha1.Secret(type=s.type, name=s.name, key=s.key) for s in eks_secrets]

        if backend_exists:
            observed = self.req.observed.resources.get(BACKEND_RESOURCE_KEY)
            if observed:
                d = resource.struct_to_dict(observed.resource)
                observed_secrets = d.get("spec", {}).get("secrets", [])
                if observed_secrets:
                    return [ssv1alpha1.Secret(type=s["type"], name=s["name"], key=s["key"]) for s in observed_secrets]

        return None

    def observed_eks_secrets(self):
        """Read the EKSCluster's status.secrets from observed state."""
        eks_observed = self.req.observed.resources.get("eks-cluster")
        if not eks_observed:
            return None
        observed_eks = eksv1alpha1.EKSCluster.model_validate(resource.struct_to_dict(eks_observed.resource))
        if not observed_eks.status:
            return None
        return observed_eks.status.secrets

    def observed_eks_secret(self, secret_type):
        """Read a specific secret from the observed EKSCluster status."""
        eks_secrets = self.observed_eks_secrets()
        if not eks_secrets:
            return None
        return next((s for s in eks_secrets if s.type == secret_type), None)

    def compose_gke_usage(self):
        """Block GKECluster deletion until the backend is deleted."""
        resource.update(
            self.rsp.desired.resources["usage-gke-by-backend"],
            usagev1beta1.Usage(
                metadata=metav1.ObjectMeta(namespace=_NAMESPACE_SYSTEM),
                spec=usagev1beta1.Spec(
                    of=usagev1beta1.Of(
                        apiVersion="infrastructure.modelplane.ai/v1alpha1",
                        kind="GKECluster",
                        resourceSelector=usagev1beta1.ResourceSelectorModel(matchControllerRef=True),
                    ),
                    by=usagev1beta1.By(
                        apiVersion="infrastructure.modelplane.ai/v1alpha1",
                        kind="ServingStack",
                        resourceSelector=usagev1beta1.ResourceSelector(matchControllerRef=True),
                    ),
                    replayDeletion=True,
                ),
            ),
        )
        self.rsp.desired.resources["usage-gke-by-backend"].ready = fnv1.READY_TRUE

    def resolve_gke_backend_secrets(self, gke_ready, backend_exists) -> list[ssv1alpha1.Secret] | None:
        """Resolve secrets for the backend from GKECluster status. Falls
        back to the observed backend's spec.secrets if GKECluster secrets aren't
        available but the backend already exists."""
        gke_secrets = self.observed_gke_secrets()

        if gke_ready and gke_secrets:
            return [ssv1alpha1.Secret(type=s.type, name=s.name, key=s.key) for s in gke_secrets]

        if backend_exists:
            observed = self.req.observed.resources.get(BACKEND_RESOURCE_KEY)
            if observed:
                d = resource.struct_to_dict(observed.resource)
                observed_secrets = d.get("spec", {}).get("secrets", [])
                if observed_secrets:
                    return [ssv1alpha1.Secret(type=s["type"], name=s["name"], key=s["key"]) for s in observed_secrets]

        return None

    def gpu_pools(self):
        """Derive status.gpuPools from each node pool's class.

        The class declares the node's devices (DRA-style); the pool declares how
        many nodes. We copy the class's devices verbatim so
        ModelDeployment.nodeSelector can match against them, and record the node
        count for the scheduler's available-node gate.
        """
        gpu_pools = []
        for pool in self.xr.spec.nodePools or []:
            cls = self.classes.get(pool.className)
            if not cls or not cls.spec.devices:
                continue
            # Copy the class's devices verbatim. model_dump drops None fields,
            # keeping the typed attribute value objects
            # (string/version/bool/int) one-of clean. by_alias keeps DRA's wire
            # names (bool/int) rather than the generated bool_/int_ attributes,
            # so the published status matches the InferenceClass schema.
            devices = [d.model_dump(by_alias=True, exclude_none=True) for d in cls.spec.devices]
            gpu_pools.append(
                {
                    "name": pool.name,
                    "nodes": pool.maxNodeCount or pool.nodeCount,
                    "devices": devices,
                }
            )
        return gpu_pools

    def observed_gke_secrets(self):
        """Read the GKECluster's status.secrets from observed state."""
        gke_observed = self.req.observed.resources.get("gke-cluster")
        if not gke_observed:
            return None
        observed_gke = gkev1alpha1.GKECluster.model_validate(resource.struct_to_dict(gke_observed.resource))
        if not observed_gke.status:
            return None
        return observed_gke.status.secrets

    def observed_gke_secret(self, secret_type):
        """Read a specific secret from the observed GKECluster status."""
        gke_secrets = self.observed_gke_secrets()
        if not gke_secrets:
            return None
        return next((s for s in gke_secrets if s.type == secret_type), None)

    def observed_gateway_address(self):
        """Read the backend's gateway address from observed state.
        Uses dict access instead of a typed model so it works for any
        backend that follows the status.gateway.address contract."""
        observed = self.req.observed.resources.get(BACKEND_RESOURCE_KEY)
        if not observed:
            return None
        d = resource.struct_to_dict(observed.resource)
        return d.get("status", {}).get("gateway", {}).get("address")

    def observed_cache_storage_class(self):
        """The effective ModelCache RWX StorageClass name, relayed up so
        ModelCache can target it without reaching into the cluster XRs.

        For provisioned (GKE/EKS) clusters it comes from the backing cluster's
        status.cache.storageClassName, which reports the Modelplane-managed
        class. For Existing clusters there is no cluster XR, so it's the
        user-supplied name. None until the cluster XR reports it."""
        cluster = self.xr.spec.cluster
        if cluster.source == CLUSTER_SOURCE_GKE:
            return self._observed_cluster_cache_class("gke-cluster", gkev1alpha1.GKECluster)
        if cluster.source == CLUSTER_SOURCE_EKS:
            return self._observed_cluster_cache_class("eks-cluster", eksv1alpha1.EKSCluster)
        if cluster.source == CLUSTER_SOURCE_EXISTING and cluster.existing and cluster.existing.cache:
            return cluster.existing.cache.storageClassName
        return None

    def _observed_cluster_cache_class(self, key, model):
        """Read status.cache.storageClassName from an observed cluster XR."""
        observed = self.req.observed.resources.get(key)
        if not observed:
            return None
        cluster = model.model_validate(resource.struct_to_dict(observed.resource))
        if cluster.status and cluster.status.cache and cluster.status.cache.storageClassName:
            return cluster.status.cache.storageClassName
        return None
