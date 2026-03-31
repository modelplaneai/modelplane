"""Schedule model placements across inference environments.

Filters environments by engine compatibility and GPU capacity, accounts for
GPU usage by other deployments, and returns a stable list of candidates that
prefers environments with existing placements.
"""

import math
from dataclasses import dataclass

from .lib import metadata, quantities
from .model.ai.modelplane.clustermodel import v1alpha1 as cmv1alpha1
from .model.ai.modelplane.inferenceenvironment import v1alpha1 as iev1alpha1
from .model.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
from .model.ai.modelplane.modelplacement import v1alpha1 as mpv1alpha1

# Maps InferenceEnvironment backends to the engines they support.
_COMPAT = {
    "KServe": ["vLLM"],
    "Dynamo": ["vLLM", "SGLang", "TensorRT-LLM"],
}


@dataclass
class Candidate:
    """An environment that matched scheduling criteria."""

    name: str
    gateway_address: str | None


def schedule(
    deployment: mdv1alpha1.ModelDeployment,
    model: cmv1alpha1.ClusterModel,
    envs: list[iev1alpha1.InferenceEnvironment],
    all_placements: list[mpv1alpha1.ModelPlacement],
) -> list[Candidate]:
    """Select environments for model placement.

    All inputs should be passed through their respective defaults.*
    functions before calling this — the function assumes Optional fields
    are populated with zero values.

    Filters environments by engine compatibility and VRAM capacity, subtracts
    GPUs used by other deployments' placements, sorts to prefer environments
    that already have placements for this deployment (stability), and returns
    at most deployment.spec.environments candidates.
    """
    model_vram_bytes = quantities.parse_quantity(model.spec.resources.vram)

    # Environments that already have a placement for this deployment.
    existing_envs = set()
    for p in all_placements:
        if (p.metadata.labels or {}).get(metadata.LABEL_KEY_DEPLOYMENT) == deployment.metadata.name:
            existing_envs.add(p.spec.inferenceEnvironmentRef.name)

    candidates = []
    for env in envs:
        if model.spec.engine not in _COMPAT.get(env.status.capacity.backend or "", []):
            continue

        # Find the pool that needs the fewest GPUs for this model.
        best_gpus_needed = None
        eligible_total = 0
        for pool in env.status.capacity.gpuPools:
            pool_mem = quantities.parse_quantity(pool.memory or "0Gi")
            if pool_mem <= 0:
                continue
            gpus_needed = max(1, math.ceil(model_vram_bytes / pool_mem))
            eligible_total += pool.count or 0
            if best_gpus_needed is None or gpus_needed < best_gpus_needed:
                best_gpus_needed = gpus_needed

        if best_gpus_needed is None:
            continue

        # Subtract GPUs used by other deployments' placements on this env.
        used_gpus = 0
        for p in all_placements:
            if (p.metadata.labels or {}).get(metadata.LABEL_KEY_DEPLOYMENT) == deployment.metadata.name:
                continue  # Don't count our own placements against us.
            if p.spec.inferenceEnvironmentRef.name == env.metadata.name:
                used_gpus += p.status.resources.gpu.count or 0

        if eligible_total - used_gpus < best_gpus_needed:
            continue

        candidates.append(
            Candidate(
                name=env.metadata.name,
                gateway_address=env.status.gateway.address,
            )
        )

    # Prefer environments that already have placements for this deployment.
    # This prevents rescheduling when a new environment comes online.
    # Within each group (existing vs new), sort by name for determinism.
    candidates.sort(
        key=lambda c: (
            0 if c.name in existing_envs else 1,
            c.name,
        )
    )
    return candidates[: int(deployment.spec.environments)]
