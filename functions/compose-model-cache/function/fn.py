"""Compose a ModelCache.

Stages a HuggingFace model onto a ReadWriteMany PVC on every matched
InferenceCluster via a one-shot hydration Job. Pods that reference the
cache (ModelDeployment.spec.modelCacheRef -> ModelReplica) mount the PVC
at /mnt/models, so weights are downloaded once per cluster and read N
times by every pod in an LWS gang.
"""

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelcache import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

# Condition types/reasons for the ModelCache XR.
CONDITION_TYPE_CLUSTERS_MATCHED = "ClustersMatched"
CONDITION_TYPE_ARTIFACT_READY = "ArtifactReady"

CONDITION_REASON_MATCHED = "Matched"
CONDITION_REASON_NO_CLUSTERS = "NoClusters"
CONDITION_REASON_HYDRATING = "Hydrating"
CONDITION_REASON_STAGED = "Staged"
CONDITION_REASON_PARTIAL = "Partial"
CONDITION_REASON_FAILED = "Failed"

# CEL readiness queries: each wrapped Object derives its own Ready condition
# from the remote resource's status (DeriveFromCelQuery), so mark_ready_resources
# can lean on the Object Ready condition instead of re-parsing status.
_PVC_READY_CEL = 'object.status.phase == "Bound"'
_JOB_READY_CEL = 'object.status.conditions.exists(c, c.type == "Complete" && c.status == "True")'

# Per-cluster phases reported in status.clusters[].phase.
PHASE_PENDING = "Pending"
PHASE_HYDRATING = "Hydrating"
PHASE_READY = "Ready"
PHASE_FAILED = "Failed"

# Namespace on the workload cluster where the PVC + Job land. Must match the
# namespace the serving pods mount from (native.py/llmd.py `_REMOTE_NAMESPACE`,
# also "default"): a pod can only mount a PVC in its own namespace. The two
# functions set this independently, so they are a contract — change together.
REMOTE_NS = "default"

# Hydration container. python:3.11-slim has pip; we install huggingface_hub
# at runtime. A Modelplane-owned image with the tool preinstalled is a
# follow-up (#115).
HYDRATION_IMAGE = "python:3.11-slim"
HYDRATION_MOUNT = "/mnt/artifact"

# ttlSecondsAfterFinished governs how long the completed Job (and its
# PVC-pinning pod) lingers before its TTL controller cascade-deletes both. Keep
# it short so a just-deleted cache's PVC isn't pinned for long - but above
# provider-kubernetes' observe interval, so the function still catches the Job's
# success (to latch Ready and drop the Job) before the TTL fires.
_JOB_TTL_SECONDS = 180

# Every management policy except Delete. Once a cluster is Ready the Job is
# dropped from the composition; without Delete, dropping orphans the external Job
# instead of deleting it, so its own TTL controller cascade-cleans the completed
# pod that pins the PVC - whereas Crossplane's delete would orphan that pod. (No
# "all but Delete" shorthand exists, and deletionPolicy: Orphan is ignored once
# managementPolicies is on.) Re-adding the Job after a flap is a cheap skip.
_JOB_MANAGEMENT = ["Observe", "Create", "Update", "LateInitialize"]


def _storage_class(cluster: icv1alpha1.InferenceCluster) -> str | None:
    """RWX storage class for the cache PVC, from the cluster's
    status.cache.storageClassName. The InferenceCluster reports the
    Modelplane-managed class for provisioned (GKE/EKS) clusters and the
    user-supplied class for Existing clusters. None until the cluster reports
    it - the cache gates on it, so a PVC never references an undecided class."""
    if cluster.status and cluster.status.cache and cluster.status.cache.storageClassName:
        return cluster.status.cache.storageClassName
    return None


# A completion marker written only after a fully successful download. The Job
# A completion marker, checked instead of directory emptiness. A re-run
# (eviction, replay, backoff) skips when the marker is present. Checking the
# marker — not a non-empty dir — keeps re-runs safe: an interrupted download
# leaves files but no marker, so the retry resumes (`hf download` is resumable)
# instead of concluding "already hydrated" and serving truncated weights. It
# also avoids the Filestore `lost+found` dir at the ext4 mount root tripping an
# emptiness check.
_HYDRATED_MARKER = f"{HYDRATION_MOUNT}/.modelplane-hydrated"
_SKIP_IF_HYDRATED = f"if [ -f {_HYDRATED_MARKER} ]; then echo 'already hydrated, skipping'; exit 0; fi; "


