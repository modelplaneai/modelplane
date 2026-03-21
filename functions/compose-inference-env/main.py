from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .model.ai.modelplane.inferenceenvironment import v1alpha1
from .model.ai.modelplane.infrastructure.gkecluster import v1alpha1 as gkev1alpha1
from .model.ai.modelplane.infrastructure.kservestack import v1alpha1 as kssv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# Static VRAM lookup table keyed by GKE accelerator type.
GPU_VRAM = {
    "nvidia-l4": "24Gi",
    "nvidia-t4": "16Gi",
    "nvidia-a100-40gb": "40Gi",
    "nvidia-a100-80gb": "80Gi",
    "nvidia-h100-80gb": "80Gi",
    "nvidia-h100-mega-80gb": "80Gi",
    "nvidia-v100": "16Gi",
}


def _has_condition(req: fnv1.RunFunctionRequest, name: str, cond: str) -> bool:
    """Check if an observed composed resource has the given condition True."""
    observed = req.observed.resources.get(name)
    if observed is None:
        return False
    return resource.get_condition(observed.resource, cond).status == "True"


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    xr = v1alpha1.InferenceEnvironment(
        **resource.struct_to_dict(req.observed.composite.resource)
    )
    name = xr.metadata.name

    kserve = xr.spec.kserve
    if not kserve or not kserve.cluster or not kserve.cluster.gke:
        rsp.conditions.append(fnv1.Condition(
            type="Ready",
            status=fnv1.STATUS_CONDITION_FALSE,
            reason="InvalidSpec",
            message="spec.kserve.cluster.gke is required",
            target=fnv1.TARGET_COMPOSITE_AND_CLAIM,
        ))
        return

    gke = kserve.cluster.gke
    ie_ns = f"ie-{name}"

    # 1. Compose a Namespace for the GKECluster and KServeStack.
    resource.update(rsp.desired.resources["namespace"], {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": ie_ns},
    })
    rsp.desired.resources["namespace"].ready = fnv1.READY_TRUE

    # 2. Always compose a GKECluster.
    # Only pass through fields the user explicitly set. Let the GKECluster
    # XRD handle its own defaults for optional fields.
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
            metadata=metav1.ObjectMeta(
                name=name,
                namespace=ie_ns,
            ),
            spec=gkev1alpha1.Spec(**gke_spec_kwargs),
        ),
    )

    # 3. Read the observed GKECluster's status.secrets and readiness.
    #    Gate downstream resources (KServeStack, ClusterProviderConfig) on the
    #    GKECluster being Ready, not just on secrets being available. The
    #    secrets are populated immediately (they're just names), but the actual
    #    kubeconfig and SA key secrets aren't created until the GKE cluster and
    #    ServiceAccountKey are provisioned.
    gke_secrets = None
    gke_ready = _has_condition(req, "gke-cluster", "Ready")
    gke_observed = req.observed.resources.get("gke-cluster")
    if gke_observed:
        gke_dict = resource.struct_to_dict(gke_observed.resource)
        gke_secrets = gke_dict.get("status", {}).get("secrets")

    # 3b. Compose a ClusterProviderConfig for provider-kubernetes so that
    #     ModelPlacements (which are namespaced, in a different namespace) can
    #     create Objects on the remote cluster. The namespaced ProviderConfig
    #     created by compose-gke-cluster is only usable from ie-{name}.
    kubeconfig_secret = None
    sa_key_secret = None
    if gke_secrets:
        for s in gke_secrets:
            if s.get("type") == "Kubeconfig":
                kubeconfig_secret = s
            elif s.get("type") == "GCPServiceAccountKey":
                sa_key_secret = s

    cluster_pc_name = f"{name}-cluster-kubeconfig"
    cluster_pc_exists = "cluster-provider-config-kubernetes" in req.observed.resources
    if (gke_ready and kubeconfig_secret) or cluster_pc_exists:
        pc_spec: dict = {
            "credentials": {
                "source": "Secret",
                "secretRef": {
                    "namespace": ie_ns,
                    "name": kubeconfig_secret["name"] if kubeconfig_secret else "",
                    "key": kubeconfig_secret["key"] if kubeconfig_secret else "",
                },
            },
        }
        if sa_key_secret:
            pc_spec["identity"] = {
                "type": "GoogleApplicationCredentials",
                "source": "Secret",
                "secretRef": {
                    "namespace": ie_ns,
                    "name": sa_key_secret["name"],
                    "key": sa_key_secret["key"],
                },
            }
        resource.update(rsp.desired.resources["cluster-provider-config-kubernetes"], {
            "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
            "kind": "ClusterProviderConfig",
            "metadata": {"name": cluster_pc_name},
            "spec": pc_spec,
        })
        rsp.desired.resources["cluster-provider-config-kubernetes"].ready = fnv1.READY_TRUE

    # 4. Gate KServeStack on GKECluster being Ready. This prevents noisy
    #    errors from Helm releases trying to connect to a cluster that
    #    doesn't exist yet. Always emit once it exists in observed state.
    kserve_stack_exists = "kserve-stack" in req.observed.resources
    if (gke_ready and gke_secrets) or kserve_stack_exists:
        kserve_version = kserve.version or "v0.16.0"
        kss_spec_kwargs = {
            "versions": kssv1alpha1.Versions(kserve=kserve_version),
        }
        if gke_secrets:
            kss_spec_kwargs["secrets"] = [
                kssv1alpha1.Secret(
                    type=s["type"],
                    name=s["name"],
                    key=s["key"],
                )
                for s in gke_secrets
            ]
        else:
            # KServeStack exists but secrets aren't available yet.
            # Use observed spec.secrets to keep emitting consistently.
            kss_observed = req.observed.resources.get("kserve-stack")
            if kss_observed:
                kss_dict = resource.struct_to_dict(kss_observed.resource)
                kss_observed_secrets = kss_dict.get("spec", {}).get("secrets", [])
                kss_spec_kwargs["secrets"] = [
                    kssv1alpha1.Secret(
                        type=s["type"],
                        name=s["name"],
                        key=s["key"],
                    )
                    for s in kss_observed_secrets
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

    # 4b. Compose a Usage that blocks GKECluster deletion until KServeStack
    #     is deleted. Without this, GKE cluster deletion can race ahead of
    #     Helm release cleanup, leaving orphaned resources on the remote
    #     cluster that can never be cleaned up.
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

    # 5. Read the observed KServeStack's status.gateway.address.
    gateway_address = None
    kss_observed = req.observed.resources.get("kserve-stack")
    if kss_observed:
        kss_dict = resource.struct_to_dict(kss_observed.resource)
        gateway_address = (
            kss_dict.get("status", {})
            .get("gateway", {})
            .get("address")
        )

    # 6. Compute capacity from node pool config.
    gpu_pools = []
    for pool in gke.nodePools:
        if pool.role == "GPU" and pool.gpu:
            acc_type = pool.gpu.acceleratorType or ""
            gpu_pools.append({
                "acceleratorType": acc_type,
                "memory": GPU_VRAM.get(acc_type, "0Gi"),
                "count": (pool.gpu.acceleratorCount or 1) * (pool.nodeCount or 1),
            })

    # 7. Write status.
    pc_name = f"{name}-cluster-kubeconfig"
    status: dict = {
        "providerConfigRef": {"name": pc_name},
        "namespace": ie_ns,
        "capacity": {
            "backend": xr.spec.backend or "KServe",
            "gpuPools": gpu_pools,
        },
    }
    if gateway_address:
        status["gateway"] = {"address": gateway_address}

    resource.update(rsp.desired.composite, {"status": status})

    # 8. Readiness.
    all_ready = True
    not_ready = []

    if _has_condition(req, "gke-cluster", "Ready"):
        rsp.desired.resources["gke-cluster"].ready = fnv1.READY_TRUE
    else:
        all_ready = False
        not_ready.append("gke-cluster")

    if "kserve-stack" in rsp.desired.resources:
        if _has_condition(req, "kserve-stack", "Ready"):
            rsp.desired.resources["kserve-stack"].ready = fnv1.READY_TRUE
        else:
            all_ready = False
            not_ready.append("kserve-stack")
    else:
        all_ready = False
        not_ready.append("kserve-stack")

    if all_ready:
        rsp.conditions.append(fnv1.Condition(
            type="Ready",
            status=fnv1.STATUS_CONDITION_TRUE,
            reason="Available",
            target=fnv1.TARGET_COMPOSITE_AND_CLAIM,
        ))
    else:
        rsp.conditions.append(fnv1.Condition(
            type="Ready",
            status=fnv1.STATUS_CONDITION_FALSE,
            reason="Creating",
            message=f"Waiting for: {', '.join(not_ready)}",
            target=fnv1.TARGET_COMPOSITE_AND_CLAIM,
        ))
