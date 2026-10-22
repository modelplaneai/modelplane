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

"""Tests for the compose-inference-gateway function."""

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
from models.ai.modelplane.inferencegateway import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-inference-gateway."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _xr(*, name: str, tls: bool, caller_secret_labels: dict[str, str] | None) -> fnv1.Resource:
    """The XR on gw-eu, with TLS from eu-tls-0 if tls, and API-key auth by Secrets matching caller_secret_labels unless None."""
    tls_spec = v1alpha1.Tls(certificateRefs=[v1alpha1.CertificateRef(name="eu-tls-0")]) if tls else None
    auth = None
    if caller_secret_labels is not None:
        auth = v1alpha1.Auth(
            method="APIKey",
            apiKey=v1alpha1.ApiKey(secretSelector=v1alpha1.SecretSelector(matchLabels=caller_secret_labels)),
        )
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            v1alpha1.InferenceGateway(
                apiVersion="modelplane.ai/v1alpha1",
                kind="InferenceGateway",
                metadata=metav1.ObjectMeta(name=name),
                spec=v1alpha1.Spec(clusterName="gw-eu", tls=tls_spec, auth=auth),
            ).model_dump(exclude_none=True, mode="json", by_alias=True)
        )
    )


def _desired_xr(*, status: dict | None, ready: fnv1.Ready) -> fnv1.Resource:
    """The desired XR, carrying status, or only its readiness if status is None."""
    if status is None:
        return fnv1.Resource(ready=ready)
    return fnv1.Resource(resource=resource.dict_to_struct({"status": status}), ready=ready)


def _cluster(*, provider_config_ref: bool) -> fnv1.Resource:
    """The gateway's InferenceCluster, gw-eu, as the clusters requirement returns it."""
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
                    }
                },
                "status": {"providerConfigRef": {"name": "gw-eu-cluster-kubeconfig"}} if provider_config_ref else {},
            }
        )
    )


def _inference_gateway(*, name: str, address: str | None) -> fnv1.Resource:
    """An InferenceGateway on gw-eu, as the gateways requirement returns it."""
    gateway: dict = {
        "apiVersion": "modelplane.ai/v1alpha1",
        "kind": "InferenceGateway",
        "metadata": {"name": name},
        "spec": {"clusterName": "gw-eu"},
    }
    if address is not None:
        gateway["status"] = {"address": address}
    return fnv1.Resource(resource=resource.dict_to_struct(gateway))


def _caller_key_secret(*, name: str, data: dict[str, str]) -> fnv1.Resource:
    """A caller-key Secret, as the caller-secrets requirement returns it."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": "modelplane-system"},
                "data": data,
            }
        )
    )


def _observed_gateway(*, address: str, ready: bool) -> fnv1.Resource:
    """The composed Gateway Object, observed once the Gateway has an address, with a Ready condition only if ready."""
    status: dict = {
        "atProvider": {
            "manifest": {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "Gateway",
                "metadata": {"name": "inference-gateway", "namespace": "modelplane-system"},
                "status": {"addresses": [{"type": "IPAddress", "value": address}]},
            }
        }
    }
    if ready:
        status["conditions"] = [
            {
                "type": "Ready",
                "status": "True",
                "reason": "Available",
                "lastTransitionTime": "2026-06-08T00:00:00Z",
            }
        ]
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "status": status,
            }
        )
    )


def _observed_caller_auth() -> fnv1.Resource:
    """The composed caller-auth Object, observed Ready once its policy is accepted."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
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
    )


def _observed_ai_gateway_anchor() -> fnv1.Resource:
    """The anchor AIGatewayRoute, observed once Envoy AI Gateway accepts it."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
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
    )


def _observed_envoy_deployment(*, ready: bool) -> fnv1.Resource:
    """The Envoy Deployment, observed ready only after its rollout completes."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "status": {
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "True" if ready else "False",
                            "reason": "Available" if ready else "Updating",
                            "lastTransitionTime": "2026-06-08T00:00:00Z",
                        }
                    ]
                },
            }
        )
    )


# The helpers below build the resources the function composes. Each is a
# provider-kubernetes Object that applies one manifest to the gateway's cluster
# through its ClusterProviderConfig, and every namespaced manifest lands in
# modelplane-system there.
#
# Each Object sets its own namespace too. An InferenceGateway is cluster-scoped,
# and Crossplane only defaults a composed namespaced resource's namespace from a
# namespaced composite. Without it every reconcile fails with "an empty
# namespace may not be set when a resource name is provided" and nothing is
# composed at all.


def _envoy_proxy() -> fnv1.Resource:
    """The composed EnvoyProxy, configuring the gateway's proxy pods and its usage-record access log."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-cluster-kubeconfig"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                            "kind": "EnvoyProxy",
                            "metadata": {"name": "inference-gateway", "namespace": "modelplane-system"},
                            "spec": {
                                # Two proxy pods spread softly across nodes and
                                # zones, a disruption budget so a drain can't
                                # evict both, and ndots:1. Without ndots:1 every
                                # backend hostname is resolved against each of
                                # the pod's search domains first, since they all
                                # have fewer than five dots. A cluster whose
                                # upstream resolver is slow then stalls
                                # resolution, and Envoy answers 503 with nothing
                                # but DNS timeouts to show for it.
                                "provider": {
                                    "type": "Kubernetes",
                                    "kubernetes": {
                                        "envoyService": {"externalTrafficPolicy": "Cluster"},
                                        "envoyDeployment": {
                                            "replicas": 2,
                                            "patch": {
                                                "type": "StrategicMerge",
                                                "value": {
                                                    "spec": {
                                                        "template": {
                                                            "spec": {
                                                                "dnsConfig": {
                                                                    "options": [{"name": "ndots", "value": "1"}]
                                                                }
                                                            }
                                                        }
                                                    }
                                                },
                                            },
                                            "pod": {
                                                "topologySpreadConstraints": [
                                                    {
                                                        "maxSkew": 1,
                                                        "topologyKey": "kubernetes.io/hostname",
                                                        "whenUnsatisfiable": "ScheduleAnyway",
                                                        "labelSelector": {
                                                            "matchLabels": {
                                                                "gateway.envoyproxy.io/owning-gateway-name": "inference-gateway",
                                                                "gateway.envoyproxy.io/owning-gateway-namespace": "modelplane-system",
                                                            }
                                                        },
                                                    },
                                                    {
                                                        "maxSkew": 1,
                                                        "topologyKey": "topology.kubernetes.io/zone",
                                                        "whenUnsatisfiable": "ScheduleAnyway",
                                                        "labelSelector": {
                                                            "matchLabels": {
                                                                "gateway.envoyproxy.io/owning-gateway-name": "inference-gateway",
                                                                "gateway.envoyproxy.io/owning-gateway-namespace": "modelplane-system",
                                                            }
                                                        },
                                                    },
                                                ]
                                            },
                                        },
                                        "envoyPDB": {"maxUnavailable": 1},
                                    },
                                },
                                # A stopping pod drains for as long as a request
                                # may run by default, so a restart doesn't cut
                                # off streams in flight.
                                "shutdown": {"drainTimeout": "300s"},
                                "telemetry": {
                                    "accessLog": {
                                        "settings": [
                                            {
                                                "format": {
                                                    "type": "JSON",
                                                    # The caller and token fields
                                                    # read request metadata, not
                                                    # the response body or a
                                                    # header. The caller header
                                                    # is stripped before a
                                                    # third-party backend sees
                                                    # it, so a log reading the
                                                    # header loses the caller on
                                                    # exactly the records that
                                                    # attribute provider spend.
                                                    "json": {
                                                        "caller": "%DYNAMIC_METADATA(io.envoy.ai_gateway:caller)%",
                                                        "service": "%REQ(X-AI-EG-MODEL)%",
                                                        "endpoint": "%DYNAMIC_METADATA(io.envoy.ai_gateway:ai_service_backend_name)%",
                                                        "served_model": "%DYNAMIC_METADATA(io.envoy.ai_gateway:model_name_override)%",
                                                        "response_model": "%DYNAMIC_METADATA(io.envoy.ai_gateway:response_model)%",
                                                        "input_tokens": "%DYNAMIC_METADATA(io.envoy.ai_gateway:llm_input_token)%",
                                                        "output_tokens": "%DYNAMIC_METADATA(io.envoy.ai_gateway:llm_output_token)%",
                                                        "total_tokens": "%DYNAMIC_METADATA(io.envoy.ai_gateway:llm_total_token)%",
                                                        "status": "%RESPONSE_CODE%",
                                                        "duration_ms": "%DURATION%",
                                                        "start_time": "%START_TIME%",
                                                    },
                                                },
                                                "sinks": [{"type": "File", "file": {"path": "/dev/stdout"}}],
                                            }
                                        ]
                                    }
                                },
                            },
                        }
                    },
                },
            }
        )
    )


def _envoy_deployment() -> fnv1.Resource:
    """The read-only Object that waits for Envoy's Deployment rollout."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-cluster-kubeconfig"},
                    "watch": True,
                    "readiness": {
                        "policy": "DeriveFromCelQuery",
                        "celQuery": (
                            "has(object.metadata) && has(object.metadata.generation) && "
                            "has(object.spec) && has(object.spec.replicas) && has(object.status) && "
                            "has(object.status.observedGeneration) && "
                            "object.status.observedGeneration >= object.metadata.generation && "
                            "has(object.status.updatedReplicas) && "
                            "object.status.updatedReplicas == object.spec.replicas && "
                            "has(object.status.replicas) && "
                            "object.status.replicas == object.spec.replicas && "
                            "has(object.status.readyReplicas) && "
                            "object.status.readyReplicas == object.spec.replicas && "
                            "has(object.status.availableReplicas) && "
                            "object.status.availableReplicas == object.spec.replicas && "
                            "has(object.status.conditions) && "
                            "object.status.conditions.exists(c, c.type == 'Available' && c.status == 'True')"
                        ),
                    },
                    "managementPolicies": ["Observe"],
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "apps/v1",
                            "kind": "Deployment",
                            "metadata": {
                                "name": "envoy-modelplane-system-inference-gateway-5dbfd6c0",
                                "namespace": "envoy-gateway-system",
                            },
                        }
                    },
                },
            }
        )
    )


def _ready_envoy_deployment() -> fnv1.Resource:
    """The desired Envoy Deployment after its observed rollout completes."""
    deployment = _envoy_deployment()
    deployment.ready = fnv1.READY_TRUE
    return deployment


