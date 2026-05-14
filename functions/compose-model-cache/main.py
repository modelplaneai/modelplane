"""Compose a ModelCache.

Stages an artifact onto a PVC on every matched InferenceCluster. For
each cluster the function composes a PVC and a one-shot hydration Job
via provider-kubernetes Objects. The Job downloads the artifact (from
HuggingFace or S3) into the PVC; pods that mount the PVC then see the
artifact as a local directory.

v0.1 surface (locked):
- Artifact kinds: Weights, Tokenizer, Bytes
- Sources: huggingFace, s3, http, inline (implemented); oci, configMap
  (discriminator locked, implementation pending)
- Storage backends: PVC, ExistingPVC
- Replication: AllMatchingClusters

Adapter / Engine kinds and ContentAddressed / Custom backends are
deferred to v0.2 per design/modelcache/design.md.

ModelDeployment integration lives in compose-model-replica:
spec.caches: [{ name }] on a ModelDeployment threads through to the
serving stack's model.uri as `pvc://<cache-pvc-name>`, where the PVC
name is derived from lib.naming.modelcache_pvc_name() and matches
the PVC compose-model-cache creates on the workload cluster.
Per-cluster scheduling gates on cache Ready (a future refinement;
v0.1 trusts that the cache hydrates before the engine pod is
scheduled).
"""

from dataclasses import dataclass

from crossplane.function import request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .lib import conditions, metadata, naming
from .lib import resource as libresource
from .model.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from .model.ai.modelplane.modelcache import v1alpha1
from .model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

# Condition types and reasons for the ModelCache XR.
CONDITION_TYPE_CLUSTERS_MATCHED = "ClustersMatched"
CONDITION_TYPE_ARTIFACT_READY = "ArtifactReady"
CONDITION_TYPE_SOURCE_IMPLEMENTED = "SourceImplemented"

CONDITION_REASON_MATCHED = "Matched"
CONDITION_REASON_NO_CLUSTERS = "NoClusters"
CONDITION_REASON_HYDRATING = "Hydrating"
CONDITION_REASON_STAGED = "Staged"
CONDITION_REASON_PARTIAL = "Partial"
CONDITION_REASON_BYO_PVC = "BYOPVC"
CONDITION_REASON_IMPLEMENTATION_PENDING = "ImplementationPending"
CONDITION_REASON_AVAILABLE = "Available"

# Per-cluster phases reported in status.clusters[].phase.
PHASE_PENDING = "Pending"
PHASE_HYDRATING = "Hydrating"
PHASE_READY = "Ready"
PHASE_FAILED = "Failed"

# Storage backend discriminators.
BACKEND_PVC = "PVC"
BACKEND_EXISTING_PVC = "ExistingPVC"

# Source discriminators that the function knows about but hasn't fully
# implemented yet. Surfaced as a clear condition rather than a runtime
# failure so users can see what's missing.
_SOURCES_NOT_YET_IMPLEMENTED = frozenset({"oci", "configMap"})

# Namespace on the remote workload cluster where staging resources land.
REMOTE_NS = metadata.NAMESPACE_REMOTE

# Image used by the hydration Job. Pinned so behavior is reproducible.
# Contains python + pip, which we use to install huggingface-cli or
# awscli at runtime. Real builds should swap for a Modelplane-owned
# image with the tools preinstalled.
HYDRATION_IMAGE = "python:3.11-slim"

# Mount path inside the hydration container. The artifact PVC mounts
# here, and the per-source command writes into it.
HYDRATION_MOUNT = "/mnt/artifact"


@dataclass
class HydrationSpec:
    """The container env and shell command for hydrating one artifact source."""

    env: list[dict]
    command: str


