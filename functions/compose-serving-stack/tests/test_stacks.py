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

The join itself is the assertion: components() fails closed on
duplicate keys and on depends_on edges the join didn't produce, so
iterating every cloud and stack pair gates every list - including the
generated ones, once mapped - without involving fn.py.
"""

import unittest

from function import stacks


class TestComponents(unittest.TestCase):
    def test_every_cloud_and_stack_joins(self) -> None:
        for cloud in stacks.clouds():
            for stack in stacks.stacks():
                with self.subTest(cloud=cloud, stack=stack):
                    got = stacks.components(cloud, stack)
                    self.assertTrue(got, "a joined stack can't be empty")

    def test_charts_have_reserved_release_names(self) -> None:
        for cloud in stacks.clouds():
            for stack in stacks.stacks():
                for c in stacks.components(cloud, stack):
                    if isinstance(c, stacks.Chart):
                        with self.subTest(cloud=cloud, stack=stack, key=c.key):
                            self.assertEqual(
                                f"mp-{c.chart}",
                                c.release,
                                "release names are mp-<chart>: stable across upgrades, reserved to Modelplane",
                            )

    def test_manifests_are_populated(self) -> None:
        for cloud in stacks.clouds():
            for stack in stacks.stacks():
                for c in stacks.components(cloud, stack):
                    if isinstance(c, stacks.Manifests):
                        with self.subTest(cloud=cloud, stack=stack, key=c.key):
                            self.assertTrue(c.manifests, "a Manifests entry can't be empty")

    def test_unknown_cloud_and_stack_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            stacks.components("Mars", "Standard")
        with self.assertRaises(ValueError):
            stacks.components("Nebius", "Turbo")


if __name__ == "__main__":
    unittest.main()
