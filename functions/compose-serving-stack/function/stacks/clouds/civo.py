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

"""The cloud half of the stack for Civo.

No generator covers Civo, so Modelplane pins the cloud half by hand,
in the same shape a generator emits. Where a component also appears on
a generated cloud, this file states the same pin, so one review moves
both halves.

Civo pre-installs no GPU stack: its GPU node images ship the NVIDIA
container toolkit but no driver, and there is no managed GPU Operator
add-on (unlike Vultr's). So this is the one hand-written cloud that
installs the GPU Operator itself: driver enabled (the operator's
containerized driver installs to /run/nvidia/driver), toolkit disabled
(baked into the node image), and device plugin disabled so its
nvidia.com/gpu ledger never books the same physical devices the DRA
driver's ResourceSlices allocate - the DRA driver stays the sole GPU
allocator. compose-civo-cluster composes no GPU readiness gate (the
operator it would observe is installed here, after the cluster XR is
Ready - gating would deadlock), so the operator chart's wait=True and
the DRA driver's depends_on are what keep GPU health on the critical
path.
"""

import copy
from dataclasses import replace

from function.stacks.components import Chart, Component, Manifests

# The pool label compose-civo-cluster writes on every node pool's nodes.
# with_nvlink_disabled selects a pool's nodes by it, so a per-pool
# NVIDIADriver claims exactly that pool away from the default driver.
_LABEL_POOL = "modelplane.ai/pool"

# ConfigMap the per-pool NVIDIADrivers read kernel module options from.
_KERNEL_MODULE_CONFIGMAP = "nvidia-kernel-config"

# Component keys with_nvlink_disabled adds to the joined list. Driver
# entries are per pool, suffixed with the pool name.
_NVLINK_CONFIG_KEY = "nvlink-disable-config"
_NVLINK_DRIVER_KEY_PREFIX = "nvlink-disabled-driver"


