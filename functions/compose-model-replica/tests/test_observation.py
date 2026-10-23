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

"""Tests for compose-model-replica's observation of whether a replica's pods have scheduled.

The observed ReplicaObservation statuses are what the cluster agent reports.
"""

import dataclasses
import datetime
from typing import Literal

import pytest
from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import observation
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class ComposeCase:
    """A test case for observation.compose."""

    name: str
    reason: str
    replica: v1alpha1.ModelReplica
    provider_config: str
    want: dict | None


@dataclasses.dataclass
class PodsScheduledCase:
    """A test case for observation.pods_scheduled."""

    name: str
    reason: str
    replica: v1alpha1.ModelReplica
    observed: fnv1.Resource | None
    want: resource.Condition


def _member(*, role: Literal["Standalone", "Leader", "Worker"], worker_nodes: int | None) -> v1alpha1.Member:
    """A member of role pinned to pool frontier, claiming one GPU, with a Worker's node span unless it's None."""
    return v1alpha1.Member(
        role=role,
        worker=v1alpha1.Worker(nodes=worker_nodes) if worker_nodes is not None else None,
        nodePoolName="frontier",
        deviceRequests=[v1alpha1.DeviceRequest(name="gpu", deviceClassName="gpu.nvidia.com", count=1)],
        template=v1alpha1.Template(
            spec=v1alpha1.Spec(containers=[v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")]),
        ),
    )


def _replica(
    *, labels: dict[str, str], members: list[v1alpha1.Member], conditions: list[v1alpha1.Condition] | None
) -> v1alpha1.ModelReplica:
    """The ModelReplica test-replica in ml-team, whose one engine, main, has members, with conditions unless they're None."""
    return v1alpha1.ModelReplica(
        metadata=metav1.ObjectMeta(name="test-replica", namespace="ml-team", labels=labels),
        spec=v1alpha1.SpecModel1(
            clusterName="cluster-a",
            engines=[v1alpha1.Engine(name="main", copies=1, members=members)],
        ),
        status=v1alpha1.Status(conditions=conditions) if conditions is not None else None,
    )


def _observed(*, status: dict) -> fnv1.Resource:
    """The observed ReplicaObservation Object, whose remote manifest carries the status the agent reported."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {"forProvider": {"manifest": {"kind": "ReplicaObservation"}}},
                "status": {
                    "atProvider": {
                        "manifest": {
                            "apiVersion": "agent.modelplane.ai/v1alpha1",
                            "kind": "ReplicaObservation",
                            "metadata": {"name": "test-replica", "namespace": "mp-ml-team-51733"},
                            "status": status,
                        },
                    },
                },
            }
        ),
    )


COMPOSE_CASES = [
    ComposeCase(
        name="Unlabelled",
        reason="A replica without a replica index composes no observation, because its pods carry no replica label for the agent to see.",
        replica=_replica(
            labels={"modelplane.ai/deployment": "my-deployment"},
            members=[_member(role="Standalone", worker_nodes=None)],
            conditions=None,
        ),
        provider_config="cluster-a-pc",
        want=None,
    ),
    ComposeCase(
        name="Labelled",
        reason="A replica of a deployment observes its engine pods by the deployment and replica index they carry.",
        replica=_replica(
            labels={"modelplane.ai/deployment": "my-deployment", "modelplane.ai/replica-index": "2"},
            members=[_member(role="Standalone", worker_nodes=None)],
            conditions=None,
        ),
        provider_config="cluster-a-pc",
        want={
            "spec": {
                "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                "readiness": {"policy": "SuccessfulCreate"},
                "watch": True,
                "forProvider": {
                    "manifest": {
                        "apiVersion": "agent.modelplane.ai/v1alpha1",
                        "kind": "ReplicaObservation",
                        "metadata": {"name": "test-replica", "namespace": "mp-ml-team-51733"},
                        "spec": {
                            "podSelector": {
                                "matchLabels": {
                                    "modelplane.ai/deployment": "my-deployment",
                                    "modelplane.ai/replica": "2",
                                },
                                "matchExpressions": [{"key": "modelplane.ai/engine", "operator": "Exists"}],
                            },
                        },
                    },
                },
            },
        },
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: ComposeCase) -> None:
    """compose builds the Object that creates a replica's ReplicaObservation."""
    got = observation.compose(case.replica, case.provider_config)
    got_dict = got.model_dump(exclude_unset=True, by_alias=True) if got is not None else None
    assert got_dict == case.want, case.reason


PODS_SCHEDULED_CASES = [
    PodsScheduledCase(
        name="NotObserved",
        reason="Before its ReplicaObservation is observed, a replica waits for the observation.",
        replica=_replica(labels={}, members=[_member(role="Standalone", worker_nodes=None)], conditions=None),
        observed=None,
        want=resource.Condition(typ="PodsScheduled", status="False", reason="WaitingForObservation"),
    ),
    PodsScheduledCase(
        name="NotReported",
        reason="Before the agent reports on its pods, a replica waits for the observation.",
        replica=_replica(labels={}, members=[_member(role="Standalone", worker_nodes=None)], conditions=None),
        observed=fnv1.Resource(
            resource=resource.dict_to_struct(
                {
                    "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                    "kind": "Object",
                    "spec": {"forProvider": {"manifest": {"kind": "ReplicaObservation"}}},
                }
            )
        ),
        want=resource.Condition(typ="PodsScheduled", status="False", reason="WaitingForObservation"),
    ),
    PodsScheduledCase(
        name="Unschedulable",
        reason="A replica whose pod the scheduler can't place relays the agent's reason and message.",
        replica=_replica(labels={}, members=[_member(role="Standalone", worker_nodes=None)], conditions=None),
        observed=_observed(
            status={
                "observedGeneration": 1,
                "conditions": [
                    {
                        "type": "Scheduled",
                        "status": "False",
                        "reason": "Unschedulable",
                        "message": "pod too-big-5859cf75cf-jzm8n: 0/2 nodes are available: 1 cannot allocate all claims, 1 node(s) didn't match Pod's node affinity/selector.",
                    },
                    {"type": "Ready", "status": "False", "reason": "NotScheduled", "message": "0 of 1 pods scheduled"},
                ],
                "pods": {"total": 1, "scheduled": 0, "ready": 0},
            }
        ),
        want=resource.Condition(
            typ="PodsScheduled",
            status="False",
            reason="Unschedulable",
            message="pod too-big-5859cf75cf-jzm8n: 0/2 nodes are available: 1 cannot allocate all claims, 1 node(s) didn't match Pod's node affinity/selector.",
        ),
    ),
    PodsScheduledCase(
        name="WorkersNotCreated",
        reason="A gang whose workers haven't all been created hasn't scheduled, even when every pod the agent sees has.",
        replica=_replica(
            labels={},
            members=[_member(role="Leader", worker_nodes=None), _member(role="Worker", worker_nodes=2)],
            conditions=None,
        ),
        observed=_observed(
            status={
                "observedGeneration": 1,
                "conditions": [
                    {"type": "Scheduled", "status": "True", "reason": "PodsScheduled"},
                    {"type": "Ready", "status": "False", "reason": "Starting", "message": "0 of 2 pods ready"},
                ],
                "pods": {"total": 2, "scheduled": 2, "ready": 0},
            }
        ),
        want=resource.Condition(
            typ="PodsScheduled", status="False", reason="WaitingForPods", message="2 of 3 pods exist"
        ),
    ),
    PodsScheduledCase(
        name="Scheduled",
        reason="A gang whose every pod exists and is bound to a node has scheduled.",
        replica=_replica(
            labels={},
            members=[_member(role="Leader", worker_nodes=None), _member(role="Worker", worker_nodes=2)],
            conditions=None,
        ),
        observed=_observed(
            status={
                "observedGeneration": 1,
                "conditions": [
                    {"type": "Scheduled", "status": "True", "reason": "PodsScheduled"},
                    {"type": "Ready", "status": "False", "reason": "Starting", "message": "0 of 3 pods ready"},
                ],
                "pods": {"total": 3, "scheduled": 3, "ready": 0},
            }
        ),
        want=resource.Condition(typ="PodsScheduled", status="True", reason="PodsScheduled"),
    ),
    PodsScheduledCase(
        name="ReasonUnchanged",
        reason="A replica whose pod is still unschedulable keeps the message it relayed when the pod became unschedulable.",
        replica=_replica(
            labels={},
            members=[_member(role="Standalone", worker_nodes=None)],
            conditions=[
                v1alpha1.Condition(
                    type="PodsScheduled",
                    status="False",
                    reason="Unschedulable",
                    message="pod llama-0: 0/2 nodes are available: 2 cannot allocate all claims.",
                    lastTransitionTime=datetime.datetime(2026, 10, 20, 10, 0, tzinfo=datetime.UTC),
                ),
            ],
        ),
        observed=_observed(
            status={
                "observedGeneration": 1,
                "conditions": [
                    {
                        "type": "Scheduled",
                        "status": "False",
                        "reason": "Unschedulable",
                        "message": "pod llama-0: 0/3 nodes are available: 3 cannot allocate all claims.",
                    },
                    {"type": "Ready", "status": "False", "reason": "NotScheduled", "message": "0 of 1 pods scheduled"},
                ],
                "pods": {"total": 1, "scheduled": 0, "ready": 0},
            }
        ),
        want=resource.Condition(
            typ="PodsScheduled",
            status="False",
            reason="Unschedulable",
            message="pod llama-0: 0/2 nodes are available: 2 cannot allocate all claims.",
        ),
    ),
    PodsScheduledCase(
        name="PodCountChanged",
        reason="A gang still waiting for its workers reports how many of its pods exist now, since that's Modelplane's count rather than the agent's message.",
        replica=_replica(
            labels={},
            members=[_member(role="Leader", worker_nodes=None), _member(role="Worker", worker_nodes=2)],
            conditions=[
                v1alpha1.Condition(
                    type="PodsScheduled",
                    status="False",
                    reason="WaitingForPods",
                    message="1 of 3 pods exist",
                    lastTransitionTime=datetime.datetime(2026, 10, 20, 10, 0, tzinfo=datetime.UTC),
                ),
            ],
        ),
        observed=_observed(
            status={
                "observedGeneration": 1,
                "conditions": [
                    {"type": "Scheduled", "status": "True", "reason": "PodsScheduled"},
                    {"type": "Ready", "status": "False", "reason": "Starting", "message": "0 of 2 pods ready"},
                ],
                "pods": {"total": 2, "scheduled": 2, "ready": 0},
            }
        ),
        want=resource.Condition(
            typ="PodsScheduled", status="False", reason="WaitingForPods", message="2 of 3 pods exist"
        ),
    ),
    PodsScheduledCase(
        name="ReasonChanged",
        reason="A replica whose pod has a new reason not to be scheduled relays the agent's new message.",
        replica=_replica(
            labels={},
            members=[_member(role="Standalone", worker_nodes=None)],
            conditions=[
                v1alpha1.Condition(
                    type="PodsScheduled",
                    status="False",
                    reason="SchedulingPending",
                    message="pod llama-0",
                    lastTransitionTime=datetime.datetime(2026, 10, 20, 10, 0, tzinfo=datetime.UTC),
                ),
            ],
        ),
        observed=_observed(
            status={
                "observedGeneration": 1,
                "conditions": [
                    {
                        "type": "Scheduled",
                        "status": "False",
                        "reason": "Unschedulable",
                        "message": "pod llama-0: 0/3 nodes are available: 3 cannot allocate all claims.",
                    },
                    {"type": "Ready", "status": "False", "reason": "NotScheduled", "message": "0 of 1 pods scheduled"},
                ],
                "pods": {"total": 1, "scheduled": 0, "ready": 0},
            }
        ),
        want=resource.Condition(
            typ="PodsScheduled",
            status="False",
            reason="Unschedulable",
            message="pod llama-0: 0/3 nodes are available: 3 cannot allocate all claims.",
        ),
    ),
]


@pytest.mark.parametrize("case", PODS_SCHEDULED_CASES, ids=lambda case: case.name)
def test_pods_scheduled(case: PodsScheduledCase) -> None:
    """pods_scheduled relays whether every one of a replica's pods has scheduled."""
    got = observation.pods_scheduled(case.replica, case.observed)
    assert got == case.want, case.reason


@dataclasses.dataclass
class PoolsCase:
    """A test case for observation.pools."""

    name: str
    reason: str
    observed: fnv1.Resource | None
    want: list[v1alpha1.Pool]


POOLS_CASES = [
    PoolsCase(
        name="NotObserved",
        reason="Before its ReplicaObservation is observed, a replica is on no pools.",
        observed=None,
        want=[],
    ),
    PoolsCase(
        name="NoDevices",
        reason="A replica the agent has seen no devices allocated to is on no pools.",
        observed=_observed(
            status={
                "observedGeneration": 1,
                "conditions": [
                    {"type": "Scheduled", "status": "False", "reason": "SchedulingPending", "message": "pod llama-0"},
                    {"type": "Ready", "status": "False", "reason": "NotScheduled", "message": "0 of 1 pods scheduled"},
                ],
                "pods": {"total": 1, "scheduled": 0, "ready": 0},
            }
        ),
        want=[],
    ),
    PoolsCase(
        name="Pools",
        reason="A replica is on the pools the agent reports, with the latest time it first saw the replica's claims allocated on each.",
        observed=_observed(
            status={
                "observedGeneration": 1,
                "conditions": [
                    {"type": "Scheduled", "status": "True", "reason": "PodsScheduled"},
                    {"type": "Ready", "status": "True", "reason": "PodsReady"},
                ],
                "pods": {"total": 2, "scheduled": 2, "ready": 2},
                "pools": [
                    {"name": "decode", "lastAllocationTime": "2026-10-20T10:02:13.482915Z"},
                    {"name": "prefill", "lastAllocationTime": "2026-10-20T10:02:14.000001Z"},
                ],
            }
        ),
        want=[
            v1alpha1.Pool(
                name="decode",
                lastAllocationTime=datetime.datetime(2026, 10, 20, 10, 2, 13, 482915, tzinfo=datetime.UTC),
            ),
            v1alpha1.Pool(
                name="prefill", lastAllocationTime=datetime.datetime(2026, 10, 20, 10, 2, 14, 1, tzinfo=datetime.UTC)
            ),
        ],
    ),
]


@pytest.mark.parametrize("case", POOLS_CASES, ids=lambda case: case.name)
def test_pools(case: PoolsCase) -> None:
    """pools relays the pools the agent reports a replica's devices are on."""
    got = observation.pools(case.observed)
    assert got == case.want, case.reason
