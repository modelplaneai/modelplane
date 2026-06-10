"""Tests for the scheduling module.

Unit tests for the retain-then-place scheduler. These construct
Pydantic models directly and call schedule() to exercise the core
logic without the protobuf/gRPC ceremony of the fn tests.

Pool selection is driven by nodeSelector device requests (DRA CEL matched
against a pool's devices) plus the available-node gate. Per-node GPU count is
expressed as a device request's count, not derived from topology.
"""

import dataclasses
import unittest

from function import cel, scheduling
from function.scheduling import Candidate, DeviceRequest
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# A GPU memory selector reused across cases.
_MEM_141 = 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("141Gi")) >= 0'
_MEM_200 = 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("200Gi")) >= 0'
_IB = 'device.attributes["nic.nvidia.com"].linkType == "infiniband"'


@dataclasses.dataclass
class Case:
    """A test case for scheduling.schedule."""

    name: str
    deployment: mdv1alpha1.ModelDeployment
    clusters: list[icv1alpha1.InferenceCluster]
    all_replicas: list[mrv1alpha1.ModelReplica]
    want: list[Candidate]


def _request(name: str = "gpu", count: int = 1, cel_exprs: list[str] | None = None) -> mdv1alpha1.Device:
    """A nodeSelector device request."""
    return mdv1alpha1.Device(
        name=name,
        count=count,
        selectors=[mdv1alpha1.Selector(cel=c) for c in (cel_exprs or [_MEM_141])],
    )