def _gateway(*, https: bool, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed Gateway: an HTTP listener, joined by an HTTPS one terminating eu-tls-0 when https is set."""
    http_listener = {
        "name": "http",
        "protocol": "HTTP",
        "port": 80,
        "allowedRoutes": {
            "namespaces": {
                "from": "Selector",
                "selector": {"matchExpressions": [{"key": "modelplane.ai/namespace", "operator": "Exists"}]},
            }
        },
    }
    https_listener = {
        "name": "https",
        "protocol": "HTTPS",
        "port": 443,
        "tls": {"mode": "Terminate", "certificateRefs": [{"name": "eu-tls-0"}]},
        "allowedRoutes": {
            "namespaces": {
                "from": "Selector",
                "selector": {"matchExpressions": [{"key": "modelplane.ai/namespace", "operator": "Exists"}]},
            }
        },
    }
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-cluster-kubeconfig"},
                    "readiness": {
                        "policy": "DeriveFromCelQuery",
                        "celQuery": "has(object.status) && has(object.status.addresses) && object.status.addresses.size() > 0",
                    },
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "gateway.networking.k8s.io/v1",
                            "kind": "Gateway",
                            "metadata": {"name": "inference-gateway", "namespace": "modelplane-system"},
                            "spec": {
                                "gatewayClassName": "envoy",
                                "infrastructure": {
                                    "parametersRef": {
                                        "group": "gateway.envoyproxy.io",
                                        "kind": "EnvoyProxy",
                                        "name": "inference-gateway",
                                    }
                                },
                                "listeners": [http_listener, https_listener] if https else [http_listener],
                            },
                        }
                    },
                },
            }
        ),
        ready=ready,
    )


def _client_selfsigned_issuer() -> fnv1.Resource:
    """The composed self-signed Issuer that signs the client CA."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-cluster-kubeconfig"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "cert-manager.io/v1",
                            "kind": "Issuer",
                            "metadata": {"name": "inference-gateway-selfsigned", "namespace": "modelplane-system"},
                            "spec": {"selfSigned": {}},
                        }
                    },
                },
            }
        )
    )


def _client_ca_certificate(*, common_name: str) -> fnv1.Resource:
    """The composed Certificate for the client CA, whose client certificates a cluster gateway trusts."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-cluster-kubeconfig"},
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
                            "metadata": {"name": "inference-gateway-ca", "namespace": "modelplane-system"},
                            "spec": {
                                "isCA": True,
                                "commonName": common_name,
                                "secretName": "inference-gateway-ca",
                                "duration": "87600h",
                                "renewBefore": "8760h",
                                "privateKey": {"algorithm": "ECDSA", "size": 256},
                                "issuerRef": {
                                    "name": "inference-gateway-selfsigned",
                                    "kind": "Issuer",
                                    "group": "cert-manager.io",
                                },
                            },
                        }
                    },
                },
            }
        )
    )


def _client_ca_issuer() -> fnv1.Resource:
    """The composed ClusterIssuer, backed by the client CA, that compose-model-route issues client certificates from."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-cluster-kubeconfig"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "cert-manager.io/v1",
                            "kind": "ClusterIssuer",
                            "metadata": {"name": "inference-gateway-ca"},
                            "spec": {"ca": {"secretName": "inference-gateway-ca"}},
                        }
                    },
                },
            }
        )
    )


def _client_ca_bundle() -> fnv1.Resource:
    """The composed trust-manager Bundle copying the client CA's certificate, without its key, into a ConfigMap."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-cluster-kubeconfig"},
                    "readiness": {
                        "policy": "DeriveFromCelQuery",
                        "celQuery": (
                            "has(object.status) && has(object.status.conditions) && "
                            "object.status.conditions.exists(c, c.type == 'Synced' && c.status == 'True')"
                        ),
                    },
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "trust.cert-manager.io/v1alpha1",
                            "kind": "Bundle",
                            "metadata": {"name": "inference-gateway-ca"},
                            "spec": {
                                "sources": [{"secret": {"name": "inference-gateway-ca", "key": "ca.crt"}}],
                                "target": {
                                    "configMap": {"key": "ca.crt"},
                                    "namespaceSelector": {
                                        "matchLabels": {"kubernetes.io/metadata.name": "modelplane-system"}
                                    },
                                },
                            },
                        }
                    },
                },
            }
        )
    )


def _client_ca_configmap() -> fnv1.Resource:
    """The composed Object observing, without writing, the ConfigMap the client CA Bundle syncs."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-cluster-kubeconfig"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "managementPolicies": ["Observe"],
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "v1",
                            "kind": "ConfigMap",
                            "metadata": {"name": "inference-gateway-ca", "namespace": "modelplane-system"},
                        }
                    },
                },
            }
        )
    )


def _caller_secret(*, name: str, data: dict[str, str]) -> fnv1.Resource:
    """A composed copy of a caller-key Secret, with its data copied verbatim."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-cluster-kubeconfig"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "v1",
                            "kind": "Secret",
                            "metadata": {"name": name, "namespace": "modelplane-system"},
                            "type": "Opaque",
                            "data": data,
                        }
                    },
                },
            }
        )
    )


def _caller_auth(*, credential_refs: list[dict], ready: fnv1.Ready) -> fnv1.Resource:
    """The composed SecurityPolicy authenticating callers by API key, against the Secrets credential_refs names."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-cluster-kubeconfig"},
                    "readiness": {
                        "policy": "DeriveFromCelQuery",
                        "celQuery": (
                            "has(object.status) && has(object.status.ancestors) && "
                            "object.status.ancestors.exists(a, has(a.conditions) && "
                            "a.conditions.exists(c, c.type == 'Accepted' && c.status == 'True'))"
                        ),
                    },
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                            "kind": "SecurityPolicy",
                            "metadata": {"name": "inference-gateway-callers", "namespace": "modelplane-system"},
                            "spec": {
                                "targetRefs": [
                                    {
                                        "group": "gateway.networking.k8s.io",
                                        "kind": "Gateway",
                                        "name": "inference-gateway",
                                    }
                                ],
                                "apiKeyAuth": {
                                    "credentialRefs": credential_refs,
                                    # Authorization for OpenAI clients, x-api-key
                                    # for Anthropic ones.
                                    "extractFrom": [{"headers": ["Authorization", "x-api-key"]}],
                                    "forwardClientIDHeader": "x-modelplane-caller",
                                    "sanitize": True,
                                },
                            },
                        }
                    },
                },
            }
        ),
        ready=ready,
    )


def _failover_policy() -> fnv1.Resource:
    """The composed BackendTrafficPolicy that retries a failed request and ejects an endpoint that keeps failing."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-cluster-kubeconfig"},
                    "readiness": {
                        "policy": "DeriveFromCelQuery",
                        "celQuery": (
                            "has(object.status) && has(object.status.ancestors) && "
                            "object.status.ancestors.exists(a, has(a.conditions) && "
                            "a.conditions.exists(c, c.type == 'Accepted' && c.status == 'True'))"
                        ),
                    },
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                            "kind": "BackendTrafficPolicy",
                            "metadata": {"name": "inference-gateway-failover", "namespace": "modelplane-system"},
                            "spec": {
                                "targetRefs": [
                                    {
                                        "group": "gateway.networking.k8s.io",
                                        "kind": "Gateway",
                                        "name": "inference-gateway",
                                    }
                                ],
                                "retry": {
                                    # Two attempts per priority, so a retry tries
                                    # another endpoint at the same priority
                                    # before moving down. At one, a single
                                    # transient failure on one replica would
                                    # send the request to the next priority,
                                    # which may be a paid provider.
                                    "numAttemptsPerPriority": 2,
                                    "numRetries": 3,
                                    "retryOn": {
                                        # retriable-status-codes has to be
                                        # present for the status codes below to
                                        # do anything: Envoy Gateway replaces
                                        # retry_on wholesale with this list, and
                                        # Envoy only consults
                                        # retriable_status_codes when retry_on
                                        # names it. Without it a provider
                                        # answering 503 or 429 is never retried,
                                        # which is the case failover exists for.
                                        "triggers": [
                                            "connect-failure",
                                            "refused-stream",
                                            "reset",
                                            "retriable-status-codes",
                                        ],
                                        # 429 so a rate-limited provider's
                                        # traffic overflows to another endpoint
                                        # rather than failing back to the caller.
                                        "httpStatusCodes": [429, 503],
                                    },
                                },
                                # Panic mode defaults to 50%, above which Envoy
                                # ignores health and spreads traffic over every
                                # endpoint including the ejected ones. Every
                                # endpoint of a ModelService shares one cluster,
                                # so ejecting a whole priority tier usually
                                # crosses it and failover stops working.
                                # panicThreshold is a sibling of passive rather
                                # than a field inside it: nested wrongly, the API
                                # server prunes it while the policy still
                                # applies.
                                "healthCheck": {
                                    "passive": {
                                        "baseEjectionTime": "30s",
                                        "consecutive5XxErrors": 5,
                                        "interval": "5s",
                                        "maxEjectionPercent": 100,
                                    },
                                    "panicThreshold": 0,
                                },
                            },
                        }
                    },
                },
            }
        )
    )


def _client_traffic_policy() -> fnv1.Resource:
    """The composed ClientTrafficPolicy that lets a whole request or response body fit in the proxy's buffer."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-cluster-kubeconfig"},
                    "readiness": {
                        "policy": "DeriveFromCelQuery",
                        "celQuery": (
                            "has(object.status) && has(object.status.ancestors) && "
                            "object.status.ancestors.exists(a, has(a.conditions) && "
                            "a.conditions.exists(c, c.type == 'Accepted' && c.status == 'True'))"
                        ),
                    },
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                            "kind": "ClientTrafficPolicy",
                            "metadata": {"name": "inference-gateway-client-traffic", "namespace": "modelplane-system"},
                            "spec": {
                                "targetRefs": [
                                    {
                                        "group": "gateway.networking.k8s.io",
                                        "kind": "Gateway",
                                        "name": "inference-gateway",
                                    }
                                ],
                                "connection": {"bufferLimit": "50Mi"},
                                "http2": {"initialStreamWindowSize": "16Mi", "initialConnectionWindowSize": "24Mi"},
                            },
                        }
                    },
                },
            }
        )
    )


def _healthz_filter() -> fnv1.Resource:
    """The composed HTTPRouteFilter answering /healthz with a 200."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-cluster-kubeconfig"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                            "kind": "HTTPRouteFilter",
                            "metadata": {"name": "inference-gateway-healthz", "namespace": "modelplane-system"},
                            "spec": {
                                "directResponse": {
                                    "statusCode": 200,
                                    "contentType": "application/json",
                                    "body": {"type": "Inline", "inline": '{"status":"ok"}'},
                                }
                            },
                        }
                    },
                },
            }
        )
    )


def _healthz_route() -> fnv1.Resource:
    """The composed HTTPRoute serving /healthz on the HTTP listener, as an Exact match that outranks any redirect."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-cluster-kubeconfig"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "gateway.networking.k8s.io/v1",
                            "kind": "HTTPRoute",
                            "metadata": {"name": "inference-gateway-healthz", "namespace": "modelplane-system"},
                            "spec": {
                                "parentRefs": [
                                    {
                                        "group": "gateway.networking.k8s.io",
                                        "kind": "Gateway",
                                        "name": "inference-gateway",
                                        "sectionName": "http",
                                    }
                                ],
                                "rules": [
                                    {
                                        "matches": [{"path": {"type": "Exact", "value": "/healthz"}}],
                                        "filters": [
                                            {
                                                "type": "ExtensionRef",
                                                "extensionRef": {
                                                    "group": "gateway.envoyproxy.io",
                                                    "kind": "HTTPRouteFilter",
                                                    "name": "inference-gateway-healthz",
                                                },
                                            }
                                        ],
                                    }
                                ],
                            },
                        }
                    },
                },
            }
        )
    )


