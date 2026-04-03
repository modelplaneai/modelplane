"""Serving profile matching.

Shared between the deploy function (scheduling) and the placement function
(profile resolution). Both need to walk a model's serving[] array and find
the first profile matching an environment's backend and labels.
"""

from ..model.ai.modelplane.clustermodel import v1alpha1 as cmv1alpha1
from ..model.ai.modelplane.inferenceenvironment import v1alpha1 as iev1alpha1


def match_profile(
    model: cmv1alpha1.ClusterModel,
    env: iev1alpha1.InferenceEnvironment,
) -> cmv1alpha1.ServingItem | None:
    """Find the first serving profile that matches an environment.

    A profile matches if its backend equals the environment's backend and
    its environmentSelector (if set) matches the environment's labels.
    """
    env_backend = env.status.capacity.backend or ""
    env_labels = env.metadata.labels or {}

    for profile in model.spec.serving or []:
        if profile.backend != env_backend:
            continue

        if profile.environmentSelector and profile.environmentSelector.matchLabels:
            required_labels = profile.environmentSelector.matchLabels
            if not all(env_labels.get(k) == v for k, v in required_labels.items()):
                continue

        return profile

    return None
