from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as k8s
from .model.ai.modelplane.clustermodel import v1alpha1 as clustermodelv1alpha1

test = compositiontest.CompositionTest(
    metadata=k8s.ObjectMeta(
        name="clustermodel-basic",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/clustermodels/composition.yaml",
        xrPath="examples/clustermodel/qwen-0.5b.yaml",
        xrdPath="apis/clustermodels/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        assertResources=[
            clustermodelv1alpha1.ClusterModel(
                apiVersion="modelplane.ai/v1alpha1",
                kind="ClusterModel",
                metadata=k8s.ObjectMeta(
                    name="qwen-0.5b-vllm",
                ),
                spec=clustermodelv1alpha1.Spec(
                    model=clustermodelv1alpha1.Model(
                        name="Qwen/Qwen2.5-0.5B-Instruct",
                    ),
                    source="HuggingFace",
                    huggingFace=clustermodelv1alpha1.HuggingFace(
                        repo="Qwen/Qwen2.5-0.5B-Instruct",
                    ),
                    engine="vLLM",
                    vllm=clustermodelv1alpha1.Vllm(
                        image="vllm/vllm-openai:v0.7.3",
                    ),
                    resources=clustermodelv1alpha1.Resources(
                        vram="2Gi",
                        cpu="3",
                        memory="10Gi",
                    ),
                ),
            ).model_dump(exclude_unset=True),
        ],
    ),
)
