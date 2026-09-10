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

Unlike managed clusters, nothing preinstalls a GPU stack on bare metal:
the driver, the operator, and the DRA driver all install here. The GPU
components are vendor-tagged - Vultr sells both AMD Instinct and NVIDIA
bare metal GPU plans - and the join filters them to the vendors the
InferenceClasses actually name, so an AMD cluster doesn't run NVIDIA
machinery and vice versa.

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
    # their bundled NFD disabled. Same pin as the generated clouds.
    Chart(
        key="node-feature-discovery",
        release="mp-node-feature-discovery",
        namespace="node-feature-discovery",
        chart="node-feature-discovery",
        repository="https://kubernetes-sigs.github.io/node-feature-discovery/charts",
        version="0.19.0",
        wait=True,
        values={
            "gc": {"enable": True, "tolerations": [{"operator": "Exists"}]},
            "master": {"enable": True, "tolerations": [{"operator": "Exists"}]},
            "topologyUpdater": {
                "createCRDs": True,
                "enable": False,
                "kubeletStateDir": "",
                "resources": {"limits": {"memory": "256Mi"}, "requests": {"cpu": "50m", "memory": "128Mi"}},
                "tolerations": [{"operator": "Exists"}],
            },
            "worker": {"enable": True, "tolerations": [{"operator": "Exists"}]},
        },
    ),
    # The AMD GPU operator, in DRA mode: the chart's default DeviceConfig
    # (crds.defaultCR) enables the DRA driver and disables the device
    # plugin - the two are mutually exclusive allocators - so the
    # gpu.amd.com DeviceClass the chart registers is the sole path to the
    # GPUs. KMM (a subchart) installs the amdgpu/ROCm driver on the bare
    # Ubuntu nodes; the chart's default driver version tracks a ROCm 7.x
    # release, which MI355X (gfx950) requires. The node labeller stays on
    # for scheduling labels; NFD is the standalone instance above.
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
                    "driver": {"enable": True},
                    "devicePlugin": {"enableDevicePlugin": False},
                    "draDriver": {
                        "enable": True,
                        "tolerations": [{"operator": "Exists"}],
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
            "daemonsets": {"tolerations": [{"operator": "Exists"}]},
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
