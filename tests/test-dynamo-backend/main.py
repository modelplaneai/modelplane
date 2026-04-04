from .lib import resource as libresource
from .model.ai.modelplane.infrastructure.dynamobackend import v1alpha1 as dsv1alpha1
from .model.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1
from .model.io.crossplane.m.kubernetes.providerconfig import v1alpha1 as k8spcv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="dynamobackend-basic",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/dynamobackends/composition.yaml",
        xrPath="tests/test-dynamo-backend/xr.yaml",
        xrdPath="apis/dynamobackends/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        assertResources=[
            # Assert the XR spec is echoed back.
            libresource.model_to_dict(
                dsv1alpha1.DynamoBackend(
                    metadata=metav1.ObjectMeta(
                        name="dynamo-us-central-dynamo",
                        namespace="modelplane-system",
                    ),
                    spec=dsv1alpha1.Spec(
                        secrets=[
                            dsv1alpha1.Secret(
                                type="Kubeconfig",
                                name="dynamo-cluster-kubeconfig",
                                key="kubeconfig",
                            ),
                        ],
                    ),
                )
            ),
            # Assert ProviderConfigs are composed on the first pass.
            libresource.model_to_dict(
                k8spcv1alpha1.ProviderConfig(
                    metadata=metav1.ObjectMeta(
                        name="dynamo-us-central-dynamo-cluster",
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
                        name="dynamo-us-central-dynamo-cluster",
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
