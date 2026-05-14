"""Compose a ModelCache.

Stages an artifact onto a PVC on every matched InferenceCluster. For
each cluster the function composes a PVC and a one-shot hydration Job
via provider-kubernetes Objects. The Job downloads the artifact (from
HuggingFace or S3) into the PVC; pods that mount the PVC then see the
artifact as a local directory.

v0.1 scope: Weights kind, PVC backend, HuggingFace + S3 sources,
replication = AllMatchingClusters. ContentAddressed / Custom backends,
Tokenizer / Bytes / Adapter / Engine kinds, BYO ExistingPVC, and
per-cluster selector overrides are deferred.

Out of scope here: ModelDeployment integration. Attaching a cache's PVC
to a model serving pod lives in compose-model-replica and is deferred
until the new ModelDeployment shape (PR #75) stabilizes.
"""

from crossplane.function import request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .lib import conditions, metadata
from .lib import resource as libresource
from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from .model.ai.modelplane.modelcache import v1alpha1
from .model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

# Condition types and reasons.
CONDITION_TYPE_CLUSTERS_MATCHED = "ClustersMatched"
CONDITION_TYPE_ARTIFACT_READY = "ArtifactReady"

CONDITION_REASON_MATCHED = "Matched"
CONDITION_REASON_NO_CLUSTERS = "NoClusters"
CONDITION_REASON_HYDRATING = "Hydrating"
CONDITION_REASON_STAGED = "Staged"
CONDITION_REASON_PARTIAL = "Partial"

# Artifact kind / storage backend / source discriminators.
ARTIFACT_KIND_WEIGHTS = "Weights"
STORAGE_BACKEND_PVC = "PVC"

# Per-cluster status phases.
PHASE_PENDING = "Pending"
PHASE_HYDRATING = "Hydrating"
PHASE_READY = "Ready"
PHASE_FAILED = "Failed"

# Namespace on the remote workload cluster where staging resources land.
REMOTE_NS = metadata.NAMESPACE_REMOTE

# Image used by the hydration Job. Pinned so behavior is reproducible.
# Contains python + huggingface-cli + awscli. Real builds should swap
# for a Modelplane-owned image.
HYDRATION_IMAGE = "python:3.11-slim"


