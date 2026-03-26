"""Helm Release builder for composition functions."""

from ..model.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from ..model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


def helm_release(
    chart: str,
    repo: str,
    version: str,
    namespace: str,
    provider_config: str,
    values: dict | None = None,
    labels: dict | None = None,
    metadata_namespace: str | None = None,
) -> helmv1beta1.Release:
    """Build a Helm Release targeting a remote (or local) cluster.

    Args:
        chart: The Helm chart name.
        repo: The chart repository URL.
        version: The chart version.
        namespace: The namespace to install the chart into on the target cluster.
        provider_config: Name of the ProviderConfig to use.
        values: Optional Helm values dict.
        labels: Optional labels for the Release metadata.
        metadata_namespace: Optional namespace for the Release resource itself.
            Set this explicitly when composing from a cluster-scoped XR, since
            cluster-scoped XRs don't auto-populate namespace on composed
            namespaced resources.
    """
    metadata = None
    if labels or metadata_namespace:
        metadata = metav1.ObjectMeta(
            namespace=metadata_namespace, labels=labels
        )

    release = helmv1beta1.Release(
        metadata=metadata,
        spec=helmv1beta1.Spec(
            providerConfigRef=helmv1beta1.ProviderConfigRef(
                kind="ProviderConfig",
                name=provider_config,
            ),
            forProvider=helmv1beta1.ForProvider(
                chart=helmv1beta1.Chart(
                    name=chart,
                    repository=repo,
                    version=version,
                ),
                namespace=namespace,
            ),
        ),
    )
    if values:
        release.spec.forProvider.values = values
    return release