class Composer:
    def __init__(self, req, rsp):
        self.req = req
        self.rsp = rsp
        # struct_to_dict required for Up CLI v0.43+: the protobuf Struct
        # carrying the XR doesn't support attribute access directly.
        self.xr = v1alpha1.ModelCache(**resource.struct_to_dict(req.observed.composite.resource))
        # Populated by resolve_inputs(); empty when Crossplane hasn't yet
        # resolved the required InferenceClusters.
        self.clusters: list[icv1alpha1.InferenceCluster] = []

    def compose(self):
        if not self.resolve_inputs():
            return

        # Fail-fast on sources whose discriminator is locked for v0.1
        # but whose implementation is still pending. Better than silently
        # composing nothing or crashing in the hydration container.
        if self._unimplemented_source():
            self._set_source_unimplemented_condition()
            self.write_status([], [])
            return

        # Source is implemented; record an affirmative condition so the
        # XR status surfaces parity with what the backend will do.
        conditions.set_condition(self.rsp, CONDITION_TYPE_SOURCE_IMPLEMENTED, True, CONDITION_REASON_AVAILABLE)

        matched = self.match_clusters()
        for cluster in matched:
            self.compose_cluster_resources(cluster)

        per_cluster_phase = [(c.metadata.name, self.derive_cluster_phase(c.metadata.name)) for c in matched]

        self.mark_ready_resources(per_cluster_phase)
        self.write_status(matched, per_cluster_phase)
        self.derive_conditions(matched, per_cluster_phase)
        self.emit_events(matched, per_cluster_phase)

    # --------------------------------------------------------------------- #
    # Inputs / matching
    # --------------------------------------------------------------------- #

    def resolve_inputs(self) -> bool:
        """Require all InferenceClusters labeled modelplane.ai/cluster=true.

        Returns False when Crossplane hasn't yet returned the required
        resources; in that case Crossplane will re-call this function.
        """
        # match_labels={} is broken (protobuf drops the empty map). Always
        # include the cluster presence label, then merge in the user's
        # selector.
        match_labels: dict[str, str] = {
            metadata.LABEL_KEY_CLUSTER: metadata.LABEL_VALUE_CLUSTER,
        }
        if self.xr.spec.clusterSelector and self.xr.spec.clusterSelector.matchLabels:
            match_labels.update(dict(self.xr.spec.clusterSelector.matchLabels))

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
        """Filter to clusters with a provisioned providerConfigRef.

        A cluster without a providerConfigRef hasn't finished provisioning
        yet; we can't compose remote resources onto it. The cluster will
        appear in a later reconcile.
        """
        return [c for c in self.clusters if c.status and c.status.providerConfigRef and c.status.providerConfigRef.name]

    # --------------------------------------------------------------------- #
    # Per-cluster composition (always emit; never gate behind readiness)
    # --------------------------------------------------------------------- #

    def compose_cluster_resources(self, cluster: icv1alpha1.InferenceCluster) -> None:
        """Compose the per-cluster Objects.

        For backend=PVC: a PVC + a hydration Job. For backend=ExistingPVC:
        nothing — the customer owns the PVC and the bytes; we only
        report on per-cluster readiness in status.

        Both Objects are always emitted once we know about the cluster —
        omitting an Object from desired state tells Crossplane to delete
        it, which would cause the hydration to redo on every dependency
        flap.
        """
        if self.xr.spec.storage.backend == BACKEND_EXISTING_PVC:
            # Customer-managed PVC: nothing to compose. Per-cluster phase
            # is derived in derive_cluster_phase().
            return

        pc_name = cluster.status.providerConfigRef.name
        cluster_name = cluster.metadata.name

        resource.update(
            self.rsp.desired.resources[self._pvc_key(cluster_name)],
            self._wrap_remote(pc_name, self._pvc_manifest()),
        )
        resource.update(
            self.rsp.desired.resources[self._job_key(cluster_name)],
            self._wrap_remote(pc_name, self._job_manifest()),
        )

    def _pvc_manifest(self) -> dict:
        """PVC for the artifact on the workload cluster."""
        pvc = self.xr.spec.storage.pvc
        # Protobuf delivers XRD numbers as float; cast for K8s Quantity.
        size_gib = int(pvc.sizeGiB)
        access_mode = pvc.accessMode or "ReadWriteMany"
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": self._pvc_name(),
                "namespace": REMOTE_NS,
                "labels": self._labels(),
            },
            "spec": {
                "accessModes": [access_mode],
                "storageClassName": pvc.storageClassName,
                "resources": {"requests": {"storage": f"{size_gib}Gi"}},
            },
        }

    def _job_manifest(self) -> dict:
        """One-shot Job that hydrates the PVC from the artifact source."""
        spec = self._build_hydration_spec()
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": self._job_name(),
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
                                "command": ["/bin/sh", "-c", spec.command],
                                "env": spec.env,
                                "volumeMounts": [
                                    {"name": "artifact", "mountPath": HYDRATION_MOUNT},
                                ],
                            },
                        ],
                        "volumes": [
                            {
                                "name": "artifact",
                                "persistentVolumeClaim": {"claimName": self._pvc_name()},
                            },
                        ],
                    },
                },
            },
        }

    def _wrap_remote(self, provider_config: str, manifest: dict) -> k8sobjv1alpha1.Object:
        """Wrap a manifest as a provider-kubernetes Object on a remote cluster."""
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

    # --------------------------------------------------------------------- #
    # Hydration: per-source command/env builders
    # --------------------------------------------------------------------- #

    def _build_hydration_spec(self) -> HydrationSpec:
        """Dispatch on the artifact source to the right hydration builder.

        Sources in _SOURCES_NOT_YET_IMPLEMENTED are filtered out earlier
        by _unimplemented_source(); they don't reach this method.
        """
        src = self.xr.spec.artifact.source
        if src.huggingFace:
            return _hf_hydration(src.huggingFace)
        if src.s3:
            return _s3_hydration(src.s3)
        if src.http:
            return _http_hydration(src.http)
        if src.inline:
            return _inline_hydration(src.inline)
        # XRD validation should prevent this; surface a clear error in
        # logs rather than crashing the container loop.
        return HydrationSpec(env=[], command=_FAIL_NO_SOURCE)

    def _unimplemented_source(self) -> str | None:
        """Return the source-field name if it's locked but not implemented."""
        src = self.xr.spec.artifact.source
        for field in _SOURCES_NOT_YET_IMPLEMENTED:
            if getattr(src, field, None) is not None:
                return field
        return None

    def _set_source_unimplemented_condition(self) -> None:
        """Surface a clear condition when a v0.1 source isn't implemented yet."""
        field = self._unimplemented_source()
        msg = f"Source `{field}` is part of v0.1 but not yet implemented"
        conditions.set_condition(
            self.rsp, CONDITION_TYPE_SOURCE_IMPLEMENTED, False, CONDITION_REASON_IMPLEMENTATION_PENDING, msg
        )
        conditions.set_condition(
            self.rsp, CONDITION_TYPE_ARTIFACT_READY, False, CONDITION_REASON_IMPLEMENTATION_PENDING, msg
        )
        response.warning(self.rsp, msg)

    # --------------------------------------------------------------------- #
    # Phase derivation (read-only over observed state)
    # --------------------------------------------------------------------- #

    def derive_cluster_phase(self, cluster_name: str) -> str:
        """Map observed PVC + Job state to a per-cluster phase.

        For backend=ExistingPVC we trust the customer-managed PVC and
        report Ready immediately on every matched cluster. A future
        refinement could check the PVC actually exists on the workload
        cluster via a remote read; for v0.1 the surface assumes the
        customer's pipeline has populated the claim.
        """
        if self.xr.spec.storage.backend == BACKEND_EXISTING_PVC:
            return PHASE_READY

        pvc_key = self._pvc_key(cluster_name)
        job_key = self._job_key(cluster_name)

        pvc_ready = conditions.has_condition(self.req, pvc_key, "Ready")
        if self._job_failed(job_key):
            return PHASE_FAILED
        if self._job_complete(job_key) and pvc_ready:
            return PHASE_READY
        if pvc_ready:
            return PHASE_HYDRATING
        return PHASE_PENDING

    def _job_complete(self, job_key: str) -> bool:
        """Check the remote Job has succeeded.

        provider-kubernetes echoes the remote resource's status back under
        Object.status.atProvider.manifest.status; a succeeded Job has
        succeeded >= 1.
        """
        manifest_status = self._observed_remote_status(job_key)
        return int(manifest_status.get("succeeded", 0) or 0) >= 1

    def _job_failed(self, job_key: str) -> bool:
        """Check the remote Job has hit its backoffLimit (status.conditions Failed=True)."""
        manifest_status = self._observed_remote_status(job_key)
        for c in manifest_status.get("conditions", []):
            if c.get("type") == "Failed" and c.get("status") == "True":
                return True
        return False

    def _observed_remote_status(self, key: str) -> dict:
        """Extract status from a provider-kubernetes Object's observed state."""
        observed = self.req.observed.resources.get(key)
        if not observed:
            return {}
        d = resource.struct_to_dict(observed.resource)
        return d.get("status", {}).get("atProvider", {}).get("manifest", {}).get("status", {}) or {}

    # --------------------------------------------------------------------- #
    # Output: readiness, status, conditions, events
    # --------------------------------------------------------------------- #

    def mark_ready_resources(self, per_cluster_phase: list[tuple[str, str]]) -> None:
        """Mark PVC + Job Objects ready=True once their cluster reaches Ready phase.

        Marking ready AFTER resource.update() — calling update() resets
        the ready flag, so the order matters.
        """
        for cluster_name, phase in per_cluster_phase:
            if phase != PHASE_READY:
                continue
            self.rsp.desired.resources[self._pvc_key(cluster_name)].ready = fnv1.READY_TRUE
            self.rsp.desired.resources[self._job_key(cluster_name)].ready = fnv1.READY_TRUE

    def write_status(self, matched, per_cluster_phase) -> None:
        """Populate status.summary, status.clusters, status.lastHydratedAt."""
        ready_count = sum(1 for _, p in per_cluster_phase if p == PHASE_READY)
        total = len(matched)
        clusters_status = [v1alpha1.Cluster(name=name, phase=phase) for name, phase in per_cluster_phase]
        status = v1alpha1.Status(
            summary=v1alpha1.Summary(ready=f"{ready_count}/{total}"),
            clusters=clusters_status,
        )
        last_hydrated = self._latest_completion_time(per_cluster_phase)
        if last_hydrated:
            status.lastHydratedAt = last_hydrated
        libresource.update_status(self.rsp.desired.composite, status)

    def _latest_completion_time(self, per_cluster_phase) -> str | None:
        """Most recent Job completionTime across all clusters.

        provider-kubernetes surfaces the remote Job's completionTime in
        atProvider.manifest.status.completionTime as an RFC3339 string.
        For backend=ExistingPVC there are no Jobs; we leave the field
        unset (the customer's pipeline owns hydration timing).
        """
        if self.xr.spec.storage.backend == BACKEND_EXISTING_PVC:
            return None
        times = []
        for cluster_name, phase in per_cluster_phase:
            if phase != PHASE_READY:
                continue
            manifest_status = self._observed_remote_status(self._job_key(cluster_name))
            ct = manifest_status.get("completionTime")
            if ct:
                times.append(ct)
        return max(times) if times else None

    def derive_conditions(self, matched, per_cluster_phase) -> None:
        if not matched:
            conditions.set_condition(self.rsp, CONDITION_TYPE_CLUSTERS_MATCHED, False, CONDITION_REASON_NO_CLUSTERS)
            conditions.set_condition(self.rsp, CONDITION_TYPE_ARTIFACT_READY, False, CONDITION_REASON_NO_CLUSTERS)
            return

        conditions.set_condition(self.rsp, CONDITION_TYPE_CLUSTERS_MATCHED, True, CONDITION_REASON_MATCHED)

        ready_count = sum(1 for _, p in per_cluster_phase if p == PHASE_READY)
        existing_pvc = self.xr.spec.storage.backend == BACKEND_EXISTING_PVC
        if ready_count == len(matched):
            reason = CONDITION_REASON_BYO_PVC if existing_pvc else CONDITION_REASON_STAGED
            conditions.set_condition(self.rsp, CONDITION_TYPE_ARTIFACT_READY, True, reason)
            self.rsp.desired.composite.ready = fnv1.READY_TRUE
        elif ready_count > 0:
            conditions.set_condition(self.rsp, CONDITION_TYPE_ARTIFACT_READY, False, CONDITION_REASON_PARTIAL)
        else:
            conditions.set_condition(self.rsp, CONDITION_TYPE_ARTIFACT_READY, False, CONDITION_REASON_HYDRATING)

    def emit_events(self, matched, per_cluster_phase) -> None:
        """Emit one-time transition events on state changes.

        Skip steady-state events to keep `kubectl describe` quiet — only
        emit on first matching of clusters and on first full readiness.
        For backend=ExistingPVC the cache is Ready immediately and we
        emit a single "Adopted" event instead of a hydration milestone.
        """
        existing_pvc = self.xr.spec.storage.backend == BACKEND_EXISTING_PVC
        was_ready = resource.get_condition(self.req.observed.composite.resource, "Ready").status == "True"
        now_ready = matched and all(p == PHASE_READY for _, p in per_cluster_phase)

        if existing_pvc:
            if matched and not was_ready:
                response.normal(
                    self.rsp,
                    f"Adopted existing PVC {self.xr.spec.storage.existingPVC.claimName} on {len(matched)} clusters",
                )
            return

        observed_keys = self.req.observed.resources.keys()
        first_compose = all(self._pvc_key(c.metadata.name) not in observed_keys for c in matched)
        if first_compose and matched:
            names = ", ".join(c.metadata.name for c in matched)
            response.normal(self.rsp, f"Staging {self.xr.spec.artifact.kind} to {len(matched)} clusters: {names}")

        if now_ready and not was_ready:
            response.normal(self.rsp, f"Artifact staged on all {len(matched)} clusters")

    # --------------------------------------------------------------------- #
    # Naming helpers
    # --------------------------------------------------------------------- #

    def _pvc_name(self) -> str:
        return naming.modelcache_pvc_name(self.xr.metadata.name)

    def _job_name(self) -> str:
        return f"{naming.modelcache_pvc_name(self.xr.metadata.name)}-hydrate"

    def _pvc_key(self, cluster_name: str) -> str:
        return f"pvc-{cluster_name}"

    def _job_key(self, cluster_name: str) -> str:
        return f"hydrate-{cluster_name}"

    def _labels(self) -> dict[str, str]:
        return {"modelplane.ai/modelcache": self.xr.metadata.name}


