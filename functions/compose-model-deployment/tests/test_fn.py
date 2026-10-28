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

"""Tests for the compose-model-deployment function."""

import asyncio
import dataclasses
import json
from typing import Any, Literal

import pytest
from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format, message
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.modeldeployment import v1alpha1
from models.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class ComposeCase:
    """A test case for RunFunction."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


@dataclasses.dataclass
class ResolveRequiredCase:
    """A test case for fn.resolve_required."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    # resolve_required's name parameter, renamed so it doesn't collide with
    # the case's own name.
    requirement: str
    want: tuple[fn.Resolution, dict | None]


@dataclasses.dataclass
class InjectNameCase:
    """A test case for fn._inject_served_model_name."""

    name: str
    reason: str
    template: mrv1alpha1.Template
    served: str
    want: mrv1alpha1.Template


@dataclasses.dataclass
class ServedModelNameCase:
    """A test case for fn.served_model_name."""

    name: str
    reason: str
    namespace: str
    deployment: str
    want: str


def _model_deployment(
    *,
    replicas: int,
    template_labels: dict[str, str] | None,
    cluster_selector: v1alpha1.ClusterSelector | None,
    model_cache: str | None,
    serving_mode: Literal["Unified", "PrefillDecode"] | None,
    endpoint_picker: dict[str, Any] | None,
    sidecar: dict[str, Any] | None,
    engines: list[dict[str, Any]],
    args: list[str] | None,
) -> fnv1.Resource:
    """The observed ModelDeployment my-model, each of its engines running one Standalone GPU member.

    A sidecar, when given, runs beside the engine container in every member. An
    endpoint picker, when given, comes with router: EndpointPicker.
    """
    member = v1alpha1.Member(
        role="Standalone",
        nodeSelector=v1alpha1.NodeSelector(
            devices=[
                v1alpha1.Device(
                    name="gpu",
                    count=1,
                    selectors=[v1alpha1.Selector(cel='device.driver == "gpu.nvidia.com"')],
                ),
            ],
        ),
        template=v1alpha1.Template(
            spec=v1alpha1.Spec(
                containers=[
                    v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest", args=args),
                    *([v1alpha1.Container.model_validate(sidecar)] if sidecar is not None else []),
                ],
            ),
        ),
    )
    xr = v1alpha1.ModelDeployment(
        metadata=metav1.ObjectMeta(name="my-model", namespace="ml-team"),
        spec=v1alpha1.SpecModel2(
            replicas=replicas,
            template=v1alpha1.TemplateModel1(
                metadata=v1alpha1.Metadata(labels=template_labels) if template_labels is not None else None,
                spec=v1alpha1.SpecModel1(
                    clusterSelector=cluster_selector,
                    modelCacheRef=v1alpha1.ModelCacheRef(name=model_cache) if model_cache is not None else None,
                    serving=v1alpha1.Serving(
                        mode=serving_mode,
                        router="EndpointPicker" if endpoint_picker is not None else None,
                        endpointPicker=v1alpha1.EndpointPicker.model_validate(endpoint_picker)
                        if endpoint_picker is not None
                        else None,
                    )
                    if serving_mode is not None
                    else None,
                    engines=[v1alpha1.Engine(**engine, members=[member]) for engine in engines],
                ),
            ),
        ),
    )
    return fnv1.Resource(resource=resource.dict_to_struct(xr.model_dump(exclude_none=True, mode="json", by_alias=True)))


def _desired_model_deployment(*, total_replicas: int, ready_replicas: int, ready: fnv1.Ready) -> fnv1.Resource:
    """The desired ModelDeployment, with how many replicas it scheduled and how many are ready."""
    return fnv1.Resource(
        resource=resource.dict_to_struct({"status": {"replicas": {"total": total_replicas, "ready": ready_replicas}}}),
        ready=ready,
    )


def _cluster(
    *,
    name: str,
    ready: bool,
    gateway_hostname: str | None,
    nodes: int,
    cache_storage: bool,
    placement_labels: dict[str, str] | None,
) -> fnv1.Resource:
    """A required InferenceCluster with one pool of single-GPU nodes."""
    spec: dict[str, Any] = {
        "cluster": {"source": "Existing", "existing": {"secretRef": {"name": "k", "key": "kubeconfig"}}},
        "stack": "Standard",
    }
    if placement_labels is not None:
        spec["placement"] = {"metadata": {"labels": placement_labels}}
    status: dict[str, Any] = {
        "conditions": [
            {
                "type": "Ready",
                "status": "True" if ready else "False",
                "reason": "Available" if ready else "Unavailable",
                "lastTransitionTime": "2025-01-01T00:00:00Z",
            }
        ],
        "providerConfigRef": {"name": name},
        "gpuPools": [
            {
                "name": "default",
                "nodes": nodes,
                "devices": [
                    {
                        "name": "gpu",
                        "claim": "DRA",
                        "driver": "gpu.nvidia.com",
                        "deviceClassName": "gpu.nvidia.com",
                        "count": 1,
                    }
                ],
            }
        ],
    }
    if gateway_hostname is not None:
        status["gateway"] = {"address": "10.0.0.1", "hostname": gateway_hostname}
    if cache_storage:
        status["cache"] = {"storageClassName": "rwx"}
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "InferenceCluster",
                "metadata": {"name": name},
                "spec": spec,
                "status": status,
            }
        )
    )


def _cache(*, cluster_selector: dict | None) -> fnv1.Resource:
    """The ModelCache qwen, which a deployment references by modelCacheRef."""
    spec: dict[str, Any] = {
        "source": "HuggingFace",
        "huggingFace": {"repo": "Qwen/Qwen2.5-7B", "sizeGiB": 20},
    }
    if cluster_selector is not None:
        spec["clusterSelector"] = cluster_selector
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "ModelCache",
                "metadata": {"name": "qwen", "namespace": "ml-team"},
                "spec": spec,
            }
        )
    )


