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

Three tables. COMPOSE_CASES compares whole RunFunctionResponses: the
Existing/Dynamo stack across the reconcile passes, a non-GCP identity secret,
the Existing/Standard stack's gateway with and without a client CA, Civo's
per-pool NVLink disable, and the telemetry collector a GKE stack composes for
its TelemetryDestinations. Its expectations are literals typed here, never read
from the stacks package, so a stack-data change shows up as a test diff. Only
the vendored CRD bundles are read from their files.
COMPOSED_RESOURCE_KEYS_CASES then pins the composed-resource key set - the
identity contract; renaming a key deletes and recreates the remote resource -
for every cloud and stack. CLUSTER_NAME_CASES pins the cluster name every
exported series is stamped with.
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
    reason: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


@dataclasses.dataclass
class ComposedResourceKeysCase:
    """A test case for the composed-resource keys RunFunction renders."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: set[str]


@dataclasses.dataclass
class ClusterNameCase:
    """A test case for fn._cluster_name."""

    name: str
    reason: str
    xr: v1alpha1.ServingStack
    want: str


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
    *,
    cloud: stacks.Cloud,
    stack: stacks.Stack,
    secrets: list[v1alpha1.Secret],
    gateway: v1alpha1.Gateway,
    gpu: v1alpha1.Gpu | None,
    dra_drivers: list[str] | None,
) -> fnv1.Resource:
    """The observed ServingStack, test-backend in namespace test-ns.

    dra_drivers populates spec.draDrivers; None leaves the field absent,
    which means the same as empty: only the untagged components install.
    """
    spec = v1alpha1.Spec(cloud=cloud, stack=stack, secrets=secrets, gateway=gateway, gpu=gpu)
    if dra_drivers is not None:
        spec.draDrivers = [v1alpha1.DraDriver(d) for d in dra_drivers]
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            v1alpha1.ServingStack(
                metadata=metav1.ObjectMeta(name="test-backend", namespace="test-ns"),
                spec=spec,
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
    """The composed cert-manager Release the hand-written clouds pin, as the Existing and Civo cases compose it.

    GKE's generated half pins its own, _gke_cert_manager.
    """
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


# Civo's DCGM exporter metrics CSV, restated once: both NvLink wants carry
# it verbatim, and it is too long to repeat beside each. Still a literal
# independent of civo.py, so a drift there breaks the cases.
_DCGM_METRICS_CSV = """# Clocks,,
DCGM_FI_DEV_SM_CLOCK,     gauge, SM clock frequency (in MHz).
DCGM_FI_DEV_MEM_CLOCK, gauge, Memory clock frequency (in MHz).
# Temperature,,
DCGM_FI_DEV_MEMORY_TEMP, gauge, Memory temperature (in C).
DCGM_FI_DEV_GPU_TEMP,    gauge, GPU temperature (in C).
# Power,,
DCGM_FI_DEV_POWER_USAGE,  gauge, Power draw (in W).
DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION, counter, Total energy consumption since boot (in mJ).
# PCIe,,
DCGM_FI_PROF_PCIE_TX_BYTES,  counter, Total number of bytes transmitted through PCIe TX (in KB) via NVML.
DCGM_FI_PROF_PCIE_RX_BYTES,  counter, Total number of bytes received through PCIe RX (in KB) via NVML.
DCGM_FI_DEV_PCIE_REPLAY_COUNTER, counter, Total number of PCIe retries.
# Utilization (the sample period varies depending on the product),,
DCGM_FI_DEV_GPU_UTIL,      gauge, GPU utilization (in %).
DCGM_FI_DEV_MEM_COPY_UTIL, gauge, Memory utilization (in %).
DCGM_FI_DEV_ENC_UTIL,      gauge, Encoder utilization (in %).
DCGM_FI_DEV_DEC_UTIL,      gauge, Decoder utilization (in %).
# Errors and violations,,
DCGM_FI_DEV_XID_ERRORS,            gauge, Value of the last XID error encountered.
DCGM_FI_DEV_POWER_VIOLATION,       counter, Throttling duration due to power constraints (in us).
DCGM_FI_DEV_THERMAL_VIOLATION,     counter, Throttling duration due to thermal constraints (in us).
DCGM_FI_DEV_SYNC_BOOST_VIOLATION,  counter, Throttling duration due to sync-boost constraints (in us).
DCGM_FI_DEV_BOARD_LIMIT_VIOLATION, counter, Throttling duration due to board limit constraints (in us).
DCGM_FI_DEV_LOW_UTIL_VIOLATION,    counter, Throttling duration due to low utilization (in us).
DCGM_FI_DEV_RELIABILITY_VIOLATION, counter, Throttling duration due to reliability constraints (in us).
# Memory usage,,
DCGM_FI_DEV_FB_FREE, gauge, Framebuffer memory free (in MiB).
DCGM_FI_DEV_FB_USED, gauge, Framebuffer memory used (in MiB).
# Retired pages,,
DCGM_FI_DEV_RETIRED_SBE,     counter, Total number of retired pages due to single-bit errors.
DCGM_FI_DEV_RETIRED_DBE,     counter, Total number of retired pages due to double-bit errors.
DCGM_FI_DEV_RETIRED_PENDING, counter, Total number of pages pending retirement.
# NVLink,,
DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL, counter, Total number of NVLink bandwidth counters for all lanes
DCGM_FI_PROF_NVLINK_TX_BYTES,       counter, The rate of data transmitted over NVLink not including protocol headers in bytes per second.
DCGM_FI_PROF_NVLINK_RX_BYTES,       counter, The rate of data received over NVLink not including protocol headers in bytes per second.
# Add DCP metrics,,
DCGM_FI_PROF_GR_ENGINE_ACTIVE,   gauge, Ratio of time the graphics engine is active (in %).
DCGM_FI_PROF_SM_ACTIVE,          gauge, The ratio of cycles an SM has at least 1 warp assigned (in %).
DCGM_FI_PROF_SM_OCCUPANCY,       gauge, The ratio of number of warps resident on an SM (in %).
DCGM_FI_PROF_PIPE_TENSOR_ACTIVE, gauge, Ratio of cycles the tensor (HMMA) pipe is active (in %).
DCGM_FI_PROF_DRAM_ACTIVE,        gauge, Ratio of cycles the device memory interface is active sending or receiving data (in %).
DCGM_FI_PROF_PCIE_TX_BYTES,      counter, The number of bytes of active pcie tx data including both header and payload.
DCGM_FI_PROF_PCIE_RX_BYTES,      counter, The number of bytes of active pcie rx data including both header and payload.
# BCP Additional metrics
DCGM_FI_DEV_GPU_NVLINK_ERRORS,      gauge, Identifies a GPU NVLink error type returned by DCGM_FI_DEV_GPU_NVLINK_ERRORS.
# Added RunAI Additional metrics https://docs.run.ai/latest/developer/metrics/metrics-api/#advanced-metrics
## NVLink
DCGM_FI_DEV_NVLINK_BANDWIDTH_L0, counter, The number of bytes of active NVLink rx or tx data including both header and payload.
## VGPU License status
DCGM_FI_DEV_VGPU_LICENSE_STATUS, gauge, vGPU License status
## Remapped rows
DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS, counter, Number of remapped rows for uncorrectable errors
DCGM_FI_DEV_CORRECTABLE_REMAPPED_ROWS, counter, Number of remapped rows for correctable errors
DCGM_FI_DEV_ROW_REMAP_FAILURE, gauge, Whether remapping of rows has failed
## Static configuration information. These appear as labels on the other metrics
DCGM_FI_DRIVER_VERSION, label, Driver Version
## Profiling metrics
DCGM_FI_PROF_PIPE_FP64_ACTIVE, gauge, Ratio of cycles the fp64 pipes are active (in %).
DCGM_FI_PROF_PIPE_FP32_ACTIVE, gauge, Ratio of cycles the fp32 pipes are active (in %).
DCGM_FI_PROF_PIPE_FP16_ACTIVE, gauge, Ratio of cycles the fp16 pipes are active (in %).
"""


def _kube_prometheus_stack(*, ready: fnv1.Ready, node_exporter_tolerations: list[dict]) -> fnv1.Resource:
    """The composed kube-prometheus-stack Release.

    node_exporter_tolerations is the inference pool taints the cloud's pin
    lets the node exporter tolerate: every taint a BYO pool may carry on
    Existing, the NVIDIA and CPU taints elsewhere.
    """
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
                            "prometheus-node-exporter": {"tolerations": node_exporter_tolerations},
                            "grafana": {"enabled": False},
                            "alertmanager": {"enabled": False},
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _node_feature_discovery(*, ready: fnv1.Ready, wait: bool, worker_tolerations: list[dict]) -> fnv1.Resource:
    """The composed node-feature-discovery Release the hand-written clouds pin, waiting for health if wait is set.

    worker_tolerations is the GPU pool taints the cloud's pin lets the
    worker tolerate: NVIDIA's and AMD's on Existing, NVIDIA's elsewhere.
    GKE's generated half pins its own, _gke_node_feature_discovery.
    """
    for_provider: dict = {
        "chart": {
            "name": "node-feature-discovery",
            "repository": "https://kubernetes-sigs.github.io/node-feature-discovery/charts",
            "version": "0.19.0",
        },
        "namespace": "node-feature-discovery",
        "values": {"worker": {"tolerations": worker_tolerations}},
    }
    if wait:
        for_provider |= {"wait": True, "waitTimeout": "10m"}
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
                    "forProvider": for_provider,
                },
            }
        ),
        ready=ready,
    )


def _nvidia_dra_driver_gpu(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed NVIDIA GPU DRA driver Release as Existing pins it, at the chart's default driver root.

    Civo's points at the gpu-operator's driver root, and is written inline in
    NvLinkEnabled.
    """
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
                            "version": "0.5.0",
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


def _gaie_crd(*, name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed Object holding the Gateway API Inference Extension CRD called name, from the vendored bundle."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"labels": {"modelplane.ai/resource": f"gaie-crds-{name}"}},
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {"manifest": _crd(filename="gaie.yaml", name=name)},
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


def _cluster_agent(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed cluster agent Release."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "Release",
                "metadata": {
                    "annotations": {"crossplane.io/external-name": "mp-cluster-agent"},
                    "labels": {"modelplane.ai/resource": "cluster-agent"},
                },
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "chart": {
                            "name": "cluster-agent",
                            "repository": "oci://xpkg.upbound.io/modelplane",
                            "version": "0.1.0",
                        },
                        "namespace": "modelplane-system",
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


def _usage(
    *, of_api_version: str, of_kind: str, of_key: str, by_api_version: str, by_kind: str, by_key: str
) -> fnv1.Resource:
    """The composed Usage holding the resource labelled of_key until the one labelled by_key is gone, marked ready on arrival."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "spec": {
                    "of": {
                        "apiVersion": of_api_version,
                        "kind": of_kind,
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": of_key},
                        },
                    },
                    "by": {
                        "apiVersion": by_api_version,
                        "kind": by_kind,
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/resource": by_key},
                        },
                    },
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _gke_cert_manager() -> fnv1.Resource:
    """The composed cert-manager Release GKE's generated half pins, not yet marked ready."""
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
                        "values": {
                            "cainjector": {
                                "resources": {
                                    "limits": {"cpu": "50m", "memory": "320Mi"},
                                    "requests": {"cpu": "50m", "memory": "320Mi"},
                                },
                                "tolerations": [],
                            },
                            "crds": {"enabled": True},
                            "fullnameOverride": "cert-manager",
                            "prometheus": {"servicemonitor": {"enabled": False}},
                            "resources": {
                                "limits": {"cpu": "50m", "memory": "90Mi"},
                                "requests": {"cpu": "50m", "memory": "90Mi"},
                            },
                            "startupapicheck": {"enabled": True, "tolerations": []},
                            "tolerations": [],
                            "webhook": {
                                "resources": {
                                    "limits": {"cpu": "50m", "memory": "40Mi"},
                                    "requests": {"cpu": "50m", "memory": "40Mi"},
                                },
                                "tolerations": [],
                            },
                            # Forced by fn._helm_release, as on every cloud.
                            "clusterResourceNamespace": "modelplane-system",
                            "enableCertificateOwnerRef": True,
                        },
                    },
                },
            }
        ),
        ready=fnv1.READY_UNSPECIFIED,
    )


def _gke_node_feature_discovery() -> fnv1.Resource:
    """The composed node-feature-discovery Release GKE's generated half pins, not yet marked ready."""
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
                        "wait": True,
                        "waitTimeout": "10m",
                        "values": {
                            "gc": {"enable": True, "tolerations": []},
                            "master": {"enable": True, "tolerations": []},
                            "topologyUpdater": {
                                "createCRDs": True,
                                "enable": False,
                                "kubeletStateDir": "",
                                "resources": {
                                    "limits": {"memory": "256Mi"},
                                    "requests": {"cpu": "50m", "memory": "128Mi"},
                                },
                                "tolerations": [
                                    {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
                                ],
                            },
                            "worker": {
                                "enable": True,
                                "tolerations": [
                                    {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
                                ],
                            },
                        },
                    },
                },
            }
        ),
        ready=fnv1.READY_UNSPECIFIED,
    )


