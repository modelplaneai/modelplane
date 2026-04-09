"""Compose an InferenceEnvironment.

This function orchestrates the internal XRs that make up an inference
environment. It dispatches on two axes: the backend (KServe, Dynamo, etc.)
determines which backend XR to install, and the cluster source (GKE, Existing)
determines how the cluster is obtained.

For provisioned clusters (source: GKE), it composes a GKECluster and threads
its secrets into the backend and ClusterProviderConfig. For BYO clusters
(source: Existing), it wires the user-supplied kubeconfig directly into the
same resources — skipping cluster provisioning entirely.
"""

from crossplane.function import resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .lib import backends, conditions, metadata, secrets
from .lib import resource as libresource
from .model.ai.modelplane.inferenceenvironment import v1alpha1
from .model.ai.modelplane.infrastructure.dynamobackend import v1alpha1 as dsv1alpha1
from .model.ai.modelplane.infrastructure.gkecluster import v1alpha1 as gkev1alpha1
from .model.ai.modelplane.infrastructure.kservebackend import v1alpha1 as kssv1alpha1
from .model.io.crossplane.m.kubernetes.clusterproviderconfig import (
    v1alpha1 as k8scpcv1alpha1,
)
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# Maps backends to their XR kinds (for Usage resources).
BACKEND_XR_KINDS = {
    backends.KSERVE: "KServeBackend",
    backends.DYNAMO: "DynamoBackend",
}

# Cluster source discriminator values from the XRD enum.
CLUSTER_SOURCE_GKE = "GKE"
CLUSTER_SOURCE_EXISTING = "Existing"

# Condition types and reasons for the InferenceEnvironment XR.
CONDITION_TYPE_CLUSTER_READY = "ClusterReady"
CONDITION_TYPE_BACKEND_READY = "BackendReady"

CONDITION_REASON_CLUSTER_RUNNING = "ClusterRunning"
CONDITION_REASON_PROVISIONING = "Provisioning"
CONDITION_REASON_WAITING_FOR_CLUSTER = "WaitingForCluster"
CONDITION_REASON_BACKEND_HEALTHY = "BackendHealthy"
CONDITION_REASON_INSTALLING = "Installing"

# Per-GPU VRAM by accelerator type. Used to compute capacity from the node
# pool config so the deploy function can match models to environments.
GPU_VRAM = {
    "nvidia-l4": "24Gi",
    "nvidia-t4": "16Gi",
    "nvidia-a100-40gb": "40Gi",
    "nvidia-a100-80gb": "80Gi",
    "nvidia-h100-80gb": "80Gi",
    "nvidia-h100-mega-80gb": "80Gi",
    "nvidia-v100": "16Gi",
}


