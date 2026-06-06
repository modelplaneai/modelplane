"""Backend dispatch for compose-model-replica.

A backend turns a ModelReplica + its InferenceCluster into the cluster-level
serving resources. Backends return provider-kubernetes Objects and/or
provider-helm Releases; the dispatcher (fn.py) applies them to the response.
"""

from typing import Protocol

from crossplane.function import resource
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

# A composed resource is either a provider-kubernetes Object or a
# provider-helm Release. fn.py writes these into the response by key.
ComposedResource = k8sobjv1alpha1.Object | helmv1beta1.Release

# Backend identifiers.
NATIVE = "native"
LLMD = "llmd"
DYNAMO = "dynamo"

# Mount path the cache PVC is exposed at inside every engine pod. Intrinsic
# to the cache contract; the deployment points the engine here.
CACHE_MOUNT_PATH = "/mnt/models"

# Volume name shared by the PVC volume and its mount.
_CACHE_VOLUME = "model-cache"


def cache_pvc_name(namespace: str, cache_name: str) -> str:
    # MUST stay in sync with compose-model-cache's _pvc_name()
    # (functions/compose-model-cache/function/fn.py) — both sides share
    # resource.child_name("modelcache", namespace, name). The namespace
    # qualifier keeps caches of the same name from different Modelplane
    # namespaces from colliding in the workload cluster's `default` namespace.
    return resource.child_name("modelcache", namespace, cache_name)


def cache_mounts(replica: v1alpha1.ModelReplica) -> tuple[list[dict], list[dict]]:
    """Return (volumes, volumeMounts) for the replica's cache, or ([], []).

    modelCacheRef carries only a name; the ModelCache is in the replica's own
    namespace, so the PVC name is qualified by replica.metadata.namespace.
    """
    ref = replica.spec.modelCacheRef
    if not ref:
        return [], []
    pvc = cache_pvc_name(replica.metadata.namespace, ref.name)
    # Mounted read-write (NOT readOnly): engines write into the model dir
    # (tokenizer/compile/lock artifacts), and a readOnly mount hard-fails them.
    # The PVC is ReadWriteMany, so every pod in the gang shares one read-write
    # mount; the hydration Job populates it once and serving pods read N times.
    return (
        [{"name": _CACHE_VOLUME, "persistentVolumeClaim": {"claimName": pvc}}],
        [{"name": _CACHE_VOLUME, "mountPath": CACHE_MOUNT_PATH}],
    )


def apply_cache_args(args: list[str], replica: v1alpha1.ModelReplica, engine) -> list[str]:
    """Inject --model=<mount> for the turnkey vLLM path only.

    KServe used to inject this; nothing does now, and without it vLLM silently
    serves facebook/opt-125m. It is vLLM-specific (the `--model` flag), so it is
    skipped when:
    - no cache is referenced;
    - the engine brings its own `command` — a non-vLLM engine like SGLang owns
      its args and points at the mount with its own flag (`--model-path`), so
      injecting `--model` would hand it an unknown flag; or
    - the user already set `--model`.

    The cache *volume/mount* (cache_mounts) is added regardless of engine shape;
    only this arg injection is vLLM-specific.
    """
    if not replica.spec.modelCacheRef or engine.command:
        return args
    if any(a == "--model" or a.startswith("--model=") for a in args):
        return args
    return [*args, f"--model={CACHE_MOUNT_PATH}"]


# Namespace for serving workloads (and their ResourceClaimTemplate) on remote
# clusters.
REMOTE_NAMESPACE = "default"

# Response resource key for the DRA ResourceClaimTemplate.
RESOURCE_CLAIM_KEY = "resource-claim"

# DRA API the ResourceClaimTemplate targets. The manifest is a raw dict wrapped
# in a provider-kubernetes Object, so no generated model is needed.
_DRA_API_VERSION = "resource.k8s.io/v1"

# Name of the pod-level claim that references the per-replica
# ResourceClaimTemplate, and the suffix of the template's own name. Containers
# reference individual requests within the claim.
_POD_CLAIM_NAME = "devices"


def wrap_object(provider_config: str, manifest: dict, *, readiness: str = "DeriveFromObject") -> k8sobjv1alpha1.Object:
    """Wrap a raw manifest in a provider-kubernetes Object for a remote cluster.

    readiness defaults to DeriveFromObject (the workload resources report their
    own status). Pass SuccessfulCreate for resources that have no runtime status
    to derive from - a ResourceClaimTemplate is a template, never reconciled, so
    DeriveFromObject would leave its Object permanently not-ready.
    """
    return k8sobjv1alpha1.Object(
        spec=k8sobjv1alpha1.Spec(
            providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                kind="ClusterProviderConfig",
                name=provider_config,
            ),
            readiness=k8sobjv1alpha1.Readiness(policy=readiness),
            forProvider=k8sobjv1alpha1.ForProvider(manifest=manifest),
        ),
    )


def engine_container(replica: v1alpha1.ModelReplica):
    """Return the container named 'engine'. The XRD's CEL validation
    guarantees exactly one exists, so this always succeeds.

    v0.1 constrains the template to a single container (the engine) via the
    XRD (containers maxItems: 1), so there is nothing to drop. Sidecar /
    multi-container support is tracked in #108 — it needs design for the LWS
    gang (which containers run on the leader vs the workers).
    """
    return next(c for c in replica.spec.workers.template.spec.containers if c.name == "engine")


