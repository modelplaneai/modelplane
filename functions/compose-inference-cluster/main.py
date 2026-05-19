"""Compose an InferenceCluster.

This function orchestrates the internal XRs that make up an inference
cluster. It dispatches on the cluster source (GKE, Existing) to determine
how the cluster is obtained, then composes a KServeBackend on it.

GPU node pools reference InferenceClasses. For provisioned (GKE)
clusters the class's provisioning block describes how to build the pool;
for BYO (Existing) clusters the class is a pure description of pools
that already exist. Either way, the class's resources block populates
status.capacity.gpuPools so the scheduler can match models.

For provisioned clusters, a system node pool is injected automatically
to host control-plane components (Envoy Gateway, KEDA, KServe etc.).
The system pool is not exposed in the user-facing API.
"""

from crossplane.function import request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .lib import conditions, metadata, secrets
from .lib import resource as libresource
from .model.ai.modelplane.inferenceclass import v1alpha1 as iclv1alpha1
from .model.ai.modelplane.inferencecluster import v1alpha1
from .model.ai.modelplane.infrastructure.gkecluster import v1alpha1 as gkev1alpha1
from .model.ai.modelplane.infrastructure.kservebackend import v1alpha1 as kssv1alpha1
from .model.io.crossplane.m.kubernetes.clusterproviderconfig import (
    v1alpha1 as k8scpcv1alpha1,
)
from .model.io.crossplane.protection.usage import v1beta1 as usagev1beta1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# KServe version installed on remote clusters. Hardcoded as an internal
# implementation detail — users don't choose or see this.
KSERVE_VERSION = "v0.16.0"

# Cluster source discriminator values from the XRD enum.
CLUSTER_SOURCE_GKE = "GKE"
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

# Composed resource key for the backend XR.
BACKEND_RESOURCE_KEY = "kserve-backend"