class Composer:
    def __init__(self, req, rsp):
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.InferenceEnvironment(**resource.struct_to_dict(req.observed.composite.resource))

    def compose(self):
        backend = self.xr.spec.backend
        if backend == backends.KSERVE:
            if not self.xr.spec.kserve or not self.xr.spec.kserve.cluster:
                response.warning(self.rsp, "spec.kserve.cluster is required when backend is KServe")
                return
            cluster = self.xr.spec.kserve.cluster
            # The composed resource key for the backend XR. Shared methods
            # (derive_conditions, observed_gateway_address, etc.) use this to
            # check readiness and read observed state without knowing which
            # backend they're serving.
            #
            # This implies a contract: any backend XR must have a Ready
            # condition and surface status.gateway.address. Both are reasonable
            # — Ready is standard for Crossplane XRs, and the gateway address
            # is how ModelDeployment routes to the environment.
            self.backend_resource_key = "kserve-backend"
        elif backend == backends.DYNAMO:
            if not self.xr.spec.dynamo or not self.xr.spec.dynamo.cluster:
                response.warning(self.rsp, "spec.dynamo.cluster is required when backend is Dynamo")
                return
            cluster = self.xr.spec.dynamo.cluster
            self.backend_resource_key = "dynamo-backend"
        else:
            response.warning(self.rsp, f"unsupported backend: {backend}")
            return

        source = cluster.source
        if source == CLUSTER_SOURCE_GKE:
            self.compose_gke(cluster.gke)
        elif source == CLUSTER_SOURCE_EXISTING:
            self.compose_existing(cluster.existing)
        else:
            response.warning(self.rsp, f"unsupported cluster source: {source}")

    def compose_gke(self, gke):
        """Compose an InferenceEnvironment backed by a Modelplane-provisioned
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
        backend_exists = self.backend_resource_key in self.req.observed.resources

        if (gke_ready and kubeconfig) or cpc_exists:
            self.compose_cluster_provider_config(
                kubeconfig.name if kubeconfig else "",
                kubeconfig.key if kubeconfig else "",
                sa_key,
            )

        backend_secrets = self.resolve_gke_backend_secrets(gke_ready, backend_exists)
        if backend_secrets or backend_exists:
            if backend_secrets:
                self.compose_backend(backend_secrets)
            self.compose_gke_usage()

        if gke_ready:
            self.rsp.desired.resources["gke-cluster"].ready = fnv1.READY_TRUE
            if not backend_exists:
                response.normal(self.rsp, "GKE cluster ready, composing backend")

        self.write_status(self.gke_gpu_pools(gke))
        self.derive_conditions(cluster_ready=gke_ready)

    def compose_existing(self, existing):
        """Compose an InferenceEnvironment backed by a user-supplied cluster.
        No gating needed — the kubeconfig secret is provided by the user.

        If identitySecretRef is provided, the identity is passed to the
        ClusterProviderConfig and backend for cloud IAM auth (e.g.
        GKE clusters where the kubeconfig uses GCP IAM instead of embedded
        credentials). Without it, the kubeconfig must be self-contained."""
        if not existing:
            response.warning(self.rsp, "Existing cluster configuration is required when source is Existing")
            return

        # Resolve the optional identity secret for cloud IAM auth.
        identity = existing.identitySecretRef if hasattr(existing, "identitySecretRef") else None

        self.compose_cluster_provider_config(existing.secretRef.name, existing.secretRef.key, sa_key=identity)

        backend_secrets = [
            {"type": secrets.SECRET_TYPE_KUBECONFIG, "name": existing.secretRef.name, "key": existing.secretRef.key},
        ]
        if identity:
            backend_secrets.append(
                {"type": secrets.SECRET_TYPE_GCP_SA_KEY, "name": identity.name, "key": identity.key},
            )
        self.compose_backend(backend_secrets)

        self.write_status(self.existing_gpu_pools(existing))
        self.derive_conditions(cluster_ready=True)

    def compose_backend(self, backend_secrets):
        """Compose the backend-specific XR. Dispatches on the backend
        discriminator to compose the right backend type."""
        backend = self.xr.spec.backend
        if backend == backends.KSERVE:
            self.compose_kserve_backend(backend_secrets)
        elif backend == backends.DYNAMO:
            self.compose_dynamo_backend(backend_secrets)

    def compose_cluster_provider_config(self, kubeconfig_name, kubeconfig_key, sa_key=None):
        """Compose a ClusterProviderConfig for provider-kubernetes so that
        ModelPlacements can create Objects on the remote cluster."""
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

    def compose_kserve_backend(self, backend_secrets):
        """Compose a KServeBackend XR with the given secrets."""
        resource.update(
            self.rsp.desired.resources["kserve-backend"],
            kssv1alpha1.KServeBackend(
                metadata=metav1.ObjectMeta(
                    name=f"{self.xr.metadata.name}-kserve",
                    namespace=metadata.NAMESPACE_SYSTEM,
                ),
                spec=kssv1alpha1.Spec(
                    versions=kssv1alpha1.Versions(kserve=self.xr.spec.kserve.version),
                    secrets=[kssv1alpha1.Secret(type=s["type"], name=s["name"], key=s["key"]) for s in backend_secrets],
                ),
            ),
        )

    def compose_dynamo_backend(self, backend_secrets):
        """Compose a DynamoBackend XR with the given secrets."""
        resource.update(
            self.rsp.desired.resources["dynamo-backend"],
            dsv1alpha1.DynamoBackend(
                metadata=metav1.ObjectMeta(
                    name=f"{self.xr.metadata.name}-dynamo",
                    namespace=metadata.NAMESPACE_SYSTEM,
                ),
                spec=dsv1alpha1.Spec(
                    versions=dsv1alpha1.Versions(dynamo=self.xr.spec.dynamo.version),
                    secrets=[dsv1alpha1.Secret(type=s["type"], name=s["name"], key=s["key"]) for s in backend_secrets],
                ),
            ),
        )

    def write_status(self, gpu_pools):
        """Write the InferenceEnvironment status."""
        status = v1alpha1.Status(
            providerConfigRef=v1alpha1.ProviderConfigRef(
                name=f"{self.xr.metadata.name}-cluster-kubeconfig",
            ),
            namespace=metadata.NAMESPACE_SYSTEM,
            capacity=v1alpha1.Capacity(
                backend=self.xr.spec.backend,
                gpuPools=gpu_pools,
            ),
        )
        gateway_address = self.observed_gateway_address()
        if gateway_address:
            status.gateway = v1alpha1.Gateway(address=gateway_address)
        libresource.update_status(self.rsp.desired.composite, status)

    def derive_conditions(self, cluster_ready):
        """Derive ClusterReady and BackendReady conditions."""
        backend_ready = conditions.has_condition(self.req, self.backend_resource_key, "Ready")
        if self.backend_resource_key in self.rsp.desired.resources and backend_ready:
            self.rsp.desired.resources[self.backend_resource_key].ready = fnv1.READY_TRUE

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
        """Compose a GKECluster XR from the GKE config."""
        gke_node_pools = []
        for pool in gke.nodePools:
            d = pool.model_dump()
            if "gpu" in d and d["gpu"] is not None:
                d["gpu"] = gkev1alpha1.Gpu(**d["gpu"])
            else:
                d.pop("gpu", None)
            gke_node_pools.append(gkev1alpha1.NodePool(**d))

        gke_spec_kwargs = gke.model_dump()
        gke_spec_kwargs["nodePools"] = gke_node_pools

        resource.update(
            self.rsp.desired.resources["gke-cluster"],
            gkev1alpha1.GKECluster(
                metadata=metav1.ObjectMeta(
                    name=self.xr.metadata.name,
                    namespace=metadata.NAMESPACE_SYSTEM,
                ),
                spec=gkev1alpha1.Spec(**gke_spec_kwargs),
            ),
        )

    def compose_gke_usage(self):
        """Block GKECluster deletion until the backend is deleted."""
        backend_kind = BACKEND_XR_KINDS.get(self.xr.spec.backend, "KServeBackend")
        resource.update(
            self.rsp.desired.resources["usage-gke-by-backend"],
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "metadata": {"namespace": metadata.NAMESPACE_SYSTEM},
                "spec": {
                    "of": {
                        "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                        "kind": "GKECluster",
                        "resourceSelector": {"matchControllerRef": True},
                    },
                    "by": {
                        "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                        "kind": backend_kind,
                        "resourceSelector": {"matchControllerRef": True},
                    },
                    "replayDeletion": True,
                },
            },
        )
        self.rsp.desired.resources["usage-gke-by-backend"].ready = fnv1.READY_TRUE

    def resolve_gke_backend_secrets(self, gke_ready, backend_exists):
        """Resolve secrets for the backend from GKECluster status. Falls
        back to the observed backend's spec.secrets if GKECluster secrets aren't
        available but the backend already exists. Returns a list of dicts with
        type/name/key — the backend-specific compose methods convert to their
        own Pydantic types."""
        gke_secrets = self.observed_gke_secrets()

        if gke_ready and gke_secrets:
            return [{"type": s.type, "name": s.name, "key": s.key} for s in gke_secrets]

        if backend_exists:
            observed = self.req.observed.resources.get(self.backend_resource_key)
            if observed:
                d = resource.struct_to_dict(observed.resource)
                observed_secrets = d.get("spec", {}).get("secrets", [])
                if observed_secrets:
                    return [{"type": s["type"], "name": s["name"], "key": s["key"]} for s in observed_secrets]

        return None

    def gke_gpu_pools(self, gke):
        """Compute GPU capacity from GKE node pool config."""
        gpu_pools = []
        for pool in gke.nodePools:
            if pool.role != "GPU" or not pool.gpu:
                continue
            nodes = pool.maxNodeCount or pool.nodeCount
            gpu_pools.append(
                {
                    "acceleratorType": pool.gpu.acceleratorType,
                    "memory": GPU_VRAM.get(pool.gpu.acceleratorType, "0Gi"),
                    "count": pool.gpu.acceleratorCount * nodes,
                }
            )
        return gpu_pools

    def existing_gpu_pools(self, existing):
        """Compute GPU capacity from declared node pools."""
        gpu_pools = []
        for pool in existing.nodePools or []:
            if not pool.gpu or not pool.gpu.acceleratorType:
                continue
            nodes = pool.maxNodeCount or pool.nodeCount
            gpu_pools.append(
                {
                    "acceleratorType": pool.gpu.acceleratorType,
                    "memory": GPU_VRAM.get(pool.gpu.acceleratorType, "0Gi"),
                    "count": pool.gpu.acceleratorCount * nodes,
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
        observed = self.req.observed.resources.get(self.backend_resource_key)
        if not observed:
            return None
        d = resource.struct_to_dict(observed.resource)
        return d.get("status", {}).get("gateway", {}).get("address")


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose an InferenceEnvironment from its cluster source and backend."""
    Composer(req, rsp).compose()
