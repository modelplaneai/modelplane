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

"""The serving stack fans out to the GPU pool, and only the GPU pool.

Two-sided checks. The fan-out test catches tolerations too narrow to reach
the tainted GPU pool, such as an Equal toleration whose value doesn't match
the taint. The audits catch the opposite class: pods tolerating taints they
shouldn't. Real AKS rejects wildcard tolerations at admission and every cloud
taints its GPU pools, so a system-pool pod carrying a wildcard or
nvidia.com/gpu toleration is a bug even when the scheduler happened to keep
it on the system node.
"""

import re

import pytest

from e2e import clouds, kube, wait

# Pods expected to carry the GPU toleration and land on the fake GPU nodes:
# the GPU-reaching DaemonSets from generate.py's TOLERATIONS table - the NFD
# worker, node-exporter, DRA kubelet plugin, gpu-operator operands and
# nvsentinel's node monitors (platform-connectors runs on every node; the
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

# GPU-only pods must never run on the system pool: they'd only land there by
# a selector or affinity regression. The NFD worker and node-exporter aren't
# here - they legitimately run on every node.
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
            "^nvidia-driver-daemonset",
        )
    )
)

# Cluster infrastructure the serving stack doesn't manage.
EXEMPT_NAMESPACES = ("kube-system", "metallb-system", "local-path-storage")


def gpu_workloads(cloud: clouds.Cloud) -> re.Pattern[str]:
    """Return the pod names allowed to tolerate the GPU taint and run on the fake nodes, for a cloud."""
    return re.compile("|".join(GPU_WORKLOADS + cloud.gpu_workloads))


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrize the fan-out test from the leg's expected DaemonSets, so a failure names its DaemonSet."""
    if "daemonset" in metafunc.fixturenames:
        cloud = clouds.CLOUDS[metafunc.config.getoption("--cloud")]
        metafunc.parametrize(("daemonset", "want"), cloud.expected_daemonsets.items(), ids=cloud.expected_daemonsets)


@pytest.mark.usefixtures("installed")
def test_daemonsets_fan_out(workload: kube.Cluster, daemonset: str, want: int) -> None:
    """A DaemonSet the cloud expects on its GPU pool reaches its ready count.

    Charts without dependents get no helm --wait, so a rollout can lag the
    ServingStack's Ready condition; poll. KWOK fakes the pods on the fake
    nodes Running, so a shortfall means the scheduler never placed them - a
    toleration too narrow to reach the tainted pool.
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
    """No pod outside the GPU allowlist tolerates the GPU taint, or every taint.

    Wherever the scheduler put the pod: a wildcard that happened to stay on
    the system node still squats on accelerated capacity the moment the real
    cloud's autoscaler adds a GPU node.
    """
    allowed = gpu_workloads(cloud)
    bad = []
    for pod in workload.core.list_pod_for_all_namespaces(_request_timeout=kube.TIMEOUT_SECONDS).items:
        namespace, name = pod.metadata.namespace, pod.metadata.name
        if namespace in EXEMPT_NAMESPACES:
            continue
        for toleration in pod.spec.tolerations or []:
            if toleration.operator == "Exists" and not toleration.key:
                bad.append(f"{namespace}/{name} tolerates every taint")
            elif toleration.key == "nvidia.com/gpu" and not allowed.search(name):
                bad.append(f"{namespace}/{name} tolerates the GPU taint")
    assert bad == [], "pods carrying tolerations the system pool must not have"


@pytest.mark.usefixtures("installed")
def test_only_gpu_workloads_on_the_gpu_pool(workload: kube.Cluster, cloud: clouds.Cloud) -> None:
    """Everything on a fake GPU node is an expected GPU workload, and GPU-only workloads stay off the system pool."""
    allowed = gpu_workloads(cloud)
    fake = {
        node.metadata.name
        for node in workload.core.list_node(label_selector="type=kwok", _request_timeout=kube.TIMEOUT_SECONDS).items
    }
    assert fake, "the workload cluster has no fake GPU nodes"
    bad = []
    for pod in workload.core.list_pod_for_all_namespaces(_request_timeout=kube.TIMEOUT_SECONDS).items:
        namespace, name, node = pod.metadata.namespace, pod.metadata.name, pod.spec.node_name
        if namespace in EXEMPT_NAMESPACES or node is None:
            continue
        if node in fake and not allowed.search(name):
            bad.append(f"{namespace}/{name} doesn't belong on fake GPU node {node}")
        elif node not in fake and GPU_ONLY.search(name):
            bad.append(f"{namespace}/{name} is GPU-only but sits on system node {node}")
    assert bad == [], "pods scheduled onto the wrong pool"
