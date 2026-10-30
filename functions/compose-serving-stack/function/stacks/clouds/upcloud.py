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

"""The cloud half of the stack for UpCloud.

No generator covers UpCloud, so Modelplane pins the whole cloud half by
hand, in the same shape a generator emits. Where a component also
appears on a generated cloud, this file states the same pin, so one
review moves both halves when a version changes - regenerate, then
mirror the shared pins here. The DCGM exporter's metrics CSV goes one
step further: rather than restating it, this file imports it from the
shared generated/aicr/dcgm.py, as civo.py does.

UpCloud pre-installs the NVIDIA driver and container toolkit in its UKS
GPU node images, at the default root (/), and runs no managed GPU
Operator add-on. So like Nebius this half installs no GPU Operator:
NFD labels the GPU nodes and the DRA driver's kubelet plugin publishes
their devices. With no operator supplying one, the standalone
dcgm-exporter chart exports the GPU fields the collector's
modelplane-gpu job scrapes.
"""

from function.stacks.clouds.generated.aicr.dcgm import DCGM_METRICS_CSV
from function.stacks.components import Chart, Component

# DRA drivers this cloud supports that arrive outside the serving
# stack; the stacks package folds them into its derived
# supported-drivers table.
EXTERNAL_DRIVERS: list[str] = []

