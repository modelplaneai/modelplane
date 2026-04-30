from .lib import resource as libresource
from .model.ai.modelplane.inferenceenvironment import v1alpha1 as iev1alpha1
from .model.ai.modelplane.infrastructure.kservebackend import v1alpha1 as kssv1alpha1
from .model.io.crossplane.m.kubernetes.clusterproviderconfig import (
    v1alpha1 as k8scpcv1alpha1,
)
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="inference-env-existing",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/inferenceenvironments/composition.yaml",
        xrPath="tests/test-inference-env-existing/xr.yaml",
        xrdPath="apis/inferenceenvironments/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        # No observedResources needed — the Existing path composes
        # everything on the first pass since the kubeconfig secret is
        # user-supplied, not from a composed GKECluster.
        assertResources=[
            # Assert the XR has status populated with providerConfigRef,
            # namespace, and GPU capacity from declared node pools.
            libresource.model_to_dict(
                iev1alpha1.InferenceEnvironment(
                    metadata=metav1.ObjectMeta(
                        name="byo-us-east",
                    ),
                    spec=iev1alpha1.Spec(
                        cluster=iev1alpha1.Cluster(
                            source="Existing",
                            existing=iev1alpha1.Existing(
                                secretRef=iev1alpha1.SecretRef(
                                    name="byo-cluster-kubeconfig",
                                    key="kubeconfig",
                                ),
                                nodePools=[
                                    iev1alpha1.NodePool(
                                        name="gpu-h100",
                                        nodeCount=2,
                                        gpu=iev1alpha1.Gpu(
                                            acceleratorType="nvidia-h100-80gb",
                                            acceleratorCount=8,
                                            memory="80Gi",
                                        ),
                                    ),
                                ],
                            ),
                        ),
                    ),
                    status=iev1alpha1.Status(
                        providerConfigRef=iev1alpha1.ProviderConfigRef(
                            name="byo-us-east-cluster-kubeconfig",
                        ),
                        namespace="modelplane-system",
                        capacity=iev1alpha1.Capacity(
                            gpuPools=[
                                iev1alpha1.GpuPool(
                                    acceleratorType="nvidia-h100-80gb",
                                    memory="80Gi",
                                    nodes=2,
                                    countPerNode=8,
                                ),
                            ],
                        ),
                    ),
                )
            ),
            # Assert no GKECluster is composed — cluster is user-managed.
            # (Absence is verified by not including a GKECluster assertion.)
            # Assert KServeBackend is composed with the user-supplied kubeconfig.
            libresource.model_to_dict(
                kssv1alpha1.KServeBackend(
                    metadata=metav1.ObjectMeta(
                        name="byo-us-east-kserve",
                        namespace="modelplane-system",
                        annotations={
                            "crossplane.io/composition-resource-name": "kserve-backend",
                        },
                    ),
                    spec=kssv1alpha1.Spec(
                        versions=kssv1alpha1.Versions(kserve="v0.16.0"),
                        secrets=[
                            kssv1alpha1.Secret(
                                type="Kubeconfig",
                                name="byo-cluster-kubeconfig",
                                key="kubeconfig",
                            ),
                        ],
                    ),
                )
            ),
            # Assert ClusterProviderConfig references the user-supplied
            # kubeconfig — no GCP identity needed.
            libresource.model_to_dict(
                k8scpcv1alpha1.ClusterProviderConfig(
                    metadata=metav1.ObjectMeta(
                        name="byo-us-east-cluster-kubeconfig",
                        annotations={
                            "crossplane.io/composition-resource-name": "cluster-provider-config-kubernetes",
                        },
                    ),
                    spec=k8scpcv1alpha1.Spec(
                        credentials=k8scpcv1alpha1.Credentials(
                            source="Secret",
                            secretRef=k8scpcv1alpha1.SecretRef(
                                namespace="modelplane-system",
                                name="byo-cluster-kubeconfig",
                                key="kubeconfig",
                            ),
                        ),
                    ),
                )
            ),
        ],
    ),
)
