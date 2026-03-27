from .lib import resource as libresource
from .model.ai.modelplane.clustermodel import v1alpha1 as cmv1alpha1
from .model.ai.modelplane.inferenceenvironment import v1alpha1 as iev1alpha1
from .model.ai.modelplane.inferencegateway import v1alpha1 as igwv1alpha1
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
            libresource.model_to_fixture(iev1alpha1.InferenceEnvironment(
                metadata=metav1.ObjectMeta(
                    name="env-a",
                    labels={"modelplane.ai/environment": "true"},
                ),
                spec=iev1alpha1.Spec(backend="KServe"),
                status=iev1alpha1.Status(
                    providerConfigRef=iev1alpha1.ProviderConfigRef(
                        name="env-a-cluster",
                    ),
                    gateway=iev1alpha1.Gateway(address="10.0.0.1"),
                    capacity=iev1alpha1.Capacity(
                        backend="KServe",
                        gpuPools=[iev1alpha1.GpuPool(
                            acceleratorType="nvidia-l4",
                            count=2,
                            memory="24Gi",
                        )],
                    ),
                ),
            )),
            # env-b: already has a placement (simulated via observedResources).
            # Both are compatible, but with environments=1 the scheduler
            # should prefer env-b because it already has an observed
            # placement — even though env-a sorts first lexicographically.
            libresource.model_to_fixture(iev1alpha1.InferenceEnvironment(
                metadata=metav1.ObjectMeta(
                    name="env-b",
                    labels={"modelplane.ai/environment": "true"},
                ),
                spec=iev1alpha1.Spec(backend="KServe"),
                status=iev1alpha1.Status(
                    providerConfigRef=iev1alpha1.ProviderConfigRef(
                        name="env-b-cluster",
                    ),
                    gateway=iev1alpha1.Gateway(address="10.0.0.2"),
                    capacity=iev1alpha1.Capacity(
                        backend="KServe",
                        gpuPools=[iev1alpha1.GpuPool(
                            acceleratorType="nvidia-l4",
                            count=2,
                            memory="24Gi",
                        )],
                    ),
                ),
            )),
            # The ClusterModel referenced by spec.modelRef.
            libresource.model_to_fixture(cmv1alpha1.ClusterModel(
                metadata=metav1.ObjectMeta(name="qwen-0.5b-vllm"),
                spec=cmv1alpha1.Spec(
                    model=cmv1alpha1.Model(name="Qwen/Qwen2.5-0.5B-Instruct"),
                    source="HuggingFace",
                    huggingFace=cmv1alpha1.HuggingFace(
                        repo="Qwen/Qwen2.5-0.5B-Instruct",
                    ),
                    engine="vLLM",
                    resources=cmv1alpha1.Resources(vram="2Gi"),
                ),
            )),
            # The InferenceGateway.
            libresource.model_to_fixture(igwv1alpha1.InferenceGateway(
                metadata=metav1.ObjectMeta(name="default"),
                spec=igwv1alpha1.Spec(backend="EnvoyGateway"),
                status=igwv1alpha1.Status(address="10.0.0.100"),
            )),
            # The existing placement on env-b, also visible as a required
            # resource so the scheduler knows env-b already has a placement.
            libresource.model_to_fixture(mpv1alpha1.ModelPlacement(
                metadata=metav1.ObjectMeta(
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
        # Simulate a second reconcile where placement-env-b already exists.
        # The scheduler should keep env-b, not reschedule to env-a.
        observedResources=[
            libresource.model_to_dict(mpv1alpha1.ModelPlacement(
                metadata=metav1.ObjectMeta(
                    name="qwen-demo-env-b",
                    namespace="ml-team",
                    annotations={
                        "crossplane.io/composition-resource-name": "placement-env-b",
                    },
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
