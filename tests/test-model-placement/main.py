from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as k8s
from .model.ai.modelplane.modelplacement import v1alpha1 as mpv1alpha1

test = compositiontest.CompositionTest(
    metadata=k8s.ObjectMeta(
        name="model-placement-basic",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modelplacements/composition.yaml",
        xrPath="tests/test-model-placement/xr.yaml",
        xrdPath="apis/modelplacements/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        assertResources=[
            # Assert the XR status has the model name and GPU count set.
            mpv1alpha1.ModelPlacement(
                apiVersion="modelplane.ai/v1alpha1",
                kind="ModelPlacement",
                metadata=k8s.ObjectMeta(
                    name="qwen-demo-us-central",
                    namespace="ml-team",
                ),
                spec=mpv1alpha1.Spec(
                    modelRef=mpv1alpha1.ModelRef(
                        name="qwen-0.5b-vllm",
                    ),
                    inferenceEnvironmentRef=mpv1alpha1.InferenceEnvironmentRef(
                        name="demo-us-central",
                    ),
                ),
            ).model_dump(exclude_unset=True),
        ],
    ),
)