def _hf_hydration(hf) -> tuple[list[dict], str]:
    """Return (env, shell command) for a HuggingFace source.

    Uses `hf download` (huggingface_hub 1.x; `huggingface-cli` is removed).
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
        if not self.resolve_inputs():
            return
        matched = self.match_clusters()
        # Derive each cluster's phase first (from observed state), then compose:
        # a hydrated cluster's Job is composed Observe-only so Crossplane doesn't
        # recreate it after the TTL controller cleans it.
        per_cluster_phase = [(c.metadata.name, self.derive_cluster_phase(c.metadata.name)) for c in matched]
        phase_by_name = dict(per_cluster_phase)
        for cluster in matched:
            self.compose_cluster_resources(cluster, phase_by_name[cluster.metadata.name])
        self.mark_ready_resources(per_cluster_phase)
        self.write_status(matched, per_cluster_phase)
        self.derive_conditions(matched, per_cluster_phase)
        self.emit_events(matched, per_cluster_phase)

    def resolve_inputs(self) -> bool:
        """Require all InferenceClusters matching the (optional) selector.

        Returns False when Crossplane hasn't resolved the requirement yet;
        Crossplane re-calls the function once it's available. A resolved-but-
        empty match flows through (match_clusters() -> NoClusters condition).
        """
        # require_resources with no match field matches every InferenceCluster;
        # narrow only when the user sets a clusterSelector.
        match_labels = None
        if self.xr.spec.clusterSelector and self.xr.spec.clusterSelector.matchLabels:
            match_labels = dict(self.xr.spec.clusterSelector.matchLabels)
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
        """Clusters ready to cache onto: provisioned (providerConfigRef set)
        and reporting an effective RWX StorageClass (status.cache). Gating on
        the StorageClass means the cache PVC never references a class the
        cluster hasn't decided on yet."""
        return [
            c
            for c in self.clusters
            if c.status and c.status.providerConfigRef and c.status.providerConfigRef.name and _storage_class(c)
        ]

    def compose_cluster_resources(self, cluster: icv1alpha1.InferenceCluster, phase: str) -> None:
        """Compose the PVC always, and the hydration Job until the cluster is Ready.

        Once Ready the Job is dropped: with _JOB_MANAGEMENT (no Delete) that
        orphans the external Job rather than deleting it, so its own TTL
        controller cascade-cleans the Job and the completed pod pinning the PVC -
        instead of Crossplane orphaning the pod. Readiness is latched in the XR
        status, so dropping the Job doesn't regress it, and a flap that re-adds
        it is a cheap idempotent skip."""
        pc = cluster.status.providerConfigRef.name
        name = cluster.metadata.name
        resource.update(
            self.rsp.desired.resources[self._pvc_key(name)],
            self._wrap_remote(pc, self._pvc_manifest(cluster), _PVC_READY_CEL),
        )
        if phase != PHASE_READY:
            resource.update(
                self.rsp.desired.resources[self._job_key(name)],
                self._wrap_remote(pc, self._job_manifest(), _JOB_READY_CEL, management_policies=_JOB_MANAGEMENT),
            )

    def _pvc_manifest(self, cluster: icv1alpha1.InferenceCluster) -> dict:
        hf = self.xr.spec.huggingFace
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

    def _wrap_remote(
        self,
        provider_config: str,
        manifest: dict,
        cel_query: str,
        management_policies: list[str] | None = None,
    ) -> k8sobjv1alpha1.Object:
        spec = k8sobjv1alpha1.Spec(
            providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                kind="ClusterProviderConfig",
                name=provider_config,
            ),
            readiness=k8sobjv1alpha1.Readiness(policy="DeriveFromCelQuery", celQuery=cel_query),
            forProvider=k8sobjv1alpha1.ForProvider(manifest=manifest),
        )
        if management_policies is not None:
            spec.managementPolicies = management_policies
        return k8sobjv1alpha1.Object(spec=spec)

    # --- naming (must stay in sync with backends/base.cache_pvc_name) ---
    # Both sides share resource.child_name("modelcache", namespace, name).
    # Namespace-qualified so same-named caches from different Modelplane
    # namespaces don't collide in the workload cluster's `default` namespace.
    def _pvc_name(self) -> str:
        return resource.child_name("modelcache", self.xr.metadata.namespace, self.xr.metadata.name)

    def _job_name(self) -> str:
        return resource.child_name("modelcache", self.xr.metadata.namespace, self.xr.metadata.name, "hydrate")

    def _pvc_key(self, cluster_name: str) -> str:
        return f"pvc-{cluster_name}"

    def _job_key(self, cluster_name: str) -> str:
        return f"hydrate-{cluster_name}"

    def _labels(self) -> dict[str, str]:
        return {"modelplane.ai/modelcache": self.xr.metadata.name}

    def _job_manifest(self) -> dict:
        env, command = _hf_hydration(self.xr.spec.huggingFace)
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": self._job_name(), "namespace": REMOTE_NS, "labels": self._labels()},
            "spec": {
                "backoffLimit": 3,
                "ttlSecondsAfterFinished": _JOB_TTL_SECONDS,
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
        # Latch Ready: a hydrated cluster stays Ready even once the Job (and its
        # completed, PVC-pinning pod) has been cleaned and no longer reports its
        # Complete condition. Reading the prior phase from the observed XR status
        # keeps readiness stable across that cleanup. Ready still requires the PVC
        # to be Bound, so if the PVC is lost the cache drops back to Pending
        # rather than reporting a stale Ready.
        job_complete = any(
            c.get("type") == "Complete" and c.get("status") == "True" for c in job_status.get("conditions", [])
        )
        hydrated = job_complete or self._was_ready(cluster_name)
        if pvc_bound and hydrated:
            return PHASE_READY
        if pvc_bound:
            return PHASE_HYDRATING
        return PHASE_PENDING

    def _was_ready(self, cluster_name: str) -> bool:
        """Whether the previous reconcile already reported this cluster Ready,
        read from the observed XR status."""
        status = self.xr.status
        if not status or not status.clusters:
            return False
        return any(c.name == cluster_name and c.phase == PHASE_READY for c in status.clusters)

    def _observed_status(self, key: str) -> dict:
        """Remote resource status echoed back under Object.status.atProvider.manifest.status."""
        observed = self.req.observed.resources.get(key)
        if not observed:
            return {}
        obj = k8sobjv1alpha1.Object.model_validate(resource.struct_to_dict(observed.resource))
        manifest = (obj.status.atProvider.manifest if obj.status and obj.status.atProvider else None) or {}
        return manifest.get("status", {}) or {}

    def mark_ready_resources(self, per_cluster_phase) -> None:
        """Mark PVC + Job Objects ready from each Object's own Ready condition.

        The Objects carry a DeriveFromCelQuery readiness policy, so the wrapped
        PVC/Job's Ready condition (PVC Bound, Job Complete) is reflected onto the
        observed Object. Runs after compose_cluster_resources() so the desired
        entries exist."""
        for name, phase in per_cluster_phase:
            # The Job is only composed until Ready (then dropped), so don't mark
            # a key we didn't compose - accessing it would create a phantom entry.
            keys = [self._pvc_key(name)]
            if phase != PHASE_READY:
                keys.append(self._job_key(name))
            for key in keys:
                observed = self.req.observed.resources.get(key)
                if observed and resource.get_condition(observed.resource, "Ready").status == "True":
                    self.rsp.desired.resources[key].ready = fnv1.READY_TRUE

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
        if any(p == PHASE_FAILED for _, p in per_cluster_phase):
            response.set_conditions(
                self.rsp,
                resource.Condition(typ=CONDITION_TYPE_ARTIFACT_READY, status="False", reason=CONDITION_REASON_FAILED),
            )
        elif ready_count == len(matched):
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
                f"Staging {self.xr.spec.huggingFace.repo} to {len(matched)} clusters: {names}",
            )
        if now_ready and not was_ready:
            response.normal(self.rsp, f"Artifact staged on all {len(matched)} clusters")