def _observed_replica(*, ready: bool | None, serving: bool | None) -> fnv1.Resource:
    """The ModelReplica my-model has on cluster-a, with no conditions if ready is None.

    Otherwise serving sets its ModelServing condition.
    """
    replica: dict[str, Any] = {
        "apiVersion": "modelplane.ai/v1alpha1",
        "kind": "ModelReplica",
        "metadata": {
            "name": "my-model-5ab63",
            "namespace": "ml-team",
            "labels": {
                "modelplane.ai/deployment": "my-model",
                "modelplane.ai/cluster": "cluster-a",
                "modelplane.ai/replica-index": "0",
            },
        },
        "spec": {
            "clusterName": "cluster-a",
            "engines": [
                {
                    "name": "main",
                    "copies": 1,
                    "members": [
                        {
                            "role": "Standalone",
                            "nodePoolName": "default",
                            "deviceRequests": [
                                {
                                    "name": "gpu",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "selectors": [{"cel": 'device.driver == "gpu.nvidia.com"'}],
                                }
                            ],
                            "template": {
                                "spec": {"containers": [{"name": "engine", "image": "vllm/vllm-openai:latest"}]}
                            },
                        }
                    ],
                }
            ],
        },
    }
    if ready is not None:
        replica["status"] = {
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True" if ready else "False",
                    "reason": "Available" if ready else "Creating",
                    "lastTransitionTime": "2025-01-01T00:00:00Z",
                },
                {
                    "type": "ModelServing",
                    "status": "True" if serving else "False",
                    "reason": "Serving" if serving else "ModelStarting",
                    "lastTransitionTime": "2025-01-01T00:00:00Z",
                },
            ]
        }
    return fnv1.Resource(resource=resource.dict_to_struct(replica))


def _observed_endpoint() -> fnv1.Resource:
    """The ModelEndpoint my-model has for its replica on cluster-a, as observed."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "ModelEndpoint",
                "metadata": {"name": "my-model-5ab63", "namespace": "ml-team"},
            }
        )
    )


def _composed_replica(
    *,
    name: str,
    cluster: str,
    labels: dict[str, str],
    model_cache: str | None,
    serving_mode: str | None,
    endpoint_picker: dict[str, Any] | None,
    sidecar: dict[str, Any] | None,
    engines: list[dict[str, str]],
    args: list[str] | None,
    ready: fnv1.Ready,
) -> fnv1.Resource:
    """A composed ModelReplica of my-model, each of its engines running one Standalone GPU member."""
    container: dict[str, Any] = {"name": "engine", "image": "vllm/vllm-openai:latest"}
    if args is not None:
        container["args"] = args
    # Every container carries MODELPLANE_SERVED_MODEL_NAME, ahead of any env the
    # user wrote, so an arg can reference it. It's how an engine comes up under
    # the name Modelplane routes to, instead of Modelplane having to be told
    # what it was started with.
    container["env"] = [{"name": "MODELPLANE_SERVED_MODEL_NAME", "value": "ml-team/my-model"}]
    member = {
        # No worker block: it's only set on Worker members, and the XRD
        # deliberately has no schema default (defaults apply before CEL
        # validation, which forbids worker on a Standalone).
        "role": "Standalone",
        "nodePoolName": "default",
        # The deployment's nodeSelector matched against the cluster's GPU device
        # (deviceClassName gpu.nvidia.com), with its CEL selector echoed
        # verbatim.
        "deviceRequests": [
            {
                "name": "gpu",
                "deviceClassName": "gpu.nvidia.com",
                "count": 1,
                "selectors": [{"cel": 'device.driver == "gpu.nvidia.com"'}],
            }
        ],
        # A sidecar passes through as written: only the engine gets the
        # served model name.
        "template": {"spec": {"containers": [container, *([sidecar] if sidecar is not None else [])]}},
    }
    spec: dict[str, Any] = {"clusterName": cluster}
    if model_cache is not None:
        spec["modelCacheRef"] = {"name": model_cache}
    if serving_mode is not None:
        serving: dict[str, Any] = {"mode": serving_mode}
        if endpoint_picker is not None:
            serving["router"] = "EndpointPicker"
            serving["endpointPicker"] = endpoint_picker
        spec["serving"] = serving
    spec["engines"] = [{**engine, "copies": 1, "members": [member]} for engine in engines]
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "ModelReplica",
                "metadata": {"name": name, "namespace": "ml-team", "labels": labels},
                "spec": spec,
            }
        ),
        ready=ready,
    )


def _composed_endpoint(*, labels: dict[str, str]) -> fnv1.Resource:
    """The ModelEndpoint for my-model's replica on cluster-a, as composed."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "ModelEndpoint",
                "metadata": {"name": "my-model-5ab63", "namespace": "ml-team", "labels": labels},
                "spec": {
                    "origin": "https://cluster.clusters.example.com",
                    "api": {"schema": "OpenAI", "prefix": "/ml-team/my-model-5ab63/v1"},
                    "model": "ml-team/my-model",
                },
            }
        )
    )


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


