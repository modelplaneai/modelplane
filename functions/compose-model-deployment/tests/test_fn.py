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
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelcache import v1alpha1 as mcv1alpha1
from models.ai.modelplane.modeldeployment import v1alpha1
from models.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# The selector used on the deployment's single GPU request, echoed verbatim
# into each resolved device request.
_GPU_CEL = 'device.driver == "gpu.nvidia.com"'

# A fixed transition time keeps observed conditions deterministic.
_TRANSITION_TIME = datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)

# The resolved DRA device requests the scheduler stamps onto each ModelReplica
# member, derived from the deployment's nodeSelector matched against the
# cluster's GPU device (deviceClassName gpu.nvidia.com).
_DEVICE_REQUESTS = [
    {
        "name": "gpu",
        "deviceClassName": "gpu.nvidia.com",
        "count": 1,
        "selectors": [{"cel": _GPU_CEL}],
    }
]

# The single Standalone-member engine every fixture deployment uses.
_ENGINE = v1alpha1.Engine(
    name="main",
    members=[
        v1alpha1.Member(
            role="Standalone",
            nodeSelector=v1alpha1.NodeSelector(
                devices=[v1alpha1.Device(name="gpu", count=1, selectors=[v1alpha1.Selector(cel=_GPU_CEL)])],
            ),
            template=v1alpha1.Template(
                spec=v1alpha1.Spec(
                    containers=[
                        v1alpha1.Container(
                            name="engine",
                            image="vllm/vllm-openai:latest",
                            args=["--model=Qwen/Qwen3-0.6B"],
                        ),
                    ],
                ),
            ),
        ),
    ],
)

