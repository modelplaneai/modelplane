"""Test multi-node KServe replica.

A replica with tensor=8 pipeline=2 should compose an LLMInferenceService with:

- 8 GPUs per pod (tensor)
- parallelism.tensor = 16 (tensor * pipeline)
- worker.size = 1 (pipeline - 1)
"""

from .lib import resource as libresource
from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from .model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

# The container shape is the same for the leader and each worker.
CONTAINER = {
    "name": "main",
    "image": "vllm/vllm-openai:v0.7.3",
    "args": [],
    "securityContext": {
        "runAsUser": 0,
        "runAsNonRoot": False,
    },
    "resources": {
        "limits": {
            "nvidia.com/gpu": "8",
        },
    },
}

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-replica-kserve-multinode",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modelreplicas/composition.yaml",
        xrPath="tests/test-model-replica-multinode/xr.yaml",
        xrdPath="apis/modelreplicas/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        extraResources=[
            # 3-node H100 cluster: 8 GPUs per node = 24 total.
            libresource.model_to_fixture(
                icv1alpha1.InferenceCluster(
                    metadata=metav1.ObjectMeta(
                        name="h100-cluster",
                        labels={"modelplane.ai/cluster": "true"},
                    ),
                    spec=icv1alpha1.Spec(cluster=icv1alpha1.Cluster(source="Existing")),
                    status=icv1alpha1.Status(
                        providerConfigRef=icv1alpha1.ProviderConfigRef(
                            name="h100-cluster-kubeconfig",
                        ),
                        gateway=icv1alpha1.Gateway(address="10.0.0.1"),
                        capacity=icv1alpha1.Capacity(
                            gpuPools=[
                                icv1alpha1.GpuPool(
                                    acceleratorType="nvidia-h100-80gb",
                                    nodes=3,
                                    countPerNode=8,
                                    memory="80Gi",
                                )
                            ],
                        ),
                    ),
                )
            ),
        ],
        assertResources=[
            # Assert LLMInferenceService has multi-node configuration:
            # - 8 GPUs per pod (tensor)
            # - parallelism.tensor = 16 (tensor * pipeline)
            # - worker.size = 1 (pipeline - 1)
            libresource.model_to_dict(
                k8sobjv1alpha1.Object(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "model-serving",
                        },
                    ),
                    spec=k8sobjv1alpha1.Spec(
                        providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                            kind="ClusterProviderConfig",
                            name="h100-cluster-kubeconfig",
                        ),
                        readiness=k8sobjv1alpha1.Readiness(
                            policy="DeriveFromObject",
                        ),
                        forProvider=k8sobjv1alpha1.ForProvider(
                            manifest={
                                "apiVersion": "serving.kserve.io/v1alpha1",
                                "kind": "LLMInferenceService",
                                "metadata": {
                                    "name": "llama405b-ef974",
                                    "namespace": "default",
                                },
                                "spec": {
                                    "model": {"uri": "hf://meta-llama/Llama-3.1-405B"},
                                    "replicas": 1,
                                    "parallelism": {"tensor": 16},
                                    "template": {
                                        "containers": [CONTAINER],
                                    },
                                    "worker": {
                                        "size": 1,
                                        "template": {
                                            "containers": [CONTAINER],
                                        },
                                    },
                                    "router": {"gateway": {}, "route": {}},
                                },
                            },
                        ),
                    ),
                )
            ),
        ],
    ),
)
