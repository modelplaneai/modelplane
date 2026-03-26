"""Kubernetes Object builder for composition functions."""

from ..model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from ..model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


def k8s_object(
    provider_config: str,
    manifest: dict,
    metadata: metav1.ObjectMeta | None = None,
    management_policies: list | None = None,
) -> k8sobjv1alpha1.Object:
    """Build a provider-kubernetes Object wrapping an arbitrary manifest.

    Args:
        provider_config: Name of the ProviderConfig to use.
        manifest: The Kubernetes resource manifest to wrap.
        metadata: Optional metadata for the Object resource itself.
        management_policies: Optional management policies (e.g.
            ["Create", "Observe", "Update"] to skip deletion).
    """
    obj = k8sobjv1alpha1.Object(
        metadata=metadata,
        spec=k8sobjv1alpha1.Spec(
            providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                kind="ProviderConfig",
                name=provider_config,
            ),
            forProvider=k8sobjv1alpha1.ForProvider(
                manifest=manifest,
            ),
        ),
    )
    if management_policies:
        obj.spec.managementPolicies = management_policies
    return obj
