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

"""The cloud legs of the serving-stack e2e matrix. See e2e/README.md.

Each leg shapes fake, KWOK-managed Nodes the way its cloud's cluster
composition (or platform) shapes real ones - labels and taint - and
names the DaemonSets the serving stack must fan out to them. A leg
carries one pool per device stack its classes would claim: an NVIDIA
GPU pool and a CPU pool on every cloud, plus an AMD GPU pool on
Existing, the one cloud that installs the AMD stack. The ServingStack
under test names those stacks' DRA drivers in spec.draDrivers, the way
compose-inference-cluster stamps them, so the legs exercise the filter:
each device stack must reach its own pool and stay off the others'.
Those legs claim every stack their cloud carries, which only one side
of the filter can fail; the existing-filtered leg keeps the AMD pool
but leaves gpu.amd.com unclaimed, so a join that ignored the filter
fails its absence check rather than passing every leg.
Everything cloud-specific lives here, so adding fidelity for a cloud
means editing its leg, not a copy of the test.
"""

import dataclasses
import typing

# Where bring-up applies the ServingStack under test, on the control plane.
SERVING_STACK = "e2e-cloud"
NAMESPACE = "modelplane-system"

# Two fake nodes per pool, so per-node DaemonSet fan-out is visible.
FAKE_NODES = ("kwok-gpu-0", "kwok-gpu-1")
FAKE_CPU_NODES = ("kwok-cpu-0", "kwok-cpu-1")
FAKE_AMD_NODES = ("kwok-amd-0", "kwok-amd-1")

# The taints the non-NVIDIA pools carry, fixed across clouds: every
# cluster composition taints CPU pools modelplane.ai/cpu=true, and
# amd.com/gpu=true is the convention BYO AMD pools carry (Modelplane
# doesn't provision them; the AMD stack's operands tolerate exactly it).
CPU_TAINT = {"key": "modelplane.ai/cpu", "value": "true", "effect": "NoSchedule"}
AMD_TAINT = {"key": "amd.com/gpu", "value": "true", "effect": "NoSchedule"}


@dataclasses.dataclass(frozen=True)
class Cloud:
    """One cloud's leg: its fake device pools' shapes, and what must land on them."""

    cloud: str
    """The ServingStack's spec.cloud."""

    labels: dict[str, str]
    """The cloud's own NVIDIA GPU node labels, stamped on the fake GPU nodes.

    What the cluster composition (or the cloud's platform) puts on a real GPU
    node, plus the NFD labels NFD's worker would set - the GPU presence labels
    (gpu.present, pci-10de.present) and the OS release labels
    (system-os_release.ID, system-os_release.VERSION_ID) the GPU operator's
    ClusterPolicy controller reads from GPU nodes since v26.7 - pre-set because
    the worker's pod on a KWOK node is fake and never labels anything.
    """

    taint_value: str
    """The value of the nvidia.com/gpu NoSchedule taint the cloud's GPU pools carry."""

    cpu_labels: dict[str, str]
    """The CPU pool's node labels, stamped on the fake CPU nodes.

    What the cluster composition puts on a real CPU node: the pool label and
    modelplane.ai/cpu, which is both what keeps system workloads traceable
    and the selector dra-driver-cpu finds its nodes by, plus the cloud's
    instance-type label for fidelity.
    """

    dra_drivers: tuple[str, ...]
    """The ServingStack's spec.draDrivers: the DRA drivers this leg's pools claim.

    What compose-inference-cluster would collect from the leg's classes; the
    join filters the stack's tagged device stacks to exactly these.
    """

    expected_daemonsets: dict[str, int]
    """The DaemonSets that must fan out to the device pools: namespace/name to ready count.

    The node-reaching DaemonSets from the stack, by the names that cloud's
    charts give them, counted across every node their tolerations and
    selectors reach: the fake pools they serve plus the real node where they
    run everywhere.
    """

    gpu_workloads: tuple[str, ...] = ()
    """Pod-name patterns allowed on the GPU pool beyond the common GPU_WORKLOADS, such as AKS's toolkit hardening."""

    amd_labels: dict[str, str] | None = None
    """The AMD GPU pool's node labels, when the leg has one (Existing only).

    The pool label plus the NFD label (feature.node.kubernetes.io/amd-gpu)
    the AMD stack's operands select their nodes by - pre-set like the NVIDIA
    NFD labels, because nothing real runs on a KWOK node.
    """

    absent_daemonsets: tuple[str, ...] = ()
    """DaemonSets that must not exist at all: namespace/name of an unclaimed stack's operands.

    The absence side of the driver filter. The pool is on the cluster and
    the cloud's list carries the stack; only spec.draDrivers leaving the
    driver out keeps these off, so a join that ignored the filter fails
    here rather than passing a leg that claims everything.
    """


