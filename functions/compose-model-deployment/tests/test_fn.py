"""Tests for the compose-model-deployment function."""

import dataclasses
import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.modeldeployment import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


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

        xr = v1alpha1.ModelDeployment(
            metadata=metav1.ObjectMeta(name="my-model", namespace="ml-team"),
            spec=v1alpha1.SpecModel(
                replicas=1,
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

        cluster_a = {
            "apiVersion": "modelplane.ai/v1alpha1",
            "kind": "InferenceCluster",
            "metadata": {"name": "cluster-a"},
            "spec": {
                "cluster": {"source": "Existing", "existing": {"secretRef": {"name": "k"}}},
            },
            "status": {
                "conditions": [
                    {
                        "type": "Ready",
                        "status": "True",
                        "reason": "Available",
                        "lastTransitionTime": "2025-01-01T00:00:00Z",
                    }
                ],
                "gateway": {"address": "10.0.0.1"},
                "providerConfigRef": {"name": "cluster-a"},
                "capacity": {"gpuPools": [{"countPerNode": 1, "nodes": 2}]},
            },
        }

        # Requirements are the same for all cases that reach resolve_inputs.
        cluster_sel = fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster")
        cluster_sel.match_labels.labels.update({"modelplane.ai/cluster": "true"})
        replica_sel = fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica")
        replica_sel.match_labels.labels.update({"modelplane.ai/replica": "true"})

        # Case 1: one ready cluster matches — composes replica and endpoint.
        req1 = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(xr)),
            ),
        )
        req1.required_resources["clusters"].items.append(fnv1.Resource(resource=resource.dict_to_struct(cluster_a)))
        req1.required_resources["all-replicas"].SetInParent()

        want1 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct({"status": {"replicas": {"total": 1, "ready": 0}}}),
                ),
                resources={
                    "replica-cluster-a": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "modelplane.ai/v1alpha1",
                                "kind": "ModelReplica",
                                "metadata": {
                                    "name": "my-model-cluster-a-bc3c4",
                                    "namespace": "ml-team",
                                    "labels": {
                                        "modelplane.ai/replica": "true",
                                        "modelplane.ai/deployment": "my-model",
                                        "modelplane.ai/cluster": "cluster-a",
                                    },
                                },
                                "spec": {
                                    "clusterName": "cluster-a",
                                    "workers": {
                                        "topology": {"tensor": 1},
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
                    "endpoint-cluster-a": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "modelplane.ai/v1alpha1",
                                "kind": "ModelEndpoint",
                                "metadata": {
                                    "name": "my-model-cluster-a-bc3c4",
                                    "namespace": "ml-team",
                                    "labels": {
                                        "modelplane.ai/deployment": "my-model",
                                        "modelplane.ai/cluster": "cluster-a",
                                    },
                                },
                                "spec": {
                                    "url": "http://10.0.0.1/ml-team/my-model-cluster-a-bc3c4/v1",
                                    "rewritePath": "/ml-team/my-model-cluster-a-bc3c4/",
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
                fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Matched 1 clusters: cluster-a"),
            ],
            context=structpb.Struct(),
        )
        want1.requirements.resources["clusters"].CopyFrom(cluster_sel)
        want1.requirements.resources["all-replicas"].CopyFrom(replica_sel)

        # Case 2: no clusters — warning.
        req2 = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(xr)),
            ),
        )
        req2.required_resources["clusters"].SetInParent()
        req2.required_resources["all-replicas"].SetInParent()

        want2 = fnv1.RunFunctionResponse(
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
        want2.requirements.resources["clusters"].CopyFrom(cluster_sel)
        want2.requirements.resources["all-replicas"].CopyFrom(replica_sel)

        # Case 3: cluster has insufficient capacity.
        req3 = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(xr)),
            ),
        )
        req3.required_resources["clusters"].items.append(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {
                        "apiVersion": "modelplane.ai/v1alpha1",
                        "kind": "InferenceCluster",
                        "metadata": {"name": "cluster-a"},
                        "spec": {
                            "cluster": {"source": "Existing", "existing": {"secretRef": {"name": "k"}}},
                        },
                        "status": {
                            "conditions": [
                                {
                                    "type": "Ready",
                                    "status": "True",
                                    "reason": "Available",
                                    "lastTransitionTime": "2025-01-01T00:00:00Z",
                                }
                            ],
                            "gateway": {"address": "10.0.0.1"},
                            "providerConfigRef": {"name": "cluster-a"},
                            "capacity": {"gpuPools": [{"countPerNode": 0, "nodes": 0}]},
                        },
                    }
                )
            )
        )
        req3.required_resources["all-replicas"].SetInParent()

        want3 = fnv1.RunFunctionResponse(
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
                    message="0 of 1 clusters matched (checked 1)",
                ),
                fnv1.Condition(
                    type="ReplicasReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="NoReplicasScheduled",
                ),
            ],
            context=structpb.Struct(),
        )
        want3.requirements.resources["clusters"].CopyFrom(cluster_sel)
        want3.requirements.resources["all-replicas"].CopyFrom(replica_sel)

        # Case 4: existing replica is preserved (stable scheduling).
        req4 = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(xr)),
                resources={
                    "replica-cluster-a": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "modelplane.ai/v1alpha1",
                                "kind": "ModelReplica",
                                "metadata": {
                                    "name": "my-model-cluster-a-bc3c4",
                                    "namespace": "ml-team",
                                    "labels": {
                                        "modelplane.ai/replica": "true",
                                        "modelplane.ai/deployment": "my-model",
                                        "modelplane.ai/cluster": "cluster-a",
                                    },
                                },
                            }
                        ),
                    ),
                    "endpoint-cluster-a": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "modelplane.ai/v1alpha1",
                                "kind": "ModelEndpoint",
                                "metadata": {"name": "my-model-cluster-a-bc3c4", "namespace": "ml-team"},
                            }
                        ),
                    ),
                },
            ),
        )
        req4.required_resources["clusters"].items.append(fnv1.Resource(resource=resource.dict_to_struct(cluster_a)))
        req4.required_resources["all-replicas"].items.append(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {
                        "apiVersion": "modelplane.ai/v1alpha1",
                        "kind": "ModelReplica",
                        "metadata": {
                            "name": "my-model-cluster-a-bc3c4",
                            "namespace": "ml-team",
                            "labels": {
                                "modelplane.ai/replica": "true",
                                "modelplane.ai/deployment": "my-model",
                                "modelplane.ai/cluster": "cluster-a",
                            },
                        },
                        "spec": {
                            "clusterName": "cluster-a",
                            "workers": {
                                "topology": {"tensor": 1, "pipeline": 1},
                                "count": 1,
                                "template": {
                                    "spec": {
                                        "containers": [
                                            {"name": "engine", "image": "vllm/vllm-openai:latest"},
                                        ]
                                    }
                                },
                            },
                        },
                    }
                )
            )
        )

        want4 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct({"status": {"replicas": {"total": 1, "ready": 0}}}),
                ),
                resources={
                    "replica-cluster-a": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "modelplane.ai/v1alpha1",
                                "kind": "ModelReplica",
                                "metadata": {
                                    "name": "my-model-cluster-a-bc3c4",
                                    "namespace": "ml-team",
                                    "labels": {
                                        "modelplane.ai/replica": "true",
                                        "modelplane.ai/deployment": "my-model",
                                        "modelplane.ai/cluster": "cluster-a",
                                    },
                                },
                                "spec": {
                                    "clusterName": "cluster-a",
                                    "workers": {
                                        "topology": {"tensor": 1},
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
                    "endpoint-cluster-a": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "modelplane.ai/v1alpha1",
                                "kind": "ModelEndpoint",
                                "metadata": {
                                    "name": "my-model-cluster-a-bc3c4",
                                    "namespace": "ml-team",
                                    "labels": {
                                        "modelplane.ai/deployment": "my-model",
                                        "modelplane.ai/cluster": "cluster-a",
                                    },
                                },
                                "spec": {
                                    "url": "http://10.0.0.1/ml-team/my-model-cluster-a-bc3c4/v1",
                                    "rewritePath": "/ml-team/my-model-cluster-a-bc3c4/",
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
                    message="Matched 1 clusters",
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
        want4.requirements.resources["clusters"].CopyFrom(cluster_sel)
        want4.requirements.resources["all-replicas"].CopyFrom(replica_sel)

        # Case 5: pinned cluster has gone offline (still exists but is
        # no longer Ready and has no gateway address). The replica stays
        # pinned to it; the endpoint is not composed.
        cluster_a_offline = {
            "apiVersion": "modelplane.ai/v1alpha1",
            "kind": "InferenceCluster",
            "metadata": {"name": "cluster-a"},
            "spec": {
                "cluster": {"source": "Existing", "existing": {"secretRef": {"name": "k"}}},
            },
            "status": {
                "conditions": [
                    {
                        "type": "Ready",
                        "status": "False",
                        "reason": "Unavailable",
                        "lastTransitionTime": "2025-01-01T00:00:00Z",
                    }
                ],
                "providerConfigRef": {"name": "cluster-a"},
                "capacity": {"gpuPools": [{"countPerNode": 1, "nodes": 2}]},
            },
        }
        existing_replica = {
            "apiVersion": "modelplane.ai/v1alpha1",
            "kind": "ModelReplica",
            "metadata": {
                "name": "my-model-cluster-a-bc3c4",
                "namespace": "ml-team",
                "labels": {
                    "modelplane.ai/replica": "true",
                    "modelplane.ai/deployment": "my-model",
                    "modelplane.ai/cluster": "cluster-a",
                },
            },
            "spec": {
                "clusterName": "cluster-a",
                "workers": {
                    "topology": {"tensor": 1, "pipeline": 1},
                    "count": 1,
                    "template": {
                        "spec": {
                            "containers": [
                                {"name": "engine", "image": "vllm/vllm-openai:latest"},
                            ]
                        }
                    },
                },
            },
        }
        req5 = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(xr)),
                resources={
                    "replica-cluster-a": fnv1.Resource(resource=resource.dict_to_struct(existing_replica)),
                },
            ),
        )
        req5.required_resources["clusters"].items.append(
            fnv1.Resource(resource=resource.dict_to_struct(cluster_a_offline))
        )
        req5.required_resources["all-replicas"].items.append(
            fnv1.Resource(resource=resource.dict_to_struct(existing_replica))
        )

        want5 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct({"status": {"replicas": {"total": 1, "ready": 0}}}),
                ),
                resources={
                    "replica-cluster-a": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "modelplane.ai/v1alpha1",
                                "kind": "ModelReplica",
                                "metadata": {
                                    "name": "my-model-cluster-a-bc3c4",
                                    "namespace": "ml-team",
                                    "labels": {
                                        "modelplane.ai/replica": "true",
                                        "modelplane.ai/deployment": "my-model",
                                        "modelplane.ai/cluster": "cluster-a",
                                    },
                                },
                                "spec": {
                                    "clusterName": "cluster-a",
                                    "workers": {
                                        "topology": {"tensor": 1},
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
                    message="Matched 1 clusters",
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
        want5.requirements.resources["clusters"].CopyFrom(cluster_sel)
        want5.requirements.resources["all-replicas"].CopyFrom(replica_sel)

        # Case 6: pinned cluster has disappeared entirely. cluster-b is
        # available with capacity, so the scheduler re-places the
        # replica onto it.
        cluster_b = {
            "apiVersion": "modelplane.ai/v1alpha1",
            "kind": "InferenceCluster",
            "metadata": {"name": "cluster-b"},
            "spec": {
                "cluster": {"source": "Existing", "existing": {"secretRef": {"name": "k"}}},
            },
            "status": {
                "conditions": [
                    {
                        "type": "Ready",
                        "status": "True",
                        "reason": "Available",
                        "lastTransitionTime": "2025-01-01T00:00:00Z",
                    }
                ],
                "gateway": {"address": "10.0.0.2"},
                "providerConfigRef": {"name": "cluster-b"},
                "capacity": {"gpuPools": [{"countPerNode": 1, "nodes": 2}]},
            },
        }
        req6 = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(xr)),
                resources={
                    "replica-cluster-a": fnv1.Resource(resource=resource.dict_to_struct(existing_replica)),
                },
            ),
        )
        req6.required_resources["clusters"].items.append(fnv1.Resource(resource=resource.dict_to_struct(cluster_b)))
        # The existing replica is observed - its pinned cluster-a is
        # gone from the cluster list, so the scheduler must re-place it.
        req6.required_resources["all-replicas"].items.append(
            fnv1.Resource(resource=resource.dict_to_struct(existing_replica))
        )

        want6 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct({"status": {"replicas": {"total": 1, "ready": 0}}}),
                ),
                resources={
                    "replica-cluster-b": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "modelplane.ai/v1alpha1",
                                "kind": "ModelReplica",
                                "metadata": {
                                    "name": "my-model-cluster-b-a9d2c",
                                    "namespace": "ml-team",
                                    "labels": {
                                        "modelplane.ai/replica": "true",
                                        "modelplane.ai/deployment": "my-model",
                                        "modelplane.ai/cluster": "cluster-b",
                                    },
                                },
                                "spec": {
                                    "clusterName": "cluster-b",
                                    "workers": {
                                        "topology": {"tensor": 1},
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
                    "endpoint-cluster-b": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "modelplane.ai/v1alpha1",
                                "kind": "ModelEndpoint",
                                "metadata": {
                                    "name": "my-model-cluster-b-a9d2c",
                                    "namespace": "ml-team",
                                    "labels": {
                                        "modelplane.ai/deployment": "my-model",
                                        "modelplane.ai/cluster": "cluster-b",
                                    },
                                },
                                "spec": {
                                    "url": "http://10.0.0.2/ml-team/my-model-cluster-b-a9d2c/v1",
                                    "rewritePath": "/ml-team/my-model-cluster-b-a9d2c/",
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
                fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Matched 1 clusters: cluster-b"),
            ],
            context=structpb.Struct(),
        )
        want6.requirements.resources["clusters"].CopyFrom(cluster_sel)
        want6.requirements.resources["all-replicas"].CopyFrom(replica_sel)

        cases = [
            Case(name="one ready cluster composes replica and endpoint", req=req1, want=want1),
            Case(name="no clusters produces warning", req=req2, want=want2),
            Case(name="insufficient capacity produces no replicas", req=req3, want=want3),
            Case(name="existing replica is preserved with stable scheduling", req=req4, want=want4),
            Case(name="offline pinned cluster keeps replica but drops endpoint", req=req5, want=want5),
            Case(name="deleted pinned cluster triggers replica re-placement", req=req6, want=want6),
        ]

        for case in cases:
            with self.subTest(case.name):
                got = await self.runner.RunFunction(case.req, None)
                self.assertEqual(
                    json_format.MessageToDict(case.want),
                    json_format.MessageToDict(got),
                    "-want, +got",
                )
