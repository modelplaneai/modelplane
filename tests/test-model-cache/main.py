from .lib import resource as libresource
from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from .model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

# Expected PVC manifest on the remote cluster. Sized to match the XR
# (200Gi) with the default ReadWriteMany access mode.
_EXPECTED_PVC_MANIFEST = {
    "apiVersion": "v1",
    "kind": "PersistentVolumeClaim",
    "metadata": {
        "name": "modelcache-llama-3-3-70b",
        "namespace": "default",
        "labels": {"modelplane.ai/modelcache": "llama-3-3-70b"},
    },
    "spec": {
        "accessModes": ["ReadWriteMany"],
        "storageClassName": "standard-rwx",
        "resources": {"requests": {"storage": "200Gi"}},
    },
}

# Expected hydration Job manifest. The command short-circuits when the
# PVC already has content, then installs huggingface-cli and downloads
# the repo into /mnt/artifact.
_EXPECTED_JOB_MANIFEST = {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "metadata": {
        "name": "modelcache-llama-3-3-70b-hydrate",
        "namespace": "default",
        "labels": {"modelplane.ai/modelcache": "llama-3-3-70b"},
    },
    "spec": {
        "backoffLimit": 3,
        "ttlSecondsAfterFinished": 3600,
        "template": {
            "metadata": {"labels": {"modelplane.ai/modelcache": "llama-3-3-70b"}},
            "spec": {
                "restartPolicy": "OnFailure",
                "containers": [
                    {
                        "name": "hydrate",
                        "image": "python:3.11-slim",
                        "command": [
                            "/bin/sh",
                            "-c",
                            (
                                "set -e; "
                                'if [ -n "$(ls -A /mnt/artifact 2>/dev/null)" ]; then '
                                "  echo 'artifact already hydrated, skipping'; exit 0; "
                                "fi; "
                                "pip install --quiet 'huggingface_hub[cli]'; "
                                "huggingface-cli download meta-llama/Llama-3.3-70B-Instruct"
                                " --revision main --local-dir /mnt/artifact"
                            ),
                        ],
                        "env": [],
                        "volumeMounts": [
                            {"name": "artifact", "mountPath": "/mnt/artifact"},
                        ],
                    },
                ],
                "volumes": [
                    {
                        "name": "artifact",
                        "persistentVolumeClaim": {"claimName": "modelcache-llama-3-3-70b"},
                    },
                ],
            },
        },
    },
}

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="model-cache-basic",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/modelcaches/composition.yaml",
        xrPath="tests/test-model-cache/xr.yaml",
        xrdPath="apis/modelcaches/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        # The function reads InferenceClusters via required resources;
        # extraResources is the test-time stand-in. Status fields are
        # populated as if the cluster is fully provisioned.
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
                        providerConfigRef=icv1alpha1.ProviderConfigRef(
                            name="prod-us-east-cluster",
                        ),
                    ),
                )
            ),
        ],
        assertResources=[
            # PVC Object on the workload cluster.
            libresource.model_to_dict(
                k8sobjv1alpha1.Object(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "pvc-prod-us-east",
                        },
                    ),
                    spec=k8sobjv1alpha1.Spec(
                        providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                            kind="ClusterProviderConfig",
                            name="prod-us-east-cluster",
                        ),
                        readiness=k8sobjv1alpha1.Readiness(policy="DeriveFromObject"),
                        forProvider=k8sobjv1alpha1.ForProvider(manifest=_EXPECTED_PVC_MANIFEST),
                    ),
                )
            ),
            # Hydration Job Object on the workload cluster.
            libresource.model_to_dict(
                k8sobjv1alpha1.Object(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "hydrate-prod-us-east",
                        },
                    ),
                    spec=k8sobjv1alpha1.Spec(
                        providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                            kind="ClusterProviderConfig",
                            name="prod-us-east-cluster",
                        ),
                        readiness=k8sobjv1alpha1.Readiness(policy="DeriveFromObject"),
                        forProvider=k8sobjv1alpha1.ForProvider(manifest=_EXPECTED_JOB_MANIFEST),
                    ),
                )
            ),
        ],
    ),
)
