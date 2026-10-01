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

import asyncio
import dataclasses
import datetime
import json
from typing import Any

import pytest
from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format, message
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.modelcache import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-model-cache."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _model_cache(*, revision: str | None, auth_secret: v1alpha1.AuthSecret | None, ready: fnv1.Ready) -> fnv1.Resource:
    """The qwen ModelCache XR, with a status reporting cluster-a staged and the XR Ready only if ready is READY_TRUE."""
    status = None
    if ready == fnv1.READY_TRUE:
        status = v1alpha1.Status(
            summary=v1alpha1.Summary(ready="1/1"),
            clusters=[v1alpha1.Cluster(name="cluster-a", phase="Ready")],
            conditions=[
                v1alpha1.Condition(
                    type="Ready",
                    status="True",
                    reason="Available",
                    lastTransitionTime=datetime.datetime(2026, 6, 8, tzinfo=datetime.UTC),
                ),
            ],
        )
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            v1alpha1.ModelCache(
                metadata=metav1.ObjectMeta(name="qwen", namespace="ml-team"),
                spec=v1alpha1.Spec(
                    source="HuggingFace",
                    huggingFace=v1alpha1.HuggingFace(
                        repo="Qwen/Qwen3-0.6B",
                        sizeGiB=20,
                        revision=revision,
                        authSecret=auth_secret,
                    ),
                ),
                status=status,
            ).model_dump(exclude_none=True, mode="json", by_alias=True)
        ),
    )


def _desired_model_cache(*, summary: str, clusters: list[dict], ready: fnv1.Ready) -> fnv1.Resource:
    """The desired ModelCache XR, reporting summary as its ready count and each cluster's phase."""
    return fnv1.Resource(
        resource=resource.dict_to_struct({"status": {"summary": {"ready": summary}, "clusters": clusters}}),
        ready=ready,
    )


def _inference_cluster(*, name: str, provider_config: str, cluster: dict, storage_class: str) -> fnv1.Resource:
    """An InferenceCluster reporting the providerConfigRef and cache StorageClass the function matches on."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "InferenceCluster",
                "metadata": {"name": name},
                "spec": {"cluster": cluster},
                "status": {
                    "providerConfigRef": {"name": provider_config},
                    "cache": {"storageClassName": storage_class},
                },
            }
        ),
    )


def _auth_secret(*, data: dict[str, str]) -> fnv1.Resource:
    """The ModelCache's control-plane authSecret, with base64 data."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "hf-token", "namespace": "ml-team"},
                "type": "Opaque",
                "data": data,
            }
        ),
    )


def _pvc_object(*, provider_config: str, storage_class: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The Object wrapping the qwen ModelCache's cache PVC on a workload cluster."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "v1",
                            "kind": "PersistentVolumeClaim",
                            "metadata": {
                                "name": "modelcache-ml-team-qwen-17db2",
                                "namespace": "mp-ml-team-51733",
                                "labels": {"modelplane.ai/modelcache": "qwen"},
                            },
                            "spec": {
                                "accessModes": ["ReadWriteMany"],
                                "resources": {"requests": {"storage": "20Gi"}},
                                "storageClassName": storage_class,
                            },
                        },
                    },
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": provider_config},
                    "readiness": {"celQuery": 'object.status.phase == "Bound"', "policy": "DeriveFromCelQuery"},
                },
            }
        ),
        ready=ready,
    )


