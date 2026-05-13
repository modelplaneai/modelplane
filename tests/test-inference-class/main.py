from .lib import resource as libresource
from .model.ai.modelplane.inferenceclass import v1alpha1 as iclv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="inference-class-basic",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/inferenceclasses/composition.yaml",
        xrPath="tests/test-inference-class/xr.yaml",
        xrdPath="apis/inferenceclasses/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        assertResources=[
            # InferenceClass is a data resource - status is empty, no
            # children are composed. Just assert the spec round-trips.
            libresource.model_to_dict(
                iclv1alpha1.InferenceClass(
                    metadata=metav1.ObjectMeta(name="gke-l4-1x-g2"),
                    spec=iclv1alpha1.Spec(
                        description="GKE g2-standard-8, 1x NVIDIA L4",
                        provisioning=iclv1alpha1.Provisioning(
                            provider="GKE",
                            gke=iclv1alpha1.Gke(
                                machineType="g2-standard-8",
                                diskSizeGb=100,
                                accelerator=iclv1alpha1.Accelerator(
                                    type="nvidia-l4",
                                    count=1,
                                ),
                            ),
                        ),
                        resources=iclv1alpha1.Resources(
                            gpu=iclv1alpha1.Gpu(
                                count=1,
                                memory="24Gi",
                            ),
                        ),
                    ),
                )
            ),
        ],
    ),
)