CLOUDS: dict[str, Cloud] = {
    # The labels mirror what compose-eks-cluster stamps on GPU node groups
    # (modelplane.ai/*). The taint is the one compose-eks-cluster applies.
    "eks": Cloud(
        cloud="EKS",
        labels={
            "node.kubernetes.io/instance-type": "p5.48xlarge",
            "modelplane.ai/pool": "gpu-fake",
            "modelplane.ai/gpu": "h100",
            "nvidia.com/gpu.present": "true",
            "feature.node.kubernetes.io/pci-10de.present": "true",
            # EKS GPU AMIs are AL2023.
            "feature.node.kubernetes.io/system-os_release.ID": "amzn",
            "feature.node.kubernetes.io/system-os_release.VERSION_ID": "2023",
        },
        taint_value="true",
        cpu_labels={
            "node.kubernetes.io/instance-type": "m6i.4xlarge",
            "modelplane.ai/pool": "cpu-fake",
            "modelplane.ai/cpu": "true",
        },
        dra_drivers=("dra.cpu", "gpu.nvidia.com"),
        expected_daemonsets={
            "nvidia-dra-driver/nvidia-dra-driver-gpu-kubelet-plugin": 2,
            "dra-driver-cpu/mp-dra-driver-cpu": 2,
            "node-feature-discovery/mp-node-feature-discovery-worker": 3,
            "monitoring/prometheus-node-exporter": 5,
        },
    ),
    # The taint VALUE is load-bearing here: the AKS stack's accelerated
    # tolerations are operator: Equal, value: "true" (AKS admission rejects
    # wildcards), so any other value would wrongly exclude them.
    # kubernetes.azure.com/accelerator is AKS's own GPU pool label; the
    # stack's nvidia-toolkit-hardening DaemonSet node-affines to it. AKS is
    # the one cloud whose stack enables nvsentinel's NIC health monitor, so
    # its pods belong on the GPU pool too.
    "aks": Cloud(
        cloud="AKS",
        labels={
            "node.kubernetes.io/instance-type": "Standard_ND96isr_H100_v5",
            "kubernetes.azure.com/accelerator": "nvidia",
            "modelplane.ai/pool": "gpufake",
            "modelplane.ai/gpu": "h100",
            "nvidia.com/gpu.present": "true",
            "feature.node.kubernetes.io/pci-10de.present": "true",
            # AKS GPU pools run Ubuntu 22.04.
            "feature.node.kubernetes.io/system-os_release.ID": "ubuntu",
            "feature.node.kubernetes.io/system-os_release.VERSION_ID": "22.04",
        },
        taint_value="true",
        cpu_labels={
            "node.kubernetes.io/instance-type": "Standard_D8s_v5",
            "modelplane.ai/pool": "cpufake",
            "modelplane.ai/cpu": "true",
        },
        dra_drivers=("dra.cpu", "gpu.nvidia.com"),
        expected_daemonsets={
            "nvidia-dra-driver/nvidia-dra-driver-gpu-kubelet-plugin": 2,
            "dra-driver-cpu/mp-dra-driver-cpu": 2,
            "node-feature-discovery/mp-node-feature-discovery-worker": 3,
            "monitoring/prometheus-node-exporter": 5,
            "gpu-operator/nvidia-toolkit-hardening": 2,
        },
        gpu_workloads=("^nvidia-toolkit-hardening", "^nic-health-monitor"),
    ),
    # Carries the pool labels the dra gpuStack profile requires
    # (gke-no-default-nvidia-gpu-device-plugin disables GKE's managed device
    # plugin; cloud.google.com/gke-nvidia-gpu-dra-driver is what the
    # autoscaler models DRA ResourceSlices from). The taint is GKE's own
    # managed GPU taint (value "present"); the GKE stack tolerates it with
    # operator: Exists, so the value only matters for fidelity. GKE is the
    # one cloud whose stack enables the GPU operator's container toolkit
    # (COS's driver installer ships none), so its daemonset belongs on the
    # GPU pool - hence the extra expected DaemonSet and gpu_workloads
    # pattern.
    "gke": Cloud(
        cloud="GKE",
        labels={
            "node.kubernetes.io/instance-type": "a3-highgpu-8g",
            "cloud.google.com/gke-accelerator": "nvidia-h100-80gb",
            "cloud.google.com/gke-nvidia-gpu-dra-driver": "true",
            "gke-no-default-nvidia-gpu-device-plugin": "true",
            "modelplane.ai/pool": "gpu-fake",
            "modelplane.ai/gpu": "h100",
            "nvidia.com/gpu.present": "true",
            "feature.node.kubernetes.io/pci-10de.present": "true",
            # GKE GPU pools run Container-Optimized OS; VERSION_ID is the
            # COS milestone.
            "feature.node.kubernetes.io/system-os_release.ID": "cos",
            "feature.node.kubernetes.io/system-os_release.VERSION_ID": "121",
        },
        taint_value="present",
        cpu_labels={
            "node.kubernetes.io/instance-type": "e2-standard-8",
            "modelplane.ai/pool": "cpu-fake",
            "modelplane.ai/cpu": "true",
        },
        dra_drivers=("dra.cpu", "gpu.nvidia.com"),
        expected_daemonsets={
            "nvidia-dra-driver/nvidia-dra-driver-gpu-kubelet-plugin": 2,
            "dra-driver-cpu/mp-dra-driver-cpu": 2,
            "node-feature-discovery/mp-node-feature-discovery-worker": 3,
            "monitoring/prometheus-node-exporter": 5,
            "gpu-operator/nvidia-container-toolkit-daemonset": 2,
        },
        gpu_workloads=("^nvidia-container-toolkit",),
    ),
    # The hand-written Civo half (stacks/clouds/civo.py) keeps its charts'
    # default names, like Nebius's. Unlike every other cloud it installs the
    # GPU operator's containerized driver (Civo GPU node images ship none),
    # whose operand DaemonSet lands on the GPU pool - hence the extra
    # gpu_workloads pattern. The labels and taint mirror what
    # compose-civo-cluster stamps on GPU node pools; no spec.gpu.pools on the
    # ServingStack, so the NVLink transform stays out of the way.
    "civo": Cloud(
        cloud="Civo",
        labels={
            "node.kubernetes.io/instance-type": "an.g1.l40s.kube.x1",
            "modelplane.ai/pool": "gpu-fake",
            "modelplane.ai/gpu": "nvidia-l40s",
            "nvidia.com/gpu.present": "true",
            "feature.node.kubernetes.io/pci-10de.present": "true",
            # Civo GPU node images are Ubuntu 22.04.
            "feature.node.kubernetes.io/system-os_release.ID": "ubuntu",
            "feature.node.kubernetes.io/system-os_release.VERSION_ID": "22.04",
        },
        taint_value="true",
        cpu_labels={
            "node.kubernetes.io/instance-type": "g4s.kube.large",
            "modelplane.ai/pool": "cpu-fake",
            "modelplane.ai/cpu": "true",
        },
        dra_drivers=("dra.cpu", "gpu.nvidia.com"),
        expected_daemonsets={
            "nvidia-dra-driver/dra-driver-nvidia-gpu-kubelet-plugin": 2,
            "dra-driver-cpu/mp-dra-driver-cpu": 2,
            "node-feature-discovery/mp-node-feature-discovery-worker": 3,
            "monitoring/mp-kube-prometheus-stack-prometheus-node-exporter": 5,
        },
        gpu_workloads=("^nvidia-driver-daemonset",),
    ),
    # The hand-written Nebius half (stacks/clouds/nebius.py) has no
    # gpu-operator or nvsentinel, and its charts keep default names, so the
    # DaemonSet names differ from the generated clouds. The labels and taint
    # mirror what compose-nebius-cluster stamps on GPU node groups.
    "nebius": Cloud(
        cloud="Nebius",
        labels={
            "node.kubernetes.io/instance-type": "gpu-h100-sxm",
            "modelplane.ai/pool": "gpu-fake",
            "modelplane.ai/gpu": "h100",
            "nvidia.com/gpu.present": "true",
            "feature.node.kubernetes.io/pci-10de.present": "true",
            # Nebius GPU node groups run Ubuntu 22.04.
            "feature.node.kubernetes.io/system-os_release.ID": "ubuntu",
            "feature.node.kubernetes.io/system-os_release.VERSION_ID": "22.04",
        },
        taint_value="true",
        cpu_labels={
            "node.kubernetes.io/instance-type": "cpu-d3",
            "modelplane.ai/pool": "cpu-fake",
            "modelplane.ai/cpu": "true",
        },
        dra_drivers=("dra.cpu", "gpu.nvidia.com"),
        expected_daemonsets={
            "nvidia-dra-driver/dra-driver-nvidia-gpu-kubelet-plugin": 2,
            "dra-driver-cpu/mp-dra-driver-cpu": 2,
            "node-feature-discovery/mp-node-feature-discovery-worker": 3,
            "monitoring/mp-kube-prometheus-stack-prometheus-node-exporter": 5,
        },
    ),
    # The hand-written Vultr half (stacks/clouds/vultr.py) has no NFD: Vultr
    # pre-installs the NVIDIA GPU Operator (and with it NFD) on every VKE GPU
    # cluster, so the stack must not run a second instance. That platform
    # add-on would set the NFD GPU labels on real nodes; here they are
    # pre-set like the other clouds. nvidia.com/gpu.deploy.device-plugin:
    # "false" is the label compose-vultr-cluster stamps so the platform
    # operator's device plugin stands down and the DRA driver is the sole
    # allocator.
    "vultr": Cloud(
        cloud="Vultr",
        labels={
            "node.kubernetes.io/instance-type": "vcg-a16-3c-32g-8vram",
            "nvidia.com/gpu.deploy.device-plugin": "false",
            "modelplane.ai/pool": "gpu-fake",
            "modelplane.ai/gpu": "a16",
            "nvidia.com/gpu.present": "true",
            "feature.node.kubernetes.io/pci-10de.present": "true",
            # VKE GPU nodes run Ubuntu 22.04; the platform's NFD would set
            # these like the GPU labels above.
            "feature.node.kubernetes.io/system-os_release.ID": "ubuntu",
            "feature.node.kubernetes.io/system-os_release.VERSION_ID": "22.04",
        },
        taint_value="true",
        cpu_labels={
            "node.kubernetes.io/instance-type": "vc2-6c-16gb",
            "modelplane.ai/pool": "cpu-fake",
            "modelplane.ai/cpu": "true",
        },
        dra_drivers=("dra.cpu", "gpu.nvidia.com"),
        expected_daemonsets={
            "nvidia-dra-driver/dra-driver-nvidia-gpu-kubelet-plugin": 2,
            "dra-driver-cpu/mp-dra-driver-cpu": 2,
            "monitoring/mp-kube-prometheus-stack-prometheus-node-exporter": 5,
        },
    ),
    # The model e2e already installs the Existing stack, but on an untainted
    # single node - this leg adds what it can't check: tainted GPU pools, so
    # the placement and toleration audits have teeth. Modelplane doesn't
    # provision or label BYO pools; the operator does, so the labels here are
    # the convention the stack relies on, and the taint is the nvidia.com/gpu
    # one BYO GPU pools commonly carry (the stack tolerates it with
    # operator: Exists).
    "existing": Cloud(
        cloud="Existing",
        labels={
            "modelplane.ai/pool": "gpu-fake",
            "nvidia.com/gpu.present": "true",
            "feature.node.kubernetes.io/pci-10de.present": "true",
            # No convention for BYO node OS; Ubuntu 22.04 is a common one.
            "feature.node.kubernetes.io/system-os_release.ID": "ubuntu",
            "feature.node.kubernetes.io/system-os_release.VERSION_ID": "22.04",
        },
        taint_value="true",
        cpu_labels={
            "modelplane.ai/pool": "cpu-fake",
            "modelplane.ai/cpu": "true",
        },
        # Existing is the one cloud that installs the AMD stack, so its leg
        # is the one with an AMD pool. The operands are named for the AMD
        # chart's default DeviceConfig.
        amd_labels={
            "modelplane.ai/pool": "amd-fake",
            "feature.node.kubernetes.io/amd-gpu": "true",
        },
        dra_drivers=("dra.cpu", "gpu.amd.com", "gpu.nvidia.com"),
        expected_daemonsets={
            "nvidia-dra-driver/dra-driver-nvidia-gpu-kubelet-plugin": 2,
            "dra-driver-cpu/mp-dra-driver-cpu": 2,
            "kube-amd-gpu/default-dra-driver": 2,
            "kube-amd-gpu/default-node-labeller": 2,
            "kube-amd-gpu/default-metrics-exporter": 2,
            "node-feature-discovery/mp-node-feature-discovery-worker": 5,
            "monitoring/mp-kube-prometheus-stack-prometheus-node-exporter": 7,
        },
    ),
    # The existing leg with gpu.amd.com left unclaimed: same pools, AMD
    # fake nodes included, but spec.draDrivers names only the NVIDIA and
    # CPU stacks. Every other leg claims everything its cloud carries, so
    # only this one fails a join that ignored the filter: the AMD operands
    # must not exist even though their pool is there and Existing's list
    # pins them. NFD and the node exporter still reach the AMD nodes -
    # their tolerations are static and NFD installs for the NVIDIA claim -
    # so their counts match the existing leg's.
    "existing-filtered": Cloud(
        cloud="Existing",
        labels={
            "modelplane.ai/pool": "gpu-fake",
            "nvidia.com/gpu.present": "true",
            "feature.node.kubernetes.io/pci-10de.present": "true",
            # No convention for BYO node OS; Ubuntu 22.04 is a common one.
            "feature.node.kubernetes.io/system-os_release.ID": "ubuntu",
            "feature.node.kubernetes.io/system-os_release.VERSION_ID": "22.04",
        },
        taint_value="true",
        cpu_labels={
            "modelplane.ai/pool": "cpu-fake",
            "modelplane.ai/cpu": "true",
        },
        amd_labels={
            "modelplane.ai/pool": "amd-fake",
            "feature.node.kubernetes.io/amd-gpu": "true",
        },
        dra_drivers=("dra.cpu", "gpu.nvidia.com"),
        expected_daemonsets={
            "nvidia-dra-driver/dra-driver-nvidia-gpu-kubelet-plugin": 2,
            "dra-driver-cpu/mp-dra-driver-cpu": 2,
            "node-feature-discovery/mp-node-feature-discovery-worker": 5,
            "monitoring/mp-kube-prometheus-stack-prometheus-node-exporter": 7,
        },
        absent_daemonsets=(
            "kube-amd-gpu/default-dra-driver",
            "kube-amd-gpu/default-node-labeller",
            "kube-amd-gpu/default-metrics-exporter",
        ),
    ),
}


