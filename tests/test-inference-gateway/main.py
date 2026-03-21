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
            # Assert the XR has gateway status fields.
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "InferenceGateway",
                "metadata": {"name": "default"},
                "status": {
                    "gateway": {
                        "name": "modelplane",
                        "namespace": "modelplane-system",
                    },
                },
            },
            # Assert the namespace is composed.
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": "modelplane-system",
                    "annotations": {
                        "crossplane.io/composition-resource-name": "namespace",
                    },
                },
            },
        ],
    ),
)
