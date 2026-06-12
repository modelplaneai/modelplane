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

# A GPU device request CEL selector, as compose-model-deployment stamps it.
_GPU_CEL = 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'


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
                engines=[
                    v1alpha1.Engine(
                        name="main",
                        copies=1,
                        members=[
                            v1alpha1.Member(
                                role="Standalone",
                                nodePoolName="frontier",
                                deviceRequests=[
                                    v1alpha1.DeviceRequest(
                                        name="gpu",
                                        deviceClassName="gpu.nvidia.com",
                                        count=1,
                                        selectors=[v1alpha1.Selector(cel=_GPU_CEL)],
                                    ),
                                ],
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
                        ],
                    ),
                ],
            ),
        ).model_dump(exclude_none=True, mode="json")

        cluster_requirement = fnv1.ResourceSelector(
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceCluster",
            match_name="cluster-a",
        )

        # Case 1: cluster resolved with providerConfigRef — composes native
        # Deployment. First reconcile: none of the composed resources are in
        # observed yet, so none are marked ready (the function only asserts
        # readiness for a resource it can see in observed state).
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
                    "model-serving-main": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ClusterProviderConfig",
                                        "name": "cluster-a-pc",
                                    },
                                    "readiness": {
                                        "policy": "DeriveFromCelQuery",
                                        "celQuery": (
                                            "has(object.status.conditions) && "
                                            "object.status.conditions.exists("
                                            'c, c.type == "Available" && c.status == "True")'
                                        ),
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "apps/v1",
                                            "kind": "Deployment",
                                            "metadata": {
                                                "name": resource.child_name("test-replica", "main"),
                                                "namespace": "default",
                                            },
                                            "spec": {
                                                "replicas": 1,
                                                "selector": {
                                                    "matchLabels": {
                                                        "modelplane.ai/workload": resource.child_name(
                                                            "test-replica", "main"
                                                        ),
                                                    },
                                                },
                                                "template": {
                                                    "metadata": {
                                                        "labels": {
                                                            "modelplane.ai/serving": "test-replica",
                                                            "modelplane.ai/workload": resource.child_name(
                                                                "test-replica", "main"
                                                            ),
                                                        },
                                                    },
                                                    "spec": {
                                                        "containers": [
                                                            {
                                                                "name": "engine",
                                                                "image": "vllm/vllm-openai:latest",
                                                                "args": ["--model=Qwen/Qwen3-0.6B"],
                                                                "ports": [{"containerPort": 8000}],
                                                                "resources": {"claims": [{"name": "devices"}]},
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
                                                        "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                                        "resourceClaims": [
                                                            {
                                                                "name": "devices",
                                                                "resourceClaimTemplateName": resource.child_name(
                                                                    "test-replica", "main", "standalone", "devices"
                                                                ),
                                                            },
                                                        ],
                                                        "tolerations": [
                                                            {
                                                                "key": "nvidia.com/gpu",
                                                                "operator": "Exists",
                                                                "effect": "NoSchedule",
                                                            },
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
                                    "readiness": {"policy": "SuccessfulCreate"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "Service",
                                            "metadata": {
                                                "name": "test-replica",
                                                "namespace": "default",
                                            },
                                            "spec": {
                                                "selector": {"modelplane.ai/serving": "test-replica"},
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
                                    "readiness": {"policy": "SuccessfulCreate"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "gateway.networking.k8s.io/v1",
                                            "kind": "HTTPRoute",
                                            "metadata": {
                                                "name": "test-replica",
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
                                                                    "value": "/ml-team/test-replica/",
                                                                },
                                                            },
                                                        ],
                                                        "filters": [
                                                            {
                                                                "type": "URLRewrite",
                                                                "urlRewrite": {
                                                                    "path": {
                                                                        "type": "ReplacePrefixMatch",
                                                                        "replacePrefixMatch": "/",
                                                                    },
                                                                },
                                                            },
                                                        ],
                                                        "backendRefs": [
                                                            {"name": "test-replica", "port": 80},
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
                    "resource-claim-main-standalone": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ClusterProviderConfig",
                                        "name": "cluster-a-pc",
                                    },
                                    "readiness": {"policy": "SuccessfulCreate"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "resource.k8s.io/v1",
                                            "kind": "ResourceClaimTemplate",
                                            "metadata": {
                                                "name": resource.child_name(
                                                    "test-replica", "main", "standalone", "devices"
                                                ),
                                                "namespace": "default",
                                            },
                                            "spec": {
                                                "spec": {
                                                    "devices": {
                                                        "requests": [
                                                            {
                                                                "name": "gpu",
                                                                "exactly": {
                                                                    "deviceClassName": "gpu.nvidia.com",
                                                                    "count": 1,
                                                                    "selectors": [
                                                                        {"cel": {"expression": _GPU_CEL}},
                                                                    ],
                                                                },
                                                            },
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

        # Case 4: the resources from case 1 now exist in observed, and the
        # workload Object reports Available (so its derived Ready is True). The
        # function marks every composed resource ready (it can now observe them),
        # the workload because it's serving and the rest because existing is
        # being ready for them. Built from case 1, mutating only what the
        # observed-ready transition changes: the four ready flags, the
        # acceptance/readiness conditions, and the dropped first-reconcile event.
        req4 = fnv1.RunFunctionRequest()
        req4.CopyFrom(req1)
        # The workload Object as provider-kubernetes observes it back: applied
        # (atProvider.manifest populated) and Available (its derived Ready=True).
        req4.observed.resources["model-serving-main"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "spec": {"forProvider": {"manifest": {"kind": "Deployment"}}},
                        "status": {
                            "atProvider": {"manifest": {"kind": "Deployment"}},
                            "conditions": [
                                {
                                    "type": "Ready",
                                    "status": "True",
                                    "reason": "Available",
                                    "lastTransitionTime": "2025-01-01T00:00:00Z",
                                },
                            ],
                        },
                    }
                ),
            )
        )
        # The other three are observed simply by being present; their content
        # doesn't matter, only that the function can see them.
        for key in ("model-service", "model-route", "resource-claim-main-standalone"):
            req4.observed.resources[key].CopyFrom(fnv1.Resource(resource=structpb.Struct()))

        want4 = fnv1.RunFunctionResponse()
        want4.CopyFrom(want1)
        for key in ("model-serving-main", "model-service", "model-route", "resource-claim-main-standalone"):
            want4.desired.resources[key].ready = fnv1.READY_TRUE
        del want4.conditions[:]
        want4.conditions.extend(
            [
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_TRUE, reason="Accepted"),
                fnv1.Condition(type="ModelReady", status=fnv1.STATUS_CONDITION_TRUE, reason="Serving"),
            ]
        )
        # The "Composing ..." event fires only the first reconcile (model-serving
        # not yet observed), so it's gone now.
        del want4.results[:]

        cases = [
            Case(name="cluster ready composes native Deployment", req=req1, want=want1),
            Case(name="cluster not resolved returns waiting conditions", req=req2, want=want2),
            Case(name="cluster without providerConfigRef returns waiting conditions", req=req3, want=want3),
            Case(name="observed resources are marked ready", req=req4, want=want4),
        ]

        for case in cases:
            with self.subTest(case.name):
                got = await self.runner.RunFunction(case.req, None)
                self.assertEqual(
                    json_format.MessageToDict(case.want),
                    json_format.MessageToDict(got),
                    "-want, +got",
                )
