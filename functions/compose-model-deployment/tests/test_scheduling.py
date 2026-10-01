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
from typing import Literal

import pytest
from function import cel, scheduling
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# The selectors the cases' device requests use. They're named because which one
# a case uses is often what it tests, such as _MEM_200 against _MEM_LT_200, and
# an 80-character literal would bury that. A request's selectors reach the
# Candidate unchanged, so the expected DeviceRequests use them too.
_MEM_141 = 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("141Gi")) >= 0'
_MEM_200 = 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("200Gi")) >= 0'
_MEM_LT_200 = 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("200Gi")) < 0'
_IB = 'device.attributes["nic.nvidia.com"].linkType == "infiniband"'

_Role = Literal["Standalone", "Leader", "Worker"]


@dataclasses.dataclass
class Case:
    """A test case for scheduling.schedule."""

    name: str
    deployment: mdv1alpha1.ModelDeployment
    clusters: list[icv1alpha1.InferenceCluster]
    all_replicas: list[mrv1alpha1.ModelReplica]
    fill: bool
    want: list[scheduling.Candidate]


def _member(*, role: _Role, worker_nodes: int | None, devices: list[mdv1alpha1.Device] | None) -> mdv1alpha1.Member:
    """A ModelDeployment member running vLLM, which claims nothing if devices is None."""
    return mdv1alpha1.Member(
        role=role,
        worker=mdv1alpha1.Worker(nodes=worker_nodes) if worker_nodes is not None else None,
        nodeSelector=mdv1alpha1.NodeSelector(devices=devices) if devices is not None else None,
        template=mdv1alpha1.Template(
            spec=mdv1alpha1.Spec(containers=[mdv1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")]),
        ),
    )


def _deployment(
    *,
    replicas: int,
    members: list[mdv1alpha1.Member],
    tolerations: list[mdv1alpha1.Toleration] | None,
) -> mdv1alpha1.ModelDeployment:
    """The ModelDeployment my-model, whose one engine, main, has the given members."""
    return mdv1alpha1.ModelDeployment(
        metadata=metav1.ObjectMeta(name="my-model", namespace="ml-team"),
        spec=mdv1alpha1.SpecModel1(
            replicas=replicas,
            template=mdv1alpha1.TemplateModel(
                spec=mdv1alpha1.SpecModel(
                    engines=[mdv1alpha1.Engine(name="main", copies=1, members=members)],
                    tolerations=tolerations,
                ),
            ),
        ),
    )


def _gpu_device(*, name: str, claim: Literal["DRA", "Synthetic"], count: int, memory: str) -> icv1alpha1.Device:
    """A gpu.nvidia.com GPU in a pool. Only a DRA device has a device class to claim it by."""
    return icv1alpha1.Device(
        name=name,
        claim=claim,
        driver="gpu.nvidia.com",
        deviceClassName="gpu.nvidia.com" if claim == "DRA" else None,
        count=count,
        capacity={"memory": icv1alpha1.Capacity(value=memory)},
    )


def _nic_device(*, link_type: str) -> icv1alpha1.Device:
    """A Synthetic NIC in a pool, which a nodeSelector can match but nothing claims."""
    return icv1alpha1.Device(
        name="nic",
        claim="Synthetic",
        driver="nic.nvidia.com",
        count=1,
        attributes={"linkType": icv1alpha1.Attributes(string=link_type)},
    )


def _cluster(
    *,
    name: str,
    gateway_hostname: str | None,
    ready: bool,
    pools: list[icv1alpha1.GpuPool],
    taints: list[icv1alpha1.Taint] | None,
    placement_labels: dict[str, str] | None,
) -> icv1alpha1.InferenceCluster:
    """An InferenceCluster with the given readiness, gateway hostname, GPU pools, taints and placement labels."""
    return icv1alpha1.InferenceCluster(
        metadata=metav1.ObjectMeta(name=name),
        spec=icv1alpha1.Spec(
            cluster=icv1alpha1.Cluster(
                source="Existing",
                existing=icv1alpha1.Existing(secretRef=icv1alpha1.SecretRef(name="k")),
            ),
            taints=taints,
            placement=(
                icv1alpha1.Placement(metadata=icv1alpha1.Metadata(labels=placement_labels))
                if placement_labels is not None
                else None
            ),
        ),
        status=icv1alpha1.Status(
            conditions=[
                icv1alpha1.Condition(
                    type="Ready",
                    status="True" if ready else "False",
                    reason="Available" if ready else "Unavailable",
                    lastTransitionTime=datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC),
                ),
            ],
            # The scheduler needs a hostname, not just an address, because an
            # InferenceGateway addresses a cluster by name.
            gateway=icv1alpha1.Gateway(address="10.0.0.1", hostname=gateway_hostname),
            providerConfigRef=icv1alpha1.ProviderConfigRef(name=name),
            gpuPools=pools,
        ),
    )


def _replica_member(
    *,
    role: _Role,
    worker_nodes: int | None,
    pool: str,
    device_requests: list[mrv1alpha1.DeviceRequest] | None,
) -> mrv1alpha1.Member:
    """A ModelReplica member running vLLM, pinned to pool, with its device requests resolved."""
    return mrv1alpha1.Member(
        role=role,
        worker=mrv1alpha1.Worker(nodes=worker_nodes) if worker_nodes is not None else None,
        nodePoolName=pool,
        deviceRequests=device_requests,
        template=mrv1alpha1.Template(
            spec=mrv1alpha1.Spec(containers=[mrv1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")]),
        ),
    )


def _replica(
    *, name: str, deployment: str, cluster: str, index: int, members: list[mrv1alpha1.Member]
) -> mrv1alpha1.ModelReplica:
    """An observed ModelReplica of deployment, pinned to (cluster, index), whose one engine, main, has members."""
    return mrv1alpha1.ModelReplica(
        metadata=metav1.ObjectMeta(
            name=name,
            namespace="ml-team",
            labels={
                "modelplane.ai/deployment": deployment,
                "modelplane.ai/cluster": cluster,
                "modelplane.ai/replica-index": str(index),
            },
        ),
        spec=mrv1alpha1.SpecModel(
            clusterName=cluster, engines=[mrv1alpha1.Engine(name="main", copies=1, members=members)]
        ),
    )


def _candidate(
    *,
    name: str,
    index: int,
    gateway_hostname: str,
    placement_labels: dict[str, str],
    members: list[scheduling.MemberPlacement],
) -> scheduling.Candidate:
    """An expected Candidate for (name, index), whose one engine, main, places members."""
    return scheduling.Candidate(
        name=name,
        index=index,
        gateway_hostname=gateway_hostname,
        placement_labels=placement_labels,
        engines=[scheduling.EnginePlacement(name="main", members=members)],
    )


SCHEDULE_CASES = [
    # Placement: retain, spread, scale and capacity. Every member requests one
    # GPU matching _MEM_141, which every pool's 141Gi GPU satisfies, so these
    # cases focus on placement rather than pool matching.
    Case(
        name="no clusters returns no candidates",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[],
        all_replicas=[],
        fill=True,
        want=[],
    ),
    Case(
        name="single ready cluster is picked",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    Case(
        name="not-ready cluster is not picked for a new replica",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=False,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[],
    ),
    Case(
        name="cluster without a gateway hostname is not picked",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname=None,
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[],
    ),
    Case(
        name="multi-node deployment needs enough nodes",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Leader",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                ),
                _member(
                    role="Worker",
                    worker_nodes=3,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                ),
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[],
    ),
    # cluster-a wins even though cluster-b is also viable. The pin
    # still matches, so it's retained with its resolved pool/requests.
    Case(
        name="existing replica is retained on its pinned cluster",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
            _cluster(
                name="cluster-b",
                gateway_hostname="cluster-b.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    Case(
        name="degraded pinned cluster is retained with empty gateway",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname=None,
                ready=False,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    Case(
        name="deleted pinned cluster triggers re-placement",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-b",
                gateway_hostname="cluster-b.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-b",
                index=0,
                gateway_hostname="cluster-b.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    Case(
        name="scale up places new replicas on additional clusters",
        deployment=_deployment(
            replicas=2,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
            _cluster(
                name="cluster-b",
                gateway_hostname="cluster-b.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
            _candidate(
                name="cluster-b",
                index=0,
                gateway_hostname="cluster-b.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # Single-node pool, already filled by the retained replica, so no
    # second replica can be placed - not even on the same cluster.
    Case(
        name="scale up with no extra capacity returns only retained",
        deployment=_deployment(
            replicas=2,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=1, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # One cluster, a 2-node pool, two 1-node replicas. With nowhere
    # to spread, both pack onto cluster-a at indices 0 and 1.
    Case(
        name="two replicas pack onto one cluster when it is the only option",
        deployment=_deployment(
            replicas=2,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
            _candidate(
                name="cluster-a",
                index=1,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # Both clusters can hold two replicas, but we prefer one each.
    Case(
        name="two replicas spread across two clusters before packing",
        deployment=_deployment(
            replicas=2,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
            _cluster(
                name="cluster-b",
                gateway_hostname="cluster-b.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
            _candidate(
                name="cluster-b",
                index=0,
                gateway_hostname="cluster-b.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # Two clusters, plenty of room. Spread gives a, b one each, then
    # the third lands back on cluster-a (lowest load, name tiebreak).
    Case(
        name="three replicas spread first then pack the remainder",
        deployment=_deployment(
            replicas=3,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=4, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
            _cluster(
                name="cluster-b",
                gateway_hostname="cluster-b.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=4, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
            _candidate(
                name="cluster-a",
                index=1,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
            _candidate(
                name="cluster-b",
                index=0,
                gateway_hostname="cluster-b.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # cluster-b holds one replica; cluster-a has room for the rest.
    # Spread puts one on each, then the third can't fit on b (full),
    # so it packs onto a.
    Case(
        name="capacity forces packing past the spread preference",
        deployment=_deployment(
            replicas=3,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=4, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
            _cluster(
                name="cluster-b",
                gateway_hostname="cluster-b.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=1, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
            _candidate(
                name="cluster-a",
                index=1,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
            _candidate(
                name="cluster-b",
                index=0,
                gateway_hostname="cluster-b.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # cluster-a already hosts a replica; cluster-b is empty. The new
    # replica prefers empty cluster-b over packing onto a.
    Case(
        name="new replica spreads onto an empty cluster before doubling up",
        deployment=_deployment(
            replicas=2,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=4, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
            _cluster(
                name="cluster-b",
                gateway_hostname="cluster-b.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=4, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
            _candidate(
                name="cluster-b",
                index=0,
                gateway_hostname="cluster-b.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # Only cluster-a exists, already hosting indices 0 and 2 (1 was
    # deleted). The new replica fills the gap at index 1.
    Case(
        name="new replica takes the lowest free index on a packed cluster",
        deployment=_deployment(
            replicas=3,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=4, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            ),
            _replica(
                name="my-model-cluster-a-2",
                deployment="my-model",
                cluster="cluster-a",
                index=2,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            ),
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
            _candidate(
                name="cluster-a",
                index=1,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
            _candidate(
                name="cluster-a",
                index=2,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # cluster-a hosts indices 0 and 1; cluster-b hosts index 0. Three
    # replicas, want two. Highest index (a/1) is dropped, keeping the
    # spread across a/0 and b/0.
    Case(
        name="scale down packs off by dropping the highest index first",
        deployment=_deployment(
            replicas=2,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=4, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
            _cluster(
                name="cluster-b",
                gateway_hostname="cluster-b.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=4, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            ),
            _replica(
                name="my-model-cluster-a-1",
                deployment="my-model",
                cluster="cluster-a",
                index=1,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            ),
            _replica(
                name="my-model-cluster-b-0",
                deployment="my-model",
                cluster="cluster-b",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            ),
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
            _candidate(
                name="cluster-b",
                index=0,
                gateway_hostname="cluster-b.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # The deployment's Worker grew to 3 nodes (4 nodes/replica), but
    # the existing replica was created with a 1-node Worker (2
    # nodes/replica) and is retained (no nodeSelector change rolls it).
    # It's re-stamped to the deployment's current 4-node shape, but still
    # consumes only its original 2 nodes in the ledger. The pool has 6, so a
    # second replica at the new 4-node cost must still fit (6 - 2 = 4).
    # Regression: charging the retained replica at the new shape (4)
    # would leave 2 free and wrongly refuse the placement.
    Case(
        name="retained replica is charged at its own node cost, not the new shape",
        deployment=_deployment(
            replicas=2,
            members=[
                _member(
                    role="Leader",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                ),
                _member(
                    role="Worker",
                    worker_nodes=3,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                ),
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=6, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Leader",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    ),
                    _replica_member(
                        role="Worker",
                        worker_nodes=1,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    ),
                ],
            )
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Leader",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                    scheduling.MemberPlacement(
                        role="Worker",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
            _candidate(
                name="cluster-a",
                index=1,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Leader",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                    scheduling.MemberPlacement(
                        role="Worker",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # cluster-a hosts two replicas, cluster-b one. Scaling 3->2 must
    # drop a's extra (a/1), NOT b's sole replica - otherwise we'd
    # leave a packed and b empty, the opposite of spread. b's index
    # is 3 (higher than a/1) to prove we drop by cluster load, not by
    # a global index comparison.
    Case(
        name="scale down drops from the most-loaded cluster to preserve spread",
        deployment=_deployment(
            replicas=2,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=4, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
            _cluster(
                name="cluster-b",
                gateway_hostname="cluster-b.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=4, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            ),
            _replica(
                name="my-model-cluster-a-1",
                deployment="my-model",
                cluster="cluster-a",
                index=1,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            ),
            _replica(
                name="my-model-cluster-b-3",
                deployment="my-model",
                cluster="cluster-b",
                index=3,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            ),
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
            _candidate(
                name="cluster-b",
                index=3,
                gateway_hostname="cluster-b.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    Case(
        name="co-located replicas are both retained across a reconcile",
        deployment=_deployment(
            replicas=2,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=4, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            ),
            _replica(
                name="my-model-cluster-a-1",
                deployment="my-model",
                cluster="cluster-a",
                index=1,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            ),
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
            _candidate(
                name="cluster-a",
                index=1,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # Both at index 0, so the (index, name) tiebreak keeps cluster-a.
    Case(
        name="scale down across clusters drops higher cluster name at equal index",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
            _cluster(
                name="cluster-b",
                gateway_hostname="cluster-b.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-b-0",
                deployment="my-model",
                cluster="cluster-b",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            ),
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            ),
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    Case(
        name="new placement is alphabetical for determinism",
        deployment=_deployment(
            replicas=2,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-c",
                gateway_hostname="cluster-c.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
            _cluster(
                name="cluster-b",
                gateway_hostname="cluster-b.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
            _candidate(
                name="cluster-b",
                index=0,
                gateway_hostname="cluster-b.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # other-model occupies the single node on cluster-a.
    Case(
        name="other deployment's replicas consume node capacity",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=1, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="other-model-cluster-a-0",
                deployment="other-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=True,
        want=[],
    ),
    # Retained on its pin: the single node it already occupies isn't
    # charged against itself, so it stays rather than being evicted.
    Case(
        name="our own observed replicas don't double-count against us",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=1, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # other-model is pinned to pool "gone", which the cluster no
    # longer publishes. Its pods are pinned to a node label no node
    # carries, so they're unschedulable and occupy nothing. The one
    # published node on "frontier" is therefore free for our replica.
    # Charging the unattributable replica would wrongly report the
    # cluster full.
    Case(
        name="another deployment pinned to a deleted pool consumes no capacity",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="frontier",
                        nodes=1,
                        devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")],
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="other-model-cluster-a-0",
                deployment="other-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="gone",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="frontier",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # Two of our replicas collide on (cluster-a, index 0) with
    # different pinned pools. Retain keeps the first by replica name
    # (my-model-cluster-a-0 on "a" sorts before the "-dup" replica on
    # "b"), independent of input order, so the schedule is a function
    # of state not of delivery order. Both pools match, so either
    # would be a valid placement - only determinism is under test.
    Case(
        name="colliding (cluster, index) retains deterministically by replica name",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="a", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    ),
                    icv1alpha1.GpuPool(
                        name="b", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    ),
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0-dup",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="b",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            ),
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="a",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            ),
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="a",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # fill=False retains existing replicas but places no new ones. A caller
    # passes fill=False when it can't yet trust the candidate set (for the
    # ModelDeployment, when a referenced ModelCache is unresolved). Retain runs
    # unconditionally; only the placement of new replicas is held.
    Case(
        name="no replicas yet: nothing is placed",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=False,
        want=[],
    ),
    Case(
        name="existing replica is retained despite fill=False",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=False,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    Case(
        name="scale-up shortfall is not filled, only the retained replica remains",
        deployment=_deployment(
            replicas=3,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
            _cluster(
                name="cluster-b",
                gateway_hostname="cluster-b.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=False,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # nodeSelector device-request matching and pool pinning.
    Case(
        name="matching request picks the cluster and records the pool",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="frontier",
                        nodes=2,
                        devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")],
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="frontier",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    Case(
        name="non-matching request filters the cluster out",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_200)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="frontier",
                        nodes=2,
                        devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")],
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[],
    ),
    # Request 8 GPUs, pool device has only 4.
    Case(
        name="device count not covered filters out",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=8, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="frontier",
                        nodes=2,
                        devices=[_gpu_device(name="gpu", claim="DRA", count=4, memory="141Gi")],
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[],
    ),
    # A pool device published with count 0 must read as "none
    # available", not default to 1. Regression: `d.count or 1`
    # treated 0 as 1 and placed a replica whose ResourceClaim no
    # device could satisfy. The status schema permits 0 even though
    # an InferenceClass device count is floored at 1.
    Case(
        name="published device count of zero satisfies no request",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="frontier",
                        nodes=2,
                        devices=[_gpu_device(name="gpu", claim="DRA", count=0, memory="141Gi")],
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[],
    ),
    # An autoscaled-to-zero pool has a matching GPU device but no
    # nodes, so it can host no replica.
    Case(
        name="published pool node count of zero hosts nothing",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="frontier",
                        nodes=0,
                        devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")],
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[],
    ),
    # Only the claim: DRA gpu request is resolved; the synthetic nic
    # matched for scheduling but isn't claimed.
    Case(
        name="synthetic NIC device matches but is not in resolved requests",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[
                        mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)]),
                        mdv1alpha1.Device(name="nic", count=1, selectors=[mdv1alpha1.Selector(cel=_IB)]),
                    ],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="frontier",
                        nodes=2,
                        devices=[
                            _gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi"),
                            _nic_device(link_type="infiniband"),
                        ],
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="frontier",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    Case(
        name="multi-device: missing NIC filters the pool out",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[
                        mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)]),
                        mdv1alpha1.Device(name="nic", count=1, selectors=[mdv1alpha1.Selector(cel=_IB)]),
                    ],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="frontier",
                        nodes=2,
                        devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")],
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[],
    ),
    # Two distinct requests, each matching the same single GPU
    # device. DRA allocates distinct devices per request, so a
    # count:1 device can satisfy only one. The pool must not match.
    Case(
        name="two requests cannot both claim one single-count device",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[
                        mdv1alpha1.Device(name="gpu-a", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)]),
                        mdv1alpha1.Device(name="gpu-b", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)]),
                    ],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="frontier",
                        nodes=2,
                        devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")],
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[],
    ),
    # Two count:5 requests need 10 GPUs total; the device has 8.
    # Capacity is consumed across requests, so the pool must not
    # match (regression: an earlier version checked each request
    # against the full device count independently).
    Case(
        name="two requests against one device must fit within its count",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[
                        mdv1alpha1.Device(name="gpu-a", count=5, selectors=[mdv1alpha1.Selector(cel=_MEM_141)]),
                        mdv1alpha1.Device(name="gpu-b", count=5, selectors=[mdv1alpha1.Selector(cel=_MEM_141)]),
                    ],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="frontier",
                        nodes=2,
                        devices=[_gpu_device(name="gpu", claim="DRA", count=8, memory="141Gi")],
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[],
    ),
    # 8-GPU device, two count:4 requests = 8 total. Both resolve.
    Case(
        name="two requests sharing a device fit when count covers both",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[
                        mdv1alpha1.Device(name="gpu-a", count=4, selectors=[mdv1alpha1.Selector(cel=_MEM_141)]),
                        mdv1alpha1.Device(name="gpu-b", count=4, selectors=[mdv1alpha1.Selector(cel=_MEM_141)]),
                    ],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="frontier",
                        nodes=2,
                        devices=[_gpu_device(name="gpu", claim="DRA", count=8, memory="141Gi")],
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="frontier",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu-a",
                                device_class_name="gpu.nvidia.com",
                                count=4,
                                cel_selectors=[_MEM_141],
                            ),
                            scheduling.DeviceRequest(
                                name="gpu-b",
                                device_class_name="gpu.nvidia.com",
                                count=4,
                                cel_selectors=[_MEM_141],
                            ),
                        ],
                    ),
                ],
            ),
        ],
    ),
    # Both pools carry a claimable GPU; the synthetic NIC's link type
    # is the discriminator. Only the infiniband pool satisfies the
    # nic selector, so it's picked though it's listed second.
    Case(
        name="only the pool whose NIC matches the selector is picked",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[
                        mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)]),
                        mdv1alpha1.Device(name="nic", count=1, selectors=[mdv1alpha1.Selector(cel=_IB)]),
                    ],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="dev",
                        nodes=2,
                        devices=[
                            _gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi"),
                            _nic_device(link_type="gpudirect-tcpx"),
                        ],
                    ),
                    icv1alpha1.GpuPool(
                        name="frontier",
                        nodes=2,
                        devices=[
                            _gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi"),
                            _nic_device(link_type="infiniband"),
                        ],
                    ),
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="frontier",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # The sole request matches a synthetic NIC. The replica's serving
    # workload would have no ResourceClaim to bind GPUs through, so
    # the pool is not a viable host and nothing is scheduled.
    Case(
        name="synthetic-only selector leaves nothing to claim, pool ineligible",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="nic", count=1, selectors=[mdv1alpha1.Selector(cel=_IB)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="frontier",
                        nodes=2,
                        devices=[
                            _gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi"),
                            _nic_device(link_type="infiniband"),
                        ],
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[],
    ),
    Case(
        name="retained replica keeps its pinned pool",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="frontier",
                        nodes=2,
                        devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")],
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="frontier",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="frontier",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # A claimable GPU keeps both pools viable hosts; the synthetic
    # NIC's link type is the drifting discriminator.
    Case(
        name="selector drift re-places replica onto a now-matching pool",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[
                        mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)]),
                        mdv1alpha1.Device(name="nic", count=1, selectors=[mdv1alpha1.Selector(cel=_IB)]),
                    ],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="a",
                        nodes=2,
                        devices=[
                            _gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi"),
                            _nic_device(link_type="gpudirect-tcpx"),
                        ],
                    ),
                    icv1alpha1.GpuPool(
                        name="b",
                        nodes=2,
                        devices=[
                            _gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi"),
                            _nic_device(link_type="infiniband"),
                        ],
                    ),
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="a",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="b",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    Case(
        name="pinned pool that still matches stays pinned (attribute drift is sticky)",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[
                        mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)]),
                        mdv1alpha1.Device(name="nic", count=1, selectors=[mdv1alpha1.Selector(cel=_IB)]),
                    ],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="a",
                        nodes=2,
                        devices=[
                            _gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi"),
                            _nic_device(link_type="infiniband"),
                        ],
                    ),
                    icv1alpha1.GpuPool(
                        name="b",
                        nodes=2,
                        devices=[
                            _gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi"),
                            _nic_device(link_type="infiniband"),
                        ],
                    ),
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="a",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="a",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    Case(
        name="no matching pool anywhere drops the replica entirely",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[
                        mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)]),
                        mdv1alpha1.Device(name="nic", count=1, selectors=[mdv1alpha1.Selector(cel=_IB)]),
                    ],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="a",
                        nodes=2,
                        devices=[
                            _gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi"),
                            _nic_device(link_type="gpudirect-tcpx"),
                        ],
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="a",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=True,
        want=[],
    ),
    Case(
        name="replica pinned to an unpublished pool is re-placed onto a matching one",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[
                        mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)]),
                        mdv1alpha1.Device(name="nic", count=1, selectors=[mdv1alpha1.Selector(cel=_IB)]),
                    ],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="frontier",
                        nodes=2,
                        devices=[
                            _gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi"),
                            _nic_device(link_type="infiniband"),
                        ],
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="frontier",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # a/0 is pinned to a pool that still matches (retained). a/1 is
    # pinned to a pool no longer published, so it's dropped and will
    # be re-placed. The pool has just 2 nodes; both are notionally in
    # use by a/0 and a/1. The refill must see a/1's node freeing up
    # (it's being deleted) and re-place onto frontier at index 1.
    # Regression: the ledger must not charge dropped replicas.
    Case(
        name="dropping a non-matching replica frees its node for the refill",
        deployment=_deployment(
            replicas=2,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="frontier",
                        nodes=2,
                        devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")],
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="frontier",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            ),
            _replica(
                name="my-model-cluster-a-1",
                deployment="my-model",
                cluster="cluster-a",
                index=1,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="gone",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            ),
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="frontier",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
            _candidate(
                name="cluster-a",
                index=1,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="frontier",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # Request 8 GPUs. Pool 'a' has 4/node (doesn't fit); pool 'b'
    # has 8 and does. The replica must pin to 'b'.
    Case(
        name="device count is checked against the pinned pool, not a cluster-wide sum",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=8, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="a", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=4, memory="141Gi")]
                    ),
                    icv1alpha1.GpuPool(
                        name="b", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=8, memory="141Gi")]
                    ),
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="b",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=8,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # Per-member placement: single-pool engines, rejection when no pool fits,
    # and claimless ride-along members.
    #
    # The leader's request matches both pools; the worker's only
    # matches big. The whole-engine pass must put both members on
    # big - the one pool that satisfies them all.
    Case(
        name="a single pool satisfying every member hosts the whole engine",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Leader",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                ),
                _member(
                    role="Worker",
                    worker_nodes=1,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_200)])],
                ),
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="small", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    ),
                    icv1alpha1.GpuPool(
                        name="big", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="200Gi")]
                    ),
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Leader",
                        pool="big",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                    scheduling.MemberPlacement(
                        role="Worker",
                        pool="big",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_200],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # The leader only fits big (>= 200Gi); the worker only fits
    # small (< 200Gi). No single pool satisfies both. The scheduler
    # never splits an engine across pools - it can't tell whether
    # big and small share a fabric - so the engine is rejected and
    # the replica goes unplaced (#149).
    Case(
        name="members no single pool satisfies are not scheduled",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Leader",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_200)])],
                ),
                _member(
                    role="Worker",
                    worker_nodes=1,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_LT_200)])],
                ),
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="small", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    ),
                    icv1alpha1.GpuPool(
                        name="big", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="200Gi")]
                    ),
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[],
    ),
    # Both members match only big (>= 141Gi); small (40Gi) matches
    # neither. big has one free node but the gang needs two. The
    # engine doesn't fit any single pool, so it's rejected; with
    # cluster-a the only cluster the replica goes unplaced.
    Case(
        name="a gang too big for its only matching pool is rejected",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Leader",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                ),
                _member(
                    role="Worker",
                    worker_nodes=1,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                ),
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="small", nodes=8, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="40Gi")]
                    ),
                    icv1alpha1.GpuPool(
                        name="big", nodes=1, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    ),
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[],
    ),
    # Same gang. cluster-a's matching pool has only one free node
    # (too few for the two-member gang), so the scheduler rejects
    # cluster-a and places the whole gang on cluster-b, whose pool
    # has room for both members.
    Case(
        name="a gang too big for one cluster's pool lands whole on another",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Leader",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                ),
                _member(
                    role="Worker",
                    worker_nodes=1,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                ),
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="small", nodes=8, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="40Gi")]
                    ),
                    icv1alpha1.GpuPool(
                        name="big", nodes=1, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    ),
                ],
                taints=None,
                placement_labels=None,
            ),
            _cluster(
                name="cluster-b",
                gateway_hostname="cluster-b.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="big", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-b",
                index=0,
                gateway_hostname="cluster-b.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Leader",
                        pool="big",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                    scheduling.MemberPlacement(
                        role="Worker",
                        pool="big",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # On pool 'a' the leader's request matches only a Synthetic
    # device (nothing to claim) while the worker claims, so the
    # whole engine *could* land there - but pool 'b' satisfies the
    # leader claimably. The engine must go to 'b'; placing on 'a'
    # would run the leader without the GPU it asked for.
    Case(
        name="a member claimable elsewhere is not stranded on a synthetic match",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Leader",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_200)])],
                ),
                _member(
                    role="Worker",
                    worker_nodes=1,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                ),
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="a",
                        nodes=2,
                        devices=[
                            _gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi"),
                            _gpu_device(name="syn", claim="Synthetic", count=1, memory="200Gi"),
                        ],
                    ),
                    icv1alpha1.GpuPool(
                        name="b", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="200Gi")]
                    ),
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Leader",
                        pool="b",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_200],
                            )
                        ],
                    ),
                    scheduling.MemberPlacement(
                        role="Worker",
                        pool="b",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # The leader's request matches only the pool's synthetic NIC on
    # every pool - deliberate (a selector that pins without
    # claiming). It places claimless alongside the claiming worker.
    Case(
        name="a member synthetic-only everywhere places claimless with its gang",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Leader",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="nic", count=1, selectors=[mdv1alpha1.Selector(cel=_IB)])],
                ),
                _member(
                    role="Worker",
                    worker_nodes=1,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                ),
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="frontier",
                        nodes=2,
                        devices=[
                            _gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi"),
                            _nic_device(link_type="infiniband"),
                        ],
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Leader",
                        pool="frontier",
                        device_requests=[],
                    ),
                    scheduling.MemberPlacement(
                        role="Worker",
                        pool="frontier",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    Case(
        name="a member that matches nowhere fails the whole replica",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Leader",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                ),
                _member(
                    role="Worker",
                    worker_nodes=1,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_200)])],
                ),
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[],
    ),
    # The leader carries no nodeSelector: it claims nothing, follows
    # the worker's pool, and costs no nodes - the 1-node pool fits
    # the whole gang because only the worker occupies a node.
    Case(
        name="a claimless leader rides along on its gang's pool at zero cost",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(role="Leader", worker_nodes=None, devices=None),
                _member(
                    role="Worker",
                    worker_nodes=1,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                ),
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="frontier",
                        nodes=1,
                        devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")],
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Leader",
                        pool="frontier",
                        device_requests=[],
                    ),
                    scheduling.MemberPlacement(
                        role="Worker",
                        pool="frontier",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    Case(
        name="a retained replica's claimless member keeps its pin",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(role="Leader", worker_nodes=None, devices=None),
                _member(
                    role="Worker",
                    worker_nodes=1,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                ),
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="frontier",
                        nodes=1,
                        devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")],
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(role="Leader", worker_nodes=None, pool="frontier", device_requests=None),
                    _replica_member(
                        role="Worker",
                        worker_nodes=1,
                        pool="frontier",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    ),
                ],
            )
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Leader",
                        pool="frontier",
                        device_requests=[],
                    ),
                    scheduling.MemberPlacement(
                        role="Worker",
                        pool="frontier",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # other-model's gang occupies only its worker's node: its
    # claimless leader shares that node. The 2-node pool has 1 node
    # free, so our 1-node deployment fits. Charging the claimless
    # leader a node would wrongly report insufficient capacity.
    Case(
        name="another deployment's claimless member consumes no capacity",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="other-model-cluster-a-0",
                deployment="other-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(role="Leader", worker_nodes=None, pool="default", device_requests=None),
                    _replica_member(
                        role="Worker",
                        worker_nodes=1,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    ),
                ],
            )
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # The deployment grew a Worker (Standalone -> Leader+Worker).
    # The observed single-member replica no longer lines up, so it
    # is re-placed with the new shape.
    Case(
        name="a member shape change re-places the replica",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Leader",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                ),
                _member(
                    role="Worker",
                    worker_nodes=1,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                ),
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=4, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Leader",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                    scheduling.MemberPlacement(
                        role="Worker",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # Taints on InferenceClusters gate placement; a matching toleration on the
    # ModelDeployment overrides them. NoSchedule keeps new replicas off a
    # cluster but leaves existing ones; NoExecute additionally drains the
    # existing ones, which fill reschedules onto a tolerated cluster.
    Case(
        name="a NoSchedule taint keeps a new replica off the cluster",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=[icv1alpha1.Taint(key="modelplane.ai/maintenance", value="on", effect="NoSchedule")],
                placement_labels=None,
            ),
            _cluster(
                name="cluster-b",
                gateway_hostname="cluster-b.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-b",
                index=0,
                gateway_hostname="cluster-b.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    Case(
        name="a NoSchedule taint leaves an existing replica in place",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=[icv1alpha1.Taint(key="modelplane.ai/maintenance", value="on", effect="NoSchedule")],
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    Case(
        name="a toleration allows placement on a tainted cluster",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=[mdv1alpha1.Toleration(key="modelplane.ai/maintenance", operator="Exists")],
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=[icv1alpha1.Taint(key="modelplane.ai/maintenance", value="on", effect="NoSchedule")],
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    Case(
        name="a NoExecute taint drains a replica and reschedules it",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=[icv1alpha1.Taint(key="modelplane.ai/decommission", effect="NoExecute")],
                placement_labels=None,
            ),
            _cluster(
                name="cluster-b",
                gateway_hostname="cluster-b.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-b",
                index=0,
                gateway_hostname="cluster-b.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    Case(
        name="a NoExecute toleration retains a replica in place",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=[mdv1alpha1.Toleration(key="modelplane.ai/decommission", operator="Exists")],
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=[icv1alpha1.Taint(key="modelplane.ai/decommission", effect="NoExecute")],
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # Draining with no tolerated cluster to reschedule onto yields fewer than
    # spec.replicas; the deploy function surfaces the shortfall.
    Case(
        name="a NoExecute drain leaves the count unmet when there's nowhere to go",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=[icv1alpha1.Taint(key="modelplane.ai/decommission", effect="NoExecute")],
                placement_labels=None,
            )
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=True,
        want=[],
    ),
    # Matching the key but not the effect doesn't tolerate: an operator who
    # tolerates only NoSchedule is still drained by a NoExecute taint.
    Case(
        name="a NoSchedule toleration does not cover a NoExecute taint",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=[
                mdv1alpha1.Toleration(key="modelplane.ai/decommission", operator="Exists", effect="NoSchedule")
            ],
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=[icv1alpha1.Taint(key="modelplane.ai/decommission", effect="NoExecute")],
                placement_labels=None,
            ),
            _cluster(
                name="cluster-b",
                gateway_hostname="cluster-b.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
        ],
        all_replicas=[
            _replica(
                name="my-model-cluster-a-0",
                deployment="my-model",
                cluster="cluster-a",
                index=0,
                members=[
                    _replica_member(
                        role="Standalone",
                        worker_nodes=None,
                        pool="default",
                        device_requests=[
                            mrv1alpha1.DeviceRequest(
                                name="gpu",
                                deviceClassName="gpu.nvidia.com",
                                count=1,
                                selectors=[mrv1alpha1.Selector(cel=_MEM_141)],
                            )
                        ],
                    )
                ],
            )
        ],
        fill=True,
        want=[
            _candidate(
                name="cluster-b",
                index=0,
                gateway_hostname="cluster-b.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # Tolerating one of a cluster's taints isn't enough; any untolerated taint
    # keeps new replicas off.
    Case(
        name="an untolerated second taint still repels",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=[mdv1alpha1.Toleration(key="modelplane.ai/maintenance", operator="Exists")],
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=[
                    icv1alpha1.Taint(key="modelplane.ai/maintenance", value="on", effect="NoSchedule"),
                    icv1alpha1.Taint(key="modelplane.ai/reserved", effect="NoSchedule"),
                ],
                placement_labels=None,
            ),
            _cluster(
                name="cluster-b",
                gateway_hostname="cluster-b.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels=None,
            ),
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-b",
                index=0,
                gateway_hostname="cluster-b.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # Equal tolerates only when key and value both match.
    Case(
        name="an Equal toleration tolerates a taint with the same value",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=[mdv1alpha1.Toleration(key="modelplane.ai/maintenance", operator="Equal", value="on")],
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=[icv1alpha1.Taint(key="modelplane.ai/maintenance", value="on", effect="NoSchedule")],
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    Case(
        name="an Equal toleration does not tolerate a taint with another value",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=[mdv1alpha1.Toleration(key="modelplane.ai/maintenance", operator="Equal", value="off")],
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=[icv1alpha1.Taint(key="modelplane.ai/maintenance", value="on", effect="NoSchedule")],
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[],
    ),
    # An Exists toleration with no key tolerates any taint on the cluster.
    Case(
        name="a keyless Exists toleration tolerates every taint",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=[mdv1alpha1.Toleration(operator="Exists")],
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=[
                    icv1alpha1.Taint(key="modelplane.ai/maintenance", value="on", effect="NoSchedule"),
                    icv1alpha1.Taint(key="modelplane.ai/decommission", effect="NoExecute"),
                ],
                placement_labels=None,
            )
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
    # A cluster's placement labels reach the Candidate, and so the ModelReplica
    # and ModelEndpoint composed from it. This is how a self-hosted endpoint
    # gets its region: a ModelService selects endpoints by label, so without it
    # a region-scoped service can't select its own replicas, and it can't label
    # them by hand because Modelplane owns them.
    Case(
        name="a cluster's placement labels reach the candidate",
        deployment=_deployment(
            replicas=1,
            members=[
                _member(
                    role="Standalone",
                    worker_nodes=None,
                    devices=[mdv1alpha1.Device(name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel=_MEM_141)])],
                )
            ],
            tolerations=None,
        ),
        clusters=[
            _cluster(
                name="cluster-a",
                gateway_hostname="cluster-a.clusters.example.com",
                ready=True,
                pools=[
                    icv1alpha1.GpuPool(
                        name="default", nodes=2, devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")]
                    )
                ],
                taints=None,
                placement_labels={"example.org/region": "eu"},
            )
        ],
        all_replicas=[],
        fill=True,
        want=[
            _candidate(
                name="cluster-a",
                index=0,
                gateway_hostname="cluster-a.clusters.example.com",
                placement_labels={"example.org/region": "eu"},
                members=[
                    scheduling.MemberPlacement(
                        role="Standalone",
                        pool="default",
                        device_requests=[
                            scheduling.DeviceRequest(
                                name="gpu",
                                device_class_name="gpu.nvidia.com",
                                count=1,
                                cel_selectors=[_MEM_141],
                            )
                        ],
                    ),
                ],
            ),
        ],
    ),
]


@pytest.mark.parametrize("case", SCHEDULE_CASES, ids=lambda case: case.name)
def test_schedule(case: Case) -> None:
    """schedule() retains existing replicas and places new ones on clusters that can host them."""
    got = scheduling.schedule(case.deployment, case.clusters, case.all_replicas, fill=case.fill)
    assert got == case.want


def test_schedule_invalid_cel_raises() -> None:
    """A malformed expression raises CELCompileError, which the caller handles."""
    with pytest.raises(cel.CELCompileError, match=r"this is \) not valid \("):
        scheduling.schedule(
            _deployment(
                replicas=1,
                members=[
                    _member(
                        role="Standalone",
                        worker_nodes=None,
                        devices=[
                            mdv1alpha1.Device(
                                name="gpu", count=1, selectors=[mdv1alpha1.Selector(cel="this is ) not valid (")]
                            )
                        ],
                    )
                ],
                tolerations=None,
            ),
            [
                _cluster(
                    name="cluster-a",
                    gateway_hostname="cluster-a.clusters.example.com",
                    ready=True,
                    pools=[
                        icv1alpha1.GpuPool(
                            name="frontier",
                            nodes=2,
                            devices=[_gpu_device(name="gpu", claim="DRA", count=1, memory="141Gi")],
                        )
                    ],
                    taints=None,
                    placement_labels=None,
                )
            ],
            [],
        )
