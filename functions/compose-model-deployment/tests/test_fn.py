"""Tests for the compose-model-deployment function."""

import dataclasses
import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modeldeployment import v1alpha1
from models.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# The resolved DRA device requests the scheduler stamps onto each ModelReplica,
# derived from the deployment's nodeSelector matched against the cluster's GPU
# device (deviceClassName gpu.nvidia.com).
_DEVICE_REQUESTS = [
    {
        "name": "gpu",
        "deviceClassName": "gpu.nvidia.com",
        "count": 1,
        "selectors": [{"cel": 'device.driver == "gpu.nvidia.com"'}],
    }
]

# A one-replica deployment requesting a single GPU. Reused across most cases.
_XR = v1alpha1.ModelDeployment(
    metadata=metav1.ObjectMeta(name="my-model", namespace="ml-team"),
    spec=v1alpha1.SpecModel(
        replicas=1,
        nodeSelector=v1alpha1.NodeSelector(
            devices=[
                v1alpha1.Device(
                    name="gpu",
                    count=1,
                    selectors=[v1alpha1.Selector(cel='device.driver == "gpu.nvidia.com"')],
                ),
            ],
        ),
        workers=v1alpha1.Workers(
            topology=v1alpha1.Topology(tensor=1),
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
    ),
).model_dump(exclude_none=True, mode="json")


def _cluster(name: str, *, ready: bool = True, address: str | None = "10.0.0.1", nodes: int = 2) -> dict:
    """An InferenceCluster input fixture, dumped to a dict.

    A ready cluster has a Ready=True condition and a gateway address. ready=False
    flips the condition to Unavailable; address=None drops the gateway entirely
    (mirroring an offline cluster). nodes=0 yields a pool with no capacity.
    """
    return icv1alpha1.InferenceCluster(
        metadata=metav1.ObjectMeta(name=name),
        spec=icv1alpha1.Spec(
            cluster=icv1alpha1.Cluster(
                source="Existing",
                existing=icv1alpha1.Existing(secretRef=icv1alpha1.SecretRef(name="k")),
            ),
        ),
        status=icv1alpha1.Status(
            conditions=[
                icv1alpha1.Condition(
                    type="Ready",
                    status="True" if ready else "False",
                    reason="Available" if ready else "Unavailable",
                    lastTransitionTime="2025-01-01T00:00:00Z",
                )
            ],
            gateway=icv1alpha1.Gateway(address=address) if address else None,
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
        nodePoolName="default",
        deviceRequests=[
            mrv1alpha1.DeviceRequest(
                name="gpu",
                deviceClassName="gpu.nvidia.com",
                count=1,
                selectors=[mrv1alpha1.Selector(cel='device.driver == "gpu.nvidia.com"')],
            ),
        ],
        workers=mrv1alpha1.Workers(
            count=1,
            topology=mrv1alpha1.Topology(tensor=1, pipeline=1),
            template=mrv1alpha1.Template(
                spec=mrv1alpha1.Spec(
                    containers=[mrv1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")],
                ),
            ),
        ),
    ),
).model_dump(exclude_none=True, mode="json")

# The requirements selectors every want echoes back. Both are bare selectors
# matching all resources of the kind.
_CLUSTER_SEL = fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster")
_REPLICA_SEL = fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica")


def _req(xr: dict, *, clusters: list[dict], replicas: list[dict] | None = None, observed: dict | None = None):
    """Build a RunFunctionRequest with the standard required_resources.

    clusters and replicas populate the "clusters" and "all-replicas" required
    resources respectively; both keys are always present (empty when there are
    no items). observed populates observed.resources.
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
    return req


def _want(resp: fnv1.RunFunctionResponse) -> fnv1.RunFunctionResponse:
    """Attach the standard requirements selectors to a response."""
    resp.requirements.resources["clusters"].CopyFrom(_CLUSTER_SEL)
    resp.requirements.resources["all-replicas"].CopyFrom(_REPLICA_SEL)
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
        """The function fans out ModelReplicas and ModelEndpoints."""

        # A deployment that sets spec.modelCacheRef.
        xr_cached = v1alpha1.ModelDeployment(
            metadata=metav1.ObjectMeta(name="my-model", namespace="ml-team"),
            spec=v1alpha1.SpecModel(
                replicas=1,
                modelCacheRef=v1alpha1.ModelCacheRef(name="qwen"),
                nodeSelector=v1alpha1.NodeSelector(
                    devices=[
                        v1alpha1.Device(
                            name="gpu",
                            count=1,
                            selectors=[v1alpha1.Selector(cel='device.driver == "gpu.nvidia.com"')],
                        ),
                    ],
                ),
                workers=v1alpha1.Workers(
                    topology=v1alpha1.Topology(tensor=1),
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
            ),
        ).model_dump(exclude_none=True, mode="json")

        # A two-replica deployment (no container args) for the co-location case.
        xr_two = v1alpha1.ModelDeployment(
            metadata=metav1.ObjectMeta(name="my-model", namespace="ml-team"),
            spec=v1alpha1.SpecModel(
                replicas=2,
                nodeSelector=v1alpha1.NodeSelector(
                    devices=[
                        v1alpha1.Device(
                            name="gpu",
                            count=1,
                            selectors=[v1alpha1.Selector(cel='device.driver == "gpu.nvidia.com"')],
                        ),
                    ],
                ),
                workers=v1alpha1.Workers(
                    topology=v1alpha1.Topology(tensor=1),
                    template=v1alpha1.Template(
                        spec=v1alpha1.Spec(
                            containers=[v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")],
                        ),
                    ),
                ),
            ),
        ).model_dump(exclude_none=True, mode="json")

        cases = [
            Case(
                name="one ready cluster composes replica and endpoint",
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
                                                "nodePoolName": "default",
                                                "deviceRequests": _DEVICE_REQUESTS,
                                                "workers": {
                                                    "topology": {"tensor": 1, "pipeline": 1},
                                                    "count": 1,
                                                    "template": {
                                                        "spec": {
                                                            "containers": [
                                                                {
                                                                    "name": "engine",
                                                                    "image": "vllm/vllm-openai:latest",
                                                                    "args": ["--model=Qwen/Qwen3-0.6B"],
                                                                }
                                                            ],
                                                        },
                                                    },
                                                },
                                            },
                                        }
                                    ),
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
                                                "url": "http://10.0.0.1/ml-team/my-model-5ab63/v1",
                                                "rewritePath": "/ml-team/my-model-5ab63/",
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
                name="existing replica is preserved with stable scheduling",
                req=_req(
                    _XR,
                    clusters=[_CLUSTER_A],
                    replicas=[_EXISTING_REPLICA],
                    observed={
                        "replica-cluster-a-0": {
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
                        },
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
                                                "nodePoolName": "default",
                                                "deviceRequests": _DEVICE_REQUESTS,
                                                "workers": {
                                                    "topology": {"tensor": 1, "pipeline": 1},
                                                    "count": 1,
                                                    "template": {
                                                        "spec": {
                                                            "containers": [
                                                                {
                                                                    "name": "engine",
                                                                    "image": "vllm/vllm-openai:latest",
                                                                    "args": ["--model=Qwen/Qwen3-0.6B"],
                                                                }
                                                            ],
                                                        },
                                                    },
                                                },
                                            },
                                        }
                                    ),
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
                                                "url": "http://10.0.0.1/ml-team/my-model-5ab63/v1",
                                                "rewritePath": "/ml-team/my-model-5ab63/",
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
                name="offline pinned cluster keeps replica but drops endpoint",
                req=_req(
                    _XR,
                    clusters=[_cluster("cluster-a", ready=False, address=None)],
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
                                                "nodePoolName": "default",
                                                "deviceRequests": _DEVICE_REQUESTS,
                                                "workers": {
                                                    "topology": {"tensor": 1, "pipeline": 1},
                                                    "count": 1,
                                                    "template": {
                                                        "spec": {
                                                            "containers": [
                                                                {
                                                                    "name": "engine",
                                                                    "image": "vllm/vllm-openai:latest",
                                                                    "args": ["--model=Qwen/Qwen3-0.6B"],
                                                                }
                                                            ],
                                                        },
                                                    },
                                                },
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
                    clusters=[_cluster("cluster-b", address="10.0.0.2")],
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
                                                "nodePoolName": "default",
                                                "deviceRequests": _DEVICE_REQUESTS,
                                                "workers": {
                                                    "topology": {"tensor": 1, "pipeline": 1},
                                                    "count": 1,
                                                    "template": {
                                                        "spec": {
                                                            "containers": [
                                                                {
                                                                    "name": "engine",
                                                                    "image": "vllm/vllm-openai:latest",
                                                                    "args": ["--model=Qwen/Qwen3-0.6B"],
                                                                }
                                                            ],
                                                        },
                                                    },
                                                },
                                            },
                                        }
                                    ),
                                ),
                                "endpoint-cluster-b-0": fnv1.Resource(
                                    resource=resource.dict_to_struct(
                                        {
                                            "apiVersion": "modelplane.ai/v1alpha1",
                                            "kind": "ModelEndpoint",
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
                                                "url": "http://10.0.0.2/ml-team/my-model-f0b76/v1",
                                                "rewritePath": "/ml-team/my-model-f0b76/",
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
                req=_req(xr_cached, clusters=[_CLUSTER_A]),
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
                                                "nodePoolName": "default",
                                                "deviceRequests": _DEVICE_REQUESTS,
                                                "modelCacheRef": {"name": "qwen"},
                                                "workers": {
                                                    "topology": {"tensor": 1, "pipeline": 1},
                                                    "count": 1,
                                                    "template": {
                                                        "spec": {
                                                            "containers": [
                                                                {
                                                                    "name": "engine",
                                                                    "image": "vllm/vllm-openai:latest",
                                                                    "args": ["--model=Qwen/Qwen3-0.6B"],
                                                                }
                                                            ],
                                                        },
                                                    },
                                                },
                                            },
                                        }
                                    ),
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
                                                "url": "http://10.0.0.1/ml-team/my-model-5ab63/v1",
                                                "rewritePath": "/ml-team/my-model-5ab63/",
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
                                                "nodePoolName": "default",
                                                "deviceRequests": _DEVICE_REQUESTS,
                                                "workers": {
                                                    "topology": {"tensor": 1, "pipeline": 1},
                                                    "count": 1,
                                                    "template": {
                                                        "spec": {
                                                            "containers": [
                                                                {
                                                                    "name": "engine",
                                                                    "image": "vllm/vllm-openai:latest",
                                                                }
                                                            ],
                                                        },
                                                    },
                                                },
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
                                                "nodePoolName": "default",
                                                "deviceRequests": _DEVICE_REQUESTS,
                                                "workers": {
                                                    "topology": {"tensor": 1, "pipeline": 1},
                                                    "count": 1,
                                                    "template": {
                                                        "spec": {
                                                            "containers": [
                                                                {
                                                                    "name": "engine",
                                                                    "image": "vllm/vllm-openai:latest",
                                                                }
                                                            ],
                                                        },
                                                    },
                                                },
                                            },
                                        }
                                    ),
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
                                                "url": "http://10.0.0.1/ml-team/my-model-5ab63/v1",
                                                "rewritePath": "/ml-team/my-model-5ab63/",
                                            },
                                        }
                                    ),
                                ),
                                "endpoint-cluster-a-1": fnv1.Resource(
                                    resource=resource.dict_to_struct(
                                        {
                                            "apiVersion": "modelplane.ai/v1alpha1",
                                            "kind": "ModelEndpoint",
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
                                                "url": "http://10.0.0.1/ml-team/my-model-609c5/v1",
                                                "rewritePath": "/ml-team/my-model-609c5/",
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
        ]

        for case in cases:
            with self.subTest(case.name):
                got = await self.runner.RunFunction(case.req, None)
                self.assertEqual(
                    json_format.MessageToDict(case.want),
                    json_format.MessageToDict(got),
                    "-want, +got",
                )
