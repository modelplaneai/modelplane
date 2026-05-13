from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-endpoint-basic",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modelendpoints/composition.yaml",
        xrPath="tests/test-model-endpoint/xr.yaml",
        xrdPath="apis/modelendpoints/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        assertResources=[
            # Assert an Envoy Gateway Backend is composed on the control
            # plane pointing at the host:port parsed from spec.url.
            {
                "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                "kind": "Backend",
                "metadata": {
                    "namespace": "ml-team",
                    "annotations": {
                        "crossplane.io/composition-resource-name": "backend",
                    },
                },
                "spec": {
                    "endpoints": [
                        {
                            "ip": {"address": "34.55.100.10", "port": 80},
                        }
                    ],
                },
            },
        ],
    ),
)