# Hardcoded system pool config. Provisioned for every GKE cluster to
# host control-plane components (Envoy Gateway, KEDA, etc.). Not exposed
# in the user-facing API.
SYSTEM_POOL_NAME = "system"
SYSTEM_POOL_MACHINE_TYPE = "e2-standard-4"
SYSTEM_POOL_NODE_COUNT = 1
SYSTEM_POOL_MIN_NODE_COUNT = 1
SYSTEM_POOL_MAX_NODE_COUNT = 2


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
            conditions.set_condition(
                self.rsp,
                CONDITION_TYPE_CLUSTER_READY,
                False,
                CONDITION_REASON_WAITING_FOR_CLASSES,
                f"Waiting for InferenceClasses: {', '.join(missing)}",
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

        gke_ready = conditions.has_condition(self.req, "gke-cluster", "Ready")
        kubeconfig = self.observed_gke_secret(secrets.SECRET_TYPE_KUBECONFIG)
        sa_key = self.observed_gke_secret(secrets.SECRET_TYPE_GCP_SA_KEY)
        cpc_exists = "cluster-provider-config-kubernetes" in self.req.observed.resources
        backend_exists = BACKEND_RESOURCE_KEY in self.req.observed.resources

        if (gke_ready and kubeconfig) or cpc_exists:
            self.compose_cluster_provider_config(
                kubeconfig.name if kubeconfig else "",
                kubeconfig.key if kubeconfig else "",
                sa_key,
            )

        backend_secrets = self.resolve_gke_backend_secrets(gke_ready, backend_exists)
        if backend_secrets or backend_exists:
            if backend_secrets:
                self.compose_kserve_backend(backend_secrets)
            self.compose_gke_usage()

        if gke_ready:
            self.rsp.desired.resources["gke-cluster"].ready = fnv1.READY_TRUE
            if not backend_exists:
                response.normal(self.rsp, "GKE cluster ready, composing backend")

        self.write_status(self.gpu_pools())
        self.derive_conditions(cluster_ready=gke_ready)

    def compose_existing(self, existing):
        """Compose an InferenceCluster backed by a user-supplied cluster.
        No gating needed — the kubeconfig secret is provided by the user."""
        if not existing:
            response.warning(self.rsp, "Existing cluster configuration is required when source is Existing")
            return

        identity = existing.identitySecretRef

        self.compose_cluster_provider_config(existing.secretRef.name, existing.secretRef.key, sa_key=identity)

        backend_secrets = [
            kssv1alpha1.Secret(
                type=secrets.SECRET_TYPE_KUBECONFIG, name=existing.secretRef.name, key=existing.secretRef.key
            ),
        ]
        if identity:
            backend_secrets.append(
                kssv1alpha1.Secret(type=secrets.SECRET_TYPE_GCP_SA_KEY, name=identity.name, key=identity.key),
            )
        self.compose_kserve_backend(backend_secrets)

        self.write_status(self.gpu_pools())
        self.derive_conditions(cluster_ready=True)

    def compose_kserve_backend(self, backend_secrets: list[kssv1alpha1.Secret]):
        """Compose a KServeBackend XR with the given secrets."""
        resource.update(
            self.rsp.desired.resources[BACKEND_RESOURCE_KEY],
            kssv1alpha1.KServeBackend(
                metadata=metav1.ObjectMeta(
                    name=f"{self.xr.metadata.name}-kserve",
                    namespace=metadata.NAMESPACE_SYSTEM,
                ),
                spec=kssv1alpha1.Spec(
                    versions=kssv1alpha1.Versions(kserve=KSERVE_VERSION),
                    secrets=backend_secrets,
                ),
            ),
        )

    def compose_cluster_provider_config(self, kubeconfig_name, kubeconfig_key, sa_key=None):
        """Compose a ClusterProviderConfig for provider-kubernetes so that
        ModelReplicas can create Objects on the remote cluster."""
        cpc = k8scpcv1alpha1.ClusterProviderConfig(
            metadata=metav1.ObjectMeta(name=f"{self.xr.metadata.name}-cluster-kubeconfig"),
            spec=k8scpcv1alpha1.Spec(
                credentials=k8scpcv1alpha1.Credentials(
                    source="Secret",
                    secretRef=k8scpcv1alpha1.SecretRef(
                        namespace=metadata.NAMESPACE_SYSTEM,
                        name=kubeconfig_name,
                        key=kubeconfig_key,
                    ),
                ),
            ),
        )
        if sa_key:
            cpc.spec.identity = k8scpcv1alpha1.Identity(
                type="GoogleApplicationCredentials",
                source="Secret",
                secretRef=k8scpcv1alpha1.SecretRef(
                    namespace=metadata.NAMESPACE_SYSTEM,
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
                name=f"{self.xr.metadata.name}-cluster-kubeconfig",
            ),
            namespace=metadata.NAMESPACE_SYSTEM,
            capacity=v1alpha1.Capacity(
                gpuPools=gpu_pools,
            ),
        )
        gateway_address = self.observed_gateway_address()
        if gateway_address:
            status.gateway = v1alpha1.Gateway(address=gateway_address)
        libresource.update_status(self.rsp.desired.composite, status)

    def derive_conditions(self, cluster_ready):
        """Derive ClusterReady and BackendReady conditions."""
        backend_ready = conditions.has_condition(self.req, BACKEND_RESOURCE_KEY, "Ready")
        if BACKEND_RESOURCE_KEY in self.rsp.desired.resources and backend_ready:
            self.rsp.desired.resources[BACKEND_RESOURCE_KEY].ready = fnv1.READY_TRUE

        conditions.set_condition(
            self.rsp,
            CONDITION_TYPE_CLUSTER_READY,
            cluster_ready,
            CONDITION_REASON_CLUSTER_RUNNING if cluster_ready else CONDITION_REASON_PROVISIONING,
        )

        if not cluster_ready:
            backend_reason = CONDITION_REASON_WAITING_FOR_CLUSTER
        elif backend_ready:
            backend_reason = CONDITION_REASON_BACKEND_HEALTHY
        else:
            backend_reason = CONDITION_REASON_INSTALLING

        conditions.set_condition(self.rsp, CONDITION_TYPE_BACKEND_READY, backend_ready, backend_reason)

    def compose_gke_cluster(self, gke):
        """Compose a GKECluster XR.

        Combines the cluster-level config (project, region) with the
        hardcoded system pool and the GPU pools derived from the user's
        node pools + referenced classes.
        """
        gke_node_pools: list[gkev1alpha1.NodePool] = [self.system_pool_for_gke()]

        for pool in self.xr.spec.nodePools or []:
            cls = self.classes.get(pool.className)
            if not cls or not cls.spec.provisioning or not cls.spec.provisioning.gke:
                # Class has no GKE provisioning block - can't provision
                # this pool. Skip rather than fail; capacity reporting
                # still works for pre-existing pools.
                continue
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
                        memory=cls.spec.resources.gpu.memory,
                    ),
                    zones=list(pool.zones or []),
                )
            )

        resource.update(
            self.rsp.desired.resources["gke-cluster"],
            gkev1alpha1.GKECluster(
                metadata=metav1.ObjectMeta(
                    name=self.xr.metadata.name,
                    namespace=metadata.NAMESPACE_SYSTEM,
                ),
                spec=gkev1alpha1.Spec(
                    project=gke.project,
                    region=gke.region,
                    kubernetesVersion=gke.kubernetesVersion,
                    nodePools=gke_node_pools,
                ),
            ),
        )

    def system_pool_for_gke(self) -> gkev1alpha1.NodePool:
        """Hardcoded system pool config for GKE clusters. Hosts the
        Envoy Gateway, KEDA, KServe controller, etc."""
        return gkev1alpha1.NodePool(
            name=SYSTEM_POOL_NAME,
            role="System",
            machineType=SYSTEM_POOL_MACHINE_TYPE,
            nodeCount=SYSTEM_POOL_NODE_COUNT,
            minNodeCount=SYSTEM_POOL_MIN_NODE_COUNT,
            maxNodeCount=SYSTEM_POOL_MAX_NODE_COUNT,
        )

    def compose_gke_usage(self):
        """Block GKECluster deletion until the backend is deleted."""
        resource.update(
            self.rsp.desired.resources["usage-gke-by-backend"],
            usagev1beta1.Usage(
                metadata=metav1.ObjectMeta(namespace=metadata.NAMESPACE_SYSTEM),
                spec=usagev1beta1.Spec(
                    of=usagev1beta1.Of(
                        apiVersion="infrastructure.modelplane.ai/v1alpha1",
                        kind="GKECluster",
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
        self.rsp.desired.resources["usage-gke-by-backend"].ready = fnv1.READY_TRUE

    def resolve_gke_backend_secrets(self, gke_ready, backend_exists) -> list[kssv1alpha1.Secret] | None:
        """Resolve secrets for the backend from GKECluster status. Falls
        back to the observed backend's spec.secrets if GKECluster secrets aren't
        available but the backend already exists."""
        gke_secrets = self.observed_gke_secrets()

        if gke_ready and gke_secrets:
            return [kssv1alpha1.Secret(type=s.type, name=s.name, key=s.key) for s in gke_secrets]

        if backend_exists:
            observed = self.req.observed.resources.get(BACKEND_RESOURCE_KEY)
            if observed:
                d = resource.struct_to_dict(observed.resource)
                observed_secrets = d.get("spec", {}).get("secrets", [])
                if observed_secrets:
                    return [kssv1alpha1.Secret(type=s["type"], name=s["name"], key=s["key"]) for s in observed_secrets]

        return None

    def gpu_pools(self):
        """Derive status.capacity.gpuPools from each node pool's class.

        The same logic applies to both GKE and Existing clusters: the
        class declares the per-node GPU resources, the pool declares how
        many nodes.
        """
        gpu_pools = []
        for pool in self.xr.spec.nodePools or []:
            cls = self.classes.get(pool.className)
            if not cls or not cls.spec.resources or not cls.spec.resources.gpu:
                continue
            gpu = cls.spec.resources.gpu
            accelerator_type = ""
            if cls.spec.provisioning and cls.spec.provisioning.gke and cls.spec.provisioning.gke.accelerator:
                accelerator_type = cls.spec.provisioning.gke.accelerator.type
            gpu_pools.append(
                {
                    "acceleratorType": accelerator_type,
                    "memory": gpu.memory,
                    "countPerNode": gpu.count,
                    "nodes": pool.maxNodeCount or pool.nodeCount,
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


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose an InferenceCluster from its cluster source and backend."""
    Composer(req, rsp).compose()
