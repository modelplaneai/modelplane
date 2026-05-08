"""Pure builders: ModelReplica + InferenceCluster + InferenceClass(es) → dicts.

Builds the KServe LLMInferenceService spec, the DRA ResourceClaim spec,
the DRA selector CEL derived from class capabilities, and the KAI
PodGroup that wraps the LWS gang. Targets KServe v0.18 today (flat
workerSpec.containers); per-version dispatch is a follow-up.

This MR scopes to **managed-kai** as the only in-cluster scheduler — the
plugin/dispatch system (Kueue, Volcano, none) lands in a separate MR. The
KAI integration is small and inline at the bottom of this file; future
schedulers will move the dispatch + capacity-adapter code out into their
own modules.

Pure over (MR, IC, Class) — the renderer doesn't read the parent MD. The
composer projected the MD into the MR's resolved spec already. This is
the IR boundary that lets BYO backends slot in via a different renderer
without touching the federation scheduler.
"""

from dataclasses import dataclass
from typing import Any

# These types are local sketches — once #64's protos are generated,
# adapters.py loads the real protobuf models into these dataclasses.


@dataclass
class RoleView:
    """One role's view from the renderer's perspective.

    Matches the shape emitters.build_replica wrote into MR.spec.{decode,
    prefill}. Carries everything the renderer needs without re-fetching MD.
    """

    topology: dict  # {strategy, tensor, pipeline, data, dataLocal, instances}
    node_selector_cel: str
    pool: str
    nodes_used: int
    gpus_per_node: int
    instances: int


@dataclass
class ClassView:
    """Resolved InferenceClass — pool's typed capabilities."""

    name: str
    capabilities: dict[str, Any]


@dataclass
class ModelReplicaView:
    """MR spec the renderer consumes."""

    parent_name: str
    parent_namespace: str
    replica_index: int
    target_cluster: str
    target_decode_pool: str
    target_prefill_pool: str | None
    decode: RoleView
    prefill: RoleView | None
    engine: dict
    source: dict


@dataclass
class ClusterView:
    """InferenceCluster the renderer consumes — kubeconfig + pool→class
    mapping. Scheduler is managed-kai for this MR."""

    name: str
    kubeconfig_secret_ref: dict  # {namespace, name, key}
    pool_to_class: dict[str, str]  # pool name → class name


# ---------------------------------------------------------------------------
# LLM-IS spec builder
# ---------------------------------------------------------------------------


def build_llmis_spec(mr: ModelReplicaView, classes: dict[str, ClassView]) -> dict:
    """Build a KServe v0.18 LLMInferenceService spec from the resolved MR.

    Topology mapping:
      Tensor          → workerSpec.containers, leaderWorkerSet=None, 1 pod
      TensorPipeline  → workerSpec.leaderWorkerSet.size = pipeline (LWS gang)
      DataExpert      → DP+EP across nodes; LWS group sized accordingly

    Disagg: top-level workerSpec is decode; spec.prefill carries its own.
    """
    decode_class = classes[mr.target_decode_pool]
    spec: dict = {
        "model": {
            "name": f"{mr.parent_namespace}/{mr.parent_name}",
            "source": mr.source,
        },
        "replicas": 1,  # one LLM-IS per ModelReplica — sticky 1
        "engine": _engine_block(mr.engine),
        "workerSpec": _worker_spec(mr.decode, decode_class),
    }
    if mr.prefill is not None and mr.target_prefill_pool is not None:
        prefill_class = classes[mr.target_prefill_pool]
        spec["prefill"] = {
            "engine": _engine_block(mr.engine),
            "workerSpec": _worker_spec(mr.prefill, prefill_class),
        }
    return spec


