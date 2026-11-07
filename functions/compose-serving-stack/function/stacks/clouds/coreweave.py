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

"""The cloud half of the stack for CoreWeave.

No generator covers CoreWeave, so Modelplane pins this half by hand.
Where a component also appears on a generated cloud, it uses the same
pin.

CoreWeave runs the NVIDIA driver, a managed GPU Operator and NFD on every
CKS cluster, and its docs say not to install a second GPU Operator. This
half installs none of them.

CoreWeave also installs NVIDIA's DRA driver, but only for ComputeDomains.
This half installs it again with GPU allocation on and ComputeDomains
off.

CoreWeave's own DCGM exporter attributes a GPU's series to a pod only if
the device plugin allocated it. This half installs a second exporter that
maps DRA allocations to pods. Both exporters run side by side without
disturbing each other's profiling metrics.
"""

from typing import Any

from function.stacks.clouds.generated.aicr.dcgm import DCGM_METRICS_CSV
from function.stacks.components import Chart, Component

# DRA drivers this cloud supports that arrive outside the serving
# stack; the stacks package folds them into its derived
# supported-drivers table.
EXTERNAL_DRIVERS: list[str] = []

# Settings for every gateway's LoadBalancer Service. CKS gives a
# LoadBalancer Service a private address without this annotation.
GATEWAY_SERVICE: dict[str, Any] = {
    "annotations": {"service.beta.kubernetes.io/coreweave-load-balancer-type": "public"},
}

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
    # Publishes each GPU node's devices as DRA ResourceSlices and registers
    # the gpu.nvidia.com DeviceClass that ModelReplica ResourceClaims use.
    # GPU allocation is opt-in, and CoreWeave's own install of the driver
    # handles ComputeDomains. nvidiaDriverRoot keeps the chart default, /,
    # where the node image puts the driver.
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
    # The same exporter and values as nebius.py.
    Chart(
        key="dcgm-exporter",
        release="mp-dcgm-exporter",
        namespace="dcgm-exporter",
        chart="dcgm-exporter",
        repository="https://nvidia.github.io/dcgm-exporter/helm-charts",
        version="4.8.4",
        values={
            "serviceMonitor": {"enabled": False},
            "customMetrics": DCGM_METRICS_CSV,
            "kubernetesDRA": {"enabled": True},
            "kubernetes": {
                "enablePodLabels": True,
                "podLabelAllowlistRegex": [r"^modelplane\.ai/(deployment|replica|role|engine)$"],
            },
            "tolerations": [
                {
                    "key": "nvidia.com/gpu",
                    "operator": "Exists",
                    "effect": "NoSchedule",
                },
            ],
            # The first three nodeSelectorTerms of dra-driver-nvidia-gpu
            # v0.5.0's kubeletPlugin affinity. CKS GPU nodes have
            # pci-0302_10de. Recheck them when that chart moves.
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
