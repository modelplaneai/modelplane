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

"""Backend dispatch for compose-model-replica.

A backend turns a ModelReplica + its InferenceCluster into the cluster-level
serving resources. Backends return provider-kubernetes Objects; the dispatcher
(fn.py) applies them to the response.
"""

import hashlib
from typing import Protocol

from crossplane.function import resource
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


def _name(meta: metav1.ObjectMeta | None) -> str:
    """The object's name, always set on resources read from the API server."""
    if meta is None or meta.name is None:
        raise ValueError("metadata.name is unexpectedly absent")
    return meta.name


def _namespace(meta: metav1.ObjectMeta | None) -> str:
    """The object's namespace, always set on namespaced resources read from the API server."""
    if meta is None or meta.namespace is None:
        raise ValueError("metadata.namespace is unexpectedly absent")
    return meta.namespace


class Backend(Protocol):
    """Composes a replica engine's cluster-level serving resources."""

    def build(
        self,
        replica: v1alpha1.ModelReplica,
        engine: v1alpha1.Engine,
        provider_config: str,
        serving_label: str,
        stack: str,
    ) -> dict[str, k8sobjv1alpha1.Object]: ...


# Backend identifiers.
NATIVE = "native"
LLMD = "llmd"
GROVE = "grove"

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

    The cache is a per-cache PVC qualified by the replica's namespace
    (modelCacheRef carries only a name, and the ModelCache is in the replica's
    own namespace). The PVC is shared across every engine and member of the
    replica.
    """
    ref = replica.spec.modelCacheRef
    if not ref:
        return [], []
    pvc = cache_pvc_name(_namespace(replica.metadata), ref.name)
    # Mounted read-write (NOT readOnly): engines write into the model dir
    # (tokenizer/compile/lock artifacts), and a readOnly mount hard-fails them.
    # The PVC is ReadWriteMany, so every pod in the gang shares one read-write
    # mount; the hydration Job populates it once and serving pods read N times.
    return (
        [{"name": _CACHE_VOLUME, "persistentVolumeClaim": {"claimName": pvc}}],
        [{"name": _CACHE_VOLUME, "mountPath": CACHE_MOUNT_PATH}],
    )


def cache_env(replica: v1alpha1.ModelReplica) -> list[dict]:
    """Env that makes the cache mount resolvable by the model's own name, or [].

    compose-model-cache stages a HuggingFace source in HuggingFace's own cache
    layout, so pointing HF_HUB_CACHE at the mount lets an engine load by repo id
    against the staged snapshot. The command is then the same cached or not, and
    Modelplane injects no --model of its own (#407). No HF_HUB_OFFLINE: it isn't
    needed to resolve the snapshot, and it breaks an engine that fetches a
    second repo at startup, like kimi-k2's gated tokenizer.

    An engine must name the revision its cache staged. A bare repo id resolves
    at the default branch, so a cache pinned to a commit or tag misses unless
    the engine passes that revision too.

    HF_HUB_CACHE is a cache root, not a pin, so an engine fetching some other
    repo by id writes it to the shared PVC rather than its own filesystem.

    HuggingFace-specific because the source is. A second ModelCache source would
    stage its own layout and gate this on the source.
    """
    if not replica.spec.modelCacheRef:
        return []
    return [{"name": "HF_HUB_CACHE", "value": CACHE_MOUNT_PATH}]


# Well-known name of the per-cluster shared ModelExpress server that
# compose-serving-stack installs in `default` on a Dynamo cluster. One server
# per cluster: engine pods reach it by its Service name. A cross-function
# contract (compose-serving-stack owns the server and Service); the two
# functions hard-code the string independently and must change together.
_MODELEXPRESS_SERVER_SERVICE = "modelexpress-server"

# Port the ModelExpress server listens on. Must stay in sync with
# compose-serving-stack's _MODELEXPRESS_PORT.
_MODELEXPRESS_PORT = 8001


def modelexpress_env(replica: v1alpha1.ModelReplica, stack: str) -> list[dict]:
    """ModelExpress P2P env for an engine pod that references a cache on a
    Dynamo cluster, or [] otherwise.

    Gated on Dynamo, where the metadata-only server runs, and on referencing a
    cache, where there are weights to seed from. A scaled Standalone deployment
    is as valid a peer set as a gang, so the native backend injects it too.
    Every variable here is read only by ModelExpress's client, so the bundle is
    inert unless the engine opts in with --load-format modelexpress, which
    Modelplane never injects.

    MODEL_EXPRESS_URL is deprecated in favour of MX_SERVER_ADDRESS but still
    takes precedence when both are set, so both are set. No cache-directory
    variable: ModelExpress falls back to HF_HUB_CACHE, which cache_env sets.

    MX_MODEL_REVISION isolates this cache's P2P source identity, which
    ModelExpress content-addresses from the model name and revision. Two caches
    of the same repo, neither pinning a revision, can hold different commits
    while both engines report none, so a replica could load a peer's other
    weights. The PVC name is stable across a cache's replicas and distinct
    across caches and namespaces; the resolved commit would be better, but only
    the hydration Job sees it. The cost is that identical caches can't share a
    source.

    POD_* identify the publishing pod. A 0.5.0 server owns the ModelMetadata CRs
    with them so Kubernetes garbage-collects them; the pinned 0.4.1 ignores
    them, so a server bump is a version change rather than a code one.
    """
    ref = replica.spec.modelCacheRef
    if not ref or stack != "Dynamo":
        return []
    address = f"{_MODELEXPRESS_SERVER_SERVICE}:{_MODELEXPRESS_PORT}"
    return [
        {"name": "MX_SERVER_ADDRESS", "value": address},
        {"name": "MODEL_EXPRESS_URL", "value": address},
        {"name": "MX_MODEL_REVISION", "value": cache_pvc_name(_namespace(replica.metadata), ref.name)},
        {"name": "MX_P2P_METADATA", "value": "1"},
        {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
        {"name": "POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
        {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}},
    ]


def modelexpress_security_context(replica: v1alpha1.ModelReplica, stack: str) -> dict | None:
    """IPC_LOCK, which GPUDirect RDMA needs to pin memory, or None unless the
    replica references a cache on a Dynamo cluster.

    Gated like modelexpress_env, but unlike that env this isn't inert. The
    restricted Pod Security Standard permits no added capability except
    NET_BIND_SERVICE, so a workload cluster that labels REMOTE_NAMESPACE
    pod-security.kubernetes.io/enforce=restricted rejects every
    cache-referencing engine on a Dynamo cluster, whether it loads through
    ModelExpress or not. Modelplane doesn't set that label on the clusters it
    provisions, but an Existing cluster it's pointed at might carry it.

    TODO(negz): if that bites someone, narrow this to members whose pool
    declares a fabric? An engine with no fabric can't do RDMA, so it has no use
    for the capability.
    """
    ref = replica.spec.modelCacheRef
    if not ref or stack != "Dynamo":
        return None
    return {"capabilities": {"add": ["IPC_LOCK"]}}


# Namespace for serving workloads (and their ResourceClaimTemplate) on remote
# clusters.
REMOTE_NAMESPACE = "default"

# Port the engine serves its OpenAI-compatible API on. A contract shared with
# the ModelEndpoint URLs, so it must not diverge between backends.
ENGINE_PORT = 8000

# Pod label carrying the serving identity (the replica name). The replica's one
# shared Service selects on it, so every engine's serving pods carry it - a
# Standalone pod, or a gang's leader (a LeaderWorkerSet leader or a Grove leader
# clique's pod, depending on the cluster's stack). A multi-node gang's workers do
# NOT (they don't serve the OpenAI API), so the Service never routes to them.
LABEL_SERVING = "modelplane.ai/serving"

# Pod label scoping a workload's own pods, used as a Deployment's selector. It's
# per-engine (the workload name) so two Standalone engines of one replica - which
# share the serving label for the Service - don't end up with overlapping
# Deployment selectors fighting over each other's pods.
LABEL_WORKLOAD = "modelplane.ai/workload"


def pod_metadata(member: v1alpha1.Member, labels: dict[str, str] | None = None) -> dict:
    """Pod template metadata for a member: its template.metadata plus managed labels.

    The member's template.metadata.labels and .annotations propagate to the pod
    template a backend composes, merged with the managed labels the backend
    passes. The XRDs reject member labels in the reserved modelplane.ai/
    namespace at admission, so a user label can never collide with the managed
    ones (or stamp the serving label onto a worker, routing traffic to a pod
    that doesn't serve the OpenAI API). Returns {} when there is nothing to
    set, so a caller can omit metadata entirely.
    """
    user = member.template.metadata
    merged = dict((user.labels if user else None) or {})
    merged.update(labels or {})
    meta: dict = {}
    if merged:
        meta["labels"] = merged
    if user and user.annotations:
        meta["annotations"] = dict(user.annotations)
    return meta


# Backend-neutral aliases for the orchestrator's own gang-coordination vars, so
# a member's command doesn't name LWS or Grove directly. Rank is LWS-only;
# Grove has no equivalent yet (see grove_leader_address_env).
LEADER_ADDRESS_ENV = "MODELPLANE_LEADER_ADDRESS"
RANK_ENV = "MODELPLANE_RANK"
_LWS_LEADER_ADDRESS_ENV = "LWS_LEADER_ADDRESS"
_LWS_WORKER_INDEX_ENV = "LWS_WORKER_INDEX"


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


def rank_env() -> dict:
    """The MODELPLANE_RANK env entry for the LWS backend.

    Aliases LWS_WORKER_INDEX, which LeaderWorkerSet injects into every gang pod
    - 0 on the leader, 1..size-1 on the followers - so one alias serves both
    the leader and the worker templates. The same ordering caveat as
    leader_address_env applies.
    """
    return {"name": RANK_ENV, "value": f"$({_LWS_WORKER_INDEX_ENV})"}


_GROVE_PCSG_NAME_ENV = "GROVE_PCSG_NAME"
_GROVE_PCSG_INDEX_ENV = "GROVE_PCSG_INDEX"
_GROVE_HEADLESS_SERVICE_ENV = "GROVE_HEADLESS_SERVICE"


def grove_leader_address_env() -> dict:
    """The MODELPLANE_LEADER_ADDRESS env entry for the Grove backend.

    Concatenating the PCSG name and index reproduces Grove's own PodClique name,
    and the leader clique holds one pod, so this resolves to the leader of *this
    gang*. The PCS-scoped vars are identical across gangs and would point every
    engine.copies at gang 0's leader.

    Needs Grove to inject its vars before template env for the expansion to see
    them (grove#753, first released in v0.1.0-alpha.12-rc2, which the serving
    stack pins).
    """
    leader_pod = f"$({_GROVE_PCSG_NAME_ENV})-$({_GROVE_PCSG_INDEX_ENV})-{GROVE_LEADER_CLIQUE}-0"
    address = leader_pod + f".$({_GROVE_HEADLESS_SERVICE_ENV})"
    return {"name": LEADER_ADDRESS_ENV, "value": address}


# Clique names are immutable and must be unique within a PodCliqueSet, so they're
# fixed rather than derived from the member role. Both cliques sit in one scaling
# group, so engine.copies scales the leader+worker pair as a unit.
GROVE_LEADER_CLIQUE = "leader"
GROVE_WORKER_CLIQUE = "worker"
GROVE_PCSG = "gang"

# The scheduler every Grove-composed PodCliqueSet's pods name, and the KAI
# queue they're labelled into (see compose-serving-stack's compose_kai_queues).
GROVE_SCHEDULER_NAME = "kai-scheduler"
GROVE_QUEUE_LABEL = "kai.scheduler/queue"
GROVE_QUEUE = "modelplane"


# Response resource keys. A replica's HTTPRoute keeps a stable key; each engine's
# workload gets an engine-scoped key and each member's claim a member-scoped one
# (the engine name plus the member role) so a multi-engine replica's resources
# don't collide in the response map.
ROUTE_KEY = "model-route"
_WORKLOAD_KEY = "model-serving"
_CLAIM_KEY = "resource-claim"

# HTTPRoute request timeout for model traffic. "0s" disables it (Gateway API
# semantics). Without an explicit timeout the gateway applies its own default
# (Envoy's is 15s), which severs token streaming mid-generation — any response
# longer than that dies with an incomplete-body error. LLM generation time is
# unbounded by design (it scales with output length), so we disable the
# request timeout and rely on the gateway's stream-idle timeout to reap
# genuinely stuck connections.
REQUEST_TIMEOUT = "0s"


def workload_key(engine: v1alpha1.Engine) -> str:
    """Response key for an engine's workload (Deployment, LeaderWorkerSet, or PodCliqueSet)."""
    return f"{_WORKLOAD_KEY}-{engine.name}"


def member_role(member: v1alpha1.Member) -> str:
    """A member's role, lowercased, defaulting to standalone.

    The discriminator for a member's claim key and ResourceClaimTemplate name.
    Unique per member only while the XRD caps an engine at one member per role
    (members maxItems: 2); if multiple Workers ever become valid this needs a
    finer discriminator.
    """
    return (member.role or ROLE_STANDALONE).lower()


def claim_key(engine: v1alpha1.Engine, member: v1alpha1.Member) -> str:
    """Response key for a member's ResourceClaimTemplate.

    One per member that claims devices: a member's pods all claim the same
    devices through the same template (a template stamps a fresh claim per
    pod), but an engine's members may claim different devices, or none. The
    member role disambiguates - an engine has at most one member per role.
    """
    return f"{_CLAIM_KEY}-{engine.name}-{member_role(member)}"


def workload_keys(replica: v1alpha1.ModelReplica) -> list[str]:
    """Response keys of every engine's workload, in engine order.

    fn.py tracks replica readiness across all of these: a replica is serving
    only when every engine's workload is ready.
    """
    return [workload_key(g) for g in replica.spec.engines]


# DRA API the ResourceClaimTemplate targets. The manifest is a raw dict wrapped
# in a provider-kubernetes Object, so no generated model is needed.
_DRA_API_VERSION = "resource.k8s.io/v1"

# Name of the pod-level claim that references the per-replica
# ResourceClaimTemplate, and the suffix of the template's own name. Containers
# reference individual requests within the claim.
_POD_CLAIM_NAME = "devices"

# CEL readiness query matching a Deployment's or LeaderWorkerSet's
# all-replicas-available signal, an Available=True condition. Both publish this
# condition when their desired replicas are up but neither publishes a Ready
# condition, so provider-kubernetes' DeriveFromObject policy (which only checks a
# Ready condition) can never mark them ready. The has() guard keeps the query
# false (not erroring) before the workload first writes status.conditions.
AVAILABLE_CEL = (
    'has(object.status.conditions) && object.status.conditions.exists(c, c.type == "Available" && c.status == "True")'
)

# Grove publishes no Ready or Available condition on a PodCliqueSet, so
# readiness comes from its replica counters: a replica counts as available once
# every clique and scaling group is at or above minAvailable. observedGeneration
# guards against a count left over from before the last spec change.
# status.podGangStatuses looks useful here but Grove never populates it.
GROVE_AVAILABLE_CEL = (
    "has(object.status) && has(object.status.observedGeneration) && "
    "object.status.observedGeneration == object.metadata.generation && "
    "object.spec.replicas > 0 && has(object.status.availableReplicas) && "
    "object.status.availableReplicas >= object.spec.replicas"
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


def serving_label(replica: v1alpha1.ModelReplica) -> str:
    """The serving label value a replica's serving pods share.

    The replica name, so the shared Service selects every engine's leader and
    Standalone pods.
    """
    return _name(replica.metadata)


def engine_container(member: v1alpha1.Member) -> v1alpha1.Container:
    """Return a member's container named 'engine'. The XRD's CEL validation
    guarantees exactly one exists per member, so this always succeeds.

    v0.1 constrains the template to a single container (the engine) via the
    XRD (containers maxItems: 1), so there is nothing to drop. Sidecar /
    multi-container support is tracked in #108 — it needs design for the Grove
    gang (which containers run on the leader vs the worker clique).
    """
    # An engine member carries its container in template.spec. The XRD types
    # spec as optional but a member with no spec defines no pod to serve, so
    # reaching here without one is a malformed replica.
    assert member.template.spec is not None
    return next(c for c in member.template.spec.containers if c.name == "engine")


def engine_member(engine: v1alpha1.Engine, role: str) -> v1alpha1.Member | None:
    """The engine's member with this role, or None.

    An engine has at most one member of each role (a single Standalone, or one
    Leader and one Worker), so the first match is the only match.
    """
    return next((m for m in engine.members if (m.role or ROLE_STANDALONE) == role), None)


def select_backend(engine: v1alpha1.Engine, stack: str) -> str:
    """Pick the serving path for an engine from its member roles.

    A single Standalone member is a self-contained pod, served natively as a
    Deployment. A Leader plus Worker gang coordinates across nodes, served by
    the cluster's chosen stack: Standard (a LeaderWorkerSet, the LLMD backend)
    or Dynamo (a Grove PodCliqueSet).
    """
    if engine_member(engine, ROLE_STANDALONE) is not None:
        return NATIVE
    return GROVE if stack == "Dynamo" else LLMD


def engine_name(replica: v1alpha1.ModelReplica, engine: v1alpha1.Engine) -> str:
    """The base name for an engine's composed workload and claim resources.

    Every engine's resources are qualified by the engine name: per-replica so
    co-located replicas of one deployment don't collide on the remote cluster,
    and per-engine so a multi-engine replica's workloads don't collide with each
    other. Names the native Deployment and the llm-d LeaderWorkerSet; a Grove
    gang uses grove_pcs_name instead, which budgets for Grove's own name-length
    validation.
    """
    return resource.child_name(_name(replica.metadata), engine.name)


# Grove rejects a PodCliqueSet whose resource names sum past 45 characters:
# len(pcs) + len(pcsg) + len(pclq). With "gang" and "worker" that leaves 35 for
# the PodCliqueSet name. This reserves more than it has to, since the composed
# pod name carries replica indices and Grove's own random suffix on top, and
# it's tighter than the 63-character DNS label budget engine_name() uses.
_GROVE_PCS_NAME_MAX = 24


def grove_pcs_name(replica: v1alpha1.ModelReplica, engine: v1alpha1.Engine) -> str:
    """The PodCliqueSet name for a gang engine.

    Same shape as resource.child_name (a deterministic hash suffix keeps two
    truncated-to-the-same-prefix names from colliding), but truncated to
    Grove's tighter name budget rather than the general 63-character DNS
    label limit.
    """
    full = f"{_name(replica.metadata)}-{engine.name}"
    h = hashlib.sha256(full.encode()).hexdigest()[:5]
    prefix = full[: _GROVE_PCS_NAME_MAX - len(h) - 1].rstrip("-")
    return f"{prefix}-{h}"


def claim_template_name(replica: v1alpha1.ModelReplica, engine: v1alpha1.Engine, member: v1alpha1.Member) -> str:
    """ResourceClaimTemplate name for a member.

    Per-replica, per-engine, per-member-role: derived from the same parts as
    engine_name (flat, not nested through engine_name's already-hashed result,
    so the name reads replica-engine-role-devices-hash) so concurrent replicas
    of the same deployment on one cluster stay distinct. One template serves
    every pod of the member - a template stamps a fresh claim per pod - but an
    engine's members may claim different devices, so each claiming member gets
    its own.
    """
    return resource.child_name(_name(replica.metadata), engine.name, member_role(member), _POD_CLAIM_NAME)


def engine_resources() -> dict:
    """Container resources for a claiming member's engine container.

    GPUs bind only via DRA: the engine references the pod-level claim backed by
    the member's ResourceClaimTemplate and never sets a device-plugin
    extended-resource limit. Only meaningful for a member with device requests;
    a claimless member's pod has no pod-level claim to reference, so its
    container carries no resources at all.

    We emit one container claim entry referencing the pod-level claim, with no
    `request` field, so the entire claim (all of its device requests) is made
    available to the engine. A per-request entry would need a unique `name` per
    entry - resources.claims is a list-map keyed on `name` alone - and the engine
    uses every device anyway, so referencing the whole claim is both correct and
    simplest.
    """
    return {"claims": [{"name": _POD_CLAIM_NAME}]}


# Taints GPU node groups carry so non-GPU pods don't land on them. A pod that
# claims a GPU must tolerate its pool's taint to schedule there. With GPUs
# bound via DRA (not the device plugin's extended resource), nothing injects
# these tolerations for us - the ExtendedResourceToleration admission
# controller only acts on nvidia.com/gpu resource requests, which DRA pods
# don't make. The pool carries one vendor's taint; tolerating the other
# vendor's too is harmless, so both are always added rather than threading
# the vendor through here.
_GPU_TOLERATIONS = [
    {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
    {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
]

# Node label identifying the pool a node belongs to. compose-eks-cluster and
# compose-gke-cluster stamp it on every node group they provision; the scheduler
# pins a replica to a pool by name, and we steer the pod onto that pool by
# selecting this label. For BYO (Existing) clusters Modelplane doesn't provision
# the nodes, so the operator must label their pool's nodes with this key for the
# pod to schedule (documented on the InferenceClass XRD).
_LABEL_POOL = "modelplane.ai/pool"


def place_pod(pod_spec: dict, replica: v1alpha1.ModelReplica, engine: v1alpha1.Engine, member: v1alpha1.Member) -> None:
    """Constrain a member's serving pod to the placement the scheduler chose.

    Pins the pod to its member's scheduled node pool, wires it to claim its
    GPUs via DRA through the member's claim, and tolerates the GPU node taint.
    Every pod of one member shares this - a native Deployment pod, a
    LeaderWorkerSet leader or worker pod, or a Grove leader or worker clique
    pod.

    The pool nodeSelector is what makes the scheduler's pool choice real: the
    control-plane scheduler matched a pool and stamped the member's
    nodePoolName, but DRA would otherwise place the pod on any pool whose
    devices satisfy the claim. Without the pin the control plane's per-pool
    capacity accounting drifts from where pods actually run, and a
    claim: Synthetic device (matched for placement but never claimed) isn't
    enforced at all, since pool selection is its only enforcement. nodePoolName
    is XRD-required, so it's always set.

    A claiming member's pods reference its ResourceClaimTemplate; a
    template-backed claim (not a shared ResourceClaim) gives each pod its own
    claim. A claimless member - one with no deviceRequests, like a
    coordinator-only leader - gets no claim at all: only the pool pin places
    its pods, packed onto the gang's nodes by the cluster's scheduler. It still
    tolerates the GPU taint, since the pool it rides along on is a GPU pool.
    """
    pod_spec["nodeSelector"] = {_LABEL_POOL: member.nodePoolName}
    if member.deviceRequests:
        pod_spec["resourceClaims"] = [
            {"name": _POD_CLAIM_NAME, "resourceClaimTemplateName": claim_template_name(replica, engine, member)}
        ]
    pod_spec.setdefault("tolerations", []).extend(_GPU_TOLERATIONS)


def resource_claim_template(
    replica: v1alpha1.ModelReplica, engine: v1alpha1.Engine, member: v1alpha1.Member, provider_config: str
) -> k8sobjv1alpha1.Object:
    """Compose a DRA ResourceClaimTemplate Object for a member.

    Each resolved device request (stamped by compose-model-deployment from the
    matched InferenceClass claim: DRA devices) becomes one DeviceRequest carrying
    its DeviceClass, count, and CEL selectors verbatim. Only called for a member
    with device requests; a claimless member composes no template. One template
    serves every pod of the member, and DRA stamps a fresh claim per pod.
    """
    # Callers (the backends) gate this on `if member.deviceRequests`, so it's
    # only reached for a member that claims devices; a claimless member composes
    # no template.
    assert member.deviceRequests is not None
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
            "metadata": {"name": claim_template_name(replica, engine, member), "namespace": REMOTE_NAMESPACE},
            "spec": {"spec": {"devices": {"requests": device_requests}}},
        },
    )