# ------------------------------------------------------------------------- #
# Per-source hydration builders (module-level — pure functions of spec)
# ------------------------------------------------------------------------- #

# Short-circuits if the PVC already has content, so a Job rerun (after
# eviction, replay, or backoff) doesn't redownload.
_SKIP_IF_HYDRATED = (
    f'if [ -n "$(ls -A {HYDRATION_MOUNT} 2>/dev/null)" ]; then '
    "  echo 'artifact already hydrated, skipping'; exit 0; "
    "fi; "
)

_FAIL_NO_SOURCE = "echo 'no artifact source set on ModelCache' >&2; exit 1"


def _hf_hydration(hf) -> HydrationSpec:
    """Build env + command for a HuggingFace artifact source."""
    env: list[dict] = []
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
    revision_arg = f" --revision {hf.revision}" if hf.revision else ""
    command = (
        "set -e; "
        f"{_SKIP_IF_HYDRATED}"
        "pip install --quiet 'huggingface_hub[cli]'; "
        f"huggingface-cli download {hf.repo}{revision_arg} --local-dir {HYDRATION_MOUNT}"
    )
    return HydrationSpec(env=env, command=command)


def _s3_hydration(s3) -> HydrationSpec:
    """Build env + command for an S3 artifact source."""
    env: list[dict] = []
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
    command = f"set -e; {_SKIP_IF_HYDRATED}pip install --quiet awscli; aws s3 sync {s3.uri} {HYDRATION_MOUNT}"
    return HydrationSpec(env=env, command=command)