def _ai_gateway_anchor(*, https: bool, ready: fnv1.Ready) -> fnv1.Resource:
    """The AIGatewayRoute keeping AI Gateway's ext-proc installed on this Gateway."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-cluster-kubeconfig"},
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
                            "metadata": {"name": "inference-gateway-anchor", "namespace": "modelplane-system"},
                            "spec": {
                                "parentRefs": [
                                    {
                                        "group": "gateway.networking.k8s.io",
                                        "kind": "Gateway",
                                        "name": "inference-gateway",
                                        "namespace": "modelplane-system",
                                        "sectionName": "https" if https else "http",
                                    }
                                ],
                                "rules": [
                                    {
                                        "matches": [
                                            {
                                                "headers": [
                                                    {
                                                        "type": "Exact",
                                                        "name": "x-modelplane-gateway-anchor",
                                                        "value": "true",
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ],
                            },
                        }
                    },
                },
            }
        ),
        ready=ready,
    )


def _healthz_auth() -> fnv1.Resource:
    """The composed SecurityPolicy letting /healthz past caller auth, so a health check needs no credential."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-cluster-kubeconfig"},
                    "readiness": {
                        "policy": "DeriveFromCelQuery",
                        "celQuery": (
                            "has(object.status) && has(object.status.ancestors) && "
                            "object.status.ancestors.exists(a, has(a.conditions) && "
                            "a.conditions.exists(c, c.type == 'Accepted' && c.status == 'True'))"
                        ),
                    },
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                            "kind": "SecurityPolicy",
                            "metadata": {"name": "inference-gateway-healthz-open", "namespace": "modelplane-system"},
                            "spec": {
                                "targetRefs": [
                                    {
                                        "group": "gateway.networking.k8s.io",
                                        "kind": "HTTPRoute",
                                        "name": "inference-gateway-healthz",
                                    }
                                ],
                                "authorization": {"defaultAction": "Allow"},
                            },
                        }
                    },
                },
            }
        )
    )


