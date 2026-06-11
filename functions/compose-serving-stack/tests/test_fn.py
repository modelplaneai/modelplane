"""Tests for the compose-serving-stack function."""

import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.infrastructure.servingstack import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


# Precomputed child_name values for test-backend.
_PC_NAME = "test-backend-cluster-63fde"

# Shared resource dicts used across test cases.
_PROVIDER_CONFIG_KUBERNETES = {
    "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
    "kind": "ProviderConfig",
    "metadata": {"name": _PC_NAME},
    "spec": {
        "credentials": {
            "secretRef": {
                "key": "kubeconfig",
                "name": "kube-secret",
                "namespace": "test-ns",
            },
            "source": "Secret",
        },
        "identity": {
            "secretRef": {
                "key": "private_key",
                "name": "sa-secret",
                "namespace": "test-ns",
            },
            "source": "Secret",
            "type": "GoogleApplicationCredentials",
        },
    },
}

_PROVIDER_CONFIG_HELM = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "ProviderConfig",
    "metadata": {"name": _PC_NAME},
    "spec": {
        "credentials": {
            "secretRef": {
                "key": "kubeconfig",
                "name": "kube-secret",
                "namespace": "test-ns",
            },
            "source": "Secret",
        },
        "identity": {
            "secretRef": {
                "key": "private_key",
                "name": "sa-secret",
                "namespace": "test-ns",
            },
            "source": "Secret",
            "type": "GoogleApplicationCredentials",
        },
    },
}

_USAGE_HELM_PC = {
    "apiVersion": "protection.crossplane.io/v1beta1",
    "kind": "Usage",
    "spec": {
        "by": {
            "apiVersion": "helm.m.crossplane.io/v1beta1",
            "kind": "Release",
            "resourceSelector": {"matchControllerRef": True},
        },
        "of": {
            "apiVersion": "helm.m.crossplane.io/v1beta1",
            "kind": "ProviderConfig",
            "resourceRef": {"name": _PC_NAME},
        },
        "replayDeletion": True,
    },
}

_USAGE_K8S_PC = {
    "apiVersion": "protection.crossplane.io/v1beta1",
    "kind": "Usage",
    "spec": {
        "by": {
            "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
            "kind": "Object",
            "resourceSelector": {"matchControllerRef": True},
        },
        "of": {
            "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
            "kind": "ProviderConfig",
            "resourceRef": {"name": _PC_NAME},
        },
        "replayDeletion": True,
    },
}