class Composer:
    def __init__(self, req, rsp):
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.ModelCache(**resource.struct_to_dict(req.observed.composite.resource))
        self.clusters: list[icv1alpha1.InferenceCluster] = []

    def compose(self):
        if not self.resolve_inputs():
            return
        matched = self.match_clusters()
        if not matched:
            self.write_status([], [])
            self.derive_conditions([], [])
            return

        per_cluster_status = []
        for cluster in matched:
            phase = self.compose_cluster(cluster)
            per_cluster_status.append((cluster.metadata.name, phase))

        self.write_status(matched, per_cluster_status)
        self.derive_conditions(matched, per_cluster_status)

    def resolve_inputs(self):
        """Fetch all InferenceClusters labeled modelplane.ai/cluster=true."""
        match_labels: dict[str, str] = {
            metadata.LABEL_KEY_CLUSTER: metadata.LABEL_VALUE_CLUSTER,
        }
        if self.xr.spec.clusterSelector and self.xr.spec.clusterSelector.matchLabels:
            match_labels.update(self.xr.spec.clusterSelector.matchLabels)

        response.require_resources(
            self.rsp,
            name="clusters",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceCluster",
            match_labels=match_labels,
        )

        cluster_dicts = request.get_required_resources(self.req, "clusters")
        if cluster_dicts is None:
            return False

        self.clusters = [icv1alpha1.InferenceCluster.model_validate(c) for c in cluster_dicts]
        return True

    def match_clusters(self) -> list[icv1alpha1.InferenceCluster]:
        """Filter to clusters whose providerConfigRef is set.

        A cluster without a providerConfigRef hasn't been provisioned
        yet; we can't compose remote resources onto it.
        """
        return [c for c in self.clusters if c.status and c.status.providerConfigRef and c.status.providerConfigRef.name]

    def compose_cluster(self, cluster: icv1alpha1.InferenceCluster) -> str:
        """Compose the PVC + hydration Job on one cluster.

        Returns the per-cluster phase.
        """
        pc_name = cluster.status.providerConfigRef.name
        cluster_name = cluster.metadata.name
        pvc_key = f"pvc-{cluster_name}"
        job_key = f"hydrate-{cluster_name}"

        pvc_manifest = self.pvc_manifest()
        job_manifest = self.job_manifest()

        resource.update(
            self.rsp.desired.resources[pvc_key],
            self.remote_object(pc_name, pvc_manifest),
        )
        resource.update(
            self.rsp.desired.resources[job_key],
            self.remote_object(pc_name, job_manifest),
        )

        # Derive phase from observed state.
        pvc_ready = conditions.has_condition(self.req, pvc_key, "Ready")
        job_complete = self._job_complete(job_key)
        job_failed = self._job_failed(job_key)

        if job_failed:
            return PHASE_FAILED
        if job_complete and pvc_ready:
            self.rsp.desired.resources[pvc_key].ready = fnv1.READY_TRUE
            self.rsp.desired.resources[job_key].ready = fnv1.READY_TRUE
            return PHASE_READY
        if pvc_ready:
            return PHASE_HYDRATING
        return PHASE_PENDING

    def pvc_manifest(self) -> dict:
        """PVC for the artifact on the workload cluster."""
        pvc = self.xr.spec.storage.pvc
        access_mode = pvc.accessMode or "ReadWriteMany"
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": self.pvc_name(),
                "namespace": REMOTE_NS,
                "labels": self._labels(),
            },
            "spec": {
                "accessModes": [access_mode],
                "storageClassName": pvc.storageClassName,
                "resources": {"requests": {"storage": f"{pvc.sizeGiB}Gi"}},
            },
        }

    def job_manifest(self) -> dict:
        """One-shot Job that hydrates the PVC from the artifact source."""
        env, command = self._hydration_env_and_command()

        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": self.job_name(),
                "namespace": REMOTE_NS,
                "labels": self._labels(),
            },
            "spec": {
                "backoffLimit": 3,
                "ttlSecondsAfterFinished": 3600,
                "template": {
                    "metadata": {"labels": self._labels()},
                    "spec": {
                        "restartPolicy": "OnFailure",
                        "containers": [
                            {
                                "name": "hydrate",
                                "image": HYDRATION_IMAGE,
                                "command": ["/bin/sh", "-c", command],
                                "env": env,
                                "volumeMounts": [
                                    {"name": "artifact", "mountPath": "/mnt/artifact"},
                                ],
                            },
                        ],
                        "volumes": [
                            {
                                "name": "artifact",
                                "persistentVolumeClaim": {"claimName": self.pvc_name()},
                            },
                        ],
                    },
                },
            },
        }

    def _hydration_env_and_command(self) -> tuple[list[dict], str]:
        """Build the env + shell command for the hydration container.

        Returns (env, command). One of huggingFace / s3 must be set on
        the spec; the discriminator is the presence of the field.
        """
        src = self.xr.spec.artifact.source
        env: list[dict] = []

        if src.huggingFace:
            hf = src.huggingFace
            revision_arg = f" --revision {hf.revision}" if hf.revision else ""
            if hf.tokenSecretRef:
                env.append(
                    {
                        "name": "HF_TOKEN",
                        "valueFrom": {
                            "secretKeyRef": {
                                "name": hf.tokenSecretRef.name,
                                "key": hf.tokenSecretRef.key or "HF_TOKEN",
                            },
                        },
                    },
                )
            # Skip download if the directory already has content (idempotent rerun).
            command = (
                "set -e; "
                "if [ -n \"$(ls -A /mnt/artifact 2>/dev/null)\" ]; then "
                "  echo 'artifact already hydrated, skipping'; exit 0; "
                "fi; "
                "pip install --quiet 'huggingface_hub[cli]'; "
                f"huggingface-cli download {hf.repo}{revision_arg} "
                "  --local-dir /mnt/artifact"
            )
            return env, command

        if src.s3:
            s3 = src.s3
            if s3.secretRef:
                env.append(
                    {
                        "name": "AWS_ACCESS_KEY_ID",
                        "valueFrom": {"secretKeyRef": {"name": s3.secretRef.name, "key": "access_key"}},
                    },
                )
                env.append(
                    {
                        "name": "AWS_SECRET_ACCESS_KEY",
                        "valueFrom": {"secretKeyRef": {"name": s3.secretRef.name, "key": "secret_key"}},
                    },
                )
            if s3.region:
                env.append({"name": "AWS_DEFAULT_REGION", "value": s3.region})
            command = (
                "set -e; "
                "if [ -n \"$(ls -A /mnt/artifact 2>/dev/null)\" ]; then "
                "  echo 'artifact already hydrated, skipping'; exit 0; "
                "fi; "
                "pip install --quiet awscli; "
                f"aws s3 sync {s3.uri} /mnt/artifact"
            )
            return env, command

        # No source set; produce a noop command so the Job at least
        # surfaces a clear error from the container instead of
        # crash-looping with an undefined command.
        return env, "echo 'no artifact source set on ModelCache' >&2; exit 1"

    def remote_object(self, provider_config: str, manifest: dict) -> k8sobjv1alpha1.Object:
        """Wrap a manifest as a provider-kubernetes Object targeting cluster."""
        return k8sobjv1alpha1.Object(
            spec=k8sobjv1alpha1.Spec(
                providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                    kind="ClusterProviderConfig",
                    name=provider_config,
                ),
                readiness=k8sobjv1alpha1.Readiness(policy="DeriveFromObject"),
                forProvider=k8sobjv1alpha1.ForProvider(manifest=manifest),
            ),
        )

    def write_status(self, matched, per_cluster_status):
        """Write spec.status.summary and spec.status.clusters."""
        ready_count = sum(1 for _, phase in per_cluster_status if phase == PHASE_READY)
        total = len(matched)

        clusters_status = [
            v1alpha1.ClustersItem(name=name, phase=phase)
            for name, phase in per_cluster_status
        ]

        status = v1alpha1.Status(
            summary=v1alpha1.Summary(ready=f"{ready_count}/{total}"),
            clusters=clusters_status,
        )
        libresource.update_status(self.rsp.desired.composite, status)

    def derive_conditions(self, matched, per_cluster_status):
        if not matched:
            conditions.set_condition(self.rsp, CONDITION_TYPE_CLUSTERS_MATCHED, False, CONDITION_REASON_NO_CLUSTERS)
            conditions.set_condition(self.rsp, CONDITION_TYPE_ARTIFACT_READY, False, CONDITION_REASON_NO_CLUSTERS)
            return

        conditions.set_condition(self.rsp, CONDITION_TYPE_CLUSTERS_MATCHED, True, CONDITION_REASON_MATCHED)

        ready_count = sum(1 for _, phase in per_cluster_status if phase == PHASE_READY)
        if ready_count == len(matched):
            conditions.set_condition(self.rsp, CONDITION_TYPE_ARTIFACT_READY, True, CONDITION_REASON_STAGED)
            self.rsp.desired.composite.ready = fnv1.READY_TRUE
        elif ready_count > 0:
            conditions.set_condition(self.rsp, CONDITION_TYPE_ARTIFACT_READY, False, CONDITION_REASON_PARTIAL)
        else:
            conditions.set_condition(self.rsp, CONDITION_TYPE_ARTIFACT_READY, False, CONDITION_REASON_HYDRATING)

    def pvc_name(self) -> str:
        return f"modelcache-{self.xr.metadata.name}"

    def job_name(self) -> str:
        return f"modelcache-{self.xr.metadata.name}-hydrate"

    def _labels(self) -> dict[str, str]:
        return {
            "modelplane.ai/modelcache": self.xr.metadata.name,
        }

    def _job_complete(self, job_key: str) -> bool:
        """Check the remote Job has succeeded by looking at the Object's
        atProvider.manifest.status.succeeded."""
        observed = self.req.observed.resources.get(job_key)
        if not observed:
            return False
        d = resource.struct_to_dict(observed.resource)
        succeeded = (
            d.get("status", {})
            .get("atProvider", {})
            .get("manifest", {})
            .get("status", {})
            .get("succeeded", 0)
        )
        return int(succeeded or 0) >= 1

    def _job_failed(self, job_key: str) -> bool:
        """Check the remote Job has hit its backoffLimit."""
        observed = self.req.observed.resources.get(job_key)
        if not observed:
            return False
        d = resource.struct_to_dict(observed.resource)
        for c in d.get("status", {}).get("atProvider", {}).get("manifest", {}).get("status", {}).get("conditions", []):
            if c.get("type") == "Failed" and c.get("status") == "True":
                return True
        return False


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose PVC + hydration Job per matched cluster."""
    Composer(req, rsp).compose()
