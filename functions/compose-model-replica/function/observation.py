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
its pods has scheduled as its ModelPodsScheduled condition, and when the agent
first saw its claims allocated on each pool as status.pools.
compose-model-deployment reads both to tell which replicas a cluster's histogram
of free devices accounts for.
"""

from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from models.ai.modelplane.agent.replicaobservation import v1alpha1 as rov1alpha1
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

from function.backends import base

# The composed resource key of the replica's ReplicaObservation.
KEY = "replica-observation"

# The replica's ModelPodsScheduled condition. Its type and True status are a
# contract with compose-model-deployment. A False status copies the agent's
# reason where it has one.
CONDITION_TYPE = "ModelPodsScheduled"
REASON_ALL_PODS_SCHEDULED = "AllPodsScheduled"
REASON_WAITING_FOR_OBSERVATION = "WaitingForObservation"
REASON_WAITING_FOR_PODS = "WaitingForPods"

# The agent's condition reporting whether a ReplicaObservation's pods are bound
# to nodes.
_AGENT_CONDITION_SCHEDULED = "Scheduled"

# provider-kubernetes stops receiving watch events once the token in a
# cluster's kubeconfig expires, which on EKS is after 15 minutes. A watched
# ReplicaObservation then only reaches the replica when it's polled, every 10
# minutes by default, so it's polled more often.
# TODO(negz): Remove once provider-kubernetes fixes this.
# https://github.com/crossplane-contrib/provider-kubernetes/issues/598
_ANNOTATION_POLL_INTERVAL = "crossplane.io/poll-interval"
_POLL_INTERVAL = "30s"


def compose(replica: v1alpha1.ModelReplica, provider_config: str) -> k8sobjv1alpha1.Object | None:
    """The Object creating the replica's ReplicaObservation on its cluster.

    It selects the replica's engine pods by the deployment and replica index
    they carry, which together identify a replica within its namespace. The
    endpoint picker carries both too, but no engine. The agent ignores a pod
    without a replica label, so a replica its deployment didn't label gets no
    observation.

    provider-kubernetes watches the ReplicaObservation, and polls it often in
    case the watch has stopped (see _POLL_INTERVAL), so the agent's reports
    reach the replica soon after they change.
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
        rov1alpha1.ReplicaObservation(
            apiVersion="agent.modelplane.ai/v1alpha1",
            kind="ReplicaObservation",
            metadata=metav1.ObjectMeta(name=meta.name, namespace=base.remote_namespace(replica)),
            spec=rov1alpha1.Spec(
                podSelector=rov1alpha1.PodSelector(
                    matchLabels={base.LABEL_DEPLOYMENT: deployment, base.LABEL_REPLICA: index},
                    matchExpressions=[rov1alpha1.MatchExpression(key=base.LABEL_ENGINE, operator="Exists")],
                ),
            ),
        ).model_dump(exclude_unset=True),
        watch=True,
    )
    obj.metadata = metav1.ObjectMeta(annotations={_ANNOTATION_POLL_INTERVAL: _POLL_INTERVAL})
    return obj


def agent_reports(cluster: icv1alpha1.InferenceCluster, replica: v1alpha1.ModelReplica) -> bool:
    """Whether the cluster agent reports on any of the pools the replica's members are pinned to.

    The agent installs the ReplicaObservation CRD. An Object of a kind the
    cluster doesn't serve can't be created, and can't be deleted either, so it
    would hold up the replica's deletion. A pool the cluster publishes
    observedNodes for is one the agent is reporting on.
    """
    pinned = {m.nodePoolName for e in replica.spec.engines for m in e.members}
    pools = (cluster.status.schedulablePools if cluster.status else None) or []
    return any(p.name in pinned and p.observedNodes is not None for p in pools)


def pods_scheduled(replica: v1alpha1.ModelReplica, status: rov1alpha1.Status | None) -> resource.Condition:
    """The replica's ModelPodsScheduled condition, given the status the agent reported for it.

    The agent's Scheduled condition is True once every pod it can see is bound
    to a node with its claims allocated, which a gang's workers can make true
    before they've all been created. So the replica's pods count as scheduled
    only once the agent sees as many as the replica runs.

    While the agent's reason is unchanged, the condition keeps the message it
    relayed when the reason last changed. Every change to a replica's status
    reconciles every ModelDeployment, and the cluster scheduler's message can
    change each time it retries a pod.
    """
    if status is None:
        return resource.Condition(typ=CONDITION_TYPE, status="False", reason=REASON_WAITING_FOR_OBSERVATION)

    scheduled = next((c for c in status.conditions or [] if c.type == _AGENT_CONDITION_SCHEDULED), None)
    if scheduled is None:
        return resource.Condition(typ=CONDITION_TYPE, status="False", reason=REASON_WAITING_FOR_OBSERVATION)
    if scheduled.status != "True":
        message = scheduled.message
        for c in (replica.status.conditions if replica.status else None) or []:
            if c.type == CONDITION_TYPE and c.status == "False" and c.reason == scheduled.reason:
                message = c.message
        return resource.Condition(typ=CONDITION_TYPE, status="False", reason=scheduled.reason, message=message)

    total = status.pods.total
    expected = _expected_pods(replica)
    if total < expected:
        return resource.Condition(
            typ=CONDITION_TYPE,
            status="False",
            reason=REASON_WAITING_FOR_PODS,
            message=f"{total} of {expected} pods exist",
        )
    return resource.Condition(typ=CONDITION_TYPE, status="True", reason=REASON_ALL_PODS_SCHEDULED)


def pools(status: rov1alpha1.Status | None) -> list[v1alpha1.Pool]:
    """The pools the replica's devices are on, given the status the agent reported for it.

    Each carries the latest time the agent first saw one of the replica's claims
    allocated devices on it, which compose-model-deployment compares with the
    time its InferenceCluster publishes for the pool.
    """
    if status is None:
        return []
    return [v1alpha1.Pool(name=p.name, lastAllocationTime=p.lastAllocationTime) for p in status.pools or []]


def status(observed: fnv1.Resource | None) -> rov1alpha1.Status | None:
    """The status the agent reported in the observed ReplicaObservation, or None until it reports.

    Raises pydantic.ValidationError if the status doesn't match the agent's
    schema.
    """
    if observed is None:
        return None
    obj = k8sobjv1alpha1.Object.model_validate(resource.struct_to_dict(observed.resource))
    if not obj.status or not obj.status.atProvider or not obj.status.atProvider.manifest:
        return None
    return rov1alpha1.ReplicaObservation.model_validate(obj.status.atProvider.manifest).status


def _expected_pods(replica: v1alpha1.ModelReplica) -> int:
    """How many engine pods the replica runs: a Worker's nodes, otherwise one, for each copy of each engine.

    compose-model-deployment's scheduler charges a replica's pods by counting
    them the same way, in _member_pods, so the two must agree.
    """
    return sum(
        int(engine.copies or 1)
        * sum(int(m.worker.nodes) if m.role == base.ROLE_WORKER and m.worker else 1 for m in engine.members)
        for engine in replica.spec.engines
    )
