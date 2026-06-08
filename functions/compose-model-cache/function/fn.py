"""Compose a ModelCache.

Stages a HuggingFace model onto a ReadWriteMany PVC on every matched
InferenceCluster via a one-shot hydration Job. Pods that reference the
cache (ModelDeployment.spec.modelCacheRef -> ModelReplica) mount the PVC
at /mnt/models, so weights are downloaded once per cluster and read N
times by every pod in an LWS gang.

v0.1 surface (locked to the merged XRD): source `huggingFace` only,
Modelplane-managed RWX PVC, replication to all matching clusters.
"""

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelcache import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

# Condition types/reasons for the ModelCache XR.
CONDITION_TYPE_SOURCE_VALID = "SourceValid"
CONDITION_TYPE_CLUSTERS_MATCHED = "ClustersMatched"
CONDITION_TYPE_ARTIFACT_READY = "ArtifactReady"

CONDITION_REASON_MATCHED = "Matched"
CONDITION_REASON_NO_CLUSTERS = "NoClusters"
CONDITION_REASON_HYDRATING = "Hydrating"
CONDITION_REASON_STAGED = "Staged"
CONDITION_REASON_PARTIAL = "Partial"
CONDITION_REASON_NO_SOURCE = "NoSource"
CONDITION_REASON_SUPPORTED = "Supported"

# Per-cluster phases reported in status.clusters[].phase.
PHASE_PENDING = "Pending"
PHASE_HYDRATING = "Hydrating"
PHASE_READY = "Ready"
PHASE_FAILED = "Failed"

# Namespace on the workload cluster where the PVC + Job land. This MUST match
# the namespace the serving pods land in (native.py/llmd.py `_REMOTE_NAMESPACE`,
# also "default") — a pod can only mount a PVC in its own namespace. The two
# functions hardcode this independently (no shared lib); they are a contract and
# must change together. If serving moves to per-deployment namespaces (negz's
# musing on #99), the cache PVC namespace moves with it.
REMOTE_NS = "default"

# The cluster-presence label every InferenceCluster carries; the matcher
# always includes it (match_labels={} is dropped by protobuf). This mirrors
# compose-model-deployment's cluster matching exactly. negz's PR #51 removes
# this workaround (bare ResourceSelector once `up` ships Crossplane >=2.2.1 and
# function-sdk-python grows a require_all helper) — when it lands, migrate this
# matcher alongside compose-model-deployment's, not separately.
LABEL_KEY_CLUSTER = "modelplane.ai/cluster"
LABEL_VALUE_CLUSTER = "true"

# Hydration container. python:3.11-slim has pip; we install huggingface_hub
# at runtime. A Modelplane-owned image with the tool preinstalled is a
# follow-up.
HYDRATION_IMAGE = "python:3.11-slim"
HYDRATION_MOUNT = "/mnt/artifact"

# Per-source default RWX storage class, mirroring the InferenceCluster XRD
# defaults (GKE/Existing -> Filestore-backed modelplane-rwx; EKS -> EFS-backed
# modelplane-rwx-efs). Used only when a cluster omits its cache block entirely:
# Pydantic doesn't apply the nested storageClassName default in that case, so a
# flat "modelplane-rwx" fallback would point an EKS PVC at a non-existent class.
_DEFAULT_STORAGE_CLASS = {"GKE": "modelplane-rwx", "EKS": "modelplane-rwx-efs", "Existing": "modelplane-rwx"}


def _storage_class(cluster: icv1alpha1.InferenceCluster) -> str:
    """RWX storage class for the cache PVC, from the InferenceCluster's
    per-source cache config, falling back to the source's XRD default."""
    c = cluster.spec.cluster
    cache = None
    if c.source == "GKE" and c.gke:
        cache = c.gke.cache
    elif c.source == "EKS" and c.eks:
        cache = c.eks.cache
    elif c.source == "Existing" and c.existing:
        cache = c.existing.cache
    if cache and cache.storageClassName:
        return cache.storageClassName
    return _DEFAULT_STORAGE_CLASS.get(c.source, "modelplane-rwx")


