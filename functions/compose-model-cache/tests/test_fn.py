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

"""Tests for the compose-model-cache function."""

import dataclasses
import datetime
import unittest
from typing import Any

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.modelcache import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# A fixed transition time keeps observed conditions deterministic.
_TRANSITION_TIME = datetime.datetime(2026, 6, 8, tzinfo=datetime.UTC)


@dataclasses.dataclass
class Case:
    """A test case for compose-model-cache."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


# The XR used across cases: a HuggingFace ModelCache in the ml-team namespace.
# Both the PVC and Job derive their names from
# resource.child_name("modelcache", "ml-team", "qwen", ...).
def _cache_xr(**hf_extra: Any) -> v1alpha1.ModelCache:
    return v1alpha1.ModelCache(
        metadata=metav1.ObjectMeta(name="qwen", namespace="ml-team"),
        spec=v1alpha1.Spec(
            source="HuggingFace",
            huggingFace=v1alpha1.HuggingFace(repo="Qwen/Qwen3-0.6B", sizeGiB=20, **hf_extra),
        ),
    )


def _cluster_dict(name: str, pc: str, *, source: str = "GKE", storage_class: str | None = None) -> dict:
    """An InferenceCluster as Crossplane returns it in a required-resource set.

    The cache PVC's StorageClass comes from status.cache.storageClassName, which
    the InferenceCluster relays from its backing cluster. The match gate requires
    both providerConfigRef AND status.cache, so every matchable fixture reports
    one. Defaults to the source's effective class (GKE -> modelplane-rwx,
    EKS -> modelplane-rwx-efs) unless overridden.
    """
    blocks = {
        "GKE": {"gke": {"project": "my-project", "region": "us-central1"}},
        "EKS": {"eks": {"region": "us-west-2"}},
    }
    if storage_class is None:
        storage_class = "modelplane-rwx-efs" if source == "EKS" else "modelplane-rwx"
    return {
        "apiVersion": "modelplane.ai/v1alpha1",
        "kind": "InferenceCluster",
        "metadata": {"name": name},
        "spec": {"cluster": {"source": source, **blocks[source]}},
        "status": {
            "providerConfigRef": {"name": pc},
            "cache": {"storageClassName": storage_class},
        },
    }


def _observed_object(manifest_status: dict, *, ready: bool = False) -> dict:
    """A full Object envelope as provider-kubernetes observes it.

    Carries the echoed remote status under status.atProvider.manifest.status
    (read by derive_cluster_phase) and, when `ready`, the Object's own Ready
    condition (read by mark_ready_resources, populated by DeriveFromCelQuery).
    """
    status: dict[str, Any] = {"atProvider": {"manifest": {"status": manifest_status}}}
    if ready:
        status["conditions"] = [
            {
                "type": "Ready",
                "status": "True",
                "reason": "Available",
                "lastTransitionTime": "2026-06-08T00:00:00Z",
            },
        ]
    return {
        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
        "kind": "Object",
        "spec": {"forProvider": {"manifest": {}}},
        "status": status,
    }


def _auth_secret(*, data: dict[str, str] | None = None) -> dict:
    """The control-plane authSecret as Crossplane returns it in a required-
    resource set: a core/v1 Secret with base64 `data`."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "hf-token", "namespace": "ml-team"},
        "type": "Opaque",
        "data": {"HF_TOKEN": _TOKEN_B64} if data is None else data,
    }


def _req(
    xr: v1alpha1.ModelCache,
    clusters: list[dict],
    observed: dict[str, dict] | None = None,
    auth: dict | None = None,
) -> fnv1.RunFunctionRequest:
    """Build a request the way the repo's other function tests do.

    - XR goes in observed.composite via dict_to_struct(model_dump(mode="json")).
    - Resolved clusters go in the `clusters` required-resource set.
    - `auth`, when given, is the resolved control-plane Secret in the
      `auth-secret` required-resource set.
    - `observed` maps a desired-resource key -> an observed Object envelope.
    """
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(
                resource=resource.dict_to_struct(xr.model_dump(exclude_none=True, mode="json")),
            ),
        ),
    )
    # Touch the key so a resolved-but-empty match (clusters == []) is present
    # with no items, the way Crossplane returns it - distinct from an unresolved
    # requirement, whose key is absent.
    req.required_resources["clusters"].items.extend(
        fnv1.Resource(resource=resource.dict_to_struct(c)) for c in clusters
    )
    if auth is not None:
        req.required_resources["auth-secret"].items.append(
            fnv1.Resource(resource=resource.dict_to_struct(auth)),
        )
    for key, obj in (observed or {}).items():
        req.observed.resources[key].resource.update(obj)
    return req


