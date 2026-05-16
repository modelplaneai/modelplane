from .lib import resource as libresource
from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from .model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

# Same single-node Qwen replica as test-model-replica, but with a
# cache reference in spec.caches. The composition function should swap
# the model.uri from hf://Qwen/... to pvc://modelcache-<cache-name>
# so the serving stack mounts the pre-staged PVC instead of fetching
# at boot.
test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-replica-with-cache",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modelreplicas/composition.yaml",
        xrPath="tests/test-model-replica-with-cache/xr.yaml",
        xrdPath="apis/modelreplicas/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        extraResources=[
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
                        readiness=k8sobjv1alpha1.Readiness(policy="DeriveFromObject"),
                        forProvider=k8sobjv1alpha1.ForProvider(
                            manifest={
                                "apiVersion": "serving.kserve.io/v1alpha1",
                                "kind": "LLMInferenceService",
                                "metadata": {
                                    "name": "qwen-cached",
                                    "namespace": "default",
                                },
                                "spec": {
                                    # ← the cache wiring: model.uri points at the
                                    # cache PVC instead of fetching from HF.
                                    "model": {"uri": "pvc://modelcache-qwen-2-5-0-5b"},
                                    "replicas": 1,
                                    "template": {
                                        "containers": [
                                            {
                                                "name": "main",
                                                "image": "vllm/vllm-openai:v0.7.3",
                                                # When spec.caches is set, the function appends
                                                # --model=/mnt/models to the engine args so vLLM
                                                # loads the cached weights instead of falling back
                                                # to its hardcoded default model.
                                                "args": ["--model=/mnt/models"],
                                                "securityContext": {
                                                    "runAsUser": 0,
                                                    "runAsNonRoot": False,
                                                },
                                                "resources": {
                                                    "limits": {
                                                        "nvidia.com/gpu": "1",
                                                        "cpu": "3",
                                                        "memory": "10Gi",
                                                    },
                                                    "requests": {
                                                        "cpu": "1",
                                                        "memory": "10Gi",
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