def _node(name: str, labels: dict[str, str], taint: dict[str, str]) -> dict[str, typing.Any]:
    """Return one fake Node: a pool's labels, its taint, and KWOK's marks.

    The kwok.x-k8s.io/node annotation hands the Node to KWOK, which keeps it
    Ready and marks every pod scheduled onto it Running. No extended
    resources: the stack is DRA-mode, with every device plugin disabled.
    """
    return {
        "apiVersion": "v1",
        "kind": "Node",
        "metadata": {
            "name": name,
            "annotations": {"kwok.x-k8s.io/node": "fake", "node.alpha.kubernetes.io/ttl": "0"},
            # type: kwok is how bring-up and the tests find the fake
            # nodes; every cloud's inference nodes are linux/amd64.
            "labels": {
                "type": "kwok",
                "kubernetes.io/os": "linux",
                "kubernetes.io/arch": "amd64",
                **labels,
            },
        },
        "spec": {"taints": [taint]},
    }


def nodes(cloud: Cloud) -> list[dict[str, typing.Any]]:
    """Return the fake Nodes of a cloud's leg, for the workload cluster.

    One tainted pool per device stack the leg claims: the cloud's NVIDIA GPU
    pool, its CPU pool, and - where the leg declares one - an AMD GPU pool.
    """
    gpu_taint = {"key": "nvidia.com/gpu", "value": cloud.taint_value, "effect": "NoSchedule"}
    fake = [_node(name, cloud.labels, gpu_taint) for name in FAKE_NODES]
    fake += [_node(name, cloud.cpu_labels, CPU_TAINT) for name in FAKE_CPU_NODES]
    if cloud.amd_labels is not None:
        fake += [_node(name, cloud.amd_labels, AMD_TAINT) for name in FAKE_AMD_NODES]
    return fake


