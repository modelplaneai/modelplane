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

"""Tests for the scheduling module.

Unit tests for the retain-then-place scheduler. These construct
Pydantic models directly and call schedule() to exercise the core
logic without the protobuf/gRPC ceremony of the fn tests.

Pool selection is driven by nodeSelector device requests (DRA CEL matched
against a pool's devices) plus the available-node gate. Per-node GPU count is
expressed as a device request's count, not derived from topology.
"""

import dataclasses
import datetime
import unittest

from function import cel, scheduling
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# A fixed transition time keeps observed conditions deterministic.
_TRANSITION_TIME = datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)

# A GPU memory selector reused across cases.
_MEM_141 = 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("141Gi")) >= 0'
_MEM_200 = 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("200Gi")) >= 0'
_MEM_LT_200 = 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("200Gi")) < 0'
_IB = 'device.attributes["nic.nvidia.com"].linkType == "infiniband"'

# Default engine name used by the single-engine helpers below.
_ENGINE = "main"


@dataclasses.dataclass
class Case:
    """A test case for scheduling.schedule."""

    name: str
    deployment: mdv1alpha1.ModelDeployment
    clusters: list[icv1alpha1.InferenceCluster]
    all_replicas: list[mrv1alpha1.ModelReplica]
    want: list[scheduling.Candidate]


def _request(name: str = "gpu", count: int = 1, cel_exprs: list[str] | None = None) -> mdv1alpha1.Device:
    """A nodeSelector device request."""
    return mdv1alpha1.Device(
        name=name,
        count=count,
        selectors=[mdv1alpha1.Selector(cel=c) for c in (cel_exprs or [_MEM_141])],
    )


