from .lib import resource as libresource
from .model.ai.modelplane.clustermodel import v1alpha1 as cmv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="clustermodel-basic",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/clustermodels/composition.yaml",
        xrPath="tests/test-cluster-model/xr.yaml",
        xrdPath="apis/clustermodels/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        assertResources=[
            libresource.model_to_dict(
                cmv1alpha1.ClusterModel(
                    metadata=metav1.ObjectMeta(
                        name="qwen-0.5b",
                    ),
                    spec=cmv1alpha1.Spec(
                        model=cmv1alpha1.Model(
                            name="Qwen/Qwen2.5-0.5B-Instruct",
                        ),
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
        ],
    ),
)