def serving_stack(cloud: Cloud) -> dict[str, typing.Any]:
    """Return the ServingStack under test, for the control plane.

    Applied directly rather than composed by an InferenceCluster: source:
    Existing hard-wires cloud: Existing, and a test-only override field isn't
    worth the API surface. The kubeconfig Secret is the one bring-up registers
    for the model e2e, so the stack installs onto the workload kind cluster,
    whose KWOK nodes are shaped like this cloud's GPU pools. The gateway
    hostname mirrors the shape compose-inference-cluster projects; with no
    clientCAs the gateway serves nothing, but its readiness needs only the
    LoadBalancer address MetalLB hands out.
    """
    return {
        "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
        "kind": "ServingStack",
        "metadata": {"name": SERVING_STACK, "namespace": NAMESPACE},
        "spec": {
            "cloud": cloud.cloud,
            "stack": "Standard",
            # What compose-inference-cluster would stamp from the leg's
            # classes: the join filters the stack's tagged device stacks to
            # exactly these, so each pool gets its stack and no other's.
            "draDrivers": list(cloud.dra_drivers),
            "gateway": {"hostname": f"gateway-{SERVING_STACK}.{NAMESPACE}.svc.cluster.local"},
            "secrets": [{"type": "Kubeconfig", "name": "local-cluster-kubeconfig", "key": "kubeconfig"}],
        },
    }
