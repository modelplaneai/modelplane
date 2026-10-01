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

"""Tests for the DRA CEL selector module.

Pins Program.matches - the device activation shape, qualified-name domains,
unknown-domain handling, and quantity()/semver() dispatch - against upstream
DRA behavior (k8s.io/dynamic-resource-allocation/cel). Table-driven: each case
is one (selector expression, device, want).
"""

import dataclasses

import pytest
from function import cel


@dataclasses.dataclass
class Case:
    """A test case for matching a DRA CEL selector against a device."""

    name: str
    expr: str
    device: dict
    want: bool


def _hopper_gpu() -> dict:
    """A gpu.nvidia.com Hopper GPU with 141Gi of memory, on PCIe root pci0."""
    return {
        "driver": "gpu.nvidia.com",
        "attributes": {
            "architecture": {"string": "Hopper"},
            "cudaComputeCapability": {"version": "9.5.3"},
            "resource.kubernetes.io/pcieRoot": {"string": "pci0"},
        },
        "capacity": {"memory": {"value": "141Gi"}},
    }


def _scalar_device(*, x: dict) -> dict:
    """A gpu.nvidia.com device whose one attribute, x, is the given typed scalar."""
    return {"driver": "gpu.nvidia.com", "attributes": {"x": x}, "capacity": {}}


def _example_device(*, color: str, size: str) -> dict:
    """A resource-driver.example.com device, as in the DRA docs' examples."""
    return {
        "driver": "resource-driver.example.com",
        "attributes": {"color": {"string": color}, "size": {"string": size}},
        "capacity": {},
    }


