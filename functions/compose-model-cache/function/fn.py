# Copyright 2026 The Modelplane Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Compose a ModelCache.

Stages a model onto a ReadWriteMany PVC on every matched InferenceCluster
via a one-shot hydration Job. Pods that reference the cache
(ModelDeployment.spec.modelCacheRef -> ModelReplica) mount the PVC at
/mnt/models, so weights are fetched once per cluster and read N times by
every pod in an LWS gang.

spec.source picks where the artifact comes from. HuggingFace downloads a
repo with huggingface_hub, which writes a flat model directory.  OCI pulls
a ModelPack artifact from a registry with modctl, which restores whatever
file layout the artifact was built with. Both land on the same mount and
an engine reads them the same way, by path.
"""

from typing import Literal

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelcache import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


def _name(meta: metav1.ObjectMeta | None) -> str:
    """The object's name, always set on resources read from the API server."""
    if meta is None or meta.name is None:
        raise ValueError("metadata.name is unexpectedly absent")
    return meta.name


def _namespace(meta: metav1.ObjectMeta | None) -> str:
    """The object's namespace, always set on namespaced resources read from the API server."""
    if meta is None or meta.namespace is None:
        raise ValueError("metadata.namespace is unexpectedly absent")
    return meta.namespace


# Condition types/reasons for the ModelCache XR.
CONDITION_TYPE_CLUSTERS_MATCHED = "ClustersMatched"
CONDITION_TYPE_ARTIFACT_READY = "ArtifactReady"

CONDITION_REASON_MATCHED = "Matched"
CONDITION_REASON_NO_CLUSTERS = "NoClusters"
CONDITION_REASON_HYDRATING = "Hydrating"
CONDITION_REASON_STAGED = "Staged"
CONDITION_REASON_PARTIAL = "Partial"
CONDITION_REASON_FAILED = "Failed"
CONDITION_REASON_AUTH_SECRET_MISSING = "AuthSecretMissing"

# CEL readiness queries: each wrapped Object derives its own Ready condition
# from the remote resource's status (DeriveFromCelQuery), so mark_ready_resources
# can lean on the Object Ready condition instead of re-parsing status.
_PVC_READY_CEL = 'object.status.phase == "Bound"'
_JOB_READY_CEL = 'object.status.conditions.exists(c, c.type == "Complete" && c.status == "True")'

# Per-cluster phases reported in status.clusters[].phase.
_Phase = Literal["Pending", "Hydrating", "Ready", "Failed"]
PHASE_PENDING: _Phase = "Pending"
PHASE_HYDRATING: _Phase = "Hydrating"
PHASE_READY: _Phase = "Ready"
PHASE_FAILED: _Phase = "Failed"

# Namespace on the workload cluster where the PVC + Job land. Must match the
# namespace the serving pods mount from (native.py/llmd.py `_REMOTE_NAMESPACE`,
# also "default"): a pod can only mount a PVC in its own namespace. The two
# functions set this independently, so they are a contract — change together.
REMOTE_NS = "default"

# Hydration containers, one per source, each fetching its tool at runtime:
# huggingface_hub from PyPI, and the modctl release tarball over HTTP. A
# Modelplane-owned image with both preinstalled is a follow-up (#115), and is
# what would let either source hydrate in an air-gapped cluster - neither can
# today, because both reach out before they reach the artifact.
HYDRATION_IMAGE = "python:3.11-slim"
OCI_HYDRATION_IMAGE = "alpine:3.20"
HYDRATION_MOUNT = "/mnt/artifact"

# modctl publishes release tarballs rather than an image, so the Job downloads
# one. Pinned: the artifactType and layer media types it writes are still the
# pre-donation `vnd.cnai.*` names rather than the `vnd.cncf.*` ones in the
# ModelPack spec, so a version bump can change what an artifact looks like.
_MODCTL_VERSION = "0.2.2"

# Where the propagated registry credential is projected for an OCI source.
# modctl looks for `config.json` under DOCKER_CONFIG, while a
# kubernetes.io/dockerconfigjson Secret stores it under `.dockerconfigjson`, so
# the volume renames the key on the way in.
_REGISTRY_AUTH_MOUNT = "/etc/modelplane/registry"

