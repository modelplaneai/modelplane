from .lib import resource as libresource
from .model.ai.modelplane.inferencegateway import v1alpha1 as igwv1alpha1
from .model.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="inference-gateway-basic",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/inferencegateways/composition.yaml",
        xrPath="tests/test-inference-gateway/xr.yaml",
        xrdPath="apis/inferencegateways/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        assertResources=[
            # Assert the XR exists. No status.address on the first pass
            # (the Gateway hasn't been assigned an IP yet).
            libresource.model_to_dict(igwv1alpha1.InferenceGateway(
                metadata=metav1.ObjectMeta(name="default"),
                spec=igwv1alpha1.Spec(backend="EnvoyGateway"),
            )),
            # Assert the Helm ProviderConfig is composed in
            # modelplane-system.
            libresource.model_to_dict(helmpcv1beta1.ProviderConfig(
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
    ),
)
