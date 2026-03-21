from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as k8s
from .model.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1

test = compositiontest.CompositionTest(
    metadata=k8s.ObjectMeta(
        name="model-deployment-basic",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modeldeployments/composition.yaml",
        xrPath="tests/test-model-deployment/xr.yaml",
        xrdPath="apis/modeldeployments/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        assertResources=[
            # Assert the XR has the right spec.
            mdv1alpha1.ModelDeployment(
                apiVersion="modelplane.ai/v1alpha1",
                kind="ModelDeployment",
                metadata=k8s.ObjectMeta(
                    name="qwen-demo",
                    namespace="ml-team",
                ),
                spec=mdv1alpha1.Spec(
                    modelRef=mdv1alpha1.ModelRef(
                        name="qwen-0.5b-vllm",
                    ),
                    environments=1,
                ),
            ).model_dump(exclude_unset=True),
        ],
    ),
)