def _nodewright_operator() -> fnv1.Resource:
    """The composed nodewright operator Release, not yet marked ready."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "Release",
                "metadata": {
                    "annotations": {"crossplane.io/external-name": "mp-nodewright"},
                    "labels": {"modelplane.ai/resource": "nodewright-operator"},
                },
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "chart": {
                            "name": "nodewright",
                            "repository": "oci://ghcr.io/nvidia/nodewright/charts",
                            "version": "v0.19.0",
                        },
                        "namespace": "skyhook",
                        "values": {
                            "controllerManager": {
                                "manager": {
                                    "env": {"copyDirRoot": "/etc/nodewright", "reapplyOnReboot": "true"},
                                    "resources": {
                                        "limits": {"cpu": "1000m", "memory": "4000Mi"},
                                        "requests": {"cpu": "1000m", "memory": "2000Mi"},
                                    },
                                },
                                "tolerations": [],
                            },
                            "fullnameOverride": "skyhook-operator",
                            "limitRange": {
                                "default": {"cpu": "1", "memory": "1Gi"},
                                "defaultRequest": {"cpu": "500m", "memory": "512Mi"},
                            },
                        },
                    },
                },
            }
        ),
        ready=fnv1.READY_UNSPECIFIED,
    )


def _prometheus_operator_crds() -> fnv1.Resource:
    """The composed Prometheus operator CRDs Release, not yet marked ready."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "Release",
                "metadata": {
                    "annotations": {"crossplane.io/external-name": "mp-prometheus-operator-crds"},
                    "labels": {"modelplane.ai/resource": "prometheus-operator-crds"},
                },
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "chart": {
                            "name": "prometheus-operator-crds",
                            "repository": "https://prometheus-community.github.io/helm-charts",
                            "version": "28.0.1",
                        },
                        "namespace": "monitoring",
                        "wait": True,
                        "waitTimeout": "10m",
                        "values": {"enabled": True},
                    },
                },
            }
        ),
        ready=fnv1.READY_UNSPECIFIED,
    )


def _gpu_operator_pre_manifests_gpu_operator() -> fnv1.Resource:
    """The composed gpu-operator Namespace the GPU operator's pre-manifests create, not yet marked ready."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"labels": {"modelplane.ai/resource": "gpu-operator-pre-manifests-gpu-operator"}},
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "gpu-operator"}}
                    },
                },
            }
        ),
        ready=fnv1.READY_UNSPECIFIED,
    )


def _gpu_operator_pre_manifests_aicr_gke_critical_pods() -> fnv1.Resource:
    """The composed ResourceQuota admitting the GPU operator's critical pods on GKE, not yet marked ready."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"labels": {"modelplane.ai/resource": "gpu-operator-pre-manifests-aicr-gke-critical-pods"}},
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "v1",
                            "kind": "ResourceQuota",
                            "metadata": {"name": "aicr-gke-critical-pods", "namespace": "gpu-operator"},
                            "spec": {
                                "hard": {"pods": "32"},
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
        ready=fnv1.READY_UNSPECIFIED,
    )


