from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as k8s
from .model.ai.modelplane.infrastructure.kservestack import v1alpha1 as kservestackv1alpha1

test = compositiontest.CompositionTest(
    metadata=k8s.ObjectMeta(
        name="kservestack-basic",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/kservestacks/composition.yaml",
        xrPath="tests/test-kservestack/xr.yaml",
        xrdPath="apis/kservestacks/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        assertResources=[
            # Assert on the XR itself.
            kservestackv1alpha1.KServeStack(
                apiVersion="infrastructure.modelplane.ai/v1alpha1",
                kind="KServeStack",
                metadata=k8s.ObjectMeta(
                    name="gpu-us-central1-kserve",
                    namespace="gpu-us-central1",
                ),
                spec=kservestackv1alpha1.Spec(
                    secrets=[
                        kservestackv1alpha1.Secret(
                            type="Kubeconfig",
                            name="gpu-us-central1-kubeconfig",
                            key="kubeconfig",
                        ),
                        kservestackv1alpha1.Secret(
                            type="GCPServiceAccountKey",
                            name="gpu-us-central1-sa-key",
                            key="private_key",
                        ),
                    ],
                ),
            ).model_dump(exclude_unset=True),
            # Assert ProviderConfigs are composed on the first pass.
            # Helm releases are gated on ProviderConfigs being observed,
            # so they don't appear until the second reconcile.
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "ProviderConfig",
                "metadata": {
                    "name": "gpu-us-central1-kserve-cluster",
                    "annotations": {
                        "crossplane.io/composition-resource-name":
                            "provider-config-kubernetes",
                    },
                },
            },
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "ProviderConfig",
                "metadata": {
                    "name": "gpu-us-central1-kserve-cluster",
                    "annotations": {
                        "crossplane.io/composition-resource-name":
                            "provider-config-helm",
                    },
                },
            },
        ],
    ),
)