# A completion marker written only after a fully successful download. The Job
# skips when the marker is present, so a re-run (eviction, replay, backoff)
# doesn't redownload a complete cache. Checking for the marker — not for a
# non-empty directory — is what makes a re-run SAFE: a download interrupted
# mid-pull leaves files but no marker, so the retry resumes (`hf download` is
# resumable) instead of seeing a non-empty dir and falsely concluding "already
# hydrated", which would serve truncated weights. Using the marker also
# sidesteps the Filestore `lost+found` directory that broke a bare emptiness
# check (it sits at the mount root of ext4-backed PVCs). Supersedes PR #78's
# emptiness check, which had the partial-download false-skip bug.
_HYDRATED_MARKER = f"{HYDRATION_MOUNT}/.modelplane-hydrated"
_SKIP_IF_HYDRATED = f"if [ -f {_HYDRATED_MARKER} ]; then echo 'already hydrated, skipping'; exit 0; fi; "


def _hf_hydration(hf) -> tuple[list[dict], str]:
    """Return (env, shell command) for a HuggingFace source.

    huggingface-hub 1.x deprecated `huggingface-cli` (and dropped the `[cli]`
    extra), so the Job previously died at install. Use `hf download` directly.
    The marker is touched only after a successful download (set -e aborts the
    chain on failure, so a failed pull never marks the cache complete).
    """
    env: list[dict] = []
    if hf.authSecret:
        env.append(
            {
                "name": "HF_TOKEN",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": hf.authSecret.name,
                        "key": hf.authSecret.key or "HF_TOKEN",
                    }
                },
            }
        )
    revision_arg = f" --revision {hf.revision}" if hf.revision else ""
    command = (
        "set -e; "
        f"{_SKIP_IF_HYDRATED}"
        "pip install --quiet huggingface_hub; "
        f"hf download {hf.repo}{revision_arg} --local-dir {HYDRATION_MOUNT}; "
        f"touch {_HYDRATED_MARKER}"
    )
    return env, command


class FunctionRunner(grpcv1.FunctionRunnerService):
    """A FunctionRunner handles gRPC RunFunctionRequests."""

    def __init__(self):
        self.log = logging.get_logger()

    async def RunFunction(self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext) -> fnv1.RunFunctionResponse:
        log = self.log.bind(tag=req.meta.tag)
        log.info("Running function")
        rsp = response.to(req)
        Composer(req, rsp).compose()
        return rsp


