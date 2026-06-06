"""Compose an InferenceCluster.

This function orchestrates the internal XRs that make up an inference
cluster. It dispatches on the cluster source (GKE, Existing) to determine
how the cluster is obtained, then composes a ServingStack on it.

GPU node pools reference InferenceClasses. For provisioned (GKE)
clusters the class's provisioning block describes how to build the pool;
for BYO (Existing) clusters the class is a pure description of pools
that already exist. Either way, the class's resources block populates
status.capacity.gpuPools so the scheduler can match models.

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
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.crossplane.protection.usage import v1beta1 as usagev1beta1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# Cluster source discriminator values from the XRD enum.
CLUSTER_SOURCE_GKE = "GKE"
CLUSTER_SOURCE_EKS = "EKS"
CLUSTER_SOURCE_EXISTING = "Existing"

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

# Secret types that couple compose-gke-cluster (writer) to this function
# (reader) and compose-serving-stack (reader).
_SECRET_TYPE_KUBECONFIG = "Kubeconfig"
_SECRET_TYPE_GCP_SA_KEY = "GCPServiceAccountKey"

# The modelplane-system namespace. Used for the ServingStack XR,
# ClusterProviderConfig secretRefs, and status.namespace.
_NAMESPACE_SYSTEM = "modelplane-system"

# Identity type for GCP service account credentials.
_IDENTITY_TYPE_GCP = "GoogleApplicationCredentials"


class FunctionRunner(grpcv1.FunctionRunnerService):
    """A FunctionRunner handles gRPC RunFunctionRequests."""

    def __init__(self):
        """Create a new FunctionRunner."""
        self.log = logging.get_logger()

    async def RunFunction(self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext) -> fnv1.RunFunctionResponse:
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

            provider_config = self.observed_provider_config_name()
            # Gate on the real network name: the GKECluster reports it only once
            # its Network is observed. The StorageClass can't pin to the bare XR
            # name — the VPC name carries a provider-generated suffix.
            network_name = self.gke_network_name()
            if network_name:
                self.compose_rwx_storage_class(gke, provider_config, network_name)

        backend_secrets = self.resolve_gke_backend_secrets(gke_ready, backend_exists)
        if backend_secrets or backend_exists:
            if backend_secrets:
                self.compose_serving_stack(backend_secrets)
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
                self.compose_kserve_backend(backend_secrets)
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

    def compose_serving_stack(self, backend_secrets: list[ssv1alpha1.Secret]):
        """Compose a ServingStack XR with the given secrets."""
        resource.update(
            self.rsp.desired.resources[BACKEND_RESOURCE_KEY],
            ssv1alpha1.ServingStack(
                metadata=metav1.ObjectMeta(
                    name=resource.child_name(self.xr.metadata.name, "serving-stack"),
                    namespace=_NAMESPACE_SYSTEM,
                ),
                spec=ssv1alpha1.Spec(secrets=backend_secrets),
            ),
        )

    def compose_cluster_provider_config(self, kubeconfig_name, kubeconfig_key, sa_key=None):
        """Compose a ClusterProviderConfig for provider-kubernetes so that
        ModelReplicas can create Objects on the remote cluster."""
        cpc = k8scpcv1alpha1.ClusterProviderConfig(
            metadata=metav1.ObjectMeta(name=resource.child_name(self.xr.metadata.name, "cluster-kubeconfig")),
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

    def observed_provider_config_name(self):
        """The ClusterProviderConfig name to reference from the StorageClass
        Object. Prefer the observed resource's actual name so it stays correct
        if the naming scheme ever changes; fall back to the derived name on the
        first reconcile, before the CPC is observed."""
        observed = self.req.observed.resources.get("cluster-provider-config-kubernetes")
        if observed:
            cpc = k8scpcv1alpha1.ClusterProviderConfig.model_validate(resource.struct_to_dict(observed.resource))
            if cpc.metadata and cpc.metadata.name:
                return cpc.metadata.name
        return resource.child_name(self.xr.metadata.name, "cluster-kubeconfig")

    def gke_network_name(self):
        """The VPC name the GKECluster reports in status, for pinning the
        Filestore StorageClass to the right network.

        Read from the observed GKECluster's status.network.name (populated by
        compose-gke-cluster from the composed Network's external-name). The GCP
        VPC name carries a provider-generated suffix, so it cannot be derived
        from the XR name. None until the GKECluster observes its network; the
        StorageClass is gated on this being present.
        """
        observed = self.req.observed.resources.get("gke-cluster")
        if not observed:
            return None
        gke = gkev1alpha1.GKECluster.model_validate(resource.struct_to_dict(observed.resource))
        if gke.status and gke.status.network and gke.status.network.name:
            return gke.status.network.name
        return None

    def compose_rwx_storage_class(self, gke, provider_config: str, network_name: str):
        """Compose a Filestore RWX StorageClass when the GKE cache uses the
        managed default. Filestore CSI defaults to the `default` VPC → PVCs
        hang; pin parameters.network to our VPC. StorageClass has no Ready
        condition, so use SuccessfulCreate (DeriveFromObject would hang)."""
        cache = gke.cache
        sc_name = cache.storageClassName if (cache and cache.storageClassName) else "modelplane-rwx"
        if sc_name != "modelplane-rwx":
            return  # admin-provided class; don't manage it
        manifest = {
            "apiVersion": "storage.k8s.io/v1",
            "kind": "StorageClass",
            "metadata": {"name": sc_name},
            "provisioner": "filestore.csi.storage.gke.io",
            "parameters": {"tier": "enterprise", "network": network_name},
            "volumeBindingMode": "Immediate",
            "allowVolumeExpansion": True,
        }
        resource.update(
            self.rsp.desired.resources["storage-class-rwx"],
            k8sobjv1alpha1.Object(
                metadata=metav1.ObjectMeta(namespace=_NAMESPACE_SYSTEM),
                spec=k8sobjv1alpha1.Spec(
                    providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                        kind="ClusterProviderConfig",
                        name=provider_config,
                    ),
                    readiness=k8sobjv1alpha1.Readiness(policy="SuccessfulCreate"),
                    forProvider=k8sobjv1alpha1.ForProvider(manifest=manifest),
                ),
            ),
        )
        self.rsp.desired.resources["storage-class-rwx"].ready = fnv1.READY_TRUE

    def write_status(self, gpu_pools):
        """Write the InferenceCluster status."""
        status = v1alpha1.Status(
            providerConfigRef=v1alpha1.ProviderConfigRef(
                name=resource.child_name(self.xr.metadata.name, "cluster-kubeconfig"),
            ),
            namespace=_NAMESPACE_SYSTEM,
            capacity=v1alpha1.CapacityModel(
                gpuPools=gpu_pools,
            ),
        )
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
                    zones=list(pool.zones or []),
                )
            )

        resource.update(
            self.rsp.desired.resources["gke-cluster"],
            gkev1alpha1.GKECluster(
                metadata=metav1.ObjectMeta(
                    name=self.xr.metadata.name,
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
            eks_node_pools.append(
                eksv1alpha1.NodePool(
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
                    zones=list(pool.zones or []),
                )
            )

        resource.update(
            self.rsp.desired.resources["eks-cluster"],
            eksv1alpha1.EKSCluster(
                metadata=metav1.ObjectMeta(
                    name=self.xr.metadata.name,
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
                        kind="KServeBackend",
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
        """Derive status.capacity.gpuPools from each node pool's class.

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
            # (string/version/boolean/integer) one-of clean.
            devices = [d.model_dump(exclude_none=True) for d in cls.spec.devices]
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
