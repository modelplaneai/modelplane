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

"""The cloud half of the stack for Vultr.

No generator covers Vultr, so Modelplane pins the cloud half by hand,
in the same shape a generator emits. Where a component also appears on
a generated cloud, this file states the same pin, so one review moves
both halves.

No node-feature-discovery here: VKE pre-installs a managed GPU
Operator add-on that runs NFD with tolerate-everything workers, and a
second instance would race it over the same label namespace. That
add-on also leaves the driver and toolkit to the node image (so the
DRA driver's default root holds). Its device plugin would advertise
nvidia.com/gpu beside the DRA ResourceSlices - two allocators with
independent ledgers over the same devices - so compose-vultr-cluster
labels Modelplane's GPU pools nvidia.com/gpu.deploy.device-plugin=false,
which the operator respects, leaving the DRA driver the sole allocator
on those nodes.
"""

from function.stacks.clouds.generated.aicr.ephemeral_storage import EPHEMERAL_STORAGE_VALUES
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
    # Reports each node's ephemeral-storage headroom and each pod's
    # usage, so modelplane_ephemeral_storage_* is populated here as on
    # the generated clouds (stacks/metrics.py renames the series). A
    # single Deployment reads every node's figures from the kubelet
    # summary API, so it needs no GPU toleration and stays on the system
    # pool. The values are the generator's, imported from the shared
    # generated/aicr/ephemeral_storage.py: the ServiceMonitor stays off,
    # as on the generated clouds, and the collector's substrate job
    # scrapes the exporter by the prometheus.io annotations they carry.
    Chart(
        key="k8s-ephemeral-storage-metrics",
        release="mp-k8s-ephemeral-storage-metrics",
        namespace="monitoring",
        chart="k8s-ephemeral-storage-metrics",
        repository="https://jmcgrath207.github.io/k8s-ephemeral-storage-metrics/chart",
        version="1.19.2",
        values=EPHEMERAL_STORAGE_VALUES,
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
]
