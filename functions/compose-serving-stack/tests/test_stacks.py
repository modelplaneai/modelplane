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

    def test_multi_doc_manifests_derive_per_doc_keys(self) -> None:
        for cloud in stacks.clouds():
            for stack in stacks.stacks():
                for c in stacks.components(cloud, stack):
                    keys = stacks.doc_keys(c)
                    if isinstance(c, stacks.Chart) or len(c.manifests) == 1:
                        self.assertEqual([c.key], keys)
                        continue
                    with self.subTest(cloud=cloud, stack=stack, key=c.key):
                        self.assertEqual(
                            [f"{c.key}-{doc['metadata']['name']}" for doc in c.manifests],
                            keys,
                            "a multi-doc bundle renders one Object per doc, keyed <key>-<name>",
                        )

    def test_ready_entries_are_single_doc(self) -> None:
        # A readiness CEL query applies to every doc in an entry, so an
        # entry carrying one keeps to a single manifest - a Service or
        # ServiceAccount has no status conditions to satisfy it.
        for cloud in stacks.clouds():
            for stack in stacks.stacks():
                for c in stacks.components(cloud, stack):
                    if isinstance(c, stacks.Manifests) and c.ready is not None:
                        with self.subTest(cloud=cloud, stack=stack, key=c.key):
                            self.assertEqual(1, len(c.manifests))

    def test_depended_on_charts_wait(self) -> None:
        # A chart another component depends on renders with helm --wait,
        # so its Ready means healthy and the install gate orders
        # dependents on health rather than deploy. Without this, the
        # gate would open the moment Helm accepted the manifests.
        for cloud in stacks.clouds():
            for stack in stacks.stacks():
                joined = stacks.components(cloud, stack)
                depended_on = {dep for c in joined for dep in c.depends_on}
                for c in joined:
                    if isinstance(c, stacks.Chart) and c.key in depended_on:
                        with self.subTest(cloud=cloud, stack=stack, key=c.key):
                            self.assertTrue(c.wait, "a depended-on chart must set wait")

    def test_unknown_cloud_and_stack_fail_closed(self) -> None:
        # The Literal types reject these at type-checking time; this
        # exercises the runtime guard behind them, which catches the API
        # and the stacks package disagreeing on a value.
        with self.assertRaises(ValueError):
            stacks.components("Mars", "Standard")  # ty: ignore[invalid-argument-type]
        with self.assertRaises(ValueError):
            stacks.components("Nebius", "Turbo")  # ty: ignore[invalid-argument-type]