def _deployment(
    name: str = "my-model",
    replicas: int = 1,
    tensor: int = 1,
    pipeline: int = 1,
    count: int = 1,
    requests: list[mdv1alpha1.Device] | None = None,
):
    """Construct a ModelDeployment with the given topology and device requests.

    nodeSelector is required, so callers that don't care about pool matching get
    a single default GPU request that any test pool's GPU device satisfies.
    """
    return mdv1alpha1.ModelDeployment(
        metadata=metav1.ObjectMeta(name=name, namespace="ml-team"),
        spec=mdv1alpha1.SpecModel(
            replicas=replicas,
            nodeSelector=mdv1alpha1.NodeSelector(devices=requests if requests is not None else [_request()]),
            workers=mdv1alpha1.Workers(
                count=count,
                topology=mdv1alpha1.Topology(tensor=tensor, pipeline=pipeline),
                template=mdv1alpha1.Template(
                    spec=mdv1alpha1.Spec(
                        containers=[mdv1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")],
                    ),
                ),
            ),
        ),
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
            lastTransitionTime="2025-01-01T00:00:00Z",
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


def _replica(
    deployment_name: str,
    cluster_name: str,
    *,
    pool: str = "default",
    index: int = 0,
    tensor: int = 1,
    pipeline: int = 1,
    count: int = 1,
) -> mrv1alpha1.ModelReplica:
    """Construct an observed ModelReplica pinned to a (cluster, index).

    nodePoolName and deviceRequests are XRD-required, so every observed replica
    carries them; the pool defaults to "default" for cases where the specific
    pool isn't material.
    """
    return mrv1alpha1.ModelReplica(
        metadata=metav1.ObjectMeta(
            name=f"{deployment_name}-{cluster_name}-{index}",
            namespace="ml-team",
            labels={
                "modelplane.ai/replica": "true",
                "modelplane.ai/deployment": deployment_name,
                "modelplane.ai/cluster": cluster_name,
                "modelplane.ai/replica-index": str(index),
            },
        ),
        spec=mrv1alpha1.SpecModel(
            clusterName=cluster_name,
            nodePoolName=pool,
            deviceRequests=[
                mrv1alpha1.DeviceRequest(
                    name="gpu",
                    deviceClassName="gpu.nvidia.com",
                    count=1,
                    selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                ),
            ],
            workers=mrv1alpha1.Workers(
                count=count,
                topology=mrv1alpha1.Topology(tensor=tensor, pipeline=pipeline),
                template=mrv1alpha1.Template(
                    spec=mrv1alpha1.Spec(
                        containers=[mrv1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")],
                    ),
                ),
            ),
        ),
    )


def _replica_with_pool(
    deployment_name: str,
    cluster_name: str,
    *,
    pool: str,
    index: int = 0,
    tensor: int = 1,
    pipeline: int = 1,
    count: int = 1,
) -> mrv1alpha1.ModelReplica:
    """An observed ModelReplica pinned to a cluster AND a specific node pool."""
    return _replica(
        deployment_name, cluster_name, pool=pool, index=index, tensor=tensor, pipeline=pipeline, count=count
    )


# Convenience: the resolved DeviceRequest for a default GPU request matching a
# default pool, used in expected candidates for nodeSelector cases.
def _resolved(name: str = "gpu", count: int = 1, cel_exprs: list[str] | None = None) -> DeviceRequest:
    return DeviceRequest(
        name=name,
        device_class_name="gpu.nvidia.com",
        count=count,
        cel_selectors=cel_exprs or [_MEM_141],
    )


# Convenience: build an expected Candidate defaulting to index 0, so the many
# single-replica-per-cluster cases stay terse. Since nodeSelector is required,
# a placed or retained replica resolves to the default pool's GPU request;
# degraded/unplaced cases pass pool="" and device_requests=[] explicitly.
def _cand(name: str, *, index: int = 0, **kwargs) -> Candidate:
    kwargs.setdefault("pool", "default")
    kwargs.setdefault("device_requests", [_resolved()])
    return Candidate(name=name, index=index, **kwargs)


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
                deployment=_deployment(tensor=1, pipeline=4),
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
                want=[
                    _cand(name="cluster-a", index=0, gateway_address="10.0.0.1", pool="default"),
                    _cand(name="cluster-a", index=1, gateway_address="10.0.0.1", pool="default"),
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
        ]

        for case in cases:
            with self.subTest(case.name):
                got = scheduling.schedule(case.deployment, case.clusters, case.all_replicas)
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


class TestCandidate(unittest.TestCase):
    """Tests for Candidate dataclass fields."""

    def test_prefill_placement_fields_default_empty(self):
        """A Candidate carries an optional prefill placement, empty by default."""
        c = scheduling.Candidate(name="c1", index=0)
        self.assertEqual(c.prefill_pool, "")
        self.assertEqual(c.prefill_device_requests, [])


def _dra_nic_device(*, link_type: str = "infiniband", count: int = 1) -> dict:
    """A DRA NIC device dict for a pool, satisfying the _IB selector."""
    return {
        "name": "nic",
        "claim": "DRA",
        "driver": "nic.nvidia.com",
        "deviceClassName": "nic.nvidia.com",
        "count": count,
        "attributes": {"linkType": {"string": link_type}},
    }


def _disagg_deployment(
    decode_req: mdv1alpha1.Device,
    prefill_req: mdv1alpha1.Device,
    replicas: int = 1,
    decode_count: int = 1,
    prefill_count: int = 1,
) -> mdv1alpha1.ModelDeployment:
    """A ModelDeployment with both decode (top-level workers) and prefill roles."""
    d = _deployment(replicas=replicas, count=decode_count, requests=[decode_req])
    d.spec.prefill = mdv1alpha1.Prefill(
        workers=mdv1alpha1.Workers(
            count=prefill_count,
            topology=mdv1alpha1.Topology(tensor=1, pipeline=1),
            template=mdv1alpha1.Template(
                spec=mdv1alpha1.Spec(
                    containers=[mdv1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")],
                ),
            ),
        ),
        nodeSelector=mdv1alpha1.NodeSelector(devices=[prefill_req]),
    )
    return d


class TestScheduleDisagg(unittest.TestCase):
    """Tests for joint decode+prefill placement in disaggregated deployments.

    A disagg replica = decode placement + prefill placement, BOTH on the same
    cluster, picked jointly from one ledger so capacity is never double-committed.
    """

    def test_decode_and_prefill_different_pools_one_cluster(self):
        """Decode and prefill placed on different pools of the same cluster.

        Cluster has two pools: 'gpu-big' (satisfies _MEM_141, decode) and
        'gpu-nic' (satisfies _IB, prefill). The scheduler must find the pair and
        record both on one Candidate.
        """
        decode_req = _request(name="gpu", cel_exprs=[_MEM_141])
        prefill_req = _request(name="nic", cel_exprs=[_IB])
        deployment = _disagg_deployment(decode_req, prefill_req)

        # gpu-big: DRA GPU device satisfying _MEM_141 (141 GiB memory).
        # gpu-nic: DRA NIC device satisfying _IB (infiniband link type).
        cluster = _cluster(
            "cluster-a",
            pools=[
                _pool("gpu-big", nodes=2, devices=[_gpu_device(memory="141Gi")]),
                _pool("gpu-nic", nodes=2, devices=[_dra_nic_device(link_type="infiniband")]),
            ],
        )

        got = scheduling.schedule(deployment, [cluster], all_replicas=[])

        self.assertEqual(len(got), 1, f"want 1 candidate, got {len(got)}: {got}")
        c = got[0]
        self.assertEqual(c.pool, "gpu-big", "decode pool should be gpu-big")
        self.assertEqual(c.prefill_pool, "gpu-nic", "prefill pool should be gpu-nic")
        self.assertTrue(len(c.prefill_device_requests) > 0, "prefill_device_requests must be non-empty")

    def test_shared_pool_capacity_sums(self):
        """When decode and prefill share one pool, their node costs sum against that pool.

        One cluster, one pool ('shared') with 1 node. Decode needs 1 node,
        prefill needs 1 node → 2 total > 1 available → no Candidate.
        With 2 nodes in the pool, one Candidate is produced with
        pool == prefill_pool == 'shared'.
        """
        decode_req = _request(name="gpu", cel_exprs=[_MEM_141])
        # Prefill selector also uses _MEM_141 so it matches the same GPU pool.
        prefill_req = _request(name="gpu", cel_exprs=[_MEM_141])
        deployment = _disagg_deployment(decode_req, prefill_req)

        # 1-node pool: decode(1) + prefill(1) = 2 > 1, should not fit.
        cluster_1 = _cluster(
            "cluster-a",
            pools=[_pool("shared", nodes=1, devices=[_gpu_device(memory="141Gi")])],
        )
        got = scheduling.schedule(deployment, [cluster_1], all_replicas=[])
        self.assertEqual(len(got), 0, f"1-node pool should not fit both roles; got {got}")

        # 2-node pool: decode(1) + prefill(1) = 2 == 2, should fit.
        cluster_2 = _cluster(
            "cluster-a",
            pools=[_pool("shared", nodes=2, devices=[_gpu_device(memory="141Gi")])],
        )
        got2 = scheduling.schedule(deployment, [cluster_2], all_replicas=[])
        self.assertEqual(len(got2), 1, f"2-node pool should fit both roles; got {got2}")
        c = got2[0]
        self.assertEqual(c.pool, "shared")
        self.assertEqual(c.prefill_pool, "shared")

    def test_no_feasible_pair_rejects_cluster(self):
        """A cluster where no pool satisfies the prefill selector yields no Candidates.

        Cluster has only one pool matching the decode selector. The prefill
        selector (_IB) has no matching pool, so the whole cluster is ineligible.
        """
        decode_req = _request(name="gpu", cel_exprs=[_MEM_141])
        prefill_req = _request(name="nic", cel_exprs=[_IB])
        deployment = _disagg_deployment(decode_req, prefill_req)

        # Only gpu-big: satisfies decode but NOT prefill (_IB requires NIC).
        cluster = _cluster(
            "cluster-a",
            pools=[_pool("gpu-big", nodes=2, devices=[_gpu_device(memory="141Gi")])],
        )

        got = scheduling.schedule(deployment, [cluster], all_replicas=[])
        self.assertEqual(len(got), 0, f"cluster with no prefill pool must yield 0 Candidates; got {got}")

    def test_ledger_charges_existing_replica_prefill_pool(self):
        """An existing disagg replica consumes its prefill pool's nodes too.

        One cluster, two pools:
          'gpu-big'  (decode, _MEM_141) — 2 nodes  (enough for two replicas' decodes)
          'gpu-nic'  (prefill, _IB)     — 1 node   (only enough for ONE prefill)

        An existing disagg ModelReplica already occupies 1 node of each pool.
        A deployment asking for replicas=2 retains the existing replica but must
        NOT place a second one — the gpu-nic prefill pool is fully consumed.

        Without the fix, _build_ledger only charges gpu-big (the decode pool),
        leaving gpu-nic with 1 free node. The fill phase then sees gpu-big free
        (2 total - 1 used = 1) and gpu-nic free (1 total - 0 wrongly charged = 1)
        and incorrectly places a second replica → 2 Candidates returned.

        With the fix, gpu-nic is also charged (1 total - 1 used = 0 free) and
        the fill phase correctly finds no room for a second prefill → 1 Candidate.
        """
        decode_req = _request(name="gpu", cel_exprs=[_MEM_141])
        prefill_req = _request(name="nic", cel_exprs=[_IB])
        deployment = _disagg_deployment(decode_req, prefill_req, replicas=2)

        # gpu-big has 2 nodes (decode pool — enough for 2 replicas).
        # gpu-nic has 1 node (prefill pool — only enough for 1 replica).
        cluster = _cluster(
            "cluster-a",
            pools=[
                _pool("gpu-big", nodes=2, devices=[_gpu_device(memory="141Gi")]),
                _pool("gpu-nic", nodes=1, devices=[_dra_nic_device(link_type="infiniband")]),
            ],
        )

        # Build an existing disagg replica that belongs to this deployment and
        # occupies 1 node from gpu-big (decode) and 1 node from gpu-nic (prefill).
        existing = _replica_with_pool("my-model", "cluster-a", pool="gpu-big", index=0)
        existing.spec.prefill = mrv1alpha1.Prefill(
            workers=mrv1alpha1.Workers(
                count=1,
                topology=mrv1alpha1.Topology(tensor=1, pipeline=1),
                template=mrv1alpha1.Template(
                    spec=mrv1alpha1.Spec(
                        containers=[mrv1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")],
                    ),
                ),
            ),
            nodePoolName="gpu-nic",
            deviceRequests=[
                mrv1alpha1.DeviceRequest(
                    name="nic",
                    deviceClassName="nic.nvidia.com",
                    count=1,
                    selectors=[mrv1alpha1.Selector(cel=_IB)],
                ),
            ],
        )

        got = scheduling.schedule(deployment, [cluster], all_replicas=[existing])

        # The existing replica is retained (index 0). The second cannot be placed
        # because gpu-nic (1 node total, 1 consumed) has 0 free nodes for prefill.
        # Without the fix this returns 2 Candidates (prefill pool not charged).
        self.assertEqual(
            len(got),
            1,
            f"prefill pool must be charged for existing disagg replicas; got {len(got)} candidates: {got}",
        )
        self.assertEqual(got[0].name, "cluster-a")
        self.assertEqual(got[0].index, 0)

    def test_retain_drops_when_prefill_pool_stops_matching(self):
        """A disagg replica is re-placed when its prefill pool no longer satisfies the selector.

        Two arms:
        1. Prefill selector still matches 'gpu-nic' → replica IS retained (1 Candidate,
           pool='gpu-big', prefill_pool='gpu-nic').
        2. Prefill selector changed to _MEM_200 which 'gpu-nic' (a NIC pool, no GPU memory)
           does NOT satisfy → replica is NOT retained; with no eligible pool for the new
           prefill selector, 0 Candidates are returned.

        Before the fix, _pinned_pool_still_matches only checks the decode pool, so arm 2
        wrongly retains the replica (returns 1 Candidate) instead of 0.
        """
        decode_req = _request(name="gpu", cel_exprs=[_MEM_141])

        # ARM 1: prefill selector still satisfied by 'gpu-nic' → retained.
        prefill_req_ok = _request(name="nic", cel_exprs=[_IB])
        deployment_ok = _disagg_deployment(decode_req, prefill_req_ok)

        # ARM 2: prefill selector now requires _MEM_200 — 'gpu-nic' is a NIC pool with no
        # GPU memory attribute, so it fails the new selector → re-placed, no eligible pool.
        prefill_req_drifted = _request(name="gpu2", cel_exprs=[_MEM_200])
        deployment_drifted = _disagg_deployment(decode_req, prefill_req_drifted)

        cluster = _cluster(
            "cluster-a",
            pools=[
                _pool("gpu-big", nodes=2, devices=[_gpu_device(memory="141Gi")]),
                _pool("gpu-nic", nodes=2, devices=[_dra_nic_device(link_type="infiniband")]),
            ],
        )

        # Build an existing disagg replica pinned to decode='gpu-big', prefill='gpu-nic'.
        existing = _replica_with_pool("my-model", "cluster-a", pool="gpu-big", index=0)
        existing.spec.prefill = mrv1alpha1.Prefill(
            workers=mrv1alpha1.Workers(
                count=1,
                topology=mrv1alpha1.Topology(tensor=1, pipeline=1),
                template=mrv1alpha1.Template(
                    spec=mrv1alpha1.Spec(
                        containers=[mrv1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")],
                    ),
                ),
            ),
            nodePoolName="gpu-nic",
            deviceRequests=[
                mrv1alpha1.DeviceRequest(
                    name="nic",
                    deviceClassName="nic.nvidia.com",
                    count=1,
                    selectors=[mrv1alpha1.Selector(cel=_IB)],
                ),
            ],
        )

        # ARM 1: prefill selector unchanged → replica must be retained.
        got_ok = scheduling.schedule(deployment_ok, [cluster], all_replicas=[existing])
        self.assertEqual(
            len(got_ok),
            1,
            f"arm1 (prefill still matches): want 1 retained Candidate, got {len(got_ok)}: {got_ok}",
        )
        self.assertEqual(got_ok[0].pool, "gpu-big", "arm1: decode pool must be gpu-big")
        self.assertEqual(got_ok[0].prefill_pool, "gpu-nic", "arm1: prefill pool must be gpu-nic")

        # ARM 2: prefill selector drifted → replica must NOT be retained; no eligible
        # pool for the new prefill selector → 0 Candidates.
        got_drifted = scheduling.schedule(deployment_drifted, [cluster], all_replicas=[existing])
        self.assertEqual(
            len(got_drifted),
            0,
            f"arm2 (prefill pool drifted): want 0 Candidates (re-place fails), got {len(got_drifted)}: {got_drifted}",
        )

    def test_decode_only_cluster_skipped_for_one_with_both(self):
        """The scheduler must not greedily commit to the decode-cheapest cluster.

        cluster-a (alphabetically first, so it wins the (load, name) tiebreak)
        has a decode pool but NO prefill pool. cluster-b has both. A greedy
        per-role placement would pick cluster-a for decode then fail to find a
        prefill pool there; the joint pair selection must skip cluster-a entirely
        and place the whole replica on cluster-b.
        """
        decode_req = _request(name="gpu", cel_exprs=[_MEM_141])
        prefill_req = _request(name="nic", cel_exprs=[_IB])
        deployment = _disagg_deployment(decode_req, prefill_req)

        cluster_a = _cluster(
            "cluster-a",
            pools=[_pool("gpu-only", nodes=2, devices=[_gpu_device(memory="141Gi")])],
        )
        cluster_b = _cluster(
            "cluster-b",
            pools=[
                _pool("gpu-big", nodes=2, devices=[_gpu_device(memory="141Gi")]),
                _pool("gpu-nic", nodes=2, devices=[_dra_nic_device(link_type="infiniband")]),
            ],
        )

        got = scheduling.schedule(deployment, [cluster_a, cluster_b], all_replicas=[])

        self.assertEqual(len(got), 1, f"want 1 candidate on cluster-b; got {got}")
        self.assertEqual(got[0].name, "cluster-b", "must skip the decode-only cluster-a")
        self.assertEqual(got[0].pool, "gpu-big")
        self.assertEqual(got[0].prefill_pool, "gpu-nic")


if __name__ == "__main__":
    unittest.main()
