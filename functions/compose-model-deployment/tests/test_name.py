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

"""Tests for the name module."""

import dataclasses

import pytest
from function import name


@dataclasses.dataclass
class OpaqueNameCase:
    """A test case for name.opaque_name."""

    name: str
    reason: str
    visible: str
    discriminators: tuple[str, ...]
    want: str


OPAQUE_NAME_CASES = [
    OpaqueNameCase(
        name="ShortName",
        reason="A short visible name keeps its dots, followed by a hash over it and the discriminators.",
        visible="qwen2.5",
        discriminators=("cluster-a", "0"),
        want="qwen2.5-659ed",
    ),
    # A '.' left next to the '-' before the hash would make the name an invalid
    # subdomain, which the API server rejects. The lengths are the point, so
    # the names are written as repeats.
    OpaqueNameCase(
        name="TruncatedOnDot",
        reason="A visible name truncated just after a dot drops the dot as well as any dash.",
        visible="a" * 56 + ".b",
        discriminators=("cluster-a", "0"),
        want="a" * 56 + "-83029",
    ),
]


@pytest.mark.parametrize("case", OPAQUE_NAME_CASES, ids=lambda case: case.name)
def test_opaque_name(case: OpaqueNameCase) -> None:
    """An opaque name reads as the visible name, then a hash over it and the discriminators."""
    assert name.opaque_name(case.visible, *case.discriminators) == case.want, case.reason
