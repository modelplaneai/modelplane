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
                "version": "v1.8.1",
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
                                },
                            },
                            "service": {
                                "fqdn": {
                                    "hostname": "ai-gateway-controller.envoy-ai-gateway-system.svc.cluster.local",
                                    "port": 1063,
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


def _gaie_crd_desired(ready: bool) -> dict:
    """The GAIE CRDs composed as provider-kubernetes Objects on the remote
    cluster, built from the same vendored bundle and helper the function
    composes so the test stays in sync. When ready is True each Object is marked
    READY_TRUE, matching a pass where the Objects are observed Ready."""
    out = {}
    for doc in fn._GAIE_CRDS:
        key = fn._gaie_crd_key(doc)
        res = fnv1.Resource()
        resource.update(res, fn._k8s_object(_PC_NAME, doc))
        if ready:
            res.ready = fnv1.READY_TRUE
        out[key] = res
    return out


def _gaie_crd_observed() -> dict:
    """Observed GAIE CRD Objects, each reporting Ready=True."""
    out = {}
    for doc in fn._GAIE_CRDS:
        out[fn._gaie_crd_key(doc)] = fnv1.Resource(
            resource=resource.dict_to_struct(
                {
                    "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                    "kind": "Object",
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                }
            )
        )
    return out


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
        "readiness": {
            "policy": "DeriveFromCelQuery",
            "celQuery": fn._GATEWAY_READY_CEL,
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
                "nvidiaDriverRoot": "/home/kubernetes/bin/nvidia",
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_DRA_DRIVER_QUOTA = {
    "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
    "kind": "Object",
    "spec": {
        "forProvider": {
            "manifest": {
                "apiVersion": "v1",
                "kind": "ResourceQuota",
                "metadata": {
                    "name": "allow-critical-pods",
                    "namespace": "dra-driver-nvidia-gpu",
                },
                "spec": {
                    "hard": {"pods": "1000"},
                    "scopeSelector": {
                        "matchExpressions": [
                            {
                                "operator": "In",
                                "scopeName": "PriorityClass",
                                "values": [
                                    "system-node-critical",
                                    "system-cluster-critical",
                                ],
                            },
                        ],
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


def _base_request(nvidia_driver_root: str = "/home/kubernetes/bin/nvidia") -> fnv1.RunFunctionRequest:
    """Build the base RunFunctionRequest used by all test cases.

    Defaults to the GKE driver root, which drives the DRA driver's
    nvidiaDriverRoot override and the critical-pods quota.
    """
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
                            nvidiaDriverRoot=nvidia_driver_root,
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
                    "cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(_CERT_MANAGER),
                    ),
                    "envoy-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_ENVOY_GATEWAY),
                    ),
                    "ai-gateway-crds": fnv1.Resource(
                        resource=resource.dict_to_struct(_AI_GATEWAY_CRDS),
                    ),
                    "ai-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_AI_GATEWAY),
                    ),
                    **_gaie_crd_desired(ready=False),
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
                    "dra-driver-critical-pods-quota": fnv1.Resource(
                        resource=resource.dict_to_struct(_DRA_DRIVER_QUOTA),
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

    async def test_default_driver_root_skips_override_keeps_quota(self) -> None:
        """With the default driver root (/), e.g. EKS, the DRA driver gets no
        nvidiaDriverRoot override, but the critical-pods quota is still composed
        (it's laid down everywhere — harmless where priority isn't restricted)."""
        req = _base_request(nvidia_driver_root="/")
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

        got = await self.runner.RunFunction(req, None)

        self.assertIn("dra-driver-critical-pods-quota", got.desired.resources)
        dra_values = resource.struct_to_dict(got.desired.resources["dra-driver"].resource)["spec"]["forProvider"][
            "values"
        ]
        self.assertNotIn("nvidiaDriverRoot", dra_values)

    async def test_gateway_gated_on_address(self) -> None:
        """The Gateway Object carries the DeriveFromCelQuery readiness, and is
        only marked ready once provider-kubernetes reports Ready=True (which it
        derives from the address-gating CEL query). This keeps the Object on the
        fast re-observe poll until the LoadBalancer address is observed, instead
        of freezing at a pre-address snapshot on the slow drift poll (#121)."""
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

        # Before the address is observed there's no Ready condition: the desired
        # Gateway Object must not be marked ready, and no address is surfaced.
        req.observed.resources["gateway"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {"apiVersion": "kubernetes.m.crossplane.io/v1alpha1", "kind": "Object"}
                ),
            ),
        )
        got = await self.runner.RunFunction(req, None)
        self.assertEqual(
            got.desired.resources["gateway"].ready,
            fnv1.READY_UNSPECIFIED,
            "gateway must not be ready before its address is observed",
        )
        self.assertEqual(
            resource.struct_to_dict(got.desired.resources["gateway"].resource)["spec"]["readiness"],
            {"policy": "DeriveFromCelQuery", "celQuery": fn._GATEWAY_READY_CEL},
            "gateway Object must gate readiness on its address via CEL",
        )
        self.assertNotIn(
            "gateway",
            resource.struct_to_dict(got.desired.composite.resource).get("status", {}),
            "no gateway address should be surfaced before it's observed",
        )

        # Once provider-kubernetes derives Ready=True from the CEL query (the
        # address is now in the observed manifest), the Object is marked ready
        # and the address propagates to the XR status.
        req.observed.resources["gateway"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "status": {
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "atProvider": {
                                "manifest": {"status": {"addresses": [{"value": "172.18.255.200"}]}},
                            },
                        },
                    }
                ),
            ),
        )
        got = await self.runner.RunFunction(req, None)
        self.assertEqual(
            got.desired.resources["gateway"].ready,
            fnv1.READY_TRUE,
            "gateway must be ready once provider-kubernetes observes the address",
        )
        self.assertEqual(
            resource.struct_to_dict(got.desired.composite.resource)["status"]["gateway"]["address"],
            "172.18.255.200",
            "gateway address must surface to the XR status once observed",
        )

    async def test_third_pass(self) -> None:
        """Steady state: composed releases report Ready, and the gateway address is
        surfaced from the observed Object's manifest. The observed gateway Object
        carries no Ready condition here, so the gateway Object itself stays unready."""
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
        for r in ("cert-manager", "ai-gateway-crds", "ai-gateway"):
            req.observed.resources[r].CopyFrom(
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
        for key, observed in _gaie_crd_observed().items():
            req.observed.resources[key].CopyFrom(observed)

        want = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {"status": {"gateway": {"address": "172.18.255.200"}}},
                    ),
                ),
                resources={
                    "cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(_CERT_MANAGER),
                        ready=fnv1.READY_TRUE,
                    ),
                    "envoy-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_ENVOY_GATEWAY),
                    ),
                    "ai-gateway-crds": fnv1.Resource(
                        resource=resource.dict_to_struct(_AI_GATEWAY_CRDS),
                        ready=fnv1.READY_TRUE,
                    ),
                    "ai-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_AI_GATEWAY),
                        ready=fnv1.READY_TRUE,
                    ),
                    **_gaie_crd_desired(ready=True),
                    # The Gateway Object's observed manifest carries the
                    # address, so write_status surfaces it - but the observed
                    # Object has no Ready condition here, so it stays unready.
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
                    "dra-driver-critical-pods-quota": fnv1.Resource(
                        resource=resource.dict_to_struct(_DRA_DRIVER_QUOTA),
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
