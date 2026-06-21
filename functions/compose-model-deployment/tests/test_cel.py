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
import unittest

from function import cel


def _device(
    driver: str = "gpu.nvidia.com",
    attributes: dict | None = None,
    capacity: dict | None = None,
    **extra: object,
) -> dict:
    """A pool device in the raw dict shape cel.Program.matches expects."""
    return {
        "driver": driver,
        "attributes": attributes or {},
        "capacity": capacity or {},
        **extra,
    }


@dataclasses.dataclass
class Case:
    name: str
    expr: str
    device: dict
    want: bool


# Reusable device fixtures.
_GPU = _device(
    driver="gpu.nvidia.com",
    attributes={
        "architecture": {"string": "Hopper"},
        "cudaComputeCapability": {"version": "9.5.3"},
        # A qualified name lands under its own domain, not the driver's.
        "resource.kubernetes.io/pcieRoot": {"string": "pci0"},
    },
    capacity={"memory": {"value": "141Gi"}},
)
_NIC = _device(driver="nic.nvidia.com", attributes={"linkType": {"string": "infiniband"}})

_ATTR = 'device.attributes["gpu.nvidia.com"]'
_CAP = 'device.capacity["gpu.nvidia.com"]'

# Device fixtures for the verbatim selector examples in the DRA docs.
# (k8s.io/docs concept page and the allocate-devices-dra task page.)
_LARGE_BLACK = _device(
    driver="resource-driver.example.com",
    attributes={"color": {"string": "black"}, "size": {"string": "large"}},
)
_SMALL_WHITE = _device(
    driver="resource-driver.example.com",
    attributes={"color": {"string": "white"}, "size": {"string": "small"}},
)
_EXAMPLE_GPU = _device(
    driver="gpu.example.com",
    attributes={"type": {"string": "gpu"}},
)
_GPU_64GI = _device(
    driver="driver.example.com",
    attributes={"type": {"string": "gpu"}},
    capacity={"memory": {"value": "64Gi"}},
)


