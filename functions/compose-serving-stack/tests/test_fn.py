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

"""Tests for the compose-serving-stack function.

Two tables. COMPOSE_CASES compares whole RunFunctionResponses: the
Existing/Dynamo stack across the reconcile passes, a non-GCP identity secret,
and the Existing/Standard stack's gateway with and without a client CA. Its
expectations are literals typed here, never read from the stacks package, so a
stack-data change shows up as a test diff. Only the vendored CRD bundles are
read from their files. COMPOSED_RESOURCE_KEYS_CASES then pins the
composed-resource key set - the identity contract; renaming a key deletes and
recreates the remote resource - for every cloud and stack.
"""

import asyncio
import dataclasses
import json
import pathlib

import pytest
import yaml
from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn, stacks
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format, message
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.infrastructure.servingstack import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class ComposeCase:
    """A test case for RunFunction's whole response."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


@dataclasses.dataclass
class ComposedResourceKeysCase:
    """A test case for the composed-resource keys RunFunction renders."""

    name: str
    req: fnv1.RunFunctionRequest
    want: set[str]


def _crd(*, filename: str, name: str) -> dict:
    """The CRD named name in the vendored bundle filename, as the file has it."""
    # The vendored CRD bundles are upstream release artifacts, a thousand lines
    # of schema, so the expectations read them rather than restating them. They
    # resolve via the installed function package, because the sandboxed test
    # check runs against the venv's copy, not the tree.
    bundle = pathlib.Path(fn.__file__).parent / "stacks" / "crds" / filename
    return next(
        doc
        for doc in yaml.safe_load_all(bundle.read_text())
        if doc and doc["kind"] == "CustomResourceDefinition" and doc["metadata"]["name"] == name
    )


def _serving_stack(
    *, cloud: stacks.Cloud, stack: stacks.Stack, secrets: list[v1alpha1.Secret], gateway: v1alpha1.Gateway
) -> fnv1.Resource:
    """The observed ServingStack, test-backend in namespace test-ns."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            v1alpha1.ServingStack(
                metadata=metav1.ObjectMeta(name="test-backend", namespace="test-ns"),
                spec=v1alpha1.Spec(cloud=cloud, stack=stack, secrets=secrets, gateway=gateway),
            ).model_dump(exclude_none=True, mode="json", by_alias=True)
        )
    )


def _desired_serving_stack(*, gateway: dict | None) -> fnv1.Resource:
    """The desired ServingStack, publishing gateway in its status if there is one."""
    status = {} if gateway is None else {"gateway": gateway}
    return fnv1.Resource(resource=resource.dict_to_struct({"status": status}))


def _observed_ready() -> fnv1.Resource:
    """An observed composed resource whose Ready condition is True."""
    return fnv1.Resource(
        resource=resource.dict_to_struct({"status": {"conditions": [{"type": "Ready", "status": "True"}]}})
    )


def _observed_kubernetes_provider_config() -> fnv1.Resource:
    """The observed provider-kubernetes ProviderConfig, which has no conditions."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {"apiVersion": "kubernetes.m.crossplane.io/v1alpha1", "kind": "ProviderConfig"}
        )
    )


def _observed_helm_provider_config() -> fnv1.Resource:
    """The observed provider-helm ProviderConfig, which has no conditions."""
    return fnv1.Resource(
        resource=resource.dict_to_struct({"apiVersion": "helm.m.crossplane.io/v1beta1", "kind": "ProviderConfig"})
    )


def _kubernetes_provider_config(*, identity: dict | None, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed provider-kubernetes ProviderConfig, authenticating as identity if there is one."""
    spec: dict = {
        "credentials": {
            "source": "Secret",
            "secretRef": {"name": "kube-secret", "namespace": "test-ns", "key": "kubeconfig"},
        },
    }
    if identity is not None:
        spec["identity"] = identity
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "ProviderConfig",
                "metadata": {"name": "test-backend-cluster-63fde"},
                "spec": spec,
            }
        ),
        ready=ready,
    )


def _helm_provider_config(*, identity: dict | None, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed provider-helm ProviderConfig, authenticating as identity if there is one."""
    spec: dict = {
        "credentials": {
            "source": "Secret",
            "secretRef": {"name": "kube-secret", "namespace": "test-ns", "key": "kubeconfig"},
        },
    }
    if identity is not None:
        spec["identity"] = identity
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "ProviderConfig",
                "metadata": {"name": "test-backend-cluster-63fde"},
                "spec": spec,
            }
        ),
        ready=ready,
    )


def _cert_manager(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed cert-manager Release."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "Release",
                "metadata": {
                    "annotations": {"crossplane.io/external-name": "mp-cert-manager"},
                    "labels": {"modelplane.ai/resource": "cert-manager"},
                },
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "chart": {
                            "name": "cert-manager",
                            "repository": "https://charts.jetstack.io",
                            "version": "v1.20.2",
                        },
                        "namespace": "cert-manager",
                        "wait": True,
                        "waitTimeout": "10m",
                        # clusterResourceNamespace and enableCertificateOwnerRef are forced by
                        # fn._helm_release for every cloud's cert-manager: the ClusterIssuer CA
                        # lives in modelplane-system, and a deleted ModelRoute's client
                        # certificate Secret must go with its Certificate.
                        "values": {
                            "crds": {"enabled": True},
                            "clusterResourceNamespace": "modelplane-system",
                            "enableCertificateOwnerRef": True,
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _kube_prometheus_stack(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed kube-prometheus-stack Release."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "Release",
                "metadata": {
                    "annotations": {"crossplane.io/external-name": "mp-kube-prometheus-stack"},
                    "labels": {"modelplane.ai/resource": "kube-prometheus-stack"},
                },
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "chart": {
                            "name": "kube-prometheus-stack",
                            "repository": "https://prometheus-community.github.io/helm-charts",
                            "version": "84.4.0",
                        },
                        "namespace": "monitoring",
                        "values": {
                            "fullnameOverride": "prometheus",
                            "prometheus": {
                                "prometheusSpec": {
                                    "podMonitorSelectorNilUsesHelmValues": False,
                                    "podMonitorNamespaceSelector": {},
                                    "additionalScrapeConfigs": [
                                        {
                                            "job_name": "envoy-gateway-proxy",
                                            "kubernetes_sd_configs": [
                                                {
                                                    "role": "pod",
                                                    "namespaces": {"names": ["envoy-gateway-system"]},
                                                }
                                            ],
                                            "relabel_configs": [
                                                {
                                                    "source_labels": [
                                                        "__meta_kubernetes_pod_label_app_kubernetes_io_component"
                                                    ],
                                                    "action": "keep",
                                                    "regex": "proxy",
                                                },
                                                {
                                                    "source_labels": ["__address__"],
                                                    "action": "replace",
                                                    "regex": "([^:]+)(?::\\d+)?",
                                                    "replacement": "$1:19001",
                                                    "target_label": "__address__",
                                                },
                                            ],
                                            "metrics_path": "/stats/prometheus",
                                        }
                                    ],
                                }
                            },
                            "grafana": {"enabled": False},
                            "alertmanager": {"enabled": False},
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _node_feature_discovery(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed node-feature-discovery Release."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "Release",
                "metadata": {
                    "annotations": {"crossplane.io/external-name": "mp-node-feature-discovery"},
                    "labels": {"modelplane.ai/resource": "node-feature-discovery"},
                },
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "chart": {
                            "name": "node-feature-discovery",
                            "repository": "https://kubernetes-sigs.github.io/node-feature-discovery/charts",
                            "version": "0.19.0",
                        },
                        "namespace": "node-feature-discovery",
                        "values": {
                            "worker": {
                                "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]
                            }
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _nvidia_dra_driver_gpu(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed NVIDIA GPU DRA driver Release."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "Release",
                "metadata": {
                    "annotations": {"crossplane.io/external-name": "mp-dra-driver-nvidia-gpu"},
                    "labels": {"modelplane.ai/resource": "nvidia-dra-driver-gpu"},
                },
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "chart": {
                            "name": "dra-driver-nvidia-gpu",
                            "repository": "oci://registry.k8s.io/dra-driver-nvidia/charts",
                            "version": "0.4.1",
                        },
                        "namespace": "nvidia-dra-driver",
                        "values": {
                            "gpuResourcesEnabledOverride": True,
                            "resources": {"computeDomains": {"enabled": False}},
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _ai_gateway_crds(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed Envoy AI Gateway CRDs Release."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "Release",
                "metadata": {
                    "annotations": {"crossplane.io/external-name": "mp-ai-gateway-crds-helm"},
                    "labels": {"modelplane.ai/resource": "ai-gateway-crds"},
                },
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "chart": {
                            "name": "ai-gateway-crds-helm",
                            "repository": "oci://docker.io/envoyproxy",
                            "version": "v1.1.0",
                        },
                        "namespace": "envoy-ai-gateway-system",
                        "wait": True,
                        "waitTimeout": "10m",
                    },
                },
            }
        ),
        ready=ready,
    )


def _gaie_crds_inferenceobjectives_x_k8s(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed x-k8s.io InferenceObjective CRD."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {
                    "labels": {"modelplane.ai/resource": "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io"}
                },
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": _crd(filename="gaie.yaml", name="inferenceobjectives.inference.networking.x-k8s.io")
                    },
                },
            }
        ),
        ready=ready,
    )


def _gaie_crds_inferencepools_k8s(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed k8s.io InferencePool CRD."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {
                    "labels": {"modelplane.ai/resource": "gaie-crds-inferencepools.inference.networking.k8s.io"}
                },
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": _crd(filename="gaie.yaml", name="inferencepools.inference.networking.k8s.io")
                    },
                },
            }
        ),
        ready=ready,
    )


def _gaie_crds_inferencepools_x_k8s(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed x-k8s.io InferencePool CRD."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {
                    "labels": {"modelplane.ai/resource": "gaie-crds-inferencepools.inference.networking.x-k8s.io"}
                },
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": _crd(filename="gaie.yaml", name="inferencepools.inference.networking.x-k8s.io")
                    },
                },
            }
        ),
        ready=ready,
    )


def _gateway_namespace(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed modelplane-system Namespace."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"labels": {"modelplane.ai/resource": "gateway-namespace"}},
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "v1",
                            "kind": "Namespace",
                            "metadata": {
                                "name": "modelplane-system",
                                "labels": {"modelplane.ai/namespace": "modelplane-system"},
                            },
                        }
                    },
                },
            }
        ),
        ready=ready,
    )


def _gateway_selfsigned_issuer() -> fnv1.Resource:
    """The composed self-signed Issuer the cluster CA roots in, marked ready."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"labels": {"modelplane.ai/resource": "gateway-selfsigned-issuer"}},
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "cert-manager.io/v1",
                            "kind": "Issuer",
                            "metadata": {"name": "modelplane-selfsigned", "namespace": "modelplane-system"},
                            "spec": {"selfSigned": {}},
                        }
                    },
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _trust_manager(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed trust-manager Release."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "Release",
                "metadata": {
                    "annotations": {"crossplane.io/external-name": "mp-trust-manager"},
                    "labels": {"modelplane.ai/resource": "trust-manager"},
                },
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "chart": {
                            "name": "trust-manager",
                            "repository": "oci://quay.io/jetstack/charts",
                            "version": "v0.25.0",
                        },
                        "namespace": "modelplane-system",
                        "values": {
                            "crds": {"enabled": True, "keep": True},
                            "app": {"trust": {"namespace": "modelplane-system"}},
                            "defaultPackage": {"enabled": False},
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _dra_driver_critical_pods_quota(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed ResourceQuota admitting the DRA driver's critical pods."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"labels": {"modelplane.ai/resource": "dra-driver-critical-pods-quota"}},
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "v1",
                            "kind": "ResourceQuota",
                            "metadata": {"name": "allow-critical-pods", "namespace": "nvidia-dra-driver"},
                            "spec": {
                                "hard": {"pods": "1000"},
                                "scopeSelector": {
                                    "matchExpressions": [
                                        {
                                            "operator": "In",
                                            "scopeName": "PriorityClass",
                                            "values": ["system-node-critical", "system-cluster-critical"],
                                        }
                                    ]
                                },
                            },
                        }
                    },
                },
            }
        ),
        ready=ready,
    )


