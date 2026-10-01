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

import asyncio
import dataclasses
import json

import pytest
from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format, message
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.modelservice import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-model-service."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _model_service(*, labels: dict[str, str] | None) -> fnv1.Resource:
    """The ModelService XR assistant in ml-team, serving kimi-k2."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            v1alpha1.ModelService(
                apiVersion="modelplane.ai/v1alpha1",
                kind="ModelService",
                metadata=metav1.ObjectMeta(name="assistant", namespace="ml-team", labels=labels),
                spec=v1alpha1.Spec(
                    endpoints=[
                        v1alpha1.Endpoint(
                            name="kimi-k2",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "kimi-k2"}),
                        )
                    ],
                    timeouts=v1alpha1.Timeouts(request="600s", idle="0s"),
                ),
            ).model_dump(exclude_none=True, mode="json", by_alias=True)
        )
    )


def _desired_model_service(*, total_routes: int, ready_routes: int, ready: fnv1.Ready) -> fnv1.Resource:
    """The desired ModelService XR, reporting its model and how many of its routes are ready."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {"status": {"model": "ml-team/assistant", "routes": {"total": total_routes, "ready": ready_routes}}}
        ),
        ready=ready,
    )


def _inference_gateway(
    *, name: str, cluster_name: str, service_selector: dict | None, address: str | None
) -> fnv1.Resource:
    """An InferenceGateway, as the gateways requirement returns it."""
    spec: dict = {"clusterName": cluster_name}
    if service_selector is not None:
        spec["serviceSelector"] = service_selector
    gateway: dict = {
        "apiVersion": "modelplane.ai/v1alpha1",
        "kind": "InferenceGateway",
        "metadata": {"name": name},
        "spec": spec,
    }
    if address is not None:
        gateway["status"] = {"address": address}
    return fnv1.Resource(resource=resource.dict_to_struct(gateway))


def _model_route(*, name: str, gateway: str, cluster: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed ModelRoute pinning assistant to gateway on cluster."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "ModelRoute",
                "metadata": {
                    "name": name,
                    "namespace": "ml-team",
                    "labels": {
                        "modelplane.ai/service": "assistant",
                        "modelplane.ai/gateway": gateway,
                        "modelplane.ai/cluster": cluster,
                    },
                },
                "spec": {
                    "gatewayName": gateway,
                    "serviceName": "assistant",
                    "endpoints": [
                        {
                            "name": "kimi-k2",
                            "selector": {"matchLabels": {"modelplane.ai/deployment": "kimi-k2"}},
                            "priority": 0,
                            "weight": 1,
                        }
                    ],
                    "timeouts": {"request": "600s", "idle": "0s"},
                },
            }
        ),
        ready=ready,
    )


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


