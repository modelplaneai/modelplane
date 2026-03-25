from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as k8s

test = compositiontest.CompositionTest(
    metadata=k8s.ObjectMeta(
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
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "InferenceGateway",
                "metadata": {"name": "default"},
            },
            # Assert the ClusterProviderConfig is composed.
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "ClusterProviderConfig",
                "metadata": {
                    "name": "modelplane-in-cluster",
                    "annotations": {
                        "crossplane.io/composition-resource-name": "provider-config-helm",
                    },
                },
            },
        ],
    ),
)
