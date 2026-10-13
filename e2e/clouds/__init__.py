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

Each leg shapes two fake, KWOK-managed GPU Nodes the way its cloud's
cluster composition (or platform) shapes real ones - labels and taint -
and names the DaemonSets the serving stack must fan out to them.
Everything cloud-specific lives here, so adding fidelity for a cloud
means editing its leg, not a copy of the test.
"""

import dataclasses
import typing

# Where bring-up applies the ServingStack under test, on the control plane.
SERVING_STACK = "e2e-cloud"
NAMESPACE = "modelplane-system"

# Two fake nodes, so per-node DaemonSet fan-out is visible.
FAKE_NODES = ("kwok-gpu-0", "kwok-gpu-1")


@dataclasses.dataclass(frozen=True)
class Cloud:
    """One cloud's leg: the fake GPU pool's shape, and what must land on it."""

    cloud: str
    """The ServingStack's spec.cloud."""

    labels: dict[str, str]
    """The cloud's own GPU node labels, stamped on the fake nodes.

    What the cluster composition (or the cloud's platform) puts on a real GPU
    node, plus the NFD GPU labels (gpu.present, pci-10de.present) NFD's worker
    would set - pre-set because the worker's pod on a KWOK node is fake and
    never labels anything.
    """

    taint_value: str
    """The value of the nvidia.com/gpu NoSchedule taint the cloud's GPU pools carry."""

    expected_daemonsets: dict[str, int]
    """The DaemonSets that must fan out to the GPU pool: namespace/name to ready count.

    The GPU-reaching DaemonSets from the stack, by the names that cloud's
    charts give them: 2 means the fake GPU nodes only, 3 means every node,
    the real one included.
    """

    gpu_workloads: tuple[str, ...] = ()
    """Pod-name patterns allowed on the GPU pool beyond the common GPU_WORKLOADS, such as AKS's toolkit hardening."""


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
        },
        taint_value="true",
        expected_daemonsets={
            "nvidia-dra-driver/nvidia-dra-driver-gpu-kubelet-plugin": 2,
            "node-feature-discovery/mp-node-feature-discovery-worker": 3,
            "monitoring/prometheus-node-exporter": 3,
        },
    ),
    # The taint VALUE is load-bearing here: the AKS stack's accelerated
    # tolerations are operator: Equal, value: "true" (AKS admission rejects
    # wildcards), so any other value would wrongly exclude them.
    # kubernetes.azure.com/accelerator is AKS's own GPU pool label; the
    # stack's nvidia-toolkit-hardening DaemonSet node-affines to it.
    "aks": Cloud(
        cloud="AKS",
        labels={
            "node.kubernetes.io/instance-type": "Standard_ND96isr_H100_v5",
            "kubernetes.azure.com/accelerator": "nvidia",
            "modelplane.ai/pool": "gpufake",
            "modelplane.ai/gpu": "h100",
            "nvidia.com/gpu.present": "true",
            "feature.node.kubernetes.io/pci-10de.present": "true",
        },
        taint_value="true",
        expected_daemonsets={
            "nvidia-dra-driver/nvidia-dra-driver-gpu-kubelet-plugin": 2,
            "node-feature-discovery/mp-node-feature-discovery-worker": 3,
            "monitoring/prometheus-node-exporter": 3,
            "gpu-operator/nvidia-toolkit-hardening": 2,
        },
        gpu_workloads=("^nvidia-toolkit-hardening",),
    ),
    # Carries the pool labels the dra gpuStack profile requires
    # (gke-no-default-nvidia-gpu-device-plugin disables GKE's managed device
    # plugin; cloud.google.com/gke-nvidia-gpu-dra-driver is what the
    # autoscaler models DRA ResourceSlices from). The taint is GKE's own
    # managed GPU taint (value "present"); the GKE stack tolerates it with
    # operator: Exists, so the value only matters for fidelity.
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
        },
        taint_value="present",
        expected_daemonsets={
            "nvidia-dra-driver/nvidia-dra-driver-gpu-kubelet-plugin": 2,
            "node-feature-discovery/mp-node-feature-discovery-worker": 3,
            "monitoring/prometheus-node-exporter": 3,
        },
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
        },
        taint_value="true",
        expected_daemonsets={
            "nvidia-dra-driver/dra-driver-nvidia-gpu-kubelet-plugin": 2,
            "node-feature-discovery/mp-node-feature-discovery-worker": 3,
            "monitoring/mp-kube-prometheus-stack-prometheus-node-exporter": 3,
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
        },
        taint_value="true",
        expected_daemonsets={
            "nvidia-dra-driver/dra-driver-nvidia-gpu-kubelet-plugin": 2,
            "node-feature-discovery/mp-node-feature-discovery-worker": 3,
            "monitoring/mp-kube-prometheus-stack-prometheus-node-exporter": 3,
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
        },
        taint_value="true",
        expected_daemonsets={
            "nvidia-dra-driver/dra-driver-nvidia-gpu-kubelet-plugin": 2,
            "monitoring/mp-kube-prometheus-stack-prometheus-node-exporter": 3,
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
        },
        taint_value="true",
        expected_daemonsets={
            "nvidia-dra-driver/dra-driver-nvidia-gpu-kubelet-plugin": 2,
            "node-feature-discovery/mp-node-feature-discovery-worker": 3,
            "monitoring/mp-kube-prometheus-stack-prometheus-node-exporter": 3,
        },
    ),
}


def nodes(cloud: Cloud) -> list[dict[str, typing.Any]]:
    """Return the fake GPU Nodes of a cloud's leg, for the workload cluster.

    The kwok.x-k8s.io/node annotation hands the Nodes to KWOK, which keeps
    them Ready and marks every pod scheduled onto them Running. No
    nvidia.com/gpu extended resource: the stack is DRA-mode, with the device
    plugin disabled everywhere.
    """
    return [
        {
            "apiVersion": "v1",
            "kind": "Node",
            "metadata": {
                "name": name,
                "annotations": {"kwok.x-k8s.io/node": "fake", "node.alpha.kubernetes.io/ttl": "0"},
                # type: kwok is how bring-up and the tests find the fake
                # nodes; every cloud's GPU nodes are linux/amd64.
                "labels": {
                    "type": "kwok",
                    "kubernetes.io/os": "linux",
                    "kubernetes.io/arch": "amd64",
                    **cloud.labels,
                },
            },
            "spec": {"taints": [{"key": "nvidia.com/gpu", "value": cloud.taint_value, "effect": "NoSchedule"}]},
        }
        for name in FAKE_NODES
    ]


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
            "gateway": {"hostname": f"gateway-{SERVING_STACK}.{NAMESPACE}.svc.cluster.local"},
            "secrets": [{"type": "Kubeconfig", "name": "local-cluster-kubeconfig", "key": "kubeconfig"}],
        },
    }