# A Standalone engine with no container args, for the co-location case.
_ENGINE_NO_ARGS = v1alpha1.Engine(
    name="main",
    members=[
        v1alpha1.Member(
            role="Standalone",
            nodeSelector=v1alpha1.NodeSelector(
                devices=[v1alpha1.Device(name="gpu", count=1, selectors=[v1alpha1.Selector(cel=_GPU_CEL)])],
            ),
            template=v1alpha1.Template(
                spec=v1alpha1.Spec(containers=[v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")]),
            ),
        ),
    ],
)


def _replica_engines(*, args: bool = True) -> list:
    """The composed ModelReplica's spec.engines: one engine whose Standalone
    member carries the matched pool and its resolved device requests.

    args toggles the engine container's --model arg, matching the fixture
    deployment a want is built from.

    Every container carries MODELPLANE_SERVED_MODEL_NAME, ahead of any env the
    user wrote, so an arg can reference it. It's how an engine comes up under the
    name Modelplane routes to instead of Modelplane having to be told what the
    engine was started with.
    """
    container: dict[str, Any] = {
        "name": "engine",
        "image": "vllm/vllm-openai:latest",
        "env": [{"name": "MODELPLANE_SERVED_MODEL_NAME", "value": "ml-team/my-model"}],
    }
    if args:
        container["args"] = ["--model=Qwen/Qwen3-0.6B"]
    return [
        {
            "name": "main",
            "copies": 1,
            "members": [
                {
                    # No worker block: it's only set on Worker members, and
                    # the XRD deliberately has no schema default (defaults
                    # apply before CEL validation, which forbids worker on a
                    # Standalone).
                    "role": "Standalone",
                    "nodePoolName": "default",
                    "deviceRequests": _DEVICE_REQUESTS,
                    "template": {"spec": {"containers": [container]}},
                }
            ],
        }
    ]


# The composed spec.engines for the args-bearing fixture deployment, shared by
# most wants below.
_REPLICA_ENGINES = _replica_engines()
_REPLICA_ENGINES_NO_ARGS = _replica_engines(args=False)

# The composed spec.engines for a PrefillDecode deployment: the standard
# single-GPU Standalone engine, one marked Prefill and one Decode.
_PD_REPLICA_ENGINES = [
    {**_replica_engines()[0], "name": "prefill", "phase": "Prefill"},
    {**_replica_engines()[0], "name": "decode", "phase": "Decode"},
]

# A one-replica deployment requesting a single GPU. Reused across most cases.
_XR = v1alpha1.ModelDeployment(
    metadata=metav1.ObjectMeta(name="my-model", namespace="ml-team"),
    spec=v1alpha1.SpecModel1(
        replicas=1,
        template=v1alpha1.TemplateModel(spec=v1alpha1.SpecModel(engines=[_ENGINE])),
    ),
).model_dump(exclude_none=True, mode="json")


def _cluster(
    name: str,
    *,
    ready: bool = True,
    hostname: str | None = "cluster.clusters.example.com",
    nodes: int = 2,
    placement_labels: dict[str, str] | None = None,
) -> dict:
    """An InferenceCluster input fixture, dumped to a dict.

    A ready cluster has a Ready=True condition and a gateway hostname. ready=False
    flips the condition to Unavailable; hostname=None drops the gateway entirely
    (mirroring an offline cluster). nodes=0 yields a pool with no capacity.
    """
    return icv1alpha1.InferenceCluster(
        metadata=metav1.ObjectMeta(name=name),
        spec=icv1alpha1.Spec(
            cluster=icv1alpha1.Cluster(
                source="Existing",
                existing=icv1alpha1.Existing(secretRef=icv1alpha1.SecretRef(name="k")),
            ),
            placement=(
                icv1alpha1.Placement(metadata=icv1alpha1.Metadata(labels=placement_labels))
                if placement_labels
                else None
            ),
        ),
        status=icv1alpha1.Status(
            conditions=[
                icv1alpha1.Condition(
                    type="Ready",
                    status="True" if ready else "False",
                    reason="Available" if ready else "Unavailable",
                    lastTransitionTime=_TRANSITION_TIME,
                )
            ],
            gateway=icv1alpha1.Gateway(address="10.0.0.1", hostname=hostname) if hostname else None,
            providerConfigRef=icv1alpha1.ProviderConfigRef(name=name),
            gpuPools=[
                icv1alpha1.GpuPool(
                    name="default",
                    nodes=nodes,
                    devices=[
                        icv1alpha1.Device(
                            name="gpu",
                            claim="DRA",
                            driver="gpu.nvidia.com",
                            deviceClassName="gpu.nvidia.com",
                            count=1,
                        )
                    ],
                )
            ],
        ),
    ).model_dump(exclude_none=True, mode="json")


# A ready cluster with a two-node GPU pool. Reused across most cases.
_CLUSTER_A = _cluster("cluster-a")


def _cache(name: str, *, match_labels: dict[str, str] | None = None) -> dict:
    """Build an observed ModelCache in the ml-team namespace.

    match_labels, when given, sets spec.clusterSelector.matchLabels - the
    footprint the deployment scheduler intersects with its own selector.
    """
    selector = mcv1alpha1.ClusterSelector(matchLabels=match_labels) if match_labels else None
    return mcv1alpha1.ModelCache(
        metadata=metav1.ObjectMeta(name=name, namespace="ml-team"),
        spec=mcv1alpha1.Spec(
            source="HuggingFace",
            huggingFace=mcv1alpha1.HuggingFace(repo="Qwen/Qwen2.5-7B", sizeGiB=20),
            clusterSelector=selector,
        ),
    ).model_dump(exclude_none=True, mode="json")


def _replica_status(replica: dict, *, ready: bool) -> dict:
    """Return a copy of an observed ModelReplica with a Ready condition.

    compose_endpoints gates each ModelEndpoint on its ModelReplica reporting
    Ready=True - the replica's engines are serving and its remote Service and
    HTTPRoute exist - so the endpoint never advertises a backend still warming
    up (#102). Tests stamp the condition on the observed replica to drive that
    gate.
    """
    replica = dict(replica)
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
    return replica


# An existing ModelReplica pinned to cluster-a, observed across cases 5 and 6.
_EXISTING_REPLICA = mrv1alpha1.ModelReplica(
    metadata=metav1.ObjectMeta(
        name="my-model-5ab63",
        namespace="ml-team",
        labels={
            "modelplane.ai/deployment": "my-model",
            "modelplane.ai/cluster": "cluster-a",
            "modelplane.ai/replica-index": "0",
        },
    ),
    spec=mrv1alpha1.SpecModel(
        clusterName="cluster-a",
        engines=[
            mrv1alpha1.Engine(
                name="main",
                copies=1,
                members=[
                    mrv1alpha1.Member(
                        role="Standalone",
                        nodePoolName="default",
                        deviceRequests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_GPU_CEL)],
                            ),
                        ],
                        template=mrv1alpha1.Template(
                            spec=mrv1alpha1.Spec(
                                containers=[mrv1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")],
                            ),
                        ),
                    ),
                ],
            ),
        ],
    ),
).model_dump(exclude_none=True, mode="json")

# The requirements selectors every want echoes back. Both are bare selectors
# matching all resources of the kind.
_CLUSTER_SEL = fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster")
_REPLICA_SEL = fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica")


def _req(
    xr: dict,
    *,
    clusters: list[dict] | None = None,
    replicas: list[dict] | None = None,
    observed: dict | None = None,
    cache: dict | None = None,
    cache_resolved_empty: bool = False,
) -> fnv1.RunFunctionRequest:
    """Build a RunFunctionRequest with the standard required_resources.

    clusters and replicas populate the "clusters" and "all-replicas" required
    resources respectively; both keys are always present (empty when there are
    no items). cache, when given, populates the "cache" required resource the
    function declares for a deployment that sets modelCacheRef.
    cache_resolved_empty marks the "cache" requirement resolved-but-empty (the
    key present with no items, i.e. the cache doesn't exist) - distinct from
    omitting it, which leaves the requirement unresolved. observed populates
    observed.resources.
    """
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(resource=resource.dict_to_struct(xr)),
            resources={key: fnv1.Resource(resource=resource.dict_to_struct(r)) for key, r in (observed or {}).items()},
        ),
    )
    if clusters:
        for c in clusters:
            req.required_resources["clusters"].items.append(fnv1.Resource(resource=resource.dict_to_struct(c)))
    else:
        req.required_resources["clusters"].SetInParent()
    if replicas:
        for r in replicas:
            req.required_resources["all-replicas"].items.append(fnv1.Resource(resource=resource.dict_to_struct(r)))
    else:
        req.required_resources["all-replicas"].SetInParent()
    if cache is not None:
        req.required_resources["cache"].items.append(fnv1.Resource(resource=resource.dict_to_struct(cache)))
    elif cache_resolved_empty:
        req.required_resources["cache"].SetInParent()
    return req