# Every ModelService here sets timeouts that aren't the defaults, so a route
# carrying the defaults fails to match. Each composed ModelRoute's cluster label
# carries its gateway's clusterName, which compose-inference-cluster selects
# routes by.
COMPOSE_CASES = [
    Case(
        name="gateways not resolved yet: require them and wait",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_model_service(labels=None)),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_service(total_routes=0, ready_routes=0, ready=fnv1.READY_FALSE)
            ),
            results=[fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for the gateways to resolve")],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForGateways",
                    message="Waiting for the gateways to resolve",
                )
            ],
        ),
    ),
    Case(
        name="no gateway selects the service: unreachable, and say so",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_model_service(labels={"region": "us"})),
            required_resources={
                "gateways": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            name="eu",
                            cluster_name="gw-eu",
                            service_selector={"matchLabels": {"region": "eu"}},
                            address=None,
                        ),
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_service(total_routes=0, ready_routes=0, ready=fnv1.READY_FALSE)
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message=(
                        "No InferenceGateway's serviceSelector matches this service's labels, so no caller can reach it"
                    ),
                )
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="NoGatewayServesThisService",
                    message=(
                        "No InferenceGateway's serviceSelector matches this service's labels, so no caller can reach it"
                    ),
                )
            ],
        ),
    ),
    # A gateway with no address is left out of readiness, but with no other
    # gateway there is nowhere a caller could reach the service.
    Case(
        name="its only gateway has no address yet: not RoutingReady",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_model_service(labels=None)),
            required_resources={
                "gateways": fnv1.Resources(
                    items=[
                        _inference_gateway(name="eu", cluster_name="gw-eu", service_selector=None, address=None),
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_service(total_routes=1, ready_routes=0, ready=fnv1.READY_FALSE),
                resources={
                    "route-eu": _model_route(
                        name="assistant-eu-b0e0c", gateway="eu", cluster="gw-eu", ready=fnv1.READY_UNSPECIFIED
                    ),
                },
            ),
            results=[fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for gateways to come up: eu")],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForGateways",
                    message="Waiting for gateways to come up: eu",
                )
            ],
        ),
    ),
    Case(
        name="two gateways serve it: a ModelRoute each, waiting for both routes",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_model_service(labels=None)),
            required_resources={
                "gateways": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            name="eu", cluster_name="gw-eu", service_selector=None, address="203.0.113.1"
                        ),
                        _inference_gateway(
                            name="us", cluster_name="gw-us", service_selector=None, address="203.0.113.2"
                        ),
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_service(total_routes=2, ready_routes=0, ready=fnv1.READY_FALSE),
                resources={
                    "route-eu": _model_route(
                        name="assistant-eu-b0e0c", gateway="eu", cluster="gw-eu", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-us": _model_route(
                        name="assistant-us-04e03", gateway="us", cluster="gw-us", ready=fnv1.READY_UNSPECIFIED
                    ),
                },
            ),
            results=[fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for routes on gateways: eu, us")],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRoutes",
                    message="Waiting for routes on gateways: eu, us",
                )
            ],
        ),
    ),
    Case(
        name="both routes accepted: ModelRoutes ready, service RoutingReady",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_service(labels=None),
                resources={
                    "route-eu": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "modelplane.ai/v1alpha1",
                                "kind": "ModelRoute",
                                "metadata": {"name": "assistant-eu-b0e0c", "namespace": "ml-team"},
                                "status": {
                                    "conditions": [
                                        {
                                            "type": "Ready",
                                            "status": "True",
                                            "reason": "Available",
                                            "lastTransitionTime": "2026-06-08T00:00:00Z",
                                        }
                                    ]
                                },
                            }
                        )
                    ),
                    "route-us": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "modelplane.ai/v1alpha1",
                                "kind": "ModelRoute",
                                "metadata": {"name": "assistant-us-04e03", "namespace": "ml-team"},
                                "status": {
                                    "conditions": [
                                        {
                                            "type": "Ready",
                                            "status": "True",
                                            "reason": "Available",
                                            "lastTransitionTime": "2026-06-08T00:00:00Z",
                                        }
                                    ]
                                },
                            }
                        )
                    ),
                },
            ),
            required_resources={
                "gateways": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            name="eu", cluster_name="gw-eu", service_selector=None, address="203.0.113.1"
                        ),
                        _inference_gateway(
                            name="us", cluster_name="gw-us", service_selector=None, address="203.0.113.2"
                        ),
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_service(total_routes=2, ready_routes=2, ready=fnv1.READY_TRUE),
                resources={
                    "route-eu": _model_route(
                        name="assistant-eu-b0e0c", gateway="eu", cluster="gw-eu", ready=fnv1.READY_TRUE
                    ),
                    "route-us": _model_route(
                        name="assistant-us-04e03", gateway="us", cluster="gw-us", ready=fnv1.READY_TRUE
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="RoutesAccepted",
                )
            ],
        ),
    ),
    # Neither gateway has a serviceSelector, so both serve the service. The us
    # gateway is still coming up (no address), so it's excluded from readiness
    # rather than failing it: both RoutingReady and the service's own Ready ignore
    # its unready ModelRoute.
    Case(
        name="a gateway with no address doesn't block readiness, nor does its unready ModelRoute",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_service(labels=None),
                resources={
                    "route-eu": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "modelplane.ai/v1alpha1",
                                "kind": "ModelRoute",
                                "metadata": {"name": "assistant-eu-b0e0c", "namespace": "ml-team"},
                                "status": {
                                    "conditions": [
                                        {
                                            "type": "Ready",
                                            "status": "True",
                                            "reason": "Available",
                                            "lastTransitionTime": "2026-06-08T00:00:00Z",
                                        }
                                    ]
                                },
                            }
                        )
                    ),
                },
            ),
            required_resources={
                "gateways": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            name="eu", cluster_name="gw-eu", service_selector=None, address="203.0.113.1"
                        ),
                        _inference_gateway(name="us", cluster_name="gw-us", service_selector=None, address=None),
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_service(total_routes=2, ready_routes=1, ready=fnv1.READY_TRUE),
                resources={
                    "route-eu": _model_route(
                        name="assistant-eu-b0e0c", gateway="eu", cluster="gw-eu", ready=fnv1.READY_TRUE
                    ),
                    "route-us": _model_route(
                        name="assistant-us-04e03", gateway="us", cluster="gw-us", ready=fnv1.READY_UNSPECIFIED
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="RoutesAccepted",
                )
            ],
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction composes a ModelRoute per serving gateway and reports routing readiness."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want)