def _leader_worker_set() -> fnv1.Resource:
    """The composed LeaderWorkerSet Release, not yet marked ready."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "Release",
                "metadata": {
                    "annotations": {"crossplane.io/external-name": "mp-lws"},
                    "labels": {"modelplane.ai/resource": "leader-worker-set"},
                },
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "chart": {"name": "lws", "repository": "oci://registry.k8s.io/lws/charts", "version": "v0.8.0"},
                        "namespace": "lws-system",
                    },
                },
            }
        ),
        ready=fnv1.READY_UNSPECIFIED,
    )


def _gateway_class(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed envoy GatewayClass, parameterised by the gateway's EnvoyProxy."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"labels": {"modelplane.ai/resource": "gateway-class"}},
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": {
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
                    },
                },
            }
        ),
        ready=ready,
    )


def _gateway(*, hostname: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed Gateway: one HTTPS listener for hostname, terminating TLS with the serving certificate."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"labels": {"modelplane.ai/resource": "gateway"}},
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "gateway.networking.k8s.io/v1",
                            "kind": "Gateway",
                            "metadata": {"name": "cluster-gateway", "namespace": "modelplane-system"},
                            "spec": {
                                "gatewayClassName": "envoy",
                                "listeners": [
                                    {
                                        "name": "https",
                                        "protocol": "HTTPS",
                                        "port": 443,
                                        "hostname": hostname,
                                        "tls": {
                                            "mode": "Terminate",
                                            "certificateRefs": [{"name": "cluster-gateway-serving"}],
                                        },
                                        "allowedRoutes": {
                                            "namespaces": {
                                                "from": "Selector",
                                                "selector": {
                                                    "matchExpressions": [
                                                        {"key": "modelplane.ai/namespace", "operator": "Exists"}
                                                    ]
                                                },
                                            }
                                        },
                                    }
                                ],
                            },
                        }
                    },
                    "readiness": {
                        "policy": "DeriveFromCelQuery",
                        "celQuery": "has(object.status.addresses) && object.status.addresses.size() > 0",
                    },
                },
            }
        ),
        ready=ready,
    )


def _gateway_ca_certificate(*, common_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed cluster CA Certificate, issued by the self-signed Issuer."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "cert-manager.io/v1",
                            "kind": "Certificate",
                            "metadata": {"name": "modelplane-cluster-ca", "namespace": "modelplane-system"},
                            "spec": {
                                "isCA": True,
                                "commonName": common_name,
                                "secretName": "modelplane-cluster-ca",
                                "duration": "87600h",
                                "renewBefore": "8760h",
                                "privateKey": {"algorithm": "ECDSA", "size": 256},
                                "issuerRef": {
                                    "name": "modelplane-selfsigned",
                                    "kind": "Issuer",
                                    "group": "cert-manager.io",
                                },
                            },
                        }
                    },
                    "readiness": {
                        "policy": "DeriveFromCelQuery",
                        "celQuery": "has(object.status) && has(object.status.conditions) && object.status.conditions.exists(c, c.type == 'Ready' && c.status == 'True')",
                    },
                },
            }
        ),
        ready=ready,
    )


def _gateway_ca_issuer(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed Issuer that signs with the cluster CA."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "cert-manager.io/v1",
                            "kind": "Issuer",
                            "metadata": {"name": "modelplane-cluster-ca", "namespace": "modelplane-system"},
                            "spec": {"ca": {"secretName": "modelplane-cluster-ca"}},
                        }
                    },
                },
            }
        ),
        ready=ready,
    )


def _gateway_serving_certificate(*, hostname: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed Certificate the gateway serves for hostname, issued by the cluster CA."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "cert-manager.io/v1",
                            "kind": "Certificate",
                            "metadata": {"name": "cluster-gateway-serving", "namespace": "modelplane-system"},
                            "spec": {
                                "secretName": "cluster-gateway-serving",
                                "dnsNames": [hostname],
                                "duration": "2160h",
                                "renewBefore": "720h",
                                "privateKey": {"algorithm": "ECDSA", "size": 256, "rotationPolicy": "Always"},
                                "issuerRef": {
                                    "name": "modelplane-cluster-ca",
                                    "kind": "Issuer",
                                    "group": "cert-manager.io",
                                },
                            },
                        }
                    },
                    "readiness": {
                        "policy": "DeriveFromCelQuery",
                        "celQuery": "has(object.status) && has(object.status.conditions) && object.status.conditions.exists(c, c.type == 'Ready' && c.status == 'True')",
                    },
                },
            }
        ),
        ready=ready,
    )


def _gateway_ca_bundle(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed trust-manager Bundle republishing the cluster CA's certificate without its key."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "trust.cert-manager.io/v1alpha1",
                            "kind": "Bundle",
                            "metadata": {"name": "modelplane-cluster-ca"},
                            "spec": {
                                "sources": [{"secret": {"name": "modelplane-cluster-ca", "key": "ca.crt"}}],
                                "target": {
                                    "configMap": {"key": "ca.crt"},
                                    "namespaceSelector": {
                                        "matchLabels": {"kubernetes.io/metadata.name": "modelplane-system"}
                                    },
                                },
                            },
                        }
                    },
                    "readiness": {
                        "policy": "DeriveFromCelQuery",
                        "celQuery": "has(object.status) && has(object.status.conditions) && object.status.conditions.exists(c, c.type == 'Synced' && c.status == 'True')",
                    },
                },
            }
        ),
        ready=ready,
    )


def _gateway_ca_configmap(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed Object observing, never managing, the CA ConfigMap trust-manager owns."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "v1",
                            "kind": "ConfigMap",
                            "metadata": {"name": "modelplane-cluster-ca", "namespace": "modelplane-system"},
                        }
                    },
                    "managementPolicies": ["Observe"],
                },
            }
        ),
        ready=ready,
    )


def _gateway_client_ca_bundle(*, ca_crt: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed ConfigMap holding ca_crt, the InferenceGateway CAs the gateway trusts."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "v1",
                            "kind": "ConfigMap",
                            "metadata": {
                                "name": "modelplane-inference-gateway-cas",
                                "namespace": "modelplane-system",
                            },
                            "data": {"ca.crt": ca_crt},
                        }
                    },
                },
            }
        ),
        ready=ready,
    )


def _gateway_client_auth(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed ClientTrafficPolicy demanding a client certificate on the HTTPS listener."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                            "kind": "ClientTrafficPolicy",
                            "metadata": {
                                "name": "cluster-gateway-client-auth",
                                "namespace": "modelplane-system",
                            },
                            "spec": {
                                "targetRefs": [
                                    {
                                        "group": "gateway.networking.k8s.io",
                                        "kind": "Gateway",
                                        "name": "cluster-gateway",
                                        "sectionName": "https",
                                    }
                                ],
                                "tls": {
                                    "clientValidation": {
                                        "caCertificateRefs": [
                                            {
                                                "kind": "ConfigMap",
                                                "group": "",
                                                "name": "modelplane-inference-gateway-cas",
                                            }
                                        ]
                                    }
                                },
                            },
                        }
                    },
                    "readiness": {
                        "policy": "DeriveFromCelQuery",
                        "celQuery": "has(object.status) && has(object.status.ancestors) && object.status.ancestors.exists(a, has(a.conditions) && a.conditions.exists(c, c.type == 'Accepted' && c.status == 'True'))",
                    },
                },
            }
        ),
        ready=ready,
    )


def _usage_cert_manager_by_envoy_gateway() -> fnv1.Resource:
    """The composed Usage holding the cert-manager Release until the envoy-gateway Release is gone."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "spec": {
                    "of": {
                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                        "kind": "Release",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "cert-manager"},
                        },
                    },
                    "by": {
                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                        "kind": "Release",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "envoy-gateway"},
                        },
                    },
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _usage_ai_gateway_crds_by_ai_gateway() -> fnv1.Resource:
    """The composed Usage holding the ai-gateway-crds Release until the ai-gateway Release is gone."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "spec": {
                    "of": {
                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                        "kind": "Release",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "ai-gateway-crds"},
                        },
                    },
                    "by": {
                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                        "kind": "Release",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "ai-gateway"},
                        },
                    },
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _usage_gateway_namespace_by_gateway_proxy() -> fnv1.Resource:
    """The composed Usage holding the gateway-namespace Object until the gateway-proxy Object is gone."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "spec": {
                    "of": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "gateway-namespace"},
                        },
                    },
                    "by": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "gateway-proxy"},
                        },
                    },
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _usage_cert_manager_by_gateway_selfsigned_issuer() -> fnv1.Resource:
    """The composed Usage holding the cert-manager Release until the gateway-selfsigned-issuer Object is gone."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "spec": {
                    "of": {
                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                        "kind": "Release",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "cert-manager"},
                        },
                    },
                    "by": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "gateway-selfsigned-issuer"},
                        },
                    },
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _usage_gateway_namespace_by_gateway_selfsigned_issuer() -> fnv1.Resource:
    """The composed Usage holding the gateway-namespace Object until the gateway-selfsigned-issuer Object is gone."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "spec": {
                    "of": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "gateway-namespace"},
                        },
                    },
                    "by": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "gateway-selfsigned-issuer"},
                        },
                    },
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _usage_gateway_selfsigned_issuer_by_trust_manager() -> fnv1.Resource:
    """The composed Usage holding the gateway-selfsigned-issuer Object until the trust-manager Release is gone."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "spec": {
                    "of": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "gateway-selfsigned-issuer"},
                        },
                    },
                    "by": {
                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                        "kind": "Release",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "trust-manager"},
                        },
                    },
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _usage_kai_scheduler_by_kai_queue_root() -> fnv1.Resource:
    """The composed Usage holding the kai-scheduler Release until the kai-queue-root Object is gone."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "spec": {
                    "of": {
                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                        "kind": "Release",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "kai-scheduler"},
                        },
                    },
                    "by": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "kai-queue-root"},
                        },
                    },
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _usage_kai_scheduler_by_kai_queue() -> fnv1.Resource:
    """The composed Usage holding the kai-scheduler Release until the kai-queue Object is gone."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "spec": {
                    "of": {
                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                        "kind": "Release",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "kai-scheduler"},
                        },
                    },
                    "by": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "kai-queue"},
                        },
                    },
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _usage_modelexpress_crds_modelmetadatas_by_modelexpress_server() -> fnv1.Resource:
    """The composed Usage holding the ModelMetadata CRD Object until the modelexpress-server Object is gone."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "spec": {
                    "of": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {
                                "modelplane.ai/resource": "modelexpress-crds-modelmetadatas.modelexpress.nvidia.com"
                            },
                        },
                    },
                    "by": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "modelexpress-server"},
                        },
                    },
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _usage_modelexpress_crds_modelcacheentries_by_modelexpress_server() -> fnv1.Resource:
    """The composed Usage holding the ModelCacheEntry CRD Object until the modelexpress-server Object is gone."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "spec": {
                    "of": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {
                                "modelplane.ai/resource": "modelexpress-crds-modelcacheentries.modelexpress.nvidia.com"
                            },
                        },
                    },
                    "by": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "modelexpress-server"},
                        },
                    },
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _usage_gateway_class_by_gateway() -> fnv1.Resource:
    """The composed Usage holding the gateway-class Object until the gateway Object is gone."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "spec": {
                    "of": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "gateway-class"},
                        },
                    },
                    "by": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "gateway"},
                        },
                    },
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _usage_envoy_gateway_by_gateway_class() -> fnv1.Resource:
    """The composed Usage holding the envoy-gateway Release until the gateway-class Object is gone."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "spec": {
                    "of": {
                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                        "kind": "Release",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "envoy-gateway"},
                        },
                    },
                    "by": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": "gateway-class"},
                        },
                    },
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


COMPOSE_CASES = [
    # Everything targeting the remote cluster is gated on the ProviderConfigs
    # having been observed; Usages reference nothing remote and compose
    # immediately. The unready ProviderConfigs keep the composite unready until
    # the stack actually renders.
    ComposeCase(
        name="first pass composes only the provider configs and usages",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Existing",
                    stack="Dynamo",
                    secrets=[
                        v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                        v1alpha1.Secret(type="GoogleApplicationCredentials", name="sa-secret", key="private_key"),
                    ],
                    gateway=v1alpha1.Gateway(
                        hostname="test-backend.gateways.example.com",
                        clientCAs=[
                            v1alpha1.ClientCA(
                                name="eu",
                                certificate="-----BEGIN CERTIFICATE-----\nfleet\n-----END CERTIFICATE-----\n",
                            )
                        ],
                    ),
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_serving_stack(gateway=None),
                resources={
                    "provider-config-kubernetes": _kubernetes_provider_config(
                        identity={
                            "type": "GoogleApplicationCredentials",
                            "source": "Secret",
                            "secretRef": {"name": "sa-secret", "namespace": "test-ns", "key": "private_key"},
                        },
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-helm": _helm_provider_config(
                        identity={
                            "type": "GoogleApplicationCredentials",
                            "source": "Secret",
                            "secretRef": {"name": "sa-secret", "namespace": "test-ns", "key": "private_key"},
                        },
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    # One Usage per depends_on edge in the joined stack data, then the
                    # two hand-written edges of the gateway chain.
                    "usage-cert-manager-by-envoy-gateway": _usage_cert_manager_by_envoy_gateway(),
                    "usage-ai-gateway-crds-by-ai-gateway": _usage_ai_gateway_crds_by_ai_gateway(),
                    "usage-gateway-namespace-by-gateway-proxy": _usage_gateway_namespace_by_gateway_proxy(),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _usage_cert_manager_by_gateway_selfsigned_issuer(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _usage_gateway_namespace_by_gateway_selfsigned_issuer(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _usage_gateway_selfsigned_issuer_by_trust_manager(),
                    "usage-kai-scheduler-by-kai-queue-root": _usage_kai_scheduler_by_kai_queue_root(),
                    "usage-kai-scheduler-by-kai-queue": _usage_kai_scheduler_by_kai_queue(),
                    "usage-modelexpress-crds-modelmetadatas.modelexpress.nvidia.com-by-modelexpress-server": _usage_modelexpress_crds_modelmetadatas_by_modelexpress_server(),
                    "usage-modelexpress-crds-modelcacheentries.modelexpress.nvidia.com-by-modelexpress-server": _usage_modelexpress_crds_modelcacheentries_by_modelexpress_server(),
                    "usage-gateway-class-by-gateway": _usage_gateway_class_by_gateway(),
                    "usage-envoy-gateway-by-gateway-class": _usage_envoy_gateway_by_gateway_class(),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # The ProviderConfigs are observed, which opens the gate on the rest of the
    # stack. depends_on gates first creation too, so only the dependency-free
    # wave renders. Each dependent waits for its dependencies' Ready before it's
    # first created: envoy-gateway on cert-manager, ai-gateway on
    # ai-gateway-crds, gateway-proxy on gateway-namespace, kai-queue-root and
    # kai-queue on kai-scheduler, modelexpress-server on modelexpress-crds,
    # gateway-selfsigned-issuer on cert-manager and gateway-namespace, and
    # trust-manager on gateway-selfsigned-issuer.
    ComposeCase(
        name="second pass renders the dependency-free wave",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Existing",
                    stack="Dynamo",
                    secrets=[
                        v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                        v1alpha1.Secret(type="GoogleApplicationCredentials", name="sa-secret", key="private_key"),
                    ],
                    gateway=v1alpha1.Gateway(
                        hostname="test-backend.gateways.example.com",
                        clientCAs=[
                            v1alpha1.ClientCA(
                                name="eu",
                                certificate="-----BEGIN CERTIFICATE-----\nfleet\n-----END CERTIFICATE-----\n",
                            )
                        ],
                    ),
                ),
                resources={
                    "provider-config-kubernetes": _observed_kubernetes_provider_config(),
                    "provider-config-helm": _observed_helm_provider_config(),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_serving_stack(gateway=None),
                resources={
                    "provider-config-kubernetes": _kubernetes_provider_config(
                        identity={
                            "type": "GoogleApplicationCredentials",
                            "source": "Secret",
                            "secretRef": {"name": "sa-secret", "namespace": "test-ns", "key": "private_key"},
                        },
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-helm": _helm_provider_config(
                        identity={
                            "type": "GoogleApplicationCredentials",
                            "source": "Secret",
                            "secretRef": {"name": "sa-secret", "namespace": "test-ns", "key": "private_key"},
                        },
                        ready=fnv1.READY_TRUE,
                    ),
                    "cert-manager": _cert_manager(ready=fnv1.READY_UNSPECIFIED),
                    "kube-prometheus-stack": _kube_prometheus_stack(ready=fnv1.READY_UNSPECIFIED),
                    "node-feature-discovery": _node_feature_discovery(ready=fnv1.READY_UNSPECIFIED),
                    "nvidia-dra-driver-gpu": _nvidia_dra_driver_gpu(ready=fnv1.READY_UNSPECIFIED),
                    "ai-gateway-crds": _ai_gateway_crds(ready=fnv1.READY_UNSPECIFIED),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _gaie_crds_inferenceobjectives_x_k8s(
                        ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _gaie_crds_inferencepools_k8s(
                        ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _gaie_crds_inferencepools_x_k8s(
                        ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gateway-namespace": _gateway_namespace(ready=fnv1.READY_UNSPECIFIED),
                    "dra-driver-critical-pods-quota": _dra_driver_critical_pods_quota(ready=fnv1.READY_UNSPECIFIED),
                    "grove": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "mp-grove-charts"},
                                    "labels": {"modelplane.ai/resource": "grove"},
                                },
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "chart": {
                                            "name": "grove-charts",
                                            "repository": "oci://ghcr.io/ai-dynamo/grove",
                                            "version": "v0.1.0-alpha.12-rc2",
                                        },
                                        "namespace": "grove-system",
                                    },
                                },
                            }
                        )
                    ),
                    "kai-scheduler": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "mp-kai-scheduler"},
                                    "labels": {"modelplane.ai/resource": "kai-scheduler"},
                                },
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "chart": {
                                            "name": "kai-scheduler",
                                            "repository": "oci://ghcr.io/kai-scheduler/kai-scheduler",
                                            "version": "v0.16.8",
                                        },
                                        "namespace": "kai-scheduler",
                                        "wait": True,
                                        "waitTimeout": "10m",
                                    },
                                },
                            }
                        )
                    ),
                    "modelexpress-crds-modelmetadatas.modelexpress.nvidia.com": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {
                                    "labels": {
                                        "modelplane.ai/resource": "modelexpress-crds-modelmetadatas.modelexpress.nvidia.com"
                                    }
                                },
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "manifest": _crd(
                                            filename="modelexpress.yaml", name="modelmetadatas.modelexpress.nvidia.com"
                                        )
                                    },
                                },
                            }
                        )
                    ),
                    "modelexpress-crds-modelcacheentries.modelexpress.nvidia.com": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {
                                    "labels": {
                                        "modelplane.ai/resource": "modelexpress-crds-modelcacheentries.modelexpress.nvidia.com"
                                    }
                                },
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "manifest": _crd(
                                            filename="modelexpress.yaml",
                                            name="modelcacheentries.modelexpress.nvidia.com",
                                        )
                                    },
                                },
                            }
                        )
                    ),
                    "modelexpress-server-sa": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"labels": {"modelplane.ai/resource": "modelexpress-server-sa"}},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "ServiceAccount",
                                            "metadata": {"name": "modelexpress-server", "namespace": "default"},
                                        }
                                    },
                                },
                            }
                        )
                    ),
                    "modelexpress-server-role": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"labels": {"modelplane.ai/resource": "modelexpress-server-role"}},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "rbac.authorization.k8s.io/v1",
                                            "kind": "Role",
                                            "metadata": {"name": "modelexpress-server", "namespace": "default"},
                                            "rules": [
                                                {
                                                    "apiGroups": ["modelexpress.nvidia.com"],
                                                    "resources": ["modelmetadatas", "modelmetadatas/status"],
                                                    "verbs": ["get", "list", "create", "update", "patch", "delete"],
                                                },
                                                {
                                                    "apiGroups": [""],
                                                    "resources": ["configmaps"],
                                                    "verbs": ["get", "list", "create", "update", "patch", "delete"],
                                                },
                                                {
                                                    "apiGroups": ["modelexpress.nvidia.com"],
                                                    "resources": ["modelcacheentries", "modelcacheentries/status"],
                                                    "verbs": ["get", "list", "create", "update", "patch", "delete"],
                                                },
                                            ],
                                        }
                                    },
                                },
                            }
                        )
                    ),
                    "modelexpress-server-rolebinding": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"labels": {"modelplane.ai/resource": "modelexpress-server-rolebinding"}},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "rbac.authorization.k8s.io/v1",
                                            "kind": "RoleBinding",
                                            "metadata": {"name": "modelexpress-server", "namespace": "default"},
                                            "subjects": [
                                                {
                                                    "kind": "ServiceAccount",
                                                    "name": "modelexpress-server",
                                                    "namespace": "default",
                                                }
                                            ],
                                            "roleRef": {
                                                "apiGroup": "rbac.authorization.k8s.io",
                                                "kind": "Role",
                                                "name": "modelexpress-server",
                                            },
                                        }
                                    },
                                },
                            }
                        )
                    ),
                    "modelexpress-server-svc": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"labels": {"modelplane.ai/resource": "modelexpress-server-svc"}},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "Service",
                                            "metadata": {"name": "modelexpress-server", "namespace": "default"},
                                            "spec": {
                                                "selector": {"modelplane.ai/modelexpress": "modelexpress-server"},
                                                "ports": [{"name": "grpc", "port": 8001, "targetPort": 8001}],
                                            },
                                        }
                                    },
                                },
                            }
                        )
                    ),
                    "gateway-class": _gateway_class(ready=fnv1.READY_UNSPECIFIED),
                    "gateway": _gateway(hostname="test-backend.gateways.example.com", ready=fnv1.READY_UNSPECIFIED),
                    "gateway-ca-certificate": _gateway_ca_certificate(
                        common_name="modelplane cluster CA test-backend.gateways.example.com",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "gateway-ca-issuer": _gateway_ca_issuer(ready=fnv1.READY_UNSPECIFIED),
                    "gateway-serving-certificate": _gateway_serving_certificate(
                        hostname="test-backend.gateways.example.com", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gateway-ca-bundle": _gateway_ca_bundle(ready=fnv1.READY_UNSPECIFIED),
                    "gateway-ca-configmap": _gateway_ca_configmap(ready=fnv1.READY_UNSPECIFIED),
                    "gateway-client-ca-bundle": _gateway_client_ca_bundle(
                        ca_crt="-----BEGIN CERTIFICATE-----\nfleet\n-----END CERTIFICATE-----\n",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "gateway-client-auth": _gateway_client_auth(ready=fnv1.READY_UNSPECIFIED),
                    "usage-cert-manager-by-envoy-gateway": _usage_cert_manager_by_envoy_gateway(),
                    "usage-ai-gateway-crds-by-ai-gateway": _usage_ai_gateway_crds_by_ai_gateway(),
                    "usage-gateway-namespace-by-gateway-proxy": _usage_gateway_namespace_by_gateway_proxy(),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _usage_cert_manager_by_gateway_selfsigned_issuer(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _usage_gateway_namespace_by_gateway_selfsigned_issuer(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _usage_gateway_selfsigned_issuer_by_trust_manager(),
                    "usage-kai-scheduler-by-kai-queue-root": _usage_kai_scheduler_by_kai_queue_root(),
                    "usage-kai-scheduler-by-kai-queue": _usage_kai_scheduler_by_kai_queue(),
                    "usage-modelexpress-crds-modelmetadatas.modelexpress.nvidia.com-by-modelexpress-server": _usage_modelexpress_crds_modelmetadatas_by_modelexpress_server(),
                    "usage-modelexpress-crds-modelcacheentries.modelexpress.nvidia.com-by-modelexpress-server": _usage_modelexpress_crds_modelcacheentries_by_modelexpress_server(),
                    "usage-gateway-class-by-gateway": _usage_gateway_class_by_gateway(),
                    "usage-envoy-gateway-by-gateway-class": _usage_envoy_gateway_by_gateway_class(),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # Every rendered resource is observed Ready, and the gateway has its address
    # assigned. So everything is marked ready, and the address lands in the XR
    # status. The ProviderConfigs are ready because they're observed, and the
    # Usages on arrival.
    ComposeCase(
        name="all dependencies ready renders the whole stack, marks it ready, and writes the gateway address",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Existing",
                    stack="Dynamo",
                    secrets=[
                        v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                        v1alpha1.Secret(type="GoogleApplicationCredentials", name="sa-secret", key="private_key"),
                    ],
                    gateway=v1alpha1.Gateway(
                        hostname="test-backend.gateways.example.com",
                        clientCAs=[
                            v1alpha1.ClientCA(
                                name="eu",
                                certificate="-----BEGIN CERTIFICATE-----\nfleet\n-----END CERTIFICATE-----\n",
                            )
                        ],
                    ),
                ),
                resources={
                    "provider-config-kubernetes": _observed_kubernetes_provider_config(),
                    "provider-config-helm": _observed_helm_provider_config(),
                    "cert-manager": _observed_ready(),
                    "kube-prometheus-stack": _observed_ready(),
                    "node-feature-discovery": _observed_ready(),
                    "nvidia-dra-driver-gpu": _observed_ready(),
                    "envoy-gateway": _observed_ready(),
                    "ai-gateway-crds": _observed_ready(),
                    "ai-gateway": _observed_ready(),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _observed_ready(),
                    "gateway-namespace": _observed_ready(),
                    "gateway-proxy": _observed_ready(),
                    "gateway-selfsigned-issuer": _observed_ready(),
                    "trust-manager": _observed_ready(),
                    "dra-driver-critical-pods-quota": _observed_ready(),
                    "grove": _observed_ready(),
                    "kai-scheduler": _observed_ready(),
                    "kai-queue-root": _observed_ready(),
                    "kai-queue": _observed_ready(),
                    "modelexpress-crds-modelmetadatas.modelexpress.nvidia.com": _observed_ready(),
                    "modelexpress-crds-modelcacheentries.modelexpress.nvidia.com": _observed_ready(),
                    "modelexpress-server-sa": _observed_ready(),
                    "modelexpress-server-role": _observed_ready(),
                    "modelexpress-server-rolebinding": _observed_ready(),
                    "modelexpress-server-svc": _observed_ready(),
                    "modelexpress-server": _observed_ready(),
                    "gateway-class": _observed_ready(),
                    "gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "status": {
                                    "conditions": [{"type": "Ready", "status": "True"}],
                                    "atProvider": {
                                        "manifest": {
                                            "status": {"addresses": [{"type": "IPAddress", "value": "203.0.113.7"}]}
                                        }
                                    },
                                }
                            }
                        )
                    ),
                    "gateway-ca-certificate": _observed_ready(),
                    "gateway-ca-issuer": _observed_ready(),
                    "gateway-serving-certificate": _observed_ready(),
                    "gateway-ca-bundle": _observed_ready(),
                    "gateway-ca-configmap": _observed_ready(),
                    "gateway-client-ca-bundle": _observed_ready(),
                    "gateway-client-auth": _observed_ready(),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_serving_stack(gateway={"address": "203.0.113.7"}),
                resources={
                    "provider-config-kubernetes": _kubernetes_provider_config(
                        identity={
                            "type": "GoogleApplicationCredentials",
                            "source": "Secret",
                            "secretRef": {"name": "sa-secret", "namespace": "test-ns", "key": "private_key"},
                        },
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-helm": _helm_provider_config(
                        identity={
                            "type": "GoogleApplicationCredentials",
                            "source": "Secret",
                            "secretRef": {"name": "sa-secret", "namespace": "test-ns", "key": "private_key"},
                        },
                        ready=fnv1.READY_TRUE,
                    ),
                    "cert-manager": _cert_manager(ready=fnv1.READY_TRUE),
                    "kube-prometheus-stack": _kube_prometheus_stack(ready=fnv1.READY_TRUE),
                    "node-feature-discovery": _node_feature_discovery(ready=fnv1.READY_TRUE),
                    "nvidia-dra-driver-gpu": _nvidia_dra_driver_gpu(ready=fnv1.READY_TRUE),
                    "envoy-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "mp-gateway-helm"},
                                    "labels": {"modelplane.ai/resource": "envoy-gateway"},
                                },
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "chart": {
                                            "name": "gateway-helm",
                                            "repository": "oci://docker.io/envoyproxy",
                                            "version": "v1.8.4",
                                        },
                                        "namespace": "envoy-gateway-system",
                                        "values": {
                                            "config": {
                                                "envoyGateway": {
                                                    "extensionApis": {"enableBackend": True},
                                                    "extensionManager": {
                                                        "hooks": {
                                                            "xdsTranslator": {
                                                                "translation": {
                                                                    "listener": {"includeAll": True},
                                                                    "route": {"includeAll": True},
                                                                    "cluster": {"includeAll": True},
                                                                    "secret": {"includeAll": True},
                                                                },
                                                                "post": ["Translation", "Cluster", "Route"],
                                                            }
                                                        },
                                                        "service": {
                                                            "fqdn": {
                                                                "hostname": "ai-gateway-controller.envoy-ai-gateway-system.svc.cluster.local",
                                                                "port": 1063,
                                                            }
                                                        },
                                                        "backendResources": [
                                                            {
                                                                "group": "inference.networking.k8s.io",
                                                                "kind": "InferencePool",
                                                                "version": "v1",
                                                            }
                                                        ],
                                                    },
                                                }
                                            }
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "ai-gateway-crds": _ai_gateway_crds(ready=fnv1.READY_TRUE),
                    "ai-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "mp-ai-gateway-helm"},
                                    "labels": {"modelplane.ai/resource": "ai-gateway"},
                                },
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "chart": {
                                            "name": "ai-gateway-helm",
                                            "repository": "oci://docker.io/envoyproxy",
                                            "version": "v1.1.0",
                                        },
                                        "namespace": "envoy-ai-gateway-system",
                                        "values": {
                                            "controller": {"logRequestHeaderAttributes": "x-modelplane-caller:caller"}
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _gaie_crds_inferenceobjectives_x_k8s(
                        ready=fnv1.READY_TRUE
                    ),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _gaie_crds_inferencepools_k8s(
                        ready=fnv1.READY_TRUE
                    ),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _gaie_crds_inferencepools_x_k8s(
                        ready=fnv1.READY_TRUE
                    ),
                    "gateway-namespace": _gateway_namespace(ready=fnv1.READY_TRUE),
                    "gateway-proxy": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"labels": {"modelplane.ai/resource": "gateway-proxy"}},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                                            "kind": "EnvoyProxy",
                                            "metadata": {"name": "cluster-gateway", "namespace": "modelplane-system"},
                                            "spec": {
                                                "provider": {
                                                    "type": "Kubernetes",
                                                    "kubernetes": {
                                                        "envoyService": {"externalTrafficPolicy": "Cluster"}
                                                    },
                                                }
                                            },
                                        }
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "gateway-selfsigned-issuer": _gateway_selfsigned_issuer(),
                    "trust-manager": _trust_manager(ready=fnv1.READY_TRUE),
                    "dra-driver-critical-pods-quota": _dra_driver_critical_pods_quota(ready=fnv1.READY_TRUE),
                    "grove": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "mp-grove-charts"},
                                    "labels": {"modelplane.ai/resource": "grove"},
                                },
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "chart": {
                                            "name": "grove-charts",
                                            "repository": "oci://ghcr.io/ai-dynamo/grove",
                                            "version": "v0.1.0-alpha.12-rc2",
                                        },
                                        "namespace": "grove-system",
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "kai-scheduler": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "mp-kai-scheduler"},
                                    "labels": {"modelplane.ai/resource": "kai-scheduler"},
                                },
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "chart": {
                                            "name": "kai-scheduler",
                                            "repository": "oci://ghcr.io/kai-scheduler/kai-scheduler",
                                            "version": "v0.16.8",
                                        },
                                        "namespace": "kai-scheduler",
                                        "wait": True,
                                        "waitTimeout": "10m",
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "kai-queue-root": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"labels": {"modelplane.ai/resource": "kai-queue-root"}},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "scheduling.run.ai/v2",
                                            "kind": "Queue",
                                            "metadata": {"name": "modelplane-root"},
                                            "spec": {
                                                "resources": {
                                                    "cpu": {"quota": -1, "limit": -1, "overQuotaWeight": 1},
                                                    "gpu": {"quota": -1, "limit": -1, "overQuotaWeight": 1},
                                                    "memory": {"quota": -1, "limit": -1, "overQuotaWeight": 1},
                                                }
                                            },
                                        }
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "kai-queue": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"labels": {"modelplane.ai/resource": "kai-queue"}},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "scheduling.run.ai/v2",
                                            "kind": "Queue",
                                            "metadata": {"name": "modelplane"},
                                            "spec": {
                                                "resources": {
                                                    "cpu": {"quota": -1, "limit": -1, "overQuotaWeight": 1},
                                                    "gpu": {"quota": -1, "limit": -1, "overQuotaWeight": 1},
                                                    "memory": {"quota": -1, "limit": -1, "overQuotaWeight": 1},
                                                },
                                                "parentQueue": "modelplane-root",
                                            },
                                        }
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "modelexpress-crds-modelmetadatas.modelexpress.nvidia.com": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {
                                    "labels": {
                                        "modelplane.ai/resource": "modelexpress-crds-modelmetadatas.modelexpress.nvidia.com"
                                    }
                                },
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "manifest": _crd(
                                            filename="modelexpress.yaml", name="modelmetadatas.modelexpress.nvidia.com"
                                        )
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "modelexpress-crds-modelcacheentries.modelexpress.nvidia.com": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {
                                    "labels": {
                                        "modelplane.ai/resource": "modelexpress-crds-modelcacheentries.modelexpress.nvidia.com"
                                    }
                                },
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "manifest": _crd(
                                            filename="modelexpress.yaml",
                                            name="modelcacheentries.modelexpress.nvidia.com",
                                        )
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "modelexpress-server-sa": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"labels": {"modelplane.ai/resource": "modelexpress-server-sa"}},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "ServiceAccount",
                                            "metadata": {"name": "modelexpress-server", "namespace": "default"},
                                        }
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "modelexpress-server-role": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"labels": {"modelplane.ai/resource": "modelexpress-server-role"}},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "rbac.authorization.k8s.io/v1",
                                            "kind": "Role",
                                            "metadata": {"name": "modelexpress-server", "namespace": "default"},
                                            "rules": [
                                                {
                                                    "apiGroups": ["modelexpress.nvidia.com"],
                                                    "resources": ["modelmetadatas", "modelmetadatas/status"],
                                                    "verbs": ["get", "list", "create", "update", "patch", "delete"],
                                                },
                                                {
                                                    "apiGroups": [""],
                                                    "resources": ["configmaps"],
                                                    "verbs": ["get", "list", "create", "update", "patch", "delete"],
                                                },
                                                {
                                                    "apiGroups": ["modelexpress.nvidia.com"],
                                                    "resources": ["modelcacheentries", "modelcacheentries/status"],
                                                    "verbs": ["get", "list", "create", "update", "patch", "delete"],
                                                },
                                            ],
                                        }
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "modelexpress-server-rolebinding": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"labels": {"modelplane.ai/resource": "modelexpress-server-rolebinding"}},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "rbac.authorization.k8s.io/v1",
                                            "kind": "RoleBinding",
                                            "metadata": {"name": "modelexpress-server", "namespace": "default"},
                                            "subjects": [
                                                {
                                                    "kind": "ServiceAccount",
                                                    "name": "modelexpress-server",
                                                    "namespace": "default",
                                                }
                                            ],
                                            "roleRef": {
                                                "apiGroup": "rbac.authorization.k8s.io",
                                                "kind": "Role",
                                                "name": "modelexpress-server",
                                            },
                                        }
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "modelexpress-server-svc": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"labels": {"modelplane.ai/resource": "modelexpress-server-svc"}},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "Service",
                                            "metadata": {"name": "modelexpress-server", "namespace": "default"},
                                            "spec": {
                                                "selector": {"modelplane.ai/modelexpress": "modelexpress-server"},
                                                "ports": [{"name": "grpc", "port": 8001, "targetPort": 8001}],
                                            },
                                        }
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "modelexpress-server": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"labels": {"modelplane.ai/resource": "modelexpress-server"}},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "apps/v1",
                                            "kind": "Deployment",
                                            "metadata": {"name": "modelexpress-server", "namespace": "default"},
                                            "spec": {
                                                "replicas": 1,
                                                "selector": {
                                                    "matchLabels": {"modelplane.ai/modelexpress": "modelexpress-server"}
                                                },
                                                "template": {
                                                    "metadata": {
                                                        "labels": {"modelplane.ai/modelexpress": "modelexpress-server"}
                                                    },
                                                    "spec": {
                                                        "serviceAccountName": "modelexpress-server",
                                                        "containers": [
                                                            {
                                                                "name": "modelexpress-server",
                                                                "image": "nvcr.io/nvidia/ai-dynamo/modelexpress-server:0.4.1",
                                                                "ports": [{"containerPort": 8001}],
                                                                "env": [
                                                                    {
                                                                        "name": "MODEL_EXPRESS_CACHE_DIRECTORY",
                                                                        "value": "/mnt/models",
                                                                    },
                                                                    {"name": "HF_HUB_CACHE", "value": "/mnt/models"},
                                                                    {
                                                                        "name": "MX_METADATA_BACKEND",
                                                                        "value": "kubernetes",
                                                                    },
                                                                    {
                                                                        "name": "POD_NAMESPACE",
                                                                        "valueFrom": {
                                                                            "fieldRef": {
                                                                                "fieldPath": "metadata.namespace"
                                                                            }
                                                                        },
                                                                    },
                                                                ],
                                                                "volumeMounts": [
                                                                    {"name": "cache", "mountPath": "/mnt/models"}
                                                                ],
                                                                "readinessProbe": {
                                                                    "tcpSocket": {"port": 8001},
                                                                    "periodSeconds": 10,
                                                                },
                                                                "livenessProbe": {
                                                                    "tcpSocket": {"port": 8001},
                                                                    "periodSeconds": 20,
                                                                },
                                                            }
                                                        ],
                                                        "volumes": [{"name": "cache", "emptyDir": {}}],
                                                    },
                                                },
                                            },
                                        }
                                    },
                                    "readiness": {
                                        "policy": "DeriveFromCelQuery",
                                        "celQuery": 'has(object.status.conditions) && object.status.conditions.exists(c, c.type == "Available" && c.status == "True")',
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "gateway-class": _gateway_class(ready=fnv1.READY_TRUE),
                    "gateway": _gateway(hostname="test-backend.gateways.example.com", ready=fnv1.READY_TRUE),
                    "gateway-ca-certificate": _gateway_ca_certificate(
                        common_name="modelplane cluster CA test-backend.gateways.example.com", ready=fnv1.READY_TRUE
                    ),
                    "gateway-ca-issuer": _gateway_ca_issuer(ready=fnv1.READY_TRUE),
                    "gateway-serving-certificate": _gateway_serving_certificate(
                        hostname="test-backend.gateways.example.com", ready=fnv1.READY_TRUE
                    ),
                    "gateway-ca-bundle": _gateway_ca_bundle(ready=fnv1.READY_TRUE),
                    "gateway-ca-configmap": _gateway_ca_configmap(ready=fnv1.READY_TRUE),
                    "gateway-client-ca-bundle": _gateway_client_ca_bundle(
                        ca_crt="-----BEGIN CERTIFICATE-----\nfleet\n-----END CERTIFICATE-----\n", ready=fnv1.READY_TRUE
                    ),
                    "gateway-client-auth": _gateway_client_auth(ready=fnv1.READY_TRUE),
                    "usage-cert-manager-by-envoy-gateway": _usage_cert_manager_by_envoy_gateway(),
                    "usage-ai-gateway-crds-by-ai-gateway": _usage_ai_gateway_crds_by_ai_gateway(),
                    "usage-gateway-namespace-by-gateway-proxy": _usage_gateway_namespace_by_gateway_proxy(),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _usage_cert_manager_by_gateway_selfsigned_issuer(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _usage_gateway_namespace_by_gateway_selfsigned_issuer(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _usage_gateway_selfsigned_issuer_by_trust_manager(),
                    "usage-kai-scheduler-by-kai-queue-root": _usage_kai_scheduler_by_kai_queue_root(),
                    "usage-kai-scheduler-by-kai-queue": _usage_kai_scheduler_by_kai_queue(),
                    "usage-modelexpress-crds-modelmetadatas.modelexpress.nvidia.com-by-modelexpress-server": _usage_modelexpress_crds_modelmetadatas_by_modelexpress_server(),
                    "usage-modelexpress-crds-modelcacheentries.modelexpress.nvidia.com-by-modelexpress-server": _usage_modelexpress_crds_modelcacheentries_by_modelexpress_server(),
                    "usage-gateway-class-by-gateway": _usage_gateway_class_by_gateway(),
                    "usage-envoy-gateway-by-gateway-class": _usage_envoy_gateway_by_gateway_class(),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # Both ProviderConfigs stamp the identity type as is rather than forcing
    # GoogleApplicationCredentials, and the secret's own namespace wins over the
    # XR's.
    ComposeCase(
        name="a non-GCP identity secret's type and namespace reach both ProviderConfigs verbatim",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Nebius",
                    stack="Standard",
                    secrets=[
                        v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                        v1alpha1.Secret(
                            type="NebiusServiceAccountCredentials",
                            name="nebius-secret",
                            key="credentials.json",
                            namespace="other-ns",
                        ),
                    ],
                    gateway=v1alpha1.Gateway(hostname="test-backend.gateways.example.com"),
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_serving_stack(gateway=None),
                resources={
                    "provider-config-kubernetes": _kubernetes_provider_config(
                        identity={
                            "type": "NebiusServiceAccountCredentials",
                            "source": "Secret",
                            "secretRef": {"name": "nebius-secret", "namespace": "other-ns", "key": "credentials.json"},
                        },
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-helm": _helm_provider_config(
                        identity={
                            "type": "NebiusServiceAccountCredentials",
                            "source": "Secret",
                            "secretRef": {"name": "nebius-secret", "namespace": "other-ns", "key": "credentials.json"},
                        },
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "usage-cert-manager-by-envoy-gateway": _usage_cert_manager_by_envoy_gateway(),
                    "usage-ai-gateway-crds-by-ai-gateway": _usage_ai_gateway_crds_by_ai_gateway(),
                    "usage-gateway-namespace-by-gateway-proxy": _usage_gateway_namespace_by_gateway_proxy(),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _usage_cert_manager_by_gateway_selfsigned_issuer(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _usage_gateway_namespace_by_gateway_selfsigned_issuer(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _usage_gateway_selfsigned_issuer_by_trust_manager(),
                    "usage-envoy-gateway-by-gateway-class": _usage_envoy_gateway_by_gateway_class(),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message="Gateway test-backend.gateways.example.com not served: no InferenceGateway has published a client CA for this cluster to trust, and serving without one would accept unauthenticated callers",
                ),
            ],
            context=structpb.Struct(),
        ),
    ),
    # The ProviderConfigs are observed, the self-signed Issuer trust-manager
    # depends on is Ready, and the CA ConfigMap trust-manager syncs carries the
    # certificate back for status.
    ComposeCase(
        name="a cluster with an InferenceGateway CA composes its own PKI and serves mTLS",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Existing",
                    stack="Standard",
                    secrets=[v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig")],
                    gateway=v1alpha1.Gateway(
                        # A full Service FQDN, so the CA certificate's commonName overflows
                        # the 64-byte X.509 limit.
                        hostname="gateway-test-backend-12345.modelplane-system.svc.cluster.local",
                        # Deliberately out of name order, to prove the bundle sorts before concatenating.
                        clientCAs=[
                            v1alpha1.ClientCA(name="fleet-b", certificate="BBB"),
                            v1alpha1.ClientCA(name="fleet-a", certificate="AAA"),
                        ],
                    ),
                ),
                resources={
                    "provider-config-kubernetes": _observed_kubernetes_provider_config(),
                    "provider-config-helm": _observed_helm_provider_config(),
                    "gateway-selfsigned-issuer": _observed_ready(),
                    "gateway-ca-configmap": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {"status": {"atProvider": {"manifest": {"data": {"ca.crt": "CLUSTERCA"}}}}}
                        )
                    ),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                # The cluster's CA, published for InferenceGateways to trust.
                composite=_desired_serving_stack(gateway={"caCertificate": "CLUSTERCA"}),
                resources={
                    "provider-config-kubernetes": _kubernetes_provider_config(identity=None, ready=fnv1.READY_TRUE),
                    "provider-config-helm": _helm_provider_config(identity=None, ready=fnv1.READY_TRUE),
                    "cert-manager": _cert_manager(ready=fnv1.READY_UNSPECIFIED),
                    "kube-prometheus-stack": _kube_prometheus_stack(ready=fnv1.READY_UNSPECIFIED),
                    "node-feature-discovery": _node_feature_discovery(ready=fnv1.READY_UNSPECIFIED),
                    "nvidia-dra-driver-gpu": _nvidia_dra_driver_gpu(ready=fnv1.READY_UNSPECIFIED),
                    "ai-gateway-crds": _ai_gateway_crds(ready=fnv1.READY_UNSPECIFIED),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _gaie_crds_inferenceobjectives_x_k8s(
                        ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _gaie_crds_inferencepools_k8s(
                        ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _gaie_crds_inferencepools_x_k8s(
                        ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gateway-namespace": _gateway_namespace(ready=fnv1.READY_UNSPECIFIED),
                    "gateway-selfsigned-issuer": _gateway_selfsigned_issuer(),
                    "trust-manager": _trust_manager(ready=fnv1.READY_UNSPECIFIED),
                    "dra-driver-critical-pods-quota": _dra_driver_critical_pods_quota(ready=fnv1.READY_UNSPECIFIED),
                    "leader-worker-set": _leader_worker_set(),
                    "gateway-class": _gateway_class(ready=fnv1.READY_UNSPECIFIED),
                    "gateway": _gateway(
                        hostname="gateway-test-backend-12345.modelplane-system.svc.cluster.local",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    # The commonName is truncated to the 64-byte X.509 limit.
                    "gateway-ca-certificate": _gateway_ca_certificate(
                        common_name="modelplane cluster CA gateway-test-backend-12345.modelplane-syst",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "gateway-ca-issuer": _gateway_ca_issuer(ready=fnv1.READY_UNSPECIFIED),
                    "gateway-serving-certificate": _gateway_serving_certificate(
                        hostname="gateway-test-backend-12345.modelplane-system.svc.cluster.local",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "gateway-ca-bundle": _gateway_ca_bundle(ready=fnv1.READY_UNSPECIFIED),
                    "gateway-ca-configmap": _gateway_ca_configmap(ready=fnv1.READY_UNSPECIFIED),
                    # Every InferenceGateway's CA, sorted by name and concatenated.
                    "gateway-client-ca-bundle": _gateway_client_ca_bundle(
                        ca_crt="AAA\nBBB\n", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gateway-client-auth": _gateway_client_auth(ready=fnv1.READY_UNSPECIFIED),
                    "usage-cert-manager-by-envoy-gateway": _usage_cert_manager_by_envoy_gateway(),
                    "usage-ai-gateway-crds-by-ai-gateway": _usage_ai_gateway_crds_by_ai_gateway(),
                    "usage-gateway-namespace-by-gateway-proxy": _usage_gateway_namespace_by_gateway_proxy(),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _usage_cert_manager_by_gateway_selfsigned_issuer(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _usage_gateway_namespace_by_gateway_selfsigned_issuer(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _usage_gateway_selfsigned_issuer_by_trust_manager(),
                    "usage-gateway-class-by-gateway": _usage_gateway_class_by_gateway(),
                    "usage-envoy-gateway-by-gateway-class": _usage_envoy_gateway_by_gateway_class(),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # Every PKI resource must be tracked for readiness: mark_readiness marks only
    # the keys compose_gateway_pki returns, so one composed but not returned
    # would silently hold the cluster un-Ready. Each is observed Ready here, so a
    # key dropped from the rendered list fails this case.
    ComposeCase(
        name="every gateway PKI resource observed Ready is marked ready",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Existing",
                    stack="Standard",
                    secrets=[v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig")],
                    gateway=v1alpha1.Gateway(
                        hostname="gateway-test-backend-12345.modelplane-system.svc.cluster.local",
                        clientCAs=[
                            v1alpha1.ClientCA(name="fleet-b", certificate="BBB"),
                            v1alpha1.ClientCA(name="fleet-a", certificate="AAA"),
                        ],
                    ),
                ),
                resources={
                    "provider-config-kubernetes": _observed_kubernetes_provider_config(),
                    "provider-config-helm": _observed_helm_provider_config(),
                    "gateway-selfsigned-issuer": _observed_ready(),
                    "gateway-ca-certificate": _observed_ready(),
                    "gateway-ca-issuer": _observed_ready(),
                    "gateway-serving-certificate": _observed_ready(),
                    "gateway-ca-bundle": _observed_ready(),
                    # The CA ConfigMap's data, alongside its Ready condition.
                    "gateway-ca-configmap": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "status": {
                                    "conditions": [{"type": "Ready", "status": "True"}],
                                    "atProvider": {"manifest": {"data": {"ca.crt": "CLUSTERCA"}}},
                                }
                            }
                        )
                    ),
                    "gateway-client-ca-bundle": _observed_ready(),
                    "gateway-client-auth": _observed_ready(),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_serving_stack(gateway={"caCertificate": "CLUSTERCA"}),
                resources={
                    "provider-config-kubernetes": _kubernetes_provider_config(identity=None, ready=fnv1.READY_TRUE),
                    "provider-config-helm": _helm_provider_config(identity=None, ready=fnv1.READY_TRUE),
                    "cert-manager": _cert_manager(ready=fnv1.READY_UNSPECIFIED),
                    "kube-prometheus-stack": _kube_prometheus_stack(ready=fnv1.READY_UNSPECIFIED),
                    "node-feature-discovery": _node_feature_discovery(ready=fnv1.READY_UNSPECIFIED),
                    "nvidia-dra-driver-gpu": _nvidia_dra_driver_gpu(ready=fnv1.READY_UNSPECIFIED),
                    "ai-gateway-crds": _ai_gateway_crds(ready=fnv1.READY_UNSPECIFIED),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _gaie_crds_inferenceobjectives_x_k8s(
                        ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _gaie_crds_inferencepools_k8s(
                        ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _gaie_crds_inferencepools_x_k8s(
                        ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gateway-namespace": _gateway_namespace(ready=fnv1.READY_UNSPECIFIED),
                    "gateway-selfsigned-issuer": _gateway_selfsigned_issuer(),
                    "trust-manager": _trust_manager(ready=fnv1.READY_UNSPECIFIED),
                    "dra-driver-critical-pods-quota": _dra_driver_critical_pods_quota(ready=fnv1.READY_UNSPECIFIED),
                    "leader-worker-set": _leader_worker_set(),
                    "gateway-class": _gateway_class(ready=fnv1.READY_UNSPECIFIED),
                    "gateway": _gateway(
                        hostname="gateway-test-backend-12345.modelplane-system.svc.cluster.local",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "gateway-ca-certificate": _gateway_ca_certificate(
                        common_name="modelplane cluster CA gateway-test-backend-12345.modelplane-syst",
                        ready=fnv1.READY_TRUE,
                    ),
                    "gateway-ca-issuer": _gateway_ca_issuer(ready=fnv1.READY_TRUE),
                    "gateway-serving-certificate": _gateway_serving_certificate(
                        hostname="gateway-test-backend-12345.modelplane-system.svc.cluster.local", ready=fnv1.READY_TRUE
                    ),
                    "gateway-ca-bundle": _gateway_ca_bundle(ready=fnv1.READY_TRUE),
                    "gateway-ca-configmap": _gateway_ca_configmap(ready=fnv1.READY_TRUE),
                    "gateway-client-ca-bundle": _gateway_client_ca_bundle(ca_crt="AAA\nBBB\n", ready=fnv1.READY_TRUE),
                    "gateway-client-auth": _gateway_client_auth(ready=fnv1.READY_TRUE),
                    "usage-cert-manager-by-envoy-gateway": _usage_cert_manager_by_envoy_gateway(),
                    "usage-ai-gateway-crds-by-ai-gateway": _usage_ai_gateway_crds_by_ai_gateway(),
                    "usage-gateway-namespace-by-gateway-proxy": _usage_gateway_namespace_by_gateway_proxy(),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _usage_cert_manager_by_gateway_selfsigned_issuer(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _usage_gateway_namespace_by_gateway_selfsigned_issuer(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _usage_gateway_selfsigned_issuer_by_trust_manager(),
                    "usage-gateway-class-by-gateway": _usage_gateway_class_by_gateway(),
                    "usage-envoy-gateway-by-gateway-class": _usage_envoy_gateway_by_gateway_class(),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # The GatewayClass and the cluster's own PKI are composed, so the CA is ready
    # to publish when the first InferenceGateway's CA arrives. The Gateway, the
    # client CA bundle and the policy demanding a client certificate aren't, and
    # nor is the Usage holding the GatewayClass for the Gateway.
    ComposeCase(
        name="a cluster with no InferenceGateway CA withholds its Gateway and warns",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Existing",
                    stack="Standard",
                    secrets=[v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig")],
                    gateway=v1alpha1.Gateway(hostname="gw.clusters.example.com"),
                ),
                resources={
                    "provider-config-kubernetes": _observed_kubernetes_provider_config(),
                    "provider-config-helm": _observed_helm_provider_config(),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_serving_stack(gateway=None),
                resources={
                    "provider-config-kubernetes": _kubernetes_provider_config(identity=None, ready=fnv1.READY_TRUE),
                    "provider-config-helm": _helm_provider_config(identity=None, ready=fnv1.READY_TRUE),
                    "cert-manager": _cert_manager(ready=fnv1.READY_UNSPECIFIED),
                    "kube-prometheus-stack": _kube_prometheus_stack(ready=fnv1.READY_UNSPECIFIED),
                    "node-feature-discovery": _node_feature_discovery(ready=fnv1.READY_UNSPECIFIED),
                    "nvidia-dra-driver-gpu": _nvidia_dra_driver_gpu(ready=fnv1.READY_UNSPECIFIED),
                    "ai-gateway-crds": _ai_gateway_crds(ready=fnv1.READY_UNSPECIFIED),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _gaie_crds_inferenceobjectives_x_k8s(
                        ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _gaie_crds_inferencepools_k8s(
                        ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _gaie_crds_inferencepools_x_k8s(
                        ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gateway-namespace": _gateway_namespace(ready=fnv1.READY_UNSPECIFIED),
                    "dra-driver-critical-pods-quota": _dra_driver_critical_pods_quota(ready=fnv1.READY_UNSPECIFIED),
                    "leader-worker-set": _leader_worker_set(),
                    "gateway-class": _gateway_class(ready=fnv1.READY_UNSPECIFIED),
                    "gateway-ca-certificate": _gateway_ca_certificate(
                        common_name="modelplane cluster CA gw.clusters.example.com", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gateway-ca-issuer": _gateway_ca_issuer(ready=fnv1.READY_UNSPECIFIED),
                    "gateway-serving-certificate": _gateway_serving_certificate(
                        hostname="gw.clusters.example.com", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gateway-ca-bundle": _gateway_ca_bundle(ready=fnv1.READY_UNSPECIFIED),
                    "gateway-ca-configmap": _gateway_ca_configmap(ready=fnv1.READY_UNSPECIFIED),
                    "usage-cert-manager-by-envoy-gateway": _usage_cert_manager_by_envoy_gateway(),
                    "usage-ai-gateway-crds-by-ai-gateway": _usage_ai_gateway_crds_by_ai_gateway(),
                    "usage-gateway-namespace-by-gateway-proxy": _usage_gateway_namespace_by_gateway_proxy(),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _usage_cert_manager_by_gateway_selfsigned_issuer(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _usage_gateway_namespace_by_gateway_selfsigned_issuer(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _usage_gateway_selfsigned_issuer_by_trust_manager(),
                    "usage-envoy-gateway-by-gateway-class": _usage_envoy_gateway_by_gateway_class(),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message="Gateway gw.clusters.example.com not served: no InferenceGateway has published a client CA for this cluster to trust, and serving without one would accept unauthenticated callers",
                ),
            ],
            context=structpb.Struct(),
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: ComposeCase) -> None:
    """RunFunction composes the serving stack, gated on what's observed."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want)