def with_nvlink_disabled(components: list[Component], pools: list[str]) -> list[Component]:
    """The component list with the given pools' GPU driver loading NVLink
    disabled, leaving every other pool's driver untouched.

    Civo's single-H100 sizes are lone H100 SXM modules whose NVLink links
    have no peer: link bring-up fails and the driver never becomes ready
    unless loaded with NVreg_NvLinkDisable=1 (per Civo's GPU configuration
    docs, validated against GPU Operator v25.10.1). The parameter must not
    reach multi-GPU pools, whose GPUs NVLink interconnects, so the
    gpu-operator chart switches to NVIDIADriver-managed drivers: the
    chart's default NVIDIADriver (deployDefaultCR) keeps driving every GPU
    node not claimed by a more specific instance, and one NVIDIADriver per
    listed pool claims that pool's nodes - same driver pin as the chart's,
    plus the kernel module ConfigMap carrying the parameter.

    The per-pool NVIDIADrivers gate on the gpu-operator release (their CRD
    ships with it) and the DRA driver gates on them, so GPU health stays
    on the critical path: a pool whose driver never loads holds its
    NVIDIADriver's status.state off ready, which holds back the DRA
    driver exactly like an unhealthy operator does today.
    """
    driver_keys = [f"{_NVLINK_DRIVER_KEY_PREFIX}-{pool}" for pool in pools]

    def transformed(c: Component) -> Component:
        if isinstance(c, Chart) and c.key == "gpu-operator":
            values = copy.deepcopy(c.values or {})
            # deployDefaultCR renders the chart's driver block below as the
            # default (fallback) NVIDIADriver, so unlisted pools keep the
            # exact configuration they get in ClusterPolicy mode.
            values["driver"]["nvidiaDriverCRD"] = {"enabled": True, "deployDefaultCR": True}
            return replace(c, values=values)
        if isinstance(c, Chart) and c.key == "nvidia-dra-driver-gpu":
            return replace(c, depends_on=[*c.depends_on, *driver_keys])
        return c

    out: list[Component] = [transformed(c) for c in components]
    out.append(
        Manifests(
            key=_NVLINK_CONFIG_KEY,
            # The namespace is created here rather than by the gpu-operator
            # release: the ConfigMap carries no dependency edge, so it can
            # apply before the chart.
            manifests=[
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": "gpu-operator"},
                },
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": _KERNEL_MODULE_CONFIGMAP,
                        "namespace": "gpu-operator",
                    },
                    "data": {"nvidia.conf": "options nvidia NVreg_NvLinkDisable=1"},
                },
            ],
        ),
    )
    for pool, key in zip(pools, driver_keys, strict=True):
        out.append(
            Manifests(
                key=key,
                depends_on=["gpu-operator", _NVLINK_CONFIG_KEY],
                # Ready only once the operator reports the pool's driver
                # rolled out, so the DRA driver's install gate orders on
                # driver health, not CR admission.
                ready='object.status.state == "ready"',
                manifests=[
                    {
                        "apiVersion": "nvidia.com/v1alpha1",
                        "kind": "NVIDIADriver",
                        "metadata": {"name": f"nvlink-disabled-{pool}"},
                        "spec": {
                            "driverType": "gpu",
                            # Driver pin and module flavor mirror the chart's
                            # driver block below; one review moves both.
                            "version": "580.173.02",
                            "useOpenKernelModules": True,
                            "nodeSelector": {_LABEL_POOL: pool},
                            "kernelModuleConfig": {"name": _KERNEL_MODULE_CONFIGMAP},
                            "tolerations": [
                                {
                                    "key": "nvidia.com/gpu",
                                    "operator": "Exists",
                                    "effect": "NoSchedule",
                                },
                            ],
                        },
                    },
                ],
            ),
        )
    return out


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
        # gpu-operator below depends on this chart: the DCGM exporter's
        # ServiceMonitor needs the Prometheus CRDs, which this release
        # carries itself (its crds subchart stays at its enabled default -
        # no separate prometheus-operator-crds chart, unlike the generated
        # clouds). wait=True makes Ready mean healthy, not just deployed.
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
    Chart(
        key="node-feature-discovery",
        release="mp-node-feature-discovery",
        namespace="node-feature-discovery",
        chart="node-feature-discovery",
        repository="https://kubernetes-sigs.github.io/node-feature-discovery/charts",
        version="0.19.0",
        # gpu-operator below depends on this chart, so its Ready must
        # mean healthy, not just deployed.
        wait=True,
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
    # Installs the NVIDIA driver on Civo's driverless GPU node images and
    # validates the GPU stack. Chart pin mirrors the generated clouds'.
    # wait=True makes Ready mean the operator's validator passed, which
    # the DRA driver below depends on - without a working driver its
    # kubelet plugin would publish no ResourceSlices.
    Chart(
        key="gpu-operator",
        release="mp-gpu-operator",
        namespace="gpu-operator",
        chart="gpu-operator",
        repository="https://helm.ngc.nvidia.com/nvidia",
        version="v26.3.3",
        wait=True,
        # kube-prometheus-stack, because the DCGM exporter's ServiceMonitor
        # needs the Prometheus CRDs that release carries. Without the
        # exporter every modelplane_gpu_* series and
        # modelplane_energy_joules_total is empty on Civo: the collector's
        # modelplane-gpu job would find no pod to scrape.
        depends_on=["node-feature-discovery", "cert-manager", "kube-prometheus-stack"],
        values={
            "ccManager": {"enabled": False},
            "cdi": {"default": True, "enabled": True},
            # The operator's DaemonSets must run on the GPU nodes, which
            # carry the nvidia.com/gpu taint from compose-civo-cluster.
            "daemonsets": {"tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]},
            # Exporter block mirrors the generated clouds' verbatim; one
            # review moves both. The CSV carries every field metrics.py
            # maps, so no modelplane_gpu_* rename lands empty.
            "dcgm": {"enabled": True},
            "dcgmExporter": {
                "config": {
                    "create": True,
                    "data": """# Clocks,,
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
""",
                    "name": "dcgm-exporter",
                },
                "serviceMonitor": {"enabled": True, "honorLabels": True, "interval": "30s"},
            },
            # The DRA driver is the sole GPU allocator: the device
            # plugin's nvidia.com/gpu ledger would book the same physical
            # devices the DRA ResourceSlices allocate.
            "devicePlugin": {"enabled": False},
            # Civo GPU node images ship no driver; the operator's
            # containerized driver installs to /run/nvidia/driver, where
            # the DRA driver's nvidiaDriverRoot points. Driver pin mirrors
            # the generated clouds'. The L40S, A100 and H100 all support
            # the open kernel modules. Single-H100 pools additionally need
            # NVLink disabled at driver load; with_nvlink_disabled above
            # scopes that to the pool when the ServingStack asks for it.
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
            "kataSandboxDevicePlugin": {"enabled": False},
            # No MIG on Civo's GPU shapes (L40S has no MIG; A100 pools are
            # whole-GPU).
            "migManager": {"enabled": False},
            # Standalone NFD above, matching the generated clouds' shape.
            "nfd": {"enabled": False},
            "operator": {
                "resources": {
                    "limits": {"cpu": "500m", "memory": "700Mi"},
                    "requests": {"cpu": "200m", "memory": "300Mi"},
                },
                "tolerations": [],
                "upgradeCRD": True,
            },
            # The NVIDIA container toolkit is baked into Civo's GPU node
            # images.
            "toolkit": {"enabled": False},
            "validator": {"plugin": {"env": [{"name": "WITH_WORKLOAD", "value": "false"}]}},
        },
    ),
    # Publishes each GPU node's devices as DRA ResourceSlices and
    # registers the gpu.nvidia.com DeviceClass ModelReplica
    # ResourceClaims request through. GPU allocation is opt-in;
    # ComputeDomains (multi-node NVLink) is unused and would pull in
    # extra prerequisites. Unlike Vultr and Nebius - whose node images
    # put the driver at the default root (/) - the gpu-operator above
    # installs the containerized driver to /run/nvidia/driver, so
    # nvidiaDriverRoot must point there (the GKE half does the same for
    # its non-default driver root).
    Chart(
        key="nvidia-dra-driver-gpu",
        release="mp-dra-driver-nvidia-gpu",
        namespace="nvidia-dra-driver",
        chart="dra-driver-nvidia-gpu",
        repository="oci://registry.k8s.io/dra-driver-nvidia/charts",
        version="0.4.1",
        depends_on=["gpu-operator"],
        values={
            "gpuResourcesEnabledOverride": True,
            "nvidiaDriverRoot": "/run/nvidia/driver",
            "resources": {"computeDomains": {"enabled": False}},
        },
    ),
]