def _worker_spec(role: RoleView, cls: ClassView) -> dict:
    """KServe v0.18 workerSpec for one role.

    leaderWorkerSet.size is the LWS group size. >1 means multi-node;
    1 (or absent) means a single pod. KServe maps this directly onto
    LeaderWorkerSet's leader/worker structure.
    """
    nodes_per_inst = role.topology.get("pipeline", 0) or 1
    return {
        "replicas": role.instances,
        "leaderWorkerSet": {"size": nodes_per_inst} if nodes_per_inst > 1 else None,
        "containers": [
            {
                "name": "engine",
                "image": role.topology.get("image"),  # carried via engine block;
                                                       # left here for completeness
                "resources": {
                    # DRA: ResourceClaim is bound by name "gpus".
                    "claims": [{"name": "gpus"}],
                    "limits": {"nvidia.com/gpu": role.gpus_per_node},
                },
            }
        ],
    }


def _engine_block(engine: dict) -> dict:
    """Pass-through engine config. Nic's #64: engine.{name, image, args}.
    No structured quantization / speculation / optimizations — args is the
    opaque seam.
    """
    return {
        "name": engine.get("name"),
        "image": engine.get("image"),
        "args": list(engine.get("args", [])),
    }


# ---------------------------------------------------------------------------
# DRA ResourceClaim builder
# ---------------------------------------------------------------------------


def build_resource_claim_spec(role: RoleView, cls: ClassView) -> dict:
    """Build a DRA ResourceClaim spec from the role's GPUs-per-node + the
    class's typed capabilities. The DRA driver matches this against
    runtime ResourceSlices at pod admission.
    """
    return {
        "devices": {
            "requests": [
                {
                    "name": "gpus",
                    "deviceClassName": _device_class_for(cls),
                    "selectors": [{"cel": cel_from_capabilities(cls.capabilities)}],
                    "count": role.gpus_per_node,
                }
            ],
        }
    }


def _device_class_for(cls: ClassView) -> str:
    """DRA DeviceClass picked from the InferenceClass's vendor."""
    vendor = cls.capabilities.get("gpu.vendor", "nvidia")
    return {"nvidia": "gpu.nvidia.com", "amd": "gpu.amd.com"}.get(vendor, "generic-gpu")


def cel_from_capabilities(capabilities: dict[str, Any]) -> str:
    """Map declared InferenceClass capabilities → DRA selector CEL.

    Walks well-known capability keys and emits the equivalent
    device.attributes predicate. Unknown keys are skipped (logged at
    higher level). Order is stable so the resulting CEL is deterministic
    (eases golden tests).

    Schema-shape:
      capabilities["gpu.vendor"]              → device.driver == "<vendor>.com"
      capabilities["gpu.product"]             → device.attributes[...].string == ...
      capabilities["gpu.vramGiB"]             → device.attributes[...].int >= ...
      capabilities["gpu.features"] (list)     → all([f in attrs.features for f in features])
    """
    parts: list[str] = []
    if vendor := capabilities.get("gpu.vendor"):
        parts.append(f'device.driver == "{vendor}.com"')
    if product := capabilities.get("gpu.product"):
        parts.append(
            f'device.attributes["{vendor or "nvidia"}.com/product"].string == "{product}"'
        )
    if vram := capabilities.get("gpu.vramGiB"):
        parts.append(
            f'device.attributes["{vendor or "nvidia"}.com/memory.gib"].int >= {int(vram)}'
        )
    features = capabilities.get("gpu.features") or []
    for feat in features:
        parts.append(
            f'"{feat}" in device.attributes["{vendor or "nvidia"}.com/features"].listString'
        )
    return " && ".join(parts) if parts else "true"


# ---------------------------------------------------------------------------
# KAI scheduler integration (managed-kai only for this MR)
#
# KAI replaces the kube-scheduler. Two changes to the rendered LLM-IS:
#   1. Stamp `schedulerName: kai-scheduler` on every pod template the
#      LLM-IS produces. KAI's mutating webhook does this for any pod that
#      forgot, but explicit is safer.
#   2. Emit a PodGroup CRD that names the gang. KAI binds gang admission
#      to the label `pod-group.scheduling.run.ai/name` on the pod template.
#      `minMember` is the total pod count (LWS gang × instances, summed
#      across decode + prefill if disagg). 1 is harmless on single-pod.
#
# Per-scheduler dispatch (Kueue, Volcano, none), capacity adapters, and
# the matching IC.spec.scheduler.type axis land in a follow-up MR.
# ---------------------------------------------------------------------------


