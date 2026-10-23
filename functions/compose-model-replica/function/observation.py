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

"""Observe whether a replica's pods have scheduled, and where.

The cluster agent, which the serving stack installs, reports on the pods a
ReplicaObservation's selector matches. The replica relays whether every one of
its pods has scheduled as its PodsScheduled condition, and when the agent first
saw its claims allocated on each pool as status.pools. compose-model-deployment
reads both to tell which replicas a cluster's histogram of free devices already
accounts for.
"""

from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

from function.backends import base

# The composed resource key of the replica's ReplicaObservation.
KEY = "replica-observation"

# The replica's PodsScheduled condition. Its type and True status are a contract
# with compose-model-deployment. A False status copies the agent's reason where
# it has one.
CONDITION_TYPE = "PodsScheduled"
REASON_PODS_SCHEDULED = "PodsScheduled"
REASON_WAITING_FOR_OBSERVATION = "WaitingForObservation"
REASON_WAITING_FOR_PODS = "WaitingForPods"

_AGENT_API_VERSION = "agent.modelplane.ai/v1alpha1"

# The agent's condition reporting whether a ReplicaObservation's pods are bound
# to nodes.
_AGENT_CONDITION_SCHEDULED = "Scheduled"


def compose(replica: v1alpha1.ModelReplica, provider_config: str) -> k8sobjv1alpha1.Object | None:
    """The Object creating the replica's ReplicaObservation on its cluster.

    It selects the replica's engine pods by the deployment and replica index
    they carry, which together identify a replica within its namespace. The
    endpoint picker carries both too, but no engine. The agent ignores a pod
    without a replica label, so a replica its deployment didn't label gets no
    observation.

    provider-kubernetes watches the ReplicaObservation, so the agent's reports
    reach the replica when they change rather than when the Object is next
    polled.
    """
    meta = replica.metadata
    if meta is None or meta.name is None:
        raise ValueError("metadata.name is unexpectedly absent")
    labels = meta.labels or {}
    deployment = labels.get(base.LABEL_DEPLOYMENT)
    index = labels.get(base.LABEL_REPLICA_INDEX)
    if not deployment or not index:
        return None
    obj = base.wrap_object(
        provider_config,
        {
            "apiVersion": _AGENT_API_VERSION,
            "kind": "ReplicaObservation",
            "metadata": {"name": meta.name, "namespace": base.remote_namespace(replica)},
            "spec": {
                "podSelector": {
                    "matchLabels": {base.LABEL_DEPLOYMENT: deployment, base.LABEL_REPLICA: index},
                    "matchExpressions": [{"key": base.LABEL_ENGINE, "operator": "Exists"}],
                },
            },
        },
    )
    obj.spec.watch = True
    return obj


def agent_reports(cluster: icv1alpha1.InferenceCluster, replica: v1alpha1.ModelReplica) -> bool:
    """Whether the cluster agent reports on any of the pools the replica's members are pinned to.

    The agent installs the ReplicaObservation CRD. An Object of a kind the
    cluster doesn't serve can't be created, and can't be deleted either, so it
    would hold up the replica's deletion. A pool the cluster publishes
    observedNodes for is one the agent is reporting on.
    """
    pinned = {m.nodePoolName for e in replica.spec.engines for m in e.members}
    gpu_pools = (cluster.status.gpuPools if cluster.status else None) or []
    return any(p.name in pinned and p.observedNodes is not None for p in gpu_pools)


def pods_scheduled(replica: v1alpha1.ModelReplica, observed: fnv1.Resource | None) -> resource.Condition:
    """The replica's PodsScheduled condition, given its observed ReplicaObservation.

    The agent's Scheduled condition is True once every pod it can see is bound
    to a node with its claims allocated, which a gang's workers can make true
    before they've all been created. So the replica's pods count as scheduled
    only once the agent sees as many as the replica runs.

    While the agent's reason is unchanged, the condition keeps the message it
    relayed when the reason last changed. Every change to a replica's status
    reconciles every ModelDeployment, and the cluster scheduler's message can
    change each time it retries a pod.
    """
    status = _status(observed)
    if not status:
        return resource.Condition(typ=CONDITION_TYPE, status="False", reason=REASON_WAITING_FOR_OBSERVATION)

    scheduled = next((c for c in status.get("conditions", []) if c.get("type") == _AGENT_CONDITION_SCHEDULED), {})
    if scheduled.get("status") != "True":
        reason = scheduled.get("reason") or REASON_WAITING_FOR_OBSERVATION
        message = scheduled.get("message")
        for c in (replica.status.conditions if replica.status else None) or []:
            if c.type == CONDITION_TYPE and c.status == "False" and c.reason == reason:
                message = c.message
        return resource.Condition(typ=CONDITION_TYPE, status="False", reason=reason, message=message)

    # A protobuf Struct carries every number as a float.
    total = int(status.get("pods", {}).get("total", 0))
    expected = _expected_pods(replica)
    if total < expected:
        return resource.Condition(
            typ=CONDITION_TYPE,
            status="False",
            reason=REASON_WAITING_FOR_PODS,
            message=f"{total} of {expected} pods exist",
        )
    return resource.Condition(typ=CONDITION_TYPE, status="True", reason=REASON_PODS_SCHEDULED)


def pools(observed: fnv1.Resource | None) -> list[v1alpha1.Pool]:
    """The pools the replica's devices are on, given its observed ReplicaObservation.

    Each carries the latest time the agent first saw one of the replica's claims
    allocated devices on it, which compose-model-deployment compares with the
    time its InferenceCluster publishes for the pool.
    """
    return [v1alpha1.Pool.model_validate(p) for p in _status(observed).get("pools", [])]


def _status(observed: fnv1.Resource | None) -> dict:
    """The status the agent reported in the observed ReplicaObservation, empty until it reports."""
    if observed is None:
        return {}
    obj = k8sobjv1alpha1.Object.model_validate(resource.struct_to_dict(observed.resource))
    if not obj.status or not obj.status.atProvider or not obj.status.atProvider.manifest:
        return {}
    # The ReplicaObservation has no model, so its status is read as a dict.
    return obj.status.atProvider.manifest.get("status", {})


def _expected_pods(replica: v1alpha1.ModelReplica) -> int:
    """How many engine pods the replica runs: a Worker's nodes, otherwise one, for each copy of each engine."""
    return sum(
        int(engine.copies or 1)
        * sum(int(m.worker.nodes) if m.role == base.ROLE_WORKER and m.worker else 1 for m in engine.members)
        for engine in replica.spec.engines
    )