def _http_hydration(http) -> HydrationSpec:
    """Build env + command for a generic HTTP(S) source.

    Downloads the URL to a single file inside the PVC. For multi-file
    artifacts use `huggingFace`, `s3`, or (when implemented) `oci`.
    """
    env: list[dict] = []
    auth_arg = ""
    if http.authSecretRef:
        env.append(
            {
                "name": "AUTH_HEADER",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": http.authSecretRef.name,
                        "key": http.authSecretRef.key or "Authorization",
                    },
                },
            },
        )
        # Inject the header only when AUTH_HEADER is non-empty.
        auth_arg = '${AUTH_HEADER:+-H "Authorization: ${AUTH_HEADER}"} '
    command = (
        f"set -e; {_SKIP_IF_HYDRATED}"
        "apt-get update -qq && apt-get install -y -qq curl >/dev/null; "
        f'curl -fsSL {auth_arg}-o {HYDRATION_MOUNT}/artifact "{http.url}"'
    )
    return HydrationSpec(env=env, command=command)


def _inline_hydration(inline) -> HydrationSpec:
    """Build env + command for an inline content source.

    Writes the literal content to a single file inside the PVC. Content
    travels as an env var so shell escaping isn't an issue.
    """
    filename = inline.filename or "artifact"
    env: list[dict] = [{"name": "INLINE_CONTENT", "value": inline.content}]
    command = f'set -e; {_SKIP_IF_HYDRATED}printf "%s" "$INLINE_CONTENT" > {HYDRATION_MOUNT}/{filename}'
    return HydrationSpec(env=env, command=command)


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose PVC + hydration Job per matched cluster."""
    Composer(req, rsp).compose()
