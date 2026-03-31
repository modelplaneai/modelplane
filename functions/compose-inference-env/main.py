"""Compose an InferenceEnvironment.

This function orchestrates the internal XRs that make up an inference
environment. It dispatches on two axes: the backend (KServe, Dynamo, etc.)
determines which stack XR to install, and the cluster source (GKE, Existing)
determines how the cluster is obtained.

For provisioned clusters (source: GKE), it composes a GKECluster and threads
its secrets into the backend stack and ClusterProviderConfig. For BYO clusters
(source: Existing), it wires the user-supplied kubeconfig directly into the
same resources — skipping cluster provisioning entirely.
"""

from crossplane.function import resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .lib import conditions, metadata, secrets
from .lib import resource as libresource
from .model.ai.modelplane.inferenceenvironment import v1alpha1
from .model.ai.modelplane.infrastructure.gkecluster import v1alpha1 as gkev1alpha1
from .model.ai.modelplane.infrastructure.kservestack import v1alpha1 as kssv1alpha1
from .model.io.crossplane.m.kubernetes.clusterproviderconfig import (
    v1alpha1 as k8scpcv1alpha1,
)
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# Backend discriminator values from the XRD enum.
BACKEND_KSERVE = "KServe"

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
        if backend == BACKEND_KSERVE:
            if not self.xr.spec.kserve or not self.xr.spec.kserve.cluster:
                response.warning(self.rsp, "spec.kserve.cluster is required when backend is KServe")
                return
            cluster = self.xr.spec.kserve.cluster
            # The composed resource key for the backend stack XR. Shared
            # methods (derive_conditions, observed_gateway_address, etc.) use
            # this to check readiness and read observed state without knowing
            # which backend they're serving.
            #
            # This implies a contract: any backend stack XR must have a Ready
            # condition and surface status.gateway.address. Both are reasonable
            # — Ready is standard for Crossplane XRs, and the gateway address
            # is how ModelDeployment routes to the environment.
            self.backend_stack_resource_key = "kserve-stack"
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
        then wires its secrets into the backend stack."""
        if not gke:
            response.warning(self.rsp, "GKE configuration is required when source is GKE")
            return

        self.compose_gke_cluster(gke)

        gke_ready = conditions.has_condition(self.req, "gke-cluster", "Ready")
        kubeconfig = self.observed_gke_secret(secrets.SECRET_TYPE_KUBECONFIG)
        sa_key = self.observed_gke_secret(secrets.SECRET_TYPE_GCP_SA_KEY)
        cpc_exists = "cluster-provider-config-kubernetes" in self.req.observed.resources
        stack_exists = self.backend_stack_resource_key in self.req.observed.resources

        if (gke_ready and kubeconfig) or cpc_exists:
            self.compose_cluster_provider_config(
                kubeconfig.name if kubeconfig else "",
                kubeconfig.key if kubeconfig else "",
                sa_key,
            )

        stack_secrets = self.resolve_gke_stack_secrets(gke_ready, stack_exists)
        if stack_secrets or stack_exists:
            if stack_secrets:
                self.compose_backend_stack(stack_secrets)
            self.compose_gke_usage()

        if gke_ready:
            self.rsp.desired.resources["gke-cluster"].ready = fnv1.READY_TRUE
            if not stack_exists:
                response.normal(self.rsp, "GKE cluster ready, composing backend stack")

        self.write_status(self.gke_gpu_pools(gke))
        self.derive_conditions(cluster_ready=gke_ready)

    def compose_existing(self, existing):
        """Compose an InferenceEnvironment backed by a user-supplied cluster.
        No gating needed — the kubeconfig secret is provided by the user."""
        if not existing:
            response.warning(self.rsp, "Existing cluster configuration is required when source is Existing")
            return

        self.compose_cluster_provider_config(existing.secretRef.name, existing.secretRef.key)
        self.compose_backend_stack([
            kssv1alpha1.Secret(
                type=secrets.SECRET_TYPE_KUBECONFIG,
                name=existing.secretRef.name,
                key=existing.secretRef.key,
            ),
        ])

        self.write_status(self.existing_gpu_pools(existing))
        self.derive_conditions(cluster_ready=True)

    def compose_backend_stack(self, stack_secrets):
        """Compose the backend-specific stack XR. Dispatches on the backend
        discriminator to compose the right stack type."""
        backend = self.xr.spec.backend
        if backend == BACKEND_KSERVE:
            self.compose_kserve_stack(stack_secrets)

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

    def compose_kserve_stack(self, stack_secrets):
        """Compose a KServeStack XR with the given secrets."""
        resource.update(
            self.rsp.desired.resources["kserve-stack"],
            kssv1alpha1.KServeStack(
                metadata=metav1.ObjectMeta(
                    name=f"{self.xr.metadata.name}-kserve",
                    namespace=metadata.NAMESPACE_SYSTEM,
                ),
                spec=kssv1alpha1.Spec(
                    versions=kssv1alpha1.Versions(kserve=self.xr.spec.kserve.version),
                    secrets=stack_secrets,
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
        stack_ready = conditions.has_condition(self.req, self.backend_stack_resource_key, "Ready")
        if self.backend_stack_resource_key in self.rsp.desired.resources and stack_ready:
            self.rsp.desired.resources[self.backend_stack_resource_key].ready = fnv1.READY_TRUE

        conditions.set_condition(
            self.rsp,
            CONDITION_TYPE_CLUSTER_READY,
            cluster_ready,
            CONDITION_REASON_CLUSTER_RUNNING if cluster_ready else CONDITION_REASON_PROVISIONING,
        )

        if not cluster_ready:
            backend_reason = CONDITION_REASON_WAITING_FOR_CLUSTER
        elif stack_ready:
            backend_reason = CONDITION_REASON_BACKEND_HEALTHY
        else:
            backend_reason = CONDITION_REASON_INSTALLING

        conditions.set_condition(self.rsp, CONDITION_TYPE_BACKEND_READY, stack_ready, backend_reason)

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
        """Block GKECluster deletion until the backend stack is deleted."""
        resource.update(
            self.rsp.desired.resources["usage-gke-by-kserve"],
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
                        "kind": "KServeStack",
                        "resourceSelector": {"matchControllerRef": True},
                    },
                    "replayDeletion": True,
                },
            },
        )
        self.rsp.desired.resources["usage-gke-by-kserve"].ready = fnv1.READY_TRUE

    def resolve_gke_stack_secrets(self, gke_ready, stack_exists):
        """Resolve secrets for the backend stack from GKECluster status. Falls
        back to the observed stack's spec.secrets if GKECluster secrets aren't
        available but the stack already exists."""
        gke_secrets = self.observed_gke_secrets()

        if gke_ready and gke_secrets:
            return [kssv1alpha1.Secret(type=s.type, name=s.name, key=s.key) for s in gke_secrets]

        if stack_exists:
            observed = self.req.observed.resources.get(self.backend_stack_resource_key)
            if observed:
                stack = kssv1alpha1.KServeStack.model_validate(resource.struct_to_dict(observed.resource))
                if stack.spec and stack.spec.secrets:
                    return [kssv1alpha1.Secret(type=s.type, name=s.name, key=s.key) for s in stack.spec.secrets]

        return None

    def gke_gpu_pools(self, gke):
        """Compute GPU capacity from GKE node pool config."""
        gpu_pools = []
        for pool in gke.nodePools:
            if pool.role != "GPU" or not pool.gpu:
                continue
            gpu_pools.append(
                {
                    "acceleratorType": pool.gpu.acceleratorType,
                    "memory": GPU_VRAM.get(pool.gpu.acceleratorType, "0Gi"),
                    "count": pool.gpu.acceleratorCount * pool.nodeCount,
                }
            )
        return gpu_pools

    def existing_gpu_pools(self, existing):
        """Compute GPU capacity from declared node pools."""
        gpu_pools = []
        for pool in existing.nodePools or []:
            if not pool.gpu or not pool.gpu.acceleratorType:
                continue
            gpu_pools.append(
                {
                    "acceleratorType": pool.gpu.acceleratorType,
                    "memory": GPU_VRAM.get(pool.gpu.acceleratorType, "0Gi"),
                    "count": pool.gpu.acceleratorCount * pool.nodeCount,
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
        """Read the backend stack's gateway address from observed state."""
        observed = self.req.observed.resources.get(self.backend_stack_resource_key)
        if not observed:
            return None
        stack = kssv1alpha1.KServeStack.model_validate(resource.struct_to_dict(observed.resource))
        if not stack.status or not stack.status.gateway:
            return None
        return stack.status.gateway.address


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose an InferenceEnvironment from its cluster source and backend."""
    Composer(req, rsp).compose()