# The function requires every InferenceCluster with a bare selector, so every
# response echoes this selector under requirements.
_CLUSTERS_SELECTOR = fnv1.ResourceSelector(
    api_version="modelplane.ai/v1alpha1",
    kind="InferenceCluster",
)

# When the cache references an authSecret, the function requires that Secret by
# name from the XR's namespace; the response echoes this selector.
_AUTH_SELECTOR = fnv1.ResourceSelector(
    api_version="v1",
    kind="Secret",
    match_name="hf-token",
    namespace="ml-team",
)

# The hydration shell script the Job runs (no revision, no auth secret).
_HYDRATE_CMD = (
    "set -e; if [ -f /mnt/artifact/.modelplane-hydrated ]; then echo 'already hydrated, skipping'; exit 0; fi; "
    "pip install --quiet huggingface_hub; hf download Qwen/Qwen3-0.6B --local-dir /mnt/artifact; "
    "touch /mnt/artifact/.modelplane-hydrated"
)
# With a pinned revision (case 2 wires --revision and the HF_TOKEN env).
_HYDRATE_CMD_REVISION = (
    "set -e; if [ -f /mnt/artifact/.modelplane-hydrated ]; then echo 'already hydrated, skipping'; exit 0; fi; "
    "pip install --quiet huggingface_hub; hf download Qwen/Qwen3-0.6B --revision main --local-dir /mnt/artifact; "
    "touch /mnt/artifact/.modelplane-hydrated"
)

_PVC_NAME = "modelcache-ml-team-qwen-17db2"
_JOB_NAME = "modelcache-ml-team-qwen-hydrate-256ec"
_AUTH_NAME = "modelcache-ml-team-qwen-auth-ae01b"
_LABELS = {"modelplane.ai/modelcache": "qwen"}

# The token data the control-plane authSecret carries, base64 as the API server
# stores it. Propagated verbatim into the workload-cluster Secret's data.
_TOKEN_B64 = "aGYtdG9rZW4tdmFsdWU="


def _pvc_object(pc: str, *, storage_class: str = "modelplane-rwx") -> dict:
    return {
        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
        "kind": "Object",
        "spec": {
            "forProvider": {
                "manifest": {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "metadata": {"name": _PVC_NAME, "namespace": "default", "labels": _LABELS},
                    "spec": {
                        "accessModes": ["ReadWriteMany"],
                        "resources": {"requests": {"storage": "20Gi"}},
                        "storageClassName": storage_class,
                    },
                },
            },
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": pc},
            "readiness": {"celQuery": 'object.status.phase == "Bound"', "policy": "DeriveFromCelQuery"},
        },
    }


def _job_object(pc: str, *, command: str = _HYDRATE_CMD, env: list | None = None) -> dict:
    return {
        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
        "kind": "Object",
        "spec": {
            "forProvider": {
                "manifest": {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": _JOB_NAME, "namespace": "default", "labels": _LABELS},
                    "spec": {
                        "backoffLimit": 3,
                        "ttlSecondsAfterFinished": 180,
                        "template": {
                            "metadata": {"labels": _LABELS},
                            "spec": {
                                "restartPolicy": "OnFailure",
                                "containers": [
                                    {
                                        "name": "hydrate",
                                        "image": "python:3.11-slim",
                                        "command": ["/bin/sh", "-c", command],
                                        "env": env or [],
                                        "volumeMounts": [{"name": "artifact", "mountPath": "/mnt/artifact"}],
                                    },
                                ],
                                "volumes": [
                                    {"name": "artifact", "persistentVolumeClaim": {"claimName": _PVC_NAME}},
                                ],
                            },
                        },
                    },
                },
            },
            "managementPolicies": ["Observe", "Create", "Update", "LateInitialize"],
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": pc},
            "readiness": {
                "celQuery": 'object.status.conditions.exists(c, c.type == "Complete" && c.status == "True")',
                "policy": "DeriveFromCelQuery",
            },
        },
    }


