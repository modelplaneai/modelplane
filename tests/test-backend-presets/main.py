"""Test KServe LLMInferenceServiceConfig preset composition.

The kserve-llmisvc-resources Helm chart ships the CRD but does NOT
create the six default LLMInferenceServiceConfig instances the
controller looks up via spec.baseRefs. compose-kserve-backend fills
that gap by composing six provider-kubernetes Object MRs targeting
the workload cluster. This test asserts:

- All six preset Object MRs are composed when kserve-controller is
  observed (the gate condition).
- Each Object MR's providerConfigRef points at the ClusterProviderConfig
  named after the parent InferenceCluster — NOT at the helm/k8s
  ProviderConfig the kserve-backend creates for Helm. Catches the
  copy-paste class of bug where the wrong PC name lands on a generated
  Object MR and the MR sits Synced=False with "ClusterProviderConfig
  ... not found".
- Manifest is shaped correctly: empty spec, kserve namespace,
  serving.kserve.io/v1alpha1 LLMInferenceServiceConfig.
"""

from .lib import resource as libresource
from .model.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1
from .model.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from .model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from .model.io.crossplane.m.kubernetes.providerconfig import v1alpha1 as k8spcv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

# KServeBackend XR is named `<inferencecluster>-kserve`. The
# composition strips that suffix to derive the parent name, then
# expects the ClusterProviderConfig at `<inferencecluster>-cluster-kubeconfig`.
# The xr.yaml here has name=gpu-us-central1-kserve so the expected CPC is:
_EXPECTED_CPC_NAME = "gpu-us-central1-cluster-kubeconfig"

_PRESET_NAMES = (
    "kserve-config-llm-default",
    "kserve-config-llm-router-route",
    "kserve-config-llm-worker-tensor-parallel",
    "kserve-config-llm-worker-pipeline-parallel",
    "kserve-config-llm-decode",
    "kserve-config-llm-prefill",
)


def _preset_object(preset_name):
    return libresource.model_to_dict(
        k8sobjv1alpha1.Object(
            metadata=metav1.ObjectMeta(
                namespace="gpu-us-central1",
                annotations={
                    "crossplane.io/composition-resource-name": f"kserve-preset-{preset_name}",
                },
            ),
            spec=k8sobjv1alpha1.Spec(
                providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                    kind="ClusterProviderConfig",
                    name=_EXPECTED_CPC_NAME,
                ),
                readiness=k8sobjv1alpha1.Readiness(policy="SuccessfulCreate"),
                forProvider=k8sobjv1alpha1.ForProvider(
                    manifest={
                        "apiVersion": "serving.kserve.io/v1alpha1",
                        "kind": "LLMInferenceServiceConfig",
                        "metadata": {
                            "name": preset_name,
                            "namespace": "kserve",
                        },
                        "spec": {},
                    },
                ),
            ),
        )
    )


test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="kservebackend-presets",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/kservebackends/composition.yaml",
        xrPath="tests/test-backend-presets/xr.yaml",
        xrdPath="apis/kservebackends/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        # Simulate a state where both ProviderConfigs + the kserve-controller
        # Helm release are observed. The preset code gates on
        # `controller_observed`; without this fixture it stays dormant.
        observedResources=[
            libresource.model_to_fixture(
                k8spcv1alpha1.ProviderConfig(
                    metadata=metav1.ObjectMeta(
                        name="gpu-us-central1-kserve-cluster",
                        annotations={
                            "crossplane.io/composition-resource-name": "provider-config-kubernetes",
                        },
                    ),
                    spec=k8spcv1alpha1.Spec(
                        credentials=k8spcv1alpha1.Credentials(source="Secret"),
                    ),
                )
            ),
            libresource.model_to_fixture(
                helmpcv1beta1.ProviderConfig(
                    metadata=metav1.ObjectMeta(
                        name="gpu-us-central1-kserve-cluster",
                        annotations={
                            "crossplane.io/composition-resource-name": "provider-config-helm",
                        },
                    ),
                    spec=helmpcv1beta1.Spec(
                        credentials=helmpcv1beta1.Credentials(source="Secret"),
                    ),
                )
            ),
            # kserve-controller Helm release observed — this is what
            # gates preset composition.
            libresource.model_to_fixture(
                helmv1beta1.Release(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "kserve-controller",
                        },
                    ),
                    spec=helmv1beta1.Spec(
                        forProvider=helmv1beta1.ForProvider(
                            chart=helmv1beta1.Chart(
                                name="kserve-llmisvc-resources",
                                repository="oci://ghcr.io/kserve/charts",
                                version="v0.16.0",
                            ),
                            namespace="kserve",
                        ),
                    ),
                )
            ),
        ],
        assertResources=[_preset_object(p) for p in _PRESET_NAMES],
    ),
)
