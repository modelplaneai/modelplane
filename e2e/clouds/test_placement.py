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

"""Each device stack fans out to its own pool, and only its own pool.

Two-sided checks, per pool. The fan-out test catches tolerations too narrow
to reach a tainted pool, such as an Equal toleration whose value doesn't
match the taint, and a filter that wrongly dropped a claimed stack. The
audits catch the opposite class: pods tolerating taints they shouldn't, and
one stack's operands landing on another stack's pool - the NVIDIA kubelet
plugin on a CPU node, or dra-driver-cpu on a GPU node. Real AKS rejects
wildcard tolerations at admission and every cloud taints its inference
pools, so a system-pool pod carrying a wildcard or pool toleration is a bug
even when the scheduler happened to keep it on the system node.
"""

import re

import pytest
from kubernetes import client

from e2e import clouds, kube, wait

# Pods expected to carry the NVIDIA GPU toleration and land on the fake GPU
# nodes: the GPU-reaching DaemonSets from generate.py's TOLERATIONS table -
# the NFD worker, node-exporter, DRA kubelet plugin, gpu-operator operands
# and nvsentinel's node monitors (platform-connectors runs on every node; the
# health monitors select on labels nvsentinel's labeler applies at runtime).
# Unanchored where the generated and hand-written halves name the same
# workload differently. Per-cloud extras come from Cloud.gpu_workloads.
GPU_WORKLOADS = (
    "node-feature-discovery-worker",
    "prometheus-node-exporter",
    "kubelet-plugin",
    "^gpu-feature-discovery",
    "^nvidia-dcgm",
    "^nvidia-mig-manager",
    "^nvidia-operator-validator",
    "^nvidia-cuda-validator",
    "^gpu-health-monitor",
    "^syslog-health-monitor",
    "^metadata-collector",
    "^platform-connectors",
)

# Pods expected to carry the CPU pool toleration and land on the fake CPU
# nodes: dra-driver-cpu (its pin's nodeSelector and toleration name exactly
# this pool) and the node exporter, which reaches every inference pool.
# NFD is deliberately absent: it exists to label GPU nodes, so it carries
# the GPU drivers' tags and stays off CPU pools.
CPU_WORKLOADS = (
    "dra-driver-cpu",
    "prometheus-node-exporter",
)

# Pods expected to carry the AMD GPU toleration and land on the fake AMD
# nodes (the Existing leg only): the AMD operator's operands, named for its
# chart's default DeviceConfig, plus the NFD worker - it labels both GPU
# vendors' nodes - and the node exporter.
AMD_WORKLOADS = (
    "node-feature-discovery-worker",
    "prometheus-node-exporter",
    "^default-dra-driver",
    "^default-node-labeller",
    "^default-metrics-exporter",
)

# Pool-only pods must never run anywhere but their own pool: they'd only
# land elsewhere by a selector or affinity regression. The NFD worker and
# node-exporter aren't here - they legitimately run on several pools.
GPU_ONLY = re.compile(
    "|".join(
        (
            "kubelet-plugin",
            "^gpu-feature-discovery",
            "^nvidia-dcgm",
            "^nvidia-mig-manager",
            "^nvidia-operator-validator",
            "^nvidia-cuda-validator",
            "^nvidia-toolkit-hardening",
            "^nvidia-container-toolkit",
            "^nvidia-driver-daemonset",
        )
    )
)
CPU_ONLY = re.compile("dra-driver-cpu")
AMD_ONLY = re.compile("|".join(("^default-dra-driver", "^default-node-labeller", "^default-metrics-exporter")))

# The taint each fake pool carries, keyed by the pool the tests report.
POOL_TAINT_KEYS = {
    "nvidia": "nvidia.com/gpu",
    "cpu": "modelplane.ai/cpu",
    "amd": "amd.com/gpu",
}

# Cluster infrastructure the serving stack doesn't manage.
EXEMPT_NAMESPACES = ("kube-system", "metallb-system", "local-path-storage")


def gpu_workloads(cloud: clouds.Cloud) -> re.Pattern[str]:
    """Return the pod names allowed to tolerate the GPU taint and run on the fake GPU nodes, for a cloud."""
    return re.compile("|".join(GPU_WORKLOADS + cloud.gpu_workloads))


def allowed_by_pool(cloud: clouds.Cloud) -> dict[str, re.Pattern[str]]:
    """Return each pool's allowed pod names, for a cloud."""
    return {
        "nvidia": gpu_workloads(cloud),
        "cpu": re.compile("|".join(CPU_WORKLOADS)),
        "amd": re.compile("|".join(AMD_WORKLOADS)),
    }


def fake_pools(workload: kube.Cluster) -> dict[str, str]:
    """Return each fake node's pool, from the taint its leg shaped it with."""
    taint_pools = {taint: pool for pool, taint in POOL_TAINT_KEYS.items()}
    pools = {}
    for node in workload.core.list_node(label_selector="type=kwok", _request_timeout=kube.TIMEOUT_SECONDS).items:
        for taint in node.spec.taints or []:
            if taint.key in taint_pools:
                pools[node.metadata.name] = taint_pools[taint.key]
    return pools


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrize the fan-out test from the leg's expected DaemonSets, so a failure names its DaemonSet."""
    if "daemonset" in metafunc.fixturenames:
        cloud = clouds.CLOUDS[metafunc.config.getoption("--cloud")]
        metafunc.parametrize(("daemonset", "want"), cloud.expected_daemonsets.items(), ids=cloud.expected_daemonsets)


