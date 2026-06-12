"""Backend dispatch for compose-model-replica.

A backend turns a ModelReplica + its InferenceCluster into the cluster-level
serving resources. Backends return provider-kubernetes Objects and/or
provider-helm Releases; the dispatcher (fn.py) applies them to the response.
"""

from crossplane.function import resource
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

# Member roles.
ROLE_STANDALONE = "Standalone"
ROLE_LEADER = "Leader"
ROLE_WORKER = "Worker"

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
    namespace, so the PVC name is qualified by replica.metadata.namespace. The
    cache is shared across every group and member of the replica.
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

# Port the engine serves its OpenAI-compatible API on. A contract shared with
# the ModelEndpoint URLs, so it must not diverge between backends.
ENGINE_PORT = 8000

# Pod label carrying the serving identity (the replica name). The replica's one
# shared Service selects on it, so every group's serving pods - a Standalone pod
# or an LWS gang leader - carry it. A multi-node gang's worker followers do NOT
# (they don't serve the OpenAI API), so the Service never routes to them.
LABEL_SERVING = "modelplane.ai/serving"

# Pod label scoping a workload's own pods, used as a Deployment's selector. It's
# per-group (the workload name) so two Standalone groups of one replica - which
# share the serving label for the Service - don't end up with overlapping
# Deployment selectors fighting over each other's pods.
LABEL_WORKLOAD = "modelplane.ai/workload"

# Backend-neutral env var carrying the gang leader's address, injected into
# every engine container of a multi-node group. A member's command finds its
# peers through this without hard-coding the underlying orchestrator's variable.
# For the LWS backend it aliases LWS_LEADER_ADDRESS; another gang scheduler would
# alias its own. $(VAR) is Kubernetes downward env expansion - the container
# sees MODELPLANE_LEADER_ADDRESS resolved to the leader's address.
LEADER_ADDRESS_ENV = "MODELPLANE_LEADER_ADDRESS"
_LWS_LEADER_ADDRESS_ENV = "LWS_LEADER_ADDRESS"


def leader_address_env() -> dict:
    """The MODELPLANE_LEADER_ADDRESS env entry for the LWS backend.

    Aliases LWS_LEADER_ADDRESS (injected by LeaderWorkerSet into every gang pod)
    via dependent env expansion. Place it ahead of the user's env entries so
    they can reference $(MODELPLANE_LEADER_ADDRESS) - expansion is
    left-to-right. (In the running pod it isn't literally first: LWS prepends
    its own LWS_* vars ahead of the container's env, which is also what makes
    the $(LWS_LEADER_ADDRESS) reference here resolve.)
    """
    return {"name": LEADER_ADDRESS_ENV, "value": f"$({_LWS_LEADER_ADDRESS_ENV})"}


# Response resource keys. A replica's shared Service and HTTPRoute keep stable
# keys; each group's workload and each member's claim get a group/member-scoped
# key (the group name, and the member role for claims) so a multi-group replica's
# resources don't collide in the response map.
SERVICE_KEY = "model-service"
ROUTE_KEY = "model-route"
_WORKLOAD_KEY = "model-serving"
_CLAIM_KEY = "resource-claim"


def workload_key(group) -> str:
    """Response key for a group's workload (Deployment or LeaderWorkerSet)."""
    return f"{_WORKLOAD_KEY}-{group.name}"


def claim_key(group, member) -> str:
    """Response key for one member's ResourceClaimTemplate."""
    role = (member.role or ROLE_STANDALONE).lower()
    return f"{_CLAIM_KEY}-{group.name}-{role}"


def workload_keys(replica: v1alpha1.ModelReplica) -> list[str]:
    """Response keys of every group's workload, in group order.

    fn.py tracks replica readiness across all of these: a replica is serving
    only when every group's workload is ready.
    """
    return [workload_key(g) for g in replica.spec.workers]


# DRA API the ResourceClaimTemplate targets. The manifest is a raw dict wrapped
# in a provider-kubernetes Object, so no generated model is needed.
_DRA_API_VERSION = "resource.k8s.io/v1"

# Name of the pod-level claim that references the per-replica
# ResourceClaimTemplate, and the suffix of the template's own name. Containers
# reference individual requests within the claim.
_POD_CLAIM_NAME = "devices"

# CEL readiness query matching workloads whose all-replicas-available signal is
# an Available=True condition. Both a Deployment and a LeaderWorkerSet publish
# this condition when their desired replicas are up; neither publishes a Ready
# condition, so provider-kubernetes' DeriveFromObject policy (which only checks
# a Ready condition) can never mark them ready. The has() guard keeps the query
# false (not erroring) before the workload first writes status.conditions.
AVAILABLE_CEL = (
    'has(object.status.conditions) && object.status.conditions.exists(c, c.type == "Available" && c.status == "True")'
)


