"""Tests for the compose-model-replica function."""

import dataclasses
import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-model-replica."""

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
        """The function dispatches to a backend to compose serving resources on a remote cluster."""

        xr = v1alpha1.ModelReplica(
            metadata=metav1.ObjectMeta(
                name="test-replica",
                namespace="ml-team",
                labels={
                    "modelplane.ai/deployment": "my-deployment",
                    "modelplane.ai/cluster": "cluster-a",
                },
            ),
            spec=v1alpha1.SpecModel(
                clusterName="cluster-a",
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

        cluster_requirement = fnv1.ResourceSelector(
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceCluster",
            match_name="cluster-a",
        )

        # Case 1: cluster resolved with providerConfigRef — composes native Deployment.
        req1 = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(xr)),
            ),
        )
        req1.required_resources["cluster"].items.append(
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
                            "providerConfigRef": {"name": "cluster-a-pc"},
                            "gateway": {"address": "10.0.0.1"},
                        },
                    }
                )
            )
        )

        want1 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "model-serving": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ClusterProviderConfig",
                                        "name": "cluster-a-pc",
                                    },
                                    "readiness": {"policy": "DeriveFromObject"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "apps/v1",
                                            "kind": "Deployment",
                                            "metadata": {
                                                "name": "my-deployment-1154c",
                                                "namespace": "default",
                                            },
                                            "spec": {
                                                "replicas": 1,
                                                "selector": {
                                                    "matchLabels": {
                                                        "modelplane.ai/serving": "my-deployment-1154c",
                                                    },
                                                },
                                                "template": {
                                                    "metadata": {
                                                        "labels": {
                                                            "modelplane.ai/serving": "my-deployment-1154c",
                                                        },
                                                    },
                                                    "spec": {
                                                        "containers": [
                                                            {
                                                                "name": "engine",
                                                                "image": "vllm/vllm-openai:latest",
                                                                "args": ["--model=Qwen/Qwen3-0.6B"],
                                                                "ports": [{"containerPort": 8000}],
                                                                "resources": {
                                                                    "limits": {"nvidia.com/gpu": "1"},
                                                                },
                                                                "volumeMounts": [
                                                                    {"name": "dshm", "mountPath": "/dev/shm"},
                                                                ],
                                                                "readinessProbe": {
                                                                    "httpGet": {"path": "/health", "port": 8000},
                                                                    "initialDelaySeconds": 30,
                                                                    "periodSeconds": 10,
                                                                },
                                                            },
                                                        ],
                                                        "volumes": [
                                                            {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                                                        ],
                                                    },
                                                },
                                            },
                                        },
                                    },
                                },
                            }
                        ),
                    ),
                    "model-service": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ClusterProviderConfig",
                                        "name": "cluster-a-pc",
                                    },
                                    "readiness": {"policy": "DeriveFromObject"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "Service",
                                            "metadata": {
                                                "name": "my-deployment-1154c",
                                                "namespace": "default",
                                            },
                                            "spec": {
                                                "selector": {"modelplane.ai/serving": "my-deployment-1154c"},
                                                "ports": [{"port": 80, "targetPort": 8000}],
                                            },
                                        },
                                    },
                                },
                            }
                        ),
                    ),
                    "model-route": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ClusterProviderConfig",
                                        "name": "cluster-a-pc",
                                    },
                                    "readiness": {"policy": "DeriveFromObject"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "gateway.networking.k8s.io/v1",
                                            "kind": "HTTPRoute",
                                            "metadata": {
                                                "name": "my-deployment-1154c",
                                                "namespace": "default",
                                            },
                                            "spec": {
                                                "parentRefs": [
                                                    {
                                                        "name": "inference-gateway",
                                                        "namespace": "modelplane-system",
                                                    },
                                                ],
                                                "rules": [
                                                    {
                                                        "matches": [
                                                            {
                                                                "path": {
                                                                    "type": "PathPrefix",
                                                                    "value": "/ml-team/my-deployment/",
                                                                },
                                                            },
                                                        ],
                                                        "backendRefs": [
                                                            {"name": "my-deployment-1154c", "port": 80},
                                                        ],
                                                    },
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
                    type="ModelAccepted",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Deploying",
                ),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForModel",
                ),
            ],
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Composing vllm/vllm-openai:latest on cluster-a",
                ),
            ],
            context=structpb.Struct(),
        )
        want1.requirements.resources["cluster"].CopyFrom(cluster_requirement)

        # Case 2: cluster not resolved — early return with conditions.
        req2 = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(xr)),
            ),
        )

        want2 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            conditions=[
                fnv1.Condition(
                    type="ModelAccepted",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForCluster",
                ),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForModel",
                ),
            ],
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Waiting for cluster to be resolved",
                ),
            ],
            context=structpb.Struct(),
        )
        want2.requirements.resources["cluster"].CopyFrom(cluster_requirement)

        # Case 3: cluster resolved but no providerConfigRef — early return.
        req3 = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(xr)),
            ),
        )
        req3.required_resources["cluster"].items.append(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {
                        "apiVersion": "modelplane.ai/v1alpha1",
                        "kind": "InferenceCluster",
                        "metadata": {"name": "cluster-a"},
                        "spec": {
                            "cluster": {"source": "Existing", "existing": {"secretRef": {"name": "k"}}},
                        },
                    }
                )
            )
        )

        want3 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            conditions=[
                fnv1.Condition(
                    type="ModelAccepted",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForCluster",
                ),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForModel",
                ),
            ],
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Waiting for cluster providerConfigRef",
                ),
            ],
            context=structpb.Struct(),
        )
        want3.requirements.resources["cluster"].CopyFrom(cluster_requirement)

        cases = [
            Case(name="cluster ready composes native Deployment", req=req1, want=want1),
            Case(name="cluster not resolved returns waiting conditions", req=req2, want=want2),
            Case(name="cluster without providerConfigRef returns waiting conditions", req=req3, want=want3),
        ]

        for case in cases:
            with self.subTest(case.name):
                got = await self.runner.RunFunction(case.req, None)
                self.assertEqual(
                    json_format.MessageToDict(case.want),
                    json_format.MessageToDict(got),
                    "-want, +got",
                )
