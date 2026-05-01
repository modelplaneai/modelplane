"""Schedule model placements across inference environments.

Matches serving profiles against environments by optional label selector,
filters by GPU capacity, accounts for GPU usage by other deployments, and
returns a stable list of candidates that prefers environments with existing
placements.
"""

import math
from dataclasses import dataclass

from .lib import metadata, quantities, serving
from .model.ai.modelplane.clustermodel import v1alpha1 as cmv1alpha1
from .model.ai.modelplane.inferenceenvironment import v1alpha1 as iev1alpha1
from .model.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
from .model.ai.modelplane.modelplacement import v1alpha1 as mpv1alpha1

# Supported scaling signals. All environments use the same backend, so
# scaling capabilities are uniform.
SUPPORTED_SCALING_SIGNALS = {"Fixed", "Concurrency"}


@dataclass
class Candidate:
    """An environment that matched scheduling criteria."""

    name: str
    gateway_address: str | None
    profile_name: str


def _pool_has_enough_nodes(pool, gpus_needed: int) -> bool:
    """Check whether a pool has enough nodes for multi-node inference.

    Returns True if the model fits on a single node or if there are enough
    nodes for multi-node.
    """
    count_per_node = int(pool.countPerNode or 0)
    if count_per_node <= 0 or gpus_needed <= count_per_node:
        return True  # Single-node — fits on one node.
    nodes_needed = math.ceil(gpus_needed / count_per_node)
    return int(pool.nodes or 0) >= nodes_needed


def _best_pool_fit(env: iev1alpha1.InferenceEnvironment, model_vram_bytes: int) -> tuple[int, int] | None:
    """Find the pool requiring the fewest GPUs and the total eligible GPU
    count across all pools that can fit the model. Returns None if no pool
    can fit the model on this environment.
    """
    best_gpus_needed = None
    eligible_total = 0
    for pool in env.status.capacity.gpuPools:
        pool_mem = quantities.parse_quantity(pool.memory or "0Gi")
        if pool_mem <= 0:
            continue
        gpus_needed = max(1, math.ceil(model_vram_bytes / pool_mem))
        if not _pool_has_enough_nodes(pool, gpus_needed):
            continue
        eligible_total += int(pool.countPerNode or 0) * int(pool.nodes or 0)
        if best_gpus_needed is None or gpus_needed < best_gpus_needed:
            best_gpus_needed = gpus_needed
    if best_gpus_needed is None:
        return None
    return best_gpus_needed, eligible_total


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

    For each candidate environment, walks the model's serving[] array to
    find the first profile whose environmentSelector (if any) matches the
    environment's labels. Filters by VRAM capacity, subtracts GPUs used by
    other deployments' placements, sorts to prefer environments that already
    have placements for this deployment (stability), and returns at most
    deployment.spec.environments candidates.
    """
    model_vram_bytes = quantities.parse_quantity(model.spec.resources.vram)

    # Environments that already have a placement for this deployment.
    existing_envs = {
        p.spec.inferenceEnvironmentRef.name
        for p in all_placements
        if (p.metadata.labels or {}).get(metadata.LABEL_KEY_DEPLOYMENT) == deployment.metadata.name
    }

    candidates = []
    for env in envs:
        # Skip environments that aren't Ready. An IE that's still provisioning
        # its cluster or installing its inference stack can't serve traffic,
        # even if its declared capacity (computed from the node pool spec)
        # would otherwise match. Scheduling on a not-yet-Ready IE creates a
        # placement that fails until the IE's ProviderConfig comes online.
        if not any(c.type == "Ready" and c.status == "True" for c in env.status.conditions or []):
            continue

        # Find the first serving profile that matches this environment.
        profile = serving.match_profile(model, env)
        if not profile:
            continue

        # Check scaling signal capability.
        if deployment.spec.scaling.signal not in SUPPORTED_SCALING_SIGNALS:
            continue

        fit = _best_pool_fit(env, model_vram_bytes)
        if fit is None:
            continue
        best_gpus_needed, eligible_total = fit

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
                profile_name=profile.name,
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