MATCHES_CASES = [
    # driver.
    Case(
        name="driver equals",
        expr='device.driver == "gpu.nvidia.com"',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="driver not equals",
        expr='device.driver == "nic.nvidia.com"',
        device=_hopper_gpu(),
        want=False,
    ),
    # Quantity comparison + methods.
    Case(
        name="quantity compareTo ge",
        expr='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("141Gi")) >= 0',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="quantity compareTo too big",
        expr='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("200Gi")) >= 0',
        device=_hopper_gpu(),
        want=False,
    ),
    Case(
        name="quantity isGreaterThan",
        expr='device.capacity["gpu.nvidia.com"].memory.isGreaterThan(quantity("80Gi"))',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="quantity isLessThan",
        expr='device.capacity["gpu.nvidia.com"].memory.isLessThan(quantity("200Gi"))',
        device=_hopper_gpu(),
        want=True,
    ),
    # Upstream sign is global-only, so q.sign() is a compile error there. This
    # pins the member form we accept anyway, a documented divergence in cel.py
    # that only makes us more permissive.
    Case(
        name="quantity sign",
        expr='device.capacity["gpu.nvidia.com"].memory.sign() == 1',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="quantity asInteger",
        expr='device.capacity["gpu.nvidia.com"].memory.asInteger() == 151397597184',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="quantity isInteger",
        expr='device.capacity["gpu.nvidia.com"].memory.isInteger()',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="quantity add",
        expr='device.capacity["gpu.nvidia.com"].memory.add(quantity("1Gi")).compareTo(quantity("142Gi")) == 0',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="isQuantity true",
        expr='isQuantity("1.3Gi")',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="isQuantity false",
        expr='isQuantity("200K")',
        device=_hopper_gpu(),
        want=False,
    ),
    # Semver comparison + methods.
    Case(
        name="semver isGreaterThan",
        expr='device.attributes["gpu.nvidia.com"].cudaComputeCapability.isGreaterThan(semver("9.0.0"))',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="semver not greater",
        expr='device.attributes["gpu.nvidia.com"].cudaComputeCapability.isGreaterThan(semver("9.9.0"))',
        device=_hopper_gpu(),
        want=False,
    ),
    Case(
        name="semver major",
        expr='device.attributes["gpu.nvidia.com"].cudaComputeCapability.major() == 9',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="semver minor",
        expr='device.attributes["gpu.nvidia.com"].cudaComputeCapability.minor() == 5',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="semver patch",
        expr='device.attributes["gpu.nvidia.com"].cudaComputeCapability.patch() == 3',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="semver equality",
        expr='device.attributes["gpu.nvidia.com"].cudaComputeCapability == semver("9.5.3")',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="isSemver strict true",
        expr='isSemver("1.0.0")',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="isSemver strict rejects short",
        expr='isSemver("1.0")',
        device=_hopper_gpu(),
        want=False,
    ),
    Case(
        name="isSemver normalize accepts short",
        expr='isSemver("1.0", true)',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="semver normalize overload",
        expr='semver("v1.0", true).major() == 1',
        device=_hopper_gpu(),
        want=True,
    ),
    # Typed scalar attributes (resolve straight to the value, no .string).
    Case(
        name="string attribute",
        expr='device.attributes["gpu.nvidia.com"].architecture == "Hopper"',
        device=_hopper_gpu(),
        want=True,
    ),
    Case(
        name="string attribute under a non-GPU driver's domain",
        expr='device.attributes["nic.nvidia.com"].linkType == "infiniband"',
        device={"driver": "nic.nvidia.com", "attributes": {"linkType": {"string": "infiniband"}}, "capacity": {}},
        want=True,
    ),
    Case(
        name="bool attribute true",
        expr='device.attributes["gpu.nvidia.com"].x',
        device=_scalar_device(x={"bool": True}),
        want=True,
    ),
    Case(
        name="bool attribute false",
        expr='device.attributes["gpu.nvidia.com"].x',
        device=_scalar_device(x={"bool": False}),
        want=False,
    ),
    Case(
        name="int attribute",
        expr='device.attributes["gpu.nvidia.com"].x >= 8',
        device=_scalar_device(x={"int": 8}),
        want=True,
    ),
    Case(
        name="int attribute below",
        expr='device.attributes["gpu.nvidia.com"].x >= 8',
        device=_scalar_device(x={"int": 4}),
        want=False,
    ),
    # Qualified names split into their own domain.
    Case(
        name="qualified name under its domain",
        expr='device.attributes["resource.kubernetes.io"].pcieRoot == "pci0"',
        device=_hopper_gpu(),
        want=True,
    ),
    # The same input as "string attribute", kept to mirror upstream's
    # separate driver-name-qualifier row.
    Case(
        name="bare name under driver domain",
        expr='device.attributes["gpu.nvidia.com"].architecture == "Hopper"',
        device=_hopper_gpu(),
        want=True,
    ),
    # Non-matches that must not raise.
    Case(
        name="two-component version is non-match",
        expr='device.attributes["gpu.nvidia.com"].cudaComputeCapability.isGreaterThan(semver("8.0.0"))',
        device={
            "driver": "gpu.nvidia.com",
            "attributes": {"cudaComputeCapability": {"version": "9.0"}},
            "capacity": {},
        },
        want=False,
    ),
    Case(
        name="malformed quantity is non-match",
        expr='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("1Gi")) >= 0',
        device={"driver": "gpu.nvidia.com", "attributes": {}, "capacity": {"memory": {"value": "10Mo"}}},
        want=False,
    ),
    Case(
        name="unknown id is non-match",
        expr='device.attributes["gpu.nvidia.com"].nope == "x"',
        device=_hopper_gpu(),
        want=False,
    ),
    # A non-bool selector must not spuriously match. Upstream rejects it
    # at compile time; we treat a non-bool result as a non-match.
    Case(
        name="non-bool string selector is non-match",
        expr='"5"',
        device=_hopper_gpu(),
        want=False,
    ),
    Case(
        name="non-bool int selector is non-match",
        expr='device.attributes["gpu.nvidia.com"].x',
        device=_scalar_device(x={"int": 5}),
        want=False,
    ),
    # Domain presence. Upstream's domain-presence idiom is "<domain>" in
    # device.attributes, not has(device.attributes["<domain>"]): cel-go's
    # has() macro rejects an index argument, so the has() form is a
    # compile error on a real cluster (celpy accepts it - see cel.py's
    # documented divergences). An unknown domain is simply absent (False),
    # not present-but-empty.
    Case(
        name="unknown domain absent",
        expr='"other.com" in device.attributes',
        device=_hopper_gpu(),
        want=False,
    ),
    Case(
        name="known domain present",
        expr='"gpu.nvidia.com" in device.attributes',
        device=_hopper_gpu(),
        want=True,
    ),
    # Reading an unknown domain resolves to an empty map (not an error),
    # so an id lookup under it is a non-match rather than a failure.
    Case(
        name="unknown domain id is non-match",
        expr='device.attributes["other.com"].x == "y"',
        device=_hopper_gpu(),
        want=False,
    ),
    # Guard a domain read with the in idiom before indexing it.
    Case(
        name="guarded known domain",
        expr='"gpu.nvidia.com" in device.attributes && device.attributes["gpu.nvidia.com"].architecture == "Hopper"',
        device=_hopper_gpu(),
        want=True,
    ),
    # The full design selector.
    Case(
        name="full design expression",
        expr=(
            'device.attributes["gpu.nvidia.com"].cudaComputeCapability.isGreaterThan(semver("9.0.0")) && '
            'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("141Gi")) >= 0'
        ),
        device=_hopper_gpu(),
        want=True,
    ),
    # Verbatim selector examples from the DRA docs (the k8s.io concept page
    # and the allocate-devices-dra task page), each against a device that
    # should match, and all but small-white against one that shouldn't.
    Case(
        name="docs: large-black subrequest matches",
        expr=(
            'device.attributes["resource-driver.example.com"].color == "black" && '
            'device.attributes["resource-driver.example.com"].size == "large"'
        ),
        device=_example_device(color="black", size="large"),
        want=True,
    ),
    Case(
        name="docs: large-black subrequest rejects small-white",
        expr=(
            'device.attributes["resource-driver.example.com"].color == "black" && '
            'device.attributes["resource-driver.example.com"].size == "large"'
        ),
        device=_example_device(color="white", size="small"),
        want=False,
    ),
    Case(
        name="docs: small-white subrequest matches",
        expr=(
            'device.attributes["resource-driver.example.com"].color == "white" && '
            'device.attributes["resource-driver.example.com"].size == "small"'
        ),
        device=_example_device(color="white", size="small"),
        want=True,
    ),
    Case(
        name="docs: extended-resource DeviceClass selector matches",
        expr="device.driver == 'gpu.example.com' && device.attributes['gpu.example.com'].type == 'gpu'",
        device={"driver": "gpu.example.com", "attributes": {"type": {"string": "gpu"}}, "capacity": {}},
        want=True,
    ),
    Case(
        name="docs: extended-resource DeviceClass selector rejects other driver",
        expr="device.driver == 'gpu.example.com' && device.attributes['gpu.example.com'].type == 'gpu'",
        device={"driver": "nic.nvidia.com", "attributes": {"linkType": {"string": "infiniband"}}, "capacity": {}},
        want=False,
    ),
    Case(
        name="docs: ResourceClaim type+memory selector matches",
        expr=(
            'device.attributes["driver.example.com"].type == "gpu" && '
            'device.capacity["driver.example.com"].memory == quantity("64Gi")'
        ),
        device={
            "driver": "driver.example.com",
            "attributes": {"type": {"string": "gpu"}},
            "capacity": {"memory": {"value": "64Gi"}},
        },
        want=True,
    ),
    Case(
        name="docs: ResourceClaim type+memory selector rejects wrong memory",
        expr=(
            'device.attributes["driver.example.com"].type == "gpu" && '
            'device.capacity["driver.example.com"].memory == quantity("64Gi")'
        ),
        device={
            "driver": "driver.example.com",
            "attributes": {"type": {"string": "gpu"}},
            "capacity": {"memory": {"value": "32Gi"}},
        },
        want=False,
    ),
]


@pytest.mark.parametrize("case", MATCHES_CASES, ids=lambda case: case.name)
def test_matches(case: Case) -> None:
    """A DRA CEL selector matches a device as it does upstream."""
    got = cel.Program(case.expr).matches(case.device)
    assert got == case.want


def test_compile_invalid_expression_raises() -> None:
    """A malformed expression fails to compile."""
    with pytest.raises(cel.CELCompileError, match=r"not \) valid \("):
        cel.Program("not ) valid (")