def _template():
    return mdv1alpha1.Template(
        spec=mdv1alpha1.Spec(
            containers=[mdv1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")],
        ),
    )


def _node_selector(requests: list[mdv1alpha1.Device] | None) -> mdv1alpha1.NodeSelector:
    return mdv1alpha1.NodeSelector(devices=requests if requests is not None else [_request()])


def _engine(
    name: str = _ENGINE,
    *,
    copies: int = 1,
    pipeline: int = 1,
    requests: list[mdv1alpha1.Device] | None = None,
) -> mdv1alpha1.Engine:
    """An engine of homogeneous members.

    pipeline == 1 is a single Standalone member; pipeline > 1 is a Leader plus a
    Worker spanning (pipeline - 1) nodes, so the engine spans `pipeline` nodes.
    Engine copies multiply that, so node cost is pipeline * copies. Every member
    carries the same nodeSelector; heterogeneous-member cases build their
    members directly.
    """
    if pipeline == 1:
        members = [mdv1alpha1.Member(role="Standalone", nodeSelector=_node_selector(requests), template=_template())]
    else:
        members = [
            mdv1alpha1.Member(role="Leader", nodeSelector=_node_selector(requests), template=_template()),
            mdv1alpha1.Member(
                role="Worker",
                worker=mdv1alpha1.Worker(nodes=pipeline - 1),
                nodeSelector=_node_selector(requests),
                template=_template(),
            ),
        ]
    return mdv1alpha1.Engine(name=name, copies=copies, members=members)


def _deployment(
    name: str = "my-model",
    replicas: int = 1,
    pipeline: int = 1,
    count: int = 1,
    requests: list[mdv1alpha1.Device] | None = None,
    engines: list[mdv1alpha1.Engine] | None = None,
):
    """Construct a ModelDeployment.

    The single-engine helpers map a node shape onto one engine: pipeline sets the
    engine's node span (a Standalone, or a Leader plus a Worker) and count sets
    the engine's copies, so node cost is pipeline * count. Multi-engine cases
    pass `engines` directly.
    """
    if engines is None:
        engines = [_engine(copies=count, pipeline=pipeline, requests=requests)]
    return mdv1alpha1.ModelDeployment(
        metadata=metav1.ObjectMeta(name=name, namespace="ml-team"),
        spec=mdv1alpha1.SpecModel(replicas=replicas, engines=engines),
    )


def _gpu_device(
    name: str = "gpu",
    *,
    claim: str = "DRA",
    driver: str = "gpu.nvidia.com",
    device_class: str = "gpu.nvidia.com",
    count: int = 1,
    memory: str = "141Gi",
) -> dict:
    """A GPU device dict for a pool, with memory capacity."""
    d = {
        "name": name,
        "claim": claim,
        "driver": driver,
        "count": count,
        "capacity": {"memory": {"value": memory}},
    }
    if claim == "DRA":
        d["deviceClassName"] = device_class
    return d


def _nic_device(*, link_type: str = "infiniband", count: int = 1) -> dict:
    """A synthetic NIC device dict for a pool."""
    return {
        "name": "nic",
        "claim": "Synthetic",
        "driver": "nic.nvidia.com",
        "count": count,
        "attributes": {"linkType": {"string": link_type}},
    }


def _pool(name: str, *, nodes: int = 2, devices: list[dict] | None = None) -> dict:
    """A pool with devices, for nodeSelector tests."""
    return {
        "name": name,
        "nodes": nodes,
        "devices": devices if devices is not None else [_gpu_device()],
    }


def _cluster(
    name: str,
    *,
    ready: bool = True,
    gateway_address: str = "10.0.0.1",
    pools: list[dict] | None = None,
) -> icv1alpha1.InferenceCluster:
    """Construct an InferenceCluster with the given readiness and pools.

    A "ready" cluster has a Ready=True condition and a gateway address.
    Setting ready=False or gateway_address="" produces a degraded cluster
    the scheduler will retain but not pick anew.
    """
    if pools is None:
        pools = [{"name": "default", "nodes": 2, "devices": [_gpu_device()]}]

    status = "True" if ready else "False"
    reason = "Available" if ready else "Unavailable"
    conditions = [
        icv1alpha1.Condition(
            type="Ready",
            status=status,
            reason=reason,
            lastTransitionTime=_TRANSITION_TIME,
        )
    ]

    return icv1alpha1.InferenceCluster(
        metadata=metav1.ObjectMeta(name=name),
        spec=icv1alpha1.Spec(
            cluster=icv1alpha1.Cluster(
                source="Existing",
                existing=icv1alpha1.Existing(secretRef=icv1alpha1.SecretRef(name="k")),
            ),
        ),
        status=icv1alpha1.Status(
            conditions=conditions,
            gateway=icv1alpha1.Gateway(address=gateway_address) if gateway_address else icv1alpha1.Gateway(),
            providerConfigRef=icv1alpha1.ProviderConfigRef(name=name),
            gpuPools=[icv1alpha1.GpuPool(**p) for p in pools],
        ),
    )


def _replica_device_requests() -> list[mrv1alpha1.DeviceRequest]:
    return [
        mrv1alpha1.DeviceRequest(
            name="gpu",
            deviceClassName="gpu.nvidia.com",
            count=1,
            selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
        ),
    ]


def _replica_engine(
    name: str = _ENGINE,
    *,
    pool: str = "default",
    copies: int = 1,
    pipeline: int = 1,
) -> mrv1alpha1.Engine:
    """One engine of an observed ModelReplica, with per-member pool pins and resolved requests."""
    template = mrv1alpha1.Template(
        spec=mrv1alpha1.Spec(containers=[mrv1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")]),
    )
    if pipeline == 1:
        members = [
            mrv1alpha1.Member(
                role="Standalone", nodePoolName=pool, deviceRequests=_replica_device_requests(), template=template
            )
        ]
    else:
        members = [
            mrv1alpha1.Member(
                role="Leader", nodePoolName=pool, deviceRequests=_replica_device_requests(), template=template
            ),
            mrv1alpha1.Member(
                role="Worker",
                worker=mrv1alpha1.Worker(nodes=pipeline - 1),
                nodePoolName=pool,
                deviceRequests=_replica_device_requests(),
                template=template,
            ),
        ]
    return mrv1alpha1.Engine(name=name, copies=copies, members=members)


def _replica(
    deployment_name: str,
    cluster_name: str,
    *,
    pool: str = "default",
    index: int = 0,
    pipeline: int = 1,
    count: int = 1,
    engines: list[mrv1alpha1.Engine] | None = None,
) -> mrv1alpha1.ModelReplica:
    """Construct an observed ModelReplica pinned to a (cluster, index).

    Mirrors _deployment's single-engine mapping: pipeline sets the engine's node
    span and count its copies, so node cost is pipeline * count.
    """
    if engines is None:
        engines = [_replica_engine(pool=pool, copies=count, pipeline=pipeline)]
    return mrv1alpha1.ModelReplica(
        metadata=metav1.ObjectMeta(
            name=f"{deployment_name}-{cluster_name}-{index}",
            namespace="ml-team",
            labels={
                "modelplane.ai/deployment": deployment_name,
                "modelplane.ai/cluster": cluster_name,
                "modelplane.ai/replica-index": str(index),
            },
        ),
        spec=mrv1alpha1.SpecModel(clusterName=cluster_name, engines=engines),
    )


def _replica_with_pool(
    deployment_name: str,
    cluster_name: str,
    *,
    pool: str,
    index: int = 0,
    pipeline: int = 1,
    count: int = 1,
) -> mrv1alpha1.ModelReplica:
    """An observed ModelReplica pinned to a cluster AND a specific node pool."""
    return _replica(deployment_name, cluster_name, pool=pool, index=index, pipeline=pipeline, count=count)


def _collision_replica(
    object_name: str,
    cluster_name: str,
    *,
    pool: str,
    index: int = 0,
) -> mrv1alpha1.ModelReplica:
    """A my-model replica with an explicit object name, to force an identity clash.

    Carries the my-model deployment label and a chosen (cluster, index) so it
    collides with a normal my-model replica, while its distinct metadata.name is
    the tiebreak the scheduler sorts on.
    """
    r = _replica_with_pool("my-model", cluster_name, pool=pool, index=index)
    assert r.metadata is not None
    r.metadata.name = object_name
    return r


# Convenience: the resolved DeviceRequest for a default GPU request matching a
# default pool, used in expected candidates for nodeSelector cases.
def _resolved(name: str = "gpu", count: int = 1, cel_exprs: list[str] | None = None) -> scheduling.DeviceRequest:
    return scheduling.DeviceRequest(
        name=name,
        device_class_name="gpu.nvidia.com",
        count=count,
        cel_selectors=cel_exprs or [_MEM_141],
    )


def _placement(
    *,
    name: str = _ENGINE,
    pool: str = "default",
    device_requests: list[scheduling.DeviceRequest] | None = None,
    pipeline: int = 1,
) -> scheduling.EnginePlacement:
    """An expected EnginePlacement: one member placement per member.

    Mirrors _engine's member shape: pipeline == 1 is a single Standalone,
    pipeline > 1 a Leader plus a Worker, all on the same pool with the same
    resolved requests.
    """
    dr = device_requests if device_requests is not None else [_resolved()]
    if pipeline == 1:
        members = [scheduling.MemberPlacement(role="Standalone", pool=pool, device_requests=dr)]
    else:
        members = [
            scheduling.MemberPlacement(role="Leader", pool=pool, device_requests=dr),
            scheduling.MemberPlacement(role="Worker", pool=pool, device_requests=dr),
        ]
    return scheduling.EnginePlacement(name=name, members=members)


# Convenience: build an expected Candidate defaulting to index 0, so the many
# single-replica-per-cluster cases stay terse. A placed or retained replica
# resolves to one engine on the default pool with the default GPU request; a
# degraded/unplaced cluster carries no gateway. Cases that need a specific pool,
# request, or engine layout pass `engines` explicitly.
def _cand(
    name: str, *, index: int = 0, pool: str = "default", device_requests=None, pipeline: int = 1, engines=None, **kwargs
) -> scheduling.Candidate:
    if engines is None:
        engines = [_placement(pool=pool, device_requests=device_requests, pipeline=pipeline)]
    return scheduling.Candidate(name=name, index=index, engines=engines, **kwargs)


class TestSchedule(unittest.TestCase):
    """Tests for scheduling.schedule placement: retain, spread, scale, capacity.

    Deployments use the default single-GPU nodeSelector request (any pool's GPU
    device satisfies it), so these focus on placement rather than pool matching;
    TestScheduleNodeSelector covers request-to-device matching.
    """

    def test_schedule(self) -> None:
        """The scheduler retains existing pins and places new replicas."""

        cases = [
            Case(
                name="no clusters returns no candidates",
                deployment=_deployment(),
                clusters=[],
                all_replicas=[],
                want=[],
            ),
            Case(
                name="single ready cluster is picked",
                deployment=_deployment(),
                clusters=[_cluster("cluster-a")],
                all_replicas=[],
                want=[_cand(name="cluster-a", gateway_address="10.0.0.1", pool="default")],
            ),
            Case(
                name="not-ready cluster is not picked for a new replica",
                deployment=_deployment(),
                clusters=[_cluster("cluster-a", ready=False)],
                all_replicas=[],
                want=[],
            ),
            Case(
                name="cluster without gateway address is not picked",
                deployment=_deployment(),
                clusters=[_cluster("cluster-a", gateway_address="")],
                all_replicas=[],
                want=[],
            ),
            Case(
                name="multi-node deployment needs enough nodes",
                deployment=_deployment(pipeline=4),
                clusters=[_cluster("cluster-a", pools=[_pool("default", nodes=2)])],
                all_replicas=[],
                want=[],
            ),
            Case(
                name="existing replica is retained on its pinned cluster",
                deployment=_deployment(),
                clusters=[_cluster("cluster-a"), _cluster("cluster-b", gateway_address="10.0.0.2")],
                all_replicas=[_replica_with_pool("my-model", "cluster-a", pool="default")],
                # cluster-a wins even though cluster-b is also viable. The pin
                # still matches, so it's retained with its resolved pool/requests.
                want=[_cand(name="cluster-a", gateway_address="10.0.0.1")],
            ),
            Case(
                name="degraded pinned cluster is retained with empty gateway",
                deployment=_deployment(),
                clusters=[_cluster("cluster-a", ready=False, gateway_address="")],
                all_replicas=[_replica_with_pool("my-model", "cluster-a", pool="default")],
                want=[_cand(name="cluster-a", gateway_address="")],
            ),
            Case(
                name="deleted pinned cluster triggers re-placement",
                deployment=_deployment(),
                clusters=[_cluster("cluster-b", gateway_address="10.0.0.2")],
                all_replicas=[_replica("my-model", "cluster-a")],
                want=[_cand(name="cluster-b", gateway_address="10.0.0.2", pool="default")],
            ),
            Case(
                name="scale up places new replicas on additional clusters",
                deployment=_deployment(replicas=2),
                clusters=[_cluster("cluster-a"), _cluster("cluster-b", gateway_address="10.0.0.2")],
                all_replicas=[_replica("my-model", "cluster-a")],
                want=[
                    _cand(name="cluster-a", gateway_address="10.0.0.1"),
                    _cand(name="cluster-b", gateway_address="10.0.0.2", pool="default"),
                ],
            ),
            Case(
                name="scale up with no extra capacity returns only retained",
                deployment=_deployment(replicas=2),
                # Single-node pool, already filled by the retained replica, so no
                # second replica can be placed - not even on the same cluster.
                clusters=[_cluster("cluster-a", pools=[_pool("default", nodes=1)])],
                all_replicas=[_replica_with_pool("my-model", "cluster-a", pool="default")],
                want=[_cand(name="cluster-a", gateway_address="10.0.0.1", pool="default")],
            ),
            Case(
                name="two replicas pack onto one cluster when it is the only option",
                deployment=_deployment(replicas=2),
                # One cluster, a 2-node pool, two 1-node replicas. With nowhere
                # to spread, both pack onto cluster-a at indices 0 and 1.
                clusters=[_cluster("cluster-a", pools=[_pool("default", nodes=2)])],
                all_replicas=[],
                want=[
                    _cand(name="cluster-a", index=0, gateway_address="10.0.0.1", pool="default"),
                    _cand(name="cluster-a", index=1, gateway_address="10.0.0.1", pool="default"),
                ],
            ),
            Case(
                name="two replicas spread across two clusters before packing",
                deployment=_deployment(replicas=2),
                # Both clusters can hold two replicas, but we prefer one each.
                clusters=[
                    _cluster("cluster-a", pools=[_pool("default", nodes=2)]),
                    _cluster("cluster-b", gateway_address="10.0.0.2", pools=[_pool("default", nodes=2)]),
                ],
                all_replicas=[],
                want=[
                    _cand(name="cluster-a", index=0, gateway_address="10.0.0.1", pool="default"),
                    _cand(name="cluster-b", index=0, gateway_address="10.0.0.2", pool="default"),
                ],
            ),
            Case(
                name="three replicas spread first then pack the remainder",
                deployment=_deployment(replicas=3),
                # Two clusters, plenty of room. Spread gives a, b one each, then
                # the third lands back on cluster-a (lowest load, name tiebreak).
                clusters=[
                    _cluster("cluster-a", pools=[_pool("default", nodes=4)]),
                    _cluster("cluster-b", gateway_address="10.0.0.2", pools=[_pool("default", nodes=4)]),
                ],
                all_replicas=[],
                want=[
                    _cand(name="cluster-a", index=0, gateway_address="10.0.0.1", pool="default"),
                    _cand(name="cluster-a", index=1, gateway_address="10.0.0.1", pool="default"),
                    _cand(name="cluster-b", index=0, gateway_address="10.0.0.2", pool="default"),
                ],
            ),
            Case(
                name="capacity forces packing past the spread preference",
                deployment=_deployment(replicas=3),
                # cluster-b holds one replica; cluster-a has room for the rest.
                # Spread puts one on each, then the third can't fit on b (full),
                # so it packs onto a.
                clusters=[
                    _cluster("cluster-a", pools=[_pool("default", nodes=4)]),
                    _cluster("cluster-b", gateway_address="10.0.0.2", pools=[_pool("default", nodes=1)]),
                ],
                all_replicas=[],
                want=[
                    _cand(name="cluster-a", index=0, gateway_address="10.0.0.1", pool="default"),
                    _cand(name="cluster-a", index=1, gateway_address="10.0.0.1", pool="default"),
                    _cand(name="cluster-b", index=0, gateway_address="10.0.0.2", pool="default"),
                ],
            ),
            Case(
                name="new replica spreads onto an empty cluster before doubling up",
                deployment=_deployment(replicas=2),
                # cluster-a already hosts a replica; cluster-b is empty. The new
                # replica prefers empty cluster-b over packing onto a.
                clusters=[
                    _cluster("cluster-a", pools=[_pool("default", nodes=4)]),
                    _cluster("cluster-b", gateway_address="10.0.0.2", pools=[_pool("default", nodes=4)]),
                ],
                all_replicas=[_replica_with_pool("my-model", "cluster-a", pool="default")],
                want=[
                    _cand(name="cluster-a", index=0, gateway_address="10.0.0.1", pool="default"),
                    _cand(name="cluster-b", index=0, gateway_address="10.0.0.2", pool="default"),
                ],
            ),
            Case(
                name="new replica takes the lowest free index on a packed cluster",
                deployment=_deployment(replicas=3),
                # Only cluster-a exists, already hosting indices 0 and 2 (1 was
                # deleted). The new replica fills the gap at index 1.
                clusters=[_cluster("cluster-a", pools=[_pool("default", nodes=4)])],
                all_replicas=[
                    _replica_with_pool("my-model", "cluster-a", pool="default", index=0),
                    _replica_with_pool("my-model", "cluster-a", pool="default", index=2),
                ],
                want=[
                    _cand(name="cluster-a", index=0, gateway_address="10.0.0.1", pool="default"),
                    _cand(name="cluster-a", index=1, gateway_address="10.0.0.1", pool="default"),
                    _cand(name="cluster-a", index=2, gateway_address="10.0.0.1", pool="default"),
                ],
            ),
            Case(
                name="scale down packs off by dropping the highest index first",
                deployment=_deployment(replicas=2),
                # cluster-a hosts indices 0 and 1; cluster-b hosts index 0. Three
                # replicas, want two. Highest index (a/1) is dropped, keeping the
                # spread across a/0 and b/0.
                clusters=[
                    _cluster("cluster-a", pools=[_pool("default", nodes=4)]),
                    _cluster("cluster-b", gateway_address="10.0.0.2", pools=[_pool("default", nodes=4)]),
                ],
                all_replicas=[
                    _replica_with_pool("my-model", "cluster-a", pool="default", index=0),
                    _replica_with_pool("my-model", "cluster-a", pool="default", index=1),
                    _replica_with_pool("my-model", "cluster-b", pool="default", index=0),
                ],
                want=[
                    _cand(name="cluster-a", index=0, gateway_address="10.0.0.1", pool="default"),
                    _cand(name="cluster-b", index=0, gateway_address="10.0.0.2", pool="default"),
                ],
            ),
            Case(
                name="retained replica is charged at its own node cost, not the new shape",
                # The deployment's workers grew to pipeline=4 (4 nodes/replica),
                # but the existing replica was created at pipeline=2 and is
                # retained (no nodeSelector change rolls it). It still consumes
                # only its original 2 nodes. The pool has 6, so a second replica
                # at the new 4-node cost must still fit (6 - 2 = 4). Regression:
                # charging the retained replica at the new shape (4) would leave
                # 2 free and wrongly refuse the placement.
                deployment=_deployment(replicas=2, pipeline=4),
                clusters=[_cluster("cluster-a", pools=[_pool("default", nodes=6)])],
                all_replicas=[_replica_with_pool("my-model", "cluster-a", pool="default", pipeline=2)],
                # The retained replica is re-stamped to the deployment's current
                # pipeline=4 shape but still charged its observed 2 nodes in the
                # ledger.
                want=[
                    _cand(name="cluster-a", index=0, gateway_address="10.0.0.1", pipeline=4),
                    _cand(name="cluster-a", index=1, gateway_address="10.0.0.1", pipeline=4),
                ],
            ),
            Case(
                name="scale down drops from the most-loaded cluster to preserve spread",
                deployment=_deployment(replicas=2),
                # cluster-a hosts two replicas, cluster-b one. Scaling 3->2 must
                # drop a's extra (a/1), NOT b's sole replica - otherwise we'd
                # leave a packed and b empty, the opposite of spread. b's index
                # is 3 (higher than a/1) to prove we drop by cluster load, not by
                # a global index comparison.
                clusters=[
                    _cluster("cluster-a", pools=[_pool("default", nodes=4)]),
                    _cluster("cluster-b", gateway_address="10.0.0.2", pools=[_pool("default", nodes=4)]),
                ],
                all_replicas=[
                    _replica_with_pool("my-model", "cluster-a", pool="default", index=0),
                    _replica_with_pool("my-model", "cluster-a", pool="default", index=1),
                    _replica_with_pool("my-model", "cluster-b", pool="default", index=3),
                ],
                want=[
                    _cand(name="cluster-a", index=0, gateway_address="10.0.0.1", pool="default"),
                    _cand(name="cluster-b", index=3, gateway_address="10.0.0.2", pool="default"),
                ],
            ),
            Case(
                name="co-located replicas are both retained across a reconcile",
                deployment=_deployment(replicas=2),
                clusters=[_cluster("cluster-a", pools=[_pool("default", nodes=4)])],
                all_replicas=[
                    _replica_with_pool("my-model", "cluster-a", pool="default", index=0),
                    _replica_with_pool("my-model", "cluster-a", pool="default", index=1),
                ],
                want=[
                    _cand(name="cluster-a", index=0, gateway_address="10.0.0.1", pool="default"),
                    _cand(name="cluster-a", index=1, gateway_address="10.0.0.1", pool="default"),
                ],
            ),
            Case(
                name="scale down across clusters drops higher cluster name at equal index",
                deployment=_deployment(replicas=1),
                clusters=[_cluster("cluster-a"), _cluster("cluster-b", gateway_address="10.0.0.2")],
                all_replicas=[
                    _replica("my-model", "cluster-b"),
                    _replica("my-model", "cluster-a"),
                ],
                # Both at index 0, so the (index, name) tiebreak keeps cluster-a.
                want=[_cand(name="cluster-a", gateway_address="10.0.0.1")],
            ),
            Case(
                name="new placement is alphabetical for determinism",
                deployment=_deployment(replicas=2),
                clusters=[
                    _cluster("cluster-c", gateway_address="10.0.0.3"),
                    _cluster("cluster-a"),
                    _cluster("cluster-b", gateway_address="10.0.0.2"),
                ],
                all_replicas=[],
                want=[
                    _cand(name="cluster-a", gateway_address="10.0.0.1", pool="default"),
                    _cand(name="cluster-b", gateway_address="10.0.0.2", pool="default"),
                ],
            ),
            Case(
                name="other deployment's replicas consume node capacity",
                deployment=_deployment(pipeline=1),
                clusters=[_cluster("cluster-a", pools=[_pool("default", nodes=1)])],
                # other-model occupies the single node on cluster-a.
                all_replicas=[_replica("other-model", "cluster-a")],
                want=[],
            ),
            Case(
                name="our own observed replicas don't double-count against us",
                deployment=_deployment(pipeline=1),
                clusters=[_cluster("cluster-a", pools=[_pool("default", nodes=1)])],
                all_replicas=[_replica_with_pool("my-model", "cluster-a", pool="default")],
                # Retained on its pin: the single node it already occupies isn't
                # charged against itself, so it stays rather than being evicted.
                want=[_cand(name="cluster-a", gateway_address="10.0.0.1")],
            ),
            Case(
                name="replica labeled for our deployment but pinned to unknown cluster is ignored",
                deployment=_deployment(),
                clusters=[_cluster("cluster-b", gateway_address="10.0.0.2")],
                all_replicas=[_replica("my-model", "cluster-a")],
                want=[_cand(name="cluster-b", gateway_address="10.0.0.2", pool="default")],
            ),
            Case(
                name="another deployment pinned to a deleted pool consumes no capacity",
                # other-model is pinned to pool "gone", which the cluster no
                # longer publishes. Its pods are pinned to a node label no node
                # carries, so they're unschedulable and occupy nothing. The one
                # published node on "frontier" is therefore free for our replica.
                # Charging the unattributable replica would wrongly report the
                # cluster full.
                deployment=_deployment(requests=[_request(name="gpu", cel_exprs=[_MEM_141])]),
                clusters=[_cluster("cluster-a", pools=[_pool("frontier", nodes=1)])],
                all_replicas=[_replica_with_pool("other-model", "cluster-a", pool="gone")],
                want=[
                    _cand(
                        name="cluster-a",
                        gateway_address="10.0.0.1",
                        pool="frontier",
                        device_requests=[_resolved()],
                    )
                ],
            ),
            Case(
                name="colliding (cluster, index) retains deterministically by replica name",
                # Two of our replicas collide on (cluster-a, index 0) with
                # different pinned pools. Retain keeps the first by replica name
                # (my-model-cluster-a-0 on "a" sorts before the "-dup" replica on
                # "b"), independent of input order, so the schedule is a function
                # of state not of delivery order. Both pools match, so either
                # would be a valid placement - only determinism is under test.
                deployment=_deployment(requests=[_request(name="gpu", cel_exprs=[_MEM_141])]),
                clusters=[
                    _cluster(
                        "cluster-a",
                        pools=[
                            _pool("a", devices=[_gpu_device()]),
                            _pool("b", devices=[_gpu_device()]),
                        ],
                    )
                ],
                all_replicas=[
                    _collision_replica("my-model-cluster-a-0-dup", "cluster-a", pool="b", index=0),
                    _replica_with_pool("my-model", "cluster-a", pool="a", index=0),
                ],
                want=[
                    _cand(
                        name="cluster-a",
                        gateway_address="10.0.0.1",
                        pool="a",
                        device_requests=[_resolved()],
                    )
                ],
            ),
        ]

        for case in cases:
            with self.subTest(case.name):
                got = scheduling.schedule(case.deployment, case.clusters, case.all_replicas)
                self.assertEqual(case.want, got, f"{case.name}: -want, +got")

    def test_fill_false_is_retain_only(self) -> None:
        """fill=False retains existing replicas but places no new ones.

        A caller passes fill=False when it can't yet trust the candidate set
        (for the ModelDeployment, when a referenced ModelCache is unresolved).
        Retain runs unconditionally; only the placement of new replicas is held.
        """
        cases = [
            Case(
                name="no replicas yet: nothing is placed",
                deployment=_deployment(),
                clusters=[_cluster("cluster-a")],
                all_replicas=[],
                want=[],
            ),
            Case(
                name="existing replica is retained despite fill=False",
                deployment=_deployment(),
                clusters=[_cluster("cluster-a")],
                all_replicas=[_replica_with_pool("my-model", "cluster-a", pool="default")],
                want=[_cand(name="cluster-a", gateway_address="10.0.0.1")],
            ),
            Case(
                name="scale-up shortfall is not filled, only the retained replica remains",
                deployment=_deployment(replicas=3),
                clusters=[_cluster("cluster-a"), _cluster("cluster-b", gateway_address="10.0.0.2")],
                all_replicas=[_replica_with_pool("my-model", "cluster-a", pool="default")],
                want=[_cand(name="cluster-a", gateway_address="10.0.0.1")],
            ),
        ]

        for case in cases:
            with self.subTest(case.name):
                got = scheduling.schedule(case.deployment, case.clusters, case.all_replicas, fill=False)
                self.assertEqual(case.want, got, f"{case.name}: -want, +got")


class TestScheduleNodeSelector(unittest.TestCase):
    """Tests for nodeSelector device-request matching and pool pinning."""

    def test_node_selector(self) -> None:
        cases = [
            Case(
                name="matching request picks the cluster and records the pool",
                deployment=_deployment(requests=[_request(cel_exprs=[_MEM_141])]),
                clusters=[_cluster("cluster-a", pools=[_pool("frontier")])],
                all_replicas=[],
                want=[
                    _cand(
                        name="cluster-a",
                        gateway_address="10.0.0.1",
                        pool="frontier",
                        device_requests=[_resolved()],
                    )
                ],
            ),
            Case(
                name="non-matching request filters the cluster out",
                deployment=_deployment(requests=[_request(cel_exprs=[_MEM_200])]),
                clusters=[_cluster("cluster-a", pools=[_pool("frontier")])],
                all_replicas=[],
                want=[],
            ),
            Case(
                name="device count not covered filters out",
                # Request 8 GPUs, pool device has only 4.
                deployment=_deployment(requests=[_request(count=8, cel_exprs=[_MEM_141])]),
                clusters=[_cluster("cluster-a", pools=[_pool("frontier", devices=[_gpu_device(count=4)])])],
                all_replicas=[],
                want=[],
            ),
            Case(
                name="published device count of zero satisfies no request",
                # A pool device published with count 0 must read as "none
                # available", not default to 1. Regression: `d.count or 1`
                # treated 0 as 1 and placed a replica whose ResourceClaim no
                # device could satisfy. The status schema permits 0 even though
                # an InferenceClass device count is floored at 1.
                deployment=_deployment(requests=[_request(count=1, cel_exprs=[_MEM_141])]),
                clusters=[_cluster("cluster-a", pools=[_pool("frontier", devices=[_gpu_device(count=0)])])],
                all_replicas=[],
                want=[],
            ),
            Case(
                name="published pool node count of zero hosts nothing",
                # An autoscaled-to-zero pool has a matching GPU device but no
                # nodes, so it can host no replica.
                deployment=_deployment(requests=[_request(count=1, cel_exprs=[_MEM_141])]),
                clusters=[_cluster("cluster-a", pools=[_pool("frontier", nodes=0)])],
                all_replicas=[],
                want=[],
            ),
            Case(
                name="synthetic NIC device matches but is not in resolved requests",
                deployment=_deployment(
                    requests=[
                        _request(name="gpu", cel_exprs=[_MEM_141]),
                        _request(name="nic", cel_exprs=[_IB]),
                    ]
                ),
                clusters=[
                    _cluster(
                        "cluster-a",
                        pools=[_pool("frontier", devices=[_gpu_device(), _nic_device()])],
                    )
                ],
                all_replicas=[],
                # Only the claim: DRA gpu request is resolved; the synthetic nic
                # matched for scheduling but isn't claimed.
                want=[
                    _cand(
                        name="cluster-a",
                        gateway_address="10.0.0.1",
                        pool="frontier",
                        device_requests=[_resolved(name="gpu")],
                    )
                ],
            ),
            Case(
                name="multi-device: missing NIC filters the pool out",
                deployment=_deployment(
                    requests=[
                        _request(name="gpu", cel_exprs=[_MEM_141]),
                        _request(name="nic", cel_exprs=[_IB]),
                    ]
                ),
                clusters=[_cluster("cluster-a", pools=[_pool("frontier", devices=[_gpu_device()])])],
                all_replicas=[],
                want=[],
            ),
            Case(
                name="two requests cannot both claim one single-count device",
                # Two distinct requests, each matching the same single GPU
                # device. DRA allocates distinct devices per request, so a
                # count:1 device can satisfy only one. The pool must not match.
                deployment=_deployment(
                    requests=[
                        _request(name="gpu-a", cel_exprs=[_MEM_141]),
                        _request(name="gpu-b", cel_exprs=[_MEM_141]),
                    ]
                ),
                clusters=[_cluster("cluster-a", pools=[_pool("frontier", devices=[_gpu_device(count=1)])])],
                all_replicas=[],
                want=[],
            ),
            Case(
                name="two requests against one device must fit within its count",
                # Two count:5 requests need 10 GPUs total; the device has 8.
                # Capacity is consumed across requests, so the pool must not
                # match (regression: an earlier version checked each request
                # against the full device count independently).
                deployment=_deployment(
                    requests=[
                        _request(name="gpu-a", count=5, cel_exprs=[_MEM_141]),
                        _request(name="gpu-b", count=5, cel_exprs=[_MEM_141]),
                    ]
                ),
                clusters=[_cluster("cluster-a", pools=[_pool("frontier", devices=[_gpu_device(count=8)])])],
                all_replicas=[],
                want=[],
            ),
            Case(
                name="two requests sharing a device fit when count covers both",
                # 8-GPU device, two count:4 requests = 8 total. Both resolve.
                deployment=_deployment(
                    requests=[
                        _request(name="gpu-a", count=4, cel_exprs=[_MEM_141]),
                        _request(name="gpu-b", count=4, cel_exprs=[_MEM_141]),
                    ]
                ),
                clusters=[_cluster("cluster-a", pools=[_pool("frontier", devices=[_gpu_device(count=8)])])],
                all_replicas=[],
                want=[
                    _cand(
                        name="cluster-a",
                        gateway_address="10.0.0.1",
                        pool="frontier",
                        device_requests=[
                            _resolved(name="gpu-a", count=4),
                            _resolved(name="gpu-b", count=4),
                        ],
                    )
                ],
            ),
            Case(
                name="first matching pool wins (deterministic)",
                # Both pools carry a claimable GPU; the synthetic NIC's link type
                # is the discriminator. Only the infiniband pool satisfies the
                # nic selector, so it wins regardless of pool order.
                deployment=_deployment(
                    requests=[
                        _request(name="gpu", cel_exprs=[_MEM_141]),
                        _request(name="nic", cel_exprs=[_IB]),
                    ]
                ),
                clusters=[
                    _cluster(
                        "cluster-a",
                        pools=[
                            _pool("dev", devices=[_gpu_device(), _nic_device(link_type="gpudirect-tcpx")]),
                            _pool("frontier", devices=[_gpu_device(), _nic_device(link_type="infiniband")]),
                        ],
                    )
                ],
                all_replicas=[],
                want=[
                    _cand(
                        name="cluster-a",
                        gateway_address="10.0.0.1",
                        pool="frontier",
                        device_requests=[_resolved(name="gpu")],
                    )
                ],
            ),
            Case(
                name="synthetic-only selector leaves nothing to claim, pool ineligible",
                # The sole request matches a synthetic NIC. The replica's serving
                # workload would have no ResourceClaim to bind GPUs through, so
                # the pool is not a viable host and nothing is scheduled.
                deployment=_deployment(requests=[_request(name="nic", cel_exprs=[_IB])]),
                clusters=[
                    _cluster(
                        "cluster-a",
                        pools=[_pool("frontier", devices=[_gpu_device(), _nic_device(link_type="infiniband")])],
                    )
                ],
                all_replicas=[],
                want=[],
            ),
            Case(
                name="retained replica keeps its pinned pool",
                deployment=_deployment(requests=[_request(cel_exprs=[_MEM_141])]),
                clusters=[_cluster("cluster-a", pools=[_pool("frontier")])],
                all_replicas=[_replica_with_pool("my-model", "cluster-a", pool="frontier")],
                want=[
                    _cand(
                        name="cluster-a",
                        gateway_address="10.0.0.1",
                        pool="frontier",
                        device_requests=[_resolved()],
                    )
                ],
            ),
            Case(
                name="selector drift re-places replica onto a now-matching pool",
                # A claimable GPU keeps both pools viable hosts; the synthetic
                # NIC's link type is the drifting discriminator.
                deployment=_deployment(
                    requests=[
                        _request(name="gpu", cel_exprs=[_MEM_141]),
                        _request(name="nic", cel_exprs=[_IB]),
                    ]
                ),
                clusters=[
                    _cluster(
                        "cluster-a",
                        pools=[
                            _pool("a", devices=[_gpu_device(), _nic_device(link_type="gpudirect-tcpx")]),
                            _pool("b", devices=[_gpu_device(), _nic_device(link_type="infiniband")]),
                        ],
                    )
                ],
                all_replicas=[_replica_with_pool("my-model", "cluster-a", pool="a")],
                want=[
                    _cand(
                        name="cluster-a",
                        gateway_address="10.0.0.1",
                        pool="b",
                        device_requests=[_resolved(name="gpu")],
                    )
                ],
            ),
            Case(
                name="pinned pool that still matches stays pinned (attribute drift is sticky)",
                deployment=_deployment(
                    requests=[
                        _request(name="gpu", cel_exprs=[_MEM_141]),
                        _request(name="nic", cel_exprs=[_IB]),
                    ]
                ),
                clusters=[
                    _cluster(
                        "cluster-a",
                        pools=[
                            _pool("a", devices=[_gpu_device(), _nic_device(link_type="infiniband")]),
                            _pool("b", devices=[_gpu_device(), _nic_device(link_type="infiniband")]),
                        ],
                    )
                ],
                all_replicas=[_replica_with_pool("my-model", "cluster-a", pool="a")],
                want=[
                    _cand(
                        name="cluster-a",
                        gateway_address="10.0.0.1",
                        pool="a",
                        device_requests=[_resolved(name="gpu")],
                    )
                ],
            ),
            Case(
                name="no matching pool anywhere drops the replica entirely",
                deployment=_deployment(
                    requests=[
                        _request(name="gpu", cel_exprs=[_MEM_141]),
                        _request(name="nic", cel_exprs=[_IB]),
                    ]
                ),
                clusters=[
                    _cluster(
                        "cluster-a",
                        pools=[_pool("a", devices=[_gpu_device(), _nic_device(link_type="gpudirect-tcpx")])],
                    )
                ],
                all_replicas=[_replica_with_pool("my-model", "cluster-a", pool="a")],
                want=[],
            ),
            Case(
                name="replica with no pool pin is re-placed when a selector now applies",
                deployment=_deployment(
                    requests=[
                        _request(name="gpu", cel_exprs=[_MEM_141]),
                        _request(name="nic", cel_exprs=[_IB]),
                    ]
                ),
                clusters=[
                    _cluster(
                        "cluster-a",
                        pools=[_pool("frontier", devices=[_gpu_device(), _nic_device(link_type="infiniband")])],
                    )
                ],
                all_replicas=[_replica("my-model", "cluster-a")],
                want=[
                    _cand(
                        name="cluster-a",
                        gateway_address="10.0.0.1",
                        pool="frontier",
                        device_requests=[_resolved(name="gpu")],
                    )
                ],
            ),
            Case(
                name="dropping a non-matching replica frees its node for the refill",
                # a/0 is pinned to a pool that still matches (retained). a/1 is
                # pinned to a pool no longer published, so it's dropped and will
                # be re-placed. The pool has just 2 nodes; both are notionally in
                # use by a/0 and a/1. The refill must see a/1's node freeing up
                # (it's being deleted) and re-place onto frontier at index 1.
                # Regression: the ledger must not charge dropped replicas.
                deployment=_deployment(replicas=2, requests=[_request(name="gpu", cel_exprs=[_MEM_141])]),
                clusters=[_cluster("cluster-a", pools=[_pool("frontier", nodes=2)])],
                all_replicas=[
                    _replica_with_pool("my-model", "cluster-a", pool="frontier", index=0),
                    _replica_with_pool("my-model", "cluster-a", pool="gone", index=1),
                ],
                want=[
                    _cand(
                        name="cluster-a",
                        index=0,
                        gateway_address="10.0.0.1",
                        pool="frontier",
                        device_requests=[_resolved()],
                    ),
                    _cand(
                        name="cluster-a",
                        index=1,
                        gateway_address="10.0.0.1",
                        pool="frontier",
                        device_requests=[_resolved()],
                    ),
                ],
            ),
            Case(
                name="device count is checked against the pinned pool, not a cluster-wide sum",
                # Request 8 GPUs. Pool 'a' has 4/node (doesn't fit); pool 'b'
                # has 8 and does. The replica must pin to 'b'.
                deployment=_deployment(requests=[_request(count=8, cel_exprs=[_MEM_141])]),
                clusters=[
                    _cluster(
                        "cluster-a",
                        pools=[
                            _pool("a", devices=[_gpu_device(count=4)]),
                            _pool("b", devices=[_gpu_device(count=8)]),
                        ],
                    )
                ],
                all_replicas=[],
                want=[
                    _cand(
                        name="cluster-a",
                        gateway_address="10.0.0.1",
                        pool="b",
                        device_requests=[_resolved(count=8)],
                    )
                ],
            ),
        ]

        for case in cases:
            with self.subTest(case.name):
                got = scheduling.schedule(case.deployment, case.clusters, case.all_replicas)
                self.assertEqual(case.want, got, f"{case.name}: -want, +got")

    def test_invalid_cel_raises(self) -> None:
        """A malformed expression raises CELCompileError (caller handles it)."""
        deployment = _deployment(requests=[_request(cel_exprs=["this is ) not valid ("])])
        with self.assertRaises(cel.CELCompileError):
            scheduling.schedule(deployment, [_cluster("cluster-a", pools=[_pool("frontier")])], [])


def _gang(
    leader_requests: list[mdv1alpha1.Device] | None,
    worker_requests: list[mdv1alpha1.Device] | None,
    *,
    worker_nodes: int = 1,
) -> mdv1alpha1.Engine:
    """A Leader/Worker engine with per-member (possibly heterogeneous) selectors.

    Either member's requests may be None, meaning that member carries no
    nodeSelector and claims nothing.
    """
    leader = mdv1alpha1.Member(role="Leader", template=_template())
    if leader_requests is not None:
        leader.nodeSelector = _node_selector(leader_requests)
    worker = mdv1alpha1.Member(role="Worker", worker=mdv1alpha1.Worker(nodes=worker_nodes), template=_template())
    if worker_requests is not None:
        worker.nodeSelector = _node_selector(worker_requests)
    return mdv1alpha1.Engine(name=_ENGINE, members=[leader, worker])


class TestScheduleMembers(unittest.TestCase):
    """Tests for per-member placement: single-pool engines, rejection when no
    pool fits, and claimless ride-along members."""

    def test_members(self) -> None:
        cases = [
            Case(
                name="a single pool satisfying every member hosts the whole engine",
                # The leader's request matches both pools; the worker's only
                # matches big. The whole-engine pass must put both members on
                # big - the one pool that satisfies them all.
                deployment=_deployment(
                    engines=[
                        _gang(
                            [_request(cel_exprs=[_MEM_141])],
                            [_request(cel_exprs=[_MEM_200])],
                        )
                    ]
                ),
                clusters=[
                    _cluster(
                        "cluster-a",
                        pools=[
                            _pool("small", devices=[_gpu_device(memory="141Gi")]),
                            _pool("big", devices=[_gpu_device(memory="200Gi")]),
                        ],
                    )
                ],
                all_replicas=[],
                want=[
                    scheduling.Candidate(
                        name="cluster-a",
                        index=0,
                        gateway_address="10.0.0.1",
                        engines=[
                            scheduling.EnginePlacement(
                                name=_ENGINE,
                                members=[
                                    scheduling.MemberPlacement(
                                        role="Leader", pool="big", device_requests=[_resolved()]
                                    ),
                                    scheduling.MemberPlacement(
                                        role="Worker",
                                        pool="big",
                                        device_requests=[_resolved(cel_exprs=[_MEM_200])],
                                    ),
                                ],
                            )
                        ],
                    )
                ],
            ),
            Case(
                name="members no single pool satisfies are not scheduled",
                # The leader only fits big (>= 200Gi); the worker only fits
                # small (< 200Gi). No single pool satisfies both. The scheduler
                # never splits an engine across pools - it can't tell whether
                # big and small share a fabric - so the engine is rejected and
                # the replica goes unplaced (#149).
                deployment=_deployment(
                    engines=[
                        _gang(
                            [_request(cel_exprs=[_MEM_200])],
                            [_request(cel_exprs=[_MEM_LT_200])],
                        )
                    ]
                ),
                clusters=[
                    _cluster(
                        "cluster-a",
                        pools=[
                            _pool("small", devices=[_gpu_device(memory="141Gi")]),
                            _pool("big", devices=[_gpu_device(memory="200Gi")]),
                        ],
                    )
                ],
                all_replicas=[],
                want=[],
            ),
            Case(
                name="a gang too big for its only matching pool is rejected",
                # Both members match only big (>= 141Gi); small (40Gi) matches
                # neither. big has one free node but the gang needs two. The
                # engine doesn't fit any single pool, so it's rejected; with big
                # the only cluster the replica goes unplaced.
                deployment=_deployment(
                    engines=[
                        _gang(
                            [_request(cel_exprs=[_MEM_141])],
                            [_request(cel_exprs=[_MEM_141])],
                        )
                    ]
                ),
                clusters=[
                    _cluster(
                        "cluster-a",
                        pools=[
                            _pool("small", nodes=8, devices=[_gpu_device(memory="40Gi")]),
                            _pool("big", nodes=1, devices=[_gpu_device(memory="141Gi")]),
                        ],
                    )
                ],
                all_replicas=[],
                want=[],
            ),
            Case(
                name="a gang too big for one cluster's pool lands whole on another",
                # Same gang. cluster-a's matching pool has only one free node
                # (too few for the two-member gang), so the scheduler rejects
                # cluster-a and places the whole gang on cluster-b, whose pool
                # has room for both members.
                deployment=_deployment(
                    engines=[
                        _gang(
                            [_request(cel_exprs=[_MEM_141])],
                            [_request(cel_exprs=[_MEM_141])],
                        )
                    ]
                ),
                clusters=[
                    _cluster(
                        "cluster-a",
                        gateway_address="10.0.0.1",
                        pools=[
                            _pool("small", nodes=8, devices=[_gpu_device(memory="40Gi")]),
                            _pool("big", nodes=1, devices=[_gpu_device(memory="141Gi")]),
                        ],
                    ),
                    _cluster(
                        "cluster-b",
                        gateway_address="10.0.0.2",
                        pools=[_pool("big", nodes=2, devices=[_gpu_device(memory="141Gi")])],
                    ),
                ],
                all_replicas=[],
                want=[
                    scheduling.Candidate(
                        name="cluster-b",
                        index=0,
                        gateway_address="10.0.0.2",
                        engines=[
                            scheduling.EnginePlacement(
                                name=_ENGINE,
                                members=[
                                    scheduling.MemberPlacement(
                                        role="Leader", pool="big", device_requests=[_resolved()]
                                    ),
                                    scheduling.MemberPlacement(
                                        role="Worker", pool="big", device_requests=[_resolved()]
                                    ),
                                ],
                            )
                        ],
                    )
                ],
            ),
            Case(
                name="a member claimable elsewhere is not stranded on a synthetic match",
                # On pool-a the leader's request matches only a Synthetic
                # device (nothing to claim) while the worker claims, so the
                # whole engine *could* land there - but pool-b satisfies the
                # leader claimably. The engine must go to pool-b; placing on
                # pool-a would run the leader without the GPU it asked for.
                deployment=_deployment(
                    engines=[
                        _gang(
                            [_request(cel_exprs=[_MEM_200])],
                            [_request(cel_exprs=[_MEM_141])],
                        )
                    ]
                ),
                clusters=[
                    _cluster(
                        "cluster-a",
                        pools=[
                            _pool(
                                "a",
                                devices=[
                                    _gpu_device(memory="141Gi"),
                                    _gpu_device(name="syn", claim="Synthetic", memory="200Gi"),
                                ],
                            ),
                            _pool("b", devices=[_gpu_device(memory="200Gi")]),
                        ],
                    )
                ],
                all_replicas=[],
                want=[
                    scheduling.Candidate(
                        name="cluster-a",
                        index=0,
                        gateway_address="10.0.0.1",
                        engines=[
                            scheduling.EnginePlacement(
                                name=_ENGINE,
                                members=[
                                    scheduling.MemberPlacement(
                                        role="Leader",
                                        pool="b",
                                        device_requests=[_resolved(cel_exprs=[_MEM_200])],
                                    ),
                                    scheduling.MemberPlacement(
                                        role="Worker",
                                        pool="b",
                                        device_requests=[_resolved(cel_exprs=[_MEM_141])],
                                    ),
                                ],
                            )
                        ],
                    )
                ],
            ),
            Case(
                name="a member synthetic-only everywhere places claimless with its gang",
                # The leader's request matches only the pool's synthetic NIC on
                # every pool - deliberate (a selector that pins without
                # claiming). It places claimless alongside the claiming worker.
                deployment=_deployment(
                    engines=[
                        _gang(
                            [_request(name="nic", cel_exprs=[_IB])],
                            [_request(cel_exprs=[_MEM_141])],
                        )
                    ]
                ),
                clusters=[
                    _cluster(
                        "cluster-a",
                        pools=[_pool("frontier", devices=[_gpu_device(), _nic_device()])],
                    )
                ],
                all_replicas=[],
                want=[
                    scheduling.Candidate(
                        name="cluster-a",
                        index=0,
                        gateway_address="10.0.0.1",
                        engines=[
                            scheduling.EnginePlacement(
                                name=_ENGINE,
                                members=[
                                    scheduling.MemberPlacement(role="Leader", pool="frontier", device_requests=[]),
                                    scheduling.MemberPlacement(
                                        role="Worker", pool="frontier", device_requests=[_resolved()]
                                    ),
                                ],
                            )
                        ],
                    )
                ],
            ),
            Case(
                name="a member that matches nowhere fails the whole replica",
                deployment=_deployment(
                    engines=[
                        _gang(
                            [_request(cel_exprs=[_MEM_141])],
                            [_request(cel_exprs=[_MEM_200])],
                        )
                    ]
                ),
                clusters=[_cluster("cluster-a", pools=[_pool("default", devices=[_gpu_device(memory="141Gi")])])],
                all_replicas=[],
                want=[],
            ),
            Case(
                name="a claimless leader rides along on its gang's pool at zero cost",
                # The leader carries no nodeSelector: it claims nothing, follows
                # the worker's pool, and costs no nodes - the 1-node pool fits
                # the whole gang because only the worker occupies a node.
                deployment=_deployment(engines=[_gang(None, [_request(cel_exprs=[_MEM_141])])]),
                clusters=[_cluster("cluster-a", pools=[_pool("frontier", nodes=1)])],
                all_replicas=[],
                want=[
                    scheduling.Candidate(
                        name="cluster-a",
                        index=0,
                        gateway_address="10.0.0.1",
                        engines=[
                            scheduling.EnginePlacement(
                                name=_ENGINE,
                                members=[
                                    scheduling.MemberPlacement(role="Leader", pool="frontier", device_requests=[]),
                                    scheduling.MemberPlacement(
                                        role="Worker", pool="frontier", device_requests=[_resolved()]
                                    ),
                                ],
                            )
                        ],
                    )
                ],
            ),
            Case(
                name="a retained replica's claimless member keeps its pin",
                deployment=_deployment(engines=[_gang(None, [_request(cel_exprs=[_MEM_141])])]),
                clusters=[_cluster("cluster-a", pools=[_pool("frontier", nodes=1)])],
                all_replicas=[
                    _replica(
                        "my-model",
                        "cluster-a",
                        engines=[
                            mrv1alpha1.Engine(
                                name=_ENGINE,
                                members=[
                                    mrv1alpha1.Member(
                                        role="Leader",
                                        nodePoolName="frontier",
                                        template=mrv1alpha1.Template(
                                            spec=mrv1alpha1.Spec(
                                                containers=[
                                                    mrv1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")
                                                ]
                                            )
                                        ),
                                    ),
                                    mrv1alpha1.Member(
                                        role="Worker",
                                        worker=mrv1alpha1.Worker(nodes=1),
                                        nodePoolName="frontier",
                                        deviceRequests=_replica_device_requests(),
                                        template=mrv1alpha1.Template(
                                            spec=mrv1alpha1.Spec(
                                                containers=[
                                                    mrv1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")
                                                ]
                                            )
                                        ),
                                    ),
                                ],
                            )
                        ],
                    )
                ],
                want=[
                    scheduling.Candidate(
                        name="cluster-a",
                        index=0,
                        gateway_address="10.0.0.1",
                        engines=[
                            scheduling.EnginePlacement(
                                name=_ENGINE,
                                members=[
                                    scheduling.MemberPlacement(role="Leader", pool="frontier", device_requests=[]),
                                    scheduling.MemberPlacement(
                                        role="Worker", pool="frontier", device_requests=[_resolved()]
                                    ),
                                ],
                            )
                        ],
                    )
                ],
            ),
            Case(
                name="another deployment's claimless member consumes no capacity",
                # other-model's gang occupies only its worker's node: its
                # claimless leader shares that node. The 2-node pool has 1 node
                # free, so our 1-node deployment fits. Charging the claimless
                # leader a node would wrongly report insufficient capacity.
                deployment=_deployment(),
                clusters=[_cluster("cluster-a", pools=[_pool("default", nodes=2)])],
                all_replicas=[
                    _replica(
                        "other-model",
                        "cluster-a",
                        engines=[
                            mrv1alpha1.Engine(
                                name="main",
                                members=[
                                    mrv1alpha1.Member(
                                        role="Leader",
                                        nodePoolName="default",
                                        template=mrv1alpha1.Template(
                                            spec=mrv1alpha1.Spec(
                                                containers=[
                                                    mrv1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")
                                                ]
                                            )
                                        ),
                                    ),
                                    mrv1alpha1.Member(
                                        role="Worker",
                                        worker=mrv1alpha1.Worker(nodes=1),
                                        nodePoolName="default",
                                        deviceRequests=_replica_device_requests(),
                                        template=mrv1alpha1.Template(
                                            spec=mrv1alpha1.Spec(
                                                containers=[
                                                    mrv1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")
                                                ]
                                            )
                                        ),
                                    ),
                                ],
                            )
                        ],
                    )
                ],
                want=[_cand(name="cluster-a", gateway_address="10.0.0.1")],
            ),
            Case(
                name="a member shape change re-places the replica",
                # The deployment grew a Worker (Standalone -> Leader+Worker).
                # The observed single-member replica no longer lines up, so it
                # is re-placed with the new shape.
                deployment=_deployment(engines=[_gang([_request()], [_request()])]),
                clusters=[_cluster("cluster-a", pools=[_pool("default", nodes=4)])],
                all_replicas=[_replica("my-model", "cluster-a")],
                want=[
                    scheduling.Candidate(
                        name="cluster-a",
                        index=0,
                        gateway_address="10.0.0.1",
                        engines=[
                            scheduling.EnginePlacement(
                                name=_ENGINE,
                                members=[
                                    scheduling.MemberPlacement(
                                        role="Leader", pool="default", device_requests=[_resolved()]
                                    ),
                                    scheduling.MemberPlacement(
                                        role="Worker", pool="default", device_requests=[_resolved()]
                                    ),
                                ],
                            )
                        ],
                    )
                ],
            ),
        ]

        for case in cases:
            with self.subTest(case.name):
                got = scheduling.schedule(case.deployment, case.clusters, case.all_replicas)
                self.assertEqual(case.want, got, f"{case.name}: -want, +got")


if __name__ == "__main__":
    unittest.main()
