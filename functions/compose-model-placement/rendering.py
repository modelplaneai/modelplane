"""Pure builders: ModelReplica + InferenceCluster + InferenceClass(es) → dicts.

Builds KServe LLMInferenceService spec, DRA ResourceClaim spec, and the
DRA selector CEL derived from class capabilities. Targets KServe v0.18
schema today (flat workerSpec.containers); per-version dispatch is a
follow-up.

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
    """InferenceCluster the renderer consumes — kubeconfig + scheduler choice
    + pool→class mapping."""

    name: str
    kubeconfig_secret_ref: dict  # {namespace, name, key}
    scheduler_type: str  # "managed-kai" / "kueue" / "none" / etc.
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