def _collector_service_account() -> fnv1.Resource:
    """The composed collector ServiceAccount, marked ready on arrival."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"labels": {"modelplane.ai/resource": "collector-serviceaccount"}},
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "v1",
                            "kind": "ServiceAccount",
                            "metadata": {
                                "name": "modelplane-collector",
                                "namespace": "modelplane-system",
                                "labels": {
                                    "app.kubernetes.io/name": "modelplane-collector",
                                    "app.kubernetes.io/managed-by": "modelplane",
                                },
                            },
                        }
                    },
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _collector_cluster_role() -> fnv1.Resource:
    """The composed collector ClusterRole, marked ready on arrival."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"labels": {"modelplane.ai/resource": "collector-clusterrole"}},
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "rbac.authorization.k8s.io/v1",
                            "kind": "ClusterRole",
                            "metadata": {
                                "name": "modelplane-collector",
                                "labels": {
                                    "app.kubernetes.io/name": "modelplane-collector",
                                    "app.kubernetes.io/managed-by": "modelplane",
                                },
                            },
                            "rules": [
                                {
                                    "apiGroups": [""],
                                    "resources": ["pods", "services", "endpoints", "nodes", "nodes/metrics"],
                                    "verbs": ["get", "list", "watch"],
                                },
                                {"nonResourceURLs": ["/metrics"], "verbs": ["get"]},
                            ],
                        }
                    },
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _collector_cluster_role_binding() -> fnv1.Resource:
    """The composed collector ClusterRoleBinding, marked ready on arrival."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"labels": {"modelplane.ai/resource": "collector-clusterrolebinding"}},
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "rbac.authorization.k8s.io/v1",
                            "kind": "ClusterRoleBinding",
                            "metadata": {
                                "name": "modelplane-collector",
                                "labels": {
                                    "app.kubernetes.io/name": "modelplane-collector",
                                    "app.kubernetes.io/managed-by": "modelplane",
                                },
                            },
                            "roleRef": {
                                "apiGroup": "rbac.authorization.k8s.io",
                                "kind": "ClusterRole",
                                "name": "modelplane-collector",
                            },
                            "subjects": [
                                {
                                    "kind": "ServiceAccount",
                                    "name": "modelplane-collector",
                                    "namespace": "modelplane-system",
                                }
                            ],
                        }
                    },
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _collector_config(
    *, exporters: dict, pipeline_exporters: list[str], extensions: dict | None, service_extensions: list[str] | None
) -> fnv1.Resource:
    """The composed ConfigMap holding the collector's config, marked ready on arrival.

    The ConfigMap holds the config as YAML text. This writes it as the dict the
    function dumps, in the order the function builds it, so it reads as config
    rather than as wrapped YAML, and PyYAML dumps it on both sides.
    test_collector.py says what each part of it is for.
    """
    service: dict = {
        "pipelines": {
            "metrics": {
                "receivers": ["prometheus"],
                "processors": [
                    "memory_limiter",
                    "resource/cluster",
                    "transform/identity",
                    "transform/modelplane",
                    "groupbyattrs/identity",
                    "filter/modelplane",
                    "batch",
                ],
                "exporters": pipeline_exporters,
            }
        },
    }
    if service_extensions is not None:
        service["extensions"] = service_extensions
    config: dict = {
        "receivers": {
            "prometheus": {
                "config": {
                    "scrape_configs": [
                        {
                            "job_name": "modelplane-engines",
                            "scrape_interval": "15s",
                            "kubernetes_sd_configs": [{"role": "pod"}],
                            "relabel_configs": [
                                {
                                    "source_labels": ["__meta_kubernetes_pod_label_modelplane_ai_deployment"],
                                    "action": "keep",
                                    "regex": ".+",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_container_port_name"],
                                    "action": "keep",
                                    "regex": "http",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_container_name"],
                                    "action": "keep",
                                    "regex": "engine|epp",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_label_modelplane_ai_deployment"],
                                    "target_label": "deployment",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_label_modelplane_ai_replica"],
                                    "target_label": "replica",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_label_modelplane_ai_engine"],
                                    "target_label": "engine",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_label_modelplane_ai_role"],
                                    "target_label": "role",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_label_modelplane_ai_phase"],
                                    "target_label": "phase",
                                },
                                {"source_labels": ["__meta_kubernetes_namespace"], "target_label": "namespace"},
                            ],
                        },
                        {
                            "job_name": "modelplane-gateway",
                            "scrape_interval": "15s",
                            "kubernetes_sd_configs": [{"role": "pod"}],
                            "relabel_configs": [
                                {
                                    "source_labels": [
                                        "__meta_kubernetes_pod_label_gateway_envoyproxy_io_owning_gateway_name"
                                    ],
                                    "action": "keep",
                                    "regex": ".+",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_container_port_name"],
                                    "action": "keep",
                                    "regex": "metrics",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_annotation_prometheus_io_path"],
                                    "action": "replace",
                                    "target_label": "__metrics_path__",
                                    "regex": "(.+)",
                                },
                                {
                                    "source_labels": [
                                        "__meta_kubernetes_pod_label_gateway_envoyproxy_io_owning_gateway_name"
                                    ],
                                    "target_label": "gateway",
                                },
                            ],
                            "metric_relabel_configs": [
                                {
                                    "source_labels": ["__name__", "envoy_cluster_name", "gateway"],
                                    "regex": "envoy_cluster_upstream_rq_active;httproute/(mp-[^/]+)/([^/]+)/rule/0;inference-gateway",
                                    "target_label": "__tmp_model",
                                    "replacement": "true",
                                },
                                {
                                    "source_labels": ["__name__", "__tmp_model"],
                                    "regex": "envoy_cluster_upstream_rq_active;",
                                    "action": "drop",
                                },
                                {
                                    "source_labels": ["__name__", "envoy_cluster_name"],
                                    "regex": "envoy_cluster_upstream_rq_active;httproute/(mp-[^/]+)/([^/]+)/rule/0",
                                    "target_label": "namespace",
                                    "replacement": "$1",
                                },
                                {
                                    "source_labels": ["__name__", "envoy_cluster_name"],
                                    "regex": "envoy_cluster_upstream_rq_active;httproute/(mp-[^/]+)/([^/]+)/rule/0",
                                    "target_label": "service",
                                    "replacement": "$2",
                                },
                                {
                                    "source_labels": ["__name__"],
                                    "regex": "envoy_cluster_upstream_rq_active",
                                    "target_label": "envoy_cluster_name",
                                    "replacement": "",
                                },
                                {
                                    "source_labels": ["__name__"],
                                    "regex": "envoy_cluster_upstream_rq_active",
                                    "target_label": "__tmp_model",
                                    "replacement": "",
                                },
                                {"target_label": "gateway", "replacement": ""},
                            ],
                        },
                        {
                            "job_name": "modelplane-gateway-genai",
                            "scrape_interval": "15s",
                            "kubernetes_sd_configs": [{"role": "pod"}],
                            "relabel_configs": [
                                {
                                    "source_labels": [
                                        "__meta_kubernetes_pod_label_gateway_envoyproxy_io_owning_gateway_name"
                                    ],
                                    "action": "keep",
                                    "regex": ".+",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_container_port_name"],
                                    "action": "keep",
                                    "regex": "aigw-admin",
                                },
                            ],
                        },
                        {
                            "job_name": "modelplane-gpu",
                            "scrape_interval": "15s",
                            "kubernetes_sd_configs": [{"role": "pod"}],
                            "relabel_configs": [
                                {
                                    "source_labels": [
                                        "__meta_kubernetes_pod_label_app_kubernetes_io_name",
                                        "__meta_kubernetes_pod_label_app",
                                    ],
                                    "action": "keep",
                                    "regex": ".*dcgm.*|.*metrics-exporter.*",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_container_port_name"],
                                    "action": "keep",
                                    "regex": "metrics|exporter-port",
                                },
                                {"source_labels": ["__meta_kubernetes_pod_node_name"], "target_label": "node"},
                            ],
                        },
                        {
                            "job_name": "modelplane-substrate",
                            "scrape_interval": "30s",
                            "kubernetes_sd_configs": [{"role": "pod"}],
                            "relabel_configs": [
                                {
                                    "source_labels": ["__meta_kubernetes_pod_annotation_prometheus_io_scrape"],
                                    "action": "keep",
                                    "regex": "true",
                                },
                                {
                                    "source_labels": [
                                        "__meta_kubernetes_pod_label_gateway_envoyproxy_io_owning_gateway_name"
                                    ],
                                    "action": "drop",
                                    "regex": ".+",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_label_modelplane_ai_deployment"],
                                    "action": "drop",
                                    "regex": ".+",
                                },
                                {
                                    "source_labels": [
                                        "__meta_kubernetes_pod_label_app_kubernetes_io_name",
                                        "__meta_kubernetes_pod_label_app",
                                    ],
                                    "action": "drop",
                                    "regex": ".*dcgm.*|.*metrics-exporter.*",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_annotation_prometheus_io_path"],
                                    "action": "replace",
                                    "target_label": "__metrics_path__",
                                    "regex": "(.+)",
                                },
                                {
                                    "source_labels": [
                                        "__address__",
                                        "__meta_kubernetes_pod_annotation_prometheus_io_port",
                                    ],
                                    "action": "replace",
                                    "target_label": "__address__",
                                    "regex": r"(\[.+\]|[^:]+)(?::\d+)?;(\d+)",
                                    "replacement": "$1:$2",
                                },
                                {"source_labels": ["__meta_kubernetes_namespace"], "target_label": "namespace"},
                            ],
                            "metric_relabel_configs": [
                                {
                                    "source_labels": ["__name__"],
                                    "regex": "envoy_cluster_upstream_rq_active",
                                    "action": "drop",
                                },
                            ],
                        },
                    ]
                }
            }
        },
        "processors": {
            "memory_limiter": {"check_interval": "1s", "limit_percentage": 80, "spike_limit_percentage": 25},
            "resource/cluster": {"attributes": [{"key": "cluster", "value": "test-backend", "action": "upsert"}]},
            "transform/identity": {
                "metric_statements": [
                    {
                        "context": "resource",
                        "statements": [
                            'keep_keys(resource.attributes, ["cluster", "deployment", "engine", "namespace", "node", "phase", "replica", "role", "service.instance.id", "service.name"])'
                        ],
                    }
                ]
            },
            "transform/modelplane": {
                "metric_statements": [
                    # The engines' token histograms as counters, extracted ahead of the
                    # statements that select on the names extraction gives them.
                    {
                        "context": "metric",
                        "statements": [
                            'extract_sum_metric(true) where metric.name == "vllm:request_prompt_tokens"',
                            'extract_sum_metric(true) where metric.name == "vllm:request_generation_tokens"',
                            'extract_count_metric(true) where metric.name == "vllm:request_prompt_tokens"',
                            'extract_sum_metric(true) where metric.name == "sglang:prompt_tokens_histogram"',
                            'extract_sum_metric(true) where metric.name == "sglang:generation_tokens_histogram"',
                            'extract_count_metric(true) where metric.name == "sglang:prompt_tokens_histogram"',
                        ],
                    },
                    {
                        "context": "metric",
                        "error_mode": "ignore",
                        "statements": [
                            'scale_metric(1048576.0) where metric.name == "DCGM_FI_DEV_FB_USED"',
                            'scale_metric(1048576.0) where metric.name == "DCGM_FI_DEV_FB_FREE"',
                            'scale_metric(0.001) where metric.name == "DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION"',
                            'scale_metric(1048576.0) where metric.name == "gpu_used_vram"',
                            'scale_metric(1048576.0) where metric.name == "gpu_free_vram"',
                            'scale_metric(0.01) where metric.name == "gpu_gfx_activity"',
                            'scale_metric(0.01) where metric.name == "gpu_umc_activity"',
                            'scale_metric(0.01) where metric.name == "gpu_prof_tensor_active_percent"',
                            'scale_metric(0.000001) where metric.name == "gpu_energy_consumed"',
                            'convert_gauge_to_sum("cumulative", true) where metric.name == "gpu_energy_consumed"',
                        ],
                    },
                    {
                        "context": "datapoint",
                        "statements": [
                            'set(datapoint.attributes["direction"], "input") where metric.name == "vllm:request_prompt_tokens_sum"',
                            'set(datapoint.attributes["direction"], "output") where metric.name == "vllm:request_generation_tokens_sum"',
                            'set(datapoint.attributes["reason"], datapoint.attributes["finished_reason"]) where metric.name == "vllm:request_success_total"',
                            'delete_key(datapoint.attributes, "finished_reason") where metric.name == "vllm:request_success_total"',
                            'set(datapoint.attributes["direction"], "input") where metric.name == "sglang:prompt_tokens_histogram_sum"',
                            'set(datapoint.attributes["direction"], "output") where metric.name == "sglang:generation_tokens_histogram_sum"',
                            'set(datapoint.attributes["status"], datapoint.attributes["status_code"]) where metric.name == "sglang:http_responses_total"',
                            'set(datapoint.attributes["status"], "ok") where metric.name == "sglang:http_responses_total" and datapoint.attributes["status_code"] == "200"',
                            'set(datapoint.attributes["status"], "client_error") where metric.name == "sglang:http_responses_total" and datapoint.attributes["status_code"] == "400"',
                            'set(datapoint.attributes["status"], "client_error") where metric.name == "sglang:http_responses_total" and datapoint.attributes["status_code"] == "404"',
                            'set(datapoint.attributes["status"], "client_error") where metric.name == "sglang:http_responses_total" and datapoint.attributes["status_code"] == "422"',
                            'set(datapoint.attributes["status"], "client_error") where metric.name == "sglang:http_responses_total" and datapoint.attributes["status_code"] == "429"',
                            'set(datapoint.attributes["status"], "server_error") where metric.name == "sglang:http_responses_total" and datapoint.attributes["status_code"] == "500"',
                            'set(datapoint.attributes["status"], "server_error") where metric.name == "sglang:http_responses_total" and datapoint.attributes["status_code"] == "503"',
                            'delete_key(datapoint.attributes, "status_code") where metric.name == "sglang:http_responses_total"',
                        ],
                    },
                    {
                        "context": "metric",
                        "statements": [
                            'set(metric.name, "modelplane_frontend_request_duration_seconds") where metric.name == "gen_ai_server_request_duration_seconds"',
                            'set(metric.name, "modelplane_frontend_ttft_seconds") where metric.name == "gen_ai_server_time_to_first_token_seconds"',
                            'set(metric.name, "modelplane_frontend_tpot_seconds") where metric.name == "gen_ai_server_time_per_output_token_seconds"',
                            'set(metric.name, "modelplane_frontend_requests_active") where metric.name == "envoy_cluster_upstream_rq_active"',
                            'set(metric.name, "modelplane_request_ttft_seconds") where metric.name == "vllm:time_to_first_token_seconds"',
                            'set(metric.name, "modelplane_request_duration_seconds") where metric.name == "vllm:e2e_request_latency_seconds"',
                            'set(metric.name, "modelplane_request_queue_seconds") where metric.name == "vllm:request_queue_time_seconds"',
                            'set(metric.name, "modelplane_request_prefill_seconds") where metric.name == "vllm:request_prefill_time_seconds"',
                            'set(metric.name, "modelplane_request_decode_seconds") where metric.name == "vllm:request_decode_time_seconds"',
                            'set(metric.name, "modelplane_requests_running") where metric.name == "vllm:num_requests_running"',
                            'set(metric.name, "modelplane_requests_waiting") where metric.name == "vllm:num_requests_waiting"',
                            'set(metric.name, "modelplane_kv_cache_utilization_ratio") where metric.name == "vllm:kv_cache_usage_perc"',
                            'set(metric.name, "modelplane_requests_preempted_total") where metric.name == "vllm:num_preemptions_total"',
                            'set(metric.name, "modelplane_prefix_cache_hits_total") where metric.name == "vllm:prefix_cache_hits_total"',
                            'set(metric.name, "modelplane_prefix_cache_lookups_total") where metric.name == "vllm:prefix_cache_queries_total"',
                            'set(metric.name, "modelplane_engine_tokens_total") where metric.name == "vllm:request_prompt_tokens_sum"',
                            'set(metric.name, "modelplane_engine_tokens_total") where metric.name == "vllm:request_generation_tokens_sum"',
                            'set(metric.name, "modelplane_engine_requests_total") where metric.name == "vllm:request_prompt_tokens_count"',
                            'set(metric.name, "modelplane_responses_total") where metric.name == "vllm:request_success_total"',
                            'set(metric.name, "modelplane_requests_running") where metric.name == "sglang:num_running_reqs"',
                            'set(metric.name, "modelplane_requests_waiting") where metric.name == "sglang:num_queue_reqs"',
                            'set(metric.name, "modelplane_kv_cache_utilization_ratio") where metric.name == "sglang:token_usage"',
                            'set(metric.name, "modelplane_requests_preempted_total") where metric.name == "sglang:num_retracted_requests_total"',
                            'set(metric.name, "modelplane_prefix_cache_hits_total") where metric.name == "sglang:cached_tokens_total"',
                            'set(metric.name, "modelplane_prefix_cache_lookups_total") where metric.name == "sglang:prompt_tokens_total"',
                            'set(metric.name, "modelplane_sglang_ttft_seconds") where metric.name == "sglang:time_to_first_token_seconds"',
                            'set(metric.name, "modelplane_engine_requests_active") where metric.name == "sglang:http_requests_active"',
                            'set(metric.name, "modelplane_engine_tokens_total") where metric.name == "sglang:prompt_tokens_histogram_sum"',
                            'set(metric.name, "modelplane_engine_tokens_total") where metric.name == "sglang:generation_tokens_histogram_sum"',
                            'set(metric.name, "modelplane_engine_requests_total") where metric.name == "sglang:prompt_tokens_histogram_count"',
                            'set(metric.name, "modelplane_engine_responses_total") where metric.name == "sglang:http_responses_total"',
                            'set(metric.name, "modelplane_route_decision_seconds") where metric.name == "llm_d_epp_scheduler_e2e_duration_seconds"',
                            'set(metric.name, "modelplane_gpu_memory_used_bytes") where metric.name == "DCGM_FI_DEV_FB_USED"',
                            'set(metric.name, "modelplane_gpu_memory_free_bytes") where metric.name == "DCGM_FI_DEV_FB_FREE"',
                            'set(metric.name, "modelplane_gpu_compute_active_ratio") where metric.name == "DCGM_FI_PROF_GR_ENGINE_ACTIVE"',
                            'set(metric.name, "modelplane_gpu_tensor_active_ratio") where metric.name == "DCGM_FI_PROF_PIPE_TENSOR_ACTIVE"',
                            'set(metric.name, "modelplane_gpu_memory_bandwidth_ratio") where metric.name == "DCGM_FI_PROF_DRAM_ACTIVE"',
                            'set(metric.name, "modelplane_gpu_temperature_celsius") where metric.name == "DCGM_FI_DEV_GPU_TEMP"',
                            'set(metric.name, "modelplane_gpu_power_watts") where metric.name == "DCGM_FI_DEV_POWER_USAGE"',
                            'set(metric.name, "modelplane_energy_joules_total") where metric.name == "DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION"',
                            'set(metric.name, "modelplane_gpu_memory_used_bytes") where metric.name == "gpu_used_vram"',
                            'set(metric.name, "modelplane_gpu_memory_free_bytes") where metric.name == "gpu_free_vram"',
                            'set(metric.name, "modelplane_gpu_compute_active_ratio") where metric.name == "gpu_gfx_activity"',
                            'set(metric.name, "modelplane_gpu_memory_bandwidth_ratio") where metric.name == "gpu_umc_activity"',
                            'set(metric.name, "modelplane_gpu_tensor_active_ratio") where metric.name == "gpu_prof_tensor_active_percent"',
                            'set(metric.name, "modelplane_gpu_temperature_celsius") where metric.name == "gpu_junction_temperature"',
                            'set(metric.name, "modelplane_gpu_power_watts") where metric.name == "gpu_power_usage"',
                            'set(metric.name, "modelplane_energy_joules_total") where metric.name == "gpu_energy_consumed"',
                        ],
                    },
                ]
            },
            "groupbyattrs/identity": {
                "keys": [
                    "cluster",
                    "namespace",
                    "deployment",
                    "replica",
                    "engine",
                    "role",
                    "phase",
                    "node",
                    "service.name",
                    "service.instance.id",
                ]
            },
            "filter/modelplane": {"metrics": {"metric": ['not IsMatch(name, "^modelplane_.*")']}},
            "batch": {"timeout": "10s"},
        },
        "exporters": exporters,
        "service": service,
    }
    if extensions is not None:
        config["extensions"] = extensions
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"labels": {"modelplane.ai/resource": "collector-config"}},
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "v1",
                            "kind": "ConfigMap",
                            "metadata": {
                                "name": "modelplane-collector",
                                "namespace": "modelplane-system",
                                "labels": {
                                    "app.kubernetes.io/name": "modelplane-collector",
                                    "app.kubernetes.io/managed-by": "modelplane",
                                },
                            },
                            "data": {"collector.yaml": yaml.safe_dump(config, sort_keys=False)},
                        }
                    },
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _collector(
    *, config_hash: str, volumes: list[dict], volume_mounts: list[dict], env_from: list[dict] | None
) -> fnv1.Resource:
    """The composed collector Deployment, restarted by its config's hash and marked ready on arrival."""
    container: dict = {
        "name": "collector",
        "image": "otel/opentelemetry-collector-contrib:0.161.0",
        "args": ["--config=/conf/collector.yaml"],
        "volumeMounts": volume_mounts,
        "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "512Mi"}},
    }
    if env_from is not None:
        container["envFrom"] = env_from
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"labels": {"modelplane.ai/resource": "collector"}},
                "spec": {
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-backend-cluster-63fde"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "apps/v1",
                            "kind": "Deployment",
                            "metadata": {
                                "name": "modelplane-collector",
                                "namespace": "modelplane-system",
                                "labels": {
                                    "app.kubernetes.io/name": "modelplane-collector",
                                    "app.kubernetes.io/managed-by": "modelplane",
                                },
                            },
                            "spec": {
                                "replicas": 1,
                                "selector": {"matchLabels": {"app.kubernetes.io/name": "modelplane-collector"}},
                                "template": {
                                    "metadata": {
                                        "labels": {"app.kubernetes.io/name": "modelplane-collector"},
                                        "annotations": {"modelplane.ai/config-hash": config_hash},
                                    },
                                    "spec": {
                                        "serviceAccountName": "modelplane-collector",
                                        "containers": [container],
                                        "volumes": volumes,
                                    },
                                },
                            },
                        }
                    },
                    "readiness": {"policy": "DeriveFromCelQuery", "celQuery": "object.status.readyReplicas > 0"},
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _telemetry_destination(*, name: str, sinks: list[dict]) -> fnv1.Resource:
    """A required TelemetryDestination exporting to sinks."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "TelemetryDestination",
                "metadata": {"name": name},
                "spec": {"sinks": sinks},
            }
        )
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
        name="NothingObserved",
        reason="Before its ProviderConfigs are observed, a stack composes only them, unready, and its Usages.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Existing",
                    stack="Dynamo",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
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
                    "usage-cert-manager-by-envoy-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="envoy-gateway",
                    ),
                    "usage-ai-gateway-crds-by-ai-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="ai-gateway-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="ai-gateway",
                    ),
                    "usage-gateway-namespace-by-gateway-proxy": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-proxy",
                    ),
                    "usage-gateway-namespace-by-cluster-agent": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="cluster-agent",
                    ),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-selfsigned-issuer",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="trust-manager",
                    ),
                    "usage-kai-scheduler-by-kai-queue-root": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="kai-scheduler",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="kai-queue-root",
                    ),
                    "usage-kai-scheduler-by-kai-queue": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="kai-scheduler",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="kai-queue",
                    ),
                    "usage-modelexpress-crds-modelmetadatas.modelexpress.nvidia.com-by-modelexpress-server": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="modelexpress-crds-modelmetadatas.modelexpress.nvidia.com",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="modelexpress-server",
                    ),
                    "usage-modelexpress-crds-modelcacheentries.modelexpress.nvidia.com-by-modelexpress-server": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="modelexpress-crds-modelcacheentries.modelexpress.nvidia.com",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="modelexpress-server",
                    ),
                    "usage-gateway-class-by-gateway": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-class",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway",
                    ),
                    "usage-envoy-gateway-by-gateway-class": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="envoy-gateway",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-class",
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "destinations": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="TelemetryDestination"
                    ),
                    "mappings": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="MetricMapping"),
                }
            ),
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
        name="ProviderConfigsObserved",
        reason="Once its ProviderConfigs are observed, a stack renders the components with no dependencies.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Existing",
                    stack="Dynamo",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
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
                    "kube-prometheus-stack": _kube_prometheus_stack(
                        ready=fnv1.READY_UNSPECIFIED,
                        node_exporter_tolerations=[
                            {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                            {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                            {"key": "modelplane.ai/cpu", "operator": "Exists", "effect": "NoSchedule"},
                        ],
                    ),
                    "node-feature-discovery": _node_feature_discovery(
                        ready=fnv1.READY_UNSPECIFIED,
                        wait=True,
                        worker_tolerations=[
                            {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                            {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                        ],
                    ),
                    "nvidia-dra-driver-gpu": _nvidia_dra_driver_gpu(ready=fnv1.READY_UNSPECIFIED),
                    "ai-gateway-crds": _ai_gateway_crds(ready=fnv1.READY_UNSPECIFIED),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferenceobjectives.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
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
                    "usage-cert-manager-by-envoy-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="envoy-gateway",
                    ),
                    "usage-ai-gateway-crds-by-ai-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="ai-gateway-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="ai-gateway",
                    ),
                    "usage-gateway-namespace-by-gateway-proxy": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-proxy",
                    ),
                    "usage-gateway-namespace-by-cluster-agent": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="cluster-agent",
                    ),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-selfsigned-issuer",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="trust-manager",
                    ),
                    "usage-kai-scheduler-by-kai-queue-root": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="kai-scheduler",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="kai-queue-root",
                    ),
                    "usage-kai-scheduler-by-kai-queue": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="kai-scheduler",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="kai-queue",
                    ),
                    "usage-modelexpress-crds-modelmetadatas.modelexpress.nvidia.com-by-modelexpress-server": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="modelexpress-crds-modelmetadatas.modelexpress.nvidia.com",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="modelexpress-server",
                    ),
                    "usage-modelexpress-crds-modelcacheentries.modelexpress.nvidia.com-by-modelexpress-server": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="modelexpress-crds-modelcacheentries.modelexpress.nvidia.com",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="modelexpress-server",
                    ),
                    "usage-gateway-class-by-gateway": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-class",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway",
                    ),
                    "usage-envoy-gateway-by-gateway-class": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="envoy-gateway",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-class",
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "destinations": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="TelemetryDestination"
                    ),
                    "mappings": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="MetricMapping"),
                }
            ),
        ),
    ),
    # The ProviderConfigs are ready because they're observed, and the Usages on
    # arrival.
    ComposeCase(
        name="AllReady",
        reason="With every rendered resource observed Ready and the gateway's address assigned, the whole stack renders Ready and the address lands in the XR's status.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Existing",
                    stack="Dynamo",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
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
                    "cluster-agent": _observed_ready(),
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
                    "kube-prometheus-stack": _kube_prometheus_stack(
                        ready=fnv1.READY_TRUE,
                        node_exporter_tolerations=[
                            {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                            {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                            {"key": "modelplane.ai/cpu", "operator": "Exists", "effect": "NoSchedule"},
                        ],
                    ),
                    "node-feature-discovery": _node_feature_discovery(
                        ready=fnv1.READY_TRUE,
                        wait=True,
                        worker_tolerations=[
                            {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                            {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                        ],
                    ),
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
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferenceobjectives.inference.networking.x-k8s.io", ready=fnv1.READY_TRUE
                    ),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.k8s.io", ready=fnv1.READY_TRUE
                    ),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.x-k8s.io", ready=fnv1.READY_TRUE
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
                    "cluster-agent": _cluster_agent(ready=fnv1.READY_TRUE),
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
                    "usage-cert-manager-by-envoy-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="envoy-gateway",
                    ),
                    "usage-ai-gateway-crds-by-ai-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="ai-gateway-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="ai-gateway",
                    ),
                    "usage-gateway-namespace-by-gateway-proxy": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-proxy",
                    ),
                    "usage-gateway-namespace-by-cluster-agent": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="cluster-agent",
                    ),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-selfsigned-issuer",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="trust-manager",
                    ),
                    "usage-kai-scheduler-by-kai-queue-root": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="kai-scheduler",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="kai-queue-root",
                    ),
                    "usage-kai-scheduler-by-kai-queue": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="kai-scheduler",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="kai-queue",
                    ),
                    "usage-modelexpress-crds-modelmetadatas.modelexpress.nvidia.com-by-modelexpress-server": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="modelexpress-crds-modelmetadatas.modelexpress.nvidia.com",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="modelexpress-server",
                    ),
                    "usage-modelexpress-crds-modelcacheentries.modelexpress.nvidia.com-by-modelexpress-server": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="modelexpress-crds-modelcacheentries.modelexpress.nvidia.com",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="modelexpress-server",
                    ),
                    "usage-gateway-class-by-gateway": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-class",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway",
                    ),
                    "usage-envoy-gateway-by-gateway-class": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="envoy-gateway",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-class",
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "destinations": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="TelemetryDestination"
                    ),
                    "mappings": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="MetricMapping"),
                }
            ),
        ),
    ),
    # The type isn't forced to GoogleApplicationCredentials, and the secret's own
    # namespace wins over the XR's.
    ComposeCase(
        name="NebiusIdentity",
        reason="A Nebius identity secret's type and namespace reach both ProviderConfigs as written.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Nebius",
                    stack="Standard",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
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
                    "usage-cert-manager-by-envoy-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="envoy-gateway",
                    ),
                    "usage-ai-gateway-crds-by-ai-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="ai-gateway-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="ai-gateway",
                    ),
                    "usage-gateway-namespace-by-gateway-proxy": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-proxy",
                    ),
                    "usage-gateway-namespace-by-cluster-agent": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="cluster-agent",
                    ),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-selfsigned-issuer",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="trust-manager",
                    ),
                    "usage-envoy-gateway-by-gateway-class": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="envoy-gateway",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-class",
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message="Gateway test-backend.gateways.example.com not served: no InferenceGateway has published a client CA for this cluster to trust, and serving without one would accept unauthenticated callers",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "destinations": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="TelemetryDestination"
                    ),
                    "mappings": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="MetricMapping"),
                }
            ),
        ),
    ),
    # The ProviderConfigs are observed, the self-signed Issuer trust-manager
    # depends on is Ready, and the CA ConfigMap trust-manager syncs carries the
    # certificate back for status.
    ComposeCase(
        name="ClientCAs",
        reason="A cluster with InferenceGateway CAs to trust composes its own PKI, serves mTLS with their bundle, and publishes its own CA.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Existing",
                    stack="Standard",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
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
                    "kube-prometheus-stack": _kube_prometheus_stack(
                        ready=fnv1.READY_UNSPECIFIED,
                        node_exporter_tolerations=[
                            {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                            {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                            {"key": "modelplane.ai/cpu", "operator": "Exists", "effect": "NoSchedule"},
                        ],
                    ),
                    "node-feature-discovery": _node_feature_discovery(
                        ready=fnv1.READY_UNSPECIFIED,
                        wait=True,
                        worker_tolerations=[
                            {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                            {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                        ],
                    ),
                    "nvidia-dra-driver-gpu": _nvidia_dra_driver_gpu(ready=fnv1.READY_UNSPECIFIED),
                    "ai-gateway-crds": _ai_gateway_crds(ready=fnv1.READY_UNSPECIFIED),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferenceobjectives.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
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
                    "usage-cert-manager-by-envoy-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="envoy-gateway",
                    ),
                    "usage-ai-gateway-crds-by-ai-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="ai-gateway-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="ai-gateway",
                    ),
                    "usage-gateway-namespace-by-gateway-proxy": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-proxy",
                    ),
                    "usage-gateway-namespace-by-cluster-agent": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="cluster-agent",
                    ),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-selfsigned-issuer",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="trust-manager",
                    ),
                    "usage-gateway-class-by-gateway": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-class",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway",
                    ),
                    "usage-envoy-gateway-by-gateway-class": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="envoy-gateway",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-class",
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "destinations": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="TelemetryDestination"
                    ),
                    "mappings": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="MetricMapping"),
                }
            ),
        ),
    ),
    # Every PKI resource must be tracked for readiness: mark_readiness marks only
    # the keys compose_gateway_pki returns, so one composed but not returned
    # would silently hold the cluster un-Ready. Each is observed Ready here, so a
    # key dropped from the rendered list fails this case.
    ComposeCase(
        name="PKIReady",
        reason="Each gateway PKI resource observed Ready is marked ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Existing",
                    stack="Standard",
                    dra_drivers=["gpu.nvidia.com"],
                    secrets=[v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig")],
                    gateway=v1alpha1.Gateway(
                        hostname="gateway-test-backend-12345.modelplane-system.svc.cluster.local",
                        clientCAs=[
                            v1alpha1.ClientCA(name="fleet-b", certificate="BBB"),
                            v1alpha1.ClientCA(name="fleet-a", certificate="AAA"),
                        ],
                    ),
                    gpu=None,
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
                    "kube-prometheus-stack": _kube_prometheus_stack(
                        ready=fnv1.READY_UNSPECIFIED,
                        node_exporter_tolerations=[
                            {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                            {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                            {"key": "modelplane.ai/cpu", "operator": "Exists", "effect": "NoSchedule"},
                        ],
                    ),
                    "node-feature-discovery": _node_feature_discovery(
                        ready=fnv1.READY_UNSPECIFIED,
                        wait=True,
                        worker_tolerations=[
                            {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                            {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                        ],
                    ),
                    "nvidia-dra-driver-gpu": _nvidia_dra_driver_gpu(ready=fnv1.READY_UNSPECIFIED),
                    "ai-gateway-crds": _ai_gateway_crds(ready=fnv1.READY_UNSPECIFIED),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferenceobjectives.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
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
                    "usage-cert-manager-by-envoy-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="envoy-gateway",
                    ),
                    "usage-ai-gateway-crds-by-ai-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="ai-gateway-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="ai-gateway",
                    ),
                    "usage-gateway-namespace-by-gateway-proxy": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-proxy",
                    ),
                    "usage-gateway-namespace-by-cluster-agent": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="cluster-agent",
                    ),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-selfsigned-issuer",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="trust-manager",
                    ),
                    "usage-gateway-class-by-gateway": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-class",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway",
                    ),
                    "usage-envoy-gateway-by-gateway-class": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="envoy-gateway",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-class",
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "destinations": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="TelemetryDestination"
                    ),
                    "mappings": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="MetricMapping"),
                }
            ),
        ),
    ),
    # The GatewayClass and the cluster's own PKI are composed, so the CA is ready
    # to publish when the first InferenceGateway's CA arrives. The Gateway, the
    # client CA bundle and the policy demanding a client certificate aren't, and
    # nor is the Usage holding the GatewayClass for the Gateway.
    ComposeCase(
        name="NoClientCAs",
        reason="A cluster with no InferenceGateway CA to trust withholds its Gateway and warns.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Existing",
                    stack="Standard",
                    dra_drivers=["gpu.nvidia.com"],
                    secrets=[v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig")],
                    gateway=v1alpha1.Gateway(hostname="gw.clusters.example.com"),
                    gpu=None,
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
                    "kube-prometheus-stack": _kube_prometheus_stack(
                        ready=fnv1.READY_UNSPECIFIED,
                        node_exporter_tolerations=[
                            {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                            {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                            {"key": "modelplane.ai/cpu", "operator": "Exists", "effect": "NoSchedule"},
                        ],
                    ),
                    "node-feature-discovery": _node_feature_discovery(
                        ready=fnv1.READY_UNSPECIFIED,
                        wait=True,
                        worker_tolerations=[
                            {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                            {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                        ],
                    ),
                    "nvidia-dra-driver-gpu": _nvidia_dra_driver_gpu(ready=fnv1.READY_UNSPECIFIED),
                    "ai-gateway-crds": _ai_gateway_crds(ready=fnv1.READY_UNSPECIFIED),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferenceobjectives.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
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
                    "usage-cert-manager-by-envoy-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="envoy-gateway",
                    ),
                    "usage-ai-gateway-crds-by-ai-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="ai-gateway-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="ai-gateway",
                    ),
                    "usage-gateway-namespace-by-gateway-proxy": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-proxy",
                    ),
                    "usage-gateway-namespace-by-cluster-agent": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="cluster-agent",
                    ),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-selfsigned-issuer",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="trust-manager",
                    ),
                    "usage-envoy-gateway-by-gateway-class": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="envoy-gateway",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-class",
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message="Gateway gw.clusters.example.com not served: no InferenceGateway has published a client CA for this cluster to trust, and serving without one would accept unauthenticated callers",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "destinations": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="TelemetryDestination"
                    ),
                    "mappings": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="MetricMapping"),
                }
            ),
        ),
    ),
    # The install gate composes a component once its dependencies are observed
    # Ready, so the chain is observed up to the per-pool driver, which the DRA
    # driver waits on. The derived Usages hold the operator release and the
    # ConfigMap until the per-pool driver is gone. The stack has only the one
    # pool, so this can't show a pool that isn't flagged going without an
    # NVIDIADriver.
    ComposeCase(
        name="NvLinkDisabled",
        reason=(
            "A Civo stack flagging a pool for NVLink disable composes gpu-operator in NVIDIADriver-CRD mode, the "
            "kernel module ConfigMap, and an NVIDIADriver selecting that pool's nodes."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Civo",
                    stack="Standard",
                    dra_drivers=["gpu.nvidia.com"],
                    secrets=[v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig")],
                    gateway=v1alpha1.Gateway(hostname="test-backend.gateways.example.com"),
                    gpu=v1alpha1.Gpu(pools=[v1alpha1.Pool(name="h100-pool", disableNvLink=True)]),
                ),
                resources={
                    "provider-config-kubernetes": _observed_kubernetes_provider_config(),
                    "provider-config-helm": _observed_helm_provider_config(),
                    "cert-manager": _observed_ready(),
                    "node-feature-discovery": _observed_ready(),
                    "gpu-operator": _observed_ready(),
                    "nvlink-disable-config-gpu-operator": _observed_ready(),
                    "nvlink-disable-config-nvidia-kernel-config": _observed_ready(),
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
                    "cert-manager": _cert_manager(ready=fnv1.READY_TRUE),
                    "kube-prometheus-stack": _kube_prometheus_stack(
                        ready=fnv1.READY_UNSPECIFIED,
                        node_exporter_tolerations=[
                            {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                            {"key": "modelplane.ai/cpu", "operator": "Exists", "effect": "NoSchedule"},
                        ],
                    ),
                    "node-feature-discovery": _node_feature_discovery(
                        ready=fnv1.READY_TRUE,
                        wait=True,
                        worker_tolerations=[
                            {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                        ],
                    ),
                    "gpu-operator": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "mp-gpu-operator"},
                                    "labels": {"modelplane.ai/resource": "gpu-operator"},
                                },
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "chart": {
                                            "name": "gpu-operator",
                                            "repository": "https://helm.ngc.nvidia.com/nvidia",
                                            "version": "v26.7.1",
                                        },
                                        "namespace": "gpu-operator",
                                        "wait": True,
                                        "waitTimeout": "10m",
                                        "values": {
                                            "ccManager": {"enabled": False},
                                            "cdi": {"default": True, "enabled": True},
                                            "daemonsets": {
                                                "tolerations": [
                                                    {
                                                        "key": "nvidia.com/gpu",
                                                        "operator": "Exists",
                                                        "effect": "NoSchedule",
                                                    }
                                                ]
                                            },
                                            "dcgm": {"enabled": True},
                                            "dcgmExporter": {
                                                "config": {
                                                    "create": True,
                                                    "data": _DCGM_METRICS_CSV,
                                                    "name": "dcgm-exporter",
                                                },
                                                "serviceMonitor": {"enabled": False},
                                            },
                                            "devicePlugin": {"enabled": False},
                                            "driver": {
                                                "enabled": True,
                                                "maxParallelUpgrades": 5,
                                                "rdma": {"enabled": False},
                                                "kernelModuleType": "open",
                                                "version": "580.173.02",
                                                "nvidiaDriverCRD": {"enabled": True, "deployDefaultCR": True},
                                            },
                                            "fullnameOverride": "gpu-operator",
                                            "gdrcopy": {"enabled": False},
                                            "gfd": {"enabled": True},
                                            "kataSandboxDevicePlugin": {"enabled": False},
                                            "migManager": {"enabled": False},
                                            "nfd": {"enabled": False},
                                            "operator": {
                                                "resources": {
                                                    "limits": {"cpu": "500m", "memory": "700Mi"},
                                                    "requests": {"cpu": "200m", "memory": "300Mi"},
                                                },
                                                "tolerations": [],
                                                "upgradeCRD": True,
                                            },
                                            "toolkit": {"enabled": False},
                                            "validator": {
                                                "plugin": {"env": [{"name": "WITH_WORKLOAD", "value": "false"}]}
                                            },
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
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
                        )
                    ),
                    "ai-gateway-crds": _ai_gateway_crds(ready=fnv1.READY_UNSPECIFIED),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferenceobjectives.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gateway-namespace": _gateway_namespace(ready=fnv1.READY_UNSPECIFIED),
                    "dra-driver-critical-pods-quota": _dra_driver_critical_pods_quota(ready=fnv1.READY_UNSPECIFIED),
                    "leader-worker-set": _leader_worker_set(),
                    "nvlink-disable-config-gpu-operator": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {
                                    "labels": {"modelplane.ai/resource": "nvlink-disable-config-gpu-operator"}
                                },
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "Namespace",
                                            "metadata": {"name": "gpu-operator"},
                                        }
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "nvlink-disable-config-nvidia-kernel-config": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {
                                    "labels": {"modelplane.ai/resource": "nvlink-disable-config-nvidia-kernel-config"}
                                },
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "ConfigMap",
                                            "metadata": {"name": "nvidia-kernel-config", "namespace": "gpu-operator"},
                                            "data": {"nvidia.conf": "options nvidia NVreg_NvLinkDisable=1"},
                                        }
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "nvlink-disabled-driver-h100-pool": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"labels": {"modelplane.ai/resource": "nvlink-disabled-driver-h100-pool"}},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "nvidia.com/v1alpha1",
                                            "kind": "NVIDIADriver",
                                            "metadata": {"name": "nvlink-disabled-h100-pool"},
                                            "spec": {
                                                "driverType": "gpu",
                                                "version": "580.173.02",
                                                "useOpenKernelModules": True,
                                                "nodeSelector": {"modelplane.ai/pool": "h100-pool"},
                                                "kernelModuleConfig": {"name": "nvidia-kernel-config"},
                                                "tolerations": [
                                                    {
                                                        "key": "nvidia.com/gpu",
                                                        "operator": "Exists",
                                                        "effect": "NoSchedule",
                                                    }
                                                ],
                                            },
                                        }
                                    },
                                    "readiness": {
                                        "celQuery": 'object.status.state == "ready"',
                                        "policy": "DeriveFromCelQuery",
                                    },
                                },
                            }
                        )
                    ),
                    "gateway-class": _gateway_class(ready=fnv1.READY_UNSPECIFIED),
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
                    "usage-node-feature-discovery-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="node-feature-discovery",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-cert-manager-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvidia-dra-driver-gpu",
                    ),
                    "usage-nvlink-disabled-driver-h100-pool-by-nvidia-dra-driver-gpu": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="nvlink-disabled-driver-h100-pool",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvidia-dra-driver-gpu",
                    ),
                    "usage-cert-manager-by-envoy-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="envoy-gateway",
                    ),
                    "usage-ai-gateway-crds-by-ai-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="ai-gateway-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="ai-gateway",
                    ),
                    "usage-gateway-namespace-by-gateway-proxy": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-proxy",
                    ),
                    "usage-gateway-namespace-by-cluster-agent": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="cluster-agent",
                    ),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-selfsigned-issuer",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="trust-manager",
                    ),
                    "usage-gpu-operator-by-nvlink-disabled-driver-h100-pool": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="nvlink-disabled-driver-h100-pool",
                    ),
                    "usage-nvlink-disable-config-gpu-operator-by-nvlink-disabled-driver-h100-pool": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="nvlink-disable-config-gpu-operator",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="nvlink-disabled-driver-h100-pool",
                    ),
                    "usage-nvlink-disable-config-nvidia-kernel-config-by-nvlink-disabled-driver-h100-pool": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="nvlink-disable-config-nvidia-kernel-config",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="nvlink-disabled-driver-h100-pool",
                    ),
                    "usage-envoy-gateway-by-gateway-class": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="envoy-gateway",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-class",
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message="Gateway test-backend.gateways.example.com not served: no InferenceGateway has published a client CA for this cluster to trust, and serving without one would accept unauthenticated callers",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "destinations": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="TelemetryDestination"
                    ),
                    "mappings": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="MetricMapping"),
                }
            ),
        ),
    ),
    # The gpu-operator release keeps its ClusterPolicy-managed driver.
    ComposeCase(
        name="NvLinkEnabled",
        reason=(
            "A Civo stack flagging no pool for NVLink disable composes the stock GPU operator, with no NVIDIADriver "
            "or kernel module ConfigMap."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Civo",
                    stack="Standard",
                    dra_drivers=["gpu.nvidia.com"],
                    secrets=[v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig")],
                    gateway=v1alpha1.Gateway(hostname="test-backend.gateways.example.com"),
                    gpu=v1alpha1.Gpu(pools=[v1alpha1.Pool(name="l40s-pool", disableNvLink=False)]),
                ),
                resources={
                    "provider-config-kubernetes": _observed_kubernetes_provider_config(),
                    "provider-config-helm": _observed_helm_provider_config(),
                    "gpu-operator": _observed_ready(),
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
                    "kube-prometheus-stack": _kube_prometheus_stack(
                        ready=fnv1.READY_UNSPECIFIED,
                        node_exporter_tolerations=[
                            {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                            {"key": "modelplane.ai/cpu", "operator": "Exists", "effect": "NoSchedule"},
                        ],
                    ),
                    "node-feature-discovery": _node_feature_discovery(
                        ready=fnv1.READY_UNSPECIFIED,
                        wait=True,
                        worker_tolerations=[
                            {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                        ],
                    ),
                    "gpu-operator": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "mp-gpu-operator"},
                                    "labels": {"modelplane.ai/resource": "gpu-operator"},
                                },
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "chart": {
                                            "name": "gpu-operator",
                                            "repository": "https://helm.ngc.nvidia.com/nvidia",
                                            "version": "v26.7.1",
                                        },
                                        "namespace": "gpu-operator",
                                        "wait": True,
                                        "waitTimeout": "10m",
                                        "values": {
                                            "ccManager": {"enabled": False},
                                            "cdi": {"default": True, "enabled": True},
                                            "daemonsets": {
                                                "tolerations": [
                                                    {
                                                        "key": "nvidia.com/gpu",
                                                        "operator": "Exists",
                                                        "effect": "NoSchedule",
                                                    }
                                                ]
                                            },
                                            "dcgm": {"enabled": True},
                                            "dcgmExporter": {
                                                "config": {
                                                    "create": True,
                                                    "data": _DCGM_METRICS_CSV,
                                                    "name": "dcgm-exporter",
                                                },
                                                "serviceMonitor": {"enabled": False},
                                            },
                                            "devicePlugin": {"enabled": False},
                                            "driver": {
                                                "enabled": True,
                                                "maxParallelUpgrades": 5,
                                                "rdma": {"enabled": False},
                                                "kernelModuleType": "open",
                                                "version": "580.173.02",
                                            },
                                            "fullnameOverride": "gpu-operator",
                                            "gdrcopy": {"enabled": False},
                                            "gfd": {"enabled": True},
                                            "kataSandboxDevicePlugin": {"enabled": False},
                                            "migManager": {"enabled": False},
                                            "nfd": {"enabled": False},
                                            "operator": {
                                                "resources": {
                                                    "limits": {"cpu": "500m", "memory": "700Mi"},
                                                    "requests": {"cpu": "200m", "memory": "300Mi"},
                                                },
                                                "tolerations": [],
                                                "upgradeCRD": True,
                                            },
                                            "toolkit": {"enabled": False},
                                            "validator": {
                                                "plugin": {"env": [{"name": "WITH_WORKLOAD", "value": "false"}]}
                                            },
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    # Not _nvidia_dra_driver_gpu: Civo's DRA driver points
                    # nvidiaDriverRoot at the gpu-operator's driver, and this is
                    # the only case that writes it out.
                    "nvidia-dra-driver-gpu": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "mp-dra-driver-nvidia-gpu"},
                                    "labels": {"modelplane.ai/resource": "nvidia-dra-driver-gpu"},
                                },
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "chart": {
                                            "name": "dra-driver-nvidia-gpu",
                                            "repository": "oci://registry.k8s.io/dra-driver-nvidia/charts",
                                            "version": "0.5.0",
                                        },
                                        "namespace": "nvidia-dra-driver",
                                        "values": {
                                            "gpuResourcesEnabledOverride": True,
                                            "nvidiaDriverRoot": "/run/nvidia/driver",
                                            "resources": {"computeDomains": {"enabled": False}},
                                        },
                                    },
                                },
                            }
                        )
                    ),
                    "ai-gateway-crds": _ai_gateway_crds(ready=fnv1.READY_UNSPECIFIED),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferenceobjectives.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gateway-namespace": _gateway_namespace(ready=fnv1.READY_UNSPECIFIED),
                    "dra-driver-critical-pods-quota": _dra_driver_critical_pods_quota(ready=fnv1.READY_UNSPECIFIED),
                    "leader-worker-set": _leader_worker_set(),
                    "gateway-class": _gateway_class(ready=fnv1.READY_UNSPECIFIED),
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
                    "usage-node-feature-discovery-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="node-feature-discovery",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-cert-manager-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvidia-dra-driver-gpu",
                    ),
                    "usage-cert-manager-by-envoy-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="envoy-gateway",
                    ),
                    "usage-ai-gateway-crds-by-ai-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="ai-gateway-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="ai-gateway",
                    ),
                    "usage-gateway-namespace-by-gateway-proxy": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-proxy",
                    ),
                    "usage-gateway-namespace-by-cluster-agent": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="cluster-agent",
                    ),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-selfsigned-issuer",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="trust-manager",
                    ),
                    "usage-envoy-gateway-by-gateway-class": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="envoy-gateway",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-class",
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message="Gateway test-backend.gateways.example.com not served: no InferenceGateway has published a client CA for this cluster to trust, and serving without one would accept unauthenticated callers",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "destinations": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="TelemetryDestination"
                    ),
                    "mappings": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="MetricMapping"),
                }
            ),
        ),
    ),
    # Everything else the stack composes is Ready only once its observed Ready
    # condition says so, because the fleet can't serve without it. The collector
    # only watches, so gating on it would put placing a replica behind exporting
    # a metric: one destination pointing at an endpoint that has gone away would
    # take every InferenceCluster in the fleet out of Ready and stop the
    # scheduler.
    ComposeCase(
        name="CollectorNotGated",
        reason="With a TelemetryDestination, a stack composes the collector, Ready before anything has observed it.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="GKE",
                    stack="Standard",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
                ),
                resources={
                    "provider-config-kubernetes": _observed_kubernetes_provider_config(),
                    "provider-config-helm": _observed_helm_provider_config(),
                },
            ),
            required_resources={
                "destinations": fnv1.Resources(
                    items=[
                        _telemetry_destination(
                            name="acme",
                            sinks=[{"name": "primary", "type": "otlphttp", "endpoint": "https://otel.acme.example"}],
                        )
                    ]
                ),
                "mappings": fnv1.Resources(),
            },
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
                    "cert-manager": _gke_cert_manager(),
                    "node-feature-discovery": _gke_node_feature_discovery(),
                    "nodewright-operator": _nodewright_operator(),
                    "prometheus-operator-crds": _prometheus_operator_crds(),
                    "gpu-operator-pre-manifests-gpu-operator": _gpu_operator_pre_manifests_gpu_operator(),
                    "gpu-operator-pre-manifests-aicr-gke-critical-pods": _gpu_operator_pre_manifests_aicr_gke_critical_pods(),
                    "ai-gateway-crds": _ai_gateway_crds(ready=fnv1.READY_UNSPECIFIED),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferenceobjectives.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gateway-namespace": _gateway_namespace(ready=fnv1.READY_UNSPECIFIED),
                    "dra-driver-critical-pods-quota": _dra_driver_critical_pods_quota(ready=fnv1.READY_UNSPECIFIED),
                    "leader-worker-set": _leader_worker_set(),
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
                    "collector-serviceaccount": _collector_service_account(),
                    "collector-clusterrole": _collector_cluster_role(),
                    "collector-clusterrolebinding": _collector_cluster_role_binding(),
                    "collector-config": _collector_config(
                        exporters={"otlphttp/primary": {"endpoint": "https://otel.acme.example"}},
                        pipeline_exporters=["otlphttp/primary"],
                        extensions=None,
                        service_extensions=None,
                    ),
                    "collector": _collector(
                        config_hash="2fbc7d728e156867",
                        volumes=[{"name": "config", "configMap": {"name": "modelplane-collector"}}],
                        volume_mounts=[{"name": "config", "mountPath": "/conf"}],
                        env_from=None,
                    ),
                    "usage-prometheus-operator-crds-by-kube-prometheus-stack": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="prometheus-operator-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="kube-prometheus-stack",
                    ),
                    "usage-node-feature-discovery-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="node-feature-discovery",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-cert-manager-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-kube-prometheus-stack-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="kube-prometheus-stack",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-gpu-operator-pre-manifests-gpu-operator-by-gpu-operator": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gpu-operator-pre-manifests-gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-gpu-operator-pre-manifests-aicr-gke-critical-pods-by-gpu-operator": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gpu-operator-pre-manifests-aicr-gke-critical-pods",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-kube-prometheus-stack-by-k8s-ephemeral-storage-metrics": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="kube-prometheus-stack",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="k8s-ephemeral-storage-metrics",
                    ),
                    "usage-prometheus-operator-crds-by-k8s-ephemeral-storage-metrics": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="prometheus-operator-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="k8s-ephemeral-storage-metrics",
                    ),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvidia-dra-driver-gpu",
                    ),
                    "usage-cert-manager-by-nvsentinel": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvsentinel",
                    ),
                    "usage-gpu-operator-by-nvsentinel": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvsentinel",
                    ),
                    "usage-prometheus-operator-crds-by-nvsentinel": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="prometheus-operator-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvsentinel",
                    ),
                    "usage-kube-prometheus-stack-by-prometheus-adapter": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="kube-prometheus-stack",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="prometheus-adapter",
                    ),
                    "usage-cert-manager-by-envoy-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="envoy-gateway",
                    ),
                    "usage-ai-gateway-crds-by-ai-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="ai-gateway-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="ai-gateway",
                    ),
                    "usage-gateway-namespace-by-gateway-proxy": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-proxy",
                    ),
                    "usage-gateway-namespace-by-cluster-agent": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="cluster-agent",
                    ),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-selfsigned-issuer",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="trust-manager",
                    ),
                    "usage-gateway-class-by-gateway": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-class",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway",
                    ),
                    "usage-envoy-gateway-by-gateway-class": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="envoy-gateway",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-class",
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "destinations": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="TelemetryDestination"
                    ),
                    "mappings": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="MetricMapping"),
                }
            ),
        ),
    ),
    # A TelemetryDestination is cluster-scoped on the control plane, and the
    # collector runs on every workload cluster in the fleet. Resolving the Secret
    # and stopping there would leave the Deployment mounting a Secret nothing out
    # there creates, so the pod would never start. The Secret is required from
    # modelplane-system because, unqualified, the requirement would match a
    # Secret of that name anywhere. Its data is copied verbatim: re-encoding
    # would corrupt a credential that isn't text.
    ComposeCase(
        name="CollectorCredential",
        reason=(
            "A destination's credential is required from modelplane-system and composed onto the cluster whose "
            "collector mounts it."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="GKE",
                    stack="Standard",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
                ),
                resources={
                    "provider-config-kubernetes": _observed_kubernetes_provider_config(),
                    "provider-config-helm": _observed_helm_provider_config(),
                },
            ),
            required_resources={
                "destinations": fnv1.Resources(
                    items=[
                        _telemetry_destination(
                            name="acme",
                            sinks=[
                                {
                                    "name": "primary",
                                    "type": "otlphttp",
                                    "endpoint": "https://otel.acme.example",
                                    "secretRef": {"name": "telemetry-credentials"},
                                    "auth": {"bearerTokenKey": "token"},
                                }
                            ],
                        )
                    ]
                ),
                "mappings": fnv1.Resources(),
                "collector-secret-primary": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "v1",
                                    "kind": "Secret",
                                    "metadata": {"name": "telemetry-credentials", "namespace": "modelplane-system"},
                                    "data": {"token": "c2hoaGg="},
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
                    "cert-manager": _gke_cert_manager(),
                    "node-feature-discovery": _gke_node_feature_discovery(),
                    "nodewright-operator": _nodewright_operator(),
                    "prometheus-operator-crds": _prometheus_operator_crds(),
                    "gpu-operator-pre-manifests-gpu-operator": _gpu_operator_pre_manifests_gpu_operator(),
                    "gpu-operator-pre-manifests-aicr-gke-critical-pods": _gpu_operator_pre_manifests_aicr_gke_critical_pods(),
                    "ai-gateway-crds": _ai_gateway_crds(ready=fnv1.READY_UNSPECIFIED),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferenceobjectives.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gateway-namespace": _gateway_namespace(ready=fnv1.READY_UNSPECIFIED),
                    "dra-driver-critical-pods-quota": _dra_driver_critical_pods_quota(ready=fnv1.READY_UNSPECIFIED),
                    "leader-worker-set": _leader_worker_set(),
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
                    "collector-serviceaccount": _collector_service_account(),
                    "collector-clusterrole": _collector_cluster_role(),
                    "collector-clusterrolebinding": _collector_cluster_role_binding(),
                    "collector-config": _collector_config(
                        exporters={
                            "otlphttp/primary": {
                                "endpoint": "https://otel.acme.example",
                                "auth": {"authenticator": "bearertokenauth/primary"},
                            }
                        },
                        pipeline_exporters=["otlphttp/primary"],
                        extensions={"bearertokenauth/primary": {"filename": "/etc/modelplane/telemetry/primary/token"}},
                        service_extensions=["bearertokenauth/primary"],
                    ),
                    "collector-secret-primary": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"labels": {"modelplane.ai/resource": "collector-secret-primary"}},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-backend-cluster-63fde",
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "Secret",
                                            "metadata": {
                                                "name": "telemetry-credentials",
                                                "namespace": "modelplane-system",
                                            },
                                            "data": {"token": "c2hoaGg="},
                                        }
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "collector": _collector(
                        config_hash="c5b6d4a261a1e104",
                        volumes=[
                            {"name": "config", "configMap": {"name": "modelplane-collector"}},
                            {"name": "credentials-primary", "secret": {"secretName": "telemetry-credentials"}},
                        ],
                        volume_mounts=[
                            {"name": "config", "mountPath": "/conf"},
                            {
                                "name": "credentials-primary",
                                "mountPath": "/etc/modelplane/telemetry/primary",
                                "readOnly": True,
                            },
                        ],
                        env_from=[{"secretRef": {"name": "telemetry-credentials"}}],
                    ),
                    "usage-prometheus-operator-crds-by-kube-prometheus-stack": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="prometheus-operator-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="kube-prometheus-stack",
                    ),
                    "usage-node-feature-discovery-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="node-feature-discovery",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-cert-manager-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-kube-prometheus-stack-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="kube-prometheus-stack",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-gpu-operator-pre-manifests-gpu-operator-by-gpu-operator": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gpu-operator-pre-manifests-gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-gpu-operator-pre-manifests-aicr-gke-critical-pods-by-gpu-operator": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gpu-operator-pre-manifests-aicr-gke-critical-pods",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-kube-prometheus-stack-by-k8s-ephemeral-storage-metrics": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="kube-prometheus-stack",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="k8s-ephemeral-storage-metrics",
                    ),
                    "usage-prometheus-operator-crds-by-k8s-ephemeral-storage-metrics": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="prometheus-operator-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="k8s-ephemeral-storage-metrics",
                    ),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvidia-dra-driver-gpu",
                    ),
                    "usage-cert-manager-by-nvsentinel": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvsentinel",
                    ),
                    "usage-gpu-operator-by-nvsentinel": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvsentinel",
                    ),
                    "usage-prometheus-operator-crds-by-nvsentinel": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="prometheus-operator-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvsentinel",
                    ),
                    "usage-kube-prometheus-stack-by-prometheus-adapter": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="kube-prometheus-stack",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="prometheus-adapter",
                    ),
                    "usage-cert-manager-by-envoy-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="envoy-gateway",
                    ),
                    "usage-ai-gateway-crds-by-ai-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="ai-gateway-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="ai-gateway",
                    ),
                    "usage-gateway-namespace-by-gateway-proxy": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-proxy",
                    ),
                    "usage-gateway-namespace-by-cluster-agent": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="cluster-agent",
                    ),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-selfsigned-issuer",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="trust-manager",
                    ),
                    "usage-gateway-class-by-gateway": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-class",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway",
                    ),
                    "usage-envoy-gateway-by-gateway-class": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="envoy-gateway",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-class",
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "destinations": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="TelemetryDestination"
                    ),
                    "mappings": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="MetricMapping"),
                    "collector-secret-primary": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="telemetry-credentials",
                        namespace="modelplane-system",
                    ),
                }
            ),
        ),
    ),
    # A second backend is a second object, not an edit to a singleton. Picking
    # one destination and warning about the rest would mean a team adding an
    # export has to edit an object another team owns, and gets silence if they
    # create their own instead.
    ComposeCase(
        name="TwoDestinations",
        reason="Every TelemetryDestination contributes its sinks to the one collector.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="GKE",
                    stack="Standard",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
                ),
                resources={
                    "provider-config-kubernetes": _observed_kubernetes_provider_config(),
                    "provider-config-helm": _observed_helm_provider_config(),
                },
            ),
            required_resources={
                "destinations": fnv1.Resources(
                    items=[
                        _telemetry_destination(
                            name="acme",
                            sinks=[{"name": "vendor", "type": "otlphttp", "endpoint": "https://otel.vendor.example"}],
                        ),
                        _telemetry_destination(
                            name="zeta",
                            sinks=[
                                {"name": "prom", "type": "prometheus_remote_write", "endpoint": "https://p.example/w"}
                            ],
                        ),
                    ]
                ),
                "mappings": fnv1.Resources(),
            },
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
                    "cert-manager": _gke_cert_manager(),
                    "node-feature-discovery": _gke_node_feature_discovery(),
                    "nodewright-operator": _nodewright_operator(),
                    "prometheus-operator-crds": _prometheus_operator_crds(),
                    "gpu-operator-pre-manifests-gpu-operator": _gpu_operator_pre_manifests_gpu_operator(),
                    "gpu-operator-pre-manifests-aicr-gke-critical-pods": _gpu_operator_pre_manifests_aicr_gke_critical_pods(),
                    "ai-gateway-crds": _ai_gateway_crds(ready=fnv1.READY_UNSPECIFIED),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferenceobjectives.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gateway-namespace": _gateway_namespace(ready=fnv1.READY_UNSPECIFIED),
                    "dra-driver-critical-pods-quota": _dra_driver_critical_pods_quota(ready=fnv1.READY_UNSPECIFIED),
                    "leader-worker-set": _leader_worker_set(),
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
                    "collector-serviceaccount": _collector_service_account(),
                    "collector-clusterrole": _collector_cluster_role(),
                    "collector-clusterrolebinding": _collector_cluster_role_binding(),
                    "collector-config": _collector_config(
                        exporters={
                            "otlphttp/vendor": {"endpoint": "https://otel.vendor.example"},
                            "prometheus_remote_write/prom": {
                                "resource_to_telemetry_conversion": {"enabled": True},
                                "endpoint": "https://p.example/w",
                            },
                        },
                        pipeline_exporters=["otlphttp/vendor", "prometheus_remote_write/prom"],
                        extensions=None,
                        service_extensions=None,
                    ),
                    "collector": _collector(
                        config_hash="9623243a4c151500",
                        volumes=[{"name": "config", "configMap": {"name": "modelplane-collector"}}],
                        volume_mounts=[{"name": "config", "mountPath": "/conf"}],
                        env_from=None,
                    ),
                    "usage-prometheus-operator-crds-by-kube-prometheus-stack": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="prometheus-operator-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="kube-prometheus-stack",
                    ),
                    "usage-node-feature-discovery-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="node-feature-discovery",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-cert-manager-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-kube-prometheus-stack-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="kube-prometheus-stack",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-gpu-operator-pre-manifests-gpu-operator-by-gpu-operator": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gpu-operator-pre-manifests-gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-gpu-operator-pre-manifests-aicr-gke-critical-pods-by-gpu-operator": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gpu-operator-pre-manifests-aicr-gke-critical-pods",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-kube-prometheus-stack-by-k8s-ephemeral-storage-metrics": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="kube-prometheus-stack",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="k8s-ephemeral-storage-metrics",
                    ),
                    "usage-prometheus-operator-crds-by-k8s-ephemeral-storage-metrics": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="prometheus-operator-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="k8s-ephemeral-storage-metrics",
                    ),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvidia-dra-driver-gpu",
                    ),
                    "usage-cert-manager-by-nvsentinel": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvsentinel",
                    ),
                    "usage-gpu-operator-by-nvsentinel": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvsentinel",
                    ),
                    "usage-prometheus-operator-crds-by-nvsentinel": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="prometheus-operator-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvsentinel",
                    ),
                    "usage-kube-prometheus-stack-by-prometheus-adapter": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="kube-prometheus-stack",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="prometheus-adapter",
                    ),
                    "usage-cert-manager-by-envoy-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="envoy-gateway",
                    ),
                    "usage-ai-gateway-crds-by-ai-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="ai-gateway-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="ai-gateway",
                    ),
                    "usage-gateway-namespace-by-gateway-proxy": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-proxy",
                    ),
                    "usage-gateway-namespace-by-cluster-agent": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="cluster-agent",
                    ),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-selfsigned-issuer",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="trust-manager",
                    ),
                    "usage-gateway-class-by-gateway": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-class",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway",
                    ),
                    "usage-envoy-gateway-by-gateway-class": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="envoy-gateway",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-class",
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "destinations": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="TelemetryDestination"
                    ),
                    "mappings": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="MetricMapping"),
                }
            ),
        ),
    ),
    # A sink names the collector's exporter instance, so two of them under one
    # name would be one exporter with two meanings. Dropping one beats failing
    # the whole fleet's telemetry over a name. acme is listed first and sorts
    # first, so this can't show the destinations being sorted before they merge.
    ComposeCase(
        name="SinkNameClash",
        reason="Of two destinations naming one sink, the first keeps it and the other is dropped with a warning.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="GKE",
                    stack="Standard",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
                ),
                resources={
                    "provider-config-kubernetes": _observed_kubernetes_provider_config(),
                    "provider-config-helm": _observed_helm_provider_config(),
                },
            ),
            required_resources={
                "destinations": fnv1.Resources(
                    items=[
                        _telemetry_destination(
                            name="acme",
                            sinks=[{"name": "primary", "type": "otlphttp", "endpoint": "https://a.example"}],
                        ),
                        _telemetry_destination(
                            name="zeta",
                            sinks=[{"name": "primary", "type": "otlphttp", "endpoint": "https://z.example"}],
                        ),
                    ]
                ),
                "mappings": fnv1.Resources(),
            },
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
                    "cert-manager": _gke_cert_manager(),
                    "node-feature-discovery": _gke_node_feature_discovery(),
                    "nodewright-operator": _nodewright_operator(),
                    "prometheus-operator-crds": _prometheus_operator_crds(),
                    "gpu-operator-pre-manifests-gpu-operator": _gpu_operator_pre_manifests_gpu_operator(),
                    "gpu-operator-pre-manifests-aicr-gke-critical-pods": _gpu_operator_pre_manifests_aicr_gke_critical_pods(),
                    "ai-gateway-crds": _ai_gateway_crds(ready=fnv1.READY_UNSPECIFIED),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferenceobjectives.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gateway-namespace": _gateway_namespace(ready=fnv1.READY_UNSPECIFIED),
                    "dra-driver-critical-pods-quota": _dra_driver_critical_pods_quota(ready=fnv1.READY_UNSPECIFIED),
                    "leader-worker-set": _leader_worker_set(),
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
                    "collector-serviceaccount": _collector_service_account(),
                    "collector-clusterrole": _collector_cluster_role(),
                    "collector-clusterrolebinding": _collector_cluster_role_binding(),
                    "collector-config": _collector_config(
                        exporters={"otlphttp/primary": {"endpoint": "https://a.example"}},
                        pipeline_exporters=["otlphttp/primary"],
                        extensions=None,
                        service_extensions=None,
                    ),
                    "collector": _collector(
                        config_hash="39f04258f672d756",
                        volumes=[{"name": "config", "configMap": {"name": "modelplane-collector"}}],
                        volume_mounts=[{"name": "config", "mountPath": "/conf"}],
                        env_from=None,
                    ),
                    "usage-prometheus-operator-crds-by-kube-prometheus-stack": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="prometheus-operator-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="kube-prometheus-stack",
                    ),
                    "usage-node-feature-discovery-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="node-feature-discovery",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-cert-manager-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-kube-prometheus-stack-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="kube-prometheus-stack",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-gpu-operator-pre-manifests-gpu-operator-by-gpu-operator": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gpu-operator-pre-manifests-gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-gpu-operator-pre-manifests-aicr-gke-critical-pods-by-gpu-operator": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gpu-operator-pre-manifests-aicr-gke-critical-pods",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-kube-prometheus-stack-by-k8s-ephemeral-storage-metrics": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="kube-prometheus-stack",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="k8s-ephemeral-storage-metrics",
                    ),
                    "usage-prometheus-operator-crds-by-k8s-ephemeral-storage-metrics": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="prometheus-operator-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="k8s-ephemeral-storage-metrics",
                    ),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvidia-dra-driver-gpu",
                    ),
                    "usage-cert-manager-by-nvsentinel": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvsentinel",
                    ),
                    "usage-gpu-operator-by-nvsentinel": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvsentinel",
                    ),
                    "usage-prometheus-operator-crds-by-nvsentinel": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="prometheus-operator-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvsentinel",
                    ),
                    "usage-kube-prometheus-stack-by-prometheus-adapter": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="kube-prometheus-stack",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="prometheus-adapter",
                    ),
                    "usage-cert-manager-by-envoy-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="envoy-gateway",
                    ),
                    "usage-ai-gateway-crds-by-ai-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="ai-gateway-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="ai-gateway",
                    ),
                    "usage-gateway-namespace-by-gateway-proxy": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-proxy",
                    ),
                    "usage-gateway-namespace-by-cluster-agent": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="cluster-agent",
                    ),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-selfsigned-issuer",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="trust-manager",
                    ),
                    "usage-gateway-class-by-gateway": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-class",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway",
                    ),
                    "usage-envoy-gateway-by-gateway-class": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="envoy-gateway",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-class",
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message="Ignored sink primary in TelemetryDestination zeta: already defined by a TelemetryDestination sorting earlier.",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "destinations": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="TelemetryDestination"
                    ),
                    "mappings": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="MetricMapping"),
                }
            ),
        ),
    ),
    # A CRD validates on write, not on what it already stored, so a mapping
    # written against an older schema comes back on read as it was stored.
    # Parsing it raises, and raising would fail the whole pipeline step: the
    # stack would compose nothing and the fleet would stop placing replicas,
    # because one telemetry object is out of date. Seen on a real cluster, where
    # a mapping predating a required field did it. The collector's config has
    # the built-in mappings alone.
    ComposeCase(
        name="StaleMapping",
        reason=(
            "A MetricMapping that no longer matches the schema is dropped with a warning, and the stack still composes."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="GKE",
                    stack="Standard",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
                ),
                resources={
                    "provider-config-kubernetes": _observed_kubernetes_provider_config(),
                    "provider-config-helm": _observed_helm_provider_config(),
                },
            ),
            required_resources={
                "destinations": fnv1.Resources(
                    items=[
                        _telemetry_destination(
                            name="acme",
                            sinks=[{"name": "primary", "type": "otlphttp", "endpoint": "https://otel.acme.example"}],
                        )
                    ]
                ),
                "mappings": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "modelplane.ai/v1alpha1",
                                    "kind": "MetricMapping",
                                    "metadata": {"name": "stale"},
                                    "spec": {
                                        "metrics": [
                                            {
                                                "from": "old_engine_transfer",
                                                "to": "modelplane_request_kv_transfer_seconds",
                                                "fromUnit": "Centiseconds",  # A unit the enum no longer carries.
                                            }
                                        ]
                                    },
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
                    "cert-manager": _gke_cert_manager(),
                    "node-feature-discovery": _gke_node_feature_discovery(),
                    "nodewright-operator": _nodewright_operator(),
                    "prometheus-operator-crds": _prometheus_operator_crds(),
                    "gpu-operator-pre-manifests-gpu-operator": _gpu_operator_pre_manifests_gpu_operator(),
                    "gpu-operator-pre-manifests-aicr-gke-critical-pods": _gpu_operator_pre_manifests_aicr_gke_critical_pods(),
                    "ai-gateway-crds": _ai_gateway_crds(ready=fnv1.READY_UNSPECIFIED),
                    "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferenceobjectives.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gaie-crds-inferencepools.inference.networking.x-k8s.io": _gaie_crd(
                        name="inferencepools.inference.networking.x-k8s.io", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "gateway-namespace": _gateway_namespace(ready=fnv1.READY_UNSPECIFIED),
                    "dra-driver-critical-pods-quota": _dra_driver_critical_pods_quota(ready=fnv1.READY_UNSPECIFIED),
                    "leader-worker-set": _leader_worker_set(),
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
                    "collector-serviceaccount": _collector_service_account(),
                    "collector-clusterrole": _collector_cluster_role(),
                    "collector-clusterrolebinding": _collector_cluster_role_binding(),
                    "collector-config": _collector_config(
                        exporters={"otlphttp/primary": {"endpoint": "https://otel.acme.example"}},
                        pipeline_exporters=["otlphttp/primary"],
                        extensions=None,
                        service_extensions=None,
                    ),
                    "collector": _collector(
                        config_hash="2fbc7d728e156867",
                        volumes=[{"name": "config", "configMap": {"name": "modelplane-collector"}}],
                        volume_mounts=[{"name": "config", "mountPath": "/conf"}],
                        env_from=None,
                    ),
                    "usage-prometheus-operator-crds-by-kube-prometheus-stack": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="prometheus-operator-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="kube-prometheus-stack",
                    ),
                    "usage-node-feature-discovery-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="node-feature-discovery",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-cert-manager-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-kube-prometheus-stack-by-gpu-operator": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="kube-prometheus-stack",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-gpu-operator-pre-manifests-gpu-operator-by-gpu-operator": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gpu-operator-pre-manifests-gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-gpu-operator-pre-manifests-aicr-gke-critical-pods-by-gpu-operator": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gpu-operator-pre-manifests-aicr-gke-critical-pods",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="gpu-operator",
                    ),
                    "usage-kube-prometheus-stack-by-k8s-ephemeral-storage-metrics": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="kube-prometheus-stack",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="k8s-ephemeral-storage-metrics",
                    ),
                    "usage-prometheus-operator-crds-by-k8s-ephemeral-storage-metrics": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="prometheus-operator-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="k8s-ephemeral-storage-metrics",
                    ),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvidia-dra-driver-gpu",
                    ),
                    "usage-cert-manager-by-nvsentinel": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvsentinel",
                    ),
                    "usage-gpu-operator-by-nvsentinel": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="gpu-operator",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvsentinel",
                    ),
                    "usage-prometheus-operator-crds-by-nvsentinel": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="prometheus-operator-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="nvsentinel",
                    ),
                    "usage-kube-prometheus-stack-by-prometheus-adapter": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="kube-prometheus-stack",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="prometheus-adapter",
                    ),
                    "usage-cert-manager-by-envoy-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="envoy-gateway",
                    ),
                    "usage-ai-gateway-crds-by-ai-gateway": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="ai-gateway-crds",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="ai-gateway",
                    ),
                    "usage-gateway-namespace-by-gateway-proxy": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-proxy",
                    ),
                    "usage-gateway-namespace-by-cluster-agent": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="cluster-agent",
                    ),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="cert-manager",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-namespace",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-selfsigned-issuer",
                    ),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-selfsigned-issuer",
                        by_api_version="helm.m.crossplane.io/v1beta1",
                        by_kind="Release",
                        by_key="trust-manager",
                    ),
                    "usage-gateway-class-by-gateway": _usage(
                        of_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        of_kind="Object",
                        of_key="gateway-class",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway",
                    ),
                    "usage-envoy-gateway-by-gateway-class": _usage(
                        of_api_version="helm.m.crossplane.io/v1beta1",
                        of_kind="Release",
                        of_key="envoy-gateway",
                        by_api_version="kubernetes.m.crossplane.io/v1alpha1",
                        by_kind="Object",
                        by_key="gateway-class",
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message="Ignoring MetricMapping stale: it does not match the current schema (1 problems), so nothing it asks for is collected.",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "destinations": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="TelemetryDestination"
                    ),
                    "mappings": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="MetricMapping"),
                }
            ),
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: ComposeCase) -> None:
    """RunFunction composes the serving stack, gated on what's observed."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason


