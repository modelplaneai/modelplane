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
both halves. The DCGM exporter's metrics CSV goes one step further:
rather than restating it, this file imports it from the shared
generated/aicr/dcgm.py, which the generator writes only after proving
every generated cloud carries the same CSV.

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

from function.stacks.clouds.generated.aicr.dcgm import DCGM_METRICS_CSV
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
        # gpu-operator below depends on this chart, so its Ready must
        # mean healthy, not just deployed.
        wait=True,
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
        version="v26.7.1",
        wait=True,
        # No kube-prometheus-stack edge, unlike the generated clouds': the
        # DCGM exporter's ServiceMonitor, the one piece that would need
        # the Prometheus CRDs, stays off below. The exporter itself must
        # run: without it every modelplane_gpu_* series and
        # modelplane_energy_joules_total is empty on Civo - the
        # collector's modelplane-gpu job would find no pod to scrape.
        depends_on=["node-feature-discovery", "cert-manager"],
        dra_drivers=["gpu.nvidia.com"],
        values={
            "ccManager": {"enabled": False},
            "cdi": {"default": True, "enabled": True},
            # The operator's DaemonSets must run on the GPU nodes, which
            # carry the nvidia.com/gpu taint from compose-civo-cluster.
            "daemonsets": {"tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]},
            # The CSV is the generator's, imported from the shared
            # generated/aicr/dcgm.py. The ServiceMonitor stays off, unlike
            # the generated clouds': the collector scrapes the exporter
            # pods directly and Civo's Prometheus selects only
            # PodMonitors, so nothing would read it - and off explicitly,
            # so a chart default flip can't render one whose CRD this
            # cluster doesn't carry.
            "dcgm": {"enabled": True},
            "dcgmExporter": {
                "config": {"create": True, "data": DCGM_METRICS_CSV, "name": "dcgm-exporter"},
                "serviceMonitor": {"enabled": False},
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
                "kernelModuleType": "open",
                "maxParallelUpgrades": 5,
                "rdma": {"enabled": False},
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
        version="0.5.0",
        depends_on=["gpu-operator"],
        dra_drivers=["gpu.nvidia.com"],
        values={
            "gpuResourcesEnabledOverride": True,
            "nvidiaDriverRoot": "/run/nvidia/driver",
            "resources": {"computeDomains": {"enabled": False}},
        },
    ),
]
