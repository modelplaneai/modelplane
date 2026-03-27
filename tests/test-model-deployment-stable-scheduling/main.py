from .lib import resource as libresource
from .model.ai.modelplane.modelplacement import v1alpha1 as mpv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-deployment-stable-scheduling",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modeldeployments/composition.yaml",
        xrPath="tests/test-model-deployment-stable-scheduling/xr.yaml",
        xrdPath="apis/modeldeployments/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        # extraResources is the up CLI's name for required resources.
        # These are resources the function reads but doesn't own, resolved
        # by Crossplane at runtime via response.require_resources().
        extraResources=[
            # env-a: a new environment that just came online. Sorts first
            # lexicographically, so without the stability sort it would
            # displace env-b's existing placement.
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "InferenceEnvironment",
                "metadata": {
                    "name": "env-a",
                    "labels": {"modelplane.ai/environment": "true"},
                },
                "spec": {"backend": "KServe"},
                "status": {
                    "providerConfigRef": {"name": "env-a-cluster"},
                    "gateway": {"address": "10.0.0.1"},
                    "capacity": {
                        "backend": "KServe",
                        "gpuPools": [{
                            "acceleratorType": "nvidia-l4",
                            "count": 2,
                            "memory": "24Gi",
                        }],
                    },
                },
            },
            # env-b: already has a placement (simulated via observedResources).
            # Both are compatible, but with environments=1 the scheduler
            # should prefer env-b because it already has an observed
            # placement — even though env-a sorts first lexicographically.
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "InferenceEnvironment",
                "metadata": {
                    "name": "env-b",
                    "labels": {"modelplane.ai/environment": "true"},
                },
                "spec": {"backend": "KServe"},
                "status": {
                    "providerConfigRef": {"name": "env-b-cluster"},
                    "gateway": {"address": "10.0.0.2"},
                    "capacity": {
                        "backend": "KServe",
                        "gpuPools": [{
                            "acceleratorType": "nvidia-l4",
                            "count": 2,
                            "memory": "24Gi",
                        }],
                    },
                },
            },
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "ClusterModel",
                "metadata": {"name": "qwen-0.5b-vllm"},
                "spec": {
                    "model": {"name": "Qwen/Qwen2.5-0.5B-Instruct"},
                    "source": "HuggingFace",
                    "huggingFace": {"repo": "Qwen/Qwen2.5-0.5B-Instruct"},
                    "engine": "vLLM",
                    "resources": {"vram": "2Gi"},
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
        # Simulate a second reconcile where placement-env-b already exists.
        # The scheduler should keep env-b, not reschedule to env-a.
        observedResources=[
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "ModelPlacement",
                "metadata": {
                    "name": "qwen-demo-env-b",
                    "namespace": "ml-team",
                    "annotations": {
                        "crossplane.io/composition-resource-name": "placement-env-b",
                    },
                    "labels": {
                        "modelplane.ai/placement": "true",
                        "modelplane.ai/deployment": "qwen-demo",
                    },
                },
                "spec": {
                    "modelRef": {
                        "kind": "ClusterModel",
                        "name": "qwen-0.5b-vllm",
                    },
                    "inferenceEnvironmentRef": {"name": "env-b"},
                },
            },
        ],
        assertResources=[
            # Assert the placement stays on env-b, not rescheduled to env-a.
            libresource.model_to_dict(mpv1alpha1.ModelPlacement(
                metadata=metav1.ObjectMeta(
                    annotations={
                        "crossplane.io/composition-resource-name": "placement-env-b",
                    },
                    name="qwen-demo-env-b",
                    namespace="ml-team",
                    labels={
                        "modelplane.ai/placement": "true",
                        "modelplane.ai/deployment": "qwen-demo",
                    },
                ),
                spec=mpv1alpha1.Spec(
                    modelRef=mpv1alpha1.ModelRef(
                        kind="ClusterModel",
                        name="qwen-0.5b-vllm",
                    ),
                    inferenceEnvironmentRef=mpv1alpha1.InferenceEnvironmentRef(
                        name="env-b",
                    ),
                ),
            )),
        ],
    ),
)
