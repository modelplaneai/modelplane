from .lib import resource as libresource
from .model.ai.modelplane.inferencegateway import v1alpha1 as igwv1alpha1
from .model.ai.modelplane.modelendpoint import v1alpha1 as mev1alpha1
from .model.ai.modelplane.modelservice import v1alpha1 as msv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-service-basic",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modelservices/composition.yaml",
        xrPath="tests/test-model-service/xr.yaml",
        xrdPath="apis/modelservices/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        extraResources=[
            # The InferenceGateway for the public address and HTTPRoute
            # parentRef.
            libresource.model_to_fixture(
                igwv1alpha1.InferenceGateway(
                    metadata=metav1.ObjectMeta(name="default"),
                    spec=igwv1alpha1.Spec(backend="EnvoyGateway"),
                    status=igwv1alpha1.Status(address="10.0.0.1"),
                )
            ),
            # One ModelEndpoint matching the selector, with its Backend
            # already composed (status.routing.backendName populated).
            libresource.model_to_fixture(
                mev1alpha1.ModelEndpoint(
                    metadata=metav1.ObjectMeta(
                        name="qwen-demo-demo-us-central",
                        namespace="ml-team",
                        labels={"modelplane.ai/deployment": "qwen-demo"},
                    ),
                    spec=mev1alpha1.Spec(
                        url="http://34.55.100.10/default/qwen-demo/v1",
                        api="OpenAI",
                        rewritePath="/default/qwen-demo/",
                    ),
                    status=mev1alpha1.Status(
                        routing=mev1alpha1.Routing(
                            backendName="qwen-demo-demo-us-central-backend-x7k2",
                        ),
                    ),
                )
            ),
        ],
        assertResources=[
            # Assert the XR exposes the public address.
            libresource.model_to_dict(
                msv1alpha1.ModelService(
                    metadata=metav1.ObjectMeta(
                        name="qwen",
                        namespace="ml-team",
                    ),
                    spec=msv1alpha1.Spec(
                        endpoints=[
                            msv1alpha1.Endpoint(
                                selector=msv1alpha1.Selector(
                                    matchLabels={"modelplane.ai/deployment": "qwen-demo"},
                                ),
                            ),
                        ],
                    ),
                    status=msv1alpha1.Status(
                        address="http://10.0.0.1/ml-team/qwen",
                    ),
                )
            ),
            # Assert the HTTPRoute is composed with the matched endpoint's
            # backend as a backendRef and the endpoint's rewritePath as
            # the URLRewrite target.
            {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "HTTPRoute",
                "metadata": {
                    "namespace": "ml-team",
                    "annotations": {
                        "crossplane.io/composition-resource-name": "httproute",
                    },
                },
                "spec": {
                    "parentRefs": [
                        {
                            "name": "modelplane",
                            "namespace": "modelplane-system",
                        }
                    ],
                    "rules": [
                        {
                            "matches": [
                                {
                                    "path": {
                                        "type": "PathPrefix",
                                        "value": "/ml-team/qwen/",
                                    },
                                }
                            ],
                            "filters": [
                                {
                                    "type": "URLRewrite",
                                    "urlRewrite": {
                                        "path": {
                                            "type": "ReplacePrefixMatch",
                                            "replacePrefixMatch": "/default/qwen-demo/",
                                        },
                                    },
                                }
                            ],
                            "backendRefs": [
                                {
                                    "group": "gateway.envoyproxy.io",
                                    "kind": "Backend",
                                    "name": "qwen-demo-demo-us-central-backend-x7k2",
                                    "port": 80,
                                    "weight": 1,
                                }
                            ],
                        }
                    ],
                },
            },
        ],
    ),
)
