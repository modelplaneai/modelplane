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
    """Construct a ModelDeployment with the given topology and device requests."""
    node_selector = mdv1alpha1.NodeSelector(devices=requests) if requests else None
    return mdv1alpha1.ModelDeployment(
        metadata=metav1.ObjectMeta(name=name, namespace="ml-team"),
        spec=mdv1alpha1.SpecModel(
            replicas=replicas,
            nodeSelector=node_selector,
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
            capacity=icv1alpha1.CapacityModel(gpuPools=[icv1alpha1.GpuPool(**p) for p in pools]),
        ),
    )


def _replica(
    deployment_name: str,
    cluster_name: str,
    *,
    tensor: int = 1,
    pipeline: int = 1,
    count: int = 1,
) -> mrv1alpha1.ModelReplica:
    """Construct an observed ModelReplica pinned to a cluster."""
    return mrv1alpha1.ModelReplica(
        metadata=metav1.ObjectMeta(
            name=f"{deployment_name}-{cluster_name}",
            namespace="ml-team",
            labels={
                "modelplane.ai/replica": "true",
                "modelplane.ai/deployment": deployment_name,
                "modelplane.ai/cluster": cluster_name,
            },
        ),
        spec=mrv1alpha1.SpecModel(
            clusterName=cluster_name,
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
    tensor: int = 1,
    pipeline: int = 1,
    count: int = 1,
) -> mrv1alpha1.ModelReplica:
    """An observed ModelReplica pinned to a cluster AND a node pool."""
    r = _replica(deployment_name, cluster_name, tensor=tensor, pipeline=pipeline, count=count)
    r.spec.nodePoolName = pool
    return r


# Convenience: the resolved DeviceRequest for a default GPU request matching a
# default pool, used in expected candidates for nodeSelector cases.
def _resolved(name: str = "gpu", count: int = 1, cel_exprs: list[str] | None = None) -> DeviceRequest:
    return DeviceRequest(
        name=name,
        device_class_name="gpu.nvidia.com",
        count=count,
        cel_selectors=cel_exprs or [_MEM_141],
    )


class TestSchedule(unittest.TestCase):
    """Tests for scheduling.schedule without a nodeSelector."""

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
                want=[Candidate(name="cluster-a", gateway_address="10.0.0.1", pool="default")],
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
                all_replicas=[_replica("my-model", "cluster-a")],
                # cluster-a wins even though cluster-b is also viable. No
                # nodeSelector, so pool/device_requests stay empty on retain.
                want=[Candidate(name="cluster-a", gateway_address="10.0.0.1")],
            ),
            Case(
                name="degraded pinned cluster is retained with empty gateway",
                deployment=_deployment(),
                clusters=[_cluster("cluster-a", ready=False, gateway_address="")],
                all_replicas=[_replica("my-model", "cluster-a")],
                want=[Candidate(name="cluster-a", gateway_address="")],
            ),
            Case(
                name="deleted pinned cluster triggers re-placement",
                deployment=_deployment(),
                clusters=[_cluster("cluster-b", gateway_address="10.0.0.2")],
                all_replicas=[_replica("my-model", "cluster-a")],
                want=[Candidate(name="cluster-b", gateway_address="10.0.0.2", pool="default")],
            ),
            Case(
                name="scale up places new replicas on additional clusters",
                deployment=_deployment(replicas=2),
                clusters=[_cluster("cluster-a"), _cluster("cluster-b", gateway_address="10.0.0.2")],
                all_replicas=[_replica("my-model", "cluster-a")],
                want=[
                    Candidate(name="cluster-a", gateway_address="10.0.0.1"),
                    Candidate(name="cluster-b", gateway_address="10.0.0.2", pool="default"),
                ],
            ),
            Case(
                name="scale up with no extra capacity returns only retained",
                deployment=_deployment(replicas=2),
                clusters=[_cluster("cluster-a")],
                all_replicas=[_replica("my-model", "cluster-a")],
                want=[Candidate(name="cluster-a", gateway_address="10.0.0.1")],
            ),
            Case(
                name="scale down keeps lexicographically earliest pins",
                deployment=_deployment(replicas=1),
                clusters=[_cluster("cluster-a"), _cluster("cluster-b", gateway_address="10.0.0.2")],
                all_replicas=[
                    _replica("my-model", "cluster-b"),
                    _replica("my-model", "cluster-a"),
                ],
                want=[Candidate(name="cluster-a", gateway_address="10.0.0.1")],
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
                    Candidate(name="cluster-a", gateway_address="10.0.0.1", pool="default"),
                    Candidate(name="cluster-b", gateway_address="10.0.0.2", pool="default"),
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
                all_replicas=[_replica("my-model", "cluster-a")],
                # Retained: keeps the replica's own (empty) pool pin, since
                # this deployment has no nodeSelector.
                want=[Candidate(name="cluster-a", gateway_address="10.0.0.1", pool="")],
            ),
            Case(
                name="replica labeled for our deployment but pinned to unknown cluster is ignored",
                deployment=_deployment(),
                clusters=[_cluster("cluster-b", gateway_address="10.0.0.2")],
                all_replicas=[_replica("my-model", "cluster-a")],
                want=[Candidate(name="cluster-b", gateway_address="10.0.0.2", pool="default")],
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
                    Candidate(
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
                    Candidate(
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
                    Candidate(
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
                deployment=_deployment(requests=[_request(name="nic", cel_exprs=[_IB])]),
                clusters=[
                    _cluster(
                        "cluster-a",
                        pools=[
                            _pool("dev", devices=[_nic_device(link_type="gpudirect-tcpx")]),
                            _pool("frontier", devices=[_nic_device(link_type="infiniband")]),
                        ],
                    )
                ],
                all_replicas=[],
                # nic is synthetic, so resolved requests is empty, but the pool
                # is still recorded.
                want=[Candidate(name="cluster-a", gateway_address="10.0.0.1", pool="frontier")],
            ),
            Case(
                name="retained replica keeps its pinned pool",
                deployment=_deployment(requests=[_request(cel_exprs=[_MEM_141])]),
                clusters=[_cluster("cluster-a", pools=[_pool("frontier")])],
                all_replicas=[_replica_with_pool("my-model", "cluster-a", pool="frontier")],
                want=[
                    Candidate(
                        name="cluster-a",
                        gateway_address="10.0.0.1",
                        pool="frontier",
                        device_requests=[_resolved()],
                    )
                ],
            ),
            Case(
                name="selector drift re-places replica onto a now-matching pool",
                deployment=_deployment(requests=[_request(name="nic", cel_exprs=[_IB])]),
                clusters=[
                    _cluster(
                        "cluster-a",
                        pools=[
                            _pool("a", devices=[_nic_device(link_type="gpudirect-tcpx")]),
                            _pool("b", devices=[_nic_device(link_type="infiniband")]),
                        ],
                    )
                ],
                all_replicas=[_replica_with_pool("my-model", "cluster-a", pool="a")],
                want=[Candidate(name="cluster-a", gateway_address="10.0.0.1", pool="b")],
            ),
            Case(
                name="pinned pool that still matches stays pinned (attribute drift is sticky)",
                deployment=_deployment(requests=[_request(name="nic", cel_exprs=[_IB])]),
                clusters=[
                    _cluster(
                        "cluster-a",
                        pools=[
                            _pool("a", devices=[_nic_device(link_type="infiniband")]),
                            _pool("b", devices=[_nic_device(link_type="infiniband")]),
                        ],
                    )
                ],
                all_replicas=[_replica_with_pool("my-model", "cluster-a", pool="a")],
                want=[Candidate(name="cluster-a", gateway_address="10.0.0.1", pool="a")],
            ),
            Case(
                name="no matching pool anywhere drops the replica entirely",
                deployment=_deployment(requests=[_request(name="nic", cel_exprs=[_IB])]),
                clusters=[_cluster("cluster-a", pools=[_pool("a", devices=[_nic_device(link_type="gpudirect-tcpx")])])],
                all_replicas=[_replica_with_pool("my-model", "cluster-a", pool="a")],
                want=[],
            ),
            Case(
                name="replica with no pool pin is re-placed when a selector now applies",
                deployment=_deployment(requests=[_request(name="nic", cel_exprs=[_IB])]),
                clusters=[
                    _cluster("cluster-a", pools=[_pool("frontier", devices=[_nic_device(link_type="infiniband")])])
                ],
                all_replicas=[_replica("my-model", "cluster-a")],
                want=[Candidate(name="cluster-a", gateway_address="10.0.0.1", pool="frontier")],
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
                    Candidate(
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


if __name__ == "__main__":
    unittest.main()