def wrap_object(
    provider_config: str,
    manifest: dict,
    *,
    cel_query: str | None = None,
) -> k8sobjv1alpha1.Object:
    """Wrap a raw manifest in a provider-kubernetes Object for a remote cluster.

    Readiness defaults to SuccessfulCreate: the Object is ready once applied.
    That's right for resources with no meaningful runtime readiness (a Service,
    an HTTPRoute, or a ResourceClaimTemplate that's never reconciled). Pass
    cel_query for a workload whose readiness must reflect its observed status -
    it selects the DeriveFromCelQuery policy with that query (see AVAILABLE_CEL).
    """
    readiness = (
        k8sobjv1alpha1.Readiness(policy="DeriveFromCelQuery", celQuery=cel_query)
        if cel_query is not None
        else k8sobjv1alpha1.Readiness(policy="SuccessfulCreate")
    )
    return k8sobjv1alpha1.Object(
        spec=k8sobjv1alpha1.Spec(
            providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                kind="ClusterProviderConfig",
                name=provider_config,
            ),
            readiness=readiness,
            forProvider=k8sobjv1alpha1.ForProvider(manifest=manifest),
        ),
    )


def serving_resources(replica: v1alpha1.ModelReplica, provider_config: str) -> dict[str, k8sobjv1alpha1.Object]:
    """Compose the Service and HTTPRoute that front a replica's serving pods.

    One Service spans every group's serving pods (Standalone pods and LWS
    leaders) via the shared serving label; one HTTPRoute exposes that Service at
    the replica's per-placement path. Built once per replica, independent of how
    many groups it has - the unified serving surface the design's Unified mode
    describes.

    Named after the replica (unique per placement) so co-located replicas on one
    cluster don't collide. The replica name is reserved for these serving
    resources; workloads are named per group (see group_name) so a
    LeaderWorkerSet never shares this Service's name and its controller can
    create the headless gang-DNS Service it needs. The route attaches to the
    workload cluster's inference gateway; the control plane rewrites the public
    /<ns>/<service>/ prefix to this replica's /<ns>/<replica>/, which the route
    strips to /.
    """
    name = replica.metadata.name
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": REMOTE_NAMESPACE},
        "spec": {"selector": {LABEL_SERVING: name}, "ports": [{"port": 80, "targetPort": ENGINE_PORT}]},
    }
    http_route = {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {"name": name, "namespace": REMOTE_NAMESPACE},
        "spec": {
            "parentRefs": [{"name": "inference-gateway", "namespace": "modelplane-system"}],
            "rules": [
                {
                    "matches": [{"path": {"type": "PathPrefix", "value": f"/{replica.metadata.namespace}/{name}/"}}],
                    "filters": [
                        {
                            "type": "URLRewrite",
                            "urlRewrite": {"path": {"type": "ReplacePrefixMatch", "replacePrefixMatch": "/"}},
                        }
                    ],
                    "backendRefs": [{"name": name, "port": 80}],
                }
            ],
        },
    }
    return {
        SERVICE_KEY: wrap_object(provider_config, service),
        ROUTE_KEY: wrap_object(provider_config, http_route),
    }


def serving_label(replica: v1alpha1.ModelReplica) -> str:
    """The serving label value a replica's serving pods share.

    The replica name, so the shared Service selects every group's leader and
    Standalone pods.
    """
    return replica.metadata.name


def engine_container(member):
    """Return a member's container named 'engine'. The XRD's CEL validation
    guarantees exactly one exists per member, so this always succeeds.

    v0.1 constrains the template to a single container (the engine) via the
    XRD (containers maxItems: 1), so there is nothing to drop. Sidecar /
    multi-container support is tracked in #108 — it needs design for the LWS
    gang (which containers run on the leader vs the workers).
    """
    return next(c for c in member.template.spec.containers if c.name == "engine")


def group_member(group, role: str):
    """The group's member with this role, or None.

    A group has at most one member of each role (a single Standalone, or one
    Leader and one Worker), so the first match is the only match.
    """
    return next((m for m in group.members if (m.role or ROLE_STANDALONE) == role), None)


def select_backend(group) -> str:
    """Pick the serving path for a group from its member roles.

    A single Standalone member is a self-contained pod, served natively as a
    Deployment. A Leader plus Worker gang coordinates across nodes, served by
    llm-d as a LeaderWorkerSet. Dynamo is dormant in v0.1.
    """
    if group_member(group, ROLE_STANDALONE) is not None:
        return NATIVE
    return LLMD


