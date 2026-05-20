from .lib import resource as libresource
from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from .model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-replica-basic",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modelreplicas/composition.yaml",
        xrPath="tests/test-model-replica/xr.yaml",
        xrdPath="apis/modelreplicas/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        # extraResources is the up CLI's name for required resources.
        # These are resources the function reads but doesn't own, resolved
        # by Crossplane at runtime via response.require_resources().
        extraResources=[
            # The InferenceCluster referenced by spec.inferenceClusterRef.
            # Status fields are populated as if the cluster is fully ready.
            libresource.model_to_fixture(
                icv1alpha1.InferenceCluster(
                    metadata=metav1.ObjectMeta(
                        name="demo-us-central",
                        labels={"modelplane.ai/cluster": "true"},
                    ),
                    spec=icv1alpha1.Spec(cluster=icv1alpha1.Cluster(source="Existing")),
                    status=icv1alpha1.Status(
                        providerConfigRef=icv1alpha1.ProviderConfigRef(
                            name="demo-us-central-cluster",
                        ),
                        gateway=icv1alpha1.Gateway(address="34.55.100.10"),
                        capacity=icv1alpha1.Capacity(
                            gpuPools=[
                                icv1alpha1.GpuPool(
                                    acceleratorType="nvidia-l4",
                                    countPerNode=1,
                                    nodes=2,
                                    memory="24Gi",
                                )
                            ],
                        ),
                    ),
                )
            ),
        ],
        assertResources=[
            # Assert the LLMInferenceService Object is composed on the remote
            # cluster with the correct vLLM container spec and GPU count.
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
                            name="demo-us-central-cluster",
                        ),
                        readiness=k8sobjv1alpha1.Readiness(
                            policy="DeriveFromObject",
                        ),
                        forProvider=k8sobjv1alpha1.ForProvider(
                            manifest={
                                "apiVersion": "serving.kserve.io/v1alpha1",
                                "kind": "LLMInferenceService",
                                "metadata": {
                                    "name": "qwen-demo-86093",
                                    "namespace": "default",
                                },
                                "spec": {
                                    "model": {"uri": "hf://Qwen/Qwen2.5-0.5B-Instruct"},
                                    "replicas": 1,
                                    "template": {
                                        "containers": [
                                            {
                                                "name": "main",
                                                "image": "vllm/vllm-openai:v0.7.3",
                                                "args": [],
                                                "securityContext": {
                                                    "runAsUser": 0,
                                                    "runAsNonRoot": False,
                                                },
                                                "resources": {
                                                    "limits": {
                                                        "nvidia.com/gpu": "1",
                                                    },
                                                },
                                            }
                                        ],
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
