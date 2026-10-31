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

"""Tests for the compose-model-route function."""

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
from models.ai.modelplane.modelroute import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-model-route."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _model_route(*, endpoints: list[v1alpha1.Endpoint]) -> fnv1.Resource:
    """The ModelRoute XR pinning ml-team's assistant service to gateway eu."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            v1alpha1.ModelRoute(
                apiVersion="modelplane.ai/v1alpha1",
                kind="ModelRoute",
                metadata=metav1.ObjectMeta(name="assistant-eu", namespace="ml-team"),
                spec=v1alpha1.Spec(
                    gatewayName="eu",
                    serviceName="assistant",
                    endpoints=endpoints,
                    timeouts=v1alpha1.Timeouts(request="600s", idle="0s"),
                ),
            ).model_dump(exclude_none=True, mode="json", by_alias=True)
        )
    )


def _desired_model_route(*, address: str | None, total_endpoints: int, ready_endpoints: int) -> fnv1.Resource:
    """The desired ModelRoute XR, not yet ready, reporting its model, its gateway's address and its endpoint counts."""
    status: dict = {"model": "ml-team/assistant"}
    if address is not None:
        status["address"] = address
    status["endpoints"] = {"total": total_endpoints, "ready": ready_endpoints}
    return fnv1.Resource(resource=resource.dict_to_struct({"status": status}), ready=fnv1.READY_FALSE)


def _inference_gateway(*, tls: bool, address: str, client_ca_published: bool, gateway_ready: bool) -> fnv1.Resource:
    """The InferenceGateway eu on cluster gw-eu, as the gateway requirement returns it."""
    spec: dict = {"clusterName": "gw-eu"}
    if tls:
        spec["tls"] = {"certificateRefs": [{"name": "eu-tls"}]}
    status: dict = {"address": address}
    status["conditions"] = [
        {
            "type": "GatewayReady",
            "status": "True" if gateway_ready else "False",
            "reason": "GatewayProgrammed" if gateway_ready else "WaitingForEnvoyDeployment",
            "message": "" if gateway_ready else "Waiting for Envoy Deployment rollout",
            "lastTransitionTime": "2026-06-08T00:00:00Z",
        }
    ]
    if client_ca_published:
        status["clientCACertificate"] = "-----BEGIN CERTIFICATE-----\nclient\n-----END CERTIFICATE-----\n"
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "InferenceGateway",
                "metadata": {"name": "eu"},
                "spec": spec,
                "status": status,
            }
        )
    )


def _cluster(*, gateway_ca_published: bool) -> fnv1.Resource:
    """The InferenceCluster gw-eu the gateway runs on, as the clusters requirement returns it."""
    status: dict = {"providerConfigRef": {"name": "gw-eu-pc"}}
    if gateway_ca_published:
        status["gateway"] = {"caCertificate": "-----BEGIN CERTIFICATE-----\ncluster\n-----END CERTIFICATE-----\n"}
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "InferenceCluster",
                "metadata": {"name": "gw-eu"},
                "spec": {
                    "cluster": {
                        "source": "Existing",
                        "existing": {"secretRef": {"name": "gw-eu-kubeconfig", "key": "kubeconfig"}},
                    },
                    "stack": "Standard",
                },
                "status": status,
            }
        )
    )


def _composed_endpoint(*, model: str | None) -> fnv1.Resource:
    """The ready ModelEndpoint self, which Modelplane composed on gw-eu, as an endpoints requirement returns it."""
    spec: dict = {"origin": "https://gw-eu.example.com"}
    if model is not None:
        spec["model"] = model
    spec["api"] = {"schema": "OpenAI", "prefix": "/v1"}
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "ModelEndpoint",
                "metadata": {
                    "name": "self",
                    "namespace": "ml-team",
                    "labels": {"modelplane.ai/cluster": "gw-eu", "modelplane.ai/deployment": "d"},
                },
                "spec": spec,
                "status": {
                    "conditions": [
                        {
                            "type": "EndpointReady",
                            "status": "True",
                            "reason": "EndpointUsable",
                            "lastTransitionTime": "2026-06-08T00:00:00Z",
                        }
                    ]
                },
            }
        )
    )


def _third_party_endpoint(
    *,
    name: str,
    origin: str,
    model: str | None,
    schema: str,
    api_key_secret: str | None,
    ready: bool,
) -> fnv1.Resource:
    """A ModelEndpoint without the cluster label, so third-party, EndpointUsable if ready and CredentialMissing if not."""
    spec: dict = {"origin": origin}
    if model is not None:
        spec["model"] = model
    spec["api"] = {"schema": schema, "prefix": "/v1"}
    if api_key_secret is not None:
        spec["credential"] = {"method": "APIKey", "apiKey": {"secretRef": {"name": api_key_secret, "key": "apiKey"}}}
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "ModelEndpoint",
                "metadata": {"name": name, "namespace": "ml-team"},
                "spec": spec,
                "status": {
                    "conditions": [
                        {
                            "type": "EndpointReady",
                            "status": "True" if ready else "False",
                            "reason": "EndpointUsable" if ready else "CredentialMissing",
                            "lastTransitionTime": "2026-06-08T00:00:00Z",
                        }
                    ]
                },
            }
        )
    )


def _api_key_secret(*, name: str, data: dict[str, str]) -> fnv1.Resource:
    """A Secret in ml-team holding an endpoint's API key, as a credential requirement returns it."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": "ml-team"},
                "data": data,
            }
        )
    )


def _composed_endpoint_backend() -> fnv1.Resource:
    """The composed Backend for self, pinning this route's copy of gw-eu's CA and presenting its client certificate."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                            "kind": "Backend",
                            "metadata": {"name": "assistant-eu-self-a42e6", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "endpoints": [{"fqdn": {"hostname": "gw-eu.example.com", "port": 443}}],
                                "tls": {
                                    "caCertificateRefs": [
                                        {"kind": "ConfigMap", "group": "", "name": "assistant-eu-gw-eu-ca-3dd16"}
                                    ],
                                    "sni": "gw-eu.example.com",
                                    "clientCertificateRef": {
                                        "kind": "Secret",
                                        "group": "",
                                        "name": "assistant-eu-client-08324",
                                    },
                                },
                            },
                        }
                    },
                },
            }
        )
    )


