"""Tests for the compose-model-cache function."""

import dataclasses
import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.modelcache import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


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
def _cache_xr(**hf_extra) -> v1alpha1.ModelCache:
    return v1alpha1.ModelCache(
        metadata=metav1.ObjectMeta(name="qwen", namespace="ml-team"),
        spec=v1alpha1.Spec(
            source="HuggingFace",
            huggingFace=v1alpha1.HuggingFace(repo="Qwen/Qwen3-0.6B", sizeGiB=20, **hf_extra),
        ),
    )


def _cluster_dict(name: str, pc: str, *, source: str = "GKE") -> dict:
    """An InferenceCluster as Crossplane returns it in a required-resource set.

    No cache block, so the function falls back to the source's XRD default
    storage class (GKE -> modelplane-rwx, EKS -> modelplane-rwx-efs).
    """
    blocks = {
        "GKE": {"gke": {"project": "my-project", "region": "us-central1"}},
        "EKS": {"eks": {"region": "us-west-2"}},
    }
    return {
        "apiVersion": "modelplane.ai/v1alpha1",
        "kind": "InferenceCluster",
        "metadata": {"name": name},
        "spec": {"cluster": {"source": source, **blocks[source]}},
        "status": {"providerConfigRef": {"name": pc}},
    }


def _observed_object(manifest_status: dict, *, ready: bool = False) -> dict:
    """A full Object envelope as provider-kubernetes observes it.

    Carries the echoed remote status under status.atProvider.manifest.status
    (read by derive_cluster_phase) and, when `ready`, the Object's own Ready
    condition (read by mark_ready_resources, populated by DeriveFromCelQuery).
    """
    obj = {
        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
        "kind": "Object",
        "spec": {"forProvider": {"manifest": {}}},
        "status": {"atProvider": {"manifest": {"status": manifest_status}}},
    }
    if ready:
        obj["status"]["conditions"] = [
            {
                "type": "Ready",
                "status": "True",
                "reason": "Available",
                "lastTransitionTime": "2026-06-08T00:00:00Z",
            },
        ]
    return obj


def _req(
    xr: v1alpha1.ModelCache,
    clusters: list[dict],
    observed: dict[str, dict] | None = None,
) -> fnv1.RunFunctionRequest:
    """Build a request the way the repo's other function tests do.

    - XR goes in observed.composite via dict_to_struct(model_dump(mode="json")).
    - Resolved clusters go in the `clusters` required-resource set.
    - `observed` maps a desired-resource key -> an observed Object envelope.
    """
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(
                resource=resource.dict_to_struct(xr.model_dump(exclude_none=True, mode="json")),
            ),
        ),
    )
    for c in clusters:
        req.required_resources["clusters"].items.append(
            fnv1.Resource(resource=resource.dict_to_struct(c)),
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
_LABELS = {"modelplane.ai/modelcache": "qwen"}


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
                        "ttlSecondsAfterFinished": 3600,
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
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": pc},
            "readiness": {
                "celQuery": 'object.status.conditions.exists(c, c.type == "Complete" && c.status == "True")',
                "policy": "DeriveFromCelQuery",
            },
        },
    }


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    """Tests for FunctionRunner.RunFunction."""

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:
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
        # command gains --revision and an HF_TOKEN env from the secret. ---
        xr2 = _cache_xr(revision="main", authSecret=v1alpha1.AuthSecret(name="hf-token"))
        env2 = [
            {
                "name": "HF_TOKEN",
                "valueFrom": {"secretKeyRef": {"name": "hf-token", "key": "HF_TOKEN"}},
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

        # --- Case 3: EKS cluster, no cache block. The PVC falls back to the EFS
        # default storage class (modelplane-rwx-efs), not the GKE/Filestore one. ---
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
            "hydrate-cluster-a": _observed_object({"succeeded": 1}, ready=True),
        }
        pvc_ready = _pvc_object("cluster-a-pc")
        job_ready = _job_object("cluster-a-pc")
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
                    "pvc-cluster-a": fnv1.Resource(resource=resource.dict_to_struct(pvc_ready), ready=fnv1.READY_TRUE),
                    "hydrate-cluster-a": fnv1.Resource(
                        resource=resource.dict_to_struct(job_ready), ready=fnv1.READY_TRUE
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
            "hydrate-a": _observed_object({"succeeded": 1}, ready=True),
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
                    "pvc-a": fnv1.Resource(
                        resource=resource.dict_to_struct(_pvc_object("a-pc")), ready=fnv1.READY_TRUE
                    ),
                    "hydrate-a": fnv1.Resource(
                        resource=resource.dict_to_struct(_job_object("a-pc")), ready=fnv1.READY_TRUE
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

        cases = [
            Case(
                name="GKE cluster first pass composes RWX PVC and hydration Job",
                req=_req(_cache_xr(), [_cluster_dict("cluster-a", "cluster-a-pc")]),
                want=want1,
            ),
            Case(
                name="HuggingFace revision and auth secret wire --revision and HF_TOKEN",
                req=_req(xr2, [_cluster_dict("cluster-a", "cluster-a-pc")]),
                want=want2,
            ),
            Case(
                name="EKS cluster PVC falls back to the EFS default storage class",
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
        ]

        for case in cases:
            with self.subTest(case.name):
                got = await self.runner.RunFunction(case.req, None)
                self.assertEqual(
                    json_format.MessageToDict(case.want),
                    json_format.MessageToDict(got),
                    "-want, +got",
                )