def _want(
    resp: fnv1.RunFunctionResponse,
    *,
    cluster_labels: dict[str, str] | None = None,
    cache_name: str | None = None,
) -> fnv1.RunFunctionResponse:
    """Attach the requirements selectors a reconcile echoes back.

    cluster_labels narrows the "clusters" selector (the intersection of the
    deployment's and the referenced cache's clusterSelectors). cache_name adds
    the "cache" selector the function declares for a referenced ModelCache.
    """
    cluster_sel = fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster")
    if cluster_labels:
        cluster_sel.match_labels.labels.update(cluster_labels)
    resp.requirements.resources["clusters"].CopyFrom(cluster_sel)
    resp.requirements.resources["all-replicas"].CopyFrom(_REPLICA_SEL)
    if cache_name is not None:
        resp.requirements.resources["cache"].CopyFrom(
            fnv1.ResourceSelector(
                api_version="modelplane.ai/v1alpha1",
                kind="ModelCache",
                match_name=cache_name,
                namespace="ml-team",
            )
        )
    return resp


@dataclasses.dataclass
class Case:
    """A test case for compose-model-deployment."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    """Tests for FunctionRunner.RunFunction."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:
        """The function fans out ModelReplicas and, once they're Ready, ModelEndpoints."""

        # A deployment that sets spec.modelCacheRef.
        xr_cached = v1alpha1.ModelDeployment(
            metadata=metav1.ObjectMeta(name="my-model", namespace="ml-team"),
            spec=v1alpha1.SpecModel1(
                replicas=1,
                template=v1alpha1.TemplateModel(
                    spec=v1alpha1.SpecModel(
                        modelCacheRef=v1alpha1.ModelCacheRef(name="qwen"),
                        engines=[_ENGINE],
                    )
                ),
            ),
        ).model_dump(exclude_none=True, mode="json")

        # A cached deployment that also sets its own clusterSelector, so the
        # scheduler intersects it with the cache's footprint.
        xr_cached_selector = v1alpha1.ModelDeployment(
            metadata=metav1.ObjectMeta(name="my-model", namespace="ml-team"),
            spec=v1alpha1.SpecModel1(
                replicas=1,
                template=v1alpha1.TemplateModel(
                    spec=v1alpha1.SpecModel(
                        clusterSelector=v1alpha1.ClusterSelector(matchLabels={"region": "us-east"}),
                        modelCacheRef=v1alpha1.ModelCacheRef(name="qwen"),
                        engines=[_ENGINE],
                    )
                ),
            ),
        ).model_dump(exclude_none=True, mode="json")

        # A two-replica deployment (no container args) for the co-location case.
        xr_two = v1alpha1.ModelDeployment(
            metadata=metav1.ObjectMeta(name="my-model", namespace="ml-team"),
            spec=v1alpha1.SpecModel1(
                replicas=2,
                template=v1alpha1.TemplateModel(spec=v1alpha1.SpecModel(engines=[_ENGINE_NO_ARGS])),
            ),
        ).model_dump(exclude_none=True, mode="json")

        # A disaggregated (PrefillDecode) deployment: a Prefill and a Decode engine.
        xr_pd = v1alpha1.ModelDeployment(
            metadata=metav1.ObjectMeta(name="my-model", namespace="ml-team"),
            spec=v1alpha1.SpecModel1(
                replicas=1,
                template=v1alpha1.TemplateModel(
                    spec=v1alpha1.SpecModel(
                        serving=v1alpha1.Serving(mode="PrefillDecode"),
                        engines=[
                            _ENGINE.model_copy(update={"name": "prefill", "phase": "Prefill"}),
                            _ENGINE.model_copy(update={"name": "decode", "phase": "Decode"}),
                        ],
                    )
                ),
            ),
        ).model_dump(exclude_none=True, mode="json")

        cases = [
            Case(
                # First reconcile: the replica is composed but not yet observed
                # Ready, so its endpoint is withheld - routing must not advertise
                # a backend whose pods are still warming up (#102).
                name="freshly scheduled replica composes no endpoint until ready",
                req=_req(_XR, clusters=[_CLUSTER_A]),
                want=_want(
                    fnv1.RunFunctionResponse(
                        meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                        desired=fnv1.State(
                            composite=fnv1.Resource(
                                resource=resource.dict_to_struct({"status": {"replicas": {"total": 1, "ready": 0}}}),
                            ),
                            resources={
                                "replica-cluster-a-0": fnv1.Resource(
                                    resource=resource.dict_to_struct(
                                        {
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
                                                "engines": _REPLICA_ENGINES,
                                            },
                                        }
                                    ),
                                ),
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
                        results=[
                            fnv1.Result(
                                severity=fnv1.SEVERITY_NORMAL,
                                message="Scheduled 1 replicas across 1 clusters: cluster-a",
                            ),
                        ],
                        context=structpb.Struct(),
                    )
                ),
            ),
            Case(
                # A replica that has gone not-Ready (e.g. a crash-loop after
                # once serving) has its endpoint withdrawn: the previously
                # observed endpoint is absent from desired, so Crossplane
                # deletes it and traffic stops routing to the dead backend
                # (#102). Omitting it from desired - not composing it - is what
                # drives the deletion.
                name="not-ready replica withdraws its endpoint",
                req=_req(
                    _XR,
                    clusters=[_CLUSTER_A],
                    replicas=[_EXISTING_REPLICA],
                    observed={
                        "replica-cluster-a-0": _replica_status(_EXISTING_REPLICA, ready=False),
                        "endpoint-cluster-a-0": {
                            "apiVersion": "modelplane.ai/v1alpha1",
                            "kind": "ModelEndpoint",
                            "metadata": {"name": "my-model-5ab63", "namespace": "ml-team"},
                        },
                    },
                ),
                want=_want(
                    fnv1.RunFunctionResponse(
                        meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                        desired=fnv1.State(
                            composite=fnv1.Resource(
                                resource=resource.dict_to_struct({"status": {"replicas": {"total": 1, "ready": 0}}}),
                            ),
                            resources={
                                "replica-cluster-a-0": fnv1.Resource(
                                    resource=resource.dict_to_struct(
                                        {
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
                                                "engines": _REPLICA_ENGINES,
                                            },
                                        }
                                    ),
                                ),
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
                        context=structpb.Struct(),
                    )
                ),
            ),
            Case(
                name="no clusters produces warning",
                req=_req(_XR, clusters=[]),
                want=_want(
                    fnv1.RunFunctionResponse(
                        meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                        desired=fnv1.State(),
                        conditions=[
                            fnv1.Condition(
                                type="ReplicasScheduled",
                                status=fnv1.STATUS_CONDITION_FALSE,
                                reason="NoClusters",
                            ),
                        ],
                        results=[
                            fnv1.Result(severity=fnv1.SEVERITY_WARNING, message="No InferenceClusters found"),
                        ],
                        context=structpb.Struct(),
                    )
                ),
            ),
            Case(
                name="insufficient capacity produces no replicas",
                req=_req(_XR, clusters=[_cluster("cluster-a", nodes=0)]),
                want=_want(
                    fnv1.RunFunctionResponse(
                        meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                        desired=fnv1.State(
                            composite=fnv1.Resource(
                                resource=resource.dict_to_struct({"status": {"replicas": {"total": 0, "ready": 0}}}),
                                ready=fnv1.READY_FALSE,
                            ),
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
                        context=structpb.Struct(),
                    )
                ),
            ),
            Case(
                name="ready replica is preserved and keeps its endpoint",
                req=_req(
                    _XR,
                    clusters=[_CLUSTER_A],
                    replicas=[_EXISTING_REPLICA],
                    observed={
                        "replica-cluster-a-0": _replica_status(_EXISTING_REPLICA, ready=True),
                        "endpoint-cluster-a-0": {
                            "apiVersion": "modelplane.ai/v1alpha1",
                            "kind": "ModelEndpoint",
                            "metadata": {"name": "my-model-5ab63", "namespace": "ml-team"},
                        },
                    },
                ),
                want=_want(
                    fnv1.RunFunctionResponse(
                        meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                        desired=fnv1.State(
                            composite=fnv1.Resource(
                                resource=resource.dict_to_struct({"status": {"replicas": {"total": 1, "ready": 1}}}),
                            ),
                            resources={
                                "replica-cluster-a-0": fnv1.Resource(
                                    resource=resource.dict_to_struct(
                                        {
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
                                                "engines": _REPLICA_ENGINES,
                                            },
                                        }
                                    ),
                                    ready=fnv1.READY_TRUE,
                                ),
                                "endpoint-cluster-a-0": fnv1.Resource(
                                    resource=resource.dict_to_struct(
                                        {
                                            "apiVersion": "modelplane.ai/v1alpha1",
                                            "kind": "ModelEndpoint",
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
                                                "origin": "https://cluster.clusters.example.com",
                                                "api": {
                                                    "schema": "OpenAI",
                                                    "prefix": "/ml-team/my-model-5ab63/v1",
                                                },
                                                "model": "ml-team/my-model",
                                            },
                                        }
                                    ),
                                ),
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
                        context=structpb.Struct(),
                    )
                ),
            ),
            Case(
                name="offline pinned cluster keeps replica but drops endpoint",
                req=_req(
                    _XR,
                    clusters=[_cluster("cluster-a", ready=False, hostname=None)],
                    replicas=[_EXISTING_REPLICA],
                    observed={"replica-cluster-a-0": _EXISTING_REPLICA},
                ),
                want=_want(
                    fnv1.RunFunctionResponse(
                        meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                        desired=fnv1.State(
                            composite=fnv1.Resource(
                                resource=resource.dict_to_struct({"status": {"replicas": {"total": 1, "ready": 0}}}),
                            ),
                            resources={
                                "replica-cluster-a-0": fnv1.Resource(
                                    resource=resource.dict_to_struct(
                                        {
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
                                                "engines": _REPLICA_ENGINES,
                                            },
                                        }
                                    ),
                                ),
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
                        context=structpb.Struct(),
                    )
                ),
            ),
            Case(
                name="deleted pinned cluster triggers replica re-placement",
                req=_req(
                    _XR,
                    clusters=[_cluster("cluster-b", hostname="cluster-b.clusters.example.com")],
                    replicas=[_EXISTING_REPLICA],
                    observed={"replica-cluster-a-0": _replica_status(_EXISTING_REPLICA, ready=True)},
                ),
                want=_want(
                    fnv1.RunFunctionResponse(
                        meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                        desired=fnv1.State(
                            composite=fnv1.Resource(
                                resource=resource.dict_to_struct({"status": {"replicas": {"total": 1, "ready": 0}}}),
                            ),
                            resources={
                                "replica-cluster-b-0": fnv1.Resource(
                                    resource=resource.dict_to_struct(
                                        {
                                            "apiVersion": "modelplane.ai/v1alpha1",
                                            "kind": "ModelReplica",
                                            "metadata": {
                                                "name": "my-model-f0b76",
                                                "namespace": "ml-team",
                                                "labels": {
                                                    "modelplane.ai/deployment": "my-model",
                                                    "modelplane.ai/cluster": "cluster-b",
                                                    "modelplane.ai/replica-index": "0",
                                                },
                                            },
                                            "spec": {
                                                "clusterName": "cluster-b",
                                                "engines": _REPLICA_ENGINES,
                                            },
                                        }
                                    ),
                                ),
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
                        results=[
                            fnv1.Result(
                                severity=fnv1.SEVERITY_NORMAL,
                                message="Scheduled 1 replicas across 1 clusters: cluster-b",
                            ),
                        ],
                        context=structpb.Struct(),
                    )
                ),
            ),
            Case(
                name="modelCacheRef is propagated onto the composed replica",
                req=_req(xr_cached, clusters=[_CLUSTER_A], cache=_cache("qwen")),
                want=_want(
                    fnv1.RunFunctionResponse(
                        meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                        desired=fnv1.State(
                            composite=fnv1.Resource(
                                resource=resource.dict_to_struct({"status": {"replicas": {"total": 1, "ready": 0}}}),
                            ),
                            resources={
                                "replica-cluster-a-0": fnv1.Resource(
                                    resource=resource.dict_to_struct(
                                        {
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
                                                "modelCacheRef": {"name": "qwen"},
                                                "engines": _REPLICA_ENGINES,
                                            },
                                        }
                                    ),
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
                        results=[
                            fnv1.Result(
                                severity=fnv1.SEVERITY_NORMAL,
                                message="Scheduled 1 replicas across 1 clusters: cluster-a",
                            ),
                        ],
                        context=structpb.Struct(),
                    ),
                    cache_name="qwen",
                ),
            ),
            Case(
                # The cache stages only to a subset of clusters; the scheduler
                # intersects the cache's footprint with the deployment's own
                # clusterSelector so replicas never land where the cache isn't.
                name="cache clusterSelector is intersected with the deployment's",
                req=_req(
                    xr_cached_selector,
                    clusters=[_CLUSTER_A],
                    cache=_cache("qwen", match_labels={"tier": "gpu"}),
                ),
                want=_want(
                    fnv1.RunFunctionResponse(
                        meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                        desired=fnv1.State(
                            composite=fnv1.Resource(
                                resource=resource.dict_to_struct({"status": {"replicas": {"total": 1, "ready": 0}}}),
                            ),
                            resources={
                                "replica-cluster-a-0": fnv1.Resource(
                                    resource=resource.dict_to_struct(
                                        {
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
                                                "modelCacheRef": {"name": "qwen"},
                                                "engines": _REPLICA_ENGINES,
                                            },
                                        }
                                    ),
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
                        results=[
                            fnv1.Result(
                                severity=fnv1.SEVERITY_NORMAL,
                                message="Scheduled 1 replicas across 1 clusters: cluster-a",
                            ),
                        ],
                        context=structpb.Struct(),
                    ),
                    cluster_labels={"region": "us-east", "tier": "gpu"},
                    cache_name="qwen",
                ),
            ),
            Case(
                # A referenced cache Crossplane hasn't fetched yet leaves the
                # footprint unknown. With no replicas to retain, the function
                # holds off placing any rather than risk landing them outside the
                # footprint: fill is suppressed, so nothing is composed, and
                # ModelCacheResolved=False (Unresolved) says why. The wait is
                # transient and self-clearing, so it's a condition, not an event.
                # The cluster and replica requirements are still declared so the
                # cache can resolve alongside them.
                name="unresolved cache suppresses new placement",
                req=_req(xr_cached, clusters=[_CLUSTER_A]),
                want=_want(
                    fnv1.RunFunctionResponse(
                        meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                        desired=fnv1.State(
                            composite=fnv1.Resource(
                                resource=resource.dict_to_struct({"status": {"replicas": {"total": 0, "ready": 0}}}),
                                ready=fnv1.READY_FALSE,
                            ),
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
                        context=structpb.Struct(),
                    ),
                    cache_name="qwen",
                ),
            ),
            Case(
                # The cache a live deployment depends on is deleted (the cache
                # requirement resolves but matches nothing - ABSENT). The cache
                # only matters when loading weights, which already happened, so
                # its disappearance must not tear the deployment down: the
                # existing replica is retained (retain ignores fill) even as
                # ModelCacheResolved goes False (NotFound) and new placement is
                # suppressed.
                name="deleted cache retains existing replicas",
                req=_req(
                    xr_cached,
                    clusters=[_CLUSTER_A],
                    replicas=[_EXISTING_REPLICA],
                    observed={"replica-cluster-a-0": _replica_status(_EXISTING_REPLICA, ready=True)},
                    cache_resolved_empty=True,
                ),
                want=_want(
                    fnv1.RunFunctionResponse(
                        meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                        desired=fnv1.State(
                            composite=fnv1.Resource(
                                resource=resource.dict_to_struct({"status": {"replicas": {"total": 1, "ready": 1}}}),
                            ),
                            resources={
                                "replica-cluster-a-0": fnv1.Resource(
                                    resource=resource.dict_to_struct(
                                        {
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
                                                "modelCacheRef": {"name": "qwen"},
                                                "engines": _REPLICA_ENGINES,
                                            },
                                        }
                                    ),
                                    ready=fnv1.READY_TRUE,
                                ),
                                "endpoint-cluster-a-0": fnv1.Resource(
                                    resource=resource.dict_to_struct(
                                        {
                                            "apiVersion": "modelplane.ai/v1alpha1",
                                            "kind": "ModelEndpoint",
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
                                                "origin": "https://cluster.clusters.example.com",
                                                "api": {
                                                    "schema": "OpenAI",
                                                    "prefix": "/ml-team/my-model-5ab63/v1",
                                                },
                                                "model": "ml-team/my-model",
                                            },
                                        }
                                    ),
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
                        results=[
                            fnv1.Result(
                                severity=fnv1.SEVERITY_WARNING,
                                message="ModelCache qwen not found; holding replica placement",
                            ),
                        ],
                        context=structpb.Struct(),
                    ),
                    cache_name="qwen",
                ),
            ),
            Case(
                name="two replicas co-locate on one cluster as distinct resources",
                req=_req(xr_two, clusters=[_CLUSTER_A]),
                want=_want(
                    fnv1.RunFunctionResponse(
                        meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                        desired=fnv1.State(
                            composite=fnv1.Resource(
                                resource=resource.dict_to_struct({"status": {"replicas": {"total": 2, "ready": 0}}}),
                            ),
                            resources={
                                "replica-cluster-a-0": fnv1.Resource(
                                    resource=resource.dict_to_struct(
                                        {
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
                                                "engines": _REPLICA_ENGINES_NO_ARGS,
                                            },
                                        }
                                    ),
                                ),
                                "replica-cluster-a-1": fnv1.Resource(
                                    resource=resource.dict_to_struct(
                                        {
                                            "apiVersion": "modelplane.ai/v1alpha1",
                                            "kind": "ModelReplica",
                                            "metadata": {
                                                "name": "my-model-609c5",
                                                "namespace": "ml-team",
                                                "labels": {
                                                    "modelplane.ai/deployment": "my-model",
                                                    "modelplane.ai/cluster": "cluster-a",
                                                    "modelplane.ai/replica-index": "1",
                                                },
                                            },
                                            "spec": {
                                                "clusterName": "cluster-a",
                                                "engines": _REPLICA_ENGINES_NO_ARGS,
                                            },
                                        }
                                    ),
                                ),
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
                        results=[
                            fnv1.Result(
                                severity=fnv1.SEVERITY_NORMAL,
                                message="Scheduled 2 replicas across 1 clusters: cluster-a",
                            ),
                        ],
                        context=structpb.Struct(),
                    )
                ),
            ),
            Case(
                # PrefillDecode copies serving and each engine's phase onto the
                # replica; the replica backend reads them to front the engines
                # with an InferencePool + endpoint picker rather than a Service.
                name="PrefillDecode copies serving and engine phases onto the replica",
                req=_req(xr_pd, clusters=[_CLUSTER_A]),
                want=_want(
                    fnv1.RunFunctionResponse(
                        meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                        desired=fnv1.State(
                            composite=fnv1.Resource(
                                resource=resource.dict_to_struct({"status": {"replicas": {"total": 1, "ready": 0}}}),
                            ),
                            resources={
                                "replica-cluster-a-0": fnv1.Resource(
                                    resource=resource.dict_to_struct(
                                        {
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
                                                "serving": {"mode": "PrefillDecode"},
                                                "engines": _PD_REPLICA_ENGINES,
                                            },
                                        }
                                    ),
                                ),
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
                        results=[
                            fnv1.Result(
                                severity=fnv1.SEVERITY_NORMAL,
                                message="Scheduled 1 replicas across 1 clusters: cluster-a",
                            ),
                        ],
                        context=structpb.Struct(),
                    )
                ),
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


def _composed(resp: fnv1.RunFunctionResponse, kind: str) -> list[dict]:
    """Composed desired resources of the given kind, as dicts."""
    result = []
    for r in resp.desired.resources.values():
        d = resource.struct_to_dict(r.resource)
        if d.get("kind") == kind:
            result.append(d)
    return result


class TestTemplateLabels(unittest.IsolatedAsyncioTestCase):
    """spec.template.metadata.labels land on the composed ModelReplicas and
    ModelEndpoints, alongside the labels Modelplane manages."""

    async def test_stamped_on_replica_and_endpoint(self) -> None:
        xr = v1alpha1.ModelDeployment(
            metadata=metav1.ObjectMeta(name="my-model", namespace="ml-team"),
            spec=v1alpha1.SpecModel1(
                replicas=1,
                template=v1alpha1.TemplateModel(
                    metadata=v1alpha1.Metadata(labels={"tier": "prod", "team": "search"}),
                    spec=v1alpha1.SpecModel(engines=[_ENGINE]),
                ),
            ),
        ).model_dump(exclude_none=True, mode="json")
        # An observed, Ready replica lets the endpoint compose this reconcile.
        req = _req(
            xr,
            clusters=[_CLUSTER_A],
            replicas=[_EXISTING_REPLICA],
            observed={"replica-cluster-a-0": _replica_status(_EXISTING_REPLICA, ready=True)},
        )
        got = await fn.FunctionRunner().RunFunction(req, None)

        composed = _composed(got, "ModelReplica") + _composed(got, "ModelEndpoint")
        self.assertEqual(len(composed), 2, "expected one ModelReplica and one ModelEndpoint")
        for obj in composed:
            labels = obj["metadata"]["labels"]
            self.assertEqual(labels.get("tier"), "prod")
            self.assertEqual(labels.get("team"), "search")
            self.assertEqual(labels.get("modelplane.ai/deployment"), "my-model")
            self.assertEqual(labels.get("modelplane.ai/cluster"), "cluster-a")
            self.assertEqual(labels.get("modelplane.ai/replica-index"), "0")

    async def test_managed_labels_win_a_collision(self) -> None:
        """The XRD's CEL rejects a template label under the modelplane.ai/ prefix,
        but the invariant lives in the function too: managed labels are stamped
        last, so a colliding label can't override them even if that CEL rule is
        relaxed or the function is reused elsewhere."""
        xr = v1alpha1.ModelDeployment(
            metadata=metav1.ObjectMeta(name="my-model", namespace="ml-team"),
            spec=v1alpha1.SpecModel1(
                replicas=1,
                template=v1alpha1.TemplateModel(
                    metadata=v1alpha1.Metadata(labels={"modelplane.ai/cluster": "wrong", "tier": "prod"}),
                    spec=v1alpha1.SpecModel(engines=[_ENGINE]),
                ),
            ),
        ).model_dump(exclude_none=True, mode="json")
        got = await fn.FunctionRunner().RunFunction(_req(xr, clusters=[_CLUSTER_A]), None)

        replica = _composed(got, "ModelReplica")[0]
        self.assertEqual(replica["metadata"]["labels"]["modelplane.ai/cluster"], "cluster-a")
        self.assertEqual(replica["metadata"]["labels"]["tier"], "prod")


class TestResolveRequired(unittest.TestCase):
    """Tests for fn.resolve_required - the three-state required-resource read."""

    def test_resolve_required(self) -> None:
        cache = {"apiVersion": "modelplane.ai/v1alpha1", "kind": "ModelCache", "metadata": {"name": "qwen"}}

        # PRESENT: the requirement resolved and matched a resource.
        req = fnv1.RunFunctionRequest()
        req.required_resources["cache"].items.append(fnv1.Resource(resource=resource.dict_to_struct(cache)))
        self.assertEqual((fn.Resolution.PRESENT, cache), fn.resolve_required(req, "cache"))

        # ABSENT: the requirement resolved but matched nothing (key present, no items).
        req = fnv1.RunFunctionRequest()
        req.required_resources["cache"].SetInParent()
        self.assertEqual((fn.Resolution.ABSENT, None), fn.resolve_required(req, "cache"))

        # UNRESOLVED: Crossplane has not fetched the requirement (key absent).
        req = fnv1.RunFunctionRequest()
        self.assertEqual((fn.Resolution.UNRESOLVED, None), fn.resolve_required(req, "cache"))


class TestServedModelName(unittest.TestCase):
    """The name an engine is started under, and how it gets there."""

    def test_it_goes_ahead_of_the_users_env(self) -> None:
        """Env expansion is left to right, so an arg or a later entry
        referencing $(MODELPLANE_SERVED_MODEL_NAME) only resolves if it's
        first."""
        template = mrv1alpha1.Template(
            spec=mrv1alpha1.Spec(
                containers=[
                    mrv1alpha1.Container(
                        name="engine",
                        image="vllm/vllm-openai:latest",
                        env=[mrv1alpha1.EnvItem(name="HF_TOKEN", value="x")],
                    )
                ]
            )
        )
        fn._inject_served_model_name(template, "ml-team/kimi-k2")
        assert template.spec is not None
        self.assertEqual(
            [(e.name, e.value) for e in template.spec.containers[0].env or []],
            [("MODELPLANE_SERVED_MODEL_NAME", "ml-team/kimi-k2"), ("HF_TOKEN", "x")],
        )

    def test_a_user_override_is_dropped(self) -> None:
        """Modelplane decides this value. Honouring an override would let the
        engine answer to a name nothing routes to, which surfaces as a 404 from
        the engine rather than anything visible in status."""
        template = mrv1alpha1.Template(
            spec=mrv1alpha1.Spec(
                containers=[
                    mrv1alpha1.Container(
                        name="engine",
                        image="vllm/vllm-openai:latest",
                        env=[mrv1alpha1.EnvItem(name="MODELPLANE_SERVED_MODEL_NAME", value="mine")],
                    )
                ]
            )
        )
        fn._inject_served_model_name(template, "ml-team/kimi-k2")
        assert template.spec is not None
        self.assertEqual(
            [(e.name, e.value) for e in template.spec.containers[0].env or []],
            [("MODELPLANE_SERVED_MODEL_NAME", "ml-team/kimi-k2")],
        )

    def test_it_is_namespaced(self) -> None:
        """So two deployments in different namespaces can't collide, and a
        ModelService can rewrite one name for a whole deployment."""
        self.assertEqual(fn.served_model_name("ml-team", "kimi-k2"), "ml-team/kimi-k2")


class TestPlacementLabels(unittest.IsolatedAsyncioTestCase):
    """A cluster's spec.placement.metadata.labels land on the ModelReplicas and
    ModelEndpoints composed there.

    This is the endpoint half of residency: a ModelService selects endpoints by
    label, so without it a region-scoped service can't select its own replicas,
    and nobody can label them by hand because Modelplane owns them. The gateway
    half is an InferenceGateway's serviceSelector.
    """

    async def test_stamped_on_replica_and_endpoint(self) -> None:
        xr = v1alpha1.ModelDeployment(
            metadata=metav1.ObjectMeta(name="my-model", namespace="ml-team"),
            spec=v1alpha1.SpecModel1(
                replicas=1,
                template=v1alpha1.TemplateModel(spec=v1alpha1.SpecModel(engines=[_ENGINE])),
            ),
        ).model_dump(exclude_none=True, mode="json")
        req = _req(
            xr,
            clusters=[_cluster("cluster-a", placement_labels={"example.org/region": "eu"})],
            replicas=[_EXISTING_REPLICA],
            observed={"replica-cluster-a-0": _replica_status(_EXISTING_REPLICA, ready=True)},
        )
        got = await fn.FunctionRunner().RunFunction(req, None)

        composed = _composed(got, "ModelReplica") + _composed(got, "ModelEndpoint")
        self.assertEqual(len(composed), 2, "expected one ModelReplica and one ModelEndpoint")
        for obj in composed:
            self.assertEqual(obj["metadata"]["labels"].get("example.org/region"), "eu")

    async def test_a_cluster_label_beats_a_template_label(self) -> None:
        """The cluster is the authority on where it is, so its placement labels
        are stamped after the deployment's own template labels."""
        xr = v1alpha1.ModelDeployment(
            metadata=metav1.ObjectMeta(name="my-model", namespace="ml-team"),
            spec=v1alpha1.SpecModel1(
                replicas=1,
                template=v1alpha1.TemplateModel(
                    metadata=v1alpha1.Metadata(labels={"example.org/region": "wrong"}),
                    spec=v1alpha1.SpecModel(engines=[_ENGINE]),
                ),
            ),
        ).model_dump(exclude_none=True, mode="json")
        got = await fn.FunctionRunner().RunFunction(
            _req(xr, clusters=[_cluster("cluster-a", placement_labels={"example.org/region": "eu"})]),
            None,
        )
        replica = _composed(got, "ModelReplica")[0]
        self.assertEqual(replica["metadata"]["labels"]["example.org/region"], "eu")
