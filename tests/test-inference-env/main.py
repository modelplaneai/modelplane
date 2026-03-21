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
            # Assert on the XR itself — status should have providerConfigRef
            # and capacity.
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
                        gpuPools=[
                            iev1alpha1.GpuPool(
                                acceleratorType="nvidia-l4",
                                memory="24Gi",
                                count=1,
                            ),
                        ],
                    ),
                ),
            ).model_dump(exclude_unset=True),
            # Assert GKECluster is composed in the right namespace with
            # the right project and region. Don't assert on fields with
            # XRD defaults (diskSizeGb, nodeCount, etc.) — those are the
            # GKECluster XRD's responsibility, not ours.
            {
                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                "kind": "GKECluster",
                "metadata": {
                    "name": "demo-us-central",
                    "namespace": "ie-demo-us-central",
                    "annotations": {
                        "crossplane.io/composition-resource-name": "gke-cluster",
                    },
                },
                "spec": {
                    "project": "my-gcp-project",
                    "region": "us-central1",
                    "nodePools": [
                        {
                            "name": "system",
                            "role": "System",
                            "machineType": "e2-standard-4",
                            "minNodeCount": 1,
                            "maxNodeCount": 2,
                        },
                        {
                            "name": "gpu-l4",
                            "role": "GPU",
                            "machineType": "g2-standard-8",
                            "maxNodeCount": 2,
                            "gpu": {
                                "acceleratorType": "nvidia-l4",
                            },
                            "zones": ["us-central1-a", "us-central1-c"],
                        },
                    ],
                },
            },
        ],
    ),
)
