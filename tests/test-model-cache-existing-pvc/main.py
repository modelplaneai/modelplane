from .lib import resource as libresource
from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from .model.ai.modelplane.modelcache import v1alpha1 as mcv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

# ExistingPVC backend: Modelplane composes no Objects on the workload
# cluster. The customer owns the PVC and its bytes. The cache reports
# Ready immediately for every matched cluster so a downstream
# ModelReplica can schedule.
test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(name="model-cache-existing-pvc"),
    spec=compositiontest.Spec(
        compositionPath="apis/modelcaches/composition.yaml",
        xrPath="tests/test-model-cache-existing-pvc/xr.yaml",
        xrdPath="apis/modelcaches/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        extraResources=[
            libresource.model_to_fixture(
                icv1alpha1.InferenceCluster(
                    metadata=metav1.ObjectMeta(
                        name="prod-us-east",
                        labels={
                            "modelplane.ai/cluster": "true",
                            "modelplane.ai/tier": "production",
                        },
                    ),
                    spec=icv1alpha1.Spec(cluster=icv1alpha1.Cluster(source="Existing")),
                    status=icv1alpha1.Status(
                        providerConfigRef=icv1alpha1.ProviderConfigRef(name="prod-us-east-cluster"),
                    ),
                )
            ),
        ],
        # The function composes no Objects for ExistingPVC. We assert
        # the XR status reflects 1/1 ready and the matched cluster shows
        # phase=Ready, which proves the backend dispatch worked.
        assertResources=[
            libresource.model_to_dict(
                mcv1alpha1.ModelCache(
                    metadata=metav1.ObjectMeta(
                        name="customer-staged-weights",
                        namespace="ml-team",
                    ),
                    spec=mcv1alpha1.Spec(
                        artifact=mcv1alpha1.Artifact(
                            kind="Weights",
                            source=mcv1alpha1.Source(
                                huggingFace=mcv1alpha1.HuggingFace(
                                    repo="meta-llama/Llama-3.3-70B-Instruct",
                                ),
                            ),
                        ),
                        mount=mcv1alpha1.Mount(path="/mnt/model"),
                        storage=mcv1alpha1.Storage(
                            backend="ExistingPVC",
                            existingPVC=mcv1alpha1.ExistingPVC(claimName="customer-llama-pvc"),
                        ),
                        clusterSelector=mcv1alpha1.ClusterSelector(
                            matchLabels={"modelplane.ai/tier": "production"},
                        ),
                        replication="AllMatchingClusters",
                    ),
                    status=mcv1alpha1.Status(
                        summary=mcv1alpha1.Summary(ready="1/1"),
                        clusters=[mcv1alpha1.Cluster(name="prod-us-east", phase="Ready")],
                    ),
                )
            ),
        ],
    ),
)