# The composed-resource key a component renders under is its identity:
# renaming one deletes and recreates the remote resource (for an Object
# holding a CRD, the CRD and its CRs). This pins the full key set per
# cloud and stack, including the Usage keys derived from depends_on, as
# reviewed literals. A failure here means the stack data changed a key -
# make sure that's intended, then update the inventory and the release
# notes.
#
# Each case's XR has an InferenceGateway CA to trust, so the Gateway and its
# client-auth policy are included. Every key the case expects is observed
# Ready, so the depends_on install gate opens and the full stack renders; a
# key the function doesn't render still fails the comparison. The cases
# compare keys rather than whole responses because every cloud's rendered
# half would restate its stack data, much of it generated, where
# COMPOSE_CASES already covers how components render.
COMPOSED_RESOURCE_KEYS_CASES = [
    ComposedResourceKeysCase(
        name="EKS Standard",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="EKS",
                    stack="Standard",
                    secrets=[
                        v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                        v1alpha1.Secret(type="GoogleApplicationCredentials", name="sa-secret", key="private_key"),
                    ],
                    gateway=v1alpha1.Gateway(
                        hostname="test-backend.gateways.example.com",
                        clientCAs=[
                            v1alpha1.ClientCA(
                                name="eu",
                                certificate="-----BEGIN CERTIFICATE-----\nfleet\n-----END CERTIFICATE-----\n",
                            )
                        ],
                    ),
                ),
                resources={
                    "provider-config-kubernetes": _observed_ready(),
                    "provider-config-helm": _observed_ready(),
                    "gateway": _observed_ready(),
                    "gateway-class": _observed_ready(),
                    "gateway-ca-certificate": _observed_ready(),
                    "gateway-ca-issuer": _observed_ready(),
                    "gateway-serving-certificate": _observed_ready(),
                    "gateway-ca-bundle": _observed_ready(),
                    "gateway-ca-configmap": _observed_ready(),
                    "gateway-client-ca-bundle": _observed_ready(),
                    "gateway-client-auth": _observed_ready(),
                    "usage-gateway-class-by-gateway": _observed_ready(),
                    "usage-envoy-gateway-by-gateway-class": _observed_ready(),
                    "ai-gateway": _observed_ready(),
                    "ai-gateway-crds": _observed_ready(),
                    "dra-driver-critical-pods-quota": _observed_ready(),
                    "envoy-gateway": _observed_ready(),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _observed_ready(),
                    "gateway-namespace": _observed_ready(),
                    "gateway-proxy": _observed_ready(),
                    "gateway-selfsigned-issuer": _observed_ready(),
                    "trust-manager": _observed_ready(),
                    "usage-ai-gateway-crds-by-ai-gateway": _observed_ready(),
                    "usage-cert-manager-by-envoy-gateway": _observed_ready(),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-proxy": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _observed_ready(),
                    "cert-manager": _observed_ready(),
                    "gpu-operator": _observed_ready(),
                    "k8s-ephemeral-storage-metrics": _observed_ready(),
                    "kube-prometheus-stack": _observed_ready(),
                    "node-feature-discovery": _observed_ready(),
                    "nodewright-operator": _observed_ready(),
                    "nvidia-dra-driver-gpu": _observed_ready(),
                    "nvsentinel": _observed_ready(),
                    "prometheus-adapter": _observed_ready(),
                    "prometheus-operator-crds": _observed_ready(),
                    "usage-cert-manager-by-gpu-operator": _observed_ready(),
                    "usage-cert-manager-by-nvsentinel": _observed_ready(),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _observed_ready(),
                    "usage-gpu-operator-by-nvsentinel": _observed_ready(),
                    "usage-kube-prometheus-stack-by-gpu-operator": _observed_ready(),
                    "usage-kube-prometheus-stack-by-k8s-ephemeral-storage-metrics": _observed_ready(),
                    "usage-kube-prometheus-stack-by-prometheus-adapter": _observed_ready(),
                    "usage-node-feature-discovery-by-gpu-operator": _observed_ready(),
                    "usage-prometheus-operator-crds-by-k8s-ephemeral-storage-metrics": _observed_ready(),
                    "usage-prometheus-operator-crds-by-kube-prometheus-stack": _observed_ready(),
                    "usage-prometheus-operator-crds-by-nvsentinel": _observed_ready(),
                    "leader-worker-set": _observed_ready(),
                },
            ),
        ),
        want={
            # Every cloud and stack.
            "provider-config-kubernetes",
            "provider-config-helm",
            "gateway",
            "gateway-class",
            "gateway-ca-certificate",
            "gateway-ca-issuer",
            "gateway-serving-certificate",
            "gateway-ca-bundle",
            "gateway-ca-configmap",
            "gateway-client-ca-bundle",
            "gateway-client-auth",
            "usage-gateway-class-by-gateway",
            "usage-envoy-gateway-by-gateway-class",
            # The common components.
            "ai-gateway",
            "ai-gateway-crds",
            "dra-driver-critical-pods-quota",
            "envoy-gateway",
            "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io",
            "gaie-crds-inferencepools.inference.networking.k8s.io",
            "gaie-crds-inferencepools.inference.networking.x-k8s.io",
            "gateway-namespace",
            "gateway-proxy",
            "gateway-selfsigned-issuer",
            "trust-manager",
            "usage-ai-gateway-crds-by-ai-gateway",
            "usage-cert-manager-by-envoy-gateway",
            "usage-cert-manager-by-gateway-selfsigned-issuer",
            "usage-gateway-namespace-by-gateway-proxy",
            "usage-gateway-namespace-by-gateway-selfsigned-issuer",
            "usage-gateway-selfsigned-issuer-by-trust-manager",
            # The EKS half, generated from aicr.
            "cert-manager",
            "gpu-operator",
            "k8s-ephemeral-storage-metrics",
            "kube-prometheus-stack",
            "node-feature-discovery",
            "nodewright-operator",
            "nvidia-dra-driver-gpu",
            "nvsentinel",
            "prometheus-adapter",
            "prometheus-operator-crds",
            "usage-cert-manager-by-gpu-operator",
            "usage-cert-manager-by-nvsentinel",
            "usage-gpu-operator-by-nvidia-dra-driver-gpu",
            "usage-gpu-operator-by-nvsentinel",
            "usage-kube-prometheus-stack-by-gpu-operator",
            "usage-kube-prometheus-stack-by-k8s-ephemeral-storage-metrics",
            "usage-kube-prometheus-stack-by-prometheus-adapter",
            "usage-node-feature-discovery-by-gpu-operator",
            "usage-prometheus-operator-crds-by-k8s-ephemeral-storage-metrics",
            "usage-prometheus-operator-crds-by-kube-prometheus-stack",
            "usage-prometheus-operator-crds-by-nvsentinel",
            # The Standard stack.
            "leader-worker-set",
        },
    ),
    ComposedResourceKeysCase(
        name="EKS Dynamo",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="EKS",
                    stack="Dynamo",
                    secrets=[
                        v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                        v1alpha1.Secret(type="GoogleApplicationCredentials", name="sa-secret", key="private_key"),
                    ],
                    gateway=v1alpha1.Gateway(
                        hostname="test-backend.gateways.example.com",
                        clientCAs=[
                            v1alpha1.ClientCA(
                                name="eu",
                                certificate="-----BEGIN CERTIFICATE-----\nfleet\n-----END CERTIFICATE-----\n",
                            )
                        ],
                    ),
                ),
                resources={
                    "provider-config-kubernetes": _observed_ready(),
                    "provider-config-helm": _observed_ready(),
                    "gateway": _observed_ready(),
                    "gateway-class": _observed_ready(),
                    "gateway-ca-certificate": _observed_ready(),
                    "gateway-ca-issuer": _observed_ready(),
                    "gateway-serving-certificate": _observed_ready(),
                    "gateway-ca-bundle": _observed_ready(),
                    "gateway-ca-configmap": _observed_ready(),
                    "gateway-client-ca-bundle": _observed_ready(),
                    "gateway-client-auth": _observed_ready(),
                    "usage-gateway-class-by-gateway": _observed_ready(),
                    "usage-envoy-gateway-by-gateway-class": _observed_ready(),
                    "ai-gateway": _observed_ready(),
                    "ai-gateway-crds": _observed_ready(),
                    "dra-driver-critical-pods-quota": _observed_ready(),
                    "envoy-gateway": _observed_ready(),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _observed_ready(),
                    "gateway-namespace": _observed_ready(),
                    "gateway-proxy": _observed_ready(),
                    "gateway-selfsigned-issuer": _observed_ready(),
                    "trust-manager": _observed_ready(),
                    "usage-ai-gateway-crds-by-ai-gateway": _observed_ready(),
                    "usage-cert-manager-by-envoy-gateway": _observed_ready(),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-proxy": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _observed_ready(),
                    "cert-manager": _observed_ready(),
                    "gpu-operator": _observed_ready(),
                    "k8s-ephemeral-storage-metrics": _observed_ready(),
                    "kube-prometheus-stack": _observed_ready(),
                    "node-feature-discovery": _observed_ready(),
                    "nodewright-operator": _observed_ready(),
                    "nvidia-dra-driver-gpu": _observed_ready(),
                    "nvsentinel": _observed_ready(),
                    "prometheus-adapter": _observed_ready(),
                    "prometheus-operator-crds": _observed_ready(),
                    "usage-cert-manager-by-gpu-operator": _observed_ready(),
                    "usage-cert-manager-by-nvsentinel": _observed_ready(),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _observed_ready(),
                    "usage-gpu-operator-by-nvsentinel": _observed_ready(),
                    "usage-kube-prometheus-stack-by-gpu-operator": _observed_ready(),
                    "usage-kube-prometheus-stack-by-k8s-ephemeral-storage-metrics": _observed_ready(),
                    "usage-kube-prometheus-stack-by-prometheus-adapter": _observed_ready(),
                    "usage-node-feature-discovery-by-gpu-operator": _observed_ready(),
                    "usage-prometheus-operator-crds-by-k8s-ephemeral-storage-metrics": _observed_ready(),
                    "usage-prometheus-operator-crds-by-kube-prometheus-stack": _observed_ready(),
                    "usage-prometheus-operator-crds-by-nvsentinel": _observed_ready(),
                    "grove": _observed_ready(),
                    "kai-queue": _observed_ready(),
                    "kai-queue-root": _observed_ready(),
                    "kai-scheduler": _observed_ready(),
                    "modelexpress-crds-modelcacheentries.modelexpress.nvidia.com": _observed_ready(),
                    "modelexpress-crds-modelmetadatas.modelexpress.nvidia.com": _observed_ready(),
                    "modelexpress-server": _observed_ready(),
                    "modelexpress-server-role": _observed_ready(),
                    "modelexpress-server-rolebinding": _observed_ready(),
                    "modelexpress-server-sa": _observed_ready(),
                    "modelexpress-server-svc": _observed_ready(),
                    "usage-kai-scheduler-by-kai-queue": _observed_ready(),
                    "usage-kai-scheduler-by-kai-queue-root": _observed_ready(),
                    "usage-modelexpress-crds-modelcacheentries.modelexpress.nvidia.com-by-modelexpress-server": _observed_ready(),
                    "usage-modelexpress-crds-modelmetadatas.modelexpress.nvidia.com-by-modelexpress-server": _observed_ready(),
                },
            ),
        ),
        want={
            # Every cloud and stack.
            "provider-config-kubernetes",
            "provider-config-helm",
            "gateway",
            "gateway-class",
            "gateway-ca-certificate",
            "gateway-ca-issuer",
            "gateway-serving-certificate",
            "gateway-ca-bundle",
            "gateway-ca-configmap",
            "gateway-client-ca-bundle",
            "gateway-client-auth",
            "usage-gateway-class-by-gateway",
            "usage-envoy-gateway-by-gateway-class",
            # The common components.
            "ai-gateway",
            "ai-gateway-crds",
            "dra-driver-critical-pods-quota",
            "envoy-gateway",
            "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io",
            "gaie-crds-inferencepools.inference.networking.k8s.io",
            "gaie-crds-inferencepools.inference.networking.x-k8s.io",
            "gateway-namespace",
            "gateway-proxy",
            "gateway-selfsigned-issuer",
            "trust-manager",
            "usage-ai-gateway-crds-by-ai-gateway",
            "usage-cert-manager-by-envoy-gateway",
            "usage-cert-manager-by-gateway-selfsigned-issuer",
            "usage-gateway-namespace-by-gateway-proxy",
            "usage-gateway-namespace-by-gateway-selfsigned-issuer",
            "usage-gateway-selfsigned-issuer-by-trust-manager",
            # The EKS half, generated from aicr.
            "cert-manager",
            "gpu-operator",
            "k8s-ephemeral-storage-metrics",
            "kube-prometheus-stack",
            "node-feature-discovery",
            "nodewright-operator",
            "nvidia-dra-driver-gpu",
            "nvsentinel",
            "prometheus-adapter",
            "prometheus-operator-crds",
            "usage-cert-manager-by-gpu-operator",
            "usage-cert-manager-by-nvsentinel",
            "usage-gpu-operator-by-nvidia-dra-driver-gpu",
            "usage-gpu-operator-by-nvsentinel",
            "usage-kube-prometheus-stack-by-gpu-operator",
            "usage-kube-prometheus-stack-by-k8s-ephemeral-storage-metrics",
            "usage-kube-prometheus-stack-by-prometheus-adapter",
            "usage-node-feature-discovery-by-gpu-operator",
            "usage-prometheus-operator-crds-by-k8s-ephemeral-storage-metrics",
            "usage-prometheus-operator-crds-by-kube-prometheus-stack",
            "usage-prometheus-operator-crds-by-nvsentinel",
            # The Dynamo stack.
            "grove",
            "kai-queue",
            "kai-queue-root",
            "kai-scheduler",
            "modelexpress-crds-modelcacheentries.modelexpress.nvidia.com",
            "modelexpress-crds-modelmetadatas.modelexpress.nvidia.com",
            "modelexpress-server",
            "modelexpress-server-role",
            "modelexpress-server-rolebinding",
            "modelexpress-server-sa",
            "modelexpress-server-svc",
            "usage-kai-scheduler-by-kai-queue",
            "usage-kai-scheduler-by-kai-queue-root",
            "usage-modelexpress-crds-modelcacheentries.modelexpress.nvidia.com-by-modelexpress-server",
            "usage-modelexpress-crds-modelmetadatas.modelexpress.nvidia.com-by-modelexpress-server",
        },
    ),
    ComposedResourceKeysCase(
        name="AKS Standard",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="AKS",
                    stack="Standard",
                    secrets=[
                        v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                        v1alpha1.Secret(type="GoogleApplicationCredentials", name="sa-secret", key="private_key"),
                    ],
                    gateway=v1alpha1.Gateway(
                        hostname="test-backend.gateways.example.com",
                        clientCAs=[
                            v1alpha1.ClientCA(
                                name="eu",
                                certificate="-----BEGIN CERTIFICATE-----\nfleet\n-----END CERTIFICATE-----\n",
                            )
                        ],
                    ),
                ),
                resources={
                    "provider-config-kubernetes": _observed_ready(),
                    "provider-config-helm": _observed_ready(),
                    "gateway": _observed_ready(),
                    "gateway-class": _observed_ready(),
                    "gateway-ca-certificate": _observed_ready(),
                    "gateway-ca-issuer": _observed_ready(),
                    "gateway-serving-certificate": _observed_ready(),
                    "gateway-ca-bundle": _observed_ready(),
                    "gateway-ca-configmap": _observed_ready(),
                    "gateway-client-ca-bundle": _observed_ready(),
                    "gateway-client-auth": _observed_ready(),
                    "usage-gateway-class-by-gateway": _observed_ready(),
                    "usage-envoy-gateway-by-gateway-class": _observed_ready(),
                    "ai-gateway": _observed_ready(),
                    "ai-gateway-crds": _observed_ready(),
                    "dra-driver-critical-pods-quota": _observed_ready(),
                    "envoy-gateway": _observed_ready(),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _observed_ready(),
                    "gateway-namespace": _observed_ready(),
                    "gateway-proxy": _observed_ready(),
                    "gateway-selfsigned-issuer": _observed_ready(),
                    "trust-manager": _observed_ready(),
                    "usage-ai-gateway-crds-by-ai-gateway": _observed_ready(),
                    "usage-cert-manager-by-envoy-gateway": _observed_ready(),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-proxy": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _observed_ready(),
                    "cert-manager": _observed_ready(),
                    "gpu-operator": _observed_ready(),
                    "k8s-ephemeral-storage-metrics": _observed_ready(),
                    "kube-prometheus-stack": _observed_ready(),
                    "node-feature-discovery": _observed_ready(),
                    "nodewright-operator": _observed_ready(),
                    "nvidia-dra-driver-gpu": _observed_ready(),
                    "nvsentinel": _observed_ready(),
                    "prometheus-adapter": _observed_ready(),
                    "prometheus-operator-crds": _observed_ready(),
                    "usage-cert-manager-by-gpu-operator": _observed_ready(),
                    "usage-cert-manager-by-nvsentinel": _observed_ready(),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _observed_ready(),
                    "usage-gpu-operator-by-nvsentinel": _observed_ready(),
                    "usage-kube-prometheus-stack-by-gpu-operator": _observed_ready(),
                    "usage-kube-prometheus-stack-by-k8s-ephemeral-storage-metrics": _observed_ready(),
                    "usage-kube-prometheus-stack-by-prometheus-adapter": _observed_ready(),
                    "usage-node-feature-discovery-by-gpu-operator": _observed_ready(),
                    "usage-prometheus-operator-crds-by-k8s-ephemeral-storage-metrics": _observed_ready(),
                    "usage-prometheus-operator-crds-by-kube-prometheus-stack": _observed_ready(),
                    "usage-prometheus-operator-crds-by-nvsentinel": _observed_ready(),
                    "gpu-operator-manifests": _observed_ready(),
                    "usage-gpu-operator-by-gpu-operator-manifests": _observed_ready(),
                    "leader-worker-set": _observed_ready(),
                },
            ),
        ),
        want={
            # Every cloud and stack.
            "provider-config-kubernetes",
            "provider-config-helm",
            "gateway",
            "gateway-class",
            "gateway-ca-certificate",
            "gateway-ca-issuer",
            "gateway-serving-certificate",
            "gateway-ca-bundle",
            "gateway-ca-configmap",
            "gateway-client-ca-bundle",
            "gateway-client-auth",
            "usage-gateway-class-by-gateway",
            "usage-envoy-gateway-by-gateway-class",
            # The common components.
            "ai-gateway",
            "ai-gateway-crds",
            "dra-driver-critical-pods-quota",
            "envoy-gateway",
            "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io",
            "gaie-crds-inferencepools.inference.networking.k8s.io",
            "gaie-crds-inferencepools.inference.networking.x-k8s.io",
            "gateway-namespace",
            "gateway-proxy",
            "gateway-selfsigned-issuer",
            "trust-manager",
            "usage-ai-gateway-crds-by-ai-gateway",
            "usage-cert-manager-by-envoy-gateway",
            "usage-cert-manager-by-gateway-selfsigned-issuer",
            "usage-gateway-namespace-by-gateway-proxy",
            "usage-gateway-namespace-by-gateway-selfsigned-issuer",
            "usage-gateway-selfsigned-issuer-by-trust-manager",
            # The AKS half, generated from aicr.
            "cert-manager",
            "gpu-operator",
            "k8s-ephemeral-storage-metrics",
            "kube-prometheus-stack",
            "node-feature-discovery",
            "nodewright-operator",
            "nvidia-dra-driver-gpu",
            "nvsentinel",
            "prometheus-adapter",
            "prometheus-operator-crds",
            "usage-cert-manager-by-gpu-operator",
            "usage-cert-manager-by-nvsentinel",
            "usage-gpu-operator-by-nvidia-dra-driver-gpu",
            "usage-gpu-operator-by-nvsentinel",
            "usage-kube-prometheus-stack-by-gpu-operator",
            "usage-kube-prometheus-stack-by-k8s-ephemeral-storage-metrics",
            "usage-kube-prometheus-stack-by-prometheus-adapter",
            "usage-node-feature-discovery-by-gpu-operator",
            "usage-prometheus-operator-crds-by-k8s-ephemeral-storage-metrics",
            "usage-prometheus-operator-crds-by-kube-prometheus-stack",
            "usage-prometheus-operator-crds-by-nvsentinel",
            # AKS additionally carries the gpu-operator's toolkit-hardening manifest.
            "gpu-operator-manifests",
            "usage-gpu-operator-by-gpu-operator-manifests",
            # The Standard stack.
            "leader-worker-set",
        },
    ),
    ComposedResourceKeysCase(
        name="AKS Dynamo",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="AKS",
                    stack="Dynamo",
                    secrets=[
                        v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                        v1alpha1.Secret(type="GoogleApplicationCredentials", name="sa-secret", key="private_key"),
                    ],
                    gateway=v1alpha1.Gateway(
                        hostname="test-backend.gateways.example.com",
                        clientCAs=[
                            v1alpha1.ClientCA(
                                name="eu",
                                certificate="-----BEGIN CERTIFICATE-----\nfleet\n-----END CERTIFICATE-----\n",
                            )
                        ],
                    ),
                ),
                resources={
                    "provider-config-kubernetes": _observed_ready(),
                    "provider-config-helm": _observed_ready(),
                    "gateway": _observed_ready(),
                    "gateway-class": _observed_ready(),
                    "gateway-ca-certificate": _observed_ready(),
                    "gateway-ca-issuer": _observed_ready(),
                    "gateway-serving-certificate": _observed_ready(),
                    "gateway-ca-bundle": _observed_ready(),
                    "gateway-ca-configmap": _observed_ready(),
                    "gateway-client-ca-bundle": _observed_ready(),
                    "gateway-client-auth": _observed_ready(),
                    "usage-gateway-class-by-gateway": _observed_ready(),
                    "usage-envoy-gateway-by-gateway-class": _observed_ready(),
                    "ai-gateway": _observed_ready(),
                    "ai-gateway-crds": _observed_ready(),
                    "dra-driver-critical-pods-quota": _observed_ready(),
                    "envoy-gateway": _observed_ready(),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _observed_ready(),
                    "gateway-namespace": _observed_ready(),
                    "gateway-proxy": _observed_ready(),
                    "gateway-selfsigned-issuer": _observed_ready(),
                    "trust-manager": _observed_ready(),
                    "usage-ai-gateway-crds-by-ai-gateway": _observed_ready(),
                    "usage-cert-manager-by-envoy-gateway": _observed_ready(),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-proxy": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _observed_ready(),
                    "cert-manager": _observed_ready(),
                    "gpu-operator": _observed_ready(),
                    "k8s-ephemeral-storage-metrics": _observed_ready(),
                    "kube-prometheus-stack": _observed_ready(),
                    "node-feature-discovery": _observed_ready(),
                    "nodewright-operator": _observed_ready(),
                    "nvidia-dra-driver-gpu": _observed_ready(),
                    "nvsentinel": _observed_ready(),
                    "prometheus-adapter": _observed_ready(),
                    "prometheus-operator-crds": _observed_ready(),
                    "usage-cert-manager-by-gpu-operator": _observed_ready(),
                    "usage-cert-manager-by-nvsentinel": _observed_ready(),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _observed_ready(),
                    "usage-gpu-operator-by-nvsentinel": _observed_ready(),
                    "usage-kube-prometheus-stack-by-gpu-operator": _observed_ready(),
                    "usage-kube-prometheus-stack-by-k8s-ephemeral-storage-metrics": _observed_ready(),
                    "usage-kube-prometheus-stack-by-prometheus-adapter": _observed_ready(),
                    "usage-node-feature-discovery-by-gpu-operator": _observed_ready(),
                    "usage-prometheus-operator-crds-by-k8s-ephemeral-storage-metrics": _observed_ready(),
                    "usage-prometheus-operator-crds-by-kube-prometheus-stack": _observed_ready(),
                    "usage-prometheus-operator-crds-by-nvsentinel": _observed_ready(),
                    "gpu-operator-manifests": _observed_ready(),
                    "usage-gpu-operator-by-gpu-operator-manifests": _observed_ready(),
                    "grove": _observed_ready(),
                    "kai-queue": _observed_ready(),
                    "kai-queue-root": _observed_ready(),
                    "kai-scheduler": _observed_ready(),
                    "modelexpress-crds-modelcacheentries.modelexpress.nvidia.com": _observed_ready(),
                    "modelexpress-crds-modelmetadatas.modelexpress.nvidia.com": _observed_ready(),
                    "modelexpress-server": _observed_ready(),
                    "modelexpress-server-role": _observed_ready(),
                    "modelexpress-server-rolebinding": _observed_ready(),
                    "modelexpress-server-sa": _observed_ready(),
                    "modelexpress-server-svc": _observed_ready(),
                    "usage-kai-scheduler-by-kai-queue": _observed_ready(),
                    "usage-kai-scheduler-by-kai-queue-root": _observed_ready(),
                    "usage-modelexpress-crds-modelcacheentries.modelexpress.nvidia.com-by-modelexpress-server": _observed_ready(),
                    "usage-modelexpress-crds-modelmetadatas.modelexpress.nvidia.com-by-modelexpress-server": _observed_ready(),
                },
            ),
        ),
        want={
            # Every cloud and stack.
            "provider-config-kubernetes",
            "provider-config-helm",
            "gateway",
            "gateway-class",
            "gateway-ca-certificate",
            "gateway-ca-issuer",
            "gateway-serving-certificate",
            "gateway-ca-bundle",
            "gateway-ca-configmap",
            "gateway-client-ca-bundle",
            "gateway-client-auth",
            "usage-gateway-class-by-gateway",
            "usage-envoy-gateway-by-gateway-class",
            # The common components.
            "ai-gateway",
            "ai-gateway-crds",
            "dra-driver-critical-pods-quota",
            "envoy-gateway",
            "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io",
            "gaie-crds-inferencepools.inference.networking.k8s.io",
            "gaie-crds-inferencepools.inference.networking.x-k8s.io",
            "gateway-namespace",
            "gateway-proxy",
            "gateway-selfsigned-issuer",
            "trust-manager",
            "usage-ai-gateway-crds-by-ai-gateway",
            "usage-cert-manager-by-envoy-gateway",
            "usage-cert-manager-by-gateway-selfsigned-issuer",
            "usage-gateway-namespace-by-gateway-proxy",
            "usage-gateway-namespace-by-gateway-selfsigned-issuer",
            "usage-gateway-selfsigned-issuer-by-trust-manager",
            # The AKS half, generated from aicr.
            "cert-manager",
            "gpu-operator",
            "k8s-ephemeral-storage-metrics",
            "kube-prometheus-stack",
            "node-feature-discovery",
            "nodewright-operator",
            "nvidia-dra-driver-gpu",
            "nvsentinel",
            "prometheus-adapter",
            "prometheus-operator-crds",
            "usage-cert-manager-by-gpu-operator",
            "usage-cert-manager-by-nvsentinel",
            "usage-gpu-operator-by-nvidia-dra-driver-gpu",
            "usage-gpu-operator-by-nvsentinel",
            "usage-kube-prometheus-stack-by-gpu-operator",
            "usage-kube-prometheus-stack-by-k8s-ephemeral-storage-metrics",
            "usage-kube-prometheus-stack-by-prometheus-adapter",
            "usage-node-feature-discovery-by-gpu-operator",
            "usage-prometheus-operator-crds-by-k8s-ephemeral-storage-metrics",
            "usage-prometheus-operator-crds-by-kube-prometheus-stack",
            "usage-prometheus-operator-crds-by-nvsentinel",
            # AKS additionally carries the gpu-operator's toolkit-hardening manifest.
            "gpu-operator-manifests",
            "usage-gpu-operator-by-gpu-operator-manifests",
            # The Dynamo stack.
            "grove",
            "kai-queue",
            "kai-queue-root",
            "kai-scheduler",
            "modelexpress-crds-modelcacheentries.modelexpress.nvidia.com",
            "modelexpress-crds-modelmetadatas.modelexpress.nvidia.com",
            "modelexpress-server",
            "modelexpress-server-role",
            "modelexpress-server-rolebinding",
            "modelexpress-server-sa",
            "modelexpress-server-svc",
            "usage-kai-scheduler-by-kai-queue",
            "usage-kai-scheduler-by-kai-queue-root",
            "usage-modelexpress-crds-modelcacheentries.modelexpress.nvidia.com-by-modelexpress-server",
            "usage-modelexpress-crds-modelmetadatas.modelexpress.nvidia.com-by-modelexpress-server",
        },
    ),
    ComposedResourceKeysCase(
        name="GKE Standard",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="GKE",
                    stack="Standard",
                    secrets=[
                        v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                        v1alpha1.Secret(type="GoogleApplicationCredentials", name="sa-secret", key="private_key"),
                    ],
                    gateway=v1alpha1.Gateway(
                        hostname="test-backend.gateways.example.com",
                        clientCAs=[
                            v1alpha1.ClientCA(
                                name="eu",
                                certificate="-----BEGIN CERTIFICATE-----\nfleet\n-----END CERTIFICATE-----\n",
                            )
                        ],
                    ),
                ),
                resources={
                    "provider-config-kubernetes": _observed_ready(),
                    "provider-config-helm": _observed_ready(),
                    "gateway": _observed_ready(),
                    "gateway-class": _observed_ready(),
                    "gateway-ca-certificate": _observed_ready(),
                    "gateway-ca-issuer": _observed_ready(),
                    "gateway-serving-certificate": _observed_ready(),
                    "gateway-ca-bundle": _observed_ready(),
                    "gateway-ca-configmap": _observed_ready(),
                    "gateway-client-ca-bundle": _observed_ready(),
                    "gateway-client-auth": _observed_ready(),
                    "usage-gateway-class-by-gateway": _observed_ready(),
                    "usage-envoy-gateway-by-gateway-class": _observed_ready(),
                    "ai-gateway": _observed_ready(),
                    "ai-gateway-crds": _observed_ready(),
                    "dra-driver-critical-pods-quota": _observed_ready(),
                    "envoy-gateway": _observed_ready(),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _observed_ready(),
                    "gateway-namespace": _observed_ready(),
                    "gateway-proxy": _observed_ready(),
                    "gateway-selfsigned-issuer": _observed_ready(),
                    "trust-manager": _observed_ready(),
                    "usage-ai-gateway-crds-by-ai-gateway": _observed_ready(),
                    "usage-cert-manager-by-envoy-gateway": _observed_ready(),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-proxy": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _observed_ready(),
                    "cert-manager": _observed_ready(),
                    "gpu-operator": _observed_ready(),
                    "k8s-ephemeral-storage-metrics": _observed_ready(),
                    "kube-prometheus-stack": _observed_ready(),
                    "node-feature-discovery": _observed_ready(),
                    "nodewright-operator": _observed_ready(),
                    "nvidia-dra-driver-gpu": _observed_ready(),
                    "nvsentinel": _observed_ready(),
                    "prometheus-adapter": _observed_ready(),
                    "prometheus-operator-crds": _observed_ready(),
                    "usage-cert-manager-by-gpu-operator": _observed_ready(),
                    "usage-cert-manager-by-nvsentinel": _observed_ready(),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _observed_ready(),
                    "usage-gpu-operator-by-nvsentinel": _observed_ready(),
                    "usage-kube-prometheus-stack-by-gpu-operator": _observed_ready(),
                    "usage-kube-prometheus-stack-by-k8s-ephemeral-storage-metrics": _observed_ready(),
                    "usage-kube-prometheus-stack-by-prometheus-adapter": _observed_ready(),
                    "usage-node-feature-discovery-by-gpu-operator": _observed_ready(),
                    "usage-prometheus-operator-crds-by-k8s-ephemeral-storage-metrics": _observed_ready(),
                    "usage-prometheus-operator-crds-by-kube-prometheus-stack": _observed_ready(),
                    "usage-prometheus-operator-crds-by-nvsentinel": _observed_ready(),
                    "gpu-operator-pre-manifests-aicr-gke-critical-pods": _observed_ready(),
                    "gpu-operator-pre-manifests-gpu-operator": _observed_ready(),
                    "usage-gpu-operator-pre-manifests-aicr-gke-critical-pods-by-gpu-operator": _observed_ready(),
                    "usage-gpu-operator-pre-manifests-gpu-operator-by-gpu-operator": _observed_ready(),
                    "leader-worker-set": _observed_ready(),
                },
            ),
        ),
        want={
            # Every cloud and stack.
            "provider-config-kubernetes",
            "provider-config-helm",
            "gateway",
            "gateway-class",
            "gateway-ca-certificate",
            "gateway-ca-issuer",
            "gateway-serving-certificate",
            "gateway-ca-bundle",
            "gateway-ca-configmap",
            "gateway-client-ca-bundle",
            "gateway-client-auth",
            "usage-gateway-class-by-gateway",
            "usage-envoy-gateway-by-gateway-class",
            # The common components.
            "ai-gateway",
            "ai-gateway-crds",
            "dra-driver-critical-pods-quota",
            "envoy-gateway",
            "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io",
            "gaie-crds-inferencepools.inference.networking.k8s.io",
            "gaie-crds-inferencepools.inference.networking.x-k8s.io",
            "gateway-namespace",
            "gateway-proxy",
            "gateway-selfsigned-issuer",
            "trust-manager",
            "usage-ai-gateway-crds-by-ai-gateway",
            "usage-cert-manager-by-envoy-gateway",
            "usage-cert-manager-by-gateway-selfsigned-issuer",
            "usage-gateway-namespace-by-gateway-proxy",
            "usage-gateway-namespace-by-gateway-selfsigned-issuer",
            "usage-gateway-selfsigned-issuer-by-trust-manager",
            # The GKE half, generated from aicr.
            "cert-manager",
            "gpu-operator",
            "k8s-ephemeral-storage-metrics",
            "kube-prometheus-stack",
            "node-feature-discovery",
            "nodewright-operator",
            "nvidia-dra-driver-gpu",
            "nvsentinel",
            "prometheus-adapter",
            "prometheus-operator-crds",
            "usage-cert-manager-by-gpu-operator",
            "usage-cert-manager-by-nvsentinel",
            "usage-gpu-operator-by-nvidia-dra-driver-gpu",
            "usage-gpu-operator-by-nvsentinel",
            "usage-kube-prometheus-stack-by-gpu-operator",
            "usage-kube-prometheus-stack-by-k8s-ephemeral-storage-metrics",
            "usage-kube-prometheus-stack-by-prometheus-adapter",
            "usage-node-feature-discovery-by-gpu-operator",
            "usage-prometheus-operator-crds-by-k8s-ephemeral-storage-metrics",
            "usage-prometheus-operator-crds-by-kube-prometheus-stack",
            "usage-prometheus-operator-crds-by-nvsentinel",
            # GKE additionally carries the critical-pods ResourceQuota aicr's
            # bundler synthesizes as a gpu-operator pre-manifest (GKE rejects
            # system-node-critical pods in a namespace without one; aicr#915).
            "gpu-operator-pre-manifests-aicr-gke-critical-pods",
            "gpu-operator-pre-manifests-gpu-operator",
            "usage-gpu-operator-pre-manifests-aicr-gke-critical-pods-by-gpu-operator",
            "usage-gpu-operator-pre-manifests-gpu-operator-by-gpu-operator",
            # The Standard stack.
            "leader-worker-set",
        },
    ),
    ComposedResourceKeysCase(
        name="GKE Dynamo",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="GKE",
                    stack="Dynamo",
                    secrets=[
                        v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                        v1alpha1.Secret(type="GoogleApplicationCredentials", name="sa-secret", key="private_key"),
                    ],
                    gateway=v1alpha1.Gateway(
                        hostname="test-backend.gateways.example.com",
                        clientCAs=[
                            v1alpha1.ClientCA(
                                name="eu",
                                certificate="-----BEGIN CERTIFICATE-----\nfleet\n-----END CERTIFICATE-----\n",
                            )
                        ],
                    ),
                ),
                resources={
                    "provider-config-kubernetes": _observed_ready(),
                    "provider-config-helm": _observed_ready(),
                    "gateway": _observed_ready(),
                    "gateway-class": _observed_ready(),
                    "gateway-ca-certificate": _observed_ready(),
                    "gateway-ca-issuer": _observed_ready(),
                    "gateway-serving-certificate": _observed_ready(),
                    "gateway-ca-bundle": _observed_ready(),
                    "gateway-ca-configmap": _observed_ready(),
                    "gateway-client-ca-bundle": _observed_ready(),
                    "gateway-client-auth": _observed_ready(),
                    "usage-gateway-class-by-gateway": _observed_ready(),
                    "usage-envoy-gateway-by-gateway-class": _observed_ready(),
                    "ai-gateway": _observed_ready(),
                    "ai-gateway-crds": _observed_ready(),
                    "dra-driver-critical-pods-quota": _observed_ready(),
                    "envoy-gateway": _observed_ready(),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _observed_ready(),
                    "gateway-namespace": _observed_ready(),
                    "gateway-proxy": _observed_ready(),
                    "gateway-selfsigned-issuer": _observed_ready(),
                    "trust-manager": _observed_ready(),
                    "usage-ai-gateway-crds-by-ai-gateway": _observed_ready(),
                    "usage-cert-manager-by-envoy-gateway": _observed_ready(),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-proxy": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _observed_ready(),
                    "cert-manager": _observed_ready(),
                    "gpu-operator": _observed_ready(),
                    "k8s-ephemeral-storage-metrics": _observed_ready(),
                    "kube-prometheus-stack": _observed_ready(),
                    "node-feature-discovery": _observed_ready(),
                    "nodewright-operator": _observed_ready(),
                    "nvidia-dra-driver-gpu": _observed_ready(),
                    "nvsentinel": _observed_ready(),
                    "prometheus-adapter": _observed_ready(),
                    "prometheus-operator-crds": _observed_ready(),
                    "usage-cert-manager-by-gpu-operator": _observed_ready(),
                    "usage-cert-manager-by-nvsentinel": _observed_ready(),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _observed_ready(),
                    "usage-gpu-operator-by-nvsentinel": _observed_ready(),
                    "usage-kube-prometheus-stack-by-gpu-operator": _observed_ready(),
                    "usage-kube-prometheus-stack-by-k8s-ephemeral-storage-metrics": _observed_ready(),
                    "usage-kube-prometheus-stack-by-prometheus-adapter": _observed_ready(),
                    "usage-node-feature-discovery-by-gpu-operator": _observed_ready(),
                    "usage-prometheus-operator-crds-by-k8s-ephemeral-storage-metrics": _observed_ready(),
                    "usage-prometheus-operator-crds-by-kube-prometheus-stack": _observed_ready(),
                    "usage-prometheus-operator-crds-by-nvsentinel": _observed_ready(),
                    "gpu-operator-pre-manifests-aicr-gke-critical-pods": _observed_ready(),
                    "gpu-operator-pre-manifests-gpu-operator": _observed_ready(),
                    "usage-gpu-operator-pre-manifests-aicr-gke-critical-pods-by-gpu-operator": _observed_ready(),
                    "usage-gpu-operator-pre-manifests-gpu-operator-by-gpu-operator": _observed_ready(),
                    "grove": _observed_ready(),
                    "kai-queue": _observed_ready(),
                    "kai-queue-root": _observed_ready(),
                    "kai-scheduler": _observed_ready(),
                    "modelexpress-crds-modelcacheentries.modelexpress.nvidia.com": _observed_ready(),
                    "modelexpress-crds-modelmetadatas.modelexpress.nvidia.com": _observed_ready(),
                    "modelexpress-server": _observed_ready(),
                    "modelexpress-server-role": _observed_ready(),
                    "modelexpress-server-rolebinding": _observed_ready(),
                    "modelexpress-server-sa": _observed_ready(),
                    "modelexpress-server-svc": _observed_ready(),
                    "usage-kai-scheduler-by-kai-queue": _observed_ready(),
                    "usage-kai-scheduler-by-kai-queue-root": _observed_ready(),
                    "usage-modelexpress-crds-modelcacheentries.modelexpress.nvidia.com-by-modelexpress-server": _observed_ready(),
                    "usage-modelexpress-crds-modelmetadatas.modelexpress.nvidia.com-by-modelexpress-server": _observed_ready(),
                },
            ),
        ),
        want={
            # Every cloud and stack.
            "provider-config-kubernetes",
            "provider-config-helm",
            "gateway",
            "gateway-class",
            "gateway-ca-certificate",
            "gateway-ca-issuer",
            "gateway-serving-certificate",
            "gateway-ca-bundle",
            "gateway-ca-configmap",
            "gateway-client-ca-bundle",
            "gateway-client-auth",
            "usage-gateway-class-by-gateway",
            "usage-envoy-gateway-by-gateway-class",
            # The common components.
            "ai-gateway",
            "ai-gateway-crds",
            "dra-driver-critical-pods-quota",
            "envoy-gateway",
            "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io",
            "gaie-crds-inferencepools.inference.networking.k8s.io",
            "gaie-crds-inferencepools.inference.networking.x-k8s.io",
            "gateway-namespace",
            "gateway-proxy",
            "gateway-selfsigned-issuer",
            "trust-manager",
            "usage-ai-gateway-crds-by-ai-gateway",
            "usage-cert-manager-by-envoy-gateway",
            "usage-cert-manager-by-gateway-selfsigned-issuer",
            "usage-gateway-namespace-by-gateway-proxy",
            "usage-gateway-namespace-by-gateway-selfsigned-issuer",
            "usage-gateway-selfsigned-issuer-by-trust-manager",
            # The GKE half, generated from aicr.
            "cert-manager",
            "gpu-operator",
            "k8s-ephemeral-storage-metrics",
            "kube-prometheus-stack",
            "node-feature-discovery",
            "nodewright-operator",
            "nvidia-dra-driver-gpu",
            "nvsentinel",
            "prometheus-adapter",
            "prometheus-operator-crds",
            "usage-cert-manager-by-gpu-operator",
            "usage-cert-manager-by-nvsentinel",
            "usage-gpu-operator-by-nvidia-dra-driver-gpu",
            "usage-gpu-operator-by-nvsentinel",
            "usage-kube-prometheus-stack-by-gpu-operator",
            "usage-kube-prometheus-stack-by-k8s-ephemeral-storage-metrics",
            "usage-kube-prometheus-stack-by-prometheus-adapter",
            "usage-node-feature-discovery-by-gpu-operator",
            "usage-prometheus-operator-crds-by-k8s-ephemeral-storage-metrics",
            "usage-prometheus-operator-crds-by-kube-prometheus-stack",
            "usage-prometheus-operator-crds-by-nvsentinel",
            # GKE additionally carries the critical-pods ResourceQuota aicr's
            # bundler synthesizes as a gpu-operator pre-manifest (GKE rejects
            # system-node-critical pods in a namespace without one; aicr#915).
            "gpu-operator-pre-manifests-aicr-gke-critical-pods",
            "gpu-operator-pre-manifests-gpu-operator",
            "usage-gpu-operator-pre-manifests-aicr-gke-critical-pods-by-gpu-operator",
            "usage-gpu-operator-pre-manifests-gpu-operator-by-gpu-operator",
            # The Dynamo stack.
            "grove",
            "kai-queue",
            "kai-queue-root",
            "kai-scheduler",
            "modelexpress-crds-modelcacheentries.modelexpress.nvidia.com",
            "modelexpress-crds-modelmetadatas.modelexpress.nvidia.com",
            "modelexpress-server",
            "modelexpress-server-role",
            "modelexpress-server-rolebinding",
            "modelexpress-server-sa",
            "modelexpress-server-svc",
            "usage-kai-scheduler-by-kai-queue",
            "usage-kai-scheduler-by-kai-queue-root",
            "usage-modelexpress-crds-modelcacheentries.modelexpress.nvidia.com-by-modelexpress-server",
            "usage-modelexpress-crds-modelmetadatas.modelexpress.nvidia.com-by-modelexpress-server",
        },
    ),
    ComposedResourceKeysCase(
        name="Nebius Standard",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Nebius",
                    stack="Standard",
                    secrets=[
                        v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                        v1alpha1.Secret(type="GoogleApplicationCredentials", name="sa-secret", key="private_key"),
                    ],
                    gateway=v1alpha1.Gateway(
                        hostname="test-backend.gateways.example.com",
                        clientCAs=[
                            v1alpha1.ClientCA(
                                name="eu",
                                certificate="-----BEGIN CERTIFICATE-----\nfleet\n-----END CERTIFICATE-----\n",
                            )
                        ],
                    ),
                ),
                resources={
                    "provider-config-kubernetes": _observed_ready(),
                    "provider-config-helm": _observed_ready(),
                    "gateway": _observed_ready(),
                    "gateway-class": _observed_ready(),
                    "gateway-ca-certificate": _observed_ready(),
                    "gateway-ca-issuer": _observed_ready(),
                    "gateway-serving-certificate": _observed_ready(),
                    "gateway-ca-bundle": _observed_ready(),
                    "gateway-ca-configmap": _observed_ready(),
                    "gateway-client-ca-bundle": _observed_ready(),
                    "gateway-client-auth": _observed_ready(),
                    "usage-gateway-class-by-gateway": _observed_ready(),
                    "usage-envoy-gateway-by-gateway-class": _observed_ready(),
                    "ai-gateway": _observed_ready(),
                    "ai-gateway-crds": _observed_ready(),
                    "dra-driver-critical-pods-quota": _observed_ready(),
                    "envoy-gateway": _observed_ready(),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _observed_ready(),
                    "gateway-namespace": _observed_ready(),
                    "gateway-proxy": _observed_ready(),
                    "gateway-selfsigned-issuer": _observed_ready(),
                    "trust-manager": _observed_ready(),
                    "usage-ai-gateway-crds-by-ai-gateway": _observed_ready(),
                    "usage-cert-manager-by-envoy-gateway": _observed_ready(),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-proxy": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _observed_ready(),
                    "cert-manager": _observed_ready(),
                    "kube-prometheus-stack": _observed_ready(),
                    "node-feature-discovery": _observed_ready(),
                    "nvidia-dra-driver-gpu": _observed_ready(),
                    "leader-worker-set": _observed_ready(),
                },
            ),
        ),
        want={
            # Every cloud and stack.
            "provider-config-kubernetes",
            "provider-config-helm",
            "gateway",
            "gateway-class",
            "gateway-ca-certificate",
            "gateway-ca-issuer",
            "gateway-serving-certificate",
            "gateway-ca-bundle",
            "gateway-ca-configmap",
            "gateway-client-ca-bundle",
            "gateway-client-auth",
            "usage-gateway-class-by-gateway",
            "usage-envoy-gateway-by-gateway-class",
            # The common components.
            "ai-gateway",
            "ai-gateway-crds",
            "dra-driver-critical-pods-quota",
            "envoy-gateway",
            "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io",
            "gaie-crds-inferencepools.inference.networking.k8s.io",
            "gaie-crds-inferencepools.inference.networking.x-k8s.io",
            "gateway-namespace",
            "gateway-proxy",
            "gateway-selfsigned-issuer",
            "trust-manager",
            "usage-ai-gateway-crds-by-ai-gateway",
            "usage-cert-manager-by-envoy-gateway",
            "usage-cert-manager-by-gateway-selfsigned-issuer",
            "usage-gateway-namespace-by-gateway-proxy",
            "usage-gateway-namespace-by-gateway-selfsigned-issuer",
            "usage-gateway-selfsigned-issuer-by-trust-manager",
            # The hand-written Nebius half.
            "cert-manager",
            "kube-prometheus-stack",
            "node-feature-discovery",
            "nvidia-dra-driver-gpu",
            # The Standard stack.
            "leader-worker-set",
        },
    ),
    ComposedResourceKeysCase(
        name="Nebius Dynamo",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Nebius",
                    stack="Dynamo",
                    secrets=[
                        v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                        v1alpha1.Secret(type="GoogleApplicationCredentials", name="sa-secret", key="private_key"),
                    ],
                    gateway=v1alpha1.Gateway(
                        hostname="test-backend.gateways.example.com",
                        clientCAs=[
                            v1alpha1.ClientCA(
                                name="eu",
                                certificate="-----BEGIN CERTIFICATE-----\nfleet\n-----END CERTIFICATE-----\n",
                            )
                        ],
                    ),
                ),
                resources={
                    "provider-config-kubernetes": _observed_ready(),
                    "provider-config-helm": _observed_ready(),
                    "gateway": _observed_ready(),
                    "gateway-class": _observed_ready(),
                    "gateway-ca-certificate": _observed_ready(),
                    "gateway-ca-issuer": _observed_ready(),
                    "gateway-serving-certificate": _observed_ready(),
                    "gateway-ca-bundle": _observed_ready(),
                    "gateway-ca-configmap": _observed_ready(),
                    "gateway-client-ca-bundle": _observed_ready(),
                    "gateway-client-auth": _observed_ready(),
                    "usage-gateway-class-by-gateway": _observed_ready(),
                    "usage-envoy-gateway-by-gateway-class": _observed_ready(),
                    "ai-gateway": _observed_ready(),
                    "ai-gateway-crds": _observed_ready(),
                    "dra-driver-critical-pods-quota": _observed_ready(),
                    "envoy-gateway": _observed_ready(),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _observed_ready(),
                    "gateway-namespace": _observed_ready(),
                    "gateway-proxy": _observed_ready(),
                    "gateway-selfsigned-issuer": _observed_ready(),
                    "trust-manager": _observed_ready(),
                    "usage-ai-gateway-crds-by-ai-gateway": _observed_ready(),
                    "usage-cert-manager-by-envoy-gateway": _observed_ready(),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-proxy": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _observed_ready(),
                    "cert-manager": _observed_ready(),
                    "kube-prometheus-stack": _observed_ready(),
                    "node-feature-discovery": _observed_ready(),
                    "nvidia-dra-driver-gpu": _observed_ready(),
                    "grove": _observed_ready(),
                    "kai-queue": _observed_ready(),
                    "kai-queue-root": _observed_ready(),
                    "kai-scheduler": _observed_ready(),
                    "modelexpress-crds-modelcacheentries.modelexpress.nvidia.com": _observed_ready(),
                    "modelexpress-crds-modelmetadatas.modelexpress.nvidia.com": _observed_ready(),
                    "modelexpress-server": _observed_ready(),
                    "modelexpress-server-role": _observed_ready(),
                    "modelexpress-server-rolebinding": _observed_ready(),
                    "modelexpress-server-sa": _observed_ready(),
                    "modelexpress-server-svc": _observed_ready(),
                    "usage-kai-scheduler-by-kai-queue": _observed_ready(),
                    "usage-kai-scheduler-by-kai-queue-root": _observed_ready(),
                    "usage-modelexpress-crds-modelcacheentries.modelexpress.nvidia.com-by-modelexpress-server": _observed_ready(),
                    "usage-modelexpress-crds-modelmetadatas.modelexpress.nvidia.com-by-modelexpress-server": _observed_ready(),
                },
            ),
        ),
        want={
            # Every cloud and stack.
            "provider-config-kubernetes",
            "provider-config-helm",
            "gateway",
            "gateway-class",
            "gateway-ca-certificate",
            "gateway-ca-issuer",
            "gateway-serving-certificate",
            "gateway-ca-bundle",
            "gateway-ca-configmap",
            "gateway-client-ca-bundle",
            "gateway-client-auth",
            "usage-gateway-class-by-gateway",
            "usage-envoy-gateway-by-gateway-class",
            # The common components.
            "ai-gateway",
            "ai-gateway-crds",
            "dra-driver-critical-pods-quota",
            "envoy-gateway",
            "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io",
            "gaie-crds-inferencepools.inference.networking.k8s.io",
            "gaie-crds-inferencepools.inference.networking.x-k8s.io",
            "gateway-namespace",
            "gateway-proxy",
            "gateway-selfsigned-issuer",
            "trust-manager",
            "usage-ai-gateway-crds-by-ai-gateway",
            "usage-cert-manager-by-envoy-gateway",
            "usage-cert-manager-by-gateway-selfsigned-issuer",
            "usage-gateway-namespace-by-gateway-proxy",
            "usage-gateway-namespace-by-gateway-selfsigned-issuer",
            "usage-gateway-selfsigned-issuer-by-trust-manager",
            # The hand-written Nebius half.
            "cert-manager",
            "kube-prometheus-stack",
            "node-feature-discovery",
            "nvidia-dra-driver-gpu",
            # The Dynamo stack.
            "grove",
            "kai-queue",
            "kai-queue-root",
            "kai-scheduler",
            "modelexpress-crds-modelcacheentries.modelexpress.nvidia.com",
            "modelexpress-crds-modelmetadatas.modelexpress.nvidia.com",
            "modelexpress-server",
            "modelexpress-server-role",
            "modelexpress-server-rolebinding",
            "modelexpress-server-sa",
            "modelexpress-server-svc",
            "usage-kai-scheduler-by-kai-queue",
            "usage-kai-scheduler-by-kai-queue-root",
            "usage-modelexpress-crds-modelcacheentries.modelexpress.nvidia.com-by-modelexpress-server",
            "usage-modelexpress-crds-modelmetadatas.modelexpress.nvidia.com-by-modelexpress-server",
        },
    ),
    ComposedResourceKeysCase(
        name="Vultr Standard",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Vultr",
                    stack="Standard",
                    secrets=[
                        v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                        v1alpha1.Secret(type="GoogleApplicationCredentials", name="sa-secret", key="private_key"),
                    ],
                    gateway=v1alpha1.Gateway(
                        hostname="test-backend.gateways.example.com",
                        clientCAs=[
                            v1alpha1.ClientCA(
                                name="eu",
                                certificate="-----BEGIN CERTIFICATE-----\nfleet\n-----END CERTIFICATE-----\n",
                            )
                        ],
                    ),
                ),
                resources={
                    "provider-config-kubernetes": _observed_ready(),
                    "provider-config-helm": _observed_ready(),
                    "gateway": _observed_ready(),
                    "gateway-class": _observed_ready(),
                    "gateway-ca-certificate": _observed_ready(),
                    "gateway-ca-issuer": _observed_ready(),
                    "gateway-serving-certificate": _observed_ready(),
                    "gateway-ca-bundle": _observed_ready(),
                    "gateway-ca-configmap": _observed_ready(),
                    "gateway-client-ca-bundle": _observed_ready(),
                    "gateway-client-auth": _observed_ready(),
                    "usage-gateway-class-by-gateway": _observed_ready(),
                    "usage-envoy-gateway-by-gateway-class": _observed_ready(),
                    "ai-gateway": _observed_ready(),
                    "ai-gateway-crds": _observed_ready(),
                    "dra-driver-critical-pods-quota": _observed_ready(),
                    "envoy-gateway": _observed_ready(),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _observed_ready(),
                    "gateway-namespace": _observed_ready(),
                    "gateway-proxy": _observed_ready(),
                    "gateway-selfsigned-issuer": _observed_ready(),
                    "trust-manager": _observed_ready(),
                    "usage-ai-gateway-crds-by-ai-gateway": _observed_ready(),
                    "usage-cert-manager-by-envoy-gateway": _observed_ready(),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-proxy": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _observed_ready(),
                    "cert-manager": _observed_ready(),
                    "kube-prometheus-stack": _observed_ready(),
                    "nvidia-dra-driver-gpu": _observed_ready(),
                    "leader-worker-set": _observed_ready(),
                },
            ),
        ),
        want={
            # Every cloud and stack.
            "provider-config-kubernetes",
            "provider-config-helm",
            "gateway",
            "gateway-class",
            "gateway-ca-certificate",
            "gateway-ca-issuer",
            "gateway-serving-certificate",
            "gateway-ca-bundle",
            "gateway-ca-configmap",
            "gateway-client-ca-bundle",
            "gateway-client-auth",
            "usage-gateway-class-by-gateway",
            "usage-envoy-gateway-by-gateway-class",
            # The common components.
            "ai-gateway",
            "ai-gateway-crds",
            "dra-driver-critical-pods-quota",
            "envoy-gateway",
            "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io",
            "gaie-crds-inferencepools.inference.networking.k8s.io",
            "gaie-crds-inferencepools.inference.networking.x-k8s.io",
            "gateway-namespace",
            "gateway-proxy",
            "gateway-selfsigned-issuer",
            "trust-manager",
            "usage-ai-gateway-crds-by-ai-gateway",
            "usage-cert-manager-by-envoy-gateway",
            "usage-cert-manager-by-gateway-selfsigned-issuer",
            "usage-gateway-namespace-by-gateway-proxy",
            "usage-gateway-namespace-by-gateway-selfsigned-issuer",
            "usage-gateway-selfsigned-issuer-by-trust-manager",
            # The hand-written Vultr half. VKE pre-installs NFD via its managed GPU
            # Operator add-on, so it carries no node-feature-discovery of its own.
            "cert-manager",
            "kube-prometheus-stack",
            "nvidia-dra-driver-gpu",
            # The Standard stack.
            "leader-worker-set",
        },
    ),
    ComposedResourceKeysCase(
        name="Vultr Dynamo",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Vultr",
                    stack="Dynamo",
                    secrets=[
                        v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                        v1alpha1.Secret(type="GoogleApplicationCredentials", name="sa-secret", key="private_key"),
                    ],
                    gateway=v1alpha1.Gateway(
                        hostname="test-backend.gateways.example.com",
                        clientCAs=[
                            v1alpha1.ClientCA(
                                name="eu",
                                certificate="-----BEGIN CERTIFICATE-----\nfleet\n-----END CERTIFICATE-----\n",
                            )
                        ],
                    ),
                ),
                resources={
                    "provider-config-kubernetes": _observed_ready(),
                    "provider-config-helm": _observed_ready(),
                    "gateway": _observed_ready(),
                    "gateway-class": _observed_ready(),
                    "gateway-ca-certificate": _observed_ready(),
                    "gateway-ca-issuer": _observed_ready(),
                    "gateway-serving-certificate": _observed_ready(),
                    "gateway-ca-bundle": _observed_ready(),
                    "gateway-ca-configmap": _observed_ready(),
                    "gateway-client-ca-bundle": _observed_ready(),
                    "gateway-client-auth": _observed_ready(),
                    "usage-gateway-class-by-gateway": _observed_ready(),
                    "usage-envoy-gateway-by-gateway-class": _observed_ready(),
                    "ai-gateway": _observed_ready(),
                    "ai-gateway-crds": _observed_ready(),
                    "dra-driver-critical-pods-quota": _observed_ready(),
                    "envoy-gateway": _observed_ready(),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _observed_ready(),
                    "gateway-namespace": _observed_ready(),
                    "gateway-proxy": _observed_ready(),
                    "gateway-selfsigned-issuer": _observed_ready(),
                    "trust-manager": _observed_ready(),
                    "usage-ai-gateway-crds-by-ai-gateway": _observed_ready(),
                    "usage-cert-manager-by-envoy-gateway": _observed_ready(),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-proxy": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _observed_ready(),
                    "cert-manager": _observed_ready(),
                    "kube-prometheus-stack": _observed_ready(),
                    "nvidia-dra-driver-gpu": _observed_ready(),
                    "grove": _observed_ready(),
                    "kai-queue": _observed_ready(),
                    "kai-queue-root": _observed_ready(),
                    "kai-scheduler": _observed_ready(),
                    "modelexpress-crds-modelcacheentries.modelexpress.nvidia.com": _observed_ready(),
                    "modelexpress-crds-modelmetadatas.modelexpress.nvidia.com": _observed_ready(),
                    "modelexpress-server": _observed_ready(),
                    "modelexpress-server-role": _observed_ready(),
                    "modelexpress-server-rolebinding": _observed_ready(),
                    "modelexpress-server-sa": _observed_ready(),
                    "modelexpress-server-svc": _observed_ready(),
                    "usage-kai-scheduler-by-kai-queue": _observed_ready(),
                    "usage-kai-scheduler-by-kai-queue-root": _observed_ready(),
                    "usage-modelexpress-crds-modelcacheentries.modelexpress.nvidia.com-by-modelexpress-server": _observed_ready(),
                    "usage-modelexpress-crds-modelmetadatas.modelexpress.nvidia.com-by-modelexpress-server": _observed_ready(),
                },
            ),
        ),
        want={
            # Every cloud and stack.
            "provider-config-kubernetes",
            "provider-config-helm",
            "gateway",
            "gateway-class",
            "gateway-ca-certificate",
            "gateway-ca-issuer",
            "gateway-serving-certificate",
            "gateway-ca-bundle",
            "gateway-ca-configmap",
            "gateway-client-ca-bundle",
            "gateway-client-auth",
            "usage-gateway-class-by-gateway",
            "usage-envoy-gateway-by-gateway-class",
            # The common components.
            "ai-gateway",
            "ai-gateway-crds",
            "dra-driver-critical-pods-quota",
            "envoy-gateway",
            "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io",
            "gaie-crds-inferencepools.inference.networking.k8s.io",
            "gaie-crds-inferencepools.inference.networking.x-k8s.io",
            "gateway-namespace",
            "gateway-proxy",
            "gateway-selfsigned-issuer",
            "trust-manager",
            "usage-ai-gateway-crds-by-ai-gateway",
            "usage-cert-manager-by-envoy-gateway",
            "usage-cert-manager-by-gateway-selfsigned-issuer",
            "usage-gateway-namespace-by-gateway-proxy",
            "usage-gateway-namespace-by-gateway-selfsigned-issuer",
            "usage-gateway-selfsigned-issuer-by-trust-manager",
            # The hand-written Vultr half. VKE pre-installs NFD via its managed GPU
            # Operator add-on, so it carries no node-feature-discovery of its own.
            "cert-manager",
            "kube-prometheus-stack",
            "nvidia-dra-driver-gpu",
            # The Dynamo stack.
            "grove",
            "kai-queue",
            "kai-queue-root",
            "kai-scheduler",
            "modelexpress-crds-modelcacheentries.modelexpress.nvidia.com",
            "modelexpress-crds-modelmetadatas.modelexpress.nvidia.com",
            "modelexpress-server",
            "modelexpress-server-role",
            "modelexpress-server-rolebinding",
            "modelexpress-server-sa",
            "modelexpress-server-svc",
            "usage-kai-scheduler-by-kai-queue",
            "usage-kai-scheduler-by-kai-queue-root",
            "usage-modelexpress-crds-modelcacheentries.modelexpress.nvidia.com-by-modelexpress-server",
            "usage-modelexpress-crds-modelmetadatas.modelexpress.nvidia.com-by-modelexpress-server",
        },
    ),
    ComposedResourceKeysCase(
        name="Existing Standard",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Existing",
                    stack="Standard",
                    secrets=[
                        v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                        v1alpha1.Secret(type="GoogleApplicationCredentials", name="sa-secret", key="private_key"),
                    ],
                    gateway=v1alpha1.Gateway(
                        hostname="test-backend.gateways.example.com",
                        clientCAs=[
                            v1alpha1.ClientCA(
                                name="eu",
                                certificate="-----BEGIN CERTIFICATE-----\nfleet\n-----END CERTIFICATE-----\n",
                            )
                        ],
                    ),
                ),
                resources={
                    "provider-config-kubernetes": _observed_ready(),
                    "provider-config-helm": _observed_ready(),
                    "gateway": _observed_ready(),
                    "gateway-class": _observed_ready(),
                    "gateway-ca-certificate": _observed_ready(),
                    "gateway-ca-issuer": _observed_ready(),
                    "gateway-serving-certificate": _observed_ready(),
                    "gateway-ca-bundle": _observed_ready(),
                    "gateway-ca-configmap": _observed_ready(),
                    "gateway-client-ca-bundle": _observed_ready(),
                    "gateway-client-auth": _observed_ready(),
                    "usage-gateway-class-by-gateway": _observed_ready(),
                    "usage-envoy-gateway-by-gateway-class": _observed_ready(),
                    "ai-gateway": _observed_ready(),
                    "ai-gateway-crds": _observed_ready(),
                    "dra-driver-critical-pods-quota": _observed_ready(),
                    "envoy-gateway": _observed_ready(),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _observed_ready(),
                    "gateway-namespace": _observed_ready(),
                    "gateway-proxy": _observed_ready(),
                    "gateway-selfsigned-issuer": _observed_ready(),
                    "trust-manager": _observed_ready(),
                    "usage-ai-gateway-crds-by-ai-gateway": _observed_ready(),
                    "usage-cert-manager-by-envoy-gateway": _observed_ready(),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-proxy": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _observed_ready(),
                    "cert-manager": _observed_ready(),
                    "kube-prometheus-stack": _observed_ready(),
                    "node-feature-discovery": _observed_ready(),
                    "nvidia-dra-driver-gpu": _observed_ready(),
                    "leader-worker-set": _observed_ready(),
                },
            ),
        ),
        want={
            # Every cloud and stack.
            "provider-config-kubernetes",
            "provider-config-helm",
            "gateway",
            "gateway-class",
            "gateway-ca-certificate",
            "gateway-ca-issuer",
            "gateway-serving-certificate",
            "gateway-ca-bundle",
            "gateway-ca-configmap",
            "gateway-client-ca-bundle",
            "gateway-client-auth",
            "usage-gateway-class-by-gateway",
            "usage-envoy-gateway-by-gateway-class",
            # The common components.
            "ai-gateway",
            "ai-gateway-crds",
            "dra-driver-critical-pods-quota",
            "envoy-gateway",
            "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io",
            "gaie-crds-inferencepools.inference.networking.k8s.io",
            "gaie-crds-inferencepools.inference.networking.x-k8s.io",
            "gateway-namespace",
            "gateway-proxy",
            "gateway-selfsigned-issuer",
            "trust-manager",
            "usage-ai-gateway-crds-by-ai-gateway",
            "usage-cert-manager-by-envoy-gateway",
            "usage-cert-manager-by-gateway-selfsigned-issuer",
            "usage-gateway-namespace-by-gateway-proxy",
            "usage-gateway-namespace-by-gateway-selfsigned-issuer",
            "usage-gateway-selfsigned-issuer-by-trust-manager",
            # The hand-written Existing half.
            "cert-manager",
            "kube-prometheus-stack",
            "node-feature-discovery",
            "nvidia-dra-driver-gpu",
            # The Standard stack.
            "leader-worker-set",
        },
    ),
    ComposedResourceKeysCase(
        name="Existing Dynamo",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Existing",
                    stack="Dynamo",
                    secrets=[
                        v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                        v1alpha1.Secret(type="GoogleApplicationCredentials", name="sa-secret", key="private_key"),
                    ],
                    gateway=v1alpha1.Gateway(
                        hostname="test-backend.gateways.example.com",
                        clientCAs=[
                            v1alpha1.ClientCA(
                                name="eu",
                                certificate="-----BEGIN CERTIFICATE-----\nfleet\n-----END CERTIFICATE-----\n",
                            )
                        ],
                    ),
                ),
                resources={
                    "provider-config-kubernetes": _observed_ready(),
                    "provider-config-helm": _observed_ready(),
                    "gateway": _observed_ready(),
                    "gateway-class": _observed_ready(),
                    "gateway-ca-certificate": _observed_ready(),
                    "gateway-ca-issuer": _observed_ready(),
                    "gateway-serving-certificate": _observed_ready(),
                    "gateway-ca-bundle": _observed_ready(),
                    "gateway-ca-configmap": _observed_ready(),
                    "gateway-client-ca-bundle": _observed_ready(),
                    "gateway-client-auth": _observed_ready(),
                    "usage-gateway-class-by-gateway": _observed_ready(),
                    "usage-envoy-gateway-by-gateway-class": _observed_ready(),
                    "ai-gateway": _observed_ready(),
                    "ai-gateway-crds": _observed_ready(),
                    "dra-driver-critical-pods-quota": _observed_ready(),
                    "envoy-gateway": _observed_ready(),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _observed_ready(),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _observed_ready(),
                    "gateway-namespace": _observed_ready(),
                    "gateway-proxy": _observed_ready(),
                    "gateway-selfsigned-issuer": _observed_ready(),
                    "trust-manager": _observed_ready(),
                    "usage-ai-gateway-crds-by-ai-gateway": _observed_ready(),
                    "usage-cert-manager-by-envoy-gateway": _observed_ready(),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-proxy": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _observed_ready(),
                    "cert-manager": _observed_ready(),
                    "kube-prometheus-stack": _observed_ready(),
                    "node-feature-discovery": _observed_ready(),
                    "nvidia-dra-driver-gpu": _observed_ready(),
                    "grove": _observed_ready(),
                    "kai-queue": _observed_ready(),
                    "kai-queue-root": _observed_ready(),
                    "kai-scheduler": _observed_ready(),
                    "modelexpress-crds-modelcacheentries.modelexpress.nvidia.com": _observed_ready(),
                    "modelexpress-crds-modelmetadatas.modelexpress.nvidia.com": _observed_ready(),
                    "modelexpress-server": _observed_ready(),
                    "modelexpress-server-role": _observed_ready(),
                    "modelexpress-server-rolebinding": _observed_ready(),
                    "modelexpress-server-sa": _observed_ready(),
                    "modelexpress-server-svc": _observed_ready(),
                    "usage-kai-scheduler-by-kai-queue": _observed_ready(),
                    "usage-kai-scheduler-by-kai-queue-root": _observed_ready(),
                    "usage-modelexpress-crds-modelcacheentries.modelexpress.nvidia.com-by-modelexpress-server": _observed_ready(),
                    "usage-modelexpress-crds-modelmetadatas.modelexpress.nvidia.com-by-modelexpress-server": _observed_ready(),
                },
            ),
        ),
        want={
            # Every cloud and stack.
            "provider-config-kubernetes",
            "provider-config-helm",
            "gateway",
            "gateway-class",
            "gateway-ca-certificate",
            "gateway-ca-issuer",
            "gateway-serving-certificate",
            "gateway-ca-bundle",
            "gateway-ca-configmap",
            "gateway-client-ca-bundle",
            "gateway-client-auth",
            "usage-gateway-class-by-gateway",
            "usage-envoy-gateway-by-gateway-class",
            # The common components.
            "ai-gateway",
            "ai-gateway-crds",
            "dra-driver-critical-pods-quota",
            "envoy-gateway",
            "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io",
            "gaie-crds-inferencepools.inference.networking.k8s.io",
            "gaie-crds-inferencepools.inference.networking.x-k8s.io",
            "gateway-namespace",
            "gateway-proxy",
            "gateway-selfsigned-issuer",
            "trust-manager",
            "usage-ai-gateway-crds-by-ai-gateway",
            "usage-cert-manager-by-envoy-gateway",
            "usage-cert-manager-by-gateway-selfsigned-issuer",
            "usage-gateway-namespace-by-gateway-proxy",
            "usage-gateway-namespace-by-gateway-selfsigned-issuer",
            "usage-gateway-selfsigned-issuer-by-trust-manager",
            # The hand-written Existing half.
            "cert-manager",
            "kube-prometheus-stack",
            "node-feature-discovery",
            "nvidia-dra-driver-gpu",
            # The Dynamo stack.
            "grove",
            "kai-queue",
            "kai-queue-root",
            "kai-scheduler",
            "modelexpress-crds-modelcacheentries.modelexpress.nvidia.com",
            "modelexpress-crds-modelmetadatas.modelexpress.nvidia.com",
            "modelexpress-server",
            "modelexpress-server-role",
            "modelexpress-server-rolebinding",
            "modelexpress-server-sa",
            "modelexpress-server-svc",
            "usage-kai-scheduler-by-kai-queue",
            "usage-kai-scheduler-by-kai-queue-root",
            "usage-modelexpress-crds-modelcacheentries.modelexpress.nvidia.com-by-modelexpress-server",
            "usage-modelexpress-crds-modelmetadatas.modelexpress.nvidia.com-by-modelexpress-server",
        },
    ),
]


@pytest.mark.parametrize("case", COMPOSED_RESOURCE_KEYS_CASES, ids=lambda case: case.name)
def test_composed_resource_keys(case: ComposedResourceKeysCase) -> None:
    """RunFunction composes exactly the inventoried resource keys for a cloud and stack."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert set(got.desired.resources) == case.want