def _job_object(*, provider_config: str, command: str, env: list[dict]) -> fnv1.Resource:
    """The Object wrapping the qwen ModelCache's hydration Job, which mounts the cache PVC."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "batch/v1",
                            "kind": "Job",
                            "metadata": {
                                "name": "modelcache-ml-team-qwen-hydrate-256ec",
                                "namespace": "mp-ml-team-51733",
                                "labels": {"modelplane.ai/modelcache": "qwen"},
                            },
                            "spec": {
                                "backoffLimit": 3,
                                "ttlSecondsAfterFinished": 180,
                                "template": {
                                    "metadata": {"labels": {"modelplane.ai/modelcache": "qwen"}},
                                    "spec": {
                                        "restartPolicy": "OnFailure",
                                        "containers": [
                                            {
                                                "name": "hydrate",
                                                "image": "python:3.11-slim",
                                                "command": ["/bin/sh", "-c", command],
                                                "env": env,
                                                "volumeMounts": [{"name": "artifact", "mountPath": "/mnt/artifact"}],
                                            },
                                        ],
                                        "volumes": [
                                            {
                                                "name": "artifact",
                                                "persistentVolumeClaim": {"claimName": "modelcache-ml-team-qwen-17db2"},
                                            },
                                        ],
                                    },
                                },
                            },
                        },
                    },
                    "managementPolicies": ["Observe", "Create", "Update", "LateInitialize"],
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": provider_config},
                    "readiness": {
                        "celQuery": 'object.status.conditions.exists(c, c.type == "Complete" && c.status == "True")',
                        "policy": "DeriveFromCelQuery",
                    },
                },
            }
        ),
    )


def _observed_pvc_object() -> fnv1.Resource:
    """The cache PVC's Object as observed, with the PVC Bound and the Object Ready."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {"forProvider": {"manifest": {}}},
                "status": {
                    "atProvider": {"manifest": {"status": {"phase": "Bound"}}},
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "True",
                            "reason": "Available",
                            "lastTransitionTime": "2026-06-08T00:00:00Z",
                        },
                    ],
                },
            }
        ),
    )


def _observed_job_object(*, condition: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The hydration Job's Object as observed, the Job reporting condition, and Ready only if ready is READY_TRUE."""
    status: dict[str, Any] = {
        "atProvider": {"manifest": {"status": {"conditions": [{"type": condition, "status": "True"}]}}},
    }
    if ready == fnv1.READY_TRUE:
        status["conditions"] = [
            {
                "type": "Ready",
                "status": "True",
                "reason": "Available",
                "lastTransitionTime": "2026-06-08T00:00:00Z",
            },
        ]
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {"forProvider": {"manifest": {}}},
                "status": status,
            }
        ),
    )


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