@pytest.mark.usefixtures("installed")
def test_daemonsets_fan_out(workload: kube.Cluster, daemonset: str, want: int) -> None:
    """A DaemonSet the cloud expects on its device pools reaches its ready count.

    Charts without dependents get no helm --wait, so a rollout can lag the
    ServingStack's Ready condition; poll. KWOK fakes the pods on the fake
    nodes Running, so a shortfall means the scheduler never placed them - a
    toleration too narrow to reach the tainted pool, a selector that misses
    its nodes, or a filter that dropped the stack the pool needs.
    """
    namespace, name = daemonset.split("/", 1)

    def fanned_out() -> None:
        ds = workload.apps.read_namespaced_daemon_set(name, namespace, _request_timeout=kube.TIMEOUT_SECONDS)
        ready = ds.status.number_ready or 0
        assert ready == want, f"DaemonSet {namespace}/{name} is {ready}/{want} ready"

    wait.until(
        fanned_out,
        timeout=10 * 60,
        what=f"DaemonSet {namespace}/{name} to reach {want} ready pods",
        retry=kube.RETRY,
    )


@pytest.mark.usefixtures("installed")
def test_tolerations_are_scoped(workload: kube.Cluster, cloud: clouds.Cloud) -> None:
    """No pod outside a pool's allowlist tolerates that pool's taint, or every taint.

    Wherever the scheduler put the pod: a wildcard that happened to stay on
    the system node still squats on accelerated capacity the moment the real
    cloud's autoscaler adds a tainted node.
    """
    allowed = allowed_by_pool(cloud)
    bad = []
    for pod in workload.core.list_pod_for_all_namespaces(_request_timeout=kube.TIMEOUT_SECONDS).items:
        namespace, name = pod.metadata.namespace, pod.metadata.name
        if namespace in EXEMPT_NAMESPACES:
            continue
        for toleration in pod.spec.tolerations or []:
            if toleration.operator == "Exists" and not toleration.key:
                bad.append(f"{namespace}/{name} tolerates every taint")
                continue
            for pool, taint_key in POOL_TAINT_KEYS.items():
                if toleration.key == taint_key and not allowed[pool].search(name):
                    bad.append(f"{namespace}/{name} tolerates the {pool} pool's taint")
    assert bad == [], "pods carrying tolerations the system pool must not have"


@pytest.mark.usefixtures("installed")
def test_pools_hold_their_own(workload: kube.Cluster, cloud: clouds.Cloud) -> None:
    """Everything on a fake node belongs to that node's pool, and pool-only workloads stay there.

    The second side is what gives the filter teeth: dra-driver-cpu off the
    CPU pool, or a GPU stack's operand off its GPU pool, means a selector or
    toleration regressed - or a device stack installed somewhere its driver
    was never claimed.
    """
    allowed = allowed_by_pool(cloud)
    pools = fake_pools(workload)
    assert pools, "the workload cluster has no fake nodes"
    only = {"nvidia": GPU_ONLY, "cpu": CPU_ONLY, "amd": AMD_ONLY}
    bad = []
    for pod in workload.core.list_pod_for_all_namespaces(_request_timeout=kube.TIMEOUT_SECONDS).items:
        namespace, name, node = pod.metadata.namespace, pod.metadata.name, pod.spec.node_name
        if namespace in EXEMPT_NAMESPACES or node is None:
            continue
        pool = pools.get(node)
        if pool is not None and not allowed[pool].search(name):
            bad.append(f"{namespace}/{name} doesn't belong on fake {pool} node {node}")
        for pool_only, pattern in only.items():
            if pool != pool_only and pattern.search(name):
                where = f"fake {pool} node {node}" if pool else f"system node {node}"
                bad.append(f"{namespace}/{name} is {pool_only}-only but sits on {where}")
    assert bad == [], "pods scheduled onto the wrong pool"


@pytest.mark.usefixtures("installed")
def test_unclaimed_stack_absent(workload: kube.Cluster, cloud: clouds.Cloud) -> None:
    """A stack whose driver no class claims stays off the cluster entirely.

    The existing-filtered leg's teeth: its AMD pool is on the cluster and
    Existing's list pins the AMD stack, so only spec.draDrivers leaving
    gpu.amd.com out keeps these DaemonSets from existing. Checked once the
    stack is Ready, by which point a join that ignored the filter would
    have installed them - every other leg claims everything its cloud
    carries and lists nothing here.
    """
    present = []
    for daemonset in cloud.absent_daemonsets:
        namespace, name = daemonset.split("/", 1)
        try:
            workload.apps.read_namespaced_daemon_set(name, namespace, _request_timeout=kube.TIMEOUT_SECONDS)
            present.append(daemonset)
        except client.ApiException as e:
            if e.status != 404:
                raise
    assert present == [], "DaemonSets of a stack whose driver no class claims"