def _composed_endpoint_ai_backend() -> fnv1.Resource:
    """The composed AIServiceBackend for self, which keeps the caller header because Modelplane operates self."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "aigateway.envoyproxy.io/v1beta1",
                            "kind": "AIServiceBackend",
                            "metadata": {"name": "assistant-eu-self-a42e6", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "schema": {"name": "OpenAI", "prefix": "/v1"},
                                "backendRef": {
                                    "group": "gateway.envoyproxy.io",
                                    "kind": "Backend",
                                    "name": "assistant-eu-self-a42e6",
                                },
                            },
                        }
                    },
                },
            }
        )
    )


def _third_party_backend(*, name: str, hostname: str) -> fnv1.Resource:
    """A composed Backend for a third-party endpoint, which trusts the system CAs."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                            "kind": "Backend",
                            "metadata": {"name": name, "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "endpoints": [{"fqdn": {"hostname": hostname, "port": 443}}],
                                "tls": {"wellKnownCACertificates": "System", "sni": hostname},
                            },
                        }
                    },
                },
            }
        )
    )


def _third_party_ai_backend(*, name: str, schema: str) -> fnv1.Resource:
    """A composed AIServiceBackend for a third-party endpoint, which strips the caller header."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "aigateway.envoyproxy.io/v1beta1",
                            "kind": "AIServiceBackend",
                            "metadata": {"name": name, "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "schema": {"name": schema, "prefix": "/v1"},
                                "backendRef": {"group": "gateway.envoyproxy.io", "kind": "Backend", "name": name},
                                "headerMutation": {"remove": ["x-modelplane-caller"]},
                            },
                        }
                    },
                },
            }
        )
    )


def _credential(*, name: str, api_key: str) -> fnv1.Resource:
    """The composed copy of an endpoint's API key Secret, under the apiKey key the AI Gateway reads."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "v1",
                            "kind": "Secret",
                            "metadata": {"name": name, "namespace": "mp-ml-team-51733"},
                            "type": "Opaque",
                            "data": {"apiKey": api_key},
                        }
                    },
                },
            }
        )
    )


def _credential_policy(*, name: str, auth: dict) -> fnv1.Resource:
    """The composed BackendSecurityPolicy sending an endpoint's API key the way auth says."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "aigateway.envoyproxy.io/v1beta1",
                            "kind": "BackendSecurityPolicy",
                            "metadata": {"name": name, "namespace": "mp-ml-team-51733"},
                            "spec": {
                                **auth,
                                "targetRefs": [
                                    {"group": "aigateway.envoyproxy.io", "kind": "AIServiceBackend", "name": name}
                                ],
                            },
                        }
                    },
                },
            }
        )
    )


def _cluster_ca() -> fnv1.Resource:
    """The composed ConfigMap holding gw-eu's gateway CA, named for this route so no other route composes it."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "v1",
                            "kind": "ConfigMap",
                            "metadata": {"name": "assistant-eu-gw-eu-ca-3dd16", "namespace": "mp-ml-team-51733"},
                            "data": {"ca.crt": "-----BEGIN CERTIFICATE-----\ncluster\n-----END CERTIFICATE-----\n"},
                        }
                    },
                },
            }
        )
    )


def _client_certificate() -> fnv1.Resource:
    """The composed client Certificate the composed endpoint's backend presents."""
    # Issued from the gateway's CA ClusterIssuer into this namespace. Named for
    # this route, so no other route in the namespace composes it, and deleted
    # with the route, so it sets no managementPolicies.
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-pc"},
                    "readiness": {
                        "policy": "DeriveFromCelQuery",
                        "celQuery": (
                            "has(object.status) && has(object.status.conditions) && "
                            "object.status.conditions.exists(c, c.type == 'Ready' && c.status == 'True')"
                        ),
                    },
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "cert-manager.io/v1",
                            "kind": "Certificate",
                            "metadata": {"name": "assistant-eu-client-08324", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "secretName": "assistant-eu-client-08324",
                                "commonName": "inference-gateway-eu",
                                "usages": ["client auth", "digital signature", "key encipherment"],
                                "duration": "2160h",
                                "renewBefore": "720h",
                                "privateKey": {"algorithm": "ECDSA", "size": 256, "rotationPolicy": "Always"},
                                "issuerRef": {
                                    "name": "inference-gateway-ca",
                                    "kind": "ClusterIssuer",
                                    "group": "cert-manager.io",
                                },
                            },
                        }
                    },
                },
            }
        )
    )


def _ai_gateway_route(*, section_name: str, backend_refs: list[dict], ready: fnv1.Ready) -> fnv1.Resource:
    """The composed AIGatewayRoute matching ml-team/assistant, bound to the gateway's section_name listener."""
    return fnv1.Resource(
        ready=ready,
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-pc"},
                    "readiness": {
                        "policy": "DeriveFromCelQuery",
                        "celQuery": (
                            "has(object.status) && has(object.status.conditions) && "
                            "object.status.conditions.exists(c, c.type == 'Accepted' && c.status == 'True')"
                        ),
                    },
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "aigateway.envoyproxy.io/v1beta1",
                            "kind": "AIGatewayRoute",
                            "metadata": {"name": "assistant", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                # The route lives in the team's namespace but
                                # attaches across to the gateway.
                                "parentRefs": [
                                    {
                                        "group": "gateway.networking.k8s.io",
                                        "kind": "Gateway",
                                        "name": "inference-gateway",
                                        "namespace": "modelplane-system",
                                        "sectionName": section_name,
                                    }
                                ],
                                "rules": [
                                    {
                                        "matches": [
                                            {
                                                "headers": [
                                                    {
                                                        "type": "Exact",
                                                        "name": "x-ai-eg-model",
                                                        "value": "ml-team/assistant",
                                                    }
                                                ]
                                            }
                                        ],
                                        "backendRefs": backend_refs,
                                        "timeouts": {"request": "600s"},
                                        "streamIdleTimeout": "0s",
                                        "modelsOwnedBy": "ml-team",
                                    }
                                ],
                                # Declaring the token costs is what makes the
                                # ext-proc ask a backend for usage on a streamed
                                # response, which otherwise reports none, and is
                                # where the metered counts in the access log
                                # come from.
                                "llmRequestCosts": [
                                    {"metadataKey": "llm_input_token", "type": "InputToken"},
                                    {"metadataKey": "llm_output_token", "type": "OutputToken"},
                                    {"metadataKey": "llm_total_token", "type": "TotalToken"},
                                ],
                            },
                        }
                    },
                },
            }
        ),
    )


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


