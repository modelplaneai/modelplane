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

"""Tests for the compose-model-endpoint function."""

import dataclasses
import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.modelendpoint import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-model-endpoint."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


def _service_only(name: str, namespace: str = "ml-team") -> fnv1.Resource:
    """Build an observed Service resource with just metadata.name set."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": name, "namespace": namespace},
            }
        ),
    )


def _endpointslice_only(name: str, namespace: str = "ml-team") -> fnv1.Resource:
    """Build an observed EndpointSlice resource with just metadata.name set."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "discovery.k8s.io/v1",
                "kind": "EndpointSlice",
                "metadata": {"name": name, "namespace": namespace},
            }
        ),
    )


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    """Tests for FunctionRunner.RunFunction."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:
        """The function composes a Service and EndpointSlice from a ModelEndpoint."""

        ip_xr = v1alpha1.ModelEndpoint(
            metadata=metav1.ObjectMeta(name="test-endpoint", namespace="ml-team"),
            spec=v1alpha1.Spec(url="http://34.55.100.10/v1"),
        ).model_dump(exclude_none=True, mode="json")

        ip_service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"namespace": "ml-team"},
            "spec": {
                "ports": [{"port": 80, "protocol": "TCP"}],
            },
        }
        ip_endpointslice = {
            "apiVersion": "discovery.k8s.io/v1",
            "kind": "EndpointSlice",
            "metadata": {
                "namespace": "ml-team",
                "labels": {"kubernetes.io/service-name": "my-service"},
            },
            "addressType": "IPv4",
            "ports": [{"name": "", "port": 80, "protocol": "TCP"}],
            "endpoints": [
                {
                    "addresses": ["34.55.100.10"],
                    "conditions": {"ready": True},
                }
            ],
        }

        fqdn_xr = v1alpha1.ModelEndpoint(
            metadata=metav1.ObjectMeta(name="test-endpoint", namespace="ml-team"),
            spec=v1alpha1.Spec(url="https://api.together.xyz/v1"),
        ).model_dump(exclude_none=True, mode="json")

        fqdn_service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"namespace": "ml-team"},
            "spec": {
                "ports": [{"port": 443, "protocol": "TCP"}],
            },
        }
        fqdn_endpointslice = {
            "apiVersion": "discovery.k8s.io/v1",
            "kind": "EndpointSlice",
            "metadata": {
                "namespace": "ml-team",
                "labels": {"kubernetes.io/service-name": "together-svc"},
            },
            "addressType": "FQDN",
            "ports": [{"name": "", "port": 443, "protocol": "TCP"}],
            "endpoints": [
                {
                    "addresses": ["api.together.xyz"],
                    "conditions": {"ready": True},
                }
            ],
        }

        cases = [
            Case(
                name="IP URL first pass composes Service only; EndpointSlice gated on Service name",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(ip_xr)),
                    ),
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct({"status": {}}),
                        ),
                        resources={
                            "service": fnv1.Resource(
                                resource=resource.dict_to_struct(ip_service),
                            ),
                        },
                    ),
                    conditions=[
                        fnv1.Condition(
                            type="RoutingReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForBackend"
                        ),
                    ],
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="IP URL second pass composes EndpointSlice; backendName not yet set",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(ip_xr)),
                        resources={
                            "service": _service_only("my-service"),
                        },
                    ),
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct({"status": {}}),
                        ),
                        resources={
                            "service": fnv1.Resource(
                                resource=resource.dict_to_struct(ip_service),
                                ready=fnv1.READY_TRUE,
                            ),
                            "endpointslice": fnv1.Resource(
                                resource=resource.dict_to_struct(ip_endpointslice),
                            ),
                        },
                    ),
                    conditions=[
                        fnv1.Condition(
                            type="RoutingReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForBackend"
                        ),
                    ],
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="IP URL third pass with EndpointSlice observed sets backendName and RoutingReady",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(ip_xr)),
                        resources={
                            "service": _service_only("my-service"),
                            "endpointslice": _endpointslice_only("my-slice"),
                        },
                    ),
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct({"status": {"routing": {"backendName": "my-service"}}}),
                        ),
                        resources={
                            "service": fnv1.Resource(
                                resource=resource.dict_to_struct(ip_service),
                                ready=fnv1.READY_TRUE,
                            ),
                            "endpointslice": fnv1.Resource(
                                resource=resource.dict_to_struct(ip_endpointslice),
                                ready=fnv1.READY_TRUE,
                            ),
                        },
                    ),
                    conditions=[
                        fnv1.Condition(
                            type="RoutingReady", status=fnv1.STATUS_CONDITION_TRUE, reason="BackendConfigured"
                        ),
                    ],
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="FQDN URL first pass composes Service only",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(fqdn_xr)),
                    ),
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct({"status": {}}),
                        ),
                        resources={
                            "service": fnv1.Resource(
                                resource=resource.dict_to_struct(fqdn_service),
                            ),
                        },
                    ),
                    conditions=[
                        fnv1.Condition(
                            type="RoutingReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForBackend"
                        ),
                    ],
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="FQDN URL with EndpointSlice observed sets backendName and RoutingReady",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(fqdn_xr)),
                        resources={
                            "service": _service_only("together-svc"),
                            "endpointslice": _endpointslice_only("together-slice"),
                        },
                    ),
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct({"status": {"routing": {"backendName": "together-svc"}}}),
                        ),
                        resources={
                            "service": fnv1.Resource(
                                resource=resource.dict_to_struct(fqdn_service),
                                ready=fnv1.READY_TRUE,
                            ),
                            "endpointslice": fnv1.Resource(
                                resource=resource.dict_to_struct(fqdn_endpointslice),
                                ready=fnv1.READY_TRUE,
                            ),
                        },
                    ),
                    conditions=[
                        fnv1.Condition(
                            type="RoutingReady", status=fnv1.STATUS_CONDITION_TRUE, reason="BackendConfigured"
                        ),
                    ],
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="IPv6 URL composes EndpointSlice with addressType IPv6",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                v1alpha1.ModelEndpoint(
                                    metadata=metav1.ObjectMeta(name="test-endpoint", namespace="ml-team"),
                                    spec=v1alpha1.Spec(url="http://[2001:db8::1]/v1"),
                                ).model_dump(exclude_none=True, mode="json")
                            ),
                        ),
                        resources={
                            "service": _service_only("v6-svc"),
                            "endpointslice": _endpointslice_only("v6-slice"),
                        },
                    ),
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct({"status": {"routing": {"backendName": "v6-svc"}}}),
                        ),
                        resources={
                            "service": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "v1",
                                        "kind": "Service",
                                        "metadata": {"namespace": "ml-team"},
                                        "spec": {
                                            "ports": [{"port": 80, "protocol": "TCP"}],
                                        },
                                    }
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                            "endpointslice": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "discovery.k8s.io/v1",
                                        "kind": "EndpointSlice",
                                        "metadata": {
                                            "namespace": "ml-team",
                                            "labels": {"kubernetes.io/service-name": "v6-svc"},
                                        },
                                        "addressType": "IPv6",
                                        "ports": [{"name": "", "port": 80, "protocol": "TCP"}],
                                        "endpoints": [
                                            {
                                                "addresses": ["2001:db8::1"],
                                                "conditions": {"ready": True},
                                            }
                                        ],
                                    }
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                        },
                    ),
                    conditions=[
                        fnv1.Condition(
                            type="RoutingReady", status=fnv1.STATUS_CONDITION_TRUE, reason="BackendConfigured"
                        ),
                    ],
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="invalid URL produces a warning and no service",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                v1alpha1.ModelEndpoint(
                                    metadata=metav1.ObjectMeta(name="test-endpoint", namespace="ml-team"),
                                    spec=v1alpha1.Spec(url="not-a-url"),
                                ).model_dump(exclude_none=True, mode="json")
                            ),
                        ),
                    ),
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(),
                    conditions=[
                        fnv1.Condition(
                            type="RoutingReady",
                            status=fnv1.STATUS_CONDITION_FALSE,
                            reason="InvalidURL",
                            message="Invalid spec.url: not-a-url",
                        ),
                    ],
                    results=[
                        fnv1.Result(severity=fnv1.SEVERITY_WARNING, message="Invalid spec.url: not-a-url"),
                    ],
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="URL with a non-integer port produces a warning and no service",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                v1alpha1.ModelEndpoint(
                                    metadata=metav1.ObjectMeta(name="test-endpoint", namespace="ml-team"),
                                    spec=v1alpha1.Spec(url="https://host:abc"),
                                ).model_dump(exclude_none=True, mode="json")
                            ),
                        ),
                    ),
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(),
                    conditions=[
                        fnv1.Condition(
                            type="RoutingReady",
                            status=fnv1.STATUS_CONDITION_FALSE,
                            reason="InvalidURL",
                            message="Invalid spec.url: https://host:abc",
                        ),
                    ],
                    results=[
                        fnv1.Result(severity=fnv1.SEVERITY_WARNING, message="Invalid spec.url: https://host:abc"),
                    ],
                    context=structpb.Struct(),
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
