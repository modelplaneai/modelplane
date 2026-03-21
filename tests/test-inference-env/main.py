from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as k8s
from .model.ai.modelplane.inferenceenvironment import v1alpha1 as iev1alpha1

test = compositiontest.CompositionTest(
    metadata=k8s.ObjectMeta(
        name="inference-env-basic",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/inferenceenvironments/composition.yaml",
        xrPath="tests/test-inference-env/xr.yaml",
        xrdPath="apis/inferenceenvironments/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        assertResources=[
            # Assert on the XR status. On the first render pass, the
            # function returns early because the Namespace isn't observed
            # yet. Status has providerConfigRef and namespace but gpuPools
            # is empty (capacity is computed after GKECluster is composed
            # on the second pass).
            iev1alpha1.InferenceEnvironment(
                apiVersion="modelplane.ai/v1alpha1",
                kind="InferenceEnvironment",
                metadata=k8s.ObjectMeta(
                    name="demo-us-central",
                ),
                spec=iev1alpha1.Spec(
                    backend="KServe",
                ),
                status=iev1alpha1.Status(
                    providerConfigRef=iev1alpha1.ProviderConfigRef(
                        name="demo-us-central-cluster-kubeconfig",
                    ),
                    namespace="ie-demo-us-central",
                    capacity=iev1alpha1.Capacity(
                        backend="KServe",
                    ),
                ),
            ).model_dump(exclude_unset=True),
            # Assert the Namespace is composed on the first pass.
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": "ie-demo-us-central",
                    "annotations": {
                        "crossplane.io/composition-resource-name": "namespace",
                    },
                },
            },
        ],
    ),
)
