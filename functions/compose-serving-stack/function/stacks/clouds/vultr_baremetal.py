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

"""The cloud half of the stack for Vultr bare metal (k3s).

No generator covers this cloud, so Modelplane pins it by hand, in the
same shape a generator emits. Where a component also appears on another
cloud, this file states the same pin, so one review moves both.

Unlike managed clusters, no node image preinstalls a Kubernetes GPU
stack on bare metal: the operators and DRA drivers all install here.
The kernel driver differs by vendor: Vultr's AMD Instinct images ship
with the amdgpu driver preinstalled, while the NVIDIA operator installs
its own. The GPU components are vendor-tagged - Vultr sells both AMD
Instinct and NVIDIA bare metal GPU plans - and the join filters them to
the vendors the InferenceClasses actually name, so an AMD cluster
doesn't run NVIDIA machinery and vice versa.

One node-feature-discovery serves both vendors: the AMD operator's NFD
subchart and the NVIDIA operator's are disabled so two instances never
race over the same label namespace. The AMD chart's default
NodeFeatureRule (feature.node.kubernetes.io/amd-gpu) still installs and
is honoured by the standalone NFD master.
"""

from function.stacks.components import Chart, Component

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
        values={"crds": {"enabled": True, "keep": False}},
    ),
    Chart(
        key="kube-prometheus-stack",
        release="mp-kube-prometheus-stack",
        namespace="monitoring",
        chart="kube-prometheus-stack",
        repository="https://prometheus-community.github.io/helm-charts",
        version="84.4.0",
        # gpu-operator below depends on this chart, so its Ready must
        # mean healthy, not just deployed.
        wait=True,
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
    # One NFD instance for both GPU vendors' operators, which both have
    # their bundled NFD disabled. Same pin as the generated clouds. The
    # worker must run on the very GPU nodes it labels, which the cluster
    # composition taints by vendor, so it tolerates both vendors' taints
    # - and only those, so it doesn't squat on other tainted capacity.
    Chart(
        key="node-feature-discovery",
        release="mp-node-feature-discovery",
        namespace="node-feature-discovery",
        chart="node-feature-discovery",
        repository="https://kubernetes-sigs.github.io/node-feature-discovery/charts",
        version="0.19.0",
        wait=True,
        values={
            "gc": {"enable": True},
            "master": {"enable": True},
            "topologyUpdater": {
                "createCRDs": True,
                "enable": False,
                "kubeletStateDir": "",
                "resources": {"limits": {"memory": "256Mi"}, "requests": {"cpu": "50m", "memory": "128Mi"}},
            },
            "worker": {
                "enable": True,
                "tolerations": [
                    {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                    {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                ],
            },
        },
    ),
    # The AMD GPU operator, in DRA mode: the chart's default DeviceConfig
    # (crds.defaultCR) enables the DRA driver and disables the device
    # plugin - the two are mutually exclusive allocators - so the
    # gpu.amd.com DeviceClass the chart registers is the sole path to the
    # GPUs. Driver management (KMM) stays off: Vultr's bare metal GPU
    # images ship with the amdgpu driver preinstalled, and the chart's
    # in-cluster driver build needs a push registry configured
    # (driver.image defaults to a docker.io/myUserName placeholder),
    # without which KMM loops on a driver image it can never resolve and
    # the DRA driver - gated on the KMM-ready node label - never
    # schedules. The node labeller stays on for scheduling labels; NFD is
    # the standalone instance above. Every per-node component tolerates
    # the amd.com/gpu taint the GPU pool carries - the chart defaults
    # tolerate nothing, which strands the daemonsets off the very nodes
    # they serve.
    Chart(
        key="amd-gpu-operator",
        release="mp-gpu-operator-charts",
        namespace="kube-amd-gpu",
        chart="gpu-operator-charts",
        repository="https://rocm.github.io/gpu-operator",
        version="v1.5.1",
        wait=True,
        depends_on=["cert-manager", "node-feature-discovery"],
        accelerator_vendor="AMD",
        values={
            "node-feature-discovery": {"enabled": False},
            # Argo-based node remediation pulls a workflow controller the
            # serving stack doesn't need.
            "remediation": {"enabled": False, "installCRDs": False},
            "deviceConfig": {
                "spec": {
                    "driver": {"enable": False},
                    "devicePlugin": {
                        "enableDevicePlugin": False,
                        "nodeLabellerTolerations": [
                            {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                        ],
                    },
                    "metricsExporter": {
                        "tolerations": [
                            {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                        ],
                    },
                    "draDriver": {
                        "enable": True,
                        "tolerations": [
                            {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                        ],
                    },
                },
            },
        },
    ),
    # The NVIDIA GPU operator. Bare metal deltas from the managed-cloud
    # pin: the driver and container toolkit install here (no node image
    # provides them), and the toolkit is pointed at k3s's containerd,
    # which lives off the stock paths. The device plugin stays off: the
    # DRA driver below is the sole allocator.
    Chart(
        key="gpu-operator",
        release="mp-gpu-operator",
        namespace="gpu-operator",
        chart="gpu-operator",
        repository="https://helm.ngc.nvidia.com/nvidia",
        version="v26.3.3",
        wait=True,
        depends_on=["cert-manager", "node-feature-discovery", "kube-prometheus-stack"],
        accelerator_vendor="NVIDIA",
        values={
            "ccManager": {"enabled": False},
            "daemonsets": {
                "tolerations": [
                    {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                ],
            },
            "dcgm": {"enabled": True},
            "devicePlugin": {"enabled": False},
            "driver": {
                "enabled": True,
                "maxParallelUpgrades": 5,
                "rdma": {"enabled": False},
                "useOpenKernelModules": True,
                "version": "580.173.02",
            },
            "fullnameOverride": "gpu-operator",
            "gdrcopy": {"enabled": False},
            "gfd": {"enabled": True},
            "hostPaths": {"driverInstallDir": "/run/nvidia/driver"},
            "kataSandboxDevicePlugin": {"enabled": False},
            "migManager": {"enabled": False},
            "nfd": {"enabled": False},
            "toolkit": {
                "enabled": True,
                "env": [
                    {
                        "name": "CONTAINERD_CONFIG",
                        "value": "/var/lib/rancher/k3s/agent/etc/containerd/config.toml.tmpl",
                    },
                    {"name": "CONTAINERD_SOCKET", "value": "/run/k3s/containerd/containerd.sock"},
                    {"name": "CONTAINERD_RUNTIME_CLASS", "value": "nvidia"},
                    {"name": "CONTAINERD_SET_AS_DEFAULT", "value": "true"},
                ],
            },
            "validator": {"plugin": {"env": [{"name": "WITH_WORKLOAD", "value": "false"}]}},
        },
    ),
    # Publishes each GPU node's devices as DRA ResourceSlices and
    # registers the gpu.nvidia.com DeviceClass. The driver root points
    # into the operator's install dir, not / as on managed clouds where
    # the node image provides the driver.
    Chart(
        key="nvidia-dra-driver-gpu",
        release="mp-dra-driver-nvidia-gpu",
        namespace="nvidia-dra-driver",
        chart="dra-driver-nvidia-gpu",
        repository="oci://registry.k8s.io/dra-driver-nvidia/charts",
        version="0.4.1",
        depends_on=["gpu-operator"],
        accelerator_vendor="NVIDIA",
        values={
            "gpuResourcesEnabledOverride": True,
            "nvidiaDriverRoot": "/run/nvidia/driver",
            "resources": {"computeDomains": {"enabled": False}},
        },
    ),
]
