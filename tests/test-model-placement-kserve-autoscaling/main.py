from .lib import resource as libresource
from .model.ai.modelplane.clustermodel import v1alpha1 as cmv1alpha1
from .model.ai.modelplane.inferenceenvironment import v1alpha1 as iev1alpha1
from .model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-placement-kserve-autoscaling",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modelplacements/composition.yaml",
        xrPath="tests/test-model-placement-kserve-autoscaling/xr.yaml",
        xrdPath="apis/modelplacements/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        extraResources=[
            libresource.model_to_fixture(
                cmv1alpha1.ClusterModel(
                    metadata=metav1.ObjectMeta(name="qwen-0.5b"),
                    spec=cmv1alpha1.Spec(
                        model=cmv1alpha1.Model(name="Qwen/Qwen2.5-0.5B-Instruct"),
                        source="HuggingFace",
                        huggingFace=cmv1alpha1.HuggingFace(
                            repo="Qwen/Qwen2.5-0.5B-Instruct",
                        ),
                        serving=[
                            cmv1alpha1.ServingItem(
                                name="vllm-kserve",
                                backend="KServe",
                                engine=cmv1alpha1.Engine(
                                    name="vLLM",
                                    image="vllm/vllm-openai:v0.7.3",
                                ),
                            ),
                        ],
                        resources=cmv1alpha1.Resources(
                            vram="2Gi",
                            cpu="3",
                            memory="10Gi",
                        ),
                    ),
                )
            ),
            libresource.model_to_fixture(
                iev1alpha1.InferenceEnvironment(
                    metadata=metav1.ObjectMeta(
                        name="kserve-us-east",
                        labels={"modelplane.ai/environment": "true"},
                    ),
                    spec=iev1alpha1.Spec(backend="KServe"),
                    status=iev1alpha1.Status(
                        providerConfigRef=iev1alpha1.ProviderConfigRef(
                            name="kserve-us-east-cluster",
                        ),
                        gateway=iev1alpha1.Gateway(address="34.55.100.10"),
                        capacity=iev1alpha1.Capacity(
                            backend="KServe",
                            gpuPools=[
                                iev1alpha1.GpuPool(
                                    acceleratorType="nvidia-l4",
                                    count=8,
                                    memory="24Gi",
                                )
                            ],
                        ),
                    ),
                )
            ),
        ],
        assertResources=[
            # Assert the LLMInferenceService has replicas set to minReplicas
            # and Prometheus scraping annotations.
            libresource.model_to_dict(
                k8sobjv1alpha1.Object(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "model-serving",
                        },
                    ),
                    spec=k8sobjv1alpha1.Spec(
                        forProvider=k8sobjv1alpha1.ForProvider(
                            manifest={
                                "apiVersion": "serving.kserve.io/v1alpha1",
                                "kind": "LLMInferenceService",
                                "metadata": {
                                    "name": "model-qwen-0-5b",
                                    "namespace": "default",
                                },
                                "spec": {
                                    "replicas": 1,
                                },
                            },
                        ),
                    ),
                )
            ),
            # Assert the KEDA ScaledObject targets the KServe Deployment.
            libresource.model_to_dict(
                k8sobjv1alpha1.Object(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "keda-scaledobject",
                        },
                    ),
                    spec=k8sobjv1alpha1.Spec(
                        providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                            kind="ClusterProviderConfig",
                            name="kserve-us-east-cluster",
                        ),
                        forProvider=k8sobjv1alpha1.ForProvider(
                            manifest={
                                "apiVersion": "keda.sh/v1alpha1",
                                "kind": "ScaledObject",
                                "metadata": {
                                    "name": "model-qwen-0-5b-scaler",
                                    "namespace": "default",
                                },
                                "spec": {
                                    "scaleTargetRef": {
                                        "apiVersion": "apps/v1",
                                        "kind": "Deployment",
                                        "name": "model-qwen-0-5b-kserve",
                                    },
                                    "minReplicaCount": 1,
                                    "maxReplicaCount": 5,
                                    "pollingInterval": 15,
                                    "cooldownPeriod": 300,
                                    "triggers": [
                                        {
                                            "type": "prometheus",
                                            "metadata": {
                                                "serverAddress": "http://prometheus-prometheus.monitoring.svc.cluster.local:9090",
                                                "metricName": "envoy_cluster_upstream_rq_active",
                                                "query": (
                                                    "envoy_cluster_upstream_rq_active"
                                                    "{envoy_cluster_name="
                                                    '"httproute/default'
                                                    "/model-qwen-0-5b-kserve-route"
                                                    '/rule/0"}'
                                                ),
                                                "threshold": "7",
                                            },
                                        },
                                    ],
                                },
                            },
                        ),
                    ),
                )
            ),
        ],
    ),
)
