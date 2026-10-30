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

"""The cloud half of the stack for BYO (source: Existing) clusters.

`Existing` isn't a cloud at all, so no generator can resolve it:
Modelplane can only say what it adds to whatever is already there, and
pins that by hand in the same shape a generator emits. Where a
component also appears on a generated cloud, this file states the same
pin, so one review moves both halves when a version changes -
regenerate, then mirror the shared pins here.

Existing is also the open-ended cloud for devices: a claimed driver
this list doesn't tag installs nothing and rejects nothing - join()'s
docstring states that rule. The list still pins device stacks like
any cloud's, each tagged with its driver so it installs
only when a class claims it: NVIDIA's today, and AMD's - Existing is
the one cloud that installs it, for BYO nodes whose images already
carry the amdgpu kernel driver.
"""

from function.stacks.components import Chart, Component, Manifests

# DRA drivers this cloud supports that arrive outside the serving
# stack. None are declared: join() treats every driver this list
# doesn't tag as externally installed on Existing.
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
            # accident rather than by intent. Scope it to the inference pool
            # taints - a BYO pool may carry any of the three - mirroring the
            # generated clouds (TOLERATIONS in generate.py); as a DaemonSet it
            # still reaches every untainted node.
            "prometheus-node-exporter": {
                "tolerations": [
                    {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                    {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
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
        # amd-gpu-operator below depends on this chart, so its Ready must
        # mean healthy, not just deployed.
        wait=True,
        # NFD exists here to label GPU nodes for the DRA drivers, so it
        # carries both GPU stacks' drivers - NVIDIA's kubelet plugin and
        # AMD's operator both find their nodes by NFD labels - and stays
        # off clusters whose classes claim no GPUs.
        dra_drivers=["gpu.nvidia.com", "gpu.amd.com"],
        # The worker must run on the very nodes it is supposed to label:
        # each DRA driver's kubelet plugin schedules only onto nodes
        # carrying an NFD GPU label (feature.node.kubernetes.io/pci-10de
        # and friends for NVIDIA). A BYO GPU node may be tainted
        # nvidia.com/gpu or amd.com/gpu, and the NFD chart's worker
        # tolerates nothing by default. Without these tolerations the
        # chain breaks silently: no worker on the GPU node, no GPU
        # label, no kubelet plugin, no ResourceSlices, and every GPU
        # ResourceClaim stays unallocatable with all components looking
        # healthy. Exists matches every taint value.
        values={
            "worker": {
                "tolerations": [
                    {
                        "key": "nvidia.com/gpu",
                        "operator": "Exists",
                        "effect": "NoSchedule",
                    },
                    {
                        "key": "amd.com/gpu",
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
    # The metrics exporter's config, referenced from the DeviceConfig
    # below. It exists to turn ProfilerMetrics on: GPU_PROF_* fields ship
    # disabled, and tensor activity (MFMA utilization) is one of them -
    # without it modelplane_gpu_tensor_active_ratio is NVIDIA-only. The
    # hardware runs one profiler instance at a time, so this takes the
    # slot application-level profiling would use on these nodes; turn it
    # off here to profile. An absent Fields list keeps the exporter's
    # default metrics unchanged. Lands in the namespace the chart's
    # release creates; provider-kubernetes retries until it exists, like
    # the critical-pods quotas in common.py.
    Manifests(
        key="amd-metrics-exporter-config",
        dra_drivers=["gpu.amd.com"],
        manifests=[
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": "mp-metrics-exporter-config",
                    "namespace": "kube-amd-gpu",
                },
                "data": {
                    "config.json": '{"GPUConfig": {"ProfilerMetrics": {"all": true}}}',
                },
            },
        ],
    ),
    # The AMD GPU operator, in DRA mode: the default DeviceConfig the
    # chart renders (crds.defaultCR) is set below to enable the DRA
    # driver and disable the device plugin, the reverse of the chart's
    # defaults - the two are mutually exclusive allocators - so the
    # gpu.amd.com DeviceClass the chart registers is the sole path to the
    # GPUs. Existing is the one cloud that installs it today.
    #
    # The default DeviceConfig tracks the release only with
    # crds.defaultCR.upgrade: the chart renders it on install, marks it
    # helm.sh/resource-policy: keep, and skips it on upgrade by default,
    # so without the flag a value change below would upgrade the release
    # and leave the live DeviceConfig at its install-time spec.
    #
    # The chart pin pins the operator's own images but not the
    # DeviceConfig's, whose DRA driver, node labeller, metrics exporter
    # and utility images default to floating tags - so each is pinned
    # here. The DRA driver is the component the claims go through; on a
    # floating tag any pod reschedule could change it under a running
    # cluster. The exporter's field names are what the modelplane-gpu-amd
    # MetricMapping reads, so it is pinned to the operator's release.
    #
    # Driver management is off: a BYO node's image must already carry
    # the amdgpu kernel driver, and the chart's in-cluster driver build
    # needs a push registry configured (driver.image defaults to a
    # docker.io placeholder), without which KMM loops on a driver image
    # it can never resolve and the DRA driver - gated on the KMM-ready
    # node label - never schedules. KMM must stay installed even with
    # the driver unmanaged: the operator watches KMM's kinds at startup
    # no matter what, and crashes without their CRDs, taking every
    # operand down with it.
    #
    # The node labeller stays on for scheduling labels; NFD is the
    # standalone instance above, so the chart's bundled one is off and
    # two instances never race over the same label namespace. Every
    # per-node component tolerates the amd.com/gpu taint AMD GPU pools
    # carry - the chart defaults tolerate nothing, which strands the
    # daemonsets off the very nodes they serve.
    Chart(
        key="amd-gpu-operator",
        release="mp-gpu-operator-charts",
        namespace="kube-amd-gpu",
        chart="gpu-operator-charts",
        repository="https://rocm.github.io/gpu-operator",
        version="v1.5.1",
        depends_on=["cert-manager", "node-feature-discovery"],
        dra_drivers=["gpu.amd.com"],
        values={
            "node-feature-discovery": {"enabled": False},
            # Argo-based node remediation pulls a workflow controller the
            # serving stack doesn't need.
            "remediation": {"enabled": False, "installCRDs": False},
            "crds": {"defaultCR": {"upgrade": True}},
            "deviceConfig": {
                "spec": {
                    "commonConfig": {
                        "utilsContainer": {"image": "docker.io/rocm/gpu-operator-utils:v1.5.1"},
                    },
                    "driver": {"enable": False},
                    "devicePlugin": {
                        "enableDevicePlugin": False,
                        "nodeLabellerImage": "docker.io/rocm/k8s-device-plugin:labeller-1.31.0.11",
                        "nodeLabellerTolerations": [
                            {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                        ],
                    },
                    "metricsExporter": {
                        "image": "docker.io/rocm/device-metrics-exporter:v1.5.1",
                        # The ConfigMap above: profiler metrics on, for
                        # the tensor-activity mapping.
                        "config": {"name": "mp-metrics-exporter-config"},
                        "tolerations": [
                            {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                        ],
                    },
                    "draDriver": {
                        "enable": True,
                        "image": "docker.io/rocm/k8s-gpu-dra-driver:v1.0.1",
                        "tolerations": [
                            {"key": "amd.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                        ],
                    },
                },
            },
        },
    ),
]
