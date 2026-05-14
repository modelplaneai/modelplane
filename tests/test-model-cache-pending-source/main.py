from .lib import resource as libresource
from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from .model.ai.modelplane.modelcache import v1alpha1 as mcv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

# Sources whose discriminator is locked in v0.1 but whose implementation
# is still pending (oci, configMap) should NOT crash or silently
# compose nothing — they should surface a clear condition so users see
# what's missing.
test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(name="model-cache-pending-source"),
    spec=compositiontest.Spec(
        compositionPath="apis/modelcaches/composition.yaml",
        xrPath="tests/test-model-cache-pending-source/xr.yaml",
        xrdPath="apis/modelcaches/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        extraResources=[
            libresource.model_to_fixture(
                icv1alpha1.InferenceCluster(
                    metadata=metav1.ObjectMeta(
                        name="prod-us-east",
                        labels={"modelplane.ai/cluster": "true"},
                    ),
                    spec=icv1alpha1.Spec(cluster=icv1alpha1.Cluster(source="Existing")),
                    status=icv1alpha1.Status(
                        providerConfigRef=icv1alpha1.ProviderConfigRef(name="prod-us-east-cluster"),
                    ),
                )
            ),
        ],
        # No Objects should be composed; the XR exits with an empty
        # status summary and an ImplementationPending reason on the
        # conditions (validated by inspection — assertResources only
        # checks the XR shape we explicitly set).
        assertResources=[
            libresource.model_to_dict(
                mcv1alpha1.ModelCache(
                    metadata=metav1.ObjectMeta(
                        name="nim-engine-via-oci",
                        namespace="ml-team",
                    ),
                    spec=mcv1alpha1.Spec(
                        artifact=mcv1alpha1.Artifact(
                            kind="Weights",
                            source=mcv1alpha1.Source(
                                oci=mcv1alpha1.Oci(
                                    image="nvcr.io/nim/meta/llama-3.1-70b-instruct:1.0.0",
                                ),
                            ),
                        ),
                        mount=mcv1alpha1.Mount(path="/mnt/model"),
                        storage=mcv1alpha1.Storage(
                            backend="PVC",
                            pvc=mcv1alpha1.Pvc(storageClassName="standard-rwx", sizeGiB=200),
                        ),
                        replication="AllMatchingClusters",
                    ),
                    status=mcv1alpha1.Status(
                        summary=mcv1alpha1.Summary(ready="0/0"),
                    ),
                )
            ),
        ],
    ),
)
