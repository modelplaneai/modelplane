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

"""Tests for the serving stack component lists.

The join itself is the assertion: join() fails closed on duplicate
keys and on depends_on edges the join didn't produce, so iterating
every cloud and stack pair gates every list - including the generated
ones, once mapped - without involving fn.py. The tests after it are
properties every joined stack must hold, each checked over the whole
stack at once so a failure lists every component that breaks it.
JOIN_FAILS_CLOSED_CASES then checks that join rejects a cloud or stack
it doesn't know. The last two tests cover Civo's NVLink transform,
which rewrites a joined stack to disable NVLink on the pools that need
it.
"""

import dataclasses

import pytest
from function import stacks
from function.stacks.clouds import civo
from function.stacks.clouds.generated.aicr import dcgm


@dataclasses.dataclass
class JoinFailsClosedCase:
    """A test case for stacks.join rejecting a cloud or stack."""

    name: str
    reason: str
    cloud: str
    stack: str
    want: str


@dataclasses.dataclass
class SupportedDriversCase:
    """A test case for stacks.supported_drivers."""

    name: str
    reason: str
    cloud: str
    want: frozenset[str]


@dataclasses.dataclass
class WithNvLinkDisabledCase:
    """A test case for civo.with_nvlink_disabled."""

    name: str
    reason: str
    components: list[stacks.Component]
    pools: list[str]
    want: list[stacks.Component]