def nodes_per_worker(replica: v1alpha1.ModelReplica) -> int:
    """Nodes spanned by one worker.

    v0.1 topology implements only tensor + pipeline, so this is `pipeline`.
    When data/dataLocal land, this becomes pipeline * (data / dataLocal).
    """
    return int(replica.spec.workers.topology.pipeline or 1)


def needs_cross_pod_coordination(replica: v1alpha1.ModelReplica) -> bool:
    """True when the replica is more than one self-contained pod.

    v0.1: true iff nodes_per_worker > 1. Extension points (no-ops until the
    fields exist): a `prefill` block (disaggregated P/D) or multi-node data
    parallelism (data > dataLocal) also make this true.
    """
    return nodes_per_worker(replica) > 1


def select_backend(replica: v1alpha1.ModelReplica) -> str:
    """Pick the lightest serving path. No user-facing backend field.

    Dynamo is dormant in v0.1: no Dynamo-only capability is wired, so a
    multi-pod replica always selects llm-d.
    """
    if not needs_cross_pod_coordination(replica):
        return NATIVE
    return LLMD


def _device_requests(replica: v1alpha1.ModelReplica):
    """Resolved claim: DRA device requests stamped by compose-model-deployment."""
    return replica.spec.deviceRequests or []


def claim_template_name(replica: v1alpha1.ModelReplica) -> str:
    """ResourceClaimTemplate name on the remote cluster.

    Per-replica, derived from the replica's own name so concurrent replicas of
    the same deployment on one cluster don't collide.
    """
    return resource.child_name(replica.metadata.name, _POD_CLAIM_NAME)


def engine_resources(replica: v1alpha1.ModelReplica) -> dict:
    """Container resources for the engine.

    When the replica carries DRA device requests, GPUs are bound via a
    ResourceClaim, so the container references the whole claim and sets no
    device-plugin limit. When it has none (the deployment had no nodeSelector),
    fall back to the legacy nvidia.com/gpu limit derived from the topology so the
    engine still gets GPUs.

    We emit one container claim entry referencing the pod-level claim, with no
    `request` field, so the entire claim (all of its device requests) is made
    available to the engine. A per-request entry would need a unique `name` per
    entry - resources.claims is a list-map keyed on `name` alone - and the engine
    uses every device anyway, so referencing the whole claim is both correct and
    simplest.
    """
    requests = _device_requests(replica)
    if not requests:
        return {"limits": {"nvidia.com/gpu": str(replica.spec.workers.topology.tensor)}}
    return {"claims": [{"name": _POD_CLAIM_NAME}]}


def attach_device_claims(pod_spec: dict, replica: v1alpha1.ModelReplica) -> None:
    """Wire a pod spec to the per-replica ResourceClaimTemplate.

    Adds a pod-level resourceClaims entry pointing at the template. No-op when
    there are no device requests (GPU binding then falls back to the device-plugin
    limit set by engine_resources). Every pod that shares this spec - a native
    Deployment pod, or an llm-d LWS leader and worker - gets its own
    template-backed claim, which is why we use a ResourceClaimTemplate rather than
    a shared ResourceClaim.
    """
    if not _device_requests(replica):
        return
    pod_spec["resourceClaims"] = [{"name": _POD_CLAIM_NAME, "resourceClaimTemplateName": claim_template_name(replica)}]


def resource_claim_template(replica: v1alpha1.ModelReplica, provider_config: str) -> k8sobjv1alpha1.Object | None:
    """Compose a DRA ResourceClaimTemplate Object for the replica, or None.

    Each resolved device request (stamped by compose-model-deployment from the
    matched InferenceClass claim: DRA devices) becomes one DeviceRequest carrying
    its DeviceClass, count, and CEL selectors verbatim. Returns None when the
    replica has no device requests.
    """
    requests = _device_requests(replica)
    if not requests:
        return None

    device_requests = []
    for r in requests:
        exactly: dict = {"deviceClassName": r.deviceClassName, "count": int(r.count or 1)}
        selectors = [{"cel": {"expression": s.cel}} for s in (r.selectors or []) if s.cel]
        if selectors:
            exactly["selectors"] = selectors
        device_requests.append({"name": r.name, "exactly": exactly})

    return wrap_object(
        provider_config,
        {
            "apiVersion": _DRA_API_VERSION,
            "kind": "ResourceClaimTemplate",
            "metadata": {"name": claim_template_name(replica), "namespace": REMOTE_NAMESPACE},
            "spec": {"spec": {"devices": {"requests": device_requests}}},
        },
        # A ResourceClaimTemplate has no runtime status; it's ready once applied.
        readiness="SuccessfulCreate",
    )


class Backend(Protocol):
    """Builds the cluster-level serving resources for one ModelReplica."""

    def build(
        self,
        replica: v1alpha1.ModelReplica,
        cluster: icv1alpha1.InferenceCluster,
    ) -> dict[str, ComposedResource]:
        """Return a mapping of response resource-key -> composed resource.

        The caller (fn.py) must pass a cluster whose
        ``status.providerConfigRef.name`` is populated; backends read it to
        target the remote cluster and do not re-default it. Composed resources
        are named after ``replica.metadata.name`` (unique per placement) so
        co-located replicas don't collide.
        """
        ...
