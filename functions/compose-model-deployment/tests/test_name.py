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
import unittest

from function import name


@dataclasses.dataclass
class Case:
    name: str
    visible: str
    discriminators: tuple[str, ...]
    want: str


class TestOpaqueName(unittest.TestCase):
    def test_opaque_name(self) -> None:
        """The visible name reads first, then a hash over it and the
        discriminators. The hashes are sha256 prefixes computed outside the code
        under test."""
        cases = [
            Case(
                name="a short name keeps its dots",
                visible="qwen2.5",
                discriminators=("cluster-a", "0"),
                want="qwen2.5-659ed",
            ),
            Case(
                # A '.' left next to the '-' before the hash would make the name
                # an invalid subdomain, which the API server rejects.
                name="a truncated prefix drops a trailing dot",
                visible="a" * 56 + ".b",
                discriminators=("cluster-a", "0"),
                want="a" * 56 + "-83029",
            ),
        ]
        for case in cases:
            with self.subTest(case.name):
                self.assertEqual(case.want, name.opaque_name(case.visible, *case.discriminators))
