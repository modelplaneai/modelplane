"""Compose an InferenceEnvironment from a GKECluster and KServeStack.

This function orchestrates the two internal XRs that make up an inference
environment: a GKECluster (the GKE cluster, VPC, and GCP resources) and a
KServeStack (cert-manager, Envoy Gateway, KServe, etc. installed on the
cluster). It threads secrets between them, computes GPU capacity from the
node pool config, and surfaces the gateway address for model routing.
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

# Condition types and reasons for the InferenceEnvironment XR.
CONDITION_TYPE_CLUSTER_READY = "ClusterReady"
CONDITION_TYPE_BACKEND_READY = "BackendReady"

CONDITION_REASON_CLUSTER_RUNNING = "ClusterRunning"
CONDITION_REASON_PROVISIONING = "Provisioning"
CONDITION_REASON_WAITING_FOR_CLUSTER = "WaitingForCluster"
CONDITION_REASON_BACKEND_HEALTHY = "BackendHealthy"
CONDITION_REASON_INSTALLING = "Installing"

# Per-GPU VRAM by GKE accelerator type. Used to compute capacity from the
# node pool config — the environment reports how much VRAM is available so
# the deploy function can match models to compatible environments.
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
        if not self.xr.spec.kserve or not self.xr.spec.kserve.cluster or not self.xr.spec.kserve.cluster.gke:
            response.warning(self.rsp, "spec.kserve.cluster.gke is required")
            return

        self.compose_gke_cluster()
        self.compose_cluster_provider_config()
        self.compose_kserve_stack()
        self.compose_usage()
        self.write_status()
        self.derive_conditions()

    def compose_gke_cluster(self):
        """Compose a GKECluster XR from the InferenceEnvironment's GKE spec."""
        gke = self.xr.spec.kserve.cluster.gke

        # The IE and GKECluster NodePool schemas share field names, so we
        # dump each pool and reconstruct as a GKECluster pool.
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

    def compose_cluster_provider_config(self):
        """Compose a ClusterProviderConfig for provider-kubernetes so that
        ModelPlacements (namespaced, in ml-team namespaces) can create Objects
        on the remote cluster. Gated on GKECluster being ready."""
        gke_ready = conditions.has_condition(self.req, "gke-cluster", "Ready")
        kubeconfig_secret = self.observed_gke_secret(secrets.SECRET_TYPE_KUBECONFIG)
        sa_key_secret = self.observed_gke_secret(secrets.SECRET_TYPE_GCP_SA_KEY)

        if not (
            (gke_ready and kubeconfig_secret) or "cluster-provider-config-kubernetes" in self.req.observed.resources
        ):
            return

        cpc = k8scpcv1alpha1.ClusterProviderConfig(
            metadata=metav1.ObjectMeta(name=f"{self.xr.metadata.name}-cluster-kubeconfig"),
            spec=k8scpcv1alpha1.Spec(
                credentials=k8scpcv1alpha1.Credentials(
                    source="Secret",
                    secretRef=k8scpcv1alpha1.SecretRef(
                        namespace=metadata.NAMESPACE_SYSTEM,
                        name=kubeconfig_secret.name if kubeconfig_secret else "",
                        key=kubeconfig_secret.key if kubeconfig_secret else "",
                    ),
                ),
            ),
        )
        if sa_key_secret:
            cpc.spec.identity = k8scpcv1alpha1.Identity(
                type="GoogleApplicationCredentials",
                source="Secret",
                secretRef=k8scpcv1alpha1.SecretRef(
                    namespace=metadata.NAMESPACE_SYSTEM,
                    name=sa_key_secret.name,
                    key=sa_key_secret.key,
                ),
            )
        resource.update(
            self.rsp.desired.resources["cluster-provider-config-kubernetes"],
            cpc,
        )
        self.rsp.desired.resources["cluster-provider-config-kubernetes"].ready = fnv1.READY_TRUE

    def compose_kserve_stack(self):
        """Compose a KServeStack XR. Gated on GKECluster being ready to prevent
        noisy errors from Helm releases trying to connect to a non-existent
        cluster."""
        gke_ready = conditions.has_condition(self.req, "gke-cluster", "Ready")
        gke_secrets = self.observed_gke_secrets()
        kserve_stack_exists = "kserve-stack" in self.req.observed.resources

        if not ((gke_ready and gke_secrets) or kserve_stack_exists):
            return

        kss_spec_kwargs = {
            "versions": kssv1alpha1.Versions(kserve=self.xr.spec.kserve.version),
        }
        if gke_secrets:
            kss_spec_kwargs["secrets"] = [kssv1alpha1.Secret(type=s.type, name=s.name, key=s.key) for s in gke_secrets]
        else:
            # KServeStack exists but secrets aren't available yet. Use observed
            # spec.secrets to keep emitting consistently.
            kss_observed = self.req.observed.resources.get("kserve-stack")
            if kss_observed:
                kss = kssv1alpha1.KServeStack.model_validate(resource.struct_to_dict(kss_observed.resource))
                if kss.spec and kss.spec.secrets:
                    kss_spec_kwargs["secrets"] = [
                        kssv1alpha1.Secret(type=s.type, name=s.name, key=s.key) for s in kss.spec.secrets
                    ]

        resource.update(
            self.rsp.desired.resources["kserve-stack"],
            kssv1alpha1.KServeStack(
                metadata=metav1.ObjectMeta(
                    name=f"{self.xr.metadata.name}-kserve",
                    namespace=metadata.NAMESPACE_SYSTEM,
                ),
                spec=kssv1alpha1.Spec(**kss_spec_kwargs),
            ),
        )

    def compose_usage(self):
        """Block GKECluster deletion until KServeStack is deleted. Without this,
        GKE cluster deletion can race ahead of Helm release cleanup, leaving
        orphaned resources on the remote cluster."""
        gke_ready = conditions.has_condition(self.req, "gke-cluster", "Ready")
        gke_secrets = self.observed_gke_secrets()
        kserve_stack_exists = "kserve-stack" in self.req.observed.resources

        if not (kserve_stack_exists or (gke_ready and gke_secrets)):
            return

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

    def write_status(self):
        gke = self.xr.spec.kserve.cluster.gke

        # Compute GPU capacity from node pool config using the static VRAM table.
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

    def derive_conditions(self):
        gke_ready = conditions.has_condition(self.req, "gke-cluster", "Ready")
        kserve_stack_exists = "kserve-stack" in self.req.observed.resources

        # GKECluster readiness.
        if gke_ready:
            self.rsp.desired.resources["gke-cluster"].ready = fnv1.READY_TRUE
            # Transition: GKE just became ready (KServeStack not yet observed).
            if not kserve_stack_exists:
                response.normal(self.rsp, "GKE cluster ready, composing KServeStack")

        # KServeStack readiness.
        kserve_ready = False
        if "kserve-stack" in self.rsp.desired.resources:
            kserve_ready = conditions.has_condition(self.req, "kserve-stack", "Ready")
            if kserve_ready:
                self.rsp.desired.resources["kserve-stack"].ready = fnv1.READY_TRUE

        # ClusterReady condition.
        conditions.set_condition(
            self.rsp,
            CONDITION_TYPE_CLUSTER_READY,
            gke_ready,
            CONDITION_REASON_CLUSTER_RUNNING if gke_ready else CONDITION_REASON_PROVISIONING,
        )

        # BackendReady condition.
        if not gke_ready:
            backend_reason = CONDITION_REASON_WAITING_FOR_CLUSTER
        elif kserve_ready:
            backend_reason = CONDITION_REASON_BACKEND_HEALTHY
        else:
            backend_reason = CONDITION_REASON_INSTALLING

        conditions.set_condition(self.rsp, CONDITION_TYPE_BACKEND_READY, kserve_ready, backend_reason)

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
        """Read the KServeStack's gateway address from observed state."""
        kss_observed = self.req.observed.resources.get("kserve-stack")
        if not kss_observed:
            return None
        kss = kssv1alpha1.KServeStack.model_validate(resource.struct_to_dict(kss_observed.resource))
        if not kss.status or not kss.status.gateway:
            return None
        return kss.status.gateway.address


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose a GKECluster, KServeStack, and ClusterProviderConfig."""
    Composer(req, rsp).compose()