class TestMatches(unittest.TestCase):
    def test_matches(self) -> None:
        cases = [
            # driver.
            Case(name="driver equals", expr='device.driver == "gpu.nvidia.com"', device=_GPU, want=True),
            Case(name="driver not equals", expr='device.driver == "nic.nvidia.com"', device=_GPU, want=False),
            # Quantity comparison + methods.
            Case(
                name="quantity compareTo ge",
                expr=f'{_CAP}.memory.compareTo(quantity("141Gi")) >= 0',
                device=_GPU,
                want=True,
            ),
            Case(
                name="quantity compareTo too big",
                expr=f'{_CAP}.memory.compareTo(quantity("200Gi")) >= 0',
                device=_GPU,
                want=False,
            ),
            Case(
                name="quantity isGreaterThan",
                expr=f'{_CAP}.memory.isGreaterThan(quantity("80Gi"))',
                device=_GPU,
                want=True,
            ),
            Case(
                name="quantity isLessThan", expr=f'{_CAP}.memory.isLessThan(quantity("200Gi"))', device=_GPU, want=True
            ),
            Case(name="quantity sign", expr=f"{_CAP}.memory.sign() == 1", device=_GPU, want=True),
            Case(name="quantity asInteger", expr=f"{_CAP}.memory.asInteger() == {141 * 2**30}", device=_GPU, want=True),
            Case(name="quantity isInteger", expr=f"{_CAP}.memory.isInteger()", device=_GPU, want=True),
            Case(
                name="quantity add",
                expr=f'{_CAP}.memory.add(quantity("1Gi")).compareTo(quantity("142Gi")) == 0',
                device=_GPU,
                want=True,
            ),
            Case(name="isQuantity true", expr='isQuantity("1.3Gi")', device=_GPU, want=True),
            Case(name="isQuantity false", expr='isQuantity("200K")', device=_GPU, want=False),
            # Semver comparison + methods.
            Case(
                name="semver isGreaterThan",
                expr=f'{_ATTR}.cudaComputeCapability.isGreaterThan(semver("9.0.0"))',
                device=_GPU,
                want=True,
            ),
            Case(
                name="semver not greater",
                expr=f'{_ATTR}.cudaComputeCapability.isGreaterThan(semver("9.9.0"))',
                device=_GPU,
                want=False,
            ),
            Case(name="semver major", expr=f"{_ATTR}.cudaComputeCapability.major() == 9", device=_GPU, want=True),
            Case(name="semver minor", expr=f"{_ATTR}.cudaComputeCapability.minor() == 5", device=_GPU, want=True),
            Case(name="semver patch", expr=f"{_ATTR}.cudaComputeCapability.patch() == 3", device=_GPU, want=True),
            Case(
                name="semver equality", expr=f'{_ATTR}.cudaComputeCapability == semver("9.5.3")', device=_GPU, want=True
            ),
            Case(name="isSemver strict true", expr='isSemver("1.0.0")', device=_GPU, want=True),
            Case(name="isSemver strict rejects short", expr='isSemver("1.0")', device=_GPU, want=False),
            Case(name="isSemver normalize accepts short", expr='isSemver("1.0", true)', device=_GPU, want=True),
            Case(name="semver normalize overload", expr='semver("v1.0", true).major() == 1', device=_GPU, want=True),
            # Typed scalar attributes (resolve straight to the value, no .string).
            Case(name="string attribute", expr=f'{_ATTR}.architecture == "Hopper"', device=_GPU, want=True),
            Case(
                name="string attribute mismatch",
                expr='device.attributes["nic.nvidia.com"].linkType == "infiniband"',
                device=_NIC,
                want=True,
            ),
            Case(
                name="bool attribute true",
                expr=f"{_ATTR}.x",
                device=_device(attributes={"x": {"bool": True}}),
                want=True,
            ),
            Case(
                name="bool attribute false",
                expr=f"{_ATTR}.x",
                device=_device(attributes={"x": {"bool": False}}),
                want=False,
            ),
            Case(
                name="int attribute",
                expr=f"{_ATTR}.x >= 8",
                device=_device(attributes={"x": {"int": 8}}),
                want=True,
            ),
            Case(
                name="int attribute below",
                expr=f"{_ATTR}.x >= 8",
                device=_device(attributes={"x": {"int": 4}}),
                want=False,
            ),
            # Qualified names split into their own domain.
            Case(
                name="qualified name under its domain",
                expr='device.attributes["resource.kubernetes.io"].pcieRoot == "pci0"',
                device=_GPU,
                want=True,
            ),
            Case(
                name="bare name under driver domain", expr=f'{_ATTR}.architecture == "Hopper"', device=_GPU, want=True
            ),
            # Non-matches that must not raise.
            Case(
                name="two-component version is non-match",
                expr=f'{_ATTR}.cudaComputeCapability.isGreaterThan(semver("8.0.0"))',
                device=_device(attributes={"cudaComputeCapability": {"version": "9.0"}}),
                want=False,
            ),
            Case(
                name="malformed quantity is non-match",
                expr=f'{_CAP}.memory.compareTo(quantity("1Gi")) >= 0',
                device=_device(capacity={"memory": {"value": "10Mo"}}),
                want=False,
            ),
            Case(name="unknown id is non-match", expr=f'{_ATTR}.nope == "x"', device=_GPU, want=False),
            # A non-bool selector must not spuriously match. Upstream rejects it
            # at compile time; we treat a non-bool result as a non-match.
            Case(name="non-bool string selector is non-match", expr='"5"', device=_GPU, want=False),
            Case(
                name="non-bool int selector is non-match",
                expr=f"{_ATTR}.x",
                device=_device(attributes={"x": {"int": 5}}),
                want=False,
            ),
            # Domain presence. Upstream's domain-presence idiom is "<domain>" in
            # device.attributes, not has(device.attributes["<domain>"]): cel-go's
            # has() macro rejects an index argument, so the has() form is a
            # compile error on a real cluster (celpy accepts it - see cel.py's
            # documented divergences). An unknown domain is simply absent (False),
            # not present-but-empty.
            Case(name="unknown domain absent", expr='"other.com" in device.attributes', device=_GPU, want=False),
            Case(name="known domain present", expr='"gpu.nvidia.com" in device.attributes', device=_GPU, want=True),
            # Reading an unknown domain resolves to an empty map (not an error),
            # so an id lookup under it is a non-match rather than a failure.
            Case(
                name="unknown domain id is non-match",
                expr='device.attributes["other.com"].x == "y"',
                device=_GPU,
                want=False,
            ),
            # Guard a domain read with the in idiom before indexing it.
            Case(
                name="guarded known domain",
                expr=f'"gpu.nvidia.com" in device.attributes && {_ATTR}.architecture == "Hopper"',
                device=_GPU,
                want=True,
            ),
            # The full design selector.
            Case(
                name="full design expression",
                expr=(
                    f'{_ATTR}.cudaComputeCapability.isGreaterThan(semver("9.0.0")) && '
                    f'{_CAP}.memory.compareTo(quantity("141Gi")) >= 0'
                ),
                device=_GPU,
                want=True,
            ),
            # Verbatim selector examples from the DRA docs, each against a device
            # that should and should not match.
            Case(
                name="docs: large-black subrequest matches",
                expr=(
                    'device.attributes["resource-driver.example.com"].color == "black" && '
                    'device.attributes["resource-driver.example.com"].size == "large"'
                ),
                device=_LARGE_BLACK,
                want=True,
            ),
            Case(
                name="docs: large-black subrequest rejects small-white",
                expr=(
                    'device.attributes["resource-driver.example.com"].color == "black" && '
                    'device.attributes["resource-driver.example.com"].size == "large"'
                ),
                device=_SMALL_WHITE,
                want=False,
            ),
            Case(
                name="docs: small-white subrequest matches",
                expr=(
                    'device.attributes["resource-driver.example.com"].color == "white" && '
                    'device.attributes["resource-driver.example.com"].size == "small"'
                ),
                device=_SMALL_WHITE,
                want=True,
            ),
            Case(
                name="docs: extended-resource DeviceClass selector matches",
                expr="device.driver == 'gpu.example.com' && device.attributes['gpu.example.com'].type == 'gpu'",
                device=_EXAMPLE_GPU,
                want=True,
            ),
            Case(
                name="docs: extended-resource DeviceClass selector rejects other driver",
                expr="device.driver == 'gpu.example.com' && device.attributes['gpu.example.com'].type == 'gpu'",
                device=_NIC,
                want=False,
            ),
            Case(
                name="docs: ResourceClaim type+memory selector matches",
                expr=(
                    'device.attributes["driver.example.com"].type == "gpu" && '
                    'device.capacity["driver.example.com"].memory == quantity("64Gi")'
                ),
                device=_GPU_64GI,
                want=True,
            ),
            Case(
                name="docs: ResourceClaim type+memory selector rejects wrong memory",
                expr=(
                    'device.attributes["driver.example.com"].type == "gpu" && '
                    'device.capacity["driver.example.com"].memory == quantity("64Gi")'
                ),
                device=_device(
                    driver="driver.example.com",
                    attributes={"type": {"string": "gpu"}},
                    capacity={"memory": {"value": "32Gi"}},
                ),
                want=False,
            ),
        ]
        for case in cases:
            with self.subTest(case.name):
                got = cel.Program(case.expr).matches(case.device)
                self.assertEqual(case.want, got, f"{case.name}: -want, +got")


class TestCompile(unittest.TestCase):
    def test_invalid_expression_raises(self) -> None:
        with self.assertRaises(cel.CELCompileError):
            cel.Program("not ) valid (")


if __name__ == "__main__":
    unittest.main()