# spec.source values, and the key a kubernetes.io/dockerconfigjson Secret stores
# its payload under.
_SOURCE_OCI = "OCI"
_DOCKERCONFIG_KEY = ".dockerconfigjson"

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
_ManagementPolicy = Literal["Observe", "Create", "Update", "Delete", "LateInitialize", "*"]
_JOB_MANAGEMENT: list[_ManagementPolicy] = ["Observe", "Create", "Update", "LateInitialize"]


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


def _hf_hydration(hf: v1alpha1.HuggingFace, auth_secret_name: str | None) -> tuple[list[dict], str]:
    """Return (env, shell command) for a HuggingFace source.

    Uses `hf download` (huggingface_hub 1.x; `huggingface-cli` is removed).
    The marker is touched only after a successful download (set -e aborts the
    chain on failure, so a failed pull never marks the cache complete).

    When the cache references an authSecret, HF_TOKEN is read from the
    propagated workload-cluster Secret (auth_secret_name), not the user's
    control-plane Secret name - the two live on different clusters.
    """
    env: list[dict] = []
    if hf.authSecret:
        env.append(
            {
                "name": "HF_TOKEN",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": auth_secret_name,
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


def _oci_hydration(oci: v1alpha1.Oci) -> tuple[list[dict], str]:
    """Return (env, shell command) for an OCI source.

    `modctl pull --extract-from-remote` streams the artifact from the registry
    and `--extract-dir` writes its files straight onto the mount, so nothing
    stages a second copy in the container filesystem.

    What lands is the layout the artifact was *built* with: modctl records a
    filepath annotation per descriptor and extract restores those paths,
    subdirectories included. A `modctl build` over a model directory therefore
    yields that same flat directory here, not a HuggingFace hub cache tree, so an
    engine reads this mount by path rather than by repo id. Synthesizing a hub
    layout from the manifest's model name is a follow-up; the artifact does not
    carry one.

    A registry credential arrives as the propagated dockerconfigjson Secret,
    projected to the filename modctl's auth lookup expects and pointed at with
    DOCKER_CONFIG.
    """
    env: list[dict] = []
    if oci.pullSecret:
        env.append({"name": "DOCKER_CONFIG", "value": _REGISTRY_AUTH_MOUNT})
    url = f"https://github.com/modelpack/modctl/releases/download/v{_MODCTL_VERSION}/modctl-{_MODCTL_VERSION}-linux-$a.tar.gz"
    command = (
        "set -e; "
        f"{_SKIP_IF_HYDRATED}"
        'case "$(uname -m)" in aarch64|arm64) a=arm64;; *) a=amd64;; esac; '
        "apk add --no-cache curl tar >/dev/null; "
        f"curl -sSfL {url} | tar -xz -C /tmp modctl; "
        f"/tmp/modctl pull {oci.ref} --extract-dir {HYDRATION_MOUNT} --extract-from-remote --no-progress; "
        f"touch {_HYDRATED_MARKER}"
    )
    return env, command


class FunctionRunner(grpcv1.FunctionRunnerServiceServicer):
    """A FunctionRunner handles gRPC RunFunctionRequests."""

    def __init__(self) -> None:
        self.log = logging.get_logger()

    async def RunFunction(
        self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext | None
    ) -> fnv1.RunFunctionResponse:  # ty: ignore[invalid-method-override]  # the generated grpc servicer base is untyped
        log = self.log.bind(tag=req.meta.tag)
        log.info("Running function")
        rsp = response.to(req)
        Composer(req, rsp).compose()
        return rsp


class Composer:
    def __init__(self, req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse) -> None:
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.ModelCache(**resource.struct_to_dict(req.observed.composite.resource))
        self.clusters: list[icv1alpha1.InferenceCluster] = []
        # The referenced authSecret key -> its base64 token value, read from the
        # control-plane Secret. Populated by resolve_inputs() once the XR
        # references an authSecret and that key is present; empty otherwise.
        self.auth_data: dict[str, str] = {}

    # --- source dispatch ---
    # Every difference between sources is one of four things: the credential the
    # Job needs, the capacity for the PVC, the hydration command, and the label
    # for an event. Each is answered here so the composition below reads the same
    # whichever source is set. A CEL rule on the XRD guarantees the sibling
    # matching spec.source is present, so each branch can assume its own.

    def _is_oci(self) -> bool:
        return self.xr.spec.source == _SOURCE_OCI

    def _credential(self) -> tuple[str, str, str] | None:
        """(field, Secret name, key) of the credential this source references on
        the control plane, or None when it needs none. The field is carried so a
        warning names what the user actually wrote - `pullSecret` for OCI,
        `authSecret` for HuggingFace - rather than a generic word for both. The
        OCI key is fixed by the dockerconfigjson Secret type; the HuggingFace one
        is user-selectable."""
        if self._is_oci():
            oci = self.xr.spec.oci
            assert oci is not None
            return ("pullSecret", oci.pullSecret.name, _DOCKERCONFIG_KEY) if oci.pullSecret else None
        hf = self.xr.spec.huggingFace
        assert hf is not None
        return ("authSecret", hf.authSecret.name, hf.authSecret.key or "HF_TOKEN") if hf.authSecret else None

    def _size_gib(self) -> int:
        """Capacity for the PVC. Declared per source because a source that
        stages nothing would have nothing to size."""
        source = self.xr.spec.oci if self._is_oci() else self.xr.spec.huggingFace
        assert source is not None
        # Protobuf delivers XRD integers as float.
        return int(source.sizeGiB)

    def _artifact(self) -> str:
        """What this cache stages, for events and messages."""
        if self._is_oci():
            assert self.xr.spec.oci is not None
            return self.xr.spec.oci.ref
        assert self.xr.spec.huggingFace is not None
        return self.xr.spec.huggingFace.repo

    def _auth_missing(self) -> bool:
        """Whether the XR references a credential that couldn't be resolved.
        compose() only runs past resolve_inputs() once the requirement (if any)
        is resolved, so empty auth_data here means the Secret was
        found-but-empty or absent, not merely unresolved."""
        return self._credential() is not None and not self.auth_data

    def compose(self) -> None:
        if not self.resolve_inputs():
            return
        matched = self.match_clusters()
        # Derive each cluster's phase first (from observed state), then compose:
        # a hydrated cluster's Job is composed Observe-only so Crossplane doesn't
        # recreate it after the TTL controller cleans it.
        per_cluster_phase: list[tuple[str, _Phase]] = [
            (_name(c.metadata), self.derive_cluster_phase(_name(c.metadata))) for c in matched
        ]
        phase_by_name = dict(per_cluster_phase)
        for cluster in matched:
            self.compose_cluster_resources(cluster, phase_by_name[_name(cluster.metadata)])
        self.mark_ready_resources(per_cluster_phase)
        self.write_status(matched, per_cluster_phase)
        self.derive_conditions(matched, per_cluster_phase)
        self.emit_events(matched, per_cluster_phase)

    def resolve_inputs(self) -> bool:
        """Require the InferenceClusters and (if set) the authSecret.

        Returns False when Crossplane hasn't resolved a requirement yet;
        Crossplane re-calls the function once it's available. A resolved-but-
        empty cluster match flows through (match_clusters() -> NoClusters
        condition).
        """
        # Require everything up front so Crossplane resolves the requirements in
        # parallel and re-calls us once they're available.

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

        # When the source references a credential, require that Secret from the
        # XR's own namespace on the control plane. It is propagated to each
        # workload cluster (compose_cluster_resources) so the hydration Job finds
        # it; without resolving it first we can't materialize it remotely.
        credential = self._credential()
        if credential:
            response.require_resources(
                self.rsp,
                name="auth-secret",
                api_version="v1",
                kind="Secret",
                match_name=credential[1],
                namespace=_namespace(self.xr.metadata),
            )

        # get_required_resources returns [] both when unresolved AND when
        # resolved-empty; the requirement key presence is the SDK-blessed way
        # to tell them apart (see crossplane.function.request docstring). Wait
        # for both to resolve before composing.
        if "clusters" not in self.req.required_resources:
            return False
        if credential and "auth-secret" not in self.req.required_resources:
            return False
        self.clusters = [
            icv1alpha1.InferenceCluster.model_validate(c) for c in request.get_required_resources(self.req, "clusters")
        ]

        # Resolve the token best-effort: a missing one doesn't block the PVC,
        # only the hydration Job and token Secret (see _resolve_auth_data).
        if credential:
            self._resolve_auth_data(credential[2], request.get_required_resource(self.req, "auth-secret"))

        return True

    def _resolve_auth_data(self, key: str, secret: dict | None) -> None:
        """Read the credential from the resolved control-plane Secret into
        self.auth_data, copying its base64 `data` verbatim (re-encoding would
        corrupt it).

        Best-effort: the caller proceeds either way. A resolved Secret that's
        missing, or whose referenced key is absent or empty, leaves auth_data
        empty (an empty value is as broken as a missing key - the Job would run
        with an empty credential). That gates the hydration Job and the
        propagated Secret out while leaving the PVC - which doesn't depend on the
        credential - to compose, so an already-staged cache isn't pruned when its
        token is later rotated away. derive_conditions surfaces the
        misconfiguration only when it actually blocks progress."""
        data = (secret.get("data") if secret else {}) or {}
        if data.get(key):
            self.auth_data = {key: data[key]}

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

    def compose_cluster_resources(self, cluster: icv1alpha1.InferenceCluster, phase: _Phase) -> None:
        """Compose the PVC always, and the hydration Job until the cluster is Ready.

        Once Ready the Job is dropped: with _JOB_MANAGEMENT (no Delete) that
        orphans the external Job rather than deleting it, so its own TTL
        controller cascade-cleans the Job and the completed pod pinning the PVC -
        instead of Crossplane orphaning the pod. Readiness is latched in the XR
        status, so dropping the Job doesn't regress it, and a flap that re-adds
        it is a cheap idempotent skip."""
        # match_clusters only returns clusters whose status.providerConfigRef.name
        # is set, so this is never None here.
        assert cluster.status and cluster.status.providerConfigRef and cluster.status.providerConfigRef.name
        pc = cluster.status.providerConfigRef.name
        name = _name(cluster.metadata)
        resource.update(
            self.rsp.desired.resources[self._pvc_key(name)],
            self._wrap_remote(pc, self._pvc_manifest(cluster), _PVC_READY_CEL),
        )
        # The PVC (above) composes regardless of auth: it doesn't depend on the
        # token, so a cache whose token is later rotated away keeps its staged
        # weights. The hydration Job and its token Secret need the token, so
        # they're held back until it's available - composing the Job against an
        # absent Secret would just fail its pod.
        if phase != PHASE_READY and not self._auth_missing():
            # The token Secret is composed before the Job that consumes it, and
            # dropped alongside the Job once the cluster is Ready. Unlike the Job
            # (orphaned via _JOB_MANAGEMENT, then TTL-cleaned), the Secret keeps
            # default management policies, so dropping it DELETES it from the
            # inference cluster. That's deliberate: the token is only needed
            # while hydrating, so removing it afterwards limits its exposure. A
            # flap back to hydrating re-composes it, and the provider re-creates
            # it before the Job pod reads it. The Secret has no status, so it
            # uses default readiness (Ready once synced).
            if self.auth_data:
                resource.update(
                    self.rsp.desired.resources[self._auth_key(name)],
                    self._wrap_remote(pc, self._auth_secret_manifest()),
                )
            resource.update(
                self.rsp.desired.resources[self._job_key(name)],
                self._wrap_remote(pc, self._job_manifest(), _JOB_READY_CEL, management_policies=_JOB_MANAGEMENT),
            )

    def _pvc_manifest(self, cluster: icv1alpha1.InferenceCluster) -> dict:
        size_gib = self._size_gib()
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

    def _auth_secret_manifest(self) -> dict:
        """The workload-cluster Secret carrying the propagated credential.

        Namespace-qualified name in REMOTE_NS, matching the PVC/Job, so caches
        from different control-plane namespaces don't collide. `data` carries the
        referenced key with its base64 value copied verbatim - the hydration Job
        reads that same key, as an env var for HuggingFace or as a projected file
        for OCI.

        An OCI credential is typed kubernetes.io/dockerconfigjson so the kubelet
        validates the payload it projects, rather than leaving a malformed
        registry config to surface as an auth failure inside the Job."""
        manifest: dict = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": self._auth_secret_name(), "namespace": REMOTE_NS, "labels": self._labels()},
            "data": self.auth_data,
        }
        if self._is_oci():
            manifest["type"] = "kubernetes.io/dockerconfigjson"
        return manifest

    def _wrap_remote(
        self,
        provider_config: str,
        manifest: dict,
        cel_query: str | None = None,
        management_policies: list[_ManagementPolicy] | None = None,
    ) -> k8sobjv1alpha1.Object:
        spec = k8sobjv1alpha1.Spec(
            providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                kind="ClusterProviderConfig",
                name=provider_config,
            ),
            forProvider=k8sobjv1alpha1.ForProvider(manifest=manifest),
        )
        # A CEL query derives Ready from the wrapped resource's status; resources
        # without a meaningful status (the Secret) omit it and use the provider's
        # default readiness (Ready once the Object is synced).
        if cel_query is not None:
            spec.readiness = k8sobjv1alpha1.Readiness(policy="DeriveFromCelQuery", celQuery=cel_query)
        if management_policies is not None:
            spec.managementPolicies = management_policies
        return k8sobjv1alpha1.Object(spec=spec)

    # --- naming (must stay in sync with backends/base.cache_pvc_name) ---
    # Both sides share resource.child_name("modelcache", namespace, name).
    # Namespace-qualified so same-named caches from different Modelplane
    # namespaces don't collide in the workload cluster's `default` namespace.
    def _pvc_name(self) -> str:
        return resource.child_name("modelcache", _namespace(self.xr.metadata), _name(self.xr.metadata))

    def _job_name(self) -> str:
        return resource.child_name("modelcache", _namespace(self.xr.metadata), _name(self.xr.metadata), "hydrate")

    def _auth_secret_name(self) -> str:
        return resource.child_name("modelcache", _namespace(self.xr.metadata), _name(self.xr.metadata), "auth")

    def _pvc_key(self, cluster_name: str) -> str:
        return f"pvc-{cluster_name}"

    def _job_key(self, cluster_name: str) -> str:
        return f"hydrate-{cluster_name}"

    def _auth_key(self, cluster_name: str) -> str:
        return f"auth-{cluster_name}"

    def _labels(self) -> dict[str, str]:
        return {"modelplane.ai/modelcache": _name(self.xr.metadata)}

    def _job_manifest(self) -> dict:
        """The one-shot hydration Job. Everything source-specific is the tool it
        runs and how it reads its credential; the Job around them is the same."""
        mounts = [{"name": "artifact", "mountPath": HYDRATION_MOUNT}]
        volumes: list[dict] = [{"name": "artifact", "persistentVolumeClaim": {"claimName": self._pvc_name()}}]
        if self._is_oci():
            oci = self.xr.spec.oci
            assert oci is not None
            image, (env, command) = OCI_HYDRATION_IMAGE, _oci_hydration(oci)
            if self.auth_data:
                # Rename the dockerconfigjson key to the filename modctl reads,
                # so DOCKER_CONFIG can point at the directory.
                mounts.append({"name": "registry-auth", "mountPath": _REGISTRY_AUTH_MOUNT, "readOnly": True})
                volumes.append(
                    {
                        "name": "registry-auth",
                        "secret": {
                            "secretName": self._auth_secret_name(),
                            "items": [{"key": _DOCKERCONFIG_KEY, "path": "config.json"}],
                        },
                    }
                )
        else:
            hf = self.xr.spec.huggingFace
            assert hf is not None
            image, (env, command) = HYDRATION_IMAGE, _hf_hydration(hf, self._auth_secret_name())
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
                                "image": image,
                                "command": ["/bin/sh", "-c", command],
                                "env": env,
                                "volumeMounts": mounts,
                            }
                        ],
                        "volumes": volumes,
                    },
                },
            },
        }

    def derive_cluster_phase(self, cluster_name: str) -> _Phase:
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

    def mark_ready_resources(self, per_cluster_phase: list[tuple[str, _Phase]]) -> None:
        """Mark composed Objects ready from each Object's own Ready condition.

        The PVC and Job carry a DeriveFromCelQuery readiness policy, so the
        wrapped resource's Ready condition (PVC Bound, Job Complete) is reflected
        onto the observed Object; the auth Secret uses default readiness (Ready
        once synced). Runs after compose_cluster_resources() so the desired
        entries exist."""
        for name, phase in per_cluster_phase:
            # Mirror compose_cluster_resources: only mark the keys it composed.
            # The Job and token Secret are composed until Ready (then dropped),
            # and held back entirely while the token is missing. Marking a key we
            # didn't compose would create a phantom entry.
            keys = [self._pvc_key(name)]
            if phase != PHASE_READY and not self._auth_missing():
                keys.append(self._job_key(name))
                if self.auth_data:
                    keys.append(self._auth_key(name))
            for key in keys:
                observed = self.req.observed.resources.get(key)
                if observed and resource.get_condition(observed.resource, "Ready").status == "True":
                    self.rsp.desired.resources[key].ready = fnv1.READY_TRUE

    def write_status(
        self, matched: list[icv1alpha1.InferenceCluster], per_cluster_phase: list[tuple[str, _Phase]]
    ) -> None:
        ready_count = sum(1 for _, p in per_cluster_phase if p == PHASE_READY)
        status = v1alpha1.Status(
            summary=v1alpha1.Summary(ready=f"{ready_count}/{len(matched)}"),
            clusters=[v1alpha1.Cluster(name=n, phase=p) for n, p in per_cluster_phase],
        )
        resource.update_status(self.rsp.desired.composite, status)

    def derive_conditions(
        self, matched: list[icv1alpha1.InferenceCluster], per_cluster_phase: list[tuple[str, _Phase]]
    ) -> None:
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
        # A missing token holds back the Job and token Secret, so a cluster that
        # isn't already Ready can't make progress. Report that over the
        # phase-derived reason, which would otherwise just say Hydrating without
        # explaining why nothing is happening, and warn naming the Secret and
        # key. A cache that's already fully Ready doesn't need the token, so a
        # token rotated away after hydration is neither reported nor warned.
        ready_count = sum(1 for _, p in per_cluster_phase if p == PHASE_READY)
        if self._auth_missing() and ready_count != len(matched):
            # _auth_missing is true only when the source references a credential.
            credential = self._credential()
            assert credential is not None
            field, name, key = credential
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_ARTIFACT_READY,
                    status="False",
                    reason=CONDITION_REASON_AUTH_SECRET_MISSING,
                ),
            )
            response.warning(
                self.rsp,
                f"{field} {_namespace(self.xr.metadata)}/{name} is missing or has no key {key!r}",
            )
        elif any(p == PHASE_FAILED for _, p in per_cluster_phase):
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

    def emit_events(
        self, matched: list[icv1alpha1.InferenceCluster], per_cluster_phase: list[tuple[str, _Phase]]
    ) -> None:
        """One-time transition events only (keep `kubectl describe` quiet)."""
        was_ready = resource.get_condition(self.req.observed.composite.resource, "Ready").status == "True"
        now_ready = bool(matched) and all(p == PHASE_READY for _, p in per_cluster_phase)
        observed_keys = self.req.observed.resources.keys()
        first_compose = matched and all(self._pvc_key(_name(c.metadata)) not in observed_keys for c in matched)
        if first_compose:
            names = ", ".join(_name(c.metadata) for c in matched)
            response.normal(
                self.rsp,
                f"Staging {self._artifact()} to {len(matched)} clusters: {names}",
            )
        if now_ready and not was_ready:
            response.normal(self.rsp, f"Artifact staged on all {len(matched)} clusters")