_USAGE_ENVOY_GW_BY_GATEWAY_CLASS = {
    "apiVersion": "protection.crossplane.io/v1beta1",
    "kind": "Usage",
    "spec": {
        "by": {
            "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
            "kind": "Object",
            "resourceSelector": {
                "matchControllerRef": True,
                "matchLabels": {"modelplane.ai/resource": "gateway-class"},
            },
        },
        "of": {
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

_USAGE_GATEWAY_CLASS_BY_GATEWAY = {
    "apiVersion": "protection.crossplane.io/v1beta1",
    "kind": "Usage",
    "spec": {
        "by": {
            "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
            "kind": "Object",
            "resourceSelector": {
                "matchControllerRef": True,
                "matchLabels": {"modelplane.ai/resource": "gateway"},
            },
        },
        "of": {
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

_CERT_MANAGER = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "spec": {
        "forProvider": {
            "chart": {
                "name": "cert-manager",
                "repository": "https://charts.jetstack.io",
                "version": "v1.17.1",
            },
            "namespace": "cert-manager",
            "values": {
                "crds": {
                    "enabled": True,
                    "keep": False,
                },
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_ENVOY_GATEWAY = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "metadata": {
        "labels": {"modelplane.ai/resource": "envoy-gateway"},
    },
    "spec": {
        "forProvider": {
            "chart": {
                "name": "gateway-helm",
                "repository": "oci://docker.io/envoyproxy",
                "version": "v1.3.0",
            },
            "namespace": "envoy-gateway-system",
            "values": {
                "config": {
                    "envoyGateway": {
                        "gateway": {
                            "controllerName": "gateway.envoyproxy.io/gatewayclass-controller",
                        },
                        "logging": {"level": {"default": "info"}},
                        "provider": {"type": "Kubernetes"},
                        "extensionApis": {
                            "enableEnvoyPatchPolicy": True,
                            "enableBackend": True,
                        },
                        "extensionManager": {
                            "hooks": {
                                "xdsTranslator": {
                                    "translation": {
                                        "listener": {"includeAll": True},
                                        "route": {"includeAll": True},
                                        "cluster": {"includeAll": True},
                                        "secret": {"includeAll": True},
                                    },
                                    "post": [
                                        "Translation",
                                        "Cluster",
                                        "Route",
                                    ],
                                },
                            },
                            "service": {
                                "fqdn": {
                                    "hostname": "ai-gateway-controller.envoy-ai-gateway-system.svc.cluster.local",
                                    "port": 1063.0,
                                },
                            },
                            "backendResources": [
                                {
                                    "group": "inference.networking.k8s.io",
                                    "kind": "InferencePool",
                                    "version": "v1",
                                },
                            ],
                        },
                    },
                },
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_AI_GATEWAY_CRDS = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "spec": {
        "forProvider": {
            "chart": {
                "name": "ai-gateway-crds-helm",
                "repository": "oci://docker.io/envoyproxy",
                "version": "v0.7.0",
            },
            "namespace": "envoy-ai-gateway-system",
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_AI_GATEWAY = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "spec": {
        "forProvider": {
            "chart": {
                "name": "ai-gateway-helm",
                "repository": "oci://docker.io/envoyproxy",
                "version": "v0.7.0",
            },
            "namespace": "envoy-ai-gateway-system",
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_GAIE_CRDS = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "spec": {
        "forProvider": {
            "chart": {
                "name": "inferencepool",
                "repository": "oci://ghcr.io/kubernetes-sigs/gateway-api-inference-extension/charts",
                "version": "v1.0.1",
            },
            "namespace": "gateway-api-inference-extension",
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_GATEWAY = {
    "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
    "kind": "Object",
    "metadata": {
        "labels": {"modelplane.ai/resource": "gateway"},
    },
    "spec": {
        "forProvider": {
            "manifest": {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "Gateway",
                "metadata": {
                    "name": "inference-gateway",
                    "namespace": "modelplane-system",
                },
                "spec": {
                    "gatewayClassName": "envoy",
                    "listeners": [
                        {
                            "allowedRoutes": {
                                "namespaces": {"from": "All"},
                            },
                            "name": "http",
                            "port": 80.0,
                            "protocol": "HTTP",
                        },
                    ],
                },
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_GATEWAY_NAMESPACE = {
    "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
    "kind": "Object",
    "spec": {
        "forProvider": {
            "manifest": {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": "modelplane-system"},
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_GATEWAY_CLASS = {
    "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
    "kind": "Object",
    "metadata": {
        "labels": {"modelplane.ai/resource": "gateway-class"},
    },
    "spec": {
        "forProvider": {
            "manifest": {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "GatewayClass",
                "metadata": {"name": "envoy"},
                "spec": {
                    "controllerName": "gateway.envoyproxy.io/gatewayclass-controller",
                },
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_LEADER_WORKER_SET = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "spec": {
        "forProvider": {
            "chart": {
                "name": "lws",
                "repository": "oci://registry.k8s.io/lws/charts",
                "version": "v0.8.0",
            },
            "namespace": "lws-system",
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_NODE_FEATURE_DISCOVERY = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "spec": {
        "forProvider": {
            "chart": {
                "name": "node-feature-discovery",
                "repository": "oci://registry.k8s.io/nfd/charts",
                "version": "0.18.3",
            },
            "namespace": "node-feature-discovery",
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_DRA_DRIVER = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "spec": {
        "forProvider": {
            "chart": {
                "name": "dra-driver-nvidia-gpu",
                "repository": "oci://registry.k8s.io/dra-driver-nvidia/charts",
                "version": "0.4.0",
            },
            "namespace": "dra-driver-nvidia-gpu",
            "values": {
                "gpuResourcesEnabledOverride": True,
                "resources": {"computeDomains": {"enabled": False}},
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_PROMETHEUS = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "spec": {
        "forProvider": {
            "chart": {
                "name": "kube-prometheus-stack",
                "repository": "https://prometheus-community.github.io/helm-charts",
                "version": "72.6.2",
            },
            "namespace": "monitoring",
            "values": {
                "alertmanager": {"enabled": False},
                "fullnameOverride": "prometheus",
                "grafana": {"enabled": False},
                "prometheus": {
                    "prometheusSpec": {
                        "additionalScrapeConfigs": [
                            {
                                "job_name": "envoy-gateway-proxy",
                                "kubernetes_sd_configs": [
                                    {
                                        "namespaces": {
                                            "names": ["envoy-gateway-system"],
                                        },
                                        "role": "pod",
                                    },
                                ],
                                "metrics_path": "/stats/prometheus",
                                "relabel_configs": [
                                    {
                                        "action": "keep",
                                        "regex": "proxy",
                                        "source_labels": [
                                            "__meta_kubernetes_pod_label_app_kubernetes_io_component",
                                        ],
                                    },
                                    {
                                        "action": "replace",
                                        "regex": "([^:]+)(?::\\d+)?",
                                        "replacement": "$1:19001",
                                        "source_labels": ["__address__"],
                                        "target_label": "__address__",
                                    },
                                ],
                            },
                        ],
                        "podMonitorNamespaceSelector": {},
                        "podMonitorSelectorNilUsesHelmValues": False,
                    },
                },
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}


def _base_request() -> fnv1.RunFunctionRequest:
    """Build the base RunFunctionRequest used by all test cases."""
    return fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(
                resource=resource.dict_to_struct(
                    v1alpha1.ServingStack(
                        metadata=metav1.ObjectMeta(
                            name="test-backend",
                            namespace="test-ns",
                        ),
                        spec=v1alpha1.Spec(
                            secrets=[
                                v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                                v1alpha1.Secret(type="GCPServiceAccountKey", name="sa-secret", key="private_key"),
                            ],
                        ),
                    ).model_dump(exclude_none=True, mode="json")
                ),
            ),
        ),
    )


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    """Tests for FunctionRunner.RunFunction."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_first_pass(self) -> None:
        """First pass composes provider configs and usages; releases gated."""
        req = _base_request()

        want = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct({"status": {}}),
                ),
                resources={
                    "provider-config-helm": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROVIDER_CONFIG_HELM),
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-kubernetes": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROVIDER_CONFIG_KUBERNETES),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-envoy-gw-by-gateway-class": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_ENVOY_GW_BY_GATEWAY_CLASS),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-gateway-class-by-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_GATEWAY_CLASS_BY_GATEWAY),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-helm-pc": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_HELM_PC),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-k8s-pc": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_K8S_PC),
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
        )

        got = await self.runner.RunFunction(req, None)
        self.assertEqual(
            json_format.MessageToDict(want),
            json_format.MessageToDict(got),
            "-want, +got",
        )

    async def test_second_pass(self) -> None:
        """Observed PCs ungate Helm releases, CRD objects, and gateway objects."""
        req = _base_request()
        req.observed.resources["provider-config-helm"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {"apiVersion": "helm.m.crossplane.io/v1beta1", "kind": "ProviderConfig"}
                ),
            ),
        )
        req.observed.resources["provider-config-kubernetes"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {"apiVersion": "kubernetes.m.crossplane.io/v1alpha1", "kind": "ProviderConfig"}
                ),
            ),
        )

        want = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct({"status": {}}),
                ),
                resources={
                    "ai-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_AI_GATEWAY),
                    ),
                    "ai-gateway-crds": fnv1.Resource(
                        resource=resource.dict_to_struct(_AI_GATEWAY_CRDS),
                    ),
                    "cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(_CERT_MANAGER),
                    ),
                    "envoy-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_ENVOY_GATEWAY),
                    ),
                    "gaie-crds": fnv1.Resource(
                        resource=resource.dict_to_struct(_GAIE_CRDS),
                    ),
                    "gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_GATEWAY),
                    ),
                    "gateway-namespace": fnv1.Resource(
                        resource=resource.dict_to_struct(_GATEWAY_NAMESPACE),
                    ),
                    "gateway-class": fnv1.Resource(
                        resource=resource.dict_to_struct(_GATEWAY_CLASS),
                    ),
                    "leader-worker-set": fnv1.Resource(
                        resource=resource.dict_to_struct(_LEADER_WORKER_SET),
                    ),
                    "node-feature-discovery": fnv1.Resource(
                        resource=resource.dict_to_struct(_NODE_FEATURE_DISCOVERY),
                    ),
                    "dra-driver": fnv1.Resource(
                        resource=resource.dict_to_struct(_DRA_DRIVER),
                    ),
                    "prometheus": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROMETHEUS),
                    ),
                    "provider-config-helm": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROVIDER_CONFIG_HELM),
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-kubernetes": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROVIDER_CONFIG_KUBERNETES),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-envoy-gw-by-gateway-class": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_ENVOY_GW_BY_GATEWAY_CLASS),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-gateway-class-by-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_GATEWAY_CLASS_BY_GATEWAY),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-helm-pc": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_HELM_PC),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-k8s-pc": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_K8S_PC),
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
        )

        got = await self.runner.RunFunction(req, None)
        self.assertEqual(
            json_format.MessageToDict(want),
            json_format.MessageToDict(got),
            "-want, +got",
        )

    async def test_third_pass(self) -> None:
        """Steady state: observed readiness propagates and the gateway address is surfaced."""
        req = _base_request()
        req.observed.resources["provider-config-helm"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {"apiVersion": "helm.m.crossplane.io/v1beta1", "kind": "ProviderConfig"}
                ),
            ),
        )
        req.observed.resources["provider-config-kubernetes"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {"apiVersion": "kubernetes.m.crossplane.io/v1alpha1", "kind": "ProviderConfig"}
                ),
            ),
        )
        req.observed.resources["cert-manager"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {
                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                        "kind": "Release",
                        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    }
                ),
            ),
        )
        req.observed.resources["gateway"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "status": {
                            "atProvider": {
                                "manifest": {"status": {"addresses": [{"value": "172.18.255.200"}]}},
                            },
                        },
                    }
                ),
            ),
        )

        want = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {"status": {"gateway": {"address": "172.18.255.200"}}},
                    ),
                ),
                resources={
                    "ai-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_AI_GATEWAY),
                    ),
                    "ai-gateway-crds": fnv1.Resource(
                        resource=resource.dict_to_struct(_AI_GATEWAY_CRDS),
                    ),
                    "cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(_CERT_MANAGER),
                        ready=fnv1.READY_TRUE,
                    ),
                    "envoy-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_ENVOY_GATEWAY),
                    ),
                    "gaie-crds": fnv1.Resource(
                        resource=resource.dict_to_struct(_GAIE_CRDS),
                    ),
                    "gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_GATEWAY),
                    ),
                    "gateway-namespace": fnv1.Resource(
                        resource=resource.dict_to_struct(_GATEWAY_NAMESPACE),
                    ),
                    "gateway-class": fnv1.Resource(
                        resource=resource.dict_to_struct(_GATEWAY_CLASS),
                    ),
                    "leader-worker-set": fnv1.Resource(
                        resource=resource.dict_to_struct(_LEADER_WORKER_SET),
                    ),
                    "node-feature-discovery": fnv1.Resource(
                        resource=resource.dict_to_struct(_NODE_FEATURE_DISCOVERY),
                    ),
                    "dra-driver": fnv1.Resource(
                        resource=resource.dict_to_struct(_DRA_DRIVER),
                    ),
                    "prometheus": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROMETHEUS),
                    ),
                    "provider-config-helm": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROVIDER_CONFIG_HELM),
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-kubernetes": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROVIDER_CONFIG_KUBERNETES),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-envoy-gw-by-gateway-class": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_ENVOY_GW_BY_GATEWAY_CLASS),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-gateway-class-by-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_GATEWAY_CLASS_BY_GATEWAY),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-helm-pc": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_HELM_PC),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-k8s-pc": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_K8S_PC),
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
        )

        got = await self.runner.RunFunction(req, None)
        self.assertEqual(
            json_format.MessageToDict(want),
            json_format.MessageToDict(got),
            "-want, +got",
        )


def _second_pass_request() -> fnv1.RunFunctionRequest:
    """Build a second-pass request (both ProviderConfigs observed)."""
    req = _base_request()
    req.observed.resources["provider-config-helm"].CopyFrom(
        fnv1.Resource(
            resource=resource.dict_to_struct(
                {"apiVersion": "helm.m.crossplane.io/v1beta1", "kind": "ProviderConfig"}
            ),
        ),
    )
    req.observed.resources["provider-config-kubernetes"].CopyFrom(
        fnv1.Resource(
            resource=resource.dict_to_struct(
                {"apiVersion": "kubernetes.m.crossplane.io/v1alpha1", "kind": "ProviderConfig"}
            ),
        ),
    )
    return req


class TestAiGatewayReleases(unittest.IsolatedAsyncioTestCase):
    """Tests for P2.2: Envoy AI Gateway + GAIE CRD releases."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def _run(self) -> fnv1.RunFunctionResponse:
        return await self.runner.RunFunction(_second_pass_request(), None)

    async def test_ai_gateway_crds_release_present(self) -> None:
        """ai-gateway-crds Release is composed with the correct chart and version."""
        got = await self._run()
        resources = json_format.MessageToDict(got).get("desired", {}).get("resources", {})
        self.assertIn("ai-gateway-crds", resources, "ai-gateway-crds Release missing")
        chart = (
            resources["ai-gateway-crds"]
            .get("resource", {})
            .get("spec", {})
            .get("forProvider", {})
            .get("chart", {})
        )
        self.assertEqual(chart.get("name"), "ai-gateway-crds-helm")
        self.assertEqual(chart.get("version"), "v0.7.0")
        self.assertEqual(
            resources["ai-gateway-crds"]["resource"]["spec"]["forProvider"]["namespace"],
            "envoy-ai-gateway-system",
        )

    async def test_ai_gateway_release_present(self) -> None:
        """ai-gateway Release is composed with the correct chart and version."""
        got = await self._run()
        resources = json_format.MessageToDict(got).get("desired", {}).get("resources", {})
        self.assertIn("ai-gateway", resources, "ai-gateway Release missing")
        chart = (
            resources["ai-gateway"]
            .get("resource", {})
            .get("spec", {})
            .get("forProvider", {})
            .get("chart", {})
        )
        self.assertEqual(chart.get("name"), "ai-gateway-helm")
        self.assertEqual(chart.get("version"), "v0.7.0")
        self.assertEqual(
            resources["ai-gateway"]["resource"]["spec"]["forProvider"]["namespace"],
            "envoy-ai-gateway-system",
        )

    async def test_gaie_crds_release_present(self) -> None:
        """gaie-crds Release is composed (Helm-based GAIE CRD install)."""
        got = await self._run()
        resources = json_format.MessageToDict(got).get("desired", {}).get("resources", {})
        self.assertIn("gaie-crds", resources, "gaie-crds Release missing")
        chart = (
            resources["gaie-crds"]
            .get("resource", {})
            .get("spec", {})
            .get("forProvider", {})
            .get("chart", {})
        )
        self.assertEqual(chart.get("name"), "inferencepool")
        self.assertEqual(chart.get("version"), "v1.0.1")

    async def test_envoy_gateway_values_enable_backend(self) -> None:
        """gateway-helm Release values include extensionApis.enableBackend: true."""
        got = await self._run()
        resources = json_format.MessageToDict(got).get("desired", {}).get("resources", {})
        values = (
            resources.get("envoy-gateway", {})
            .get("resource", {})
            .get("spec", {})
            .get("forProvider", {})
            .get("values", {})
        )
        extension_apis = values.get("config", {}).get("envoyGateway", {}).get("extensionApis", {})
        self.assertTrue(extension_apis.get("enableBackend"), "extensionApis.enableBackend must be True")

    async def test_envoy_gateway_values_backend_resources_inference_pool(self) -> None:
        """gateway-helm Release values include backendResources entry for InferencePool."""
        got = await self._run()
        resources = json_format.MessageToDict(got).get("desired", {}).get("resources", {})
        values = (
            resources.get("envoy-gateway", {})
            .get("resource", {})
            .get("spec", {})
            .get("forProvider", {})
            .get("values", {})
        )
        backend_resources = (
            values.get("config", {})
            .get("envoyGateway", {})
            .get("extensionManager", {})
            .get("backendResources", [])
        )
        self.assertTrue(
            any(
                br.get("group") == "inference.networking.k8s.io"
                and br.get("kind") == "InferencePool"
                and br.get("version") == "v1"
                for br in backend_resources
            ),
            f"InferencePool backendResources entry not found; got: {backend_resources}",
        )

    async def test_envoy_gateway_values_extension_manager_service(self) -> None:
        """gateway-helm Release values wire the AI Gateway controller endpoint."""
        got = await self._run()
        resources = json_format.MessageToDict(got).get("desired", {}).get("resources", {})
        values = (
            resources.get("envoy-gateway", {})
            .get("resource", {})
            .get("spec", {})
            .get("forProvider", {})
            .get("values", {})
        )
        fqdn = (
            values.get("config", {})
            .get("envoyGateway", {})
            .get("extensionManager", {})
            .get("service", {})
            .get("fqdn", {})
        )
        self.assertEqual(
            fqdn.get("hostname"),
            "ai-gateway-controller.envoy-ai-gateway-system.svc.cluster.local",
        )
        self.assertEqual(fqdn.get("port"), 1063.0)

    async def test_ai_gateway_releases_gated_on_first_pass(self) -> None:
        """AI Gateway releases are absent on first pass (ProviderConfigs not yet observed)."""
        got = await self.runner.RunFunction(_base_request(), None)
        resources = json_format.MessageToDict(got).get("desired", {}).get("resources", {})
        self.assertNotIn("ai-gateway-crds", resources)
        self.assertNotIn("ai-gateway", resources)
        self.assertNotIn("gaie-crds", resources)
