from .lib import resource as libresource
from .model.ai.modelplane.infrastructure.kservebackend import v1alpha1 as kssv1alpha1
from .model.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1
from .model.io.crossplane.m.kubernetes.providerconfig import v1alpha1 as k8spcv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="kservebackend-basic",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/kservebackends/composition.yaml",
        xrPath="tests/test-backend/xr.yaml",
        xrdPath="apis/kservebackends/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        assertResources=[
            # Assert the XR spec is echoed back.
            libresource.model_to_dict(
                kssv1alpha1.KServeBackend(
                    metadata=metav1.ObjectMeta(
                        name="gpu-us-central1-kserve",
                        namespace="gpu-us-central1",
                    ),
                    spec=kssv1alpha1.Spec(
                        secrets=[
                            kssv1alpha1.Secret(
                                type="Kubeconfig",
                                name="gpu-us-central1-kubeconfig",
                                key="kubeconfig",
                            ),
                            kssv1alpha1.Secret(
                                type="GCPServiceAccountKey",
                                name="gpu-us-central1-sa-key",
                                key="private_key",
                            ),
                        ],
                    ),
                )
            ),
            # Assert ProviderConfigs are composed on the first pass.
            # Helm releases are gated on ProviderConfigs being observed,
            # so they don't appear until the second reconcile.
            libresource.model_to_dict(
                k8spcv1alpha1.ProviderConfig(
                    metadata=metav1.ObjectMeta(
                        name="gpu-us-central1-kserve-cluster",
                        annotations={
                            "crossplane.io/composition-resource-name": "provider-config-kubernetes",
                        },
                    ),
                    spec=k8spcv1alpha1.Spec(
                        credentials=k8spcv1alpha1.Credentials(source="Secret"),
                    ),
                )
            ),
            libresource.model_to_dict(
                helmpcv1beta1.ProviderConfig(
                    metadata=metav1.ObjectMeta(
                        name="gpu-us-central1-kserve-cluster",
                        annotations={
                            "crossplane.io/composition-resource-name": "provider-config-helm",
                        },
                    ),
                    spec=helmpcv1beta1.Spec(
                        credentials=helmpcv1beta1.Credentials(source="Secret"),
                    ),
                )
            ),
        ],
    ),
)
