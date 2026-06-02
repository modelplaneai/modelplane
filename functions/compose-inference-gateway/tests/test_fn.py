"""Tests for the compose-inference-gateway function."""

import dataclasses
import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.inferencegateway import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-inference-gateway."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _crd_desired_resources(ready: bool) -> dict:
    """Desired Gateway API CRD resources, built from the same vendored bundle
    the function composes so the test stays in sync. When ready is True each
    CRD is marked READY_TRUE, matching a pass where the CRDs are observed as
    Established."""
    out = {}
    for doc in fn._GATEWAY_API_CRDS:
        key = fn._crd_key(doc)
        res = fnv1.Resource(resource=resource.dict_to_struct(doc))
        if ready:
            res.ready = fnv1.READY_TRUE
        out[key] = res
    return out


def _crd_observed_resources() -> dict:
    """Observed Gateway API CRD resources, each reporting Established."""
    out = {}
    for doc in fn._GATEWAY_API_CRDS:
        key = fn._crd_key(doc)
        observed = {
            "apiVersion": doc["apiVersion"],
            "kind": doc["kind"],
            "status": {"conditions": [{"type": "Established", "status": "True"}]},
        }
        out[key] = fnv1.Resource(resource=resource.dict_to_struct(observed))
    return out


def _gateway_usage_resources() -> dict:
    """The Usages ordering the GatewayClass and Gateway ahead of the Traefik
    release on teardown."""
    release_by = {
        "apiVersion": "helm.m.crossplane.io/v1beta1",
        "kind": "Release",
        "resourceSelector": {
            "matchControllerRef": True,
            "matchLabels": {"modelplane.ai/release": "traefik"},
        },
    }
    return {
        "usage-gateway-class-by-traefik": fnv1.Resource(
            resource=resource.dict_to_struct(
                {
                    "apiVersion": "protection.crossplane.io/v1beta1",
                    "kind": "ClusterUsage",
                    "spec": {
                        "of": {
                            "apiVersion": "gateway.networking.k8s.io/v1",
                            "kind": "GatewayClass",
                            "resourceRef": {"name": "traefik"},
                        },
                        "by": release_by,
                        "replayDeletion": True,
                    },
                }
            ),
            ready=fnv1.READY_TRUE,
        ),
        "usage-gateway-by-traefik": fnv1.Resource(
            resource=resource.dict_to_struct(
                {
                    "apiVersion": "protection.crossplane.io/v1beta1",
                    "kind": "Usage",
                    "metadata": {"namespace": "modelplane-system"},
                    "spec": {
                        "of": {
                            "apiVersion": "gateway.networking.k8s.io/v1",
                            "kind": "Gateway",
                            "resourceRef": {"name": "modelplane"},
                        },
                        "by": release_by,
                        "replayDeletion": True,
                    },
                }
            ),
            ready=fnv1.READY_TRUE,
        ),
    }