@pytest.mark.parametrize("stack", stacks.stacks())
@pytest.mark.parametrize("cloud", stacks.clouds())
def test_join_not_empty(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
    """Every cloud and stack pair joins into a non-empty stack."""
    assert stacks.join(cloud, stack), "a joined stack can't be empty"


@pytest.mark.parametrize("stack", stacks.stacks())
@pytest.mark.parametrize("cloud", stacks.clouds())
def test_release_names(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
    """Every Chart's Helm release is named mp-<chart>."""
    charts = [c for c in stacks.join(cloud, stack) if isinstance(c, stacks.Chart)]
    assert {c.key: c.release for c in charts} == {c.key: f"mp-{c.chart}" for c in charts}, (
        "release names are mp-<chart>: stable across upgrades, reserved to Modelplane"
    )


@pytest.mark.parametrize("stack", stacks.stacks())
@pytest.mark.parametrize("cloud", stacks.clouds())
def test_manifests_populated(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
    """Every Manifests entry carries at least one manifest."""
    empty = [c.key for c in stacks.join(cloud, stack) if isinstance(c, stacks.Manifests) and not c.manifests]
    assert empty == [], "a Manifests entry can't be empty"


@pytest.mark.parametrize("stack", stacks.stacks())
@pytest.mark.parametrize("cloud", stacks.clouds())
def test_doc_keys(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
    """doc_keys agrees with a restatement of its own rule."""
    # This restates doc_keys line for line, so it only catches one copy
    # changing without the other. COMPOSED_RESOURCE_KEYS_CASES in test_fn.py
    # pins the real keys, as literals.
    joined = stacks.join(cloud, stack)
    want = {}
    for c in joined:
        if isinstance(c, stacks.Chart) or len(c.manifests) == 1:
            want[c.key] = [c.key]
        else:
            want[c.key] = [f"{c.key}-{doc['metadata']['name']}" for doc in c.manifests]
    assert {c.key: stacks.components.doc_keys(c) for c in joined} == want, (
        "a multi-doc bundle renders one Object per doc, keyed <key>-<name>"
    )


@pytest.mark.parametrize("stack", stacks.stacks())
@pytest.mark.parametrize("cloud", stacks.clouds())
def test_ready_single_doc(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
    """A Manifests entry with a readiness query carries a single manifest."""
    # A readiness CEL query applies to every doc in an entry, so an
    # entry carrying one keeps to a single manifest - a Service or
    # ServiceAccount has no status conditions to satisfy it.
    not_single = [
        c.key
        for c in stacks.join(cloud, stack)
        if isinstance(c, stacks.Manifests) and c.ready is not None and len(c.manifests) != 1
    ]
    assert not_single == [], "a readiness query applies to every doc, so an entry carrying one has one manifest"


@pytest.mark.parametrize("stack", stacks.stacks())
@pytest.mark.parametrize("cloud", stacks.clouds())
def test_dependencies_wait(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
    """Every Chart another component depends on sets wait."""
    # A chart another component depends on renders with helm --wait,
    # so its Ready means healthy and the install gate orders
    # dependents on health rather than deploy. Without this, the
    # gate would open the moment Helm accepted the manifests.
    joined = stacks.join(cloud, stack)
    depended_on = {dep for c in joined for dep in c.depends_on}
    not_waiting = [c.key for c in joined if isinstance(c, stacks.Chart) and c.key in depended_on and not c.wait]
    assert not_waiting == [], "a depended-on chart must set wait"


@pytest.mark.parametrize("stack", stacks.stacks())
@pytest.mark.parametrize("cloud", stacks.clouds())
def test_no_wildcard_tolerations(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
    """No component of a joined stack carries a keyless toleration."""
    # A keyless toleration tolerates every taint, so the pod lands
    # on tainted GPU nodes: control-plane charts squat on
    # accelerated capacity and their eviction stalls autoscaler
    # scale-down. aicr's bundler stamps exactly that wildcard on
    # every pod it renders; the generator scopes each one
    # (TOLERATIONS in generate.py). This pins that no keyless
    # toleration survives in any joined stack, chart values and
    # manifests alike. Not only under keys named tolerations: the
    # wildcard once hid under nvsentinel's
    # global.systemNodeTolerations, which both the generator and
    # this test matched past.
    wildcards = []

    def check(node: object, where: str) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if (key == "tolerations" or key.endswith("Tolerations")) and isinstance(val, list):
                    wildcards.extend((where, t) for t in val if not (isinstance(t, dict) and "key" in t))
                else:
                    check(val, where)
        elif isinstance(node, list):
            for item in node:
                check(item, where)

    for c in stacks.join(cloud, stack):
        check(c.values if isinstance(c, stacks.Chart) else c.manifests, c.key)
    assert wildcards == [], "keyless (wildcard) tolerations, by component"


# The Literal types reject these at type-checking time; these exercise the
# runtime guard behind them, which catches the API and the stacks package
# disagreeing on a value.
JOIN_FAILS_CLOSED_CASES = [
    JoinFailsClosedCase(
        name="UnknownCloud",
        reason="A cloud the stacks package doesn't know fails the join.",
        cloud="Mars",
        stack="Standard",
        want="unknown cloud 'Mars'",
    ),
    JoinFailsClosedCase(
        name="UnknownStack",
        reason="A stack the stacks package doesn't know fails the join.",
        cloud="Nebius",
        stack="Turbo",
        want="unknown stack 'Turbo'",
    ),
]


@pytest.mark.parametrize("case", JOIN_FAILS_CLOSED_CASES, ids=lambda case: case.name)
def test_join_fails_closed(case: JoinFailsClosedCase) -> None:
    """join rejects a cloud or stack it doesn't know."""
    with pytest.raises(ValueError, match=case.want):
        stacks.join(case.cloud, case.stack)  # ty: ignore[invalid-argument-type]  # cases pass values outside the Literals


# The drivers each cloud's serving stack supports, pinned as literals so
# a new dra_drivers tag or external declaration lands here deliberately -
# and moves compose-inference-cluster's hand-kept _SUPPORTED_DRA_DRIVERS
# mirror in the same review. Existing additionally treats any driver
# beyond its tags as externally installed (see test_join_unknown_existing).
SUPPORTED_DRIVERS_CASES = [
    SupportedDriversCase(
        name="EKS",
        reason="EKS tags the NVIDIA and CPU stacks and declares dra.net installed outside the serving stack.",
        cloud="EKS",
        want=frozenset({"gpu.nvidia.com", "dra.cpu", "dra.net"}),
    ),
    SupportedDriversCase(
        name="AKS",
        reason="AKS tags the NVIDIA and CPU stacks.",
        cloud="AKS",
        want=frozenset({"gpu.nvidia.com", "dra.cpu"}),
    ),
    SupportedDriversCase(
        name="GKE",
        reason="GKE tags the NVIDIA and CPU stacks.",
        cloud="GKE",
        want=frozenset({"gpu.nvidia.com", "dra.cpu"}),
    ),
    SupportedDriversCase(
        name="Nebius",
        reason="Nebius tags the NVIDIA and CPU stacks.",
        cloud="Nebius",
        want=frozenset({"gpu.nvidia.com", "dra.cpu"}),
    ),
    SupportedDriversCase(
        name="Vultr",
        reason="Vultr tags the NVIDIA and CPU stacks.",
        cloud="Vultr",
        want=frozenset({"gpu.nvidia.com", "dra.cpu"}),
    ),
    SupportedDriversCase(
        name="Civo",
        reason="Civo tags the NVIDIA and CPU stacks.",
        cloud="Civo",
        want=frozenset({"gpu.nvidia.com", "dra.cpu"}),
    ),
    SupportedDriversCase(
        name="Existing",
        reason="Existing is the one cloud that tags the AMD stack, beside NVIDIA and CPU.",
        cloud="Existing",
        want=frozenset({"gpu.nvidia.com", "gpu.amd.com", "dra.cpu"}),
    ),
]


@pytest.mark.parametrize("case", SUPPORTED_DRIVERS_CASES, ids=lambda case: case.name)
def test_supported_drivers(case: SupportedDriversCase) -> None:
    """Each cloud's derived supported drivers match the pinned set."""
    assert stacks.supported_drivers(case.cloud) == case.want, case.reason  # ty: ignore[invalid-argument-type]  # parametrize erases the Literal


@pytest.mark.parametrize("stack", stacks.stacks())
@pytest.mark.parametrize("cloud", stacks.clouds())
def test_join_filter_empty(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
    """An empty draDrivers list keeps exactly the untagged components."""
    assert stacks.join(cloud, stack, dra_drivers=[]) == [c for c in stacks.join(cloud, stack) if not c.dra_drivers]


@pytest.mark.parametrize("stack", stacks.stacks())
@pytest.mark.parametrize("cloud", stacks.clouds())
def test_join_filter_all(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
    """Listing every tagged driver reproduces the unfiltered join."""
    joined = stacks.join(cloud, stack)
    drivers = sorted({d for c in joined for d in c.dra_drivers})
    assert stacks.join(cloud, stack, dra_drivers=drivers) == joined


def test_join_filter_nvidia() -> None:
    """gpu.nvidia.com alone installs the NVIDIA stack and no other."""
    keys = {c.key for c in stacks.join("Existing", "Standard", dra_drivers=["gpu.nvidia.com"])}
    assert {"node-feature-discovery", "nvidia-dra-driver-gpu", "dra-driver-critical-pods-quota"} <= keys
    assert {"dra-driver-cpu", "dra-driver-cpu-critical-pods-quota", "amd-gpu-operator"}.isdisjoint(keys)


def test_join_filter_cpu() -> None:
    """dra.cpu alone installs the CPU driver and neither GPU stack - NFD included."""
    keys = {c.key for c in stacks.join("Existing", "Standard", dra_drivers=["dra.cpu"])}
    assert {"dra-driver-cpu", "dra-driver-cpu-critical-pods-quota"} <= keys
    assert {"node-feature-discovery", "nvidia-dra-driver-gpu", "amd-gpu-operator"}.isdisjoint(keys)


def test_join_filter_amd() -> None:
    """gpu.amd.com on Existing installs the AMD operator and the NFD both GPU stacks share."""
    keys = {c.key for c in stacks.join("Existing", "Standard", dra_drivers=["gpu.amd.com"])}
    assert {"amd-gpu-operator", "node-feature-discovery"} <= keys
    assert {"nvidia-dra-driver-gpu", "dra-driver-critical-pods-quota", "dra-driver-cpu"}.isdisjoint(keys)


@pytest.mark.parametrize("cloud", [c for c in stacks.clouds() if c != "Existing"])
def test_join_unknown_driver(cloud: stacks.Cloud) -> None:
    """A driver the cloud neither tags nor declares external fails the join."""
    with pytest.raises(ValueError, match=r"gpu\.example\.com"):
        stacks.join(cloud, "Standard", dra_drivers=["gpu.example.com"])


def test_join_unknown_existing() -> None:
    """Existing treats an unlisted driver as externally installed: no raise, no tagged component."""
    joined = stacks.join("Existing", "Standard", dra_drivers=["gpu.example.com"])
    assert [c.key for c in joined if c.dra_drivers] == []


def test_join_external_driver() -> None:
    """EKS's declared-external dra.net is accepted and installs nothing; elsewhere it raises."""
    joined = stacks.join("EKS", "Standard", dra_drivers=["dra.net"])
    assert [c.key for c in joined if c.dra_drivers] == []
    with pytest.raises(ValueError, match=r"dra\.net"):
        stacks.join("GKE", "Standard", dra_drivers=["dra.net"])


def test_join_tagged_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dependency not carrying every driver its dependent does fails every join for the cloud."""
    broken = [
        stacks.Chart(
            key="dep",
            release="mp-dep",
            namespace="d",
            chart="dep",
            repository="r",
            version="1",
            wait=True,
            dra_drivers=["gpu.nvidia.com"],
        ),
        stacks.Chart(
            key="user",
            release="mp-user",
            namespace="d",
            chart="user",
            repository="r",
            version="1",
            depends_on=["dep"],
            dra_drivers=["gpu.nvidia.com", "dra.cpu"],
        ),
    ]
    monkeypatch.setitem(stacks._CLOUDS, "Existing", broken)
    with pytest.raises(ValueError, match="filtering could remove"):
        stacks.join("Existing", "Standard")


def test_join_untagged_dependent(monkeypatch: pytest.MonkeyPatch) -> None:
    """An untagged component depending on a tagged one fails: the filter keeps it but can drop its dependency."""
    broken = [
        stacks.Chart(
            key="dep",
            release="mp-dep",
            namespace="d",
            chart="dep",
            repository="r",
            version="1",
            wait=True,
            dra_drivers=["gpu.nvidia.com"],
        ),
        stacks.Chart(
            key="user",
            release="mp-user",
            namespace="d",
            chart="user",
            repository="r",
            version="1",
            depends_on=["dep"],
        ),
    ]
    monkeypatch.setitem(stacks._CLOUDS, "Existing", broken)
    with pytest.raises(ValueError, match="filtering could remove"):
        stacks.join("Existing", "Standard")


@pytest.mark.parametrize("cloud", ["AKS", "EKS", "GKE"])
def test_generated_dcgm_csv(cloud: str) -> None:
    """Each generated cloud's inline DCGM exporter CSV is the shared one.

    The generated files carry the CSV inline to stay self-contained,
    and the hand-written clouds that run the exporter import
    generated/aicr/dcgm.py instead of restating it. emit_dcgm
    proves the copies identical at generation time; this asserts the
    files on disk still agree, so a hand edit to any one copy - or a
    partial regeneration that slipped past the generator's guard -
    can't silently leave a hand-written cloud's exporter config behind
    the generated clouds'.
    """
    joined = stacks.join(cloud, "Standard")  # ty: ignore[invalid-argument-type]  # parametrize erases the Literal
    operator = next(c for c in joined if isinstance(c, stacks.Chart) and c.key == "gpu-operator")
    assert operator.values is not None
    assert operator.values["dcgmExporter"]["config"]["data"] == dcgm.DCGM_METRICS_CSV


# Civo's DCGM exporter metrics CSV, restated once rather than beside each
# want: both wants carry it verbatim, and it is too long to repeat without
# burying the driver values each case is actually about. Still a literal
# independent of the shared generated/aicr/dcgm.py civo.py imports, so a
# generator-moved CSV still fails these cases until a human confirms the
# move suits Civo.
_DCGM_METRICS_CSV = """# Clocks,,
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
"""


# Each case's input is the real joined Civo stack rather than a literal one.
# The transform finds the gpu-operator and DRA driver charts by key, and where
# a key matches nothing it rewrites nothing, without complaint. Only the real
# stack shows the keys still match the charts Civo composes. The cost is that
# both wants restate those two charts as Civo pins them, so bumping either
# chart breaks both cases.
WITH_NVLINK_DISABLED_CASES = [
    WithNvLinkDisabledCase(
        name="OnePool",
        reason=(
            "Disabling NVLink on one pool switches the gpu-operator chart to NVIDIADriver-CRD mode, adds the kernel "
            "module ConfigMap and that pool's NVIDIADriver, and gates the DRA driver on it."
        ),
        components=stacks.join("Civo", "Standard"),
        pools=["h100-pool"],
        want=[
            stacks.Chart(
                key="gpu-operator",
                release="mp-gpu-operator",
                namespace="gpu-operator",
                chart="gpu-operator",
                repository="https://helm.ngc.nvidia.com/nvidia",
                version="v26.7.1",
                wait=True,
                depends_on=["node-feature-discovery", "cert-manager"],
                dra_drivers=["gpu.nvidia.com"],
                values={
                    "ccManager": {"enabled": False},
                    "cdi": {"default": True, "enabled": True},
                    "daemonsets": {
                        "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]
                    },
                    "dcgm": {"enabled": True},
                    "dcgmExporter": {
                        "config": {"create": True, "data": _DCGM_METRICS_CSV, "name": "dcgm-exporter"},
                        "serviceMonitor": {"enabled": False},
                    },
                    "devicePlugin": {"enabled": False},
                    "driver": {
                        "enabled": True,
                        "maxParallelUpgrades": 5,
                        "rdma": {"enabled": False},
                        "kernelModuleType": "open",
                        "version": "580.173.02",
                        # deployDefaultCR keeps the chart's default NVIDIADriver
                        # driving the pools the transform doesn't name, so
                        # flipping modes changes nothing for them.
                        "nvidiaDriverCRD": {"enabled": True, "deployDefaultCR": True},
                    },
                    "fullnameOverride": "gpu-operator",
                    "gdrcopy": {"enabled": False},
                    "gfd": {"enabled": True},
                    "kataSandboxDevicePlugin": {"enabled": False},
                    "migManager": {"enabled": False},
                    "nfd": {"enabled": False},
                    "operator": {
                        "resources": {
                            "limits": {"cpu": "500m", "memory": "700Mi"},
                            "requests": {"cpu": "200m", "memory": "300Mi"},
                        },
                        "tolerations": [],
                        "upgradeCRD": True,
                    },
                    "toolkit": {"enabled": False},
                    "validator": {"plugin": {"env": [{"name": "WITH_WORKLOAD", "value": "false"}]}},
                },
            ),
            stacks.Chart(
                key="nvidia-dra-driver-gpu",
                release="mp-dra-driver-nvidia-gpu",
                namespace="nvidia-dra-driver",
                chart="dra-driver-nvidia-gpu",
                repository="oci://registry.k8s.io/dra-driver-nvidia/charts",
                version="0.5.0",
                depends_on=["gpu-operator", "nvlink-disabled-driver-h100-pool"],
                dra_drivers=["gpu.nvidia.com"],
                values={
                    "gpuResourcesEnabledOverride": True,
                    "nvidiaDriverRoot": "/run/nvidia/driver",
                    "resources": {"computeDomains": {"enabled": False}},
                },
            ),
            stacks.Manifests(
                key="nvlink-disable-config",
                manifests=[
                    {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "gpu-operator"}},
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": "nvidia-kernel-config", "namespace": "gpu-operator"},
                        "data": {"nvidia.conf": "options nvidia NVreg_NvLinkDisable=1"},
                    },
                ],
            ),
            stacks.Manifests(
                key="nvlink-disabled-driver-h100-pool",
                manifests=[
                    {
                        "apiVersion": "nvidia.com/v1alpha1",
                        "kind": "NVIDIADriver",
                        "metadata": {"name": "nvlink-disabled-h100-pool"},
                        "spec": {
                            "driverType": "gpu",
                            # The chart's driver pin and module flavor: one
                            # review moves both.
                            "version": "580.173.02",
                            "useOpenKernelModules": True,
                            "nodeSelector": {"modelplane.ai/pool": "h100-pool"},
                            "kernelModuleConfig": {"name": "nvidia-kernel-config"},
                            "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}],
                        },
                    }
                ],
                depends_on=["gpu-operator", "nvlink-disable-config"],
                # The operator-populated state, so the DRA driver's install
                # gate orders on driver health.
                ready='object.status.state == "ready"',
            ),
        ],
    ),
    WithNvLinkDisabledCase(
        name="TwoPools",
        reason="Disabling NVLink on two pools adds an NVIDIADriver for each, and gates the DRA driver on both.",
        components=stacks.join("Civo", "Standard"),
        pools=["pool-a", "pool-b"],
        want=[
            stacks.Chart(
                key="gpu-operator",
                release="mp-gpu-operator",
                namespace="gpu-operator",
                chart="gpu-operator",
                repository="https://helm.ngc.nvidia.com/nvidia",
                version="v26.7.1",
                wait=True,
                depends_on=["node-feature-discovery", "cert-manager"],
                dra_drivers=["gpu.nvidia.com"],
                values={
                    "ccManager": {"enabled": False},
                    "cdi": {"default": True, "enabled": True},
                    "daemonsets": {
                        "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]
                    },
                    "dcgm": {"enabled": True},
                    "dcgmExporter": {
                        "config": {"create": True, "data": _DCGM_METRICS_CSV, "name": "dcgm-exporter"},
                        "serviceMonitor": {"enabled": False},
                    },
                    "devicePlugin": {"enabled": False},
                    "driver": {
                        "enabled": True,
                        "maxParallelUpgrades": 5,
                        "rdma": {"enabled": False},
                        "kernelModuleType": "open",
                        "version": "580.173.02",
                        "nvidiaDriverCRD": {"enabled": True, "deployDefaultCR": True},
                    },
                    "fullnameOverride": "gpu-operator",
                    "gdrcopy": {"enabled": False},
                    "gfd": {"enabled": True},
                    "kataSandboxDevicePlugin": {"enabled": False},
                    "migManager": {"enabled": False},
                    "nfd": {"enabled": False},
                    "operator": {
                        "resources": {
                            "limits": {"cpu": "500m", "memory": "700Mi"},
                            "requests": {"cpu": "200m", "memory": "300Mi"},
                        },
                        "tolerations": [],
                        "upgradeCRD": True,
                    },
                    "toolkit": {"enabled": False},
                    "validator": {"plugin": {"env": [{"name": "WITH_WORKLOAD", "value": "false"}]}},
                },
            ),
            stacks.Chart(
                key="nvidia-dra-driver-gpu",
                release="mp-dra-driver-nvidia-gpu",
                namespace="nvidia-dra-driver",
                chart="dra-driver-nvidia-gpu",
                repository="oci://registry.k8s.io/dra-driver-nvidia/charts",
                version="0.5.0",
                depends_on=["gpu-operator", "nvlink-disabled-driver-pool-a", "nvlink-disabled-driver-pool-b"],
                dra_drivers=["gpu.nvidia.com"],
                values={
                    "gpuResourcesEnabledOverride": True,
                    "nvidiaDriverRoot": "/run/nvidia/driver",
                    "resources": {"computeDomains": {"enabled": False}},
                },
            ),
            stacks.Manifests(
                key="nvlink-disable-config",
                manifests=[
                    {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "gpu-operator"}},
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": "nvidia-kernel-config", "namespace": "gpu-operator"},
                        "data": {"nvidia.conf": "options nvidia NVreg_NvLinkDisable=1"},
                    },
                ],
            ),
            stacks.Manifests(
                key="nvlink-disabled-driver-pool-a",
                manifests=[
                    {
                        "apiVersion": "nvidia.com/v1alpha1",
                        "kind": "NVIDIADriver",
                        "metadata": {"name": "nvlink-disabled-pool-a"},
                        "spec": {
                            "driverType": "gpu",
                            "version": "580.173.02",
                            "useOpenKernelModules": True,
                            "nodeSelector": {"modelplane.ai/pool": "pool-a"},
                            "kernelModuleConfig": {"name": "nvidia-kernel-config"},
                            "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}],
                        },
                    }
                ],
                depends_on=["gpu-operator", "nvlink-disable-config"],
                ready='object.status.state == "ready"',
            ),
            stacks.Manifests(
                key="nvlink-disabled-driver-pool-b",
                manifests=[
                    {
                        "apiVersion": "nvidia.com/v1alpha1",
                        "kind": "NVIDIADriver",
                        "metadata": {"name": "nvlink-disabled-pool-b"},
                        "spec": {
                            "driverType": "gpu",
                            "version": "580.173.02",
                            "useOpenKernelModules": True,
                            "nodeSelector": {"modelplane.ai/pool": "pool-b"},
                            "kernelModuleConfig": {"name": "nvidia-kernel-config"},
                            "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}],
                        },
                    }
                ],
                depends_on=["gpu-operator", "nvlink-disable-config"],
                ready='object.status.state == "ready"',
            ),
        ],
    ),
]


@pytest.mark.parametrize("case", WITH_NVLINK_DISABLED_CASES, ids=lambda case: case.name)
def test_with_nvlink_disabled(case: WithNvLinkDisabledCase) -> None:
    """with_nvlink_disabled rewrites a joined stack to disable NVLink on the named pools."""
    got = civo.with_nvlink_disabled(case.components, case.pools)
    # Only what the transform adds or rewrites, in order. The rest of the list
    # is the joined stack passed through, vendored CRDs and all, and restating
    # it would bury the components each case is about. A component the
    # transform dropped wouldn't show up here.
    changed = [c for c in got if c not in case.components]
    assert changed == case.want, case.reason


def test_join_not_mutated() -> None:
    """with_nvlink_disabled leaves the joined stack it was given unchanged."""
    # The transform must copy: the joined lists share the module-level
    # component objects, and mutating them would leak NVLink disable
    # into every later request. This checks the two fields the transform
    # rewrites against their stock values rather than comparing the stack
    # before and after, because a before-and-after comparison would miss an
    # edit an earlier test's call had already made.
    civo.with_nvlink_disabled(stacks.join("Civo", "Standard"), ["h100-pool"])
    joined = stacks.join("Civo", "Standard")
    op = next(c for c in joined if isinstance(c, stacks.Chart) and c.key == "gpu-operator")
    dra = next(c for c in joined if isinstance(c, stacks.Chart) and c.key == "nvidia-dra-driver-gpu")
    assert op.values is not None
    assert "nvidiaDriverCRD" not in op.values["driver"]
    assert dra.depends_on == ["gpu-operator"]
