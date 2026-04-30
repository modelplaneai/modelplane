from .lib import resource as libresource
from .model.ai.modelplane.model import v1alpha1 as mv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-basic",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/models/composition.yaml",
        xrPath="tests/test-model/xr.yaml",
        xrdPath="apis/models/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        assertResources=[
            # Assert the XR spec is echoed back.
            libresource.model_to_dict(
                mv1alpha1.ModelModel(
                    metadata=metav1.ObjectMeta(
                        name="qwen-0.5b",
                        namespace="ml-team",
                    ),
                    spec=mv1alpha1.Spec(
                        model=mv1alpha1.Model(
                            name="Qwen/Qwen2.5-0.5B-Instruct",
                        ),
                        source="HuggingFace",
                        huggingFace=mv1alpha1.HuggingFace(
                            repo="Qwen/Qwen2.5-0.5B-Instruct",
                        ),
                        serving=[
                            mv1alpha1.ServingItem(
                                name="vllm-kserve",
                                engine=mv1alpha1.Engine(
                                    name="vLLM",
                                    image="vllm/vllm-openai:v0.7.3",
                                ),
                            ),
                        ],
                        resources=mv1alpha1.Resources(
                            vram="2Gi",
                        ),
                    ),
                )
            ),
        ],
    ),
)