def _traefik_desired_release(ready: bool) -> fnv1.Resource:
    """The desired Traefik Helm Release the function composes once the
    ProviderConfig is observed and the Gateway API CRDs are Established."""
    res = fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "Release",
                "metadata": {
                    "namespace": "modelplane-system",
                    "labels": {"modelplane.ai/release": "traefik"},
                },
                "spec": {
                    "providerConfigRef": {
                        "kind": "ProviderConfig",
                        "name": "modelplane-in-cluster",
                    },
                    "forProvider": {
                        "chart": {
                            "name": "traefik",
                            "repository": "https://traefik.github.io/charts",
                            "version": "40.2.0",
                        },
                        "namespace": "traefik-system",
                        "values": {
                            "providers": {
                                "kubernetesGateway": {
                                    "enabled": True,
                                    "statusAddress": {
                                        "service": {
                                            "namespace": "traefik-system",
                                            "name": "traefik",
                                        },
                                    },
                                },
                                "kubernetesIngress": {"enabled": False},
                            },
                            "service": {"nameOverride": "traefik"},
                            "gateway": {"enabled": False},
                            "gatewayClass": {"enabled": False},
                        },
                    },
                },
            }
        ),
    )
    if ready:
        res.ready = fnv1.READY_TRUE
    return res


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    """Tests for FunctionRunner.RunFunction."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:
        """The function composes an InferenceGateway."""
        cases = [
            Case(
                name="first pass composes provider config and gateway api crds; traefik and gateway are gated",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                v1alpha1.InferenceGateway(
                                    metadata=metav1.ObjectMeta(
                                        name="test-gateway",
                                        namespace="modelplane-system",
                                    ),
                                    spec=v1alpha1.Spec(traefik=v1alpha1.Traefik(version="40.2.0")),
                                ).model_dump(exclude_none=True, mode="json")
                            ),
                        ),
                    ),
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct({"status": {}}),
                        ),
                        resources={
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "ProviderConfig",
                                        "metadata": {
                                            "name": "modelplane-in-cluster",
                                            "namespace": "modelplane-system",
                                        },
                                        "spec": {"credentials": {"source": "InjectedIdentity"}},
                                    }
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                            # CRDs are composed on the first pass but not yet
                            # observed as Established, so they aren't ready and
                            # Traefik stays gated.
                            **_crd_desired_resources(ready=False),
                        },
                    ),
                    conditions=[
                        fnv1.Condition(
                            type="ControllerReady",
                            status=fnv1.STATUS_CONDITION_FALSE,
                            reason="Installing",
                        ),
                    ],
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="traefik is composed with its gateway usages in the same pass the crds become established",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                v1alpha1.InferenceGateway(
                                    metadata=metav1.ObjectMeta(
                                        name="test-gateway",
                                        namespace="modelplane-system",
                                    ),
                                    spec=v1alpha1.Spec(traefik=v1alpha1.Traefik(version="40.2.0")),
                                ).model_dump(exclude_none=True, mode="json")
                            ),
                        ),
                        # The ProviderConfig is observed and the CRDs report
                        # Established, so Traefik is composed this pass. It is
                        # not yet observed: its Usages must still be composed
                        # now so deletion-order protection is in place the
                        # moment the Release is first emitted as desired state.
                        resources={
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "ProviderConfig",
                                        "metadata": {"name": "modelplane-in-cluster"},
                                    }
                                ),
                            ),
                            **_crd_observed_resources(),
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
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "ProviderConfig",
                                        "metadata": {
                                            "name": "modelplane-in-cluster",
                                            "namespace": "modelplane-system",
                                        },
                                        "spec": {"credentials": {"source": "InjectedIdentity"}},
                                    }
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                            # Traefik is composed but not yet observed, so it
                            # isn't marked ready and the Gateway/GatewayClass
                            # stay gated.
                            "traefik": _traefik_desired_release(ready=False),
                            "usage-pc-by-traefik": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "protection.crossplane.io/v1beta1",
                                        "kind": "Usage",
                                        "metadata": {"namespace": "modelplane-system"},
                                        "spec": {
                                            "of": {
                                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                                "kind": "ProviderConfig",
                                                "resourceRef": {"name": "modelplane-in-cluster"},
                                            },
                                            "by": {
                                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                                "kind": "Release",
                                                "resourceSelector": {
                                                    "matchControllerRef": True,
                                                    "matchLabels": {"modelplane.ai/release": "traefik"},
                                                },
                                            },
                                            "replayDeletion": True,
                                        },
                                    }
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                            **_crd_desired_resources(ready=True),
                            # The gateway usages are composed alongside Traefik,
                            # before the Release is observed.
                            **_gateway_usage_resources(),
                        },
                    ),
                    conditions=[
                        fnv1.Condition(
                            type="ControllerReady",
                            status=fnv1.STATUS_CONDITION_FALSE,
                            reason="Installing",
                        ),
                    ],
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="second pass with observed crds and traefik ready composes gateway resources",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                v1alpha1.InferenceGateway(
                                    metadata=metav1.ObjectMeta(
                                        name="test-gateway",
                                        namespace="modelplane-system",
                                    ),
                                    spec=v1alpha1.Spec(traefik=v1alpha1.Traefik(version="40.2.0")),
                                ).model_dump(exclude_none=True, mode="json")
                            ),
                        ),
                        resources={
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "ProviderConfig",
                                        "metadata": {"name": "modelplane-in-cluster"},
                                    }
                                ),
                            ),
                            "traefik": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "status": {
                                            "conditions": [{"type": "Ready", "status": "True"}],
                                        },
                                    }
                                ),
                            ),
                            "gateway": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "gateway.networking.k8s.io/v1",
                                        "kind": "Gateway",
                                        "metadata": {"name": "modelplane"},
                                        "status": {
                                            "addresses": [{"value": "10.0.0.42"}],
                                            "conditions": [{"type": "Accepted", "status": "True"}],
                                        },
                                    }
                                ),
                            ),
                            "gateway-class": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "gateway.networking.k8s.io/v1",
                                        "kind": "GatewayClass",
                                        "metadata": {"name": "traefik"},
                                        "status": {
                                            "conditions": [{"type": "Accepted", "status": "True"}],
                                        },
                                    }
                                ),
                            ),
                            # CRDs observed as Established ungate Traefik.
                            **_crd_observed_resources(),
                        },
                    ),
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {"status": {"address": "10.0.0.42"}},
                            ),
                        ),
                        resources={
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "ProviderConfig",
                                        "metadata": {
                                            "name": "modelplane-in-cluster",
                                            "namespace": "modelplane-system",
                                        },
                                        "spec": {"credentials": {"source": "InjectedIdentity"}},
                                    }
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                            "traefik": _traefik_desired_release(ready=True),
                            "usage-pc-by-traefik": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "protection.crossplane.io/v1beta1",
                                        "kind": "Usage",
                                        "metadata": {"namespace": "modelplane-system"},
                                        "spec": {
                                            "of": {
                                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                                "kind": "ProviderConfig",
                                                "resourceRef": {"name": "modelplane-in-cluster"},
                                            },
                                            "by": {
                                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                                "kind": "Release",
                                                "resourceSelector": {
                                                    "matchControllerRef": True,
                                                    "matchLabels": {"modelplane.ai/release": "traefik"},
                                                },
                                            },
                                            "replayDeletion": True,
                                        },
                                    }
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                            "gateway-class": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "gateway.networking.k8s.io/v1",
                                        "kind": "GatewayClass",
                                        "metadata": {"name": "traefik"},
                                        "spec": {
                                            "controllerName": "traefik.io/gateway-controller",
                                        },
                                    }
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                            "gateway": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "gateway.networking.k8s.io/v1",
                                        "kind": "Gateway",
                                        "metadata": {
                                            "name": "modelplane",
                                            "namespace": "modelplane-system",
                                        },
                                        "spec": {
                                            "gatewayClassName": "traefik",
                                            "listeners": [
                                                {
                                                    "name": "web",
                                                    "protocol": "HTTP",
                                                    "port": 8000,
                                                    "allowedRoutes": {"namespaces": {"from": "All"}},
                                                },
                                            ],
                                        },
                                    }
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                            # CRDs remain composed and are ready now that
                            # they're observed as Established.
                            **_crd_desired_resources(ready=True),
                            # Usages ordering GatewayClass/Gateway ahead of the
                            # Traefik release on teardown.
                            **_gateway_usage_resources(),
                        },
                    ),
                    conditions=[
                        fnv1.Condition(
                            type="ControllerReady",
                            status=fnv1.STATUS_CONDITION_TRUE,
                            reason="ControllerHealthy",
                        ),
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
