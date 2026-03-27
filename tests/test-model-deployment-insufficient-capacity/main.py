from .lib import resource as libresource
from .model.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-deployment-insufficient-capacity",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modeldeployments/composition.yaml",
        xrPath="tests/test-model-deployment-insufficient-capacity/xr.yaml",
        xrdPath="apis/modeldeployments/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        # extraResources is the up CLI's name for required resources.
        # These are resources the function reads but doesn't own, resolved
        # by Crossplane at runtime via response.require_resources().
        extraResources=[
            # An environment with a single 24Gi L4 GPU — not enough for
            # a 140Gi model that needs 6+ GPUs but only 1 is available.
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "InferenceEnvironment",
                "metadata": {
                    "name": "small-env",
                    "labels": {"modelplane.ai/environment": "true"},
                },
                "spec": {"backend": "KServe"},
                "status": {
                    "providerConfigRef": {"name": "small-cluster"},
                    "gateway": {"address": "10.0.0.1"},
                    "capacity": {
                        "backend": "KServe",
                        "gpuPools": [{
                            "acceleratorType": "nvidia-l4",
                            "count": 1,
                            "memory": "24Gi",
                        }],
                    },
                },
            },
            # A 140Gi model — needs ceil(140/24) = 6 GPUs, but only 1
            # is available. Should not be scheduled.
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "ClusterModel",
                "metadata": {"name": "llama-70b"},
                "spec": {
                    "model": {"name": "meta-llama/Llama-3-70B"},
                    "source": "HuggingFace",
                    "huggingFace": {"repo": "meta-llama/Llama-3-70B"},
                    "engine": "vLLM",
                    "resources": {"vram": "140Gi"},
                },
            },
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "InferenceGateway",
                "metadata": {"name": "default"},
                "spec": {"backend": "EnvoyGateway"},
                "status": {"address": "10.0.0.100"},
            },
        ],
        assertResources=[
            # Assert no placements — the model doesn't fit.
            libresource.model_to_dict(mdv1alpha1.ModelDeployment(
                metadata=metav1.ObjectMeta(
                    name="llama-demo",
                    namespace="ml-team",
                ),
                spec=mdv1alpha1.Spec(
                    modelRef=mdv1alpha1.ModelRef(name="llama-70b"),
                    environments=1,
                ),
                status=mdv1alpha1.Status(
                    model=mdv1alpha1.Model(name="meta-llama/Llama-3-70B"),
                    placements=mdv1alpha1.Placements(total=0, ready=0),
                ),
            )),
        ],
    ),
)
