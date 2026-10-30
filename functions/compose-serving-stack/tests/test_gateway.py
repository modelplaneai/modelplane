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

"""Tests for the cluster gateway pair."""

import dataclasses
from typing import Any

import pytest
from function import gateway
from models.ai.modelplane.infrastructure.servingstack import v1alpha1


def _gateway_class() -> dict:
    """The GatewayClass every case composes, pointing at the cluster-gateway EnvoyProxy."""
    return {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "GatewayClass",
        "metadata": {"name": "envoy"},
        "spec": {
            "controllerName": "gateway.envoyproxy.io/gatewayclass-controller",
            "parametersRef": {
                "group": "gateway.envoyproxy.io",
                "kind": "EnvoyProxy",
                "name": "cluster-gateway",
                "namespace": "modelplane-system",
            },
        },
    }


def _listeners() -> list[dict]:
    """The HTTPS listener every case's Gateway carries."""
    return [
        {
            "name": "https",
            "protocol": "HTTPS",
            "port": 443,
            "hostname": "test-backend.gateways.example.com",
            "tls": {
                "mode": "Terminate",
                "certificateRefs": [{"name": "cluster-gateway-serving"}],
            },
            "allowedRoutes": {
                "namespaces": {
                    "from": "Selector",
                    "selector": {"matchExpressions": [{"key": "modelplane.ai/namespace", "operator": "Exists"}]},
                }
            },
        }
    ]


@dataclasses.dataclass
class Case:
    """A test case for gateway.objects."""

    name: str
    reason: str
    gw: v1alpha1.Gateway
    want: list[tuple[str, dict[str, Any], str | None]]


OBJECTS_CASES = [
    Case(
        name="Plain",
        reason="Without service annotations, the pair is the GatewayClass and the Gateway, which uses the GatewayClass-level EnvoyProxy.",
        gw=v1alpha1.Gateway(hostname="test-backend.gateways.example.com"),
        want=[
            ("gateway-class", _gateway_class(), None),
            (
                "gateway",
                {
                    "apiVersion": "gateway.networking.k8s.io/v1",
                    "kind": "Gateway",
                    "metadata": {"name": "cluster-gateway", "namespace": "modelplane-system"},
                    "spec": {
                        "gatewayClassName": "envoy",
                        "listeners": _listeners(),
                    },
                },
                gateway.READY_CEL,
            ),
        ],
    ),
    Case(
        name="ServiceAnnotations",
        reason="With service annotations, a per-gateway EnvoyProxy carries them on the Gateway's Service with {gateway} replaced by the Gateway's name, and the Gateway references it through its own parametersRef.",
        gw=v1alpha1.Gateway(
            hostname="test-backend.gateways.example.com",
            serviceAnnotations={
                "service.beta.kubernetes.io/upcloud-load-balancer-config": (
                    '{"name": "11111111-2222-3333-4444-555555555555-{gateway}", "frontends": [{"mode": "tcp"}]}'
                ),
            },
        ),
        want=[
            ("gateway-class", _gateway_class(), None),
            (
                "gateway-lb-proxy",
                {
                    "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                    "kind": "EnvoyProxy",
                    "metadata": {"name": "cluster-gateway-lb", "namespace": "modelplane-system"},
                    "spec": {
                        "provider": {
                            "type": "Kubernetes",
                            "kubernetes": {
                                "envoyService": {
                                    "externalTrafficPolicy": "Cluster",
                                    "annotations": {
                                        "service.beta.kubernetes.io/upcloud-load-balancer-config": (
                                            '{"name": "11111111-2222-3333-4444-555555555555-cluster-gateway", "frontends": [{"mode": "tcp"}]}'
                                        ),
                                    },
                                },
                            },
                        },
                    },
                },
                None,
            ),
            (
                "gateway",
                {
                    "apiVersion": "gateway.networking.k8s.io/v1",
                    "kind": "Gateway",
                    "metadata": {"name": "cluster-gateway", "namespace": "modelplane-system"},
                    "spec": {
                        "gatewayClassName": "envoy",
                        "infrastructure": {
                            "parametersRef": {
                                "group": "gateway.envoyproxy.io",
                                "kind": "EnvoyProxy",
                                "name": "cluster-gateway-lb",
                            },
                        },
                        "listeners": _listeners(),
                    },
                },
                gateway.READY_CEL,
            ),
        ],
    ),
]


@pytest.mark.parametrize("case", OBJECTS_CASES, ids=lambda case: case.name)
def test_objects(case: Case) -> None:
    """objects renders the gateway pair from spec.gateway."""
    got = gateway.objects(case.gw)
    assert got == case.want, case.reason