def _redirect_route() -> fnv1.Resource:
    """The composed HTTPRoute redirecting everything on :80 but /healthz to HTTPS."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "gw-eu-cluster-kubeconfig"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "gateway.networking.k8s.io/v1",
                            "kind": "HTTPRoute",
                            "metadata": {"name": "inference-gateway-redirect", "namespace": "modelplane-system"},
                            "spec": {
                                "parentRefs": [
                                    {
                                        "group": "gateway.networking.k8s.io",
                                        "kind": "Gateway",
                                        "name": "inference-gateway",
                                        "sectionName": "http",
                                    }
                                ],
                                "rules": [
                                    {
                                        "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                                        "filters": [
                                            {
                                                "type": "RequestRedirect",
                                                "requestRedirect": {"scheme": "https", "statusCode": 301},
                                            }
                                        ],
                                    }
                                ],
                            },
                        }
                    },
                },
            }
        )
    )


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


# Secret data is base64 encoded, as the API server stores it: c2stMQ== is
# "sk-1", c2stMg== "sk-2", c2stbXAtYTFiMmMz "sk-mp-a1b2c3", Y2VydA== "cert" and
# a2V5 "key".
COMPOSE_CASES = [
    # Passes where the gateway can't be composed compose nothing, and say why.
    # The whole response shows nothing is composed against a cluster the
    # gateway can't reach or doesn't own, rather than a subset being applied.
    Case(
        name="RequirementsUnresolved",
        reason="Before its cluster and the other gateways resolve, a gateway composes nothing and waits for them.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_xr(name="eu", tls=False, caller_secret_labels=None)),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=_desired_xr(status=None, ready=fnv1.READY_FALSE)),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Waiting for the gateway's cluster and the other gateways to resolve",
                )
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="GatewayReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForCluster",
                    message="Waiting for the gateway's cluster and the other gateways to resolve",
                )
            ],
        ),
    ),
    Case(
        name="ClusterNotFound",
        reason="A gateway whose named InferenceCluster doesn't exist composes nothing and says so.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_xr(name="eu", tls=False, caller_secret_labels=None)),
            required_resources={
                "clusters": fnv1.Resources(),
                "gateways": fnv1.Resources(items=[_inference_gateway(name="eu", address=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=_desired_xr(status=None, ready=fnv1.READY_FALSE)),
            results=[fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="InferenceCluster gw-eu does not exist")],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="GatewayReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForCluster",
                    message="InferenceCluster gw-eu does not exist",
                )
            ],
        ),
    ),
    Case(
        name="LowerNamedGateway",
        reason=(
            "A gateway whose cluster a lower-named gateway also targets, with neither yet holding an address "
            "or a creation timestamp, composes nothing and reports the cluster taken."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_xr(name="eu", tls=False, caller_secret_labels=None)),
            required_resources={
                "clusters": fnv1.Resources(items=[_cluster(provider_config_ref=True)]),
                "gateways": fnv1.Resources(
                    items=[_inference_gateway(name="eu", address=None), _inference_gateway(name="aaa", address=None)]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=_desired_xr(status=None, ready=fnv1.READY_FALSE)),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL, message="InferenceCluster gw-eu already hosts InferenceGateway aaa"
                )
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="GatewayReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ClusterAlreadyHasGateway",
                    message="InferenceCluster gw-eu already hosts InferenceGateway aaa",
                )
            ],
        ),
    ),
    # Taking the cluster would delete the incumbent's Gateway and bring its load
    # balancer back on a different address.
    Case(
        name="IncumbentHasAddress",
        reason=(
            "A gateway named aaa composes nothing on a cluster where zzz already has an address, "
            "though aaa sorts first, so the incumbent keeps its cluster."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_xr(name="aaa", tls=False, caller_secret_labels=None)),
            required_resources={
                "clusters": fnv1.Resources(items=[_cluster(provider_config_ref=True)]),
                "gateways": fnv1.Resources(
                    items=[
                        _inference_gateway(name="aaa", address=None),
                        _inference_gateway(name="zzz", address="34.56.129.3"),
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=_desired_xr(status=None, ready=fnv1.READY_FALSE)),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL, message="InferenceCluster gw-eu already hosts InferenceGateway zzz"
                )
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="GatewayReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ClusterAlreadyHasGateway",
                    message="InferenceCluster gw-eu already hosts InferenceGateway zzz",
                )
            ],
        ),
    ),
    Case(
        name="NoProviderConfigRef",
        reason="A gateway whose cluster hasn't published a providerConfigRef yet composes nothing and waits for it.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_xr(name="eu", tls=False, caller_secret_labels=None)),
            required_resources={
                "clusters": fnv1.Resources(items=[_cluster(provider_config_ref=False)]),
                "gateways": fnv1.Resources(items=[_inference_gateway(name="eu", address=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=_desired_xr(status=None, ready=fnv1.READY_FALSE)),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="InferenceCluster gw-eu has not published a providerConfigRef",
                )
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="GatewayReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForCluster",
                    message="InferenceCluster gw-eu has not published a providerConfigRef",
                )
            ],
        ),
    ),
    # The getting-started shape.
    Case(
        name="NoTLSOrAuth",
        reason=(
            "With no TLS or auth, a gateway composes its Gateway objects and client PKI but no caller auth or "
            "redirect, and reports nothing in status until the Gateway has an address."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_xr(name="eu", tls=False, caller_secret_labels=None)),
            required_resources={
                "clusters": fnv1.Resources(items=[_cluster(provider_config_ref=True)]),
                "gateways": fnv1.Resources(items=[_inference_gateway(name="eu", address=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(status={}, ready=fnv1.READY_FALSE),
                resources={
                    "envoy-proxy": _envoy_proxy(),
                    "envoy-deployment": _envoy_deployment(),
                    "gateway": _gateway(https=False, ready=fnv1.READY_UNSPECIFIED),
                    "client-selfsigned-issuer": _client_selfsigned_issuer(),
                    "client-ca-certificate": _client_ca_certificate(common_name="Modelplane InferenceGateway CA eu"),
                    "client-ca-issuer": _client_ca_issuer(),
                    "client-ca-bundle": _client_ca_bundle(),
                    "client-ca-configmap": _client_ca_configmap(),
                    "failover-policy": _failover_policy(),
                    "client-traffic-policy": _client_traffic_policy(),
                    "healthz-filter": _healthz_filter(),
                    "healthz-route": _healthz_route(),
                    "ai-gateway-anchor": _ai_gateway_anchor(https=False, ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL, message="Waiting for the Gateway on cluster gw-eu to be programmed"
                )
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="GatewayReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForGateway",
                    message="Waiting for the Gateway on cluster gw-eu to be programmed",
                )
            ],
        ),
    ),
    # The URLs are what a caller can actually put in an SDK's base_url.
    Case(
        name="GatewayHasAddress",
        reason=(
            "A plain HTTP gateway whose Gateway has an address but isn't programmed publishes the address and the "
            "URLs built on it, and isn't Ready."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(name="eu", tls=False, caller_secret_labels=None),
                resources={"gateway": _observed_gateway(address="34.56.129.3", ready=False)},
            ),
            required_resources={
                "clusters": fnv1.Resources(items=[_cluster(provider_config_ref=True)]),
                "gateways": fnv1.Resources(items=[_inference_gateway(name="eu", address=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(
                    status={
                        "address": "34.56.129.3",
                        "endpoints": {
                            "openAI": "http://34.56.129.3/v1",
                            "anthropic": "http://34.56.129.3/anthropic/v1",
                        },
                    },
                    ready=fnv1.READY_FALSE,
                ),
                resources={
                    "envoy-proxy": _envoy_proxy(),
                    "envoy-deployment": _envoy_deployment(),
                    "gateway": _gateway(https=False, ready=fnv1.READY_UNSPECIFIED),
                    "client-selfsigned-issuer": _client_selfsigned_issuer(),
                    "client-ca-certificate": _client_ca_certificate(common_name="Modelplane InferenceGateway CA eu"),
                    "client-ca-issuer": _client_ca_issuer(),
                    "client-ca-bundle": _client_ca_bundle(),
                    "client-ca-configmap": _client_ca_configmap(),
                    "failover-policy": _failover_policy(),
                    "client-traffic-policy": _client_traffic_policy(),
                    "healthz-filter": _healthz_filter(),
                    "healthz-route": _healthz_route(),
                    "ai-gateway-anchor": _ai_gateway_anchor(https=False, ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL, message="Waiting for the Gateway on cluster gw-eu to be programmed"
                )
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="GatewayReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForGateway",
                    message="Waiting for the Gateway on cluster gw-eu to be programmed",
                )
            ],
        ),
    ),
    # A bare IPv6 literal collides with the port separator in a URL, so an SDK
    # given http://2001:db8::1/v1 as a base_url can't use it.
    Case(
        name="IPv6Address",
        reason="A Gateway with an IPv6 address has it bracketed in the URLs the gateway publishes.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(name="eu", tls=False, caller_secret_labels=None),
                resources={"gateway": _observed_gateway(address="2001:db8::1", ready=False)},
            ),
            required_resources={
                "clusters": fnv1.Resources(items=[_cluster(provider_config_ref=True)]),
                "gateways": fnv1.Resources(items=[_inference_gateway(name="eu", address=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(
                    status={
                        "address": "2001:db8::1",
                        "endpoints": {
                            "openAI": "http://[2001:db8::1]/v1",
                            "anthropic": "http://[2001:db8::1]/anthropic/v1",
                        },
                    },
                    ready=fnv1.READY_FALSE,
                ),
                resources={
                    "envoy-proxy": _envoy_proxy(),
                    "envoy-deployment": _envoy_deployment(),
                    "gateway": _gateway(https=False, ready=fnv1.READY_UNSPECIFIED),
                    "client-selfsigned-issuer": _client_selfsigned_issuer(),
                    "client-ca-certificate": _client_ca_certificate(common_name="Modelplane InferenceGateway CA eu"),
                    "client-ca-issuer": _client_ca_issuer(),
                    "client-ca-bundle": _client_ca_bundle(),
                    "client-ca-configmap": _client_ca_configmap(),
                    "failover-policy": _failover_policy(),
                    "client-traffic-policy": _client_traffic_policy(),
                    "healthz-filter": _healthz_filter(),
                    "healthz-route": _healthz_route(),
                    "ai-gateway-anchor": _ai_gateway_anchor(https=False, ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL, message="Waiting for the Gateway on cluster gw-eu to be programmed"
                )
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="GatewayReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForGateway",
                    message="Waiting for the Gateway on cluster gw-eu to be programmed",
                )
            ],
        ),
    ),
    # cert-manager's webhook rejects a commonName past X.509's 64-byte limit,
    # and a gateway name is a cluster-scoped resource name, so it can be up to
    # 253 characters.
    Case(
        name="LongGatewayName",
        reason="A gateway with a 63-character name has its client CA's commonName cut to 64 bytes.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    name="gaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    tls=False,
                    caller_secret_labels=None,
                )
            ),
            required_resources={
                "clusters": fnv1.Resources(items=[_cluster(provider_config_ref=True)]),
                "gateways": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            name="gaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", address=None
                        )
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(status={}, ready=fnv1.READY_FALSE),
                resources={
                    "envoy-proxy": _envoy_proxy(),
                    "envoy-deployment": _envoy_deployment(),
                    "gateway": _gateway(https=False, ready=fnv1.READY_UNSPECIFIED),
                    "client-selfsigned-issuer": _client_selfsigned_issuer(),
                    "client-ca-certificate": _client_ca_certificate(
                        common_name="Modelplane InferenceGateway CA gaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                    ),
                    "client-ca-issuer": _client_ca_issuer(),
                    "client-ca-bundle": _client_ca_bundle(),
                    "client-ca-configmap": _client_ca_configmap(),
                    "failover-policy": _failover_policy(),
                    "client-traffic-policy": _client_traffic_policy(),
                    "healthz-filter": _healthz_filter(),
                    "healthz-route": _healthz_route(),
                    "ai-gateway-anchor": _ai_gateway_anchor(https=False, ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL, message="Waiting for the Gateway on cluster gw-eu to be programmed"
                )
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="GatewayReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForGateway",
                    message="Waiting for the Gateway on cluster gw-eu to be programmed",
                )
            ],
        ),
    ),
    # A cluster only trusts this gateway once it has the CA, so nothing reaches
    # an engine before it appears.
    Case(
        name="ClientCAObserved",
        reason=(
            "A programmed gateway that observes trust-manager's ConfigMap publishes the client CA from it in "
            "status, as plain text."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(name="eu", tls=False, caller_secret_labels=None),
                resources={
                    "gateway": _observed_gateway(address="gw.example.org", ready=True),
                    "ai-gateway-anchor": _observed_ai_gateway_anchor(),
                    "envoy-deployment": _observed_envoy_deployment(ready=True),
                    "client-ca-configmap": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "status": {
                                    "atProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "ConfigMap",
                                            "data": {"ca.crt": "-----BEGIN CERTIFICATE-----\nclient\n"},
                                        }
                                    }
                                },
                            }
                        ),
                    ),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(items=[_cluster(provider_config_ref=True)]),
                "gateways": fnv1.Resources(items=[_inference_gateway(name="eu", address=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(
                    status={
                        "address": "gw.example.org",
                        "clientCACertificate": "-----BEGIN CERTIFICATE-----\nclient\n",
                        "endpoints": {
                            "openAI": "http://gw.example.org/v1",
                            "anthropic": "http://gw.example.org/anthropic/v1",
                        },
                    },
                    ready=fnv1.READY_UNSPECIFIED,
                ),
                resources={
                    "envoy-proxy": _envoy_proxy(),
                    "envoy-deployment": _ready_envoy_deployment(),
                    "gateway": _gateway(https=False, ready=fnv1.READY_TRUE),
                    "client-selfsigned-issuer": _client_selfsigned_issuer(),
                    "client-ca-certificate": _client_ca_certificate(common_name="Modelplane InferenceGateway CA eu"),
                    "client-ca-issuer": _client_ca_issuer(),
                    "client-ca-bundle": _client_ca_bundle(),
                    "client-ca-configmap": _client_ca_configmap(),
                    "failover-policy": _failover_policy(),
                    "client-traffic-policy": _client_traffic_policy(),
                    "healthz-filter": _healthz_filter(),
                    "healthz-route": _healthz_route(),
                    "ai-gateway-anchor": _ai_gateway_anchor(https=False, ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                }
            ),
            conditions=[
                fnv1.Condition(type="GatewayReady", status=fnv1.STATUS_CONDITION_TRUE, reason="GatewayProgrammed"),
            ],
        ),
    ),
    # With no CA published no cluster trusts the gateway yet, so no cluster
    # publishes a hostname on its account.
    Case(
        name="ClientCANotObserved",
        reason="A programmed gateway that hasn't observed trust-manager's ConfigMap publishes no client CA.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(name="eu", tls=False, caller_secret_labels=None),
                resources={
                    "gateway": _observed_gateway(address="gw.example.org", ready=True),
                    "ai-gateway-anchor": _observed_ai_gateway_anchor(),
                    "envoy-deployment": _observed_envoy_deployment(ready=True),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(items=[_cluster(provider_config_ref=True)]),
                "gateways": fnv1.Resources(items=[_inference_gateway(name="eu", address=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(
                    status={
                        "address": "gw.example.org",
                        "endpoints": {
                            "openAI": "http://gw.example.org/v1",
                            "anthropic": "http://gw.example.org/anthropic/v1",
                        },
                    },
                    ready=fnv1.READY_UNSPECIFIED,
                ),
                resources={
                    "envoy-proxy": _envoy_proxy(),
                    "envoy-deployment": _ready_envoy_deployment(),
                    "gateway": _gateway(https=False, ready=fnv1.READY_TRUE),
                    "client-selfsigned-issuer": _client_selfsigned_issuer(),
                    "client-ca-certificate": _client_ca_certificate(common_name="Modelplane InferenceGateway CA eu"),
                    "client-ca-issuer": _client_ca_issuer(),
                    "client-ca-bundle": _client_ca_bundle(),
                    "client-ca-configmap": _client_ca_configmap(),
                    "failover-policy": _failover_policy(),
                    "client-traffic-policy": _client_traffic_policy(),
                    "healthz-filter": _healthz_filter(),
                    "healthz-route": _healthz_route(),
                    "ai-gateway-anchor": _ai_gateway_anchor(https=False, ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                }
            ),
            conditions=[
                fnv1.Condition(type="GatewayReady", status=fnv1.STATUS_CONDITION_TRUE, reason="GatewayProgrammed"),
            ],
        ),
    ),
    Case(
        name="EnvoyDeploymentRollingOut",
        reason=(
            "An accepted anchor route and Gateway address do not make the gateway Ready while the Envoy "
            "Deployment rollout is incomplete."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(name="eu", tls=False, caller_secret_labels=None),
                resources={
                    "gateway": _observed_gateway(address="gw.example.org", ready=True),
                    "ai-gateway-anchor": _observed_ai_gateway_anchor(),
                    "envoy-deployment": _observed_envoy_deployment(ready=False),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(items=[_cluster(provider_config_ref=True)]),
                "gateways": fnv1.Resources(items=[_inference_gateway(name="eu", address=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(
                    status={
                        "address": "gw.example.org",
                        "endpoints": {
                            "openAI": "http://gw.example.org/v1",
                            "anthropic": "http://gw.example.org/anthropic/v1",
                        },
                    },
                    ready=fnv1.READY_FALSE,
                ),
                resources={
                    "envoy-proxy": _envoy_proxy(),
                    "envoy-deployment": _envoy_deployment(),
                    "gateway": _gateway(https=False, ready=fnv1.READY_TRUE),
                    "client-selfsigned-issuer": _client_selfsigned_issuer(),
                    "client-ca-certificate": _client_ca_certificate(common_name="Modelplane InferenceGateway CA eu"),
                    "client-ca-issuer": _client_ca_issuer(),
                    "client-ca-bundle": _client_ca_bundle(),
                    "client-ca-configmap": _client_ca_configmap(),
                    "failover-policy": _failover_policy(),
                    "client-traffic-policy": _client_traffic_policy(),
                    "healthz-filter": _healthz_filter(),
                    "healthz-route": _healthz_route(),
                    "ai-gateway-anchor": _ai_gateway_anchor(https=False, ready=fnv1.READY_TRUE),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Waiting for the Envoy Deployment on cluster gw-eu to finish rolling out",
                )
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="GatewayReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForEnvoyDeployment",
                    message="Waiting for the Envoy Deployment on cluster gw-eu to finish rolling out",
                ),
            ],
        ),
    ),
    # A ModelService's backends address a cluster gateway by the name
    # compose-inference-cluster derived, and this gateway's Envoy resolves it,
    # so its cluster needs a Service of that name. A hostname is how a cloud
    # load balancer names itself. An IP gets the Endpoints the EndpointSlice
    # supersedes as well, because kube-dns reads only that and is what GKE runs.
    Case(
        name="ClusterGatewaysPublished",
        reason=(
            "Each cluster publishing a gateway address and name gets a Service of that name on this gateway's "
            "cluster, headless with Endpoints and an EndpointSlice for an IP or ExternalName for a hostname, "
            "while this gateway's own cluster, publishing neither, gets none."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_xr(name="eu", tls=False, caller_secret_labels=None)),
            required_resources={
                "gateways": fnv1.Resources(items=[_inference_gateway(name="eu", address=None)]),
                "clusters": fnv1.Resources(
                    items=[
                        _cluster(provider_config_ref=True),
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "modelplane.ai/v1alpha1",
                                    "kind": "InferenceCluster",
                                    "metadata": {"name": "prod-ipv4"},
                                    "spec": {
                                        "cluster": {
                                            "source": "Existing",
                                            "existing": {
                                                "secretRef": {"name": "prod-ipv4-kubeconfig", "key": "kubeconfig"}
                                            },
                                        }
                                    },
                                    "status": {
                                        "gateway": {
                                            "address": "203.0.113.7",
                                            "hostname": "prod-ipv4-gateway-aaaaa.modelplane-system.svc.cluster.local",
                                        }
                                    },
                                }
                            )
                        ),
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "modelplane.ai/v1alpha1",
                                    "kind": "InferenceCluster",
                                    "metadata": {"name": "prod-ipv6"},
                                    "spec": {
                                        "cluster": {
                                            "source": "Existing",
                                            "existing": {
                                                "secretRef": {"name": "prod-ipv6-kubeconfig", "key": "kubeconfig"}
                                            },
                                        }
                                    },
                                    "status": {
                                        "gateway": {
                                            "address": "2001:db8::1",
                                            "hostname": "prod-ipv6-gateway-bbbbb.modelplane-system.svc.cluster.local",
                                        }
                                    },
                                }
                            )
                        ),
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "modelplane.ai/v1alpha1",
                                    "kind": "InferenceCluster",
                                    "metadata": {"name": "prod-dns"},
                                    "spec": {
                                        "cluster": {
                                            "source": "Existing",
                                            "existing": {
                                                "secretRef": {"name": "prod-dns-kubeconfig", "key": "kubeconfig"}
                                            },
                                        }
                                    },
                                    "status": {
                                        "gateway": {
                                            "address": "lb-x.elb.amazonaws.com",
                                            "hostname": "prod-dns-gateway-ccccc.modelplane-system.svc.cluster.local",
                                        }
                                    },
                                }
                            )
                        ),
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(status={}, ready=fnv1.READY_FALSE),
                resources={
                    "envoy-proxy": _envoy_proxy(),
                    "envoy-deployment": _envoy_deployment(),
                    "gateway": _gateway(https=False, ready=fnv1.READY_UNSPECIFIED),
                    "client-selfsigned-issuer": _client_selfsigned_issuer(),
                    "client-ca-certificate": _client_ca_certificate(common_name="Modelplane InferenceGateway CA eu"),
                    "client-ca-issuer": _client_ca_issuer(),
                    "client-ca-bundle": _client_ca_bundle(),
                    "client-ca-configmap": _client_ca_configmap(),
                    "cluster-name-prod-ipv4-gateway-aaaaa": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ClusterProviderConfig",
                                        "name": "gw-eu-cluster-kubeconfig",
                                    },
                                    "readiness": {"policy": "SuccessfulCreate"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "Service",
                                            "metadata": {
                                                "name": "prod-ipv4-gateway-aaaaa",
                                                "namespace": "modelplane-system",
                                            },
                                            "spec": {"clusterIP": "None", "ports": [{"name": "https", "port": 443}]},
                                        }
                                    },
                                },
                            }
                        )
                    ),
                    "cluster-name-endpoints-prod-ipv4-gateway-aaaaa": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ClusterProviderConfig",
                                        "name": "gw-eu-cluster-kubeconfig",
                                    },
                                    "readiness": {"policy": "SuccessfulCreate"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "Endpoints",
                                            "metadata": {
                                                "name": "prod-ipv4-gateway-aaaaa",
                                                "namespace": "modelplane-system",
                                                # Off, or the mirroring controller writes a second
                                                # EndpointSlice over the one composed beside this.
                                                "labels": {"endpointslice.kubernetes.io/skip-mirror": "true"},
                                            },
                                            "subsets": [
                                                {
                                                    "addresses": [{"ip": "203.0.113.7"}],
                                                    "ports": [{"name": "https", "port": 443}],
                                                }
                                            ],
                                        }
                                    },
                                },
                            }
                        )
                    ),
                    "cluster-name-slice-prod-ipv4-gateway-aaaaa": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ClusterProviderConfig",
                                        "name": "gw-eu-cluster-kubeconfig",
                                    },
                                    "readiness": {"policy": "SuccessfulCreate"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "discovery.k8s.io/v1",
                                            "kind": "EndpointSlice",
                                            "metadata": {
                                                "name": "prod-ipv4-gateway-aaaaa",
                                                "namespace": "modelplane-system",
                                                "labels": {"kubernetes.io/service-name": "prod-ipv4-gateway-aaaaa"},
                                            },
                                            "addressType": "IPv4",
                                            "ports": [{"name": "https", "port": 443}],
                                            "endpoints": [
                                                {"addresses": ["203.0.113.7"], "conditions": {"ready": True}}
                                            ],
                                        }
                                    },
                                },
                            }
                        )
                    ),
                    "cluster-name-prod-ipv6-gateway-bbbbb": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ClusterProviderConfig",
                                        "name": "gw-eu-cluster-kubeconfig",
                                    },
                                    "readiness": {"policy": "SuccessfulCreate"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "Service",
                                            "metadata": {
                                                "name": "prod-ipv6-gateway-bbbbb",
                                                "namespace": "modelplane-system",
                                            },
                                            "spec": {"clusterIP": "None", "ports": [{"name": "https", "port": 443}]},
                                        }
                                    },
                                },
                            }
                        )
                    ),
                    "cluster-name-endpoints-prod-ipv6-gateway-bbbbb": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ClusterProviderConfig",
                                        "name": "gw-eu-cluster-kubeconfig",
                                    },
                                    "readiness": {"policy": "SuccessfulCreate"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "Endpoints",
                                            "metadata": {
                                                "name": "prod-ipv6-gateway-bbbbb",
                                                "namespace": "modelplane-system",
                                                # Off, or the mirroring controller writes a second
                                                # EndpointSlice over the one composed beside this.
                                                "labels": {"endpointslice.kubernetes.io/skip-mirror": "true"},
                                            },
                                            "subsets": [
                                                {
                                                    "addresses": [{"ip": "2001:db8::1"}],
                                                    "ports": [{"name": "https", "port": 443}],
                                                }
                                            ],
                                        }
                                    },
                                },
                            }
                        )
                    ),
                    "cluster-name-slice-prod-ipv6-gateway-bbbbb": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ClusterProviderConfig",
                                        "name": "gw-eu-cluster-kubeconfig",
                                    },
                                    "readiness": {"policy": "SuccessfulCreate"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "discovery.k8s.io/v1",
                                            "kind": "EndpointSlice",
                                            "metadata": {
                                                "name": "prod-ipv6-gateway-bbbbb",
                                                "namespace": "modelplane-system",
                                                "labels": {"kubernetes.io/service-name": "prod-ipv6-gateway-bbbbb"},
                                            },
                                            "addressType": "IPv6",
                                            "ports": [{"name": "https", "port": 443}],
                                            "endpoints": [
                                                {"addresses": ["2001:db8::1"], "conditions": {"ready": True}}
                                            ],
                                        }
                                    },
                                },
                            }
                        )
                    ),
                    "cluster-name-prod-dns-gateway-ccccc": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ClusterProviderConfig",
                                        "name": "gw-eu-cluster-kubeconfig",
                                    },
                                    "readiness": {"policy": "SuccessfulCreate"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "Service",
                                            "metadata": {
                                                "name": "prod-dns-gateway-ccccc",
                                                "namespace": "modelplane-system",
                                            },
                                            "spec": {"type": "ExternalName", "externalName": "lb-x.elb.amazonaws.com"},
                                        }
                                    },
                                },
                            }
                        )
                    ),
                    "failover-policy": _failover_policy(),
                    "client-traffic-policy": _client_traffic_policy(),
                    "healthz-filter": _healthz_filter(),
                    "healthz-route": _healthz_route(),
                    "ai-gateway-anchor": _ai_gateway_anchor(https=False, ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL, message="Waiting for the Gateway on cluster gw-eu to be programmed"
                )
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="GatewayReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForGateway",
                    message="Waiting for the Gateway on cluster gw-eu to be programmed",
                )
            ],
        ),
    ),
    # Keeping the Gateway keeps its address. The listener is left without a
    # certificate on the cluster until the Secret appears, rather than the whole
    # Gateway withdrawn and its load balancer moved.
    Case(
        name="TLSSecretMissing",
        reason=(
            "A gateway whose TLS Secret doesn't exist still composes its HTTPS Gateway and redirect, and reports "
            "the Secret missing."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_xr(name="eu", tls=True, caller_secret_labels=None)),
            required_resources={
                "clusters": fnv1.Resources(items=[_cluster(provider_config_ref=True)]),
                "gateways": fnv1.Resources(items=[_inference_gateway(name="eu", address=None)]),
                "tls-secret-0": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(status={}, ready=fnv1.READY_FALSE),
                resources={
                    "envoy-proxy": _envoy_proxy(),
                    "envoy-deployment": _envoy_deployment(),
                    "gateway": _gateway(https=True, ready=fnv1.READY_UNSPECIFIED),
                    "client-selfsigned-issuer": _client_selfsigned_issuer(),
                    "client-ca-certificate": _client_ca_certificate(common_name="Modelplane InferenceGateway CA eu"),
                    "client-ca-issuer": _client_ca_issuer(),
                    "client-ca-bundle": _client_ca_bundle(),
                    "client-ca-configmap": _client_ca_configmap(),
                    "failover-policy": _failover_policy(),
                    "client-traffic-policy": _client_traffic_policy(),
                    "healthz-filter": _healthz_filter(),
                    "healthz-route": _healthz_route(),
                    "ai-gateway-anchor": _ai_gateway_anchor(https=True, ready=fnv1.READY_UNSPECIFIED),
                    "redirect-route": _redirect_route(),
                },
            ),
            results=[fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for TLS Secrets: eu-tls-0")],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "tls-secret-0": fnv1.ResourceSelector(
                        api_version="v1", kind="Secret", namespace="modelplane-system", match_name="eu-tls-0"
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="GatewayReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="SecretsMissing",
                    message="Waiting for TLS Secrets: eu-tls-0",
                )
            ],
        ),
    ),
    # A caller depends on the HTTPS listener, the Secrets copied to the
    # cluster, the caller policy naming them, and /healthz and the :80 redirect
    # exempted from that policy. Status publishes no URLs because a caller
    # reaches a TLS gateway on a DNS name only its owner knows. The certificate
    # is copied verbatim, keeping the name the Gateway refers to it by. The
    # redirect is exempted because it must happen before auth, or an
    # unauthenticated caller would get a 401 instead of being sent to HTTPS.
    Case(
        name="AnchorRouteNotAccepted",
        reason=("A programmed TLS gateway with accepted caller auth waits for its permanent AI Gateway route."),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(name="eu", tls=True, caller_secret_labels={"modelplane.ai/inference-keys": "true"}),
                resources={
                    "gateway": _observed_gateway(address="34.56.129.3", ready=True),
                    "caller-auth": _observed_caller_auth(),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(items=[_cluster(provider_config_ref=True)]),
                "gateways": fnv1.Resources(items=[_inference_gateway(name="eu", address=None)]),
                "caller-secrets": fnv1.Resources(
                    items=[_caller_key_secret(name="ml-team-keys", data={"ml-team-assistant": "c2stbXAtYTFiMmMz"})]
                ),
                "tls-secret-0": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "v1",
                                    "kind": "Secret",
                                    "metadata": {"name": "eu-tls-0", "namespace": "modelplane-system"},
                                    "data": {"tls.crt": "Y2VydA==", "tls.key": "a2V5"},
                                }
                            )
                        )
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(status={"address": "34.56.129.3"}, ready=fnv1.READY_FALSE),
                resources={
                    "caller-secret-ml-team-keys": _caller_secret(
                        name="callers-ml-team-keys", data={"ml-team-assistant": "c2stbXAtYTFiMmMz"}
                    ),
                    "tls-secret-eu-tls-0": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ClusterProviderConfig",
                                        "name": "gw-eu-cluster-kubeconfig",
                                    },
                                    "readiness": {"policy": "SuccessfulCreate"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "Secret",
                                            "metadata": {"name": "eu-tls-0", "namespace": "modelplane-system"},
                                            "type": "kubernetes.io/tls",
                                            "data": {"tls.crt": "Y2VydA==", "tls.key": "a2V5"},
                                        }
                                    },
                                },
                            }
                        )
                    ),
                    "envoy-proxy": _envoy_proxy(),
                    "envoy-deployment": _envoy_deployment(),
                    "gateway": _gateway(https=True, ready=fnv1.READY_TRUE),
                    "client-selfsigned-issuer": _client_selfsigned_issuer(),
                    "client-ca-certificate": _client_ca_certificate(common_name="Modelplane InferenceGateway CA eu"),
                    "client-ca-issuer": _client_ca_issuer(),
                    "client-ca-bundle": _client_ca_bundle(),
                    "client-ca-configmap": _client_ca_configmap(),
                    "caller-auth": _caller_auth(
                        credential_refs=[{"name": "callers-ml-team-keys"}], ready=fnv1.READY_TRUE
                    ),
                    "failover-policy": _failover_policy(),
                    "client-traffic-policy": _client_traffic_policy(),
                    "healthz-filter": _healthz_filter(),
                    "healthz-route": _healthz_route(),
                    "ai-gateway-anchor": _ai_gateway_anchor(https=True, ready=fnv1.READY_UNSPECIFIED),
                    "healthz-auth": _healthz_auth(),
                    "redirect-route": _redirect_route(),
                    "redirect-auth": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ClusterProviderConfig",
                                        "name": "gw-eu-cluster-kubeconfig",
                                    },
                                    "readiness": {
                                        "policy": "DeriveFromCelQuery",
                                        "celQuery": (
                                            "has(object.status) && has(object.status.ancestors) && "
                                            "object.status.ancestors.exists(a, has(a.conditions) && "
                                            "a.conditions.exists(c, c.type == 'Accepted' && c.status == 'True'))"
                                        ),
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                                            "kind": "SecurityPolicy",
                                            "metadata": {
                                                "name": "inference-gateway-redirect-open",
                                                "namespace": "modelplane-system",
                                            },
                                            "spec": {
                                                "targetRefs": [
                                                    {
                                                        "group": "gateway.networking.k8s.io",
                                                        "kind": "HTTPRoute",
                                                        "name": "inference-gateway-redirect",
                                                    }
                                                ],
                                                "authorization": {"defaultAction": "Allow"},
                                            },
                                        }
                                    },
                                },
                            }
                        )
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Waiting for the AI Gateway anchor route on cluster gw-eu to be accepted",
                )
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "caller-secrets": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        namespace="modelplane-system",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/inference-keys": "true"}),
                    ),
                    "tls-secret-0": fnv1.ResourceSelector(
                        api_version="v1", kind="Secret", namespace="modelplane-system", match_name="eu-tls-0"
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="GatewayReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForAIGatewayRoute",
                    message="Waiting for the AI Gateway anchor route on cluster gw-eu to be accepted",
                ),
            ],
        ),
    ),
    # A gateway whose caller policy was rejected refuses every request with a
    # 500 while its Gateway still has an address. Envoy Gateway rejects the
    # policy when two selected Secrets share a key value, so this is reachable
    # by writing two Secrets. The function treats a policy whose Object hasn't
    # been observed the same as a rejected one.
    Case(
        name="CallerPolicyNotObserved",
        reason=(
            "A programmed gateway whose caller policy's Object hasn't been observed publishes its URLs but "
            "reports the policy not accepted and isn't Ready."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(name="eu", tls=False, caller_secret_labels={"modelplane.ai/inference-keys": "true"}),
                resources={
                    "gateway": _observed_gateway(address="34.56.129.3", ready=True),
                    "ai-gateway-anchor": _observed_ai_gateway_anchor(),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(items=[_cluster(provider_config_ref=True)]),
                "gateways": fnv1.Resources(items=[_inference_gateway(name="eu", address=None)]),
                "caller-secrets": fnv1.Resources(
                    items=[_caller_key_secret(name="ml-team-keys", data={"a": "c2stMQ=="})]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(
                    status={
                        "address": "34.56.129.3",
                        "endpoints": {
                            "openAI": "http://34.56.129.3/v1",
                            "anthropic": "http://34.56.129.3/anthropic/v1",
                        },
                    },
                    ready=fnv1.READY_FALSE,
                ),
                resources={
                    "caller-secret-ml-team-keys": _caller_secret(name="callers-ml-team-keys", data={"a": "c2stMQ=="}),
                    "envoy-proxy": _envoy_proxy(),
                    "envoy-deployment": _envoy_deployment(),
                    "gateway": _gateway(https=False, ready=fnv1.READY_TRUE),
                    "client-selfsigned-issuer": _client_selfsigned_issuer(),
                    "client-ca-certificate": _client_ca_certificate(common_name="Modelplane InferenceGateway CA eu"),
                    "client-ca-issuer": _client_ca_issuer(),
                    "client-ca-bundle": _client_ca_bundle(),
                    "client-ca-configmap": _client_ca_configmap(),
                    "caller-auth": _caller_auth(
                        credential_refs=[{"name": "callers-ml-team-keys"}], ready=fnv1.READY_UNSPECIFIED
                    ),
                    "failover-policy": _failover_policy(),
                    "client-traffic-policy": _client_traffic_policy(),
                    "healthz-filter": _healthz_filter(),
                    "healthz-route": _healthz_route(),
                    "ai-gateway-anchor": _ai_gateway_anchor(https=False, ready=fnv1.READY_TRUE),
                    "healthz-auth": _healthz_auth(),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="The gateway's caller authentication policy has not been accepted, so every request is refused",
                )
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "caller-secrets": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        namespace="modelplane-system",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/inference-keys": "true"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="GatewayReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="CallerAuthNotAccepted",
                    message="The gateway's caller authentication policy has not been accepted, so every request is refused",
                )
            ],
        ),
    ),
    # Envoy Gateway rejects the caller policy when two callers share a key, but
    # the policy's Object still reads as accepted until provider-kubernetes
    # next observes it. The gateway reports the outage from the Secrets
    # themselves, and skips a repeated caller name before comparing its key, as
    # Envoy Gateway does. These four cases observe the policy as accepted.
    Case(
        name="KeySharedAcrossSecrets",
        reason="Two caller Secrets holding the same key leave the gateway not Ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(name="eu", tls=False, caller_secret_labels={"modelplane.ai/inference-keys": "true"}),
                resources={
                    "gateway": _observed_gateway(address="34.56.129.3", ready=True),
                    "ai-gateway-anchor": _observed_ai_gateway_anchor(),
                    "envoy-deployment": _observed_envoy_deployment(ready=True),
                    "caller-auth": _observed_caller_auth(),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(items=[_cluster(provider_config_ref=True)]),
                "gateways": fnv1.Resources(items=[_inference_gateway(name="eu", address=None)]),
                "caller-secrets": fnv1.Resources(
                    items=[
                        _caller_key_secret(name="team-a-keys", data={"a": "c2stMQ=="}),
                        _caller_key_secret(name="team-b-keys", data={"b": "c2stMQ=="}),
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(
                    status={
                        "address": "34.56.129.3",
                        "endpoints": {
                            "openAI": "http://34.56.129.3/v1",
                            "anthropic": "http://34.56.129.3/anthropic/v1",
                        },
                    },
                    ready=fnv1.READY_FALSE,
                ),
                resources={
                    "caller-secret-team-a-keys": _caller_secret(name="callers-team-a-keys", data={"a": "c2stMQ=="}),
                    "caller-secret-team-b-keys": _caller_secret(name="callers-team-b-keys", data={"b": "c2stMQ=="}),
                    "envoy-proxy": _envoy_proxy(),
                    "envoy-deployment": _ready_envoy_deployment(),
                    "gateway": _gateway(https=False, ready=fnv1.READY_TRUE),
                    "client-selfsigned-issuer": _client_selfsigned_issuer(),
                    "client-ca-certificate": _client_ca_certificate(common_name="Modelplane InferenceGateway CA eu"),
                    "client-ca-issuer": _client_ca_issuer(),
                    "client-ca-bundle": _client_ca_bundle(),
                    "client-ca-configmap": _client_ca_configmap(),
                    "caller-auth": _caller_auth(
                        credential_refs=[{"name": "callers-team-a-keys"}, {"name": "callers-team-b-keys"}],
                        ready=fnv1.READY_TRUE,
                    ),
                    "failover-policy": _failover_policy(),
                    "client-traffic-policy": _client_traffic_policy(),
                    "healthz-filter": _healthz_filter(),
                    "healthz-route": _healthz_route(),
                    "ai-gateway-anchor": _ai_gateway_anchor(https=False, ready=fnv1.READY_TRUE),
                    "healthz-auth": _healthz_auth(),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Caller team-b-keys/b has the same key as team-a-keys/a, so Envoy Gateway rejects the "
                    "caller authentication policy and every request is refused",
                )
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "caller-secrets": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        namespace="modelplane-system",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/inference-keys": "true"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="GatewayReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="CallerAuthNotAccepted",
                    message="Caller team-b-keys/b has the same key as team-a-keys/a, so Envoy Gateway rejects the "
                    "caller authentication policy and every request is refused",
                )
            ],
        ),
    ),
    Case(
        name="KeyRepeatedInSecret",
        reason="One caller Secret holding the same key for two callers leaves the gateway not Ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(name="eu", tls=False, caller_secret_labels={"modelplane.ai/inference-keys": "true"}),
                resources={
                    "gateway": _observed_gateway(address="34.56.129.3", ready=True),
                    "ai-gateway-anchor": _observed_ai_gateway_anchor(),
                    "caller-auth": _observed_caller_auth(),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(items=[_cluster(provider_config_ref=True)]),
                "gateways": fnv1.Resources(items=[_inference_gateway(name="eu", address=None)]),
                "caller-secrets": fnv1.Resources(
                    items=[_caller_key_secret(name="team-a-keys", data={"a": "c2stMQ==", "z": "c2stMQ=="})]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(
                    status={
                        "address": "34.56.129.3",
                        "endpoints": {
                            "openAI": "http://34.56.129.3/v1",
                            "anthropic": "http://34.56.129.3/anthropic/v1",
                        },
                    },
                    ready=fnv1.READY_FALSE,
                ),
                resources={
                    "caller-secret-team-a-keys": _caller_secret(
                        name="callers-team-a-keys", data={"a": "c2stMQ==", "z": "c2stMQ=="}
                    ),
                    "envoy-proxy": _envoy_proxy(),
                    "envoy-deployment": _envoy_deployment(),
                    "gateway": _gateway(https=False, ready=fnv1.READY_TRUE),
                    "client-selfsigned-issuer": _client_selfsigned_issuer(),
                    "client-ca-certificate": _client_ca_certificate(common_name="Modelplane InferenceGateway CA eu"),
                    "client-ca-issuer": _client_ca_issuer(),
                    "client-ca-bundle": _client_ca_bundle(),
                    "client-ca-configmap": _client_ca_configmap(),
                    "caller-auth": _caller_auth(
                        credential_refs=[{"name": "callers-team-a-keys"}], ready=fnv1.READY_TRUE
                    ),
                    "failover-policy": _failover_policy(),
                    "client-traffic-policy": _client_traffic_policy(),
                    "healthz-filter": _healthz_filter(),
                    "healthz-route": _healthz_route(),
                    "ai-gateway-anchor": _ai_gateway_anchor(https=False, ready=fnv1.READY_TRUE),
                    "healthz-auth": _healthz_auth(),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Caller team-a-keys/z has the same key as team-a-keys/a, so Envoy Gateway rejects the "
                    "caller authentication policy and every request is refused",
                )
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "caller-secrets": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        namespace="modelplane-system",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/inference-keys": "true"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="GatewayReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="CallerAuthNotAccepted",
                    message="Caller team-a-keys/z has the same key as team-a-keys/a, so Envoy Gateway rejects the "
                    "caller authentication policy and every request is refused",
                )
            ],
        ),
    ),
    # Walked as listed, team-a's x would be the skipped one, and no key would
    # repeat.
    Case(
        name="NameOrderRepeatsKey",
        reason=(
            "Caller Secrets listed out of name order are walked in name order, so team-b's repeated caller x is "
            "skipped, its y repeats team-a's x key, and the gateway isn't Ready."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(name="eu", tls=False, caller_secret_labels={"modelplane.ai/inference-keys": "true"}),
                resources={
                    "gateway": _observed_gateway(address="34.56.129.3", ready=True),
                    "ai-gateway-anchor": _observed_ai_gateway_anchor(),
                    "caller-auth": _observed_caller_auth(),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(items=[_cluster(provider_config_ref=True)]),
                "gateways": fnv1.Resources(items=[_inference_gateway(name="eu", address=None)]),
                "caller-secrets": fnv1.Resources(
                    items=[
                        _caller_key_secret(name="team-b-keys", data={"x": "c2stMg==", "y": "c2stMQ=="}),
                        _caller_key_secret(name="team-a-keys", data={"x": "c2stMQ=="}),
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(
                    status={
                        "address": "34.56.129.3",
                        "endpoints": {
                            "openAI": "http://34.56.129.3/v1",
                            "anthropic": "http://34.56.129.3/anthropic/v1",
                        },
                    },
                    ready=fnv1.READY_FALSE,
                ),
                resources={
                    "caller-secret-team-a-keys": _caller_secret(name="callers-team-a-keys", data={"x": "c2stMQ=="}),
                    "caller-secret-team-b-keys": _caller_secret(
                        name="callers-team-b-keys", data={"x": "c2stMg==", "y": "c2stMQ=="}
                    ),
                    "envoy-proxy": _envoy_proxy(),
                    "envoy-deployment": _envoy_deployment(),
                    "gateway": _gateway(https=False, ready=fnv1.READY_TRUE),
                    "client-selfsigned-issuer": _client_selfsigned_issuer(),
                    "client-ca-certificate": _client_ca_certificate(common_name="Modelplane InferenceGateway CA eu"),
                    "client-ca-issuer": _client_ca_issuer(),
                    "client-ca-bundle": _client_ca_bundle(),
                    "client-ca-configmap": _client_ca_configmap(),
                    "caller-auth": _caller_auth(
                        credential_refs=[{"name": "callers-team-a-keys"}, {"name": "callers-team-b-keys"}],
                        ready=fnv1.READY_TRUE,
                    ),
                    "failover-policy": _failover_policy(),
                    "client-traffic-policy": _client_traffic_policy(),
                    "healthz-filter": _healthz_filter(),
                    "healthz-route": _healthz_route(),
                    "ai-gateway-anchor": _ai_gateway_anchor(https=False, ready=fnv1.READY_TRUE),
                    "healthz-auth": _healthz_auth(),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Caller team-b-keys/y has the same key as team-a-keys/x, so Envoy Gateway rejects the "
                    "caller authentication policy and every request is refused",
                )
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "caller-secrets": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        namespace="modelplane-system",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/inference-keys": "true"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="GatewayReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="CallerAuthNotAccepted",
                    message="Caller team-b-keys/y has the same key as team-a-keys/x, so Envoy Gateway rejects the "
                    "caller authentication policy and every request is refused",
                )
            ],
        ),
    ),
    Case(
        name="CallerNameRepeated",
        reason=(
            "A caller name repeated in a later Secret is skipped before its key is compared, so the gateway is Ready."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(name="eu", tls=False, caller_secret_labels={"modelplane.ai/inference-keys": "true"}),
                resources={
                    "gateway": _observed_gateway(address="34.56.129.3", ready=True),
                    "ai-gateway-anchor": _observed_ai_gateway_anchor(),
                    "envoy-deployment": _observed_envoy_deployment(ready=True),
                    "caller-auth": _observed_caller_auth(),
                },
            ),
            required_resources={
                "clusters": fnv1.Resources(items=[_cluster(provider_config_ref=True)]),
                "gateways": fnv1.Resources(items=[_inference_gateway(name="eu", address=None)]),
                "caller-secrets": fnv1.Resources(
                    items=[
                        _caller_key_secret(name="team-a-keys", data={"a": "c2stMQ=="}),
                        _caller_key_secret(name="team-b-keys", data={"a": "c2stMQ==", "b": "c2stMg=="}),
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(
                    status={
                        "address": "34.56.129.3",
                        "endpoints": {
                            "openAI": "http://34.56.129.3/v1",
                            "anthropic": "http://34.56.129.3/anthropic/v1",
                        },
                    },
                    ready=fnv1.READY_UNSPECIFIED,
                ),
                resources={
                    "caller-secret-team-a-keys": _caller_secret(name="callers-team-a-keys", data={"a": "c2stMQ=="}),
                    "caller-secret-team-b-keys": _caller_secret(
                        name="callers-team-b-keys", data={"a": "c2stMQ==", "b": "c2stMg=="}
                    ),
                    "envoy-proxy": _envoy_proxy(),
                    "envoy-deployment": _ready_envoy_deployment(),
                    "gateway": _gateway(https=False, ready=fnv1.READY_TRUE),
                    "client-selfsigned-issuer": _client_selfsigned_issuer(),
                    "client-ca-certificate": _client_ca_certificate(common_name="Modelplane InferenceGateway CA eu"),
                    "client-ca-issuer": _client_ca_issuer(),
                    "client-ca-bundle": _client_ca_bundle(),
                    "client-ca-configmap": _client_ca_configmap(),
                    "caller-auth": _caller_auth(
                        credential_refs=[{"name": "callers-team-a-keys"}, {"name": "callers-team-b-keys"}],
                        ready=fnv1.READY_TRUE,
                    ),
                    "failover-policy": _failover_policy(),
                    "client-traffic-policy": _client_traffic_policy(),
                    "healthz-filter": _healthz_filter(),
                    "healthz-route": _healthz_route(),
                    "ai-gateway-anchor": _ai_gateway_anchor(https=False, ready=fnv1.READY_TRUE),
                    "healthz-auth": _healthz_auth(),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "caller-secrets": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        namespace="modelplane-system",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/inference-keys": "true"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(type="GatewayReady", status=fnv1.STATUS_CONDITION_TRUE, reason="GatewayProgrammed"),
            ],
        ),
    ),
    # Envoy Gateway keeps the first Secret listed when two hold the same caller
    # name, so listing them by name keeps the winner from changing between
    # reconciles.
    Case(
        name="CallerSecretsUnordered",
        reason="Caller Secrets that resolve out of name order are listed in name order in the caller policy.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(name="eu", tls=False, caller_secret_labels={"modelplane.ai/inference-keys": "true"})
            ),
            required_resources={
                "clusters": fnv1.Resources(items=[_cluster(provider_config_ref=True)]),
                "gateways": fnv1.Resources(items=[_inference_gateway(name="eu", address=None)]),
                "caller-secrets": fnv1.Resources(
                    items=[
                        _caller_key_secret(name="team-b-keys", data={"b": "c2stMg=="}),
                        _caller_key_secret(name="team-a-keys", data={"a": "c2stMQ=="}),
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(status={}, ready=fnv1.READY_FALSE),
                resources={
                    "caller-secret-team-a-keys": _caller_secret(name="callers-team-a-keys", data={"a": "c2stMQ=="}),
                    "caller-secret-team-b-keys": _caller_secret(name="callers-team-b-keys", data={"b": "c2stMg=="}),
                    "envoy-proxy": _envoy_proxy(),
                    "envoy-deployment": _envoy_deployment(),
                    "gateway": _gateway(https=False, ready=fnv1.READY_UNSPECIFIED),
                    "client-selfsigned-issuer": _client_selfsigned_issuer(),
                    "client-ca-certificate": _client_ca_certificate(common_name="Modelplane InferenceGateway CA eu"),
                    "client-ca-issuer": _client_ca_issuer(),
                    "client-ca-bundle": _client_ca_bundle(),
                    "client-ca-configmap": _client_ca_configmap(),
                    "caller-auth": _caller_auth(
                        credential_refs=[{"name": "callers-team-a-keys"}, {"name": "callers-team-b-keys"}],
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "failover-policy": _failover_policy(),
                    "client-traffic-policy": _client_traffic_policy(),
                    "healthz-filter": _healthz_filter(),
                    "healthz-route": _healthz_route(),
                    "ai-gateway-anchor": _ai_gateway_anchor(https=False, ready=fnv1.READY_UNSPECIFIED),
                    "healthz-auth": _healthz_auth(),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL, message="Waiting for the Gateway on cluster gw-eu to be programmed"
                )
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "caller-secrets": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        namespace="modelplane-system",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/inference-keys": "true"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="GatewayReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForGateway",
                    message="Waiting for the Gateway on cluster gw-eu to be programmed",
                )
            ],
        ),
    ),
    # Keeping the Gateway keeps its load balancer and address. The caller policy
    # denies every request rather than authenticating nobody by omission. This
    # case and the next differ only in the message reported.
    #
    # The deny-all caller policy is a different role from _caller_auth's API-key
    # policy, and only these two cases compose it, so it's written inline.
    Case(
        name="NoCallerSecretMatched",
        reason=(
            "A caller selector that matches no Secret still composes the Gateway, copies no caller Secret, "
            "composes a caller policy denying every request, and reports the Secrets missing."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(name="eu", tls=False, caller_secret_labels={"modelplane.ai/inference-keys": "true"})
            ),
            required_resources={
                "clusters": fnv1.Resources(items=[_cluster(provider_config_ref=True)]),
                "gateways": fnv1.Resources(items=[_inference_gateway(name="eu", address=None)]),
                "caller-secrets": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(status={}, ready=fnv1.READY_FALSE),
                resources={
                    "envoy-proxy": _envoy_proxy(),
                    "envoy-deployment": _envoy_deployment(),
                    "gateway": _gateway(https=False, ready=fnv1.READY_UNSPECIFIED),
                    "client-selfsigned-issuer": _client_selfsigned_issuer(),
                    "client-ca-certificate": _client_ca_certificate(common_name="Modelplane InferenceGateway CA eu"),
                    "client-ca-issuer": _client_ca_issuer(),
                    "client-ca-bundle": _client_ca_bundle(),
                    "client-ca-configmap": _client_ca_configmap(),
                    "caller-auth": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ClusterProviderConfig",
                                        "name": "gw-eu-cluster-kubeconfig",
                                    },
                                    "readiness": {
                                        "policy": "DeriveFromCelQuery",
                                        "celQuery": (
                                            "has(object.status) && has(object.status.ancestors) && "
                                            "object.status.ancestors.exists(a, has(a.conditions) && "
                                            "a.conditions.exists(c, c.type == 'Accepted' && c.status == 'True'))"
                                        ),
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                                            "kind": "SecurityPolicy",
                                            "metadata": {
                                                "name": "inference-gateway-callers",
                                                "namespace": "modelplane-system",
                                            },
                                            "spec": {
                                                "targetRefs": [
                                                    {
                                                        "group": "gateway.networking.k8s.io",
                                                        "kind": "Gateway",
                                                        "name": "inference-gateway",
                                                    }
                                                ],
                                                "authorization": {"defaultAction": "Deny"},
                                            },
                                        }
                                    },
                                },
                            }
                        )
                    ),
                    "failover-policy": _failover_policy(),
                    "client-traffic-policy": _client_traffic_policy(),
                    "healthz-filter": _healthz_filter(),
                    "healthz-route": _healthz_route(),
                    "ai-gateway-anchor": _ai_gateway_anchor(https=False, ready=fnv1.READY_UNSPECIFIED),
                    "healthz-auth": _healthz_auth(),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="spec.auth.apiKey.secretSelector matches no Secret, so no caller could authenticate",
                )
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "caller-secrets": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        namespace="modelplane-system",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/inference-keys": "true"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="GatewayReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="SecretsMissing",
                    message="spec.auth.apiKey.secretSelector matches no Secret, so no caller could authenticate",
                )
            ],
        ),
    ),
    Case(
        name="CallerSecretsUnresolved",
        reason=(
            "Caller Secrets that haven't resolved yet still compose the Gateway, copy no caller Secret, compose "
            "a caller policy denying every request, and report the Secrets missing."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(name="eu", tls=False, caller_secret_labels={"modelplane.ai/inference-keys": "true"})
            ),
            required_resources={
                "clusters": fnv1.Resources(items=[_cluster(provider_config_ref=True)]),
                "gateways": fnv1.Resources(items=[_inference_gateway(name="eu", address=None)]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(status={}, ready=fnv1.READY_FALSE),
                resources={
                    "envoy-proxy": _envoy_proxy(),
                    "envoy-deployment": _envoy_deployment(),
                    "gateway": _gateway(https=False, ready=fnv1.READY_UNSPECIFIED),
                    "client-selfsigned-issuer": _client_selfsigned_issuer(),
                    "client-ca-certificate": _client_ca_certificate(common_name="Modelplane InferenceGateway CA eu"),
                    "client-ca-issuer": _client_ca_issuer(),
                    "client-ca-bundle": _client_ca_bundle(),
                    "client-ca-configmap": _client_ca_configmap(),
                    "caller-auth": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ClusterProviderConfig",
                                        "name": "gw-eu-cluster-kubeconfig",
                                    },
                                    "readiness": {
                                        "policy": "DeriveFromCelQuery",
                                        "celQuery": (
                                            "has(object.status) && has(object.status.ancestors) && "
                                            "object.status.ancestors.exists(a, has(a.conditions) && "
                                            "a.conditions.exists(c, c.type == 'Accepted' && c.status == 'True'))"
                                        ),
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                                            "kind": "SecurityPolicy",
                                            "metadata": {
                                                "name": "inference-gateway-callers",
                                                "namespace": "modelplane-system",
                                            },
                                            "spec": {
                                                "targetRefs": [
                                                    {
                                                        "group": "gateway.networking.k8s.io",
                                                        "kind": "Gateway",
                                                        "name": "inference-gateway",
                                                    }
                                                ],
                                                "authorization": {"defaultAction": "Deny"},
                                            },
                                        }
                                    },
                                },
                            }
                        )
                    ),
                    "failover-policy": _failover_policy(),
                    "client-traffic-policy": _client_traffic_policy(),
                    "healthz-filter": _healthz_filter(),
                    "healthz-route": _healthz_route(),
                    "ai-gateway-anchor": _ai_gateway_anchor(https=False, ready=fnv1.READY_UNSPECIFIED),
                    "healthz-auth": _healthz_auth(),
                },
            ),
            results=[fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for caller key Secrets to resolve")],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "caller-secrets": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        namespace="modelplane-system",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/inference-keys": "true"}),
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="GatewayReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="SecretsMissing",
                    message="Waiting for caller key Secrets to resolve",
                )
            ],
        ),
    ),
    # No composed Object observes a Secret, which is what keeps this gateway's
    # client CA private key off the control plane. provider-kubernetes copies
    # an observed object's whole manifest into the Object's status, and its
    # --sanitize-secrets flag defaults to false, so observing a Secret publishes
    # every key in it to anyone who can get objects. This CA signs the
    # certificate every cluster gateway in the fleet accepts, so leaking its
    # key means anyone can reach any engine.
    #
    # Without auth or TLS this function composes no Secret at all.
    #
    # Observing is what matters here. The Secrets this function writes also end
    # up in status, because provider-kubernetes reports what it observes of what
    # it manages, so this alone doesn't keep their contents off the control
    # plane. Those hold caller keys and serving certificates that came from
    # control-plane Secrets to begin with, so the exposure is a wider audience
    # for data already present rather than data that would otherwise never be
    # there, and prerequisites.yaml runs provider-kubernetes with
    # --sanitize-secrets to redact it. A CA private key is different in kind: it
    # is generated on the workload cluster and observing it is the only way it
    # could ever reach the control plane.
    Case(
        name="TLSAndAuthUnobserved",
        reason=(
            "With TLS and auth on and nothing observed yet, a gateway copies its TLS and caller Secrets to the "
            "cluster, and no composed Object observes a Secret."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_xr(name="eu", tls=True, caller_secret_labels={"team": "ml"})),
            required_resources={
                "clusters": fnv1.Resources(items=[_cluster(provider_config_ref=True)]),
                "gateways": fnv1.Resources(items=[_inference_gateway(name="eu", address=None)]),
                "caller-secrets": fnv1.Resources(
                    items=[_caller_key_secret(name="ml-team-keys", data={"alice": "a2V5"})]
                ),
                "tls-secret-0": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "v1",
                                    "kind": "Secret",
                                    "metadata": {"name": "eu-tls-0", "namespace": "modelplane-system"},
                                    "data": {"tls.crt": "Y2VydA==", "tls.key": "a2V5"},
                                }
                            )
                        )
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(status={}, ready=fnv1.READY_FALSE),
                resources={
                    "caller-secret-ml-team-keys": _caller_secret(name="callers-ml-team-keys", data={"alice": "a2V5"}),
                    "tls-secret-eu-tls-0": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ClusterProviderConfig",
                                        "name": "gw-eu-cluster-kubeconfig",
                                    },
                                    "readiness": {"policy": "SuccessfulCreate"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "Secret",
                                            "metadata": {"name": "eu-tls-0", "namespace": "modelplane-system"},
                                            "type": "kubernetes.io/tls",
                                            "data": {"tls.crt": "Y2VydA==", "tls.key": "a2V5"},
                                        }
                                    },
                                },
                            }
                        )
                    ),
                    "envoy-proxy": _envoy_proxy(),
                    "envoy-deployment": _envoy_deployment(),
                    "gateway": _gateway(https=True, ready=fnv1.READY_UNSPECIFIED),
                    "client-selfsigned-issuer": _client_selfsigned_issuer(),
                    "client-ca-certificate": _client_ca_certificate(common_name="Modelplane InferenceGateway CA eu"),
                    "client-ca-issuer": _client_ca_issuer(),
                    "client-ca-bundle": _client_ca_bundle(),
                    "client-ca-configmap": _client_ca_configmap(),
                    "caller-auth": _caller_auth(
                        credential_refs=[{"name": "callers-ml-team-keys"}], ready=fnv1.READY_UNSPECIFIED
                    ),
                    "failover-policy": _failover_policy(),
                    "client-traffic-policy": _client_traffic_policy(),
                    "healthz-filter": _healthz_filter(),
                    "healthz-route": _healthz_route(),
                    "ai-gateway-anchor": _ai_gateway_anchor(https=True, ready=fnv1.READY_UNSPECIFIED),
                    "healthz-auth": _healthz_auth(),
                    "redirect-route": _redirect_route(),
                    "redirect-auth": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ClusterProviderConfig",
                                        "name": "gw-eu-cluster-kubeconfig",
                                    },
                                    "readiness": {
                                        "policy": "DeriveFromCelQuery",
                                        "celQuery": (
                                            "has(object.status) && has(object.status.ancestors) && "
                                            "object.status.ancestors.exists(a, has(a.conditions) && "
                                            "a.conditions.exists(c, c.type == 'Accepted' && c.status == 'True'))"
                                        ),
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                                            "kind": "SecurityPolicy",
                                            "metadata": {
                                                "name": "inference-gateway-redirect-open",
                                                "namespace": "modelplane-system",
                                            },
                                            "spec": {
                                                "targetRefs": [
                                                    {
                                                        "group": "gateway.networking.k8s.io",
                                                        "kind": "HTTPRoute",
                                                        "name": "inference-gateway-redirect",
                                                    }
                                                ],
                                                "authorization": {"defaultAction": "Allow"},
                                            },
                                        }
                                    },
                                },
                            }
                        )
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL, message="Waiting for the Gateway on cluster gw-eu to be programmed"
                )
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
                    "caller-secrets": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        namespace="modelplane-system",
                        match_labels=fnv1.MatchLabels(labels={"team": "ml"}),
                    ),
                    "tls-secret-0": fnv1.ResourceSelector(
                        api_version="v1", kind="Secret", namespace="modelplane-system", match_name="eu-tls-0"
                    ),
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="GatewayReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForGateway",
                    message="Waiting for the Gateway on cluster gw-eu to be programmed",
                )
            ],
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction composes an InferenceGateway and reports its readiness."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
