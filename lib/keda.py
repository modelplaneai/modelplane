"""KEDA configuration for backend clusters.

Both KServe and Dynamo backends install KEDA for autoscaling. This module
provides shared configuration and a helper to compose the Helm release so
that every backend uses the same chart and namespace.
"""

from ..model.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from . import helm

NAMESPACE = "keda"
CHART = "keda"
REPO = "https://kedacore.github.io/charts"


def helm_release(version: str, provider_config: str) -> helmv1beta1.Release:
    """Build a KEDA Helm release for a backend cluster."""
    return helm.helm_release(
        chart=CHART,
        repo=REPO,
        version=version,
        namespace=NAMESPACE,
        provider_config=provider_config,
    )