COMPOSE_CASES = [
    # Routing must not advertise a backend whose pods are still warming up
    # (#102).
    ComposeCase(
        name="FreshlyScheduled",
        reason="A newly scheduled replica composes without an endpoint, because it isn't observed serving yet.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=2,
                            cache_storage=False,
                            placement_labels=None,
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_FALSE),
                resources={
                    "replica-cluster-a-0": _composed_replica(
                        name="my-model-5ab63",
                        cluster="cluster-a",
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        },
                        model_cache=None,
                        serving_mode=None,
                        endpoint_picker=None,
                        sidecar=None,
                        engines=[{"name": "main"}],
                        args=["--model=Qwen/Qwen3-0.6B"],
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Scheduled 1 replicas across 1 clusters: cluster-a",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Scheduling",
                    message="Created 0 of 1 replicas",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas ready",
                ),
            ],
        ),
    ),
    # A replica can stop serving after once serving, say in a crash-loop.
    # Deleting its endpoint stops traffic routing to the dead backend (#102).
    ComposeCase(
        name="ReplicaNotReady",
        reason="A replica observed not serving loses its endpoint, which is left out of desired so Crossplane deletes it.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
                resources={
                    "replica-cluster-a-0": _observed_replica(ready=False, serving=False),
                    "endpoint-cluster-a-0": _observed_endpoint(),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=2,
                            cache_storage=False,
                            placement_labels=None,
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(items=[_observed_replica(ready=None, serving=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_FALSE),
                resources={
                    "replica-cluster-a-0": _composed_replica(
                        name="my-model-5ab63",
                        cluster="cluster-a",
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        },
                        model_cache=None,
                        serving_mode=None,
                        endpoint_picker=None,
                        sidecar=None,
                        engines=[{"name": "main"}],
                        args=["--model=Qwen/Qwen3-0.6B"],
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="ReplicasCreated",
                    message="Scheduled 1 of 1 replicas",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas ready",
                ),
            ],
        ),
    ),
    ComposeCase(
        name="NoClusters",
        reason="With no InferenceClusters, the function composes nothing, warns, and reports NoClusters.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
            ),
            required_resources={
                "clusters": fnv1.Resources(),
                "all-replicas": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            # Inline rather than _desired_model_deployment, because with no
            # cluster the function returns before it writes any status.
            desired=fnv1.State(composite=fnv1.Resource(ready=fnv1.READY_FALSE)),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_WARNING, message="No InferenceClusters found"),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="NoClusters",
                ),
            ],
        ),
    ),
    ComposeCase(
        name="InsufficientCapacity",
        reason="A cluster with no nodes leaves the replica unscheduled, reported as InsufficientCapacity.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=0,
                            cache_storage=False,
                            placement_labels=None,
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=0, ready_replicas=0, ready=fnv1.READY_FALSE),
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="InsufficientCapacity",
                    message="0 of 1 replicas scheduled (checked 1 clusters)",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="NoReplicasScheduled",
                    message="0 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="NoReplicasScheduled",
                    message="0 of 1 replicas ready",
                ),
            ],
        ),
    ),
    # Zero desired parks the deployment before resolve_inputs runs, so it
    # reads as NoReplicasDesired rather than as a capacity failure.
    ComposeCase(
        name="ZeroReplicas",
        reason="A deployment of zero replicas declares no requirements, composes nothing, and reports NoReplicasDesired.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=0,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
            ),
            required_resources={
                "clusters": fnv1.Resources(),
                "all-replicas": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=0, ready_replicas=0, ready=fnv1.READY_TRUE),
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="NoReplicasDesired",
                    message="0 replicas desired",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="NoReplicasDesired",
                    message="0 replicas desired",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="NoReplicasDesired",
                    message="0 replicas desired",
                ),
            ],
        ),
    ),
    # The scale-down is announced while the replica and endpoint still exist.
    ComposeCase(
        name="ScaleToZero",
        reason="Scaling a running deployment to zero prunes its replica and endpoint, and announces the scale-down.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=0,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
                resources={
                    "replica-cluster-a-0": _observed_replica(ready=True, serving=True),
                    "endpoint-cluster-a-0": _observed_endpoint(),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(),
                "all-replicas": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=0, ready_replicas=0, ready=fnv1.READY_TRUE),
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Scaled to zero: removing all replicas",
                ),
            ],
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="NoReplicasDesired",
                    message="0 replicas desired",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="NoReplicasDesired",
                    message="0 replicas desired",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="NoReplicasDesired",
                    message="0 replicas desired",
                ),
            ],
        ),
    ),
    ComposeCase(
        name="ReplicaReady",
        reason="A replica observed serving and Ready keeps its place and its endpoint, and the deployment reports all replicas ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
                resources={
                    "replica-cluster-a-0": _observed_replica(ready=True, serving=True),
                    "endpoint-cluster-a-0": _observed_endpoint(),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=2,
                            cache_storage=False,
                            placement_labels=None,
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(items=[_observed_replica(ready=None, serving=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=1, ready=fnv1.READY_UNSPECIFIED),
                resources={
                    "replica-cluster-a-0": _composed_replica(
                        name="my-model-5ab63",
                        cluster="cluster-a",
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        },
                        model_cache=None,
                        serving_mode=None,
                        endpoint_picker=None,
                        sidecar=None,
                        engines=[{"name": "main"}],
                        args=["--model=Qwen/Qwen3-0.6B"],
                        ready=fnv1.READY_TRUE,
                    ),
                    "endpoint-cluster-a-0": _composed_endpoint(
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        }
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="ReplicasCreated",
                    message="Scheduled 1 of 1 replicas",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Serving",
                    message="1 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="AllReplicasReady",
                    message="1 of 1 replicas ready",
                ),
            ],
        ),
    ),
    ComposeCase(
        name="NoGatewayHostname",
        reason=(
            "A Ready replica on a cluster whose gateway has no hostname gets no endpoint, so the deployment "
            "isn't serving or Ready."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
                resources={
                    "replica-cluster-a-0": _observed_replica(ready=True, serving=True),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname=None,
                            nodes=2,
                            cache_storage=False,
                            placement_labels=None,
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(items=[_observed_replica(ready=None, serving=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=1, ready=fnv1.READY_FALSE),
                resources={
                    "replica-cluster-a-0": _composed_replica(
                        name="my-model-5ab63",
                        cluster="cluster-a",
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        },
                        model_cache=None,
                        serving_mode=None,
                        endpoint_picker=None,
                        sidecar=None,
                        engines=[{"name": "main"}],
                        args=["--model=Qwen/Qwen3-0.6B"],
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="ReplicasCreated",
                    message="Scheduled 1 of 1 replicas",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForGateway",
                    message="0 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForGateway",
                    message="0 of 1 replicas ready",
                ),
            ],
        ),
    ),
    ComposeCase(
        name="PartiallyScheduled",
        reason=(
            "A deployment that could place only one of its two replicas serves from the one it placed but isn't "
            "scheduled or ready, even with that replica at full capacity."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=2,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
                resources={
                    "replica-cluster-a-0": _observed_replica(ready=True, serving=True),
                    "endpoint-cluster-a-0": _observed_endpoint(),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=1,
                            cache_storage=False,
                            placement_labels=None,
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(items=[_observed_replica(ready=None, serving=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=1, ready=fnv1.READY_FALSE),
                resources={
                    "replica-cluster-a-0": _composed_replica(
                        name="my-model-5ab63",
                        cluster="cluster-a",
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        },
                        model_cache=None,
                        serving_mode=None,
                        endpoint_picker=None,
                        sidecar=None,
                        engines=[{"name": "main"}],
                        args=["--model=Qwen/Qwen3-0.6B"],
                        ready=fnv1.READY_TRUE,
                    ),
                    "endpoint-cluster-a-0": _composed_endpoint(
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        }
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="InsufficientCapacity",
                    message="Scheduled 1 of 2 replicas (checked 1 clusters)",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Serving",
                    message="1 of 2 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Scheduling",
                    message="1 of 2 replicas ready",
                ),
            ],
        ),
    ),
    ComposeCase(
        name="ReplicaDegraded",
        reason=(
            "A replica that's serving but not at full capacity, such as one with a failed copy, keeps its endpoint, "
            "and the deployment reports it serving but not ready."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
                resources={
                    "replica-cluster-a-0": _observed_replica(ready=False, serving=True),
                    "endpoint-cluster-a-0": _observed_endpoint(),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=2,
                            cache_storage=False,
                            placement_labels=None,
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(items=[_observed_replica(ready=None, serving=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_FALSE),
                resources={
                    "replica-cluster-a-0": _composed_replica(
                        name="my-model-5ab63",
                        cluster="cluster-a",
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        },
                        model_cache=None,
                        serving_mode=None,
                        endpoint_picker=None,
                        sidecar=None,
                        engines=[{"name": "main"}],
                        args=["--model=Qwen/Qwen3-0.6B"],
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "endpoint-cluster-a-0": _composed_endpoint(
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        }
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="ReplicasCreated",
                    message="Scheduled 1 of 1 replicas",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Serving",
                    message="1 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Degraded",
                    message="0 of 1 replicas ready",
                ),
            ],
        ),
    ),
    # Either the missing hostname or the missing conditions alone would hold
    # back the endpoint, so this pins neither. NoGatewayHostname pins the
    # hostname.
    ComposeCase(
        name="ClusterOffline",
        reason="A replica stays on its cluster while the cluster is offline, and gets no endpoint, with no gateway hostname on the cluster and no conditions on the replica.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
                resources={
                    "replica-cluster-a-0": _observed_replica(ready=None, serving=None),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=False,
                            gateway_hostname=None,
                            nodes=2,
                            cache_storage=False,
                            placement_labels=None,
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(items=[_observed_replica(ready=None, serving=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_FALSE),
                resources={
                    "replica-cluster-a-0": _composed_replica(
                        name="my-model-5ab63",
                        cluster="cluster-a",
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        },
                        model_cache=None,
                        serving_mode=None,
                        endpoint_picker=None,
                        sidecar=None,
                        engines=[{"name": "main"}],
                        args=["--model=Qwen/Qwen3-0.6B"],
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="ReplicasCreated",
                    message="Scheduled 1 of 1 replicas",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas ready",
                ),
            ],
        ),
    ),
    ComposeCase(
        name="ClusterDeleted",
        reason="A replica whose cluster no longer exists is re-placed on another cluster.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
                resources={
                    "replica-cluster-a-0": _observed_replica(ready=True, serving=True),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-b",
                            ready=True,
                            gateway_hostname="cluster-b.clusters.example.com",
                            nodes=2,
                            cache_storage=False,
                            placement_labels=None,
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(items=[_observed_replica(ready=None, serving=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_FALSE),
                resources={
                    "replica-cluster-b-0": _composed_replica(
                        name="my-model-f0b76",
                        cluster="cluster-b",
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-b",
                            "modelplane.ai/replica-index": "0",
                        },
                        model_cache=None,
                        serving_mode=None,
                        endpoint_picker=None,
                        sidecar=None,
                        engines=[{"name": "main"}],
                        args=["--model=Qwen/Qwen3-0.6B"],
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Scheduled 1 replicas across 1 clusters: cluster-b",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Scheduling",
                    message="Created 0 of 1 replicas",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas ready",
                ),
            ],
        ),
    ),
    ComposeCase(
        name="ModelCacheRef",
        reason="A deployment's modelCacheRef is copied onto its replica, and ModelCacheResolved reports the cache resolved.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache="qwen",
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=2,
                            cache_storage=True,
                            placement_labels=None,
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(),
                "cache": fnv1.Resources(items=[_cache(cluster_selector=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_FALSE),
                resources={
                    "replica-cluster-a-0": _composed_replica(
                        name="my-model-5ab63",
                        cluster="cluster-a",
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        },
                        model_cache="qwen",
                        serving_mode=None,
                        endpoint_picker=None,
                        sidecar=None,
                        engines=[{"name": "main"}],
                        args=["--model=Qwen/Qwen3-0.6B"],
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Scheduled 1 replicas across 1 clusters: cluster-a",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                    "cache": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelCache",
                        match_name="qwen",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ModelCacheResolved", status=fnv1.STATUS_CONDITION_TRUE, reason="ModelCacheResolved"
                ),
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Scheduling",
                    message="Created 0 of 1 replicas",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas ready",
                ),
            ],
        ),
    ),
    # The cache stages only onto the clusters its selector matches, so a
    # replica must never land where the cache isn't.
    ComposeCase(
        name="CacheClusterSelector",
        reason="The clusters requirement selects the labels of both the deployment's and the cache's clusterSelector.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=v1alpha1.ClusterSelector(matchLabels={"region": "us-east"}),
                    model_cache="qwen",
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=2,
                            cache_storage=True,
                            placement_labels=None,
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(),
                "cache": fnv1.Resources(items=[_cache(cluster_selector={"matchLabels": {"tier": "gpu"}})]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_FALSE),
                resources={
                    "replica-cluster-a-0": _composed_replica(
                        name="my-model-5ab63",
                        cluster="cluster-a",
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        },
                        model_cache="qwen",
                        serving_mode=None,
                        endpoint_picker=None,
                        sidecar=None,
                        engines=[{"name": "main"}],
                        args=["--model=Qwen/Qwen3-0.6B"],
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Scheduled 1 replicas across 1 clusters: cluster-a",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_labels=fnv1.MatchLabels(labels={"region": "us-east", "tier": "gpu"}),
                    ),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                    "cache": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelCache",
                        match_name="qwen",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ModelCacheResolved", status=fnv1.STATUS_CONDITION_TRUE, reason="ModelCacheResolved"
                ),
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Scheduling",
                    message="Created 0 of 1 replicas",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas ready",
                ),
            ],
        ),
    ),
    # compose-model-cache stages only onto clusters that report cache
    # storage, so a cluster that reports none can't host the replica's PVC.
    ComposeCase(
        name="CacheStorageRequired",
        reason="A cached replica skips cluster-a, which has no cache storage, for cluster-b, though cluster-a would win the tiebreak by name.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache="qwen",
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=2,
                            cache_storage=False,
                            placement_labels=None,
                        ),
                        _cluster(
                            name="cluster-b",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=2,
                            cache_storage=True,
                            placement_labels=None,
                        ),
                    ]
                ),
                "all-replicas": fnv1.Resources(),
                "cache": fnv1.Resources(items=[_cache(cluster_selector=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_FALSE),
                resources={
                    "replica-cluster-b-0": _composed_replica(
                        name="my-model-f0b76",
                        cluster="cluster-b",
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-b",
                            "modelplane.ai/replica-index": "0",
                        },
                        model_cache="qwen",
                        serving_mode=None,
                        endpoint_picker=None,
                        sidecar=None,
                        engines=[{"name": "main"}],
                        args=["--model=Qwen/Qwen3-0.6B"],
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Scheduled 1 replicas across 1 clusters: cluster-b",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                    "cache": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelCache",
                        match_name="qwen",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ModelCacheResolved", status=fnv1.STATUS_CONDITION_TRUE, reason="ModelCacheResolved"
                ),
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Scheduling",
                    message="Created 0 of 1 replicas",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas ready",
                ),
            ],
        ),
    ),
    # A replica placed before #186 was fixed, or before its cluster lost cache
    # storage, can be running on a cluster without it. Its PVC never appears,
    # so it's re-placed the way a replica is when the cache's selector stops
    # matching.
    ComposeCase(
        name="RunningWithoutStorage",
        reason="A cached replica running on a cluster without cache storage is re-placed on a cluster with it.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache="qwen",
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
                resources={
                    "replica-cluster-a-0": _observed_replica(ready=True, serving=True),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=2,
                            cache_storage=False,
                            placement_labels=None,
                        ),
                        _cluster(
                            name="cluster-b",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=2,
                            cache_storage=True,
                            placement_labels=None,
                        ),
                    ]
                ),
                "all-replicas": fnv1.Resources(items=[_observed_replica(ready=None, serving=None)]),
                "cache": fnv1.Resources(items=[_cache(cluster_selector=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_FALSE),
                resources={
                    "replica-cluster-b-0": _composed_replica(
                        name="my-model-f0b76",
                        cluster="cluster-b",
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-b",
                            "modelplane.ai/replica-index": "0",
                        },
                        model_cache="qwen",
                        serving_mode=None,
                        endpoint_picker=None,
                        sidecar=None,
                        engines=[{"name": "main"}],
                        args=["--model=Qwen/Qwen3-0.6B"],
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Scheduled 1 replicas across 1 clusters: cluster-b",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                    "cache": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelCache",
                        match_name="qwen",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ModelCacheResolved", status=fnv1.STATUS_CONDITION_TRUE, reason="ModelCacheResolved"
                ),
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Scheduling",
                    message="Created 0 of 1 replicas",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas ready",
                ),
            ],
        ),
    ),
    ComposeCase(
        name="NoCacheStorage",
        reason="When no candidate cluster has cache storage nothing is placed, and ReplicasScheduled says so rather than blaming capacity.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache="qwen",
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=2,
                            cache_storage=False,
                            placement_labels=None,
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(),
                "cache": fnv1.Resources(items=[_cache(cluster_selector=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=0, ready_replicas=0, ready=fnv1.READY_FALSE),
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                    "cache": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelCache",
                        match_name="qwen",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ModelCacheResolved", status=fnv1.STATUS_CONDITION_TRUE, reason="ModelCacheResolved"
                ),
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="NoCacheStorage",
                    message="0 of 1 replicas scheduled: no candidate cluster has storage for ModelCache qwen",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="NoReplicasScheduled",
                    message="0 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="NoReplicasScheduled",
                    message="0 of 1 replicas ready",
                ),
            ],
        ),
    ),
    # An unfetched cache leaves the footprint unknown, so with no replicas to
    # retain the function holds off placing any rather than risk landing them
    # outside it. The wait is transient and self-clearing, so it's a
    # condition, not an event. The cluster and replica requirements are still
    # declared so the cache can resolve alongside them. The request has no
    # "cache" required resource, which is what marks it unresolved.
    ComposeCase(
        name="CacheUnresolved",
        reason="Until Crossplane fetches the referenced cache no replica is placed, and ModelCacheResolved reports ModelCacheUnresolved.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache="qwen",
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=2,
                            cache_storage=False,
                            placement_labels=None,
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=0, ready_replicas=0, ready=fnv1.READY_FALSE),
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                    "cache": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelCache",
                        match_name="qwen",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ModelCacheResolved",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelCacheUnresolved",
                    message="Waiting for ModelCache qwen",
                ),
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelCacheUnresolved",
                    message="0 of 1 replicas scheduled: placement waits for ModelCache qwen",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="NoReplicasScheduled",
                    message="0 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="NoReplicasScheduled",
                    message="0 of 1 replicas ready",
                ),
            ],
        ),
    ),
    # The "cache" requirement resolves but matches nothing. The cache only
    # matters when loading weights, which already happened, so its
    # disappearance must not tear the deployment down: the scheduler keeps a
    # placed replica even while it may not place new ones.
    ComposeCase(
        name="CacheDeleted",
        reason="When the referenced cache is deleted the running replica keeps its place and endpoint, ModelCacheResolved reports ModelCacheNotFound, and the deployment isn't Ready because it can't place a replacement.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache="qwen",
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
                resources={
                    "replica-cluster-a-0": _observed_replica(ready=True, serving=True),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=2,
                            cache_storage=False,
                            placement_labels=None,
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(items=[_observed_replica(ready=None, serving=None)]),
                "cache": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=1, ready=fnv1.READY_FALSE),
                resources={
                    "replica-cluster-a-0": _composed_replica(
                        name="my-model-5ab63",
                        cluster="cluster-a",
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        },
                        model_cache="qwen",
                        serving_mode=None,
                        endpoint_picker=None,
                        sidecar=None,
                        engines=[{"name": "main"}],
                        args=["--model=Qwen/Qwen3-0.6B"],
                        ready=fnv1.READY_TRUE,
                    ),
                    "endpoint-cluster-a-0": _composed_endpoint(
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        }
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message="ModelCache qwen not found; holding replica placement",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                    "cache": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelCache",
                        match_name="qwen",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ModelCacheResolved",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelCacheNotFound",
                    message="ModelCache qwen not found; holding replica placement",
                ),
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="ReplicasCreated",
                    message="Scheduled 1 of 1 replicas",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Serving",
                    message="1 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="AllReplicasReady",
                    message="1 of 1 replicas ready",
                ),
            ],
        ),
    ),
    ComposeCase(
        name="TwoReplicasOneCluster",
        reason="Two replicas placed on one cluster compose as two distinct ModelReplicas.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=2,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=None,
                ),
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=2,
                            cache_storage=False,
                            placement_labels=None,
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=2, ready_replicas=0, ready=fnv1.READY_FALSE),
                resources={
                    "replica-cluster-a-0": _composed_replica(
                        name="my-model-5ab63",
                        cluster="cluster-a",
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        },
                        model_cache=None,
                        serving_mode=None,
                        endpoint_picker=None,
                        sidecar=None,
                        engines=[{"name": "main"}],
                        args=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "replica-cluster-a-1": _composed_replica(
                        name="my-model-609c5",
                        cluster="cluster-a",
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "1",
                        },
                        model_cache=None,
                        serving_mode=None,
                        endpoint_picker=None,
                        sidecar=None,
                        engines=[{"name": "main"}],
                        args=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Scheduled 2 replicas across 1 clusters: cluster-a",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Scheduling",
                    message="Created 0 of 2 replicas",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 2 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 2 replicas ready",
                ),
            ],
        ),
    ),
    # compose-model-replica reads serving and each engine's phase to pick
    # disaggregated routing, which role-labels the phase engines. The picker
    # and the sidecar are the user's, so they reach the replica as written.
    ComposeCase(
        name="PrefillDecode",
        reason=(
            "A PrefillDecode deployment copies its serving mode, endpoint picker, each engine's phase, and the "
            "containers beside its engines onto the replica."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode="PrefillDecode",
                    endpoint_picker={
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "name": "epp",
                                        "image": "ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0",
                                        "args": ["--pool-name=$(MODELPLANE_POOL_NAME)"],
                                    }
                                ]
                            }
                        },
                        "config": {"kind": "EndpointPickerConfig", "plugins": [{"type": "always-disagg-pd-decider"}]},
                    },
                    sidecar={
                        "name": "sidecar",
                        "image": "ghcr.io/llm-d/llm-d-router-disagg-sidecar:v0.9.0",
                        "args": ["--kv-connector=nixlv2"],
                        "ports": [{"containerPort": 8000}],
                    },
                    engines=[{"name": "prefill", "phase": "Prefill"}, {"name": "decode", "phase": "Decode"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=2,
                            cache_storage=False,
                            placement_labels=None,
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_FALSE),
                resources={
                    "replica-cluster-a-0": _composed_replica(
                        name="my-model-5ab63",
                        cluster="cluster-a",
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        },
                        model_cache=None,
                        serving_mode="PrefillDecode",
                        endpoint_picker={
                            "template": {
                                "spec": {
                                    "containers": [
                                        {
                                            "name": "epp",
                                            "image": "ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0",
                                            "args": ["--pool-name=$(MODELPLANE_POOL_NAME)"],
                                        }
                                    ]
                                }
                            },
                            "config": {
                                "kind": "EndpointPickerConfig",
                                "plugins": [{"type": "always-disagg-pd-decider"}],
                            },
                        },
                        sidecar={
                            "name": "sidecar",
                            "image": "ghcr.io/llm-d/llm-d-router-disagg-sidecar:v0.9.0",
                            "args": ["--kv-connector=nixlv2"],
                            "ports": [{"containerPort": 8000}],
                        },
                        engines=[
                            {"name": "prefill", "phase": "Prefill"},
                            {"name": "decode", "phase": "Decode"},
                        ],
                        args=["--model=Qwen/Qwen3-0.6B"],
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Scheduled 1 replicas across 1 clusters: cluster-a",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Scheduling",
                    message="Created 0 of 1 replicas",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas ready",
                ),
            ],
        ),
    ),
    # The observed, Ready replica lets the endpoint compose this reconcile.
    ComposeCase(
        name="TemplateLabels",
        reason="A deployment's template labels land on the replica and its endpoint, alongside the labels Modelplane manages.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels={"tier": "prod", "team": "search"},
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
                resources={
                    "replica-cluster-a-0": _observed_replica(ready=True, serving=True),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=2,
                            cache_storage=False,
                            placement_labels=None,
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(items=[_observed_replica(ready=None, serving=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=1, ready=fnv1.READY_UNSPECIFIED),
                resources={
                    "replica-cluster-a-0": _composed_replica(
                        name="my-model-5ab63",
                        cluster="cluster-a",
                        labels={
                            "tier": "prod",
                            "team": "search",
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        },
                        model_cache=None,
                        serving_mode=None,
                        endpoint_picker=None,
                        sidecar=None,
                        engines=[{"name": "main"}],
                        args=["--model=Qwen/Qwen3-0.6B"],
                        ready=fnv1.READY_TRUE,
                    ),
                    "endpoint-cluster-a-0": _composed_endpoint(
                        labels={
                            "tier": "prod",
                            "team": "search",
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        }
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="ReplicasCreated",
                    message="Scheduled 1 of 1 replicas",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Serving",
                    message="1 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="AllReplicasReady",
                    message="1 of 1 replicas ready",
                ),
            ],
        ),
    ),
    # The XRD's CEL rejects a template label under the modelplane.ai/
    # prefix, but the invariant lives in the function too: managed labels
    # are stamped last, so they win even if that CEL rule is relaxed or the
    # function is reused elsewhere.
    ComposeCase(
        name="ManagedLabelWins",
        reason="A template label with the same key as a managed label is overridden by the managed one.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels={"modelplane.ai/cluster": "wrong", "tier": "prod"},
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=2,
                            cache_storage=False,
                            placement_labels=None,
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_FALSE),
                resources={
                    "replica-cluster-a-0": _composed_replica(
                        name="my-model-5ab63",
                        cluster="cluster-a",
                        labels={
                            "tier": "prod",
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        },
                        model_cache=None,
                        serving_mode=None,
                        endpoint_picker=None,
                        sidecar=None,
                        engines=[{"name": "main"}],
                        args=["--model=Qwen/Qwen3-0.6B"],
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Scheduled 1 replicas across 1 clusters: cluster-a",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Scheduling",
                    message="Created 0 of 1 replicas",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas ready",
                ),
            ],
        ),
    ),
    # Placement labels are the endpoint half of residency. A ModelService
    # selects endpoints by label, so without them a region-scoped service
    # can't select its own replicas, and nobody can label them by hand because
    # Modelplane owns them. The gateway half is an InferenceGateway's
    # serviceSelector.
    ComposeCase(
        name="PlacementLabels",
        reason="A cluster's placement labels land on the replica composed there and on its endpoint.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
                resources={
                    "replica-cluster-a-0": _observed_replica(ready=True, serving=True),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=2,
                            cache_storage=False,
                            placement_labels={"example.org/region": "eu"},
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(items=[_observed_replica(ready=None, serving=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=1, ready=fnv1.READY_UNSPECIFIED),
                resources={
                    "replica-cluster-a-0": _composed_replica(
                        name="my-model-5ab63",
                        cluster="cluster-a",
                        labels={
                            "example.org/region": "eu",
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        },
                        model_cache=None,
                        serving_mode=None,
                        endpoint_picker=None,
                        sidecar=None,
                        engines=[{"name": "main"}],
                        args=["--model=Qwen/Qwen3-0.6B"],
                        ready=fnv1.READY_TRUE,
                    ),
                    "endpoint-cluster-a-0": _composed_endpoint(
                        labels={
                            "example.org/region": "eu",
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        }
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="ReplicasCreated",
                    message="Scheduled 1 of 1 replicas",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Serving",
                    message="1 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="AllReplicasReady",
                    message="1 of 1 replicas ready",
                ),
            ],
        ),
    ),
    # The cluster is the authority on where it is, so its placement
    # labels are stamped after the deployment's own template labels.
    ComposeCase(
        name="PlacementLabelWins",
        reason="A cluster's placement label overrides a template label of the same key.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels={"example.org/region": "wrong"},
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=2,
                            cache_storage=False,
                            placement_labels={"example.org/region": "eu"},
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_FALSE),
                resources={
                    "replica-cluster-a-0": _composed_replica(
                        name="my-model-5ab63",
                        cluster="cluster-a",
                        labels={
                            "example.org/region": "eu",
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        },
                        model_cache=None,
                        serving_mode=None,
                        endpoint_picker=None,
                        sidecar=None,
                        engines=[{"name": "main"}],
                        args=["--model=Qwen/Qwen3-0.6B"],
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Scheduled 1 replicas across 1 clusters: cluster-a",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Scheduling",
                    message="Created 0 of 1 replicas",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas ready",
                ),
            ],
        ),
    ),
    # The XR is a dict rather than built from the generated model, which
    # can't represent the member without template.spec that this case tests.
    ComposeCase(
        name="TemplateSpecMissing",
        reason="A stored deployment whose member has no template.spec stops with a fatal result naming it.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "apiVersion": "modelplane.ai/v1alpha1",
                            "kind": "ModelDeployment",
                            "metadata": {"name": "my-model", "namespace": "ml-team"},
                            "spec": {
                                "replicas": 1,
                                "template": {
                                    "spec": {
                                        "engines": [
                                            {
                                                "name": "main",
                                                "members": [
                                                    {
                                                        "role": "Standalone",
                                                        "nodeSelector": {
                                                            "devices": [
                                                                {
                                                                    "name": "gpu",
                                                                    "selectors": [
                                                                        {"cel": 'device.driver == "gpu.nvidia.com"'}
                                                                    ],
                                                                },
                                                            ],
                                                        },
                                                        "template": {},
                                                    },
                                                ],
                                            },
                                        ],
                                    },
                                },
                            },
                        }
                    )
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message=(
                        "The ModelDeployment doesn't match its schema: "
                        "spec.template.spec.engines[0].members[0].template.spec: Field required"
                    ),
                ),
            ],
            context=structpb.Struct(),
        ),
    ),
    ComposeCase(
        name="ForeignReplicaMalformed",
        reason="A replica in another namespace that doesn't parse is skipped with a warning that doesn't name it.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=2,
                            cache_storage=False,
                            placement_labels=None,
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(
                    items=[
                        # A dict rather than the generated model, which can't
                        # represent the member without template.spec.
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "modelplane.ai/v1alpha1",
                                    "kind": "ModelReplica",
                                    "metadata": {
                                        "name": "other-model-0a1b2",
                                        "namespace": "other-team",
                                        "labels": {"modelplane.ai/deployment": "other-model"},
                                    },
                                    "spec": {
                                        "clusterName": "cluster-a",
                                        "engines": [
                                            {
                                                "name": "main",
                                                "members": [
                                                    {
                                                        "role": "Standalone",
                                                        "nodePoolName": "gpu-pool",
                                                        "deviceRequests": [
                                                            {"name": "gpu", "deviceClassName": "gpu.nvidia.com"}
                                                        ],
                                                        "template": {},
                                                    },
                                                ],
                                            },
                                        ],
                                    },
                                }
                            )
                        ),
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_FALSE),
                resources={
                    "replica-cluster-a-0": _composed_replica(
                        name="my-model-5ab63",
                        cluster="cluster-a",
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        },
                        model_cache=None,
                        serving_mode=None,
                        endpoint_picker=None,
                        sidecar=None,
                        engines=[{"name": "main"}],
                        args=["--model=Qwen/Qwen3-0.6B"],
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message=(
                        "Ignoring a ModelReplica in another namespace that doesn't match its schema. "
                        "Its own Synced condition reports the problem."
                    ),
                ),
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Scheduled 1 replicas across 1 clusters: cluster-a",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Scheduling",
                    message="Created 0 of 1 replicas",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas ready",
                ),
            ],
        ),
    ),
    ComposeCase(
        name="LocalReplicaMalformed",
        reason="A replica in the deployment's namespace that doesn't parse is skipped with a warning naming it.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    endpoint_picker=None,
                    sidecar=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
            ),
            required_resources={
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(
                            name="cluster-a",
                            ready=True,
                            gateway_hostname="cluster.clusters.example.com",
                            nodes=2,
                            cache_storage=False,
                            placement_labels=None,
                        )
                    ]
                ),
                "all-replicas": fnv1.Resources(
                    items=[
                        # A dict rather than the generated model, which can't
                        # represent the member without template.spec.
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "modelplane.ai/v1alpha1",
                                    "kind": "ModelReplica",
                                    "metadata": {
                                        "name": "other-model-0a1b2",
                                        "namespace": "ml-team",
                                        "labels": {"modelplane.ai/deployment": "other-model"},
                                    },
                                    "spec": {
                                        "clusterName": "cluster-a",
                                        "engines": [
                                            {
                                                "name": "main",
                                                "members": [
                                                    {
                                                        "role": "Standalone",
                                                        "nodePoolName": "gpu-pool",
                                                        "deviceRequests": [
                                                            {"name": "gpu", "deviceClassName": "gpu.nvidia.com"}
                                                        ],
                                                        "template": {},
                                                    },
                                                ],
                                            },
                                        ],
                                    },
                                }
                            )
                        ),
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_FALSE),
                resources={
                    "replica-cluster-a-0": _composed_replica(
                        name="my-model-5ab63",
                        cluster="cluster-a",
                        labels={
                            "modelplane.ai/deployment": "my-model",
                            "modelplane.ai/cluster": "cluster-a",
                            "modelplane.ai/replica-index": "0",
                        },
                        model_cache=None,
                        serving_mode=None,
                        endpoint_picker=None,
                        sidecar=None,
                        engines=[{"name": "main"}],
                        args=["--model=Qwen/Qwen3-0.6B"],
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message=(
                        "Ignoring ModelReplica other-model-0a1b2: it doesn't match its schema: "
                        "spec.engines[0].members[0].template.spec: Field required"
                    ),
                ),
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Scheduled 1 replicas across 1 clusters: cluster-a",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "all-replicas": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica"),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Scheduling",
                    message="Created 0 of 1 replicas",
                ),
                fnv1.Condition(
                    type="ReplicasServing",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas serving",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 replicas ready",
                ),
            ],
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: ComposeCase) -> None:
    """RunFunction fans out ModelReplicas and, once they're Ready, ModelEndpoints."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason


RESOLVE_REQUIRED_CASES = [
    ResolveRequiredCase(
        name="Present",
        reason="A requirement that resolved and matched a resource is PRESENT, with that resource.",
        req=fnv1.RunFunctionRequest(
            required_resources={
                "cache": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "modelplane.ai/v1alpha1",
                                    "kind": "ModelCache",
                                    "metadata": {"name": "qwen"},
                                }
                            )
                        )
                    ]
                ),
            },
        ),
        requirement="cache",
        want=(
            fn.Resolution.PRESENT,
            {"apiVersion": "modelplane.ai/v1alpha1", "kind": "ModelCache", "metadata": {"name": "qwen"}},
        ),
    ),
    # The SDK returns None for this and for a requirement Crossplane hasn't
    # fetched alike. Only the requirement's key tells them apart.
    ResolveRequiredCase(
        name="Absent",
        reason="A requirement that resolved but matched nothing is ABSENT.",
        req=fnv1.RunFunctionRequest(required_resources={"cache": fnv1.Resources()}),
        requirement="cache",
        want=(fn.Resolution.ABSENT, None),
    ),
    ResolveRequiredCase(
        name="Unresolved",
        reason="A requirement Crossplane hasn't fetched is UNRESOLVED.",
        req=fnv1.RunFunctionRequest(),
        requirement="cache",
        want=(fn.Resolution.UNRESOLVED, None),
    ),
]


@pytest.mark.parametrize("case", RESOLVE_REQUIRED_CASES, ids=lambda case: case.name)
def test_resolve_required(case: ResolveRequiredCase) -> None:
    """resolve_required tells a found, a missing, and an unfetched requirement apart."""
    assert fn.resolve_required(case.req, case.requirement) == case.want, case.reason


INJECT_NAME_CASES = [
    # Env expansion is left to right, so an arg or a later entry
    # referencing $(MODELPLANE_SERVED_MODEL_NAME) only resolves if it's
    # first.
    InjectNameCase(
        name="UserEnv",
        reason="The served model name goes ahead of the env the user wrote.",
        template=mrv1alpha1.Template(
            spec=mrv1alpha1.Spec(
                containers=[
                    mrv1alpha1.Container(
                        name="engine",
                        image="vllm/vllm-openai:latest",
                        env=[mrv1alpha1.EnvItem(name="HF_TOKEN", value="x")],
                    )
                ]
            )
        ),
        served="ml-team/kimi-k2",
        want=mrv1alpha1.Template(
            spec=mrv1alpha1.Spec(
                containers=[
                    mrv1alpha1.Container(
                        name="engine",
                        image="vllm/vllm-openai:latest",
                        env=[
                            mrv1alpha1.EnvItem(name="MODELPLANE_SERVED_MODEL_NAME", value="ml-team/kimi-k2"),
                            mrv1alpha1.EnvItem(name="HF_TOKEN", value="x"),
                        ],
                    )
                ]
            )
        ),
    ),
    # Modelplane decides this value. Honouring an override would let the
    # engine answer to a name nothing routes to, which surfaces as a 404
    # from the engine rather than anything visible in status.
    InjectNameCase(
        name="UserOverride",
        reason="A MODELPLANE_SERVED_MODEL_NAME the user wrote is replaced by the served model name.",
        template=mrv1alpha1.Template(
            spec=mrv1alpha1.Spec(
                containers=[
                    mrv1alpha1.Container(
                        name="engine",
                        image="vllm/vllm-openai:latest",
                        env=[mrv1alpha1.EnvItem(name="MODELPLANE_SERVED_MODEL_NAME", value="mine")],
                    )
                ]
            )
        ),
        served="ml-team/kimi-k2",
        want=mrv1alpha1.Template(
            spec=mrv1alpha1.Spec(
                containers=[
                    mrv1alpha1.Container(
                        name="engine",
                        image="vllm/vllm-openai:latest",
                        env=[mrv1alpha1.EnvItem(name="MODELPLANE_SERVED_MODEL_NAME", value="ml-team/kimi-k2")],
                    )
                ]
            )
        ),
    ),
]


@pytest.mark.parametrize("case", INJECT_NAME_CASES, ids=lambda case: case.name)
def test_inject_name(case: InjectNameCase) -> None:
    """_inject_served_model_name puts the served model name first in each container's env."""
    got = case.template.model_copy(deep=True)  # noqa: MPT401  # _inject_served_model_name edits it in place.
    fn._inject_served_model_name(got, case.served)
    assert got.model_dump() == case.want.model_dump(), case.reason


SERVED_MODEL_NAME_CASES = [
    # So two deployments in different namespaces can't collide, and a
    # ModelService can rewrite one name for a whole deployment.
    ServedModelNameCase(
        name="Namespaced",
        reason="A deployment's served model name is its namespace and name, joined by a slash.",
        namespace="ml-team",
        deployment="kimi-k2",
        want="ml-team/kimi-k2",
    ),
]


@pytest.mark.parametrize("case", SERVED_MODEL_NAME_CASES, ids=lambda case: case.name)
def test_served_model_name(case: ServedModelNameCase) -> None:
    """served_model_name prefixes the deployment's name with its namespace."""
    assert fn.served_model_name(case.namespace, case.deployment) == case.want, case.reason
