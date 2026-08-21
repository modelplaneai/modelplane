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


def apply_cache_args(args: list[str], replica: v1alpha1.ModelReplica, engine: v1alpha1.Container) -> list[str]:
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
    ref = replica.spec.modelCacheRef
    if not ref or engine.command:
        return args
    if any(a == "--model" or a.startswith("--model=") for a in args):
        return args
    return [*args, f"--model={CACHE_MOUNT_PATH}"]


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

    Gated on the cluster's stack being Dynamo (so the shared metadata-only
    ModelExpress server is present) and the replica referencing a cache (so
    there are weights on a PVC to seed from). Both the native (Standalone) and
    Grove (Leader/Worker gang) backends inject it on Dynamo; a Standalone
    deployment scaled to several replicas is as valid a P2P peer set as a gang.
    Injecting it whenever those conditions hold is harmless: the bundle is
    inert unless the engine opts in (see HF_HUB_OFFLINE note below).

    MX_SERVER_ADDRESS is the current variable; MODELEXPRESS_URL is deprecated
    but still read by every ModelExpress client path and takes precedence
    when both are set, so both are set here during that transition. Both point
    at the per-cluster shared server's Service (modelexpress-server:8001), not
    a per-cache name. The server is metadata-only: it coordinates peer
    discovery and never downloads weights. MX_P2P_METADATA turns on the
    peer-to-peer path, where the weights move GPU-to-GPU between engine pods
    (NIXL) and the server only brokers which peer holds them. HF_HUB_CACHE
    matches the PVC mount so the loader resolves against the pre-staged tree.

    MX_MODEL_REVISION isolates this cache's P2P source identity. ModelExpress
    content-addresses a source from the model path plus revision, but every
    cache mounts at the same CACHE_MOUNT_PATH, so without a distinguishing
    revision two different caches with the same engine config would hash to
    one source id and a replica could pull a peer serving other weights. The
    ModelCache name is stable, identical across every replica of the cache
    (so genuine peers still match), and distinct across caches. It is not the
    resolved HF revision - a cache carries no resolved revision yet - so a
    delete-and-recreate under the same name reuses the id; the ModelMetadata
    CRs are pod-owned and GC'd with the old replicas, so stale peers clear
    themselves. POD_NAME/POD_UID/POD_NAMESPACE let the server set an owner
    reference from this pod onto those CRs.

    Deliberately no HF_HUB_OFFLINE: weights load from the local PVC (a local
    --model path never reaches HuggingFace) or via the P2P loader, so offline
    mode guards nothing on the weight path, while forcing it would break
    engines that fetch auxiliary files by repo id at startup (kimi-k2's gated
    tokenizer, say). With it gone every variable here is read only by the
    ModelExpress loader, so the bundle is genuinely inert unless the engine
    command opts in with --load-format modelexpress - which Modelplane never
    injects, here or anywhere: the ML team's engine command decides whether to
    use ModelExpress's loader at all.
    """
    ref = replica.spec.modelCacheRef
    if not ref or stack != "Dynamo":
        return []
    address = f"{_MODELEXPRESS_SERVER_SERVICE}:{_MODELEXPRESS_PORT}"
    return [
        {"name": "MX_SERVER_ADDRESS", "value": address},
        {"name": "MODELEXPRESS_URL", "value": address},
        {"name": "MX_MODEL_REVISION", "value": ref.name},
        {"name": "HF_HUB_CACHE", "value": CACHE_MOUNT_PATH},
        {"name": "MX_P2P_METADATA", "value": "1"},
        {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
        {"name": "POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
        {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}},
    ]


def modelexpress_security_context(replica: v1alpha1.ModelReplica, stack: str) -> dict | None:
    """IPC_LOCK, which GPUDirect RDMA needs to pin memory, or None unless the
    replica references a cache on a Dynamo cluster.

    Gated the same way as modelexpress_env: only meaningful where the
    ModelExpress P2P path is wired in.
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


# Backend-neutral gang-coordination env vars Modelplane injects (for the LWS
# backend) into every engine container of a multi-node engine's gang: the
# leader's address, and the pod's rank (0 for the leader, 1..worker.nodes for
# each follower). A member's command finds its peers and its own place in the
# gang through these without hard-coding the underlying orchestrator's
# variables. For the LWS backend they alias LWS_LEADER_ADDRESS and
# LWS_WORKER_INDEX; another gang scheduler would alias its own. $(VAR) is
# Kubernetes downward env expansion - the container sees the MODELPLANE_ vars
# resolved to their values.
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


# Grove clique names for a gang engine's two cliques, and the scaling group
# that groups them. Clique names must be unique within a PodCliqueSet and are
# immutable, so these are fixed rather than derived from the member role. The
# two cliques form one PodCliqueScalingGroup, so engine.copies scales the whole
# leader+worker gang as a unit: each copy is an independent gang with its own
# 0-based GROVE_PCSG_POD_INDEX rank space (see GroveBackend).
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

# CEL readiness query for a Grove PodCliqueSet. Grove publishes no Ready or
# Available condition on the PodCliqueSet itself - its only condition type is
# TopologyLevelsUnavailable, which we don't use - so readiness has to be
# derived from its replica counters instead. A PodCliqueSet is available when
# every one of its replicas has all standalone cliques and scaling groups
# above their minAvailable threshold, which Grove rolls up into
# status.availableReplicas; observedGeneration guards against reading a count
# left over from before the last spec change. status.podGangStatuses looks
# like it would be useful here but Grove never populates it.
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
    other. Used for the native Deployment; a Grove gang uses grove_pcs_name
    instead, which budgets for Grove's own name-length validation.
    """
    return resource.child_name(_name(replica.metadata), engine.name)


# Grove validates a *combined* resource name of at most 45 characters. This
# backend nests both cliques in a PodCliqueScalingGroup, so Grove names a gang's
# pod <pcs>-<pcsReplica>-<scalingGroup>-<sgReplica>-<clique>-<podIndex>, and the
# whole thing must fit. Reserve room for that suffix at its longest -
# "-0-gang-63-worker-99" is 20 characters (a single PCS replica, up to 64
# scaling-group copies and worker pods at two digits, and the longer "worker"
# clique) - leaving this much for the PodCliqueSet name itself. That's tighter
# than the 63-character DNS label budget engine_name() otherwise uses.
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
    pod_spec.setdefault("tolerations", []).append(_GPU_TOLERATION)


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