@dataclass
class KaiBundle:
    """KAI-wrapped LLM-IS spec + the PodGroup CRD to apply alongside it."""

    llmis_spec: dict
    pod_group: dict


def with_kai_gang(llmis_spec: dict, mr_name: str, namespace: str) -> KaiBundle:
    """Wrap an LLM-IS spec for managed-kai gang admission.

    Returns the mutated spec plus a sibling PodGroup that the renderer
    applies to the same target cluster.
    """
    gang_label = f"{mr_name}-gang"

    out = _stamp_scheduler_name(llmis_spec, "kai-scheduler")
    out = _stamp_pod_label(out, "pod-group.scheduling.run.ai/name", gang_label)

    pod_group = {
        "apiVersion": "scheduling.run.ai/v2alpha2",
        "kind": "PodGroup",
        "metadata": {
            "name": gang_label,
            "namespace": namespace,
            "labels": {"pod-group.scheduling.run.ai/name": gang_label},
        },
        "spec": {
            "minMember": gang_size(llmis_spec),
            "queue": _kai_queue_name(namespace),
            "priorityClassName": "inference",
        },
    }
    return KaiBundle(llmis_spec=out, pod_group=pod_group)


def gang_size(llmis_spec: dict) -> int:
    """Total pods in this LLM-IS's gang. Decode contributes (LWS size or 1)
    × workerSpec.replicas; prefill (if disagg) contributes the same."""
    total = _role_pod_count(llmis_spec)
    if "prefill" in llmis_spec and llmis_spec["prefill"]:
        total += _role_pod_count(llmis_spec["prefill"])
    return max(1, total)


def _stamp_scheduler_name(llmis_spec: dict, name: str) -> dict:
    """Set schedulerName on every pod template the LLM-IS produces.

    KServe v0.18 propagates the field from workerSpec onto the rendered
    pod-spec. Disagg adds the same to prefill.workerSpec.
    """
    out = dict(llmis_spec)
    if "workerSpec" in out:
        out["workerSpec"] = {**out["workerSpec"], "schedulerName": name}
    if "prefill" in out and out["prefill"] and "workerSpec" in out["prefill"]:
        out["prefill"] = {
            **out["prefill"],
            "workerSpec": {**out["prefill"]["workerSpec"], "schedulerName": name},
        }
    return out


def _stamp_pod_label(llmis_spec: dict, key: str, value: str) -> dict:
    """Add a label that flows through to every pod the LLM-IS produces.

    KServe propagates workerSpec.metadata.labels onto the rendered pod
    template; disagg adds the same to prefill.workerSpec.
    """
    out = dict(llmis_spec)
    if "workerSpec" in out:
        ws = dict(out["workerSpec"])
        ws.setdefault("metadata", {}).setdefault("labels", {})[key] = value
        out["workerSpec"] = ws
    if "prefill" in out and out["prefill"] and "workerSpec" in out["prefill"]:
        ws = dict(out["prefill"]["workerSpec"])
        ws.setdefault("metadata", {}).setdefault("labels", {})[key] = value
        out["prefill"] = {**out["prefill"], "workerSpec": ws}
    return out


def _role_pod_count(role_or_spec: dict) -> int:
    ws = role_or_spec.get("workerSpec") or {}
    instances = ws.get("replicas", 1) or 1
    lws = ws.get("leaderWorkerSet")
    nodes_per_inst = (lws or {}).get("size", 1) if lws else 1
    return nodes_per_inst * instances


def _kai_queue_name(namespace: str) -> str:
    """KAI Queues are per-Project; we map each Modelplane namespace to its
    own Project + Queue. The IC onboarding controller (separate MR)
    creates these when scheduler.type resolves to managed-kai."""
    return f"modelplane-{namespace}"
