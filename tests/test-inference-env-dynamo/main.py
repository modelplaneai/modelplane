from .lib import resource as libresource
from .model.ai.modelplane.inferenceenvironment import v1alpha1 as iev1alpha1
from .model.ai.modelplane.infrastructure.dynamobackend import v1alpha1 as dsv1alpha1
from .model.io.crossplane.m.kubernetes.clusterproviderconfig import (
    v1alpha1 as k8scpcv1alpha1,
)
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="inference-env-dynamo",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/inferenceenvironments/composition.yaml",
        xrPath="tests/test-inference-env-dynamo/xr.yaml",
        xrdPath="apis/inferenceenvironments/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        assertResources=[
            # Assert the XR has status populated with providerConfigRef,
            # namespace, and GPU capacity from declared node pools.
            libresource.model_to_dict(
                iev1alpha1.InferenceEnvironment(
                    metadata=metav1.ObjectMeta(
                        name="dynamo-us-central",
                    ),
                    spec=iev1alpha1.Spec(
                        backend="Dynamo",
                    ),
                    status=iev1alpha1.Status(
                        providerConfigRef=iev1alpha1.ProviderConfigRef(
                            name="dynamo-us-central-cluster-kubeconfig",
                        ),
                        namespace="modelplane-system",
                        capacity=iev1alpha1.Capacity(
                            backend="Dynamo",
                            gpuPools=[
                                iev1alpha1.GpuPool(
                                    acceleratorType="nvidia-h100-80gb",
                                    memory="80Gi",
                                    count=16,
                                ),
                            ],
                        ),
                    ),
                )
            ),
            # Assert DynamoBackend is composed with the user-supplied kubeconfig.
            libresource.model_to_dict(
                dsv1alpha1.DynamoBackend(
                    metadata=metav1.ObjectMeta(
                        name="dynamo-us-central-dynamo",
                        namespace="modelplane-system",
                        annotations={
                            "crossplane.io/composition-resource-name": "dynamo-backend",
                        },
                    ),
                    spec=dsv1alpha1.Spec(
                        versions=dsv1alpha1.Versions(dynamo="1.0.0"),
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
            # Assert ClusterProviderConfig references the user-supplied kubeconfig.
            libresource.model_to_dict(
                k8scpcv1alpha1.ClusterProviderConfig(
                    metadata=metav1.ObjectMeta(
                        name="dynamo-us-central-cluster-kubeconfig",
                        annotations={
                            "crossplane.io/composition-resource-name": "cluster-provider-config-kubernetes",
                        },
                    ),
                    spec=k8scpcv1alpha1.Spec(
                        credentials=k8scpcv1alpha1.Credentials(
                            source="Secret",
                            secretRef=k8scpcv1alpha1.SecretRef(
                                namespace="modelplane-system",
                                name="dynamo-cluster-kubeconfig",
                                key="kubeconfig",
                            ),
                        ),
                    ),
                )
            ),
        ],
    ),
)
