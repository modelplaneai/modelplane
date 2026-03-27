from .lib import resource as libresource
from .model.ai.modelplane.clustermodel import v1alpha1 as cmv1alpha1
from .model.ai.modelplane.inferenceenvironment import v1alpha1 as iev1alpha1
from .model.ai.modelplane.inferencegateway import v1alpha1 as igwv1alpha1
from .model.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
from .model.ai.modelplane.modelplacement import v1alpha1 as mpv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-deployment-incompatible-env",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modeldeployments/composition.yaml",
        xrPath="tests/test-model-deployment-incompatible-env/xr.yaml",
        xrdPath="apis/modeldeployments/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        # extraResources is the up CLI's name for required resources.
        # These are resources the function reads but doesn't own, resolved
        # by Crossplane at runtime via response.require_resources().
        extraResources=[
            # Compatible: KServe backend supports vLLM.
            libresource.model_to_fixture(iev1alpha1.InferenceEnvironment(
                metadata=metav1.ObjectMeta(
                    name="compatible-env",
                    labels={"modelplane.ai/environment": "true"},
                ),
                spec=iev1alpha1.Spec(backend="KServe"),
                status=iev1alpha1.Status(
                    providerConfigRef=iev1alpha1.ProviderConfigRef(
                        name="compatible-cluster",
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
            # Incompatible: no backend set — engine compatibility check
            # should filter this out.
            libresource.model_to_fixture(iev1alpha1.InferenceEnvironment(
                metadata=metav1.ObjectMeta(
                    name="incompatible-env",
                    labels={"modelplane.ai/environment": "true"},
                ),
                spec=iev1alpha1.Spec(backend="KServe"),
                status=iev1alpha1.Status(
                    providerConfigRef=iev1alpha1.ProviderConfigRef(
                        name="incompatible-cluster",
                    ),
                    gateway=iev1alpha1.Gateway(address="10.0.0.2"),
                    capacity=iev1alpha1.Capacity(
                        backend="SomeOtherBackend",
                        gpuPools=[iev1alpha1.GpuPool(
                            acceleratorType="nvidia-l4",
                            count=2,
                            memory="24Gi",
                        )],
                    ),
                ),
            )),
            # The ClusterModel requesting vLLM engine.
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
        ],
        assertResources=[
            # Assert only the compatible environment gets a placement.
            libresource.model_to_dict(mpv1alpha1.ModelPlacement(
                metadata=metav1.ObjectMeta(
                    annotations={
                        "crossplane.io/composition-resource-name": "placement-compatible-env",
                    },
                    name="qwen-demo-compatible-env",
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
                        name="compatible-env",
                    ),
                ),
            )),
            # Assert the XR status shows 1 placement, not 2.
            libresource.model_to_dict(mdv1alpha1.ModelDeployment(
                metadata=metav1.ObjectMeta(
                    name="qwen-demo",
                    namespace="ml-team",
                ),
                spec=mdv1alpha1.Spec(
                    modelRef=mdv1alpha1.ModelRef(name="qwen-0.5b-vllm"),
                    environments=1,
                ),
                status=mdv1alpha1.Status(
                    model=mdv1alpha1.Model(name="Qwen/Qwen2.5-0.5B-Instruct"),
                    placements=mdv1alpha1.Placements(total=1, ready=0),
                    endpoint=mdv1alpha1.Endpoint(
                        url="http://10.0.0.100/ml-team/qwen-demo/v1/chat/completions",
                    ),
                ),
            )),
        ],
    ),
)