# The composed-resource key a component renders under is its identity:
# renaming one deletes and recreates the remote resource (for an Object
# holding a CRD, the CRD and its CRs). This pins the full key set per
# cloud and stack, including the Usage keys derived from depends_on, as
# reviewed literals. A failure here means the stack data changed a key -
# make sure that's intended, then update that case's want and the release
# notes.
#
# Each case's XR has an InferenceGateway CA to trust, so the Gateway and its
# client-auth policy are included. Every key the case expects is observed
# Ready, so the depends_on install gate opens and the full stack renders; a
# key the function doesn't render still fails the comparison. The identity
# secret every XR carries, and the observed Usages, don't affect the keys.
# The cases compare keys rather than whole responses because every cloud's
# rendered half would restate its stack data, much of it generated, where
# COMPOSE_CASES already covers how components render.
COMPOSED_RESOURCE_KEYS_CASES = [
    ComposedResourceKeysCase(
        name="EKSStandard",
        reason=(
            "On EKS, the Standard stack composes the gateway, the common components, EKS's generated half "
            "and the Standard stack's own components, each under its pinned key."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="EKS",
                    stack="Standard",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
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
                    "cluster-agent": _observed_ready(),
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
                    "usage-gateway-namespace-by-cluster-agent": _observed_ready(),
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
            "cluster-agent",
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
            "usage-gateway-namespace-by-cluster-agent",
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
        name="EKSDynamo",
        reason=(
            "On EKS, the Dynamo stack composes the gateway, the common components, EKS's generated half "
            "and the Dynamo stack's own components, each under its pinned key."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="EKS",
                    stack="Dynamo",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
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
                    "cluster-agent": _observed_ready(),
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
                    "usage-gateway-namespace-by-cluster-agent": _observed_ready(),
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
            "cluster-agent",
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
            "usage-gateway-namespace-by-cluster-agent",
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
        name="AKSStandard",
        reason=(
            "On AKS, the Standard stack composes the gateway, the common components, AKS's generated half "
            "and the Standard stack's own components, each under its pinned key."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="AKS",
                    stack="Standard",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
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
                    "cluster-agent": _observed_ready(),
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
                    "usage-gateway-namespace-by-cluster-agent": _observed_ready(),
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
            "cluster-agent",
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
            "usage-gateway-namespace-by-cluster-agent",
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
        name="AKSDynamo",
        reason=(
            "On AKS, the Dynamo stack composes the gateway, the common components, AKS's generated half "
            "and the Dynamo stack's own components, each under its pinned key."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="AKS",
                    stack="Dynamo",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
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
                    "cluster-agent": _observed_ready(),
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
                    "usage-gateway-namespace-by-cluster-agent": _observed_ready(),
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
            "cluster-agent",
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
            "usage-gateway-namespace-by-cluster-agent",
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
        name="GKEStandard",
        reason=(
            "On GKE, the Standard stack composes the gateway, the common components, GKE's generated half "
            "and the Standard stack's own components, each under its pinned key."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="GKE",
                    stack="Standard",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
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
                    "cluster-agent": _observed_ready(),
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
                    "usage-gateway-namespace-by-cluster-agent": _observed_ready(),
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
            "cluster-agent",
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
            "usage-gateway-namespace-by-cluster-agent",
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
        name="GKEDynamo",
        reason=(
            "On GKE, the Dynamo stack composes the gateway, the common components, GKE's generated half "
            "and the Dynamo stack's own components, each under its pinned key."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="GKE",
                    stack="Dynamo",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
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
                    "cluster-agent": _observed_ready(),
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
                    "usage-gateway-namespace-by-cluster-agent": _observed_ready(),
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
            "cluster-agent",
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
            "usage-gateway-namespace-by-cluster-agent",
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
        name="NebiusStandard",
        reason=(
            "On Nebius, the Standard stack composes the gateway, the common components, Nebius's hand-written half "
            "and the Standard stack's own components, each under its pinned key."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Nebius",
                    stack="Standard",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
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
                    "cluster-agent": _observed_ready(),
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
                    "usage-gateway-namespace-by-cluster-agent": _observed_ready(),
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
            "cluster-agent",
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
            "usage-gateway-namespace-by-cluster-agent",
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
        name="NebiusDynamo",
        reason=(
            "On Nebius, the Dynamo stack composes the gateway, the common components, Nebius's hand-written half "
            "and the Dynamo stack's own components, each under its pinned key."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Nebius",
                    stack="Dynamo",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
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
                    "cluster-agent": _observed_ready(),
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
                    "usage-gateway-namespace-by-cluster-agent": _observed_ready(),
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
            "cluster-agent",
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
            "usage-gateway-namespace-by-cluster-agent",
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
        name="VultrStandard",
        reason=(
            "On Vultr, the Standard stack composes the gateway, the common components, Vultr's hand-written half "
            "and the Standard stack's own components, each under its pinned key."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Vultr",
                    stack="Standard",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
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
                    "cluster-agent": _observed_ready(),
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
                    "usage-gateway-namespace-by-cluster-agent": _observed_ready(),
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
            "cluster-agent",
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
            "usage-gateway-namespace-by-cluster-agent",
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
        name="VultrDynamo",
        reason=(
            "On Vultr, the Dynamo stack composes the gateway, the common components, Vultr's hand-written half "
            "and the Dynamo stack's own components, each under its pinned key."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Vultr",
                    stack="Dynamo",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
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
                    "cluster-agent": _observed_ready(),
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
                    "usage-gateway-namespace-by-cluster-agent": _observed_ready(),
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
            "cluster-agent",
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
            "usage-gateway-namespace-by-cluster-agent",
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
        name="CivoStandard",
        reason=(
            "On Civo, the Standard stack composes the gateway, the common components, Civo's hand-written half "
            "and the Standard stack's own components, each under its pinned key."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Civo",
                    stack="Standard",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
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
                    "cluster-agent": _observed_ready(),
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
                    "usage-gateway-namespace-by-cluster-agent": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _observed_ready(),
                    "cert-manager": _observed_ready(),
                    "kube-prometheus-stack": _observed_ready(),
                    "node-feature-discovery": _observed_ready(),
                    "nvidia-dra-driver-gpu": _observed_ready(),
                    "gpu-operator": _observed_ready(),
                    "usage-cert-manager-by-gpu-operator": _observed_ready(),
                    "usage-node-feature-discovery-by-gpu-operator": _observed_ready(),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _observed_ready(),
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
            "cluster-agent",
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
            "usage-gateway-namespace-by-cluster-agent",
            "usage-gateway-namespace-by-gateway-selfsigned-issuer",
            "usage-gateway-selfsigned-issuer-by-trust-manager",
            # The hand-written Civo half. Civo pre-installs no GPU stack at all,
            # so it's the one that carries the GPU Operator, driver included,
            # with the Usages for its depends_on edges and the DRA driver's edge
            # onto it.
            "cert-manager",
            "kube-prometheus-stack",
            "node-feature-discovery",
            "nvidia-dra-driver-gpu",
            "gpu-operator",
            "usage-cert-manager-by-gpu-operator",
            "usage-node-feature-discovery-by-gpu-operator",
            "usage-gpu-operator-by-nvidia-dra-driver-gpu",
            # The Standard stack.
            "leader-worker-set",
        },
    ),
    ComposedResourceKeysCase(
        name="CivoDynamo",
        reason=(
            "On Civo, the Dynamo stack composes the gateway, the common components, Civo's hand-written half "
            "and the Dynamo stack's own components, each under its pinned key."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Civo",
                    stack="Dynamo",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
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
                    "cluster-agent": _observed_ready(),
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
                    "usage-gateway-namespace-by-cluster-agent": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _observed_ready(),
                    "cert-manager": _observed_ready(),
                    "kube-prometheus-stack": _observed_ready(),
                    "node-feature-discovery": _observed_ready(),
                    "nvidia-dra-driver-gpu": _observed_ready(),
                    "gpu-operator": _observed_ready(),
                    "usage-cert-manager-by-gpu-operator": _observed_ready(),
                    "usage-node-feature-discovery-by-gpu-operator": _observed_ready(),
                    "usage-gpu-operator-by-nvidia-dra-driver-gpu": _observed_ready(),
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
            "cluster-agent",
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
            "usage-gateway-namespace-by-cluster-agent",
            "usage-gateway-namespace-by-gateway-selfsigned-issuer",
            "usage-gateway-selfsigned-issuer-by-trust-manager",
            # The hand-written Civo half. Civo pre-installs no GPU stack at all,
            # so it's the one that carries the GPU Operator, driver included,
            # with the Usages for its depends_on edges and the DRA driver's edge
            # onto it.
            "cert-manager",
            "kube-prometheus-stack",
            "node-feature-discovery",
            "nvidia-dra-driver-gpu",
            "gpu-operator",
            "usage-cert-manager-by-gpu-operator",
            "usage-node-feature-discovery-by-gpu-operator",
            "usage-gpu-operator-by-nvidia-dra-driver-gpu",
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
        name="ExistingStandard",
        reason=(
            "On Existing, the Standard stack composes the gateway, the common components, the hand-written half "
            "and the Standard stack's own components, each under its pinned key."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Existing",
                    stack="Standard",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
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
                    "cluster-agent": _observed_ready(),
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
                    "usage-gateway-namespace-by-cluster-agent": _observed_ready(),
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
            "cluster-agent",
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
            "usage-gateway-namespace-by-cluster-agent",
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
        name="ExistingDynamo",
        reason=(
            "On Existing, the Dynamo stack composes the gateway, the common components, the hand-written half "
            "and the Dynamo stack's own components, each under its pinned key."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Existing",
                    stack="Dynamo",
                    dra_drivers=["gpu.nvidia.com"],
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
                    gpu=None,
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
                    "cluster-agent": _observed_ready(),
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
                    "usage-gateway-namespace-by-cluster-agent": _observed_ready(),
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
            "cluster-agent",
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
            "usage-gateway-namespace-by-cluster-agent",
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
    ComposedResourceKeysCase(
        name="ExistingStandardCPUOnly",
        reason=(
            "With draDrivers [dra.cpu], only the CPU device stack joins the untagged components: "
            "dra-driver-cpu and its critical-pods quota install, and the NVIDIA and AMD stacks - "
            "NFD included - stay off the cluster."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Existing",
                    stack="Standard",
                    dra_drivers=["dra.cpu"],
                    secrets=[
                        v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
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
                    gpu=None,
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
                    "cluster-agent": _observed_ready(),
                    "dra-driver-cpu": _observed_ready(),
                    "dra-driver-cpu-critical-pods-quota": _observed_ready(),
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
                    "usage-gateway-namespace-by-cluster-agent": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _observed_ready(),
                    "cert-manager": _observed_ready(),
                    "kube-prometheus-stack": _observed_ready(),
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
            # The common components, including the dra.cpu-tagged device
            # stack this cluster's classes claim.
            "ai-gateway",
            "ai-gateway-crds",
            "cluster-agent",
            "dra-driver-cpu",
            "dra-driver-cpu-critical-pods-quota",
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
            "usage-gateway-namespace-by-cluster-agent",
            "usage-gateway-namespace-by-gateway-selfsigned-issuer",
            "usage-gateway-selfsigned-issuer-by-trust-manager",
            # The hand-written Existing half, less its GPU stacks.
            "cert-manager",
            "kube-prometheus-stack",
            # The Standard stack.
            "leader-worker-set",
        },
    ),
    ComposedResourceKeysCase(
        name="ExistingStandardAbsent",
        reason=(
            "With draDrivers absent - the cluster composition always sets it, so absent means the "
            "same as empty - every device stack filters out, NVIDIA, AMD and CPU alike, and only "
            "the untagged components install."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Existing",
                    stack="Standard",
                    dra_drivers=None,
                    secrets=[
                        v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
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
                    gpu=None,
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
                    "cluster-agent": _observed_ready(),
                    "amd-gpu-operator": _observed_ready(),
                    "dra-driver-cpu": _observed_ready(),
                    "dra-driver-cpu-critical-pods-quota": _observed_ready(),
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
                    "usage-cert-manager-by-amd-gpu-operator": _observed_ready(),
                    "usage-cert-manager-by-envoy-gateway": _observed_ready(),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-proxy": _observed_ready(),
                    "usage-gateway-namespace-by-cluster-agent": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _observed_ready(),
                    "usage-node-feature-discovery-by-amd-gpu-operator": _observed_ready(),
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
            # The common components, less every tagged device stack.
            "ai-gateway",
            "ai-gateway-crds",
            "cluster-agent",
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
            "usage-gateway-namespace-by-cluster-agent",
            "usage-gateway-namespace-by-gateway-selfsigned-issuer",
            "usage-gateway-selfsigned-issuer-by-trust-manager",
            # The hand-written Existing half, less its GPU stacks.
            "cert-manager",
            "kube-prometheus-stack",
            # The Standard stack.
            "leader-worker-set",
        },
    ),
    # The other side of the filter from the Absent case: every stack the
    # Existing list carries is claimed, the way compose-inference-cluster
    # stamps it for a cluster whose classes claim NVIDIA, AMD and CPU
    # devices. This is the one golden with the AMD half desired, so a key
    # regression there - the operator, its usages, or the exporter config
    # that turns profiler metrics on - lands here.
    ComposedResourceKeysCase(
        name="ExistingStandardAllDrivers",
        reason=(
            "With every driver the Existing list carries claimed, each device stack joins the "
            "untagged components under its pinned key: NVIDIA's, AMD's - exporter config "
            "included - and the CPU driver's."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(
                    cloud="Existing",
                    stack="Standard",
                    dra_drivers=["dra.cpu", "gpu.amd.com", "gpu.nvidia.com"],
                    secrets=[
                        v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
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
                    gpu=None,
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
                    "cluster-agent": _observed_ready(),
                    "amd-gpu-operator": _observed_ready(),
                    "amd-metrics-exporter-config": _observed_ready(),
                    "dra-driver-cpu": _observed_ready(),
                    "dra-driver-cpu-critical-pods-quota": _observed_ready(),
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
                    "usage-cert-manager-by-amd-gpu-operator": _observed_ready(),
                    "usage-cert-manager-by-envoy-gateway": _observed_ready(),
                    "usage-cert-manager-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-proxy": _observed_ready(),
                    "usage-gateway-namespace-by-cluster-agent": _observed_ready(),
                    "usage-gateway-namespace-by-gateway-selfsigned-issuer": _observed_ready(),
                    "usage-gateway-selfsigned-issuer-by-trust-manager": _observed_ready(),
                    "usage-node-feature-discovery-by-amd-gpu-operator": _observed_ready(),
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
            # The common components, every device stack claimed.
            "ai-gateway",
            "ai-gateway-crds",
            "cluster-agent",
            "dra-driver-cpu",
            "dra-driver-cpu-critical-pods-quota",
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
            "usage-gateway-namespace-by-cluster-agent",
            "usage-gateway-namespace-by-gateway-selfsigned-issuer",
            "usage-gateway-selfsigned-issuer-by-trust-manager",
            # The hand-written Existing half, both GPU stacks included.
            "amd-gpu-operator",
            "amd-metrics-exporter-config",
            "cert-manager",
            "kube-prometheus-stack",
            "node-feature-discovery",
            "nvidia-dra-driver-gpu",
            "usage-cert-manager-by-amd-gpu-operator",
            "usage-node-feature-discovery-by-amd-gpu-operator",
            # The Standard stack.
            "leader-worker-set",
        },
    ),
]


@pytest.mark.parametrize("case", COMPOSED_RESOURCE_KEYS_CASES, ids=lambda case: case.name)
def test_composed_resource_keys(case: ComposedResourceKeysCase) -> None:
    """RunFunction composes exactly the pinned resource keys for a cloud and stack."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert set(got.desired.resources) == case.want, case.reason


# These call fn._cluster_name rather than RunFunction. Through RunFunction the
# name surfaces only in the collector config's resource/cluster processor, and
# reaching that takes a TelemetryDestination and a whole stack's response.
CLUSTER_NAME_CASES = [
    ClusterNameCase(
        name="CompositeLabel",
        reason="A stack Crossplane labels with its composite is named for the composite an operator named.",
        xr=v1alpha1.ServingStack(
            metadata=metav1.ObjectMeta(name="local-serving-stack-d4206", labels={"crossplane.io/composite": "local"}),
            spec=v1alpha1.Spec(
                cloud="Existing",
                secrets=[v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig")],
                gateway=v1alpha1.Gateway(hostname="test-backend.gateways.example.com"),
            ),
        ),
        want="local",
    ),
    # Better a generated name on the series than none at all.
    ClusterNameCase(
        name="NoCompositeLabel",
        reason="A stack with no composite label is named for itself, generated suffix and all.",
        xr=v1alpha1.ServingStack(
            metadata=metav1.ObjectMeta(name="local-serving-stack-d4206"),
            spec=v1alpha1.Spec(
                cloud="Existing",
                secrets=[v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig")],
                gateway=v1alpha1.Gateway(hostname="test-backend.gateways.example.com"),
            ),
        ),
        want="local-serving-stack-d4206",
    ),
]


@pytest.mark.parametrize("case", CLUSTER_NAME_CASES, ids=lambda case: case.name)
def test_cluster_name(case: ClusterNameCase) -> None:
    """The cluster every exported series is stamped with is named as its operator named it."""
    assert fn._cluster_name(case.xr) == case.want, case.reason