# Every ModelRoute here sets timeouts other than the ModelService's defaults, so
# the AIGatewayRoute's timeouts can only have come from the ModelRoute. Every
# object composed onto the gateway's cluster lands in mp-ml-team-51733, the
# namespace mirroring the route's own. compose-inference-cluster composes that
# namespace, so it isn't among the composed resources.
#
# The cases where the route can't be composed compose nothing and say why. Their
# whole responses show that no subset of the route is applied.
#
# Secret data is base64 encoded, as the API server stores it: c2stMQ== is
# "sk-1", c2stdG9n "sk-tog" and c2stcHJvdmlkZXI= "sk-provider".
COMPOSE_CASES = [
    Case(
        name="GatewayNotReady",
        reason="A ModelRoute composes a new route during an InferenceGateway rollout and reports it not ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_route(
                    endpoints=[
                        v1alpha1.Endpoint(
                            name="d",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "d"}),
                        )
                    ]
                )
            ),
            required_resources={
                "gateway": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            tls=False, address="203.0.113.1", client_ca_published=True, gateway_ready=False
                        )
                    ]
                ),
                "clusters": fnv1.Resources(items=[_cluster(gateway_ca_published=True)]),
                "endpoints-d": fnv1.Resources(items=[_composed_endpoint(model=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_route(address="203.0.113.1", total_endpoints=1, ready_endpoints=1),
                resources={
                    "backend-self": _composed_endpoint_backend(),
                    "aibackend-self": _composed_endpoint_ai_backend(),
                    "cluster-ca-gw-eu": _cluster_ca(),
                    "client-certificate": _client_certificate(),
                    "route": _ai_gateway_route(
                        ready=fnv1.READY_UNSPECIFIED,
                        section_name="http",
                        backend_refs=[{"name": "assistant-eu-self-a42e6", "weight": 1, "priority": 0}],
                    ),
                },
            ),
            results=[fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for Envoy Deployment rollout")],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateway": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceGateway", match_name="eu"
                    ),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "endpoints-d": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "d"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForGateway",
                    message="Waiting for Envoy Deployment rollout",
                )
            ],
        ),
    ),
    Case(
        name="ClientCAUnpublished",
        reason="A ModelRoute whose gateway hasn't published its client CA composes nothing, because no backend could name its client certificate.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_route(
                    endpoints=[
                        v1alpha1.Endpoint(
                            name="d",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "d"}),
                        )
                    ]
                )
            ),
            required_resources={
                "gateway": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            tls=False, address="203.0.113.1", client_ca_published=False, gateway_ready=True
                        )
                    ]
                ),
                "clusters": fnv1.Resources(items=[_cluster(gateway_ca_published=True)]),
                "endpoints-d": fnv1.Resources(items=[_composed_endpoint(model=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=_desired_model_route(address=None, total_endpoints=0, ready_endpoints=0)),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL, message="InferenceGateway eu has not published its client CA"
                )
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateway": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceGateway", match_name="eu"
                    ),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "endpoints-d": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "d"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForGateway",
                    message="InferenceGateway eu has not published its client CA",
                )
            ],
        ),
    ),
    # The unready endpoint has the composed one's name and origin but no cluster
    # label. An unready endpoint is dropped before its cluster is read, so the
    # label would change nothing.
    Case(
        name="EndpointNotReady",
        reason="A ModelRoute whose only selected endpoint isn't ready composes nothing and reports NoReadyEndpoints.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_route(
                    endpoints=[
                        v1alpha1.Endpoint(
                            name="d",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "d"}),
                        )
                    ]
                )
            ),
            required_resources={
                "gateway": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            tls=False, address="203.0.113.1", client_ca_published=True, gateway_ready=True
                        )
                    ]
                ),
                "clusters": fnv1.Resources(items=[_cluster(gateway_ca_published=True)]),
                "endpoints-d": fnv1.Resources(
                    items=[
                        _third_party_endpoint(
                            name="self",
                            origin="https://gw-eu.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret=None,
                            ready=False,
                        )
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_route(address="203.0.113.1", total_endpoints=1, ready_endpoints=0)
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="None of the 1 selected ModelEndpoints is ready to carry traffic",
                )
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateway": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceGateway", match_name="eu"
                    ),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "endpoints-d": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "d"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="NoReadyEndpoints",
                    message="None of the 1 selected ModelEndpoints is ready to carry traffic",
                )
            ],
        ),
    ),
    Case(
        name="ClusterCAUnpublished",
        reason="A composed endpoint whose cluster hasn't published its gateway CA is left out of the route with a warning, leaving no ready endpoint.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_route(
                    endpoints=[
                        v1alpha1.Endpoint(
                            name="d",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "d"}),
                        )
                    ]
                )
            ),
            required_resources={
                "gateway": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            tls=False, address="203.0.113.1", client_ca_published=True, gateway_ready=True
                        )
                    ]
                ),
                "clusters": fnv1.Resources(items=[_cluster(gateway_ca_published=False)]),
                "endpoints-d": fnv1.Resources(items=[_composed_endpoint(model=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_route(address="203.0.113.1", total_endpoints=1, ready_endpoints=0)
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message="Endpoints left out of the route, their cluster has published no gateway CA: self",
                ),
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="None of the 1 selected ModelEndpoints is ready to carry traffic",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateway": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceGateway", match_name="eu"
                    ),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "endpoints-d": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "d"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="NoReadyEndpoints",
                    message="None of the 1 selected ModelEndpoints is ready to carry traffic",
                )
            ],
        ),
    ),
    Case(
        name="CredentialKeyMissing",
        reason="An endpoint whose credential Secret lacks the apiKey key is left out of the route with a warning, leaving no ready endpoint.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_route(
                    endpoints=[
                        v1alpha1.Endpoint(
                            name="a",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "a"}),
                        )
                    ]
                )
            ),
            required_resources={
                "gateway": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            tls=False, address="203.0.113.1", client_ca_published=True, gateway_ready=True
                        )
                    ]
                ),
                "clusters": fnv1.Resources(items=[_cluster(gateway_ca_published=True)]),
                "endpoints-a": fnv1.Resources(
                    items=[
                        _third_party_endpoint(
                            name="wrongkey",
                            origin="https://a.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret="k",
                            ready=True,
                        )
                    ]
                ),
                "credential-wrongkey": fnv1.Resources(items=[_api_key_secret(name="k", data={"token": "c2stMQ=="})]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_route(address="203.0.113.1", total_endpoints=1, ready_endpoints=0)
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message="Endpoints left out of the route, their credential Secret missing or missing its key: wrongkey",
                ),
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="None of the 1 selected ModelEndpoints is ready to carry traffic",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateway": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceGateway", match_name="eu"
                    ),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "endpoints-a": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "a"}),
                    ),
                    "credential-wrongkey": fnv1.ResourceSelector(
                        api_version="v1", kind="Secret", namespace="ml-team", match_name="k"
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="NoReadyEndpoints",
                    message="None of the 1 selected ModelEndpoints is ready to carry traffic",
                )
            ],
        ),
    ),
    Case(
        name="ComposedAndThirdParty",
        reason="A composed endpoint at priority 0 and a third-party provider at priority 1 get backends, a credential, a cluster CA, a client certificate and a route.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_route(
                    endpoints=[
                        v1alpha1.Endpoint(
                            name="d",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "d"}),
                            priority=0,
                        ),
                        v1alpha1.Endpoint(
                            name="together",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "together"}),
                            priority=1,
                        ),
                    ]
                )
            ),
            required_resources={
                "gateway": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            tls=False, address="203.0.113.1", client_ca_published=True, gateway_ready=True
                        )
                    ]
                ),
                "clusters": fnv1.Resources(items=[_cluster(gateway_ca_published=True)]),
                "endpoints-d": fnv1.Resources(items=[_composed_endpoint(model="d")]),
                "endpoints-together": fnv1.Resources(
                    items=[
                        _third_party_endpoint(
                            name="together",
                            origin="https://api.together.xyz",
                            model="Qwen/Qwen2.5",
                            schema="OpenAI",
                            api_key_secret="together-key",
                            ready=True,
                        )
                    ]
                ),
                "credential-together": fnv1.Resources(
                    items=[_api_key_secret(name="together-key", data={"apiKey": "c2stdG9n"})]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_route(address="203.0.113.1", total_endpoints=2, ready_endpoints=2),
                resources={
                    "backend-self": _composed_endpoint_backend(),
                    "aibackend-self": _composed_endpoint_ai_backend(),
                    "backend-together": _third_party_backend(
                        name="assistant-eu-together-20044", hostname="api.together.xyz"
                    ),
                    "aibackend-together": _third_party_ai_backend(name="assistant-eu-together-20044", schema="OpenAI"),
                    "credential-together": _credential(
                        name="assistant-eu-together-credential-fe51d", api_key="c2stdG9n"
                    ),
                    "credpolicy-together": _credential_policy(
                        name="assistant-eu-together-20044",
                        auth={
                            "type": "APIKey",
                            "apiKey": {"secretRef": {"name": "assistant-eu-together-credential-fe51d"}},
                        },
                    ),
                    "cluster-ca-gw-eu": _cluster_ca(),
                    "client-certificate": _client_certificate(),
                    "route": _ai_gateway_route(
                        ready=fnv1.READY_UNSPECIFIED,
                        section_name="http",
                        backend_refs=[
                            {"name": "assistant-eu-self-a42e6", "weight": 1, "priority": 0, "modelNameOverride": "d"},
                            {
                                "name": "assistant-eu-together-20044",
                                "weight": 1,
                                "priority": 1,
                                "modelNameOverride": "Qwen/Qwen2.5",
                            },
                        ],
                    ),
                },
            ),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for the route on gateway eu to be accepted")
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateway": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceGateway", match_name="eu"
                    ),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "endpoints-d": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "d"}),
                    ),
                    "endpoints-together": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "together"}),
                    ),
                    "credential-together": fnv1.ResourceSelector(
                        api_version="v1", kind="Secret", namespace="ml-team", match_name="together-key"
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRoute",
                    message="Waiting for the route on gateway eu to be accepted",
                )
            ],
        ),
    ),
    Case(
        name="GatewayWithoutTLS",
        reason="On a gateway without TLS, a ModelRoute binds its AIGatewayRoute to the HTTP listener.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_route(
                    endpoints=[
                        v1alpha1.Endpoint(
                            name="d",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "d"}),
                        )
                    ]
                )
            ),
            required_resources={
                "gateway": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            tls=False, address="203.0.113.1", client_ca_published=True, gateway_ready=True
                        )
                    ]
                ),
                "clusters": fnv1.Resources(items=[_cluster(gateway_ca_published=True)]),
                "endpoints-d": fnv1.Resources(items=[_composed_endpoint(model=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_route(address="203.0.113.1", total_endpoints=1, ready_endpoints=1),
                resources={
                    "backend-self": _composed_endpoint_backend(),
                    "aibackend-self": _composed_endpoint_ai_backend(),
                    "cluster-ca-gw-eu": _cluster_ca(),
                    "client-certificate": _client_certificate(),
                    "route": _ai_gateway_route(
                        ready=fnv1.READY_UNSPECIFIED,
                        section_name="http",
                        backend_refs=[{"name": "assistant-eu-self-a42e6", "weight": 1, "priority": 0}],
                    ),
                },
            ),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for the route on gateway eu to be accepted")
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateway": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceGateway", match_name="eu"
                    ),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "endpoints-d": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "d"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRoute",
                    message="Waiting for the route on gateway eu to be accepted",
                )
            ],
        ),
    ),
    Case(
        name="GatewayRollout",
        reason="With an existing route, an unready gateway keeps the route desired and reports it not ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_route(
                    endpoints=[
                        v1alpha1.Endpoint(
                            name="d",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "d"}),
                        )
                    ]
                ),
                resources={
                    "route": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "status": {
                                    "conditions": [
                                        {
                                            "type": "Ready",
                                            "status": "True",
                                            "reason": "Accepted",
                                            "lastTransitionTime": "2026-06-08T00:00:00Z",
                                        }
                                    ]
                                }
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    )
                },
            ),
            required_resources={
                "gateway": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            tls=False, address="203.0.113.1", client_ca_published=True, gateway_ready=False
                        )
                    ]
                ),
                "clusters": fnv1.Resources(items=[_cluster(gateway_ca_published=True)]),
                "endpoints-d": fnv1.Resources(items=[_composed_endpoint(model=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_route(address="203.0.113.1", total_endpoints=1, ready_endpoints=1),
                resources={
                    "backend-self": _composed_endpoint_backend(),
                    "aibackend-self": _composed_endpoint_ai_backend(),
                    "cluster-ca-gw-eu": _cluster_ca(),
                    "client-certificate": _client_certificate(),
                    "route": _ai_gateway_route(
                        ready=fnv1.READY_TRUE,
                        section_name="http",
                        backend_refs=[{"name": "assistant-eu-self-a42e6", "weight": 1, "priority": 0}],
                    ),
                },
            ),
            results=[fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for Envoy Deployment rollout")],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateway": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceGateway", match_name="eu"
                    ),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "endpoints-d": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "d"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForGateway",
                    message="Waiting for Envoy Deployment rollout",
                )
            ],
        ),
    ),
    # A TLS gateway serves inference on its HTTPS listener alone. Binding to :80
    # on a TLS gateway would carry credentials in the clear.
    Case(
        name="GatewayWithTLS",
        reason="On a TLS gateway, a ModelRoute binds its AIGatewayRoute to the HTTPS listener.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_route(
                    endpoints=[
                        v1alpha1.Endpoint(
                            name="d",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "d"}),
                        )
                    ]
                )
            ),
            required_resources={
                "gateway": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            tls=True, address="203.0.113.1", client_ca_published=True, gateway_ready=True
                        )
                    ]
                ),
                "clusters": fnv1.Resources(items=[_cluster(gateway_ca_published=True)]),
                "endpoints-d": fnv1.Resources(items=[_composed_endpoint(model=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_route(address="203.0.113.1", total_endpoints=1, ready_endpoints=1),
                resources={
                    "backend-self": _composed_endpoint_backend(),
                    "aibackend-self": _composed_endpoint_ai_backend(),
                    "cluster-ca-gw-eu": _cluster_ca(),
                    "client-certificate": _client_certificate(),
                    "route": _ai_gateway_route(
                        ready=fnv1.READY_UNSPECIFIED,
                        section_name="https",
                        backend_refs=[{"name": "assistant-eu-self-a42e6", "weight": 1, "priority": 0}],
                    ),
                },
            ),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for the route on gateway eu to be accepted")
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateway": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceGateway", match_name="eu"
                    ),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "endpoints-d": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "d"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRoute",
                    message="Waiting for the route on gateway eu to be accepted",
                )
            ],
        ),
    ),
    # This is GatewayWithoutTLS with the gateway at another address. Every other
    # case's gateway is at 203.0.113.1, so only this one shows the status takes
    # the gateway's address rather than a fixed one.
    Case(
        name="GatewayAddress",
        reason="A ModelRoute reports its gateway's address and its endpoint counts in its status.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_route(
                    endpoints=[
                        v1alpha1.Endpoint(
                            name="d",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "d"}),
                        )
                    ]
                )
            ),
            required_resources={
                "gateway": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            tls=False, address="203.0.113.9", client_ca_published=True, gateway_ready=True
                        )
                    ]
                ),
                "clusters": fnv1.Resources(items=[_cluster(gateway_ca_published=True)]),
                "endpoints-d": fnv1.Resources(items=[_composed_endpoint(model=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_route(address="203.0.113.9", total_endpoints=1, ready_endpoints=1),
                resources={
                    "backend-self": _composed_endpoint_backend(),
                    "aibackend-self": _composed_endpoint_ai_backend(),
                    "cluster-ca-gw-eu": _cluster_ca(),
                    "client-certificate": _client_certificate(),
                    "route": _ai_gateway_route(
                        ready=fnv1.READY_UNSPECIFIED,
                        section_name="http",
                        backend_refs=[{"name": "assistant-eu-self-a42e6", "weight": 1, "priority": 0}],
                    ),
                },
            ),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for the route on gateway eu to be accepted")
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateway": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceGateway", match_name="eu"
                    ),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "endpoints-d": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "d"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRoute",
                    message="Waiting for the route on gateway eu to be accepted",
                )
            ],
        ),
    ),
    # A canary entry and a catch-all entry must not both weight one endpoint. The
    # endpoint isn't Modelplane-composed, so no client certificate is issued.
    Case(
        name="EndpointMatchedTwice",
        reason="An endpoint two entries both select gets one backendRef, not one per entry.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_route(
                    endpoints=[
                        v1alpha1.Endpoint(
                            name="canary",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "kimi"}),
                            priority=0,
                        ),
                        v1alpha1.Endpoint(
                            name="catchall",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "kimi"}),
                            priority=1,
                        ),
                    ]
                )
            ),
            required_resources={
                "gateway": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            tls=False, address="203.0.113.1", client_ca_published=True, gateway_ready=True
                        )
                    ]
                ),
                "clusters": fnv1.Resources(items=[_cluster(gateway_ca_published=True)]),
                "endpoints-canary": fnv1.Resources(
                    items=[
                        _third_party_endpoint(
                            name="kimi-a",
                            origin="https://a.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret=None,
                            ready=True,
                        )
                    ]
                ),
                "endpoints-catchall": fnv1.Resources(
                    items=[
                        _third_party_endpoint(
                            name="kimi-a",
                            origin="https://a.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret=None,
                            ready=True,
                        )
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_route(address="203.0.113.1", total_endpoints=1, ready_endpoints=1),
                resources={
                    "backend-kimi-a": _third_party_backend(name="assistant-eu-kimi-a-bf6de", hostname="a.example.com"),
                    "aibackend-kimi-a": _third_party_ai_backend(name="assistant-eu-kimi-a-bf6de", schema="OpenAI"),
                    "route": _ai_gateway_route(
                        ready=fnv1.READY_UNSPECIFIED,
                        section_name="http",
                        backend_refs=[{"name": "assistant-eu-kimi-a-bf6de", "weight": 1, "priority": 0}],
                    ),
                },
            ),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for the route on gateway eu to be accepted")
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateway": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceGateway", match_name="eu"
                    ),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "endpoints-canary": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "kimi"}),
                    ),
                    "endpoints-catchall": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "kimi"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRoute",
                    message="Waiting for the route on gateway eu to be accepted",
                )
            ],
        ),
    ),
    # A ModelService's priorities are an ordering; Envoy's are levels it walks
    # from 0. A user writing 0 and 5, or a tier gone unready during a roll, would
    # otherwise leave gaps in what Envoy gets.
    Case(
        name="PriorityGaps",
        reason="Entries at priorities 0, 5 and 9, the middle one with no ready endpoint, compose backendRefs at priorities 0 and 1.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_route(
                    endpoints=[
                        v1alpha1.Endpoint(
                            name="a",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "a"}),
                            priority=0,
                        ),
                        v1alpha1.Endpoint(
                            name="b",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "b"}),
                            priority=5,
                        ),
                        v1alpha1.Endpoint(
                            name="c",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "c"}),
                            priority=9,
                        ),
                    ]
                )
            ),
            required_resources={
                "gateway": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            tls=False, address="203.0.113.1", client_ca_published=True, gateway_ready=True
                        )
                    ]
                ),
                "clusters": fnv1.Resources(items=[_cluster(gateway_ca_published=True)]),
                "endpoints-a": fnv1.Resources(
                    items=[
                        _third_party_endpoint(
                            name="a-0",
                            origin="https://a.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret=None,
                            ready=True,
                        )
                    ]
                ),
                "endpoints-b": fnv1.Resources(
                    items=[
                        _third_party_endpoint(
                            name="b-0",
                            origin="https://b.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret=None,
                            ready=False,
                        )
                    ]
                ),
                "endpoints-c": fnv1.Resources(
                    items=[
                        _third_party_endpoint(
                            name="c-0",
                            origin="https://c.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret=None,
                            ready=True,
                        )
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_route(address="203.0.113.1", total_endpoints=3, ready_endpoints=2),
                resources={
                    "backend-a-0": _third_party_backend(name="assistant-eu-a-0-b66b8", hostname="a.example.com"),
                    "aibackend-a-0": _third_party_ai_backend(name="assistant-eu-a-0-b66b8", schema="OpenAI"),
                    "backend-c-0": _third_party_backend(name="assistant-eu-c-0-782db", hostname="c.example.com"),
                    "aibackend-c-0": _third_party_ai_backend(name="assistant-eu-c-0-782db", schema="OpenAI"),
                    "route": _ai_gateway_route(
                        ready=fnv1.READY_UNSPECIFIED,
                        section_name="http",
                        backend_refs=[
                            {"name": "assistant-eu-a-0-b66b8", "weight": 1, "priority": 0},
                            {"name": "assistant-eu-c-0-782db", "weight": 1, "priority": 1},
                        ],
                    ),
                },
            ),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for the route on gateway eu to be accepted")
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateway": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceGateway", match_name="eu"
                    ),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "endpoints-a": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "a"}),
                    ),
                    "endpoints-b": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "b"}),
                    ),
                    "endpoints-c": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "c"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRoute",
                    message="Waiting for the route on gateway eu to be accepted",
                )
            ],
        ),
    ),
    # An entry's weight is written once but applied per backend, so it spreads
    # over the endpoints it matched while the ratio between entries survives: 90
    # over three is 30 each, 10 over one is 10, reduced by the gcd to the
    # smallest equivalent integers.
    Case(
        name="WeightSpread",
        reason="Entries weighted 90 over three endpoints and 10 over one compose backendRefs weighted 3, 3, 3 and 1.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_route(
                    endpoints=[
                        v1alpha1.Endpoint(
                            name="big",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "big"}),
                            weight=90,
                        ),
                        v1alpha1.Endpoint(
                            name="small",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "small"}),
                            weight=10,
                        ),
                    ]
                )
            ),
            required_resources={
                "gateway": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            tls=False, address="203.0.113.1", client_ca_published=True, gateway_ready=True
                        )
                    ]
                ),
                "clusters": fnv1.Resources(items=[_cluster(gateway_ca_published=True)]),
                "endpoints-big": fnv1.Resources(
                    items=[
                        _third_party_endpoint(
                            name="big-0",
                            origin="https://big-0.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret=None,
                            ready=True,
                        ),
                        _third_party_endpoint(
                            name="big-1",
                            origin="https://big-1.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret=None,
                            ready=True,
                        ),
                        _third_party_endpoint(
                            name="big-2",
                            origin="https://big-2.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret=None,
                            ready=True,
                        ),
                    ]
                ),
                "endpoints-small": fnv1.Resources(
                    items=[
                        _third_party_endpoint(
                            name="small-0",
                            origin="https://small-0.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret=None,
                            ready=True,
                        )
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_route(address="203.0.113.1", total_endpoints=4, ready_endpoints=4),
                resources={
                    "backend-big-0": _third_party_backend(
                        name="assistant-eu-big-0-1e944", hostname="big-0.example.com"
                    ),
                    "aibackend-big-0": _third_party_ai_backend(name="assistant-eu-big-0-1e944", schema="OpenAI"),
                    "backend-big-1": _third_party_backend(
                        name="assistant-eu-big-1-91055", hostname="big-1.example.com"
                    ),
                    "aibackend-big-1": _third_party_ai_backend(name="assistant-eu-big-1-91055", schema="OpenAI"),
                    "backend-big-2": _third_party_backend(
                        name="assistant-eu-big-2-5c954", hostname="big-2.example.com"
                    ),
                    "aibackend-big-2": _third_party_ai_backend(name="assistant-eu-big-2-5c954", schema="OpenAI"),
                    "backend-small-0": _third_party_backend(
                        name="assistant-eu-small-0-60d20", hostname="small-0.example.com"
                    ),
                    "aibackend-small-0": _third_party_ai_backend(name="assistant-eu-small-0-60d20", schema="OpenAI"),
                    "route": _ai_gateway_route(
                        ready=fnv1.READY_UNSPECIFIED,
                        section_name="http",
                        backend_refs=[
                            {"name": "assistant-eu-big-0-1e944", "weight": 3, "priority": 0},
                            {"name": "assistant-eu-big-1-91055", "weight": 3, "priority": 0},
                            {"name": "assistant-eu-big-2-5c954", "weight": 3, "priority": 0},
                            {"name": "assistant-eu-small-0-60d20", "weight": 1, "priority": 0},
                        ],
                    ),
                },
            ),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for the route on gateway eu to be accepted")
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateway": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceGateway", match_name="eu"
                    ),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "endpoints-big": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "big"}),
                    ),
                    "endpoints-small": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "small"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRoute",
                    message="Waiting for the route on gateway eu to be accepted",
                )
            ],
        ),
    ),
    # Weight 1 over five endpoints must floor none of them to 0, which would drop
    # them from the load assignment rather than share.
    Case(
        name="WeightBelowCount",
        reason="An entry weighted 1 over five endpoints composes a backendRef weighted 1 for each of them.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_route(
                    endpoints=[
                        v1alpha1.Endpoint(
                            name="many",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "many"}),
                            weight=1,
                        )
                    ]
                )
            ),
            required_resources={
                "gateway": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            tls=False, address="203.0.113.1", client_ca_published=True, gateway_ready=True
                        )
                    ]
                ),
                "clusters": fnv1.Resources(items=[_cluster(gateway_ca_published=True)]),
                "endpoints-many": fnv1.Resources(
                    items=[
                        _third_party_endpoint(
                            name="many-0",
                            origin="https://many-0.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret=None,
                            ready=True,
                        ),
                        _third_party_endpoint(
                            name="many-1",
                            origin="https://many-1.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret=None,
                            ready=True,
                        ),
                        _third_party_endpoint(
                            name="many-2",
                            origin="https://many-2.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret=None,
                            ready=True,
                        ),
                        _third_party_endpoint(
                            name="many-3",
                            origin="https://many-3.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret=None,
                            ready=True,
                        ),
                        _third_party_endpoint(
                            name="many-4",
                            origin="https://many-4.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret=None,
                            ready=True,
                        ),
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_route(address="203.0.113.1", total_endpoints=5, ready_endpoints=5),
                resources={
                    "backend-many-0": _third_party_backend(
                        name="assistant-eu-many-0-e4e60", hostname="many-0.example.com"
                    ),
                    "aibackend-many-0": _third_party_ai_backend(name="assistant-eu-many-0-e4e60", schema="OpenAI"),
                    "backend-many-1": _third_party_backend(
                        name="assistant-eu-many-1-b8a15", hostname="many-1.example.com"
                    ),
                    "aibackend-many-1": _third_party_ai_backend(name="assistant-eu-many-1-b8a15", schema="OpenAI"),
                    "backend-many-2": _third_party_backend(
                        name="assistant-eu-many-2-a7a3b", hostname="many-2.example.com"
                    ),
                    "aibackend-many-2": _third_party_ai_backend(name="assistant-eu-many-2-a7a3b", schema="OpenAI"),
                    "backend-many-3": _third_party_backend(
                        name="assistant-eu-many-3-db5a5", hostname="many-3.example.com"
                    ),
                    "aibackend-many-3": _third_party_ai_backend(name="assistant-eu-many-3-db5a5", schema="OpenAI"),
                    "backend-many-4": _third_party_backend(
                        name="assistant-eu-many-4-bd6f9", hostname="many-4.example.com"
                    ),
                    "aibackend-many-4": _third_party_ai_backend(name="assistant-eu-many-4-bd6f9", schema="OpenAI"),
                    "route": _ai_gateway_route(
                        ready=fnv1.READY_UNSPECIFIED,
                        section_name="http",
                        backend_refs=[
                            {"name": "assistant-eu-many-0-e4e60", "weight": 1, "priority": 0},
                            {"name": "assistant-eu-many-1-b8a15", "weight": 1, "priority": 0},
                            {"name": "assistant-eu-many-2-a7a3b", "weight": 1, "priority": 0},
                            {"name": "assistant-eu-many-3-db5a5", "weight": 1, "priority": 0},
                            {"name": "assistant-eu-many-4-bd6f9", "weight": 1, "priority": 0},
                        ],
                    ),
                },
            ),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for the route on gateway eu to be accepted")
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateway": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceGateway", match_name="eu"
                    ),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "endpoints-many": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "many"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRoute",
                    message="Waiting for the route on gateway eu to be accepted",
                )
            ],
        ),
    ),
    # A max-weight entry beside a tiny one spread over two endpoints scales past
    # the per-backendRef limit even though every weight is in bounds, so it
    # rescales to the limit rather than composing a route the API server rejects.
    Case(
        name="ExtremeRatio",
        reason="Entries weighted 1000000 over one endpoint and 1 over two compose backendRefs clamped to the weight limit: 1000000, 1 and 1.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_route(
                    endpoints=[
                        v1alpha1.Endpoint(
                            name="big",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "big"}),
                            priority=0,
                            weight=1000000,
                        ),
                        v1alpha1.Endpoint(
                            name="small",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "small"}),
                            priority=0,
                            weight=1,
                        ),
                    ]
                )
            ),
            required_resources={
                "gateway": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            tls=False, address="203.0.113.1", client_ca_published=True, gateway_ready=True
                        )
                    ]
                ),
                "clusters": fnv1.Resources(items=[_cluster(gateway_ca_published=True)]),
                "endpoints-big": fnv1.Resources(
                    items=[
                        _third_party_endpoint(
                            name="big-0",
                            origin="https://big-0.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret=None,
                            ready=True,
                        )
                    ]
                ),
                "endpoints-small": fnv1.Resources(
                    items=[
                        _third_party_endpoint(
                            name="small-0",
                            origin="https://small-0.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret=None,
                            ready=True,
                        ),
                        _third_party_endpoint(
                            name="small-1",
                            origin="https://small-1.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret=None,
                            ready=True,
                        ),
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_route(address="203.0.113.1", total_endpoints=3, ready_endpoints=3),
                resources={
                    "backend-big-0": _third_party_backend(
                        name="assistant-eu-big-0-1e944", hostname="big-0.example.com"
                    ),
                    "aibackend-big-0": _third_party_ai_backend(name="assistant-eu-big-0-1e944", schema="OpenAI"),
                    "backend-small-0": _third_party_backend(
                        name="assistant-eu-small-0-60d20", hostname="small-0.example.com"
                    ),
                    "aibackend-small-0": _third_party_ai_backend(name="assistant-eu-small-0-60d20", schema="OpenAI"),
                    "backend-small-1": _third_party_backend(
                        name="assistant-eu-small-1-cad94", hostname="small-1.example.com"
                    ),
                    "aibackend-small-1": _third_party_ai_backend(name="assistant-eu-small-1-cad94", schema="OpenAI"),
                    "route": _ai_gateway_route(
                        ready=fnv1.READY_UNSPECIFIED,
                        section_name="http",
                        backend_refs=[
                            {"name": "assistant-eu-big-0-1e944", "weight": 1000000, "priority": 0},
                            {"name": "assistant-eu-small-0-60d20", "weight": 1, "priority": 0},
                            {"name": "assistant-eu-small-1-cad94", "weight": 1, "priority": 0},
                        ],
                    ),
                },
            ),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for the route on gateway eu to be accepted")
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateway": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceGateway", match_name="eu"
                    ),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "endpoints-big": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "big"}),
                    ),
                    "endpoints-small": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "small"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRoute",
                    message="Waiting for the route on gateway eu to be accepted",
                )
            ],
        ),
    ),
    # The remainder is handed to the first endpoints of a tier, so the order must
    # be the endpoints' names rather than the API server's unspecified list order,
    # or the composed weights churn.
    Case(
        name="UnsortedEndpoints",
        reason="Endpoints listed as z, a and m compose backendRefs in name order: a, m, z.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_route(
                    endpoints=[
                        v1alpha1.Endpoint(
                            name="d",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "d"}),
                        )
                    ]
                )
            ),
            required_resources={
                "gateway": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            tls=False, address="203.0.113.1", client_ca_published=True, gateway_ready=True
                        )
                    ]
                ),
                "clusters": fnv1.Resources(items=[_cluster(gateway_ca_published=True)]),
                "endpoints-d": fnv1.Resources(
                    items=[
                        _third_party_endpoint(
                            name="z",
                            origin="https://z.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret=None,
                            ready=True,
                        ),
                        _third_party_endpoint(
                            name="a",
                            origin="https://a.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret=None,
                            ready=True,
                        ),
                        _third_party_endpoint(
                            name="m",
                            origin="https://m.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret=None,
                            ready=True,
                        ),
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_route(address="203.0.113.1", total_endpoints=3, ready_endpoints=3),
                resources={
                    "backend-a": _third_party_backend(name="assistant-eu-a-18a56", hostname="a.example.com"),
                    "aibackend-a": _third_party_ai_backend(name="assistant-eu-a-18a56", schema="OpenAI"),
                    "backend-m": _third_party_backend(name="assistant-eu-m-4d1e4", hostname="m.example.com"),
                    "aibackend-m": _third_party_ai_backend(name="assistant-eu-m-4d1e4", schema="OpenAI"),
                    "backend-z": _third_party_backend(name="assistant-eu-z-e5d6f", hostname="z.example.com"),
                    "aibackend-z": _third_party_ai_backend(name="assistant-eu-z-e5d6f", schema="OpenAI"),
                    "route": _ai_gateway_route(
                        ready=fnv1.READY_UNSPECIFIED,
                        section_name="http",
                        backend_refs=[
                            {"name": "assistant-eu-a-18a56", "weight": 1, "priority": 0},
                            {"name": "assistant-eu-m-4d1e4", "weight": 1, "priority": 0},
                            {"name": "assistant-eu-z-e5d6f", "weight": 1, "priority": 0},
                        ],
                    ),
                },
            ),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for the route on gateway eu to be accepted")
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateway": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceGateway", match_name="eu"
                    ),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "endpoints-d": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "d"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRoute",
                    message="Waiting for the route on gateway eu to be accepted",
                )
            ],
        ),
    ),
    Case(
        name="OpenAIAPIKey",
        reason="A third-party endpoint speaking OpenAI's API gets a credential policy that sends its key as a bearer token.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_route(
                    endpoints=[
                        v1alpha1.Endpoint(
                            name="d",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "d"}),
                        )
                    ]
                )
            ),
            required_resources={
                "gateway": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            tls=False, address="203.0.113.1", client_ca_published=True, gateway_ready=True
                        )
                    ]
                ),
                "clusters": fnv1.Resources(items=[_cluster(gateway_ca_published=True)]),
                "endpoints-d": fnv1.Resources(
                    items=[
                        _third_party_endpoint(
                            name="provider",
                            origin="https://api.example.com",
                            model=None,
                            schema="OpenAI",
                            api_key_secret="provider-key",
                            ready=True,
                        )
                    ]
                ),
                "credential-provider": fnv1.Resources(
                    items=[_api_key_secret(name="provider-key", data={"apiKey": "c2stcHJvdmlkZXI="})]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_route(address="203.0.113.1", total_endpoints=1, ready_endpoints=1),
                resources={
                    "backend-provider": _third_party_backend(
                        name="assistant-eu-provider-348cb", hostname="api.example.com"
                    ),
                    "aibackend-provider": _third_party_ai_backend(name="assistant-eu-provider-348cb", schema="OpenAI"),
                    "credential-provider": _credential(
                        name="assistant-eu-provider-credential-4d66b", api_key="c2stcHJvdmlkZXI="
                    ),
                    "credpolicy-provider": _credential_policy(
                        name="assistant-eu-provider-348cb",
                        auth={
                            "type": "APIKey",
                            "apiKey": {"secretRef": {"name": "assistant-eu-provider-credential-4d66b"}},
                        },
                    ),
                    "route": _ai_gateway_route(
                        ready=fnv1.READY_UNSPECIFIED,
                        section_name="http",
                        backend_refs=[{"name": "assistant-eu-provider-348cb", "weight": 1, "priority": 0}],
                    ),
                },
            ),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for the route on gateway eu to be accepted")
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateway": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceGateway", match_name="eu"
                    ),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "endpoints-d": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "d"}),
                    ),
                    "credential-provider": fnv1.ResourceSelector(
                        api_version="v1", kind="Secret", namespace="ml-team", match_name="provider-key"
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRoute",
                    message="Waiting for the route on gateway eu to be accepted",
                )
            ],
        ),
    ),
    Case(
        name="AnthropicAPIKey",
        reason="A third-party endpoint speaking Anthropic's API gets a credential policy that sends its key in x-api-key.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_route(
                    endpoints=[
                        v1alpha1.Endpoint(
                            name="d",
                            selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": "d"}),
                        )
                    ]
                )
            ),
            required_resources={
                "gateway": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            tls=False, address="203.0.113.1", client_ca_published=True, gateway_ready=True
                        )
                    ]
                ),
                "clusters": fnv1.Resources(items=[_cluster(gateway_ca_published=True)]),
                "endpoints-d": fnv1.Resources(
                    items=[
                        _third_party_endpoint(
                            name="provider",
                            origin="https://api.example.com",
                            model=None,
                            schema="Anthropic",
                            api_key_secret="provider-key",
                            ready=True,
                        )
                    ]
                ),
                "credential-provider": fnv1.Resources(
                    items=[_api_key_secret(name="provider-key", data={"apiKey": "c2stcHJvdmlkZXI="})]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_model_route(address="203.0.113.1", total_endpoints=1, ready_endpoints=1),
                resources={
                    "backend-provider": _third_party_backend(
                        name="assistant-eu-provider-348cb", hostname="api.example.com"
                    ),
                    "aibackend-provider": _third_party_ai_backend(
                        name="assistant-eu-provider-348cb", schema="Anthropic"
                    ),
                    "credential-provider": _credential(
                        name="assistant-eu-provider-credential-4d66b", api_key="c2stcHJvdmlkZXI="
                    ),
                    "credpolicy-provider": _credential_policy(
                        name="assistant-eu-provider-348cb",
                        auth={
                            "type": "AnthropicAPIKey",
                            "anthropicAPIKey": {"secretRef": {"name": "assistant-eu-provider-credential-4d66b"}},
                        },
                    ),
                    "route": _ai_gateway_route(
                        ready=fnv1.READY_UNSPECIFIED,
                        section_name="http",
                        backend_refs=[{"name": "assistant-eu-provider-348cb", "weight": 1, "priority": 0}],
                    ),
                },
            ),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for the route on gateway eu to be accepted")
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateway": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceGateway", match_name="eu"
                    ),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "endpoints-d": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelEndpoint",
                        namespace="ml-team",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "d"}),
                    ),
                    "credential-provider": fnv1.ResourceSelector(
                        api_version="v1", kind="Secret", namespace="ml-team", match_name="provider-key"
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="RoutingReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRoute",
                    message="Waiting for the route on gateway eu to be accepted",
                )
            ],
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction composes a ModelRoute's backends and AIGatewayRoute, or composes nothing and says why."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