def _auth_object(pc: str) -> dict:
    """The workload-cluster Secret Object propagating the HF token. No readiness
    block: a Secret has no status, so the Object uses default readiness."""
    return {
        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
        "kind": "Object",
        "spec": {
            "forProvider": {
                "manifest": {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": _AUTH_NAME, "namespace": "default", "labels": _LABELS},
                    "data": {"HF_TOKEN": _TOKEN_B64},
                },
            },
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": pc},
        },
    }


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    """Tests for FunctionRunner.RunFunction."""

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:  # noqa: PLR0915
        """The function composes a ModelCache."""
        # --- Case 1: GKE cluster, first pass. Composes the RWX PVC + hydration
        # Job per matched cluster; nothing observed yet so phase is Pending and
        # ArtifactReady is Hydrating. Emits the one-time "Staging" event. ---
        want1 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "status": {
                                "summary": {"ready": "0/1"},
                                "clusters": [{"name": "cluster-a", "phase": "Pending"}],
                            },
                        },
                    ),
                ),
                resources={
                    "pvc-cluster-a": fnv1.Resource(resource=resource.dict_to_struct(_pvc_object("cluster-a-pc"))),
                    "hydrate-cluster-a": fnv1.Resource(resource=resource.dict_to_struct(_job_object("cluster-a-pc"))),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Hydrating"),
            ],
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Staging Qwen/Qwen3-0.6B to 1 clusters: cluster-a",
                ),
            ],
            context=structpb.Struct(),
        )
        want1.requirements.resources["clusters"].CopyFrom(_CLUSTERS_SELECTOR)

        # --- Case 2: GKE cluster with a pinned revision + auth secret. The Job
        # command gains --revision, and the function propagates the token to a
        # workload-cluster Secret (auth-cluster-a) whose name the Job's HF_TOKEN
        # env references - not the user's control-plane Secret name. ---
        xr2 = _cache_xr(revision="main", authSecret=v1alpha1.AuthSecret(name="hf-token"))
        env2 = [
            {
                "name": "HF_TOKEN",
                "valueFrom": {"secretKeyRef": {"name": _AUTH_NAME, "key": "HF_TOKEN"}},
            },
        ]
        want2 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "status": {
                                "summary": {"ready": "0/1"},
                                "clusters": [{"name": "cluster-a", "phase": "Pending"}],
                            },
                        },
                    ),
                ),
                resources={
                    "auth-cluster-a": fnv1.Resource(resource=resource.dict_to_struct(_auth_object("cluster-a-pc"))),
                    "pvc-cluster-a": fnv1.Resource(resource=resource.dict_to_struct(_pvc_object("cluster-a-pc"))),
                    "hydrate-cluster-a": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            _job_object("cluster-a-pc", command=_HYDRATE_CMD_REVISION, env=env2),
                        ),
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Hydrating"),
            ],
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Staging Qwen/Qwen3-0.6B to 1 clusters: cluster-a",
                ),
            ],
            context=structpb.Struct(),
        )
        want2.requirements.resources["clusters"].CopyFrom(_CLUSTERS_SELECTOR)
        want2.requirements.resources["auth-secret"].CopyFrom(_AUTH_SELECTOR)

        # --- Case 3: EKS cluster reporting an EFS RWX class on status.cache. The
        # PVC sources its storageClassName from status.cache (modelplane-rwx-efs),
        # not the GKE/Filestore one. ---
        want3 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "status": {
                                "summary": {"ready": "0/1"},
                                "clusters": [{"name": "eks-a", "phase": "Pending"}],
                            },
                        },
                    ),
                ),
                resources={
                    "pvc-eks-a": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            _pvc_object("eks-a-pc", storage_class="modelplane-rwx-efs"),
                        ),
                    ),
                    "hydrate-eks-a": fnv1.Resource(resource=resource.dict_to_struct(_job_object("eks-a-pc"))),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Hydrating"),
            ],
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Staging Qwen/Qwen3-0.6B to 1 clusters: eks-a",
                ),
            ],
            context=structpb.Struct(),
        )
        want3.requirements.resources["clusters"].CopyFrom(_CLUSTERS_SELECTOR)

        # --- Case 4: ready. The observed PVC + Job Objects each carry their own
        # Ready condition (from DeriveFromCelQuery), and the wrapped manifest
        # status shows PVC Bound + Job succeeded. Phase Ready, both Objects
        # marked ready, summary 1/1, XR ready, ArtifactReady Staged. The
        # already-composed PVC suppresses the "Staging" event; the
        # not-previously-ready -> ready transition emits the "staged" event. ---
        observed4 = {
            "pvc-cluster-a": _observed_object({"phase": "Bound"}, ready=True),
            "hydrate-cluster-a": _observed_object({"conditions": [{"type": "Complete", "status": "True"}]}, ready=True),
        }
        pvc_ready = _pvc_object("cluster-a-pc")
        want4 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "status": {
                                "summary": {"ready": "1/1"},
                                "clusters": [{"name": "cluster-a", "phase": "Ready"}],
                            },
                        },
                    ),
                    ready=fnv1.READY_TRUE,
                ),
                resources={
                    # Job dropped once Ready; only the PVC remains composed.
                    "pvc-cluster-a": fnv1.Resource(resource=resource.dict_to_struct(pvc_ready), ready=fnv1.READY_TRUE),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_TRUE, reason="Staged"),
            ],
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Artifact staged on all 1 clusters"),
            ],
            context=structpb.Struct(),
        )
        want4.requirements.resources["clusters"].CopyFrom(_CLUSTERS_SELECTOR)

        # --- Case 5: hydrating. PVC Bound (Object Ready) but the Job hasn't
        # completed, so phase is Hydrating, only the PVC is marked ready, summary
        # 0/1, and the XR is not ready. No transition event fires. ---
        observed5 = {"pvc-cluster-a": _observed_object({"phase": "Bound"}, ready=True)}
        want5 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "status": {
                                "summary": {"ready": "0/1"},
                                "clusters": [{"name": "cluster-a", "phase": "Hydrating"}],
                            },
                        },
                    ),
                ),
                resources={
                    "pvc-cluster-a": fnv1.Resource(
                        resource=resource.dict_to_struct(_pvc_object("cluster-a-pc")), ready=fnv1.READY_TRUE
                    ),
                    "hydrate-cluster-a": fnv1.Resource(resource=resource.dict_to_struct(_job_object("cluster-a-pc"))),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Hydrating"),
            ],
            context=structpb.Struct(),
        )
        want5.requirements.resources["clusters"].CopyFrom(_CLUSTERS_SELECTOR)

        # --- Case 6: failed. The Job reports a Failed condition (and is NOT
        # Ready). A Failed Job takes precedence over PVC binding, so phase is
        # Failed, only the PVC is marked ready, summary 0/1, XR not ready, and
        # ArtifactReady is False with reason Failed. ---
        observed6 = {
            "pvc-cluster-a": _observed_object({"phase": "Bound"}, ready=True),
            "hydrate-cluster-a": _observed_object({"conditions": [{"type": "Failed", "status": "True"}]}),
        }
        want6 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "status": {
                                "summary": {"ready": "0/1"},
                                "clusters": [{"name": "cluster-a", "phase": "Failed"}],
                            },
                        },
                    ),
                ),
                resources={
                    "pvc-cluster-a": fnv1.Resource(
                        resource=resource.dict_to_struct(_pvc_object("cluster-a-pc")), ready=fnv1.READY_TRUE
                    ),
                    "hydrate-cluster-a": fnv1.Resource(resource=resource.dict_to_struct(_job_object("cluster-a-pc"))),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Failed"),
            ],
            context=structpb.Struct(),
        )
        want6.requirements.resources["clusters"].CopyFrom(_CLUSTERS_SELECTOR)

        # --- Case 7: partial (1/2). Cluster a is Ready (PVC Bound + Job
        # succeeded), cluster b is still Hydrating (PVC Bound only). Summary
        # 1/2, ArtifactReady False with reason Partial, XR not ready. ---
        observed7 = {
            "pvc-a": _observed_object({"phase": "Bound"}, ready=True),
            "hydrate-a": _observed_object({"conditions": [{"type": "Complete", "status": "True"}]}, ready=True),
            "pvc-b": _observed_object({"phase": "Bound"}, ready=True),
        }
        want7 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "status": {
                                "summary": {"ready": "1/2"},
                                "clusters": [
                                    {"name": "a", "phase": "Ready"},
                                    {"name": "b", "phase": "Hydrating"},
                                ],
                            },
                        },
                    ),
                ),
                resources={
                    # Cluster a is Ready, so its Job is dropped; b is still Hydrating.
                    "pvc-a": fnv1.Resource(
                        resource=resource.dict_to_struct(_pvc_object("a-pc")), ready=fnv1.READY_TRUE
                    ),
                    "pvc-b": fnv1.Resource(
                        resource=resource.dict_to_struct(_pvc_object("b-pc")), ready=fnv1.READY_TRUE
                    ),
                    "hydrate-b": fnv1.Resource(resource=resource.dict_to_struct(_job_object("b-pc"))),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Partial"),
            ],
            context=structpb.Struct(),
        )
        want7.requirements.resources["clusters"].CopyFrom(_CLUSTERS_SELECTOR)

        # --- Case 8: latch. A previously-Ready cluster whose hydration Job was
        # dropped (and TTL-cleaned), so only the PVC is observed now. The status
        # latch keeps phase Ready and the Job is not re-composed, PVC marked
        # ready, summary 1/1, XR ready. Already-ready, so no transition event. ---
        xr_ready = _cache_xr()
        xr_ready.status = v1alpha1.Status(
            summary=v1alpha1.Summary(ready="1/1"),
            clusters=[v1alpha1.Cluster(name="cluster-a", phase="Ready")],
            conditions=[
                v1alpha1.Condition(
                    type="Ready",
                    status="True",
                    reason="Available",
                    lastTransitionTime=_TRANSITION_TIME,
                ),
            ],
        )
        observed8 = {"pvc-cluster-a": _observed_object({"phase": "Bound"}, ready=True)}
        want8 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "status": {
                                "summary": {"ready": "1/1"},
                                "clusters": [{"name": "cluster-a", "phase": "Ready"}],
                            },
                        },
                    ),
                    ready=fnv1.READY_TRUE,
                ),
                resources={
                    # Latched Ready with the Job already dropped: only the PVC.
                    "pvc-cluster-a": fnv1.Resource(
                        resource=resource.dict_to_struct(_pvc_object("cluster-a-pc")), ready=fnv1.READY_TRUE
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_TRUE, reason="Staged"),
            ],
            context=structpb.Struct(),
        )
        want8.requirements.resources["clusters"].CopyFrom(_CLUSTERS_SELECTOR)

        # --- Case 9: authSecret referenced but not yet resolved. The function
        # requires both the clusters and the auth Secret, then returns early
        # (no resources, status, or conditions) until Crossplane resolves the
        # Secret and re-calls it. ---
        xr9 = _cache_xr(authSecret=v1alpha1.AuthSecret(name="hf-token"))
        want9 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            context=structpb.Struct(),
        )
        want9.requirements.resources["clusters"].CopyFrom(_CLUSTERS_SELECTOR)
        want9.requirements.resources["auth-secret"].CopyFrom(_AUTH_SELECTOR)

        # --- Case 10: authSecret resolved but the Secret lacks the referenced
        # key (here it carries OTHER, not HF_TOKEN). The PVC still composes - it
        # doesn't depend on the token, so a cache isn't pruned for a missing one
        # - but the hydration Job and token Secret are held back. ArtifactReady
        # is False with reason AuthSecretMissing, and a warning names the Secret
        # and key so the user can fix it instead of seeing the XR stall. ---
        want10 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "status": {
                                "summary": {"ready": "0/1"},
                                "clusters": [{"name": "cluster-a", "phase": "Pending"}],
                            },
                        },
                    ),
                ),
                resources={
                    "pvc-cluster-a": fnv1.Resource(resource=resource.dict_to_struct(_pvc_object("cluster-a-pc"))),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_FALSE, reason="AuthSecretMissing"),
            ],
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message="authSecret ml-team/hf-token is missing or has no key 'HF_TOKEN'",
                ),
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Staging Qwen/Qwen3-0.6B to 1 clusters: cluster-a",
                ),
            ],
            context=structpb.Struct(),
        )
        want10.requirements.resources["clusters"].CopyFrom(_CLUSTERS_SELECTOR)
        want10.requirements.resources["auth-secret"].CopyFrom(_AUTH_SELECTOR)

        # --- Case 11: Ready cluster with an authSecret. The token is only needed
        # while hydrating, so once the cluster is Ready the auth Secret is dropped
        # alongside the Job (only the PVC remains composed), even though the
        # control-plane Secret still resolves. Keeps the token from lingering on
        # the inference cluster after hydration. ---
        xr11 = _cache_xr(authSecret=v1alpha1.AuthSecret(name="hf-token"))
        observed11 = {
            "auth-cluster-a": _observed_object({}, ready=True),
            "pvc-cluster-a": _observed_object({"phase": "Bound"}, ready=True),
            "hydrate-cluster-a": _observed_object({"conditions": [{"type": "Complete", "status": "True"}]}, ready=True),
        }
        want11 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "status": {
                                "summary": {"ready": "1/1"},
                                "clusters": [{"name": "cluster-a", "phase": "Ready"}],
                            },
                        },
                    ),
                    ready=fnv1.READY_TRUE,
                ),
                resources={
                    # Ready: the auth Secret and Job are both dropped, only the PVC remains.
                    "pvc-cluster-a": fnv1.Resource(
                        resource=resource.dict_to_struct(_pvc_object("cluster-a-pc")), ready=fnv1.READY_TRUE
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_TRUE, reason="Staged"),
            ],
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Artifact staged on all 1 clusters"),
            ],
            context=structpb.Struct(),
        )
        want11.requirements.resources["clusters"].CopyFrom(_CLUSTERS_SELECTOR)
        want11.requirements.resources["auth-secret"].CopyFrom(_AUTH_SELECTOR)

        # --- Case 12: token rotated away after a cache is Ready. A latched-Ready
        # cluster whose authSecret now resolves without the key. The PVC keeps
        # composing (and stays Ready via the status latch) rather than being
        # pruned, and because hydration is already done the missing token is
        # neither reported (ArtifactReady stays Staged) nor warned. ---
        xr12 = _cache_xr(authSecret=v1alpha1.AuthSecret(name="hf-token"))
        xr12.status = v1alpha1.Status(
            summary=v1alpha1.Summary(ready="1/1"),
            clusters=[v1alpha1.Cluster(name="cluster-a", phase="Ready")],
            conditions=[
                v1alpha1.Condition(
                    type="Ready",
                    status="True",
                    reason="Available",
                    lastTransitionTime=_TRANSITION_TIME,
                ),
            ],
        )
        observed12 = {"pvc-cluster-a": _observed_object({"phase": "Bound"}, ready=True)}
        want12 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "status": {
                                "summary": {"ready": "1/1"},
                                "clusters": [{"name": "cluster-a", "phase": "Ready"}],
                            },
                        },
                    ),
                    ready=fnv1.READY_TRUE,
                ),
                resources={
                    "pvc-cluster-a": fnv1.Resource(
                        resource=resource.dict_to_struct(_pvc_object("cluster-a-pc")), ready=fnv1.READY_TRUE
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_TRUE, reason="Staged"),
            ],
            context=structpb.Struct(),
        )
        want12.requirements.resources["clusters"].CopyFrom(_CLUSTERS_SELECTOR)
        want12.requirements.resources["auth-secret"].CopyFrom(_AUTH_SELECTOR)

        # --- Case 13: authSecret missing AND no clusters matched. NoClusters is
        # the dominant signal - the cache can't progress regardless of the token
        # - so both conditions report NoClusters and the missing token is neither
        # reported nor warned. ---
        xr13 = _cache_xr(authSecret=v1alpha1.AuthSecret(name="hf-token"))
        want13 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct({"status": {"summary": {"ready": "0/0"}, "clusters": []}}),
                ),
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_FALSE, reason="NoClusters"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_FALSE, reason="NoClusters"),
            ],
            context=structpb.Struct(),
        )
        want13.requirements.resources["clusters"].CopyFrom(_CLUSTERS_SELECTOR)
        want13.requirements.resources["auth-secret"].CopyFrom(_AUTH_SELECTOR)

        cases = [
            Case(
                name="GKE cluster first pass composes RWX PVC and hydration Job",
                req=_req(_cache_xr(), [_cluster_dict("cluster-a", "cluster-a-pc")]),
                want=want1,
            ),
            Case(
                name="HuggingFace revision and auth secret wire --revision and HF_TOKEN",
                req=_req(xr2, [_cluster_dict("cluster-a", "cluster-a-pc")], auth=_auth_secret()),
                want=want2,
            ),
            Case(
                name="EKS cluster PVC sources the EFS class from status.cache",
                req=_req(_cache_xr(), [_cluster_dict("eks-a", "eks-a-pc", source="EKS")]),
                want=want3,
            ),
            Case(
                name="PVC bound and Job complete reports Ready",
                req=_req(_cache_xr(), [_cluster_dict("cluster-a", "cluster-a-pc")], observed4),
                want=want4,
            ),
            Case(
                name="PVC bound but Job running reports Hydrating",
                req=_req(_cache_xr(), [_cluster_dict("cluster-a", "cluster-a-pc")], observed5),
                want=want5,
            ),
            Case(
                name="failed Job reports Failed and takes precedence over PVC binding",
                req=_req(_cache_xr(), [_cluster_dict("cluster-a", "cluster-a-pc")], observed6),
                want=want6,
            ),
            Case(
                name="one of two clusters ready reports partial",
                req=_req(_cache_xr(), [_cluster_dict("a", "a-pc"), _cluster_dict("b", "b-pc")], observed7),
                want=want7,
            ),
            Case(
                name="hydrated cluster stays Ready after its Job is TTL-cleaned",
                req=_req(xr_ready, [_cluster_dict("cluster-a", "cluster-a-pc")], observed8),
                want=want8,
            ),
            Case(
                name="authSecret unresolved requires it and returns early",
                req=_req(xr9, [_cluster_dict("cluster-a", "cluster-a-pc")]),
                want=want9,
            ),
            Case(
                name="authSecret resolved without the referenced key composes PVC and warns",
                req=_req(
                    xr9,
                    [_cluster_dict("cluster-a", "cluster-a-pc")],
                    auth=_auth_secret(data={"OTHER": _TOKEN_B64}),
                ),
                want=want10,
            ),
            Case(
                name="authSecret resolved with an empty token value composes PVC and warns",
                req=_req(
                    xr9,
                    [_cluster_dict("cluster-a", "cluster-a-pc")],
                    auth=_auth_secret(data={"HF_TOKEN": ""}),
                ),
                want=want10,
            ),
            Case(
                name="Ready cluster drops the auth Secret with the Job",
                req=_req(
                    xr11,
                    [_cluster_dict("cluster-a", "cluster-a-pc")],
                    observed11,
                    auth=_auth_secret(),
                ),
                want=want11,
            ),
            Case(
                name="token rotated away after Ready keeps the PVC and stays Ready",
                req=_req(
                    xr12,
                    [_cluster_dict("cluster-a", "cluster-a-pc")],
                    observed12,
                    auth=_auth_secret(data={"OTHER": _TOKEN_B64}),
                ),
                want=want12,
            ),
            Case(
                name="authSecret missing with no clusters reports NoClusters not AuthSecretMissing",
                req=_req(xr13, [], auth=_auth_secret(data={"OTHER": _TOKEN_B64})),
                want=want13,
            ),
        ]

        for case in cases:
            with self.subTest(case.name):
                got = await self.runner.RunFunction(case.req, None)
                self.assertEqual(
                    json_format.MessageToDict(case.want),
                    json_format.MessageToDict(got),
                    "-want, +got",
                )
