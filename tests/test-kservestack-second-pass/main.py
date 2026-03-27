from .lib import resource as libresource
from .model.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1
from .model.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from .model.io.crossplane.m.kubernetes.providerconfig import v1alpha1 as k8spcv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="kservestack-second-pass",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/kservestacks/composition.yaml",
        xrPath="tests/test-kservestack-second-pass/xr.yaml",
        xrdPath="apis/kservestacks/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        # Simulate a second reconcile where both ProviderConfigs are
        # observed. This unblocks the gated Helm releases.
        observedResources=[
            libresource.model_to_fixture(
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
            libresource.model_to_fixture(
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
        assertResources=[
            # Assert cert-manager Release is composed.
            libresource.model_to_dict(
                helmv1beta1.Release(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "cert-manager",
                        },
                    ),
                    spec=helmv1beta1.Spec(
                        forProvider=helmv1beta1.ForProvider(
                            chart=helmv1beta1.Chart(
                                name="cert-manager",
                                repository="https://charts.jetstack.io",
                                version="v1.17.1",
                            ),
                            namespace="cert-manager",
                        ),
                        providerConfigRef=helmv1beta1.ProviderConfigRef(
                            kind="ProviderConfig",
                            name="gpu-us-central1-kserve-cluster",
                        ),
                    ),
                )
            ),
            # Assert Envoy Gateway Release is composed.
            libresource.model_to_dict(
                helmv1beta1.Release(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "envoy-gateway",
                        },
                    ),
                    spec=helmv1beta1.Spec(
                        forProvider=helmv1beta1.ForProvider(
                            chart=helmv1beta1.Chart(
                                name="gateway-helm",
                                repository="oci://docker.io/envoyproxy",
                                version="v1.3.0",
                            ),
                            namespace="envoy-gateway-system",
                        ),
                        providerConfigRef=helmv1beta1.ProviderConfigRef(
                            kind="ProviderConfig",
                            name="gpu-us-central1-kserve-cluster",
                        ),
                    ),
                )
            ),
            # Assert LeaderWorkerSet Release is composed.
            libresource.model_to_dict(
                helmv1beta1.Release(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "leader-worker-set",
                        },
                    ),
                    spec=helmv1beta1.Spec(
                        forProvider=helmv1beta1.ForProvider(
                            chart=helmv1beta1.Chart(
                                name="lws",
                                repository="oci://registry.k8s.io/lws/charts",
                                version="v0.7.0",
                            ),
                            namespace="lws-system",
                        ),
                        providerConfigRef=helmv1beta1.ProviderConfigRef(
                            kind="ProviderConfig",
                            name="gpu-us-central1-kserve-cluster",
                        ),
                    ),
                )
            ),
        ],
    ),
)