class Composer:
    def __init__(self, req, rsp):
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.ModelCache(**resource.struct_to_dict(req.observed.composite.resource))
        self.clusters: list[icv1alpha1.InferenceCluster] = []

    def compose(self):
        # The XRD can't yet enforce "exactly one source" (no CEL union rule —
        # issue #28), so a ModelCache with an empty/unknown source reaches us.
        # Fail fast with a clear condition rather than NPE in _hf_hydration.
        # This is also the seam where future sources (s3/http/inline) plug in:
        # extend _source_supported() and dispatch in _job_manifest().
        if not self._source_supported():
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_SOURCE_VALID,
                    status="False",
                    reason=CONDITION_REASON_NO_SOURCE,
                    message="spec.source.huggingFace is required (the only v0.1 source)",
                ),
            )
            response.warning(self.rsp, "ModelCache has no supported source set")
            return
        response.set_conditions(
            self.rsp,
            resource.Condition(typ=CONDITION_TYPE_SOURCE_VALID, status="True", reason=CONDITION_REASON_SUPPORTED),
        )
        if not self.resolve_inputs():
            return
        matched = self.match_clusters()
        for cluster in matched:
            self.compose_cluster_resources(cluster)
        per_cluster_phase = [(c.metadata.name, self.derive_cluster_phase(c.metadata.name)) for c in matched]
        self.mark_ready_resources(per_cluster_phase)
        self.write_status(matched, per_cluster_phase)
        self.derive_conditions(matched, per_cluster_phase)
        self.emit_events(matched, per_cluster_phase)

    def _source_supported(self) -> bool:
        """True when the cache declares a source this version implements.

        v0.1 implements only huggingFace. New sources extend this and the
        dispatch in _job_manifest()."""
        return self.xr.spec.source.huggingFace is not None

    def resolve_inputs(self) -> bool:
        """Require all InferenceClusters matching the (optional) selector.

        Returns False when Crossplane hasn't resolved the requirement yet;
        Crossplane re-calls the function once it's available. A resolved-but-
        empty match flows through (match_clusters() -> NoClusters condition).
        """
        match_labels = {LABEL_KEY_CLUSTER: LABEL_VALUE_CLUSTER}
        if self.xr.spec.clusterSelector and self.xr.spec.clusterSelector.matchLabels:
            match_labels.update({k: str(v) for k, v in self.xr.spec.clusterSelector.matchLabels.items()})

        response.require_resources(
            self.rsp,
            name="clusters",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceCluster",
            match_labels=match_labels,
        )

        # get_required_resources returns [] both when unresolved AND when
        # resolved-empty; the requirement key presence is the SDK-blessed way
        # to tell them apart (see crossplane.function.request docstring).
        if "clusters" not in self.req.required_resources:
            return False
        self.clusters = [
            icv1alpha1.InferenceCluster.model_validate(c) for c in request.get_required_resources(self.req, "clusters")
        ]
        return True

    def match_clusters(self) -> list[icv1alpha1.InferenceCluster]:
        """Clusters that have finished provisioning (providerConfigRef set)."""
        return [c for c in self.clusters if c.status and c.status.providerConfigRef and c.status.providerConfigRef.name]

    def compose_cluster_resources(self, cluster: icv1alpha1.InferenceCluster) -> None:
        """Always emit the PVC + Job for a matched cluster (never gate on
        readiness — omitting an Object tells Crossplane to delete it, which
        would re-trigger hydration on every dependency flap)."""
        pc = cluster.status.providerConfigRef.name
        name = cluster.metadata.name
        resource.update(
            self.rsp.desired.resources[self._pvc_key(name)],
            self._wrap_remote(pc, self._pvc_manifest(cluster)),
        )
        resource.update(
            self.rsp.desired.resources[self._job_key(name)],
            self._wrap_remote(pc, self._job_manifest()),
        )

    def _pvc_manifest(self, cluster: icv1alpha1.InferenceCluster) -> dict:
        hf = self.xr.spec.source.huggingFace
        size_gib = int(hf.sizeGiB)  # protobuf delivers XRD ints as float
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": self._pvc_name(), "namespace": REMOTE_NS, "labels": self._labels()},
            "spec": {
                "accessModes": ["ReadWriteMany"],
                "storageClassName": _storage_class(cluster),
                "resources": {"requests": {"storage": f"{size_gib}Gi"}},
            },
        }

    def _wrap_remote(self, provider_config: str, manifest: dict) -> k8sobjv1alpha1.Object:
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

    # --- naming (must stay in sync with backends/base.cache_pvc_name) ---
    # Namespace-qualified so caches of the same name from different Modelplane
    # namespaces don't collide in the workload cluster's `default` namespace
    # (Nic's recurring #99 collision concern).
    def _pvc_name(self) -> str:
        return f"modelcache-{self.xr.metadata.namespace}-{self.xr.metadata.name}"[:63]

    def _job_name(self) -> str:
        return f"{self._pvc_name()}-hydrate"[:63]

    def _pvc_key(self, cluster_name: str) -> str:
        return f"pvc-{cluster_name}"

    def _job_key(self, cluster_name: str) -> str:
        return f"hydrate-{cluster_name}"

    def _labels(self) -> dict[str, str]:
        return {"modelplane.ai/modelcache": self.xr.metadata.name}

    def _job_manifest(self) -> dict:
        env, command = _hf_hydration(self.xr.spec.source.huggingFace)
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": self._job_name(), "namespace": REMOTE_NS, "labels": self._labels()},
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
                                "volumeMounts": [{"name": "artifact", "mountPath": HYDRATION_MOUNT}],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "artifact",
                                "persistentVolumeClaim": {"claimName": self._pvc_name()},
                            }
                        ],
                    },
                },
            },
        }

    def derive_cluster_phase(self, cluster_name: str) -> str:
        pvc_bound = self._observed_status(self._pvc_key(cluster_name)).get("phase") == "Bound"
        job_status = self._observed_status(self._job_key(cluster_name))
        if any(c.get("type") == "Failed" and c.get("status") == "True" for c in job_status.get("conditions", [])):
            return PHASE_FAILED
        if int(job_status.get("succeeded", 0) or 0) >= 1 and pvc_bound:
            return PHASE_READY
        if pvc_bound:
            return PHASE_HYDRATING
        return PHASE_PENDING

    def _observed_status(self, key: str) -> dict:
        """Remote resource status echoed back under Object.status.atProvider.manifest.status."""
        observed = self.req.observed.resources.get(key)
        if not observed:
            return {}
        d = resource.struct_to_dict(observed.resource)
        return d.get("status", {}).get("atProvider", {}).get("manifest", {}).get("status", {}) or {}

    def mark_ready_resources(self, per_cluster_phase) -> None:
        """Mark PVC + Job Objects ready once their cluster reaches Ready.
        Runs after compose_cluster_resources() so the desired entries exist."""
        for name, phase in per_cluster_phase:
            if phase != PHASE_READY:
                continue
            self.rsp.desired.resources[self._pvc_key(name)].ready = fnv1.READY_TRUE
            self.rsp.desired.resources[self._job_key(name)].ready = fnv1.READY_TRUE

    def write_status(self, matched, per_cluster_phase) -> None:
        ready_count = sum(1 for _, p in per_cluster_phase if p == PHASE_READY)
        status = v1alpha1.Status(
            summary=v1alpha1.Summary(ready=f"{ready_count}/{len(matched)}"),
            clusters=[v1alpha1.Cluster(name=n, phase=p) for n, p in per_cluster_phase],
        )
        resource.update_status(self.rsp.desired.composite, status)

    def derive_conditions(self, matched, per_cluster_phase) -> None:
        if not matched:
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_CLUSTERS_MATCHED,
                    status="False",
                    reason=CONDITION_REASON_NO_CLUSTERS,
                ),
                resource.Condition(
                    typ=CONDITION_TYPE_ARTIFACT_READY,
                    status="False",
                    reason=CONDITION_REASON_NO_CLUSTERS,
                ),
            )
            return
        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_CLUSTERS_MATCHED,
                status="True",
                reason=CONDITION_REASON_MATCHED,
            ),
        )
        ready_count = sum(1 for _, p in per_cluster_phase if p == PHASE_READY)
        if ready_count == len(matched):
            response.set_conditions(
                self.rsp,
                resource.Condition(typ=CONDITION_TYPE_ARTIFACT_READY, status="True", reason=CONDITION_REASON_STAGED),
            )
            self.rsp.desired.composite.ready = fnv1.READY_TRUE
        elif ready_count > 0:
            response.set_conditions(
                self.rsp,
                resource.Condition(typ=CONDITION_TYPE_ARTIFACT_READY, status="False", reason=CONDITION_REASON_PARTIAL),
            )
        else:
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_ARTIFACT_READY, status="False", reason=CONDITION_REASON_HYDRATING
                ),
            )

    def emit_events(self, matched, per_cluster_phase) -> None:
        """One-time transition events only (keep `kubectl describe` quiet)."""
        was_ready = resource.get_condition(self.req.observed.composite.resource, "Ready").status == "True"
        now_ready = bool(matched) and all(p == PHASE_READY for _, p in per_cluster_phase)
        observed_keys = self.req.observed.resources.keys()
        first_compose = matched and all(self._pvc_key(c.metadata.name) not in observed_keys for c in matched)
        if first_compose:
            names = ", ".join(c.metadata.name for c in matched)
            response.normal(
                self.rsp,
                f"Staging {self.xr.spec.source.huggingFace.repo} to {len(matched)} clusters: {names}",
            )
        if now_ready and not was_ready:
            response.normal(self.rsp, f"Artifact staged on all {len(matched)} clusters")