COMPONENTS: list[Component] = [
    Chart(
        key="cert-manager",
        release="mp-cert-manager",
        namespace="cert-manager",
        chart="cert-manager",
        repository="https://charts.jetstack.io",
        version="v1.20.2",
        # envoy-gateway in common.py depends on this chart (a cross-half
        # edge), so its Ready must mean healthy, not just deployed.
        wait=True,
        # The chart keeps its CRDs on uninstall by default, which the gateway PKI
        # needs: it composes Certificates and Issuers as provider-kubernetes
        # Objects, and removing their CRDs would stop provider-kubernetes
        # observing them to release their finalizers.
        values={"crds": {"enabled": True}},
    ),
    Chart(
        key="kube-prometheus-stack",
        release="mp-kube-prometheus-stack",
        namespace="monitoring",
        chart="kube-prometheus-stack",
        repository="https://prometheus-community.github.io/helm-charts",
        version="84.4.0",
        values={
            "fullnameOverride": "prometheus",
            "prometheus": {
                "prometheusSpec": {
                    # Discover PodMonitors across all namespaces.
                    "podMonitorSelectorNilUsesHelmValues": False,
                    "podMonitorNamespaceSelector": {},
                    # Scrape Envoy Gateway proxy pods for upstream
                    # request metrics (envoy_cluster_upstream_rq_active):
                    # in-flight requests at the proxy level.
                    "additionalScrapeConfigs": [
                        {
                            "job_name": "envoy-gateway-proxy",
                            "kubernetes_sd_configs": [
                                {
                                    "role": "pod",
                                    "namespaces": {
                                        "names": ["envoy-gateway-system"],
                                    },
                                },
                            ],
                            "relabel_configs": [
                                {
                                    "source_labels": [
                                        "__meta_kubernetes_pod_label_app_kubernetes_io_component",
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
                        },
                    ],
                },
            },
            # The node-exporter chart's default toleration is keyless - it
            # tolerates every taint, landing the pod on tainted nodes by
            # accident rather than by intent. Scope it to the GPU and CPU pool
            # taints, mirroring the generated clouds (TOLERATIONS in
            # generate.py); as a DaemonSet it still reaches every untainted
            # node.
            "prometheus-node-exporter": {
                "tolerations": [
                    {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                    {"key": "modelplane.ai/cpu", "operator": "Exists", "effect": "NoSchedule"},
                ],
            },
            # Disable components we don't need for observability.
            "grafana": {"enabled": False},
            "alertmanager": {"enabled": False},
        },
    ),
    Chart(
        key="node-feature-discovery",
        release="mp-node-feature-discovery",
        namespace="node-feature-discovery",
        chart="node-feature-discovery",
        repository="https://kubernetes-sigs.github.io/node-feature-discovery/charts",
        version="0.19.0",
        # NFD exists here to label GPU nodes for the DRA driver, so it
        # carries the GPU stack's driver and stays off clusters whose
        # classes claim no GPUs.
        dra_drivers=["gpu.nvidia.com"],
        # The worker must run on the very nodes it is supposed to label:
        # the DRA driver's kubelet plugin schedules only onto nodes
        # carrying an NFD GPU label (feature.node.kubernetes.io/pci-10de
        # and friends). The cluster compositions taint GPU nodes with
        # nvidia.com/gpu, and the NFD chart's worker tolerates nothing
        # by default. Without this toleration the chain breaks silently:
        # no worker on the GPU node, no pci-10de label, no kubelet
        # plugin, no ResourceSlices, and every GPU ResourceClaim stays
        # unallocatable with all components looking healthy. Exists
        # matches every taint value.
        values={
            "worker": {
                "tolerations": [
                    {
                        "key": "nvidia.com/gpu",
                        "operator": "Exists",
                        "effect": "NoSchedule",
                    },
                ],
            },
        },
    ),
    # Publishes each GPU node's devices as DRA ResourceSlices and
    # registers the gpu.nvidia.com DeviceClass ModelReplica
    # ResourceClaims request through. GPU allocation is opt-in;
    # ComputeDomains (multi-node NVLink) is unused and would pull in
    # extra prerequisites. nvidiaDriverRoot stays at the chart default
    # (/): UKS GPU node images put the driver at the default root.
    Chart(
        key="nvidia-dra-driver-gpu",
        release="mp-dra-driver-nvidia-gpu",
        namespace="nvidia-dra-driver",
        chart="dra-driver-nvidia-gpu",
        repository="oci://registry.k8s.io/dra-driver-nvidia/charts",
        version="0.5.0",
        dra_drivers=["gpu.nvidia.com"],
        values={
            "gpuResourcesEnabledOverride": True,
            "resources": {"computeDomains": {"enabled": False}},
        },
    ),
    # Exports DCGM fields for the collector's modelplane-gpu job, which
    # keeps pods named .*dcgm.* with a port named metrics. Without this every
    # modelplane_gpu_* series is empty. DCGM runs embedded (the chart
    # default). The counters are the generated clouds' list, not the
    # chart's shorter default, so UpCloud exports the same DCGM fields as
    # EKS, GKE, AKS and Civo. The node terms are the DRA kubelet plugin's
    # PCI terms; the standalone NFD above labels UKS GPU nodes pci-10de.
    # The collector finds the pod itself, so no ServiceMonitor.
    Chart(
        key="dcgm-exporter",
        release="mp-dcgm-exporter",
        namespace="dcgm-exporter",
        chart="dcgm-exporter",
        repository="https://nvidia.github.io/dcgm-exporter/helm-charts",
        version="4.8.4",
        # Exports NVIDIA GPU telemetry, so it follows the GPU stack's
        # driver and stays off clusters whose classes claim no GPUs.
        dra_drivers=["gpu.nvidia.com"],
        values={
            "serviceMonitor": {"enabled": False},
            "customMetrics": DCGM_METRICS_CSV,
            # Attribute each GPU's series to the pod holding it. UpCloud GPUs
            # are allocated through DRA, which the exporter only maps back to
            # pods with this set; the chart adds the RBAC it needs.
            "kubernetesDRA": {"enabled": True},
            "tolerations": [
                {
                    "key": "nvidia.com/gpu",
                    "operator": "Exists",
                    "effect": "NoSchedule",
                },
            ],
            # The first three nodeSelectorTerms of dra-driver-nvidia-gpu
            # v0.5.0's kubeletPlugin affinity. Recheck them when that chart
            # moves.
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {"matchExpressions": [{"key": key, "operator": "In", "values": ["true"]}]}
                            for key in (
                                "feature.node.kubernetes.io/pci-10de.present",
                                "feature.node.kubernetes.io/pci-0302_10de.present",
                                "feature.node.kubernetes.io/pci-0300_10de.present",
                            )
                        ],
                    },
                },
            },
        },
    ),
]
