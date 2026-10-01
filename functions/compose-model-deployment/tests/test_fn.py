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
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


@dataclasses.dataclass
class ResolveRequiredCase:
    """A test case for fn.resolve_required."""

    name: str
    req: fnv1.RunFunctionRequest
    # resolve_required's name parameter, renamed so it doesn't collide with
    # the case's own name.
    requirement: str
    want: tuple[fn.Resolution, dict | None]


@dataclasses.dataclass
class InjectServedModelNameCase:
    """A test case for fn._inject_served_model_name."""

    name: str
    template: mrv1alpha1.Template
    served: str
    want: mrv1alpha1.Template


@dataclasses.dataclass
class ServedModelNameCase:
    """A test case for fn.served_model_name."""

    name: str
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
    engines: list[dict[str, Any]],
    args: list[str] | None,
) -> fnv1.Resource:
    """The observed ModelDeployment my-model, each of its engines running one Standalone GPU member."""
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
                containers=[v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest", args=args)],
            ),
        ),
    )
    xr = v1alpha1.ModelDeployment(
        metadata=metav1.ObjectMeta(name="my-model", namespace="ml-team"),
        spec=v1alpha1.SpecModel1(
            replicas=replicas,
            template=v1alpha1.TemplateModel(
                metadata=v1alpha1.Metadata(labels=template_labels) if template_labels is not None else None,
                spec=v1alpha1.SpecModel(
                    clusterSelector=cluster_selector,
                    modelCacheRef=v1alpha1.ModelCacheRef(name=model_cache) if model_cache is not None else None,
                    serving=v1alpha1.Serving(mode=serving_mode) if serving_mode is not None else None,
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
    """An observed InferenceCluster with one pool of single-GPU nodes."""
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


def _observed_replica(*, ready: bool | None) -> fnv1.Resource:
    """The ModelReplica my-model has on cluster-a, with no Ready condition if ready is None."""
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
                }
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
        "template": {"spec": {"containers": [container]}},
    }
    spec: dict[str, Any] = {"clusterName": cluster}
    if model_cache is not None:
        spec["modelCacheRef"] = {"name": model_cache}
    if serving_mode is not None:
        spec["serving"] = {"mode": serving_mode}
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
    # First reconcile: the replica is composed but not yet observed
    # Ready, so its endpoint is withheld - routing must not advertise
    # a backend whose pods are still warming up (#102).
    ComposeCase(
        name="freshly scheduled replica composes no endpoint until ready",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
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
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_UNSPECIFIED),
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
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 ready",
                ),
            ],
        ),
    ),
    # A replica that has gone not-Ready (e.g. a crash-loop after once
    # serving) has its endpoint withdrawn: the previously observed
    # endpoint is absent from desired, so Crossplane deletes it and
    # traffic stops routing to the dead backend (#102). Omitting it from
    # desired - not composing it - is what drives the deletion.
    ComposeCase(
        name="not-ready replica withdraws its endpoint",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
                resources={
                    "replica-cluster-a-0": _observed_replica(ready=False),
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
                "all-replicas": fnv1.Resources(items=[_observed_replica(ready=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_UNSPECIFIED),
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
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 ready",
                ),
            ],
        ),
    ),
    ComposeCase(
        name="no clusters produces warning",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
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
        name="insufficient capacity produces no replicas",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
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
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="NoReplicasScheduled",
                ),
            ],
        ),
    ),
    # Zero desired parks the deployment before resolve_inputs runs: no
    # requirements are declared (the want carries none), nothing is
    # composed, and both conditions read True with the NoReplicasDesired
    # reason rather than a capacity failure.
    ComposeCase(
        name="scaled to zero composes nothing and reports NoReplicasDesired",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=0,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
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
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="NoReplicasDesired",
                    message="0 replicas desired",
                ),
            ],
        ),
    ),
    # Scaling an existing deployment to zero: the observed replica and
    # endpoint are absent from desired (pruned), and the transition is
    # announced while they still exist.
    ComposeCase(
        name="scale to zero prunes observed replicas and emits an event",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=0,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
                resources={
                    "replica-cluster-a-0": _observed_replica(ready=True),
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
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="NoReplicasDesired",
                    message="0 replicas desired",
                ),
            ],
        ),
    ),
    ComposeCase(
        name="ready replica is preserved and keeps its endpoint",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
                resources={
                    "replica-cluster-a-0": _observed_replica(ready=True),
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
                "all-replicas": fnv1.Resources(items=[_observed_replica(ready=None)]),
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
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="AllReplicasReady",
                    message="1 of 1 ready",
                ),
            ],
        ),
    ),
    # The replica isn't observed Ready, so it gets no endpoint whatever the
    # state of its cluster.
    ComposeCase(
        name="offline pinned cluster keeps its replica",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
                resources={
                    "replica-cluster-a-0": _observed_replica(ready=None),
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
                "all-replicas": fnv1.Resources(items=[_observed_replica(ready=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_UNSPECIFIED),
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
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 ready",
                ),
            ],
        ),
    ),
    ComposeCase(
        name="deleted pinned cluster triggers replica re-placement",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
                resources={
                    "replica-cluster-a-0": _observed_replica(ready=True),
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
                "all-replicas": fnv1.Resources(items=[_observed_replica(ready=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_UNSPECIFIED),
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
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 ready",
                ),
            ],
        ),
    ),
    ComposeCase(
        name="modelCacheRef is propagated onto the composed replica",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache="qwen",
                    serving_mode=None,
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
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_UNSPECIFIED),
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
                    type="ModelCacheResolved",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="ModelCacheResolved",
                ),
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Scheduling",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 ready",
                ),
            ],
        ),
    ),
    # The cache stages only to a subset of clusters, so the clusters
    # requirement matches the labels of both the cache's clusterSelector and
    # the deployment's own, and replicas never land where the cache isn't.
    ComposeCase(
        name="cache clusterSelector is intersected with the deployment's",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=v1alpha1.ClusterSelector(matchLabels={"region": "us-east"}),
                    model_cache="qwen",
                    serving_mode=None,
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
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_UNSPECIFIED),
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
                    type="ModelCacheResolved",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="ModelCacheResolved",
                ),
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Scheduling",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 ready",
                ),
            ],
        ),
    ),
    # compose-model-cache stages only onto clusters that report cache
    # storage, so cluster-a, which reports none, can't host the replica's
    # PVC. The replica lands on cluster-b, though cluster-a would win the
    # tiebreak by name.
    ComposeCase(
        name="a cached replica lands only on a cluster with cache storage",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache="qwen",
                    serving_mode=None,
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
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_UNSPECIFIED),
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
                    type="ModelCacheResolved",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="ModelCacheResolved",
                ),
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Scheduling",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 ready",
                ),
            ],
        ),
    ),
    # A replica is running on cluster-a, which has no cache storage, say
    # because the bug this guards against put it there. Its PVC never
    # appears, so it's dropped and re-placed on cluster-b, the way a
    # replica is when the cache's selector stops matching.
    ComposeCase(
        name="a running cached replica on a cluster without cache storage is re-placed",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache="qwen",
                    serving_mode=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
                resources={
                    "replica-cluster-a-0": _observed_replica(ready=True),
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
                "all-replicas": fnv1.Resources(items=[_observed_replica(ready=None)]),
                "cache": fnv1.Resources(items=[_cache(cluster_selector=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_UNSPECIFIED),
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
                    type="ModelCacheResolved",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="ModelCacheResolved",
                ),
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Scheduling",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 ready",
                ),
            ],
        ),
    ),
    # The only candidate has no cache storage, so the cache can't stage
    # there and nothing is placed. ReplicasScheduled says why rather than
    # blaming capacity.
    ComposeCase(
        name="no candidate with cache storage places nothing",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache="qwen",
                    serving_mode=None,
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
                    type="ModelCacheResolved",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="ModelCacheResolved",
                ),
                fnv1.Condition(
                    type="ReplicasScheduled",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="NoCacheStorage",
                    message="0 of 1 replicas scheduled: no candidate cluster has storage for ModelCache qwen",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="NoReplicasScheduled",
                ),
            ],
        ),
    ),
    # A referenced cache Crossplane hasn't fetched yet leaves the
    # footprint unknown. With no replicas to retain, the function holds
    # off placing any rather than risk landing them outside the
    # footprint: fill is suppressed, so nothing is composed, and
    # ModelCacheResolved=False (ModelCacheUnresolved) says why. The wait is
    # transient and self-clearing, so it's a condition, not an event. The
    # cluster and replica requirements are still declared so the cache
    # can resolve alongside them. The request has no "cache" requirement
    # at all, which is what marks it unresolved.
    ComposeCase(
        name="unresolved cache suppresses new placement",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache="qwen",
                    serving_mode=None,
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
                    reason="InsufficientCapacity",
                    message="0 of 1 replicas scheduled (checked 1 clusters)",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="NoReplicasScheduled",
                ),
            ],
        ),
    ),
    # The cache a live deployment depends on is deleted: the "cache"
    # requirement resolves but matches nothing - ABSENT. The cache only
    # matters when loading weights, which already happened, so its
    # disappearance must not tear the deployment down: the existing
    # replica is retained (retain ignores fill) even as
    # ModelCacheResolved goes False (ModelCacheNotFound) and new placement is
    # suppressed.
    ComposeCase(
        name="deleted cache retains existing replicas",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache="qwen",
                    serving_mode=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
                resources={
                    "replica-cluster-a-0": _observed_replica(ready=True),
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
                "all-replicas": fnv1.Resources(items=[_observed_replica(ready=None)]),
                "cache": fnv1.Resources(),
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
                        model_cache="qwen",
                        serving_mode=None,
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
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="AllReplicasReady",
                    message="1 of 1 ready",
                ),
            ],
        ),
    ),
    ComposeCase(
        name="two replicas co-locate on one cluster as distinct resources",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=2,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
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
                composite=_desired_model_deployment(total_replicas=2, ready_replicas=0, ready=fnv1.READY_UNSPECIFIED),
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
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 2 ready",
                ),
            ],
        ),
    ),
    # PrefillDecode copies serving and each engine's phase onto the
    # replica. compose-model-replica reads them to pick disaggregated
    # routing, which role-labels the phase engines and puts the pd-sidecar
    # on decode.
    ComposeCase(
        name="PrefillDecode copies serving and engine phases onto the replica",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode="PrefillDecode",
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
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_UNSPECIFIED),
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
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 ready",
                ),
            ],
        ),
    ),
    # spec.template.metadata.labels land on the composed ModelReplica and
    # ModelEndpoint, alongside the labels Modelplane manages. The
    # observed, Ready replica lets the endpoint compose this reconcile.
    ComposeCase(
        name="template labels are stamped on the replica and endpoint",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels={"tier": "prod", "team": "search"},
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
                resources={
                    "replica-cluster-a-0": _observed_replica(ready=True),
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
                "all-replicas": fnv1.Resources(items=[_observed_replica(ready=None)]),
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
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="AllReplicasReady",
                    message="1 of 1 ready",
                ),
            ],
        ),
    ),
    # The XRD's CEL rejects a template label under the modelplane.ai/
    # prefix, but the invariant lives in the function too: managed labels
    # are stamped last, so a colliding label can't override them even if
    # that CEL rule is relaxed or the function is reused elsewhere.
    ComposeCase(
        name="a managed label beats a template label of the same key",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels={"modelplane.ai/cluster": "wrong", "tier": "prod"},
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
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
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_UNSPECIFIED),
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
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 ready",
                ),
            ],
        ),
    ),
    # A cluster's spec.placement.metadata.labels land on the ModelReplica
    # and ModelEndpoint composed there. This is the endpoint half of
    # residency: a ModelService selects endpoints by label, so without it
    # a region-scoped service can't select its own replicas, and nobody
    # can label them by hand because Modelplane owns them. The gateway
    # half is an InferenceGateway's serviceSelector.
    ComposeCase(
        name="placement labels are stamped on the replica and endpoint",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels=None,
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
                    engines=[{"name": "main"}],
                    args=["--model=Qwen/Qwen3-0.6B"],
                ),
                resources={
                    "replica-cluster-a-0": _observed_replica(ready=True),
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
                "all-replicas": fnv1.Resources(items=[_observed_replica(ready=None)]),
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
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="AllReplicasReady",
                    message="1 of 1 ready",
                ),
            ],
        ),
    ),
    # The cluster is the authority on where it is, so its placement
    # labels are stamped after the deployment's own template labels.
    ComposeCase(
        name="a cluster's placement label beats a template label of the same key",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_deployment(
                    replicas=1,
                    template_labels={"example.org/region": "wrong"},
                    cluster_selector=None,
                    model_cache=None,
                    serving_mode=None,
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
                composite=_desired_model_deployment(total_replicas=1, ready_replicas=0, ready=fnv1.READY_UNSPECIFIED),
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
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 1 ready",
                ),
            ],
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: ComposeCase) -> None:
    """RunFunction fans out ModelReplicas and, once they're Ready, ModelEndpoints."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want)


RESOLVE_REQUIRED_CASES = [
    ResolveRequiredCase(
        name="a requirement that resolved and matched a resource is present",
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
        name="a requirement that resolved but matched nothing is absent",
        req=fnv1.RunFunctionRequest(required_resources={"cache": fnv1.Resources()}),
        requirement="cache",
        want=(fn.Resolution.ABSENT, None),
    ),
    ResolveRequiredCase(
        name="a requirement Crossplane hasn't fetched is unresolved",
        req=fnv1.RunFunctionRequest(),
        requirement="cache",
        want=(fn.Resolution.UNRESOLVED, None),
    ),
]


@pytest.mark.parametrize("case", RESOLVE_REQUIRED_CASES, ids=lambda case: case.name)
def test_resolve_required(case: ResolveRequiredCase) -> None:
    """resolve_required tells a found, a missing, and an unfetched requirement apart."""
    assert fn.resolve_required(case.req, case.requirement) == case.want


INJECT_SERVED_MODEL_NAME_CASES = [
    # Env expansion is left to right, so an arg or a later entry
    # referencing $(MODELPLANE_SERVED_MODEL_NAME) only resolves if it's
    # first.
    InjectServedModelNameCase(
        name="the served model name goes ahead of the user's env",
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
    InjectServedModelNameCase(
        name="a user override is dropped",
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


@pytest.mark.parametrize("case", INJECT_SERVED_MODEL_NAME_CASES, ids=lambda case: case.name)
def test_inject_served_model_name(case: InjectServedModelNameCase) -> None:
    """_inject_served_model_name puts the served model name first in each container's env."""
    # _inject_served_model_name edits the template in place, so give it a copy
    # and leave the table as written.
    got = case.template.model_copy(deep=True)
    fn._inject_served_model_name(got, case.served)
    assert got.model_dump() == case.want.model_dump()


SERVED_MODEL_NAME_CASES = [
    # So two deployments in different namespaces can't collide, and a
    # ModelService can rewrite one name for a whole deployment.
    ServedModelNameCase(
        name="the served model name is namespaced",
        namespace="ml-team",
        deployment="kimi-k2",
        want="ml-team/kimi-k2",
    ),
]


@pytest.mark.parametrize("case", SERVED_MODEL_NAME_CASES, ids=lambda case: case.name)
def test_served_model_name(case: ServedModelNameCase) -> None:
    """served_model_name prefixes the deployment's name with its namespace."""
    assert fn.served_model_name(case.namespace, case.deployment) == case.want
