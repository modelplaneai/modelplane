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

"""The cloud half of the stack for Nebius, which AICR doesn't cover.

Nebius has no AICR `service` value, so Modelplane pins the whole cloud
half by hand, in the same shape the generator emits. Where a component
is AICR's on a generated cloud (all four here), this file states the
same pin Modelplane ships today, so one review moves both halves when a
version changes.
"""

from function.stacks.components import Chart, Component

COMPONENTS: list[Component] = [
    Chart(
        key="cert-manager",
        release="mp-cert-manager",
        namespace="cert-manager",
        chart="cert-manager",
        repository="https://charts.jetstack.io",
        version="v1.17.1",
        values={"crds": {"enabled": True, "keep": False}},
    ),
    Chart(
        key="kube-prometheus-stack",
        release="mp-kube-prometheus-stack",
        namespace="monitoring",
        chart="kube-prometheus-stack",
        repository="https://prometheus-community.github.io/helm-charts",
        version="72.6.2",
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
        repository="oci://registry.k8s.io/nfd/charts",
        version="0.18.3",
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
    # (/): the node image puts the driver at the default root.
    Chart(
        key="nvidia-dra-driver-gpu",
        release="mp-dra-driver-nvidia-gpu",
        # AICR's namespace for this chart; the generated clouds and the
        # critical-pods quota in common.py align on it.
        namespace="nvidia-dra-driver",
        chart="dra-driver-nvidia-gpu",
        repository="oci://registry.k8s.io/dra-driver-nvidia/charts",
        version="0.4.0",
        values={
            "gpuResourcesEnabledOverride": True,
            "resources": {"computeDomains": {"enabled": False}},
        },
    ),
]