# Every case is a HuggingFace ModelCache named qwen in the ml-team namespace. The
# function requires every InferenceCluster with a bare selector, so every
# response carries that requirement.
COMPOSE_CASES = [
    # Nothing is observed yet, so the one-time Staging event fires. The Job sets
    # HF_HUB_CACHE rather than passing --local-dir, so `hf download` writes
    # HuggingFace's cache layout to the mount and a serving pod can load the
    # model by repo id.
    Case(
        name="GKE cluster first pass composes RWX PVC and hydration Job",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_cache(revision=None, auth_secret=None, ready=fnv1.READY_UNSPECIFIED),
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _inference_cluster(
                            name="cluster-a",
                            provider_config="cluster-a-pc",
                            cluster={"source": "GKE", "gke": {"project": "my-project", "region": "us-central1"}},
                            storage_class="modelplane-rwx",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_cache(
                    summary="0/1",
                    clusters=[{"name": "cluster-a", "phase": "Pending"}],
                    ready=fnv1.READY_UNSPECIFIED,
                ),
                resources={
                    "pvc-cluster-a": _pvc_object(
                        provider_config="cluster-a-pc",
                        storage_class="modelplane-rwx",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "hydrate-cluster-a": _job_object(
                        provider_config="cluster-a-pc",
                        command=(
                            "set -e; if [ -f /mnt/artifact/.modelplane-hydrated ]; then echo 'already hydrated, skipping'; exit 0; fi; "
                            "pip install --quiet huggingface_hub; hf download Qwen/Qwen3-0.6B; "
                            "touch /mnt/artifact/.modelplane-hydrated"
                        ),
                        env=[{"name": "HF_HUB_CACHE", "value": "/mnt/artifact"}],
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Staging Qwen/Qwen3-0.6B to 1 clusters: cluster-a",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Hydrating"),
            ],
        ),
    ),
    # The function copies the token's base64 verbatim to a workload-cluster
    # Secret, and the Job's HF_TOKEN references that Secret, because the
    # control-plane Secret isn't on the workload cluster. A Secret has no
    # status, so its Object has no readiness block and uses default readiness.
    Case(
        name="HuggingFace revision and auth secret wire --revision and HF_TOKEN",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_cache(
                    revision="main",
                    auth_secret=v1alpha1.AuthSecret(name="hf-token"),
                    ready=fnv1.READY_UNSPECIFIED,
                ),
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _inference_cluster(
                            name="cluster-a",
                            provider_config="cluster-a-pc",
                            cluster={"source": "GKE", "gke": {"project": "my-project", "region": "us-central1"}},
                            storage_class="modelplane-rwx",
                        ),
                    ],
                ),
                "auth-secret": fnv1.Resources(items=[_auth_secret(data={"HF_TOKEN": "aGYtdG9rZW4tdmFsdWU="})]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_cache(
                    summary="0/1",
                    clusters=[{"name": "cluster-a", "phase": "Pending"}],
                    ready=fnv1.READY_UNSPECIFIED,
                ),
                resources={
                    "auth-cluster-a": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "spec": {
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "Secret",
                                            "metadata": {
                                                "name": "modelcache-ml-team-qwen-auth-ae01b",
                                                "namespace": "mp-ml-team-51733",
                                                "labels": {"modelplane.ai/modelcache": "qwen"},
                                            },
                                            "data": {"HF_TOKEN": "aGYtdG9rZW4tdmFsdWU="},
                                        },
                                    },
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                                },
                            }
                        ),
                    ),
                    "pvc-cluster-a": _pvc_object(
                        provider_config="cluster-a-pc",
                        storage_class="modelplane-rwx",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "hydrate-cluster-a": _job_object(
                        provider_config="cluster-a-pc",
                        command=(
                            "set -e; if [ -f /mnt/artifact/.modelplane-hydrated ]; then echo 'already hydrated, skipping'; exit 0; fi; "
                            "pip install --quiet huggingface_hub; hf download Qwen/Qwen3-0.6B --revision main; "
                            "touch /mnt/artifact/.modelplane-hydrated"
                        ),
                        env=[
                            {"name": "HF_HUB_CACHE", "value": "/mnt/artifact"},
                            {
                                "name": "HF_TOKEN",
                                "valueFrom": {
                                    "secretKeyRef": {"name": "modelcache-ml-team-qwen-auth-ae01b", "key": "HF_TOKEN"},
                                },
                            },
                        ],
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Staging Qwen/Qwen3-0.6B to 1 clusters: cluster-a",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "auth-secret": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="hf-token",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Hydrating"),
            ],
        ),
    ),
    Case(
        name="EKS cluster PVC sources the EFS class from status.cache",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_cache(revision=None, auth_secret=None, ready=fnv1.READY_UNSPECIFIED),
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _inference_cluster(
                            name="eks-a",
                            provider_config="eks-a-pc",
                            cluster={"source": "EKS", "eks": {"region": "us-west-2"}},
                            storage_class="modelplane-rwx-efs",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_cache(
                    summary="0/1",
                    clusters=[{"name": "eks-a", "phase": "Pending"}],
                    ready=fnv1.READY_UNSPECIFIED,
                ),
                resources={
                    "pvc-eks-a": _pvc_object(
                        provider_config="eks-a-pc",
                        storage_class="modelplane-rwx-efs",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "hydrate-eks-a": _job_object(
                        provider_config="eks-a-pc",
                        command=(
                            "set -e; if [ -f /mnt/artifact/.modelplane-hydrated ]; then echo 'already hydrated, skipping'; exit 0; fi; "
                            "pip install --quiet huggingface_hub; hf download Qwen/Qwen3-0.6B; "
                            "touch /mnt/artifact/.modelplane-hydrated"
                        ),
                        env=[{"name": "HF_HUB_CACHE", "value": "/mnt/artifact"}],
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Staging Qwen/Qwen3-0.6B to 1 clusters: eks-a",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Hydrating"),
            ],
        ),
    ),
    # The observed PVC suppresses the Staging event, and the XR becoming Ready
    # emits the staged one. Once the cluster is Ready its Job is dropped, so only
    # the PVC is composed.
    Case(
        name="PVC bound and Job complete reports Ready",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_cache(revision=None, auth_secret=None, ready=fnv1.READY_UNSPECIFIED),
                resources={
                    "pvc-cluster-a": _observed_pvc_object(),
                    "hydrate-cluster-a": _observed_job_object(condition="Complete", ready=fnv1.READY_TRUE),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _inference_cluster(
                            name="cluster-a",
                            provider_config="cluster-a-pc",
                            cluster={"source": "GKE", "gke": {"project": "my-project", "region": "us-central1"}},
                            storage_class="modelplane-rwx",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_cache(
                    summary="1/1",
                    clusters=[{"name": "cluster-a", "phase": "Ready"}],
                    ready=fnv1.READY_TRUE,
                ),
                resources={
                    "pvc-cluster-a": _pvc_object(
                        provider_config="cluster-a-pc",
                        storage_class="modelplane-rwx",
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Artifact staged on all 1 clusters"),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_TRUE, reason="Staged"),
            ],
        ),
    ),
    # The observed PVC suppresses the Staging event.
    Case(
        name="PVC bound with no Job observed reports Hydrating",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_cache(revision=None, auth_secret=None, ready=fnv1.READY_UNSPECIFIED),
                resources={
                    "pvc-cluster-a": _observed_pvc_object(),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _inference_cluster(
                            name="cluster-a",
                            provider_config="cluster-a-pc",
                            cluster={"source": "GKE", "gke": {"project": "my-project", "region": "us-central1"}},
                            storage_class="modelplane-rwx",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_cache(
                    summary="0/1",
                    clusters=[{"name": "cluster-a", "phase": "Hydrating"}],
                    ready=fnv1.READY_UNSPECIFIED,
                ),
                resources={
                    "pvc-cluster-a": _pvc_object(
                        provider_config="cluster-a-pc",
                        storage_class="modelplane-rwx",
                        ready=fnv1.READY_TRUE,
                    ),
                    "hydrate-cluster-a": _job_object(
                        provider_config="cluster-a-pc",
                        command=(
                            "set -e; if [ -f /mnt/artifact/.modelplane-hydrated ]; then echo 'already hydrated, skipping'; exit 0; fi; "
                            "pip install --quiet huggingface_hub; hf download Qwen/Qwen3-0.6B; "
                            "touch /mnt/artifact/.modelplane-hydrated"
                        ),
                        env=[{"name": "HF_HUB_CACHE", "value": "/mnt/artifact"}],
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Hydrating"),
            ],
        ),
    ),
    Case(
        name="failed Job reports Failed and takes precedence over PVC binding",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_cache(revision=None, auth_secret=None, ready=fnv1.READY_UNSPECIFIED),
                resources={
                    "pvc-cluster-a": _observed_pvc_object(),
                    "hydrate-cluster-a": _observed_job_object(condition="Failed", ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _inference_cluster(
                            name="cluster-a",
                            provider_config="cluster-a-pc",
                            cluster={"source": "GKE", "gke": {"project": "my-project", "region": "us-central1"}},
                            storage_class="modelplane-rwx",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_cache(
                    summary="0/1",
                    clusters=[{"name": "cluster-a", "phase": "Failed"}],
                    ready=fnv1.READY_UNSPECIFIED,
                ),
                resources={
                    "pvc-cluster-a": _pvc_object(
                        provider_config="cluster-a-pc",
                        storage_class="modelplane-rwx",
                        ready=fnv1.READY_TRUE,
                    ),
                    "hydrate-cluster-a": _job_object(
                        provider_config="cluster-a-pc",
                        command=(
                            "set -e; if [ -f /mnt/artifact/.modelplane-hydrated ]; then echo 'already hydrated, skipping'; exit 0; fi; "
                            "pip install --quiet huggingface_hub; hf download Qwen/Qwen3-0.6B; "
                            "touch /mnt/artifact/.modelplane-hydrated"
                        ),
                        env=[{"name": "HF_HUB_CACHE", "value": "/mnt/artifact"}],
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Failed"),
            ],
        ),
    ),
    # Cluster a is Ready, so its Job is dropped, while b, with only its PVC
    # observed, is still Hydrating.
    Case(
        name="one of two clusters ready reports partial",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_cache(revision=None, auth_secret=None, ready=fnv1.READY_UNSPECIFIED),
                resources={
                    "pvc-a": _observed_pvc_object(),
                    "hydrate-a": _observed_job_object(condition="Complete", ready=fnv1.READY_TRUE),
                    "pvc-b": _observed_pvc_object(),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _inference_cluster(
                            name="a",
                            provider_config="a-pc",
                            cluster={"source": "GKE", "gke": {"project": "my-project", "region": "us-central1"}},
                            storage_class="modelplane-rwx",
                        ),
                        _inference_cluster(
                            name="b",
                            provider_config="b-pc",
                            cluster={"source": "GKE", "gke": {"project": "my-project", "region": "us-central1"}},
                            storage_class="modelplane-rwx",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_cache(
                    summary="1/2",
                    clusters=[{"name": "a", "phase": "Ready"}, {"name": "b", "phase": "Hydrating"}],
                    ready=fnv1.READY_UNSPECIFIED,
                ),
                resources={
                    "pvc-a": _pvc_object(provider_config="a-pc", storage_class="modelplane-rwx", ready=fnv1.READY_TRUE),
                    "pvc-b": _pvc_object(provider_config="b-pc", storage_class="modelplane-rwx", ready=fnv1.READY_TRUE),
                    "hydrate-b": _job_object(
                        provider_config="b-pc",
                        command=(
                            "set -e; if [ -f /mnt/artifact/.modelplane-hydrated ]; then echo 'already hydrated, skipping'; exit 0; fi; "
                            "pip install --quiet huggingface_hub; hf download Qwen/Qwen3-0.6B; "
                            "touch /mnt/artifact/.modelplane-hydrated"
                        ),
                        env=[{"name": "HF_HUB_CACHE", "value": "/mnt/artifact"}],
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Partial"),
            ],
        ),
    ),
    # The XR's status already reports the cluster Ready, and only its PVC is
    # observed, as after the TTL controller cleans up the Job. The status latch
    # keeps the cluster Ready, so the Job isn't composed again, and the XR was
    # already Ready, so no event fires.
    Case(
        name="hydrated cluster stays Ready after its Job is TTL-cleaned",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_cache(revision=None, auth_secret=None, ready=fnv1.READY_TRUE),
                resources={
                    "pvc-cluster-a": _observed_pvc_object(),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _inference_cluster(
                            name="cluster-a",
                            provider_config="cluster-a-pc",
                            cluster={"source": "GKE", "gke": {"project": "my-project", "region": "us-central1"}},
                            storage_class="modelplane-rwx",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_cache(
                    summary="1/1",
                    clusters=[{"name": "cluster-a", "phase": "Ready"}],
                    ready=fnv1.READY_TRUE,
                ),
                resources={
                    "pvc-cluster-a": _pvc_object(
                        provider_config="cluster-a-pc",
                        storage_class="modelplane-rwx",
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_TRUE, reason="Staged"),
            ],
        ),
    ),
    # The function waits for Crossplane to resolve the authSecret before it
    # composes anything.
    Case(
        name="authSecret unresolved requires it and returns early",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_cache(
                    revision=None,
                    auth_secret=v1alpha1.AuthSecret(name="hf-token"),
                    ready=fnv1.READY_UNSPECIFIED,
                ),
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _inference_cluster(
                            name="cluster-a",
                            provider_config="cluster-a-pc",
                            cluster={"source": "GKE", "gke": {"project": "my-project", "region": "us-central1"}},
                            storage_class="modelplane-rwx",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "auth-secret": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="hf-token",
                        namespace="ml-team",
                    ),
                },
            ),
        ),
    ),
    # The Secret carries OTHER rather than HF_TOKEN. The PVC doesn't need the
    # token, so it still composes and a cache isn't pruned for a missing one, but
    # the Job and token Secret are held back. The XR is marked not ready, since
    # the PVC alone would make it ready once it binds.
    Case(
        name="authSecret resolved without the referenced key composes PVC and warns",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_cache(
                    revision=None,
                    auth_secret=v1alpha1.AuthSecret(name="hf-token"),
                    ready=fnv1.READY_UNSPECIFIED,
                ),
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _inference_cluster(
                            name="cluster-a",
                            provider_config="cluster-a-pc",
                            cluster={"source": "GKE", "gke": {"project": "my-project", "region": "us-central1"}},
                            storage_class="modelplane-rwx",
                        ),
                    ],
                ),
                "auth-secret": fnv1.Resources(items=[_auth_secret(data={"OTHER": "aGYtdG9rZW4tdmFsdWU="})]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_cache(
                    summary="0/1",
                    clusters=[{"name": "cluster-a", "phase": "Pending"}],
                    ready=fnv1.READY_FALSE,
                ),
                resources={
                    "pvc-cluster-a": _pvc_object(
                        provider_config="cluster-a-pc",
                        storage_class="modelplane-rwx",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
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
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "auth-secret": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="hf-token",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_FALSE, reason="AuthSecretMissing"),
            ],
        ),
    ),
    # An empty token value is as broken as a missing key: the Job would run with
    # an empty HF_TOKEN.
    Case(
        name="authSecret resolved with an empty token value composes PVC and warns",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_cache(
                    revision=None,
                    auth_secret=v1alpha1.AuthSecret(name="hf-token"),
                    ready=fnv1.READY_UNSPECIFIED,
                ),
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _inference_cluster(
                            name="cluster-a",
                            provider_config="cluster-a-pc",
                            cluster={"source": "GKE", "gke": {"project": "my-project", "region": "us-central1"}},
                            storage_class="modelplane-rwx",
                        ),
                    ],
                ),
                "auth-secret": fnv1.Resources(items=[_auth_secret(data={"HF_TOKEN": ""})]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_cache(
                    summary="0/1",
                    clusters=[{"name": "cluster-a", "phase": "Pending"}],
                    ready=fnv1.READY_FALSE,
                ),
                resources={
                    "pvc-cluster-a": _pvc_object(
                        provider_config="cluster-a-pc",
                        storage_class="modelplane-rwx",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
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
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "auth-secret": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="hf-token",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_FALSE, reason="AuthSecretMissing"),
            ],
        ),
    ),
    # The token is only needed while hydrating, so once the cluster is Ready the
    # auth Secret is dropped with the Job, even though the control-plane Secret
    # still resolves. That keeps the token off the inference cluster after
    # hydration.
    Case(
        name="Ready cluster drops the auth Secret with the Job",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_cache(
                    revision=None,
                    auth_secret=v1alpha1.AuthSecret(name="hf-token"),
                    ready=fnv1.READY_UNSPECIFIED,
                ),
                resources={
                    "auth-cluster-a": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "spec": {"forProvider": {"manifest": {}}},
                                "status": {
                                    "atProvider": {"manifest": {"status": {}}},
                                    "conditions": [
                                        {
                                            "type": "Ready",
                                            "status": "True",
                                            "reason": "Available",
                                            "lastTransitionTime": "2026-06-08T00:00:00Z",
                                        },
                                    ],
                                },
                            }
                        ),
                    ),
                    "pvc-cluster-a": _observed_pvc_object(),
                    "hydrate-cluster-a": _observed_job_object(condition="Complete", ready=fnv1.READY_TRUE),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _inference_cluster(
                            name="cluster-a",
                            provider_config="cluster-a-pc",
                            cluster={"source": "GKE", "gke": {"project": "my-project", "region": "us-central1"}},
                            storage_class="modelplane-rwx",
                        ),
                    ],
                ),
                "auth-secret": fnv1.Resources(items=[_auth_secret(data={"HF_TOKEN": "aGYtdG9rZW4tdmFsdWU="})]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_cache(
                    summary="1/1",
                    clusters=[{"name": "cluster-a", "phase": "Ready"}],
                    ready=fnv1.READY_TRUE,
                ),
                resources={
                    "pvc-cluster-a": _pvc_object(
                        provider_config="cluster-a-pc",
                        storage_class="modelplane-rwx",
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Artifact staged on all 1 clusters"),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "auth-secret": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="hf-token",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_TRUE, reason="Staged"),
            ],
        ),
    ),
    # The XR's status already reports the cluster Ready, and its authSecret now
    # lacks the key. The PVC doesn't need the token, so it isn't pruned, and
    # hydration is done, so the missing token is neither reported nor warned.
    Case(
        name="token rotated away after Ready keeps the PVC and stays Ready",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_cache(
                    revision=None,
                    auth_secret=v1alpha1.AuthSecret(name="hf-token"),
                    ready=fnv1.READY_TRUE,
                ),
                resources={
                    "pvc-cluster-a": _observed_pvc_object(),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _inference_cluster(
                            name="cluster-a",
                            provider_config="cluster-a-pc",
                            cluster={"source": "GKE", "gke": {"project": "my-project", "region": "us-central1"}},
                            storage_class="modelplane-rwx",
                        ),
                    ],
                ),
                "auth-secret": fnv1.Resources(items=[_auth_secret(data={"OTHER": "aGYtdG9rZW4tdmFsdWU="})]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_cache(
                    summary="1/1",
                    clusters=[{"name": "cluster-a", "phase": "Ready"}],
                    ready=fnv1.READY_TRUE,
                ),
                resources={
                    "pvc-cluster-a": _pvc_object(
                        provider_config="cluster-a-pc",
                        storage_class="modelplane-rwx",
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "auth-secret": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="hf-token",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_TRUE, reason="Matched"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_TRUE, reason="Staged"),
            ],
        ),
    ),
    # Crossplane reports a selector that matches nothing as a present but empty
    # entry, unlike an unresolved one, whose key is absent. NoClusters dominates,
    # since the cache can't progress whatever the token, so the missing token is
    # neither reported nor warned.
    Case(
        name="authSecret without the key and no clusters reports NoClusters not AuthSecretMissing",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_cache(
                    revision=None,
                    auth_secret=v1alpha1.AuthSecret(name="hf-token"),
                    ready=fnv1.READY_UNSPECIFIED,
                ),
            ),
            required_resources={
                "clusters": fnv1.Resources(),
                "auth-secret": fnv1.Resources(items=[_auth_secret(data={"OTHER": "aGYtdG9rZW4tdmFsdWU="})]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_cache(summary="0/0", clusters=[], ready=fnv1.READY_FALSE),
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "auth-secret": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="hf-token",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClustersMatched", status=fnv1.STATUS_CONDITION_FALSE, reason="NoClusters"),
                fnv1.Condition(type="ArtifactReady", status=fnv1.STATUS_CONDITION_FALSE, reason="NoClusters"),
            ],
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction composes a ModelCache's PVC and hydration Job per cluster and reports their progress."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want)