def group_name(replica: v1alpha1.ModelReplica, group) -> str:
    """The base name for a group's composed workload and claim resources.

    Every group's resources are qualified by the group name: per-replica so
    co-located replicas of one deployment don't collide on the remote cluster,
    and per-group so a multi-group replica's workloads don't collide with each
    other.

    Crucially this name always differs from the replica name, which the shared
    serving Service and HTTPRoute use. A LeaderWorkerSet's controller creates a
    headless Service named after the LWS for gang pod DNS (the leader address
    the followers join) - but only if no Service of that name exists. If the
    LWS shared the serving Service's name, that headless Service would never be
    created, gang DNS would never resolve, and the gang could never form.
    """
    return resource.child_name(replica.metadata.name, group.name)


def claim_template_name(replica: v1alpha1.ModelReplica, group, member) -> str:
    """ResourceClaimTemplate name for one member of a group.

    Per-replica, per-group, per-member: derived from the same parts as
    group_name plus the member role (flat, not nested through group_name's
    already-hashed result, so the name reads replica-group-role-devices-hash) so
    a gang's Leader and Worker claims don't collide, and concurrent replicas of
    the same deployment on one cluster stay distinct.
    """
    role = (member.role or ROLE_STANDALONE).lower()
    return resource.child_name(replica.metadata.name, group.name, role, _POD_CLAIM_NAME)


def engine_resources() -> dict:
    """Container resources for the engine.

    GPUs bind only via DRA: the engine references the pod-level claim backed by
    the replica's ResourceClaimTemplate and never sets a device-plugin
    extended-resource limit. Every replica carries device requests (the XRD
    requires them), so the claim always exists.

    We emit one container claim entry referencing the pod-level claim, with no
    `request` field, so the entire claim (all of its device requests) is made
    available to the engine. A per-request entry would need a unique `name` per
    entry - resources.claims is a list-map keyed on `name` alone - and the engine
    uses every device anyway, so referencing the whole claim is both correct and
    simplest.
    """
    return {"claims": [{"name": _POD_CLAIM_NAME}]}


# Taint GPU node groups carry so non-GPU pods don't land on them. A pod that
# claims a GPU must tolerate it to schedule there. With GPUs bound via DRA (not
# the device plugin's extended resource), nothing injects this toleration for us
# - the ExtendedResourceToleration admission controller only acts on
# nvidia.com/gpu resource requests, which DRA pods don't make.
_GPU_TOLERATION = {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}

# Node label identifying the pool a node belongs to. compose-eks-cluster and
# compose-gke-cluster stamp it on every node group they provision; the scheduler
# pins a replica to a pool by name, and we steer the pod onto that pool by
# selecting this label. For BYO (Existing) clusters Modelplane doesn't provision
# the nodes, so the operator must label their pool's nodes with this key for the
# pod to schedule (documented on the InferenceClass XRD).
_LABEL_POOL = "modelplane.ai/pool"


def place_pod(pod_spec: dict, replica: v1alpha1.ModelReplica, group, member) -> None:
    """Constrain a member's serving pod to the placement the scheduler chose.

    Pins the pod to its group's scheduled node pool, wires it to claim its GPUs
    via DRA through the member's claim, and tolerates the GPU node taint. Every
    pod of one member shares this - a native Deployment pod, or an llm-d LWS
    leader or worker.

    The pool nodeSelector is what makes the scheduler's pool choice real: the
    control-plane scheduler matched a pool and stamped the group's nodePoolName,
    but DRA would otherwise place the pod on any pool whose devices satisfy the
    claim. Without the pin the control plane's per-pool capacity accounting
    drifts from where pods actually run, and a claim: Synthetic device (matched
    for placement but never claimed) isn't enforced at all, since pool selection
    is its only enforcement. nodePoolName is XRD-required, so it's always set.

    The DRA claim references the member's ResourceClaimTemplate; every member
    carries device requests (the XRD requires them), so every serving pod claims
    through DRA. A template-backed claim (not a shared ResourceClaim) gives each
    pod its own claim.
    """
    pod_spec["nodeSelector"] = {_LABEL_POOL: group.nodePoolName}
    pod_spec["resourceClaims"] = [
        {"name": _POD_CLAIM_NAME, "resourceClaimTemplateName": claim_template_name(replica, group, member)}
    ]
    pod_spec.setdefault("tolerations", []).append(_GPU_TOLERATION)


def resource_claim_template(
    replica: v1alpha1.ModelReplica, group, member, provider_config: str
) -> k8sobjv1alpha1.Object:
    """Compose a DRA ResourceClaimTemplate Object for one member of a group.

    Each resolved device request (stamped by compose-model-deployment from the
    matched InferenceClass claim: DRA devices) becomes one DeviceRequest carrying
    its DeviceClass, count, and CEL selectors verbatim. Every member carries at
    least one device request (the XRD requires them).
    """
    device_requests = []
    for r in member.deviceRequests:
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
            "metadata": {"name": claim_template_name(replica, group, member), "namespace": REMOTE_NAMESPACE},
            "spec": {"spec": {"devices": {"requests": device_requests}}},
        },
    )
