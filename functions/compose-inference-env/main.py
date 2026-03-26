"""Compose an InferenceEnvironment from a GKECluster and KServeStack.

This function orchestrates the two internal XRs that make up an inference
environment: a GKECluster (the GKE cluster, VPC, and GCP resources) and a
KServeStack (cert-manager, Envoy Gateway, KServe, etc. installed on the
cluster). It threads secrets between them, computes GPU capacity from the
node pool config, and surfaces the gateway address for model routing.
"""

from crossplane.function import resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .lib import conditions
from .model.ai.modelplane.inferenceenvironment import v1alpha1
from .model.ai.modelplane.infrastructure.gkecluster import v1alpha1 as gkev1alpha1
from .model.ai.modelplane.infrastructure.kservestack import v1alpha1 as kssv1alpha1
from .model.io.crossplane.m.kubernetes.clusterproviderconfig import (
    v1alpha1 as k8scpcv1alpha1,
)
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

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


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose a GKECluster, KServeStack, and ClusterProviderConfig."""
    xr = v1alpha1.InferenceEnvironment(
        **resource.struct_to_dict(req.observed.composite.resource)
    )
    name = xr.metadata.name

    kserve = xr.spec.kserve
    if not kserve or not kserve.cluster or not kserve.cluster.gke:
        response.warning(rsp, "spec.kserve.cluster.gke is required")
        return

    gke = kserve.cluster.gke

    # All composed resources go in modelplane-system. Using a shared namespace
    # avoids the terminating-namespace problem: when an IE is deleted, a per-IE
    # namespace would enter Terminating state and block ProviderConfigUsage
    # creation, preventing managed resources from reconciling their deletion.
    ie_ns = "modelplane-system"

    # Compose a GKECluster. Only pass through fields the user explicitly set —
    # let the GKECluster XRD handle its own defaults for optional fields.
    gke_node_pools = []
    for pool in gke.nodePools:
        np_kwargs: dict = {
            "name": pool.name,
            "role": pool.role,
            "machineType": pool.machineType,
        }
        if pool.diskSizeGb is not None:
            np_kwargs["diskSizeGb"] = pool.diskSizeGb
        if pool.nodeCount is not None:
            np_kwargs["nodeCount"] = pool.nodeCount
        if pool.minNodeCount is not None:
            np_kwargs["minNodeCount"] = pool.minNodeCount
        if pool.maxNodeCount is not None:
            np_kwargs["maxNodeCount"] = pool.maxNodeCount
        if pool.gpu:
            gpu_kwargs: dict = {}
            if pool.gpu.acceleratorType is not None:
                gpu_kwargs["acceleratorType"] = pool.gpu.acceleratorType
            if pool.gpu.acceleratorCount is not None:
                gpu_kwargs["acceleratorCount"] = pool.gpu.acceleratorCount
            np_kwargs["gpu"] = gkev1alpha1.Gpu(**gpu_kwargs)
        if pool.zones is not None:
            np_kwargs["zones"] = pool.zones
        gke_node_pools.append(gkev1alpha1.NodePool(**np_kwargs))

    gke_spec_kwargs: dict = {
        "project": gke.project,
        "region": gke.region,
        "nodePools": gke_node_pools,
    }
    if gke.kubernetesVersion is not None:
        gke_spec_kwargs["kubernetesVersion"] = gke.kubernetesVersion

    resource.update(
        rsp.desired.resources["gke-cluster"],
        gkev1alpha1.GKECluster(
            metadata=metav1.ObjectMeta(name=name, namespace=ie_ns),
            spec=gkev1alpha1.Spec(**gke_spec_kwargs),
        ),
    )

    # Read the observed GKECluster's status.secrets and readiness. Gate
    # downstream resources (KServeStack, ClusterProviderConfig) on the
    # GKECluster being Ready — not just on secrets being available. The
    # secrets are populated immediately (they're just names), but the actual
    # kubeconfig and SA key secrets aren't created until the GKE cluster and
    # ServiceAccountKey are provisioned.
    gke_secrets = None
    gke_ready = conditions.has_condition(req, "gke-cluster", "Ready")
    gke_observed = req.observed.resources.get("gke-cluster")
    if gke_observed:
        observed_gke = gkev1alpha1.GKECluster.model_validate(
            resource.struct_to_dict(gke_observed.resource)
        )
        gke_secrets = observed_gke.status.secrets if observed_gke.status else None

    # Compose a ClusterProviderConfig for provider-kubernetes so that
    # ModelPlacements (which are namespaced, in ml-team) can create Objects on
    # the remote cluster. The namespaced ProviderConfig created by
    # compose-gke-cluster is only usable from modelplane-system.
    kubeconfig_secret = None
    sa_key_secret = None
    if gke_secrets:
        for s in gke_secrets:
            if s.type == "Kubeconfig":
                kubeconfig_secret = s
            elif s.type == "GCPServiceAccountKey":
                sa_key_secret = s

    cluster_pc_name = f"{name}-cluster-kubeconfig"
    cluster_pc_exists = "cluster-provider-config-kubernetes" in req.observed.resources
    if (gke_ready and kubeconfig_secret) or cluster_pc_exists:
        cpc = k8scpcv1alpha1.ClusterProviderConfig(
            metadata=metav1.ObjectMeta(name=cluster_pc_name),
            spec=k8scpcv1alpha1.Spec(
                credentials=k8scpcv1alpha1.Credentials(
                    source="Secret",
                    secretRef=k8scpcv1alpha1.SecretRef(
                        namespace=ie_ns,
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
                    namespace=ie_ns,
                    name=sa_key_secret.name,
                    key=sa_key_secret.key,
                ),
            )
        resource.update(
            rsp.desired.resources["cluster-provider-config-kubernetes"], cpc,
        )
        rsp.desired.resources["cluster-provider-config-kubernetes"].ready = fnv1.READY_TRUE

    # Gate KServeStack on GKECluster being Ready. This prevents noisy errors
    # from Helm releases trying to connect to a cluster that doesn't exist yet.
    # Always emit once it exists in observed state.
    kserve_stack_exists = "kserve-stack" in req.observed.resources
    if (gke_ready and gke_secrets) or kserve_stack_exists:
        kserve_version = kserve.version or "v0.16.0"
        kss_spec_kwargs = {
            "versions": kssv1alpha1.Versions(kserve=kserve_version),
        }
        if gke_secrets:
            kss_spec_kwargs["secrets"] = [
                kssv1alpha1.Secret(type=s.type, name=s.name, key=s.key)
                for s in gke_secrets
            ]
        else:
            # KServeStack exists but secrets aren't available yet. Use observed
            # spec.secrets to keep emitting consistently.
            kss_observed = req.observed.resources.get("kserve-stack")
            if kss_observed:
                kss = kssv1alpha1.KServeStack.model_validate(
                    resource.struct_to_dict(kss_observed.resource)
                )
                if kss.spec and kss.spec.secrets:
                    kss_spec_kwargs["secrets"] = [
                        kssv1alpha1.Secret(type=s.type, name=s.name, key=s.key)
                        for s in kss.spec.secrets
                    ]

        resource.update(
            rsp.desired.resources["kserve-stack"],
            kssv1alpha1.KServeStack(
                metadata=metav1.ObjectMeta(
                    name=f"{name}-kserve",
                    namespace=ie_ns,
                ),
                spec=kssv1alpha1.Spec(**kss_spec_kwargs),
            ),
        )

    # Block GKECluster deletion until KServeStack is deleted. Without this,
    # GKE cluster deletion can race ahead of Helm release cleanup, leaving
    # orphaned resources on the remote cluster.
    if kserve_stack_exists or (gke_ready and gke_secrets):
        resource.update(rsp.desired.resources["usage-gke-by-kserve"], {
            "apiVersion": "protection.crossplane.io/v1beta1",
            "kind": "Usage",
            "metadata": {"namespace": ie_ns},
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
        })
        rsp.desired.resources["usage-gke-by-kserve"].ready = fnv1.READY_TRUE

    # Read the observed KServeStack's status.gateway.address.
    gateway_address = None
    kss_observed = req.observed.resources.get("kserve-stack")
    if kss_observed:
        kss_dict = resource.struct_to_dict(kss_observed.resource)
        gateway_address = (
            kss_dict.get("status", {}).get("gateway", {}).get("address")
        )

    # Compute GPU capacity from node pool config using the static VRAM table.
    gpu_pools = []
    for pool in gke.nodePools:
        if pool.role == "GPU" and pool.gpu:
            acc_type = pool.gpu.acceleratorType or ""
            gpu_pools.append({
                "acceleratorType": acc_type,
                "memory": GPU_VRAM.get(acc_type, "0Gi"),
                "count": (pool.gpu.acceleratorCount or 1) * (pool.nodeCount or 1),
            })

    # Write status for consumption by compose-model-placement and
    # compose-model-deployment.
    status: dict = {
        "providerConfigRef": {"name": cluster_pc_name},
        "namespace": ie_ns,
        "capacity": {
            "backend": xr.spec.backend or "KServe",
            "gpuPools": gpu_pools,
        },
    }
    if gateway_address:
        status["gateway"] = {"address": gateway_address}
    resource.update(rsp.desired.composite, {"status": status})

    # Track readiness. Emit transition events when gating dependencies
    # become ready so kubectl describe shows provisioning progress.
    not_ready = []

    gke_is_ready = conditions.has_condition(req, "gke-cluster", "Ready")
    if gke_is_ready:
        rsp.desired.resources["gke-cluster"].ready = fnv1.READY_TRUE
        # Transition: GKE just became ready (KServeStack not yet observed).
        if not kserve_stack_exists:
            response.normal(rsp, "GKE cluster ready, composing KServeStack")
    else:
        not_ready.append("gke-cluster")

    kserve_ready = False
    if "kserve-stack" in rsp.desired.resources:
        kserve_ready = conditions.has_condition(req, "kserve-stack", "Ready")
        if kserve_ready:
            rsp.desired.resources["kserve-stack"].ready = fnv1.READY_TRUE
        else:
            not_ready.append("kserve-stack")
    else:
        not_ready.append("kserve-stack")

    # ClusterReady: the underlying cluster is provisioned and reachable.
    rsp.conditions.append(fnv1.Condition(
        type="ClusterReady",
        status=fnv1.STATUS_CONDITION_TRUE if gke_is_ready else fnv1.STATUS_CONDITION_FALSE,
        reason="ClusterRunning" if gke_is_ready else "Provisioning",
        target=fnv1.TARGET_COMPOSITE,
    ))

    # BackendReady: the inference backend is installed and healthy.
    if not gke_is_ready:
        backend_reason = "WaitingForCluster"
    elif kserve_ready:
        backend_reason = "BackendHealthy"
    else:
        backend_reason = "Installing"

    rsp.conditions.append(fnv1.Condition(
        type="BackendReady",
        status=fnv1.STATUS_CONDITION_TRUE if kserve_ready else fnv1.STATUS_CONDITION_FALSE,
        reason=backend_reason,
        target=fnv1.TARGET_COMPOSITE,
    ))

    if not not_ready:
        rsp.desired.composite.ready = fnv1.READY_TRUE
        if not conditions.was_ready(req):
            addr = f", gateway: {gateway_address}" if gateway_address else ""
            response.normal(rsp, f"Ready{addr}")
    else:
        response.normal(rsp, f"Waiting for: {', '.join(not_ready)}")
