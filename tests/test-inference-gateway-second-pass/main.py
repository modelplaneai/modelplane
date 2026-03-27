from .lib import resource as libresource
from .model.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1
from .model.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="inference-gateway-second-pass",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/inferencegateways/composition.yaml",
        xrPath="tests/test-inference-gateway-second-pass/xr.yaml",
        xrdPath="apis/inferencegateways/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        # Simulate a second reconcile where the Helm ProviderConfig is
        # observed. This unblocks the gated Helm releases.
        observedResources=[
            libresource.model_to_fixture(helmpcv1beta1.ProviderConfig(
                metadata=metav1.ObjectMeta(
                    name="modelplane-in-cluster",
                    namespace="modelplane-system",
                    annotations={
                        "crossplane.io/composition-resource-name":
                            "provider-config-helm",
                    },
                ),
                spec=helmpcv1beta1.Spec(
                    credentials=helmpcv1beta1.Credentials(
                        source="InjectedIdentity",
                    ),
                ),
            )),
        ],
        assertResources=[
            # Assert MetalLB Release is composed.
            libresource.model_to_dict(helmv1beta1.Release(
                metadata=metav1.ObjectMeta(
                    namespace="modelplane-system",
                    annotations={
                        "crossplane.io/composition-resource-name":
                            "metallb",
                    },
                    labels={
                        "modelplane.ai/release": "metallb",
                    },
                ),
                spec=helmv1beta1.Spec(
                    forProvider=helmv1beta1.ForProvider(
                        chart=helmv1beta1.Chart(
                            name="metallb",
                            repository="https://metallb.github.io/metallb",
                            version="0.14.9",
                        ),
                        namespace="metallb-system",
                    ),
                    providerConfigRef=helmv1beta1.ProviderConfigRef(
                        kind="ProviderConfig",
                        name="modelplane-in-cluster",
                    ),
                ),
            )),
            # Assert Envoy Gateway Release is composed.
            libresource.model_to_dict(helmv1beta1.Release(
                metadata=metav1.ObjectMeta(
                    namespace="modelplane-system",
                    annotations={
                        "crossplane.io/composition-resource-name":
                            "envoy-gateway",
                    },
                    labels={
                        "modelplane.ai/release": "envoy-gateway",
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
                        name="modelplane-in-cluster",
                    ),
                ),
            )),
        ],
    ),
)
