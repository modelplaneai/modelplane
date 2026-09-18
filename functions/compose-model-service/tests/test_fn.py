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

"""Tests for the compose-model-service function."""

import dataclasses
import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.inferencegateway import v1alpha1 as igv1alpha1
from models.ai.modelplane.modelroute import v1alpha1 as mrtv1alpha1
from models.ai.modelplane.modelservice import v1alpha1

_NS = "ml-team"
_SVC = "assistant"
_MODEL = f"{_NS}/{_SVC}"


@dataclasses.dataclass
class Case:
    """A test case for compose-model-service."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _entry(deployment: str, *, priority: int | None = None, weight: int | None = None) -> v1alpha1.Endpoint:
    kwargs = {}
    if priority is not None:
        kwargs["priority"] = priority
    if weight is not None:
        kwargs["weight"] = weight
    return v1alpha1.Endpoint(
        name=deployment,
        selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": deployment}),
        **kwargs,
    )


def _service(entries: list[v1alpha1.Endpoint], labels: dict[str, str] | None = None) -> dict:
    xr = v1alpha1.ModelService(
        apiVersion="modelplane.ai/v1alpha1",
        kind="ModelService",
        metadata={"name": _SVC, "namespace": _NS, **({"labels": labels} if labels else {})},
        spec=v1alpha1.Spec(endpoints=entries),
    )
    return xr.model_dump(exclude_none=True, mode="json", by_alias=True)


def _gateway(name: str, cluster: str, *, selector: dict[str, str] | None = None, address: str | None = None) -> dict:
    gw = igv1alpha1.InferenceGateway(
        apiVersion="modelplane.ai/v1alpha1",
        kind="InferenceGateway",
        metadata={"name": name},
        spec=igv1alpha1.Spec(
            clusterName=cluster,
            **({"serviceSelector": igv1alpha1.ServiceSelector(matchLabels=selector)} if selector else {}),
        ),
    )
    d = gw.model_dump(exclude_none=True, mode="json", by_alias=True)
    if address:
        d["status"] = {"address": address}
    return d


def _route(gateway: str) -> dict:
    """The ModelRoute the function composes for one gateway, as a plain dict.

    The endpoints are spelled out rather than derived from the service's, so a
    bug in the copy the function does can't hide in an expectation computed the
    same way. Every case here drives the single kimi-k2 entry, which the copy
    fills to its priority/weight defaults.
    """
    route = mrtv1alpha1.ModelRoute(
        apiVersion="modelplane.ai/v1alpha1",
        kind="ModelRoute",
        metadata={
            "name": resource.child_name(_SVC, gateway),
            "namespace": _NS,
            "labels": {"modelplane.ai/service": _SVC, "modelplane.ai/gateway": gateway},
        },
        spec=mrtv1alpha1.Spec(
            gatewayName=gateway,
            serviceName=_SVC,
            endpoints=[
                mrtv1alpha1.Endpoint(
                    name="kimi-k2",
                    selector=mrtv1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "kimi-k2"}),
                    priority=0,
                    weight=1,
                )
            ],
        ),
    )
    return route.model_dump(exclude_none=True, mode="json", by_alias=True)


def _observed_route(gateway: str, ready: bool) -> fnv1.Resource:
    """A composed ModelRoute as observed back, Ready or not."""
    d = {
        "apiVersion": "modelplane.ai/v1alpha1",
        "kind": "ModelRoute",
        "metadata": {"name": resource.child_name(_SVC, gateway), "namespace": _NS},
        "status": {
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True" if ready else "False",
                    "reason": "Available" if ready else "Creating",
                    "lastTransitionTime": "2026-06-08T00:00:00Z",
                }
            ]
        },
    }
    return fnv1.Resource(resource=resource.dict_to_struct(d))


def _required(**resources) -> dict:  # noqa: ANN003
    return {
        name: fnv1.Resources(items=[fnv1.Resource(resource=resource.dict_to_struct(r)) for r in items])
        for name, items in resources.items()
    }


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:
        entries = [_entry("kimi-k2")]
        cases = [
            Case(
                name="gateways not resolved yet: require them and wait",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_service(entries)))),
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {"status": {"model": _MODEL, "routes": {"total": 0, "ready": 0}}}
                            ),
                            ready=fnv1.READY_FALSE,
                        )
                    ),
                    context=structpb.Struct(),
                    requirements=fnv1.Requirements(
                        resources={
                            "gateways": fnv1.ResourceSelector(
                                api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"
                            ),
                        }
                    ),
                    conditions=[
                        fnv1.Condition(
                            type=fn.CONDITION_TYPE_ROUTING_READY,
                            status=fnv1.STATUS_CONDITION_FALSE,
                            reason=fn.CONDITION_REASON_WAITING_FOR_GATEWAYS,
                            message="Waiting for the gateways to resolve",
                        )
                    ],
                    results=[fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for the gateways to resolve")],
                ),
            ),
            Case(
                name="no gateway selects the service: unreachable, and say so",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(_service(entries, labels={"region": "us"}))
                        )
                    ),
                    required_resources=_required(gateways=[_gateway("eu", "gw-eu", selector={"region": "eu"})]),
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {"status": {"model": _MODEL, "routes": {"total": 0, "ready": 0}}}
                            ),
                            ready=fnv1.READY_FALSE,
                        )
                    ),
                    context=structpb.Struct(),
                    requirements=fnv1.Requirements(
                        resources={
                            "gateways": fnv1.ResourceSelector(
                                api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"
                            ),
                        }
                    ),
                    conditions=[
                        fnv1.Condition(
                            type=fn.CONDITION_TYPE_ROUTING_READY,
                            status=fnv1.STATUS_CONDITION_FALSE,
                            reason=fn.CONDITION_REASON_NO_GATEWAY,
                            message=(
                                "No InferenceGateway's serviceSelector matches this service's labels, "
                                "so no caller can reach it"
                            ),
                        )
                    ],
                    results=[
                        fnv1.Result(
                            severity=fnv1.SEVERITY_NORMAL,
                            message=(
                                "No InferenceGateway's serviceSelector matches this service's labels, "
                                "so no caller can reach it"
                            ),
                        )
                    ],
                ),
            ),
            Case(
                name="two gateways serve it: a ModelRoute each, waiting for both routes",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_service(entries))),
                    ),
                    required_resources=_required(
                        gateways=[
                            _gateway("eu", "gw-eu", address="203.0.113.1"),
                            _gateway("us", "gw-us", address="203.0.113.2"),
                        ],
                    ),
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {"status": {"model": _MODEL, "routes": {"total": 2, "ready": 0}}}
                            ),
                            ready=fnv1.READY_FALSE,
                        ),
                        resources={
                            "route-eu": fnv1.Resource(resource=resource.dict_to_struct(_route("eu"))),
                            "route-us": fnv1.Resource(resource=resource.dict_to_struct(_route("us"))),
                        },
                    ),
                    context=structpb.Struct(),
                    requirements=fnv1.Requirements(
                        resources={
                            "gateways": fnv1.ResourceSelector(
                                api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"
                            ),
                        }
                    ),
                    conditions=[
                        fnv1.Condition(
                            type=fn.CONDITION_TYPE_ROUTING_READY,
                            status=fnv1.STATUS_CONDITION_FALSE,
                            reason=fn.CONDITION_REASON_WAITING_FOR_ROUTES,
                            message="Waiting for routes on gateways: eu, us",
                        )
                    ],
                    results=[
                        fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for routes on gateways: eu, us")
                    ],
                ),
            ),
            Case(
                name="both routes accepted: ModelRoutes ready, service RoutingReady",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_service(entries))),
                        resources={
                            "route-eu": _observed_route("eu", ready=True),
                            "route-us": _observed_route("us", ready=True),
                        },
                    ),
                    required_resources=_required(
                        gateways=[
                            _gateway("eu", "gw-eu", address="203.0.113.1"),
                            _gateway("us", "gw-us", address="203.0.113.2"),
                        ],
                    ),
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {"status": {"model": _MODEL, "routes": {"total": 2, "ready": 2}}}
                            )
                        ),
                        resources={
                            "route-eu": fnv1.Resource(
                                resource=resource.dict_to_struct(_route("eu")), ready=fnv1.READY_TRUE
                            ),
                            "route-us": fnv1.Resource(
                                resource=resource.dict_to_struct(_route("us")), ready=fnv1.READY_TRUE
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                    requirements=fnv1.Requirements(
                        resources={
                            "gateways": fnv1.ResourceSelector(
                                api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"
                            ),
                        }
                    ),
                    conditions=[
                        fnv1.Condition(
                            type=fn.CONDITION_TYPE_ROUTING_READY,
                            status=fnv1.STATUS_CONDITION_TRUE,
                            reason=fn.CONDITION_REASON_ROUTES_ACCEPTED,
                        )
                    ],
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

    async def test_absent_selector_serves_every_service(self) -> None:
        """A gateway with no serviceSelector serves the service, and one still
        coming up (no address) is excluded from readiness rather than failing
        it."""
        entries = [_entry("kimi-k2")]
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(_service(entries))),
                resources={"route-eu": _observed_route("eu", ready=True)},
            ),
            required_resources=_required(
                gateways=[
                    _gateway("eu", "gw-eu", address="203.0.113.1"),
                    _gateway("us", "gw-us"),  # no address: still coming up
                ],
            ),
        )
        got = await self.runner.RunFunction(req, None)
        self.assertIn("route-eu", got.desired.resources)
        self.assertIn("route-us", got.desired.resources)
        cond = next(c for c in got.conditions if c.type == fn.CONDITION_TYPE_ROUTING_READY)
        self.assertEqual(cond.status, fnv1.STATUS_CONDITION_TRUE, "us has no address, so it doesn't block")
