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
from function.stacks.clouds import civo


class TestComponents(unittest.TestCase):
    def test_every_cloud_and_stack_joins(self) -> None:
        for cloud in stacks.clouds():
            for stack in stacks.stacks():
                with self.subTest(cloud=cloud, stack=stack):
                    got = stacks.join(cloud, stack)
                    self.assertTrue(got, "a joined stack can't be empty")

    def test_charts_have_reserved_release_names(self) -> None:
        for cloud in stacks.clouds():
            for stack in stacks.stacks():
                for c in stacks.join(cloud, stack):
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
                for c in stacks.join(cloud, stack):
                    if isinstance(c, stacks.Manifests):
                        with self.subTest(cloud=cloud, stack=stack, key=c.key):
                            self.assertTrue(c.manifests, "a Manifests entry can't be empty")

    def test_multi_doc_manifests_derive_per_doc_keys(self) -> None:
        for cloud in stacks.clouds():
            for stack in stacks.stacks():
                for c in stacks.join(cloud, stack):
                    keys = stacks.components.doc_keys(c)
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
                for c in stacks.join(cloud, stack):
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
                joined = stacks.join(cloud, stack)
                depended_on = {dep for c in joined for dep in c.depends_on}
                for c in joined:
                    if isinstance(c, stacks.Chart) and c.key in depended_on:
                        with self.subTest(cloud=cloud, stack=stack, key=c.key):
                            self.assertTrue(c.wait, "a depended-on chart must set wait")

    def test_no_wildcard_tolerations(self) -> None:
        # A keyless toleration tolerates every taint, so the pod lands
        # on tainted GPU nodes: control-plane charts squat on
        # accelerated capacity and their eviction stalls autoscaler
        # scale-down. aicr's bundler stamps exactly that wildcard on
        # every pod it renders; the generator scopes each one
        # (TOLERATIONS in generate.py). This pins that no keyless
        # toleration survives in any joined stack, chart values and
        # manifests alike.
        def check(node: object, where: str) -> None:
            if isinstance(node, dict):
                for key, val in node.items():
                    if key == "tolerations" and isinstance(val, list):
                        for toleration in val:
                            self.assertTrue(
                                isinstance(toleration, dict) and "key" in toleration,
                                f"keyless (wildcard) toleration in {where}",
                            )
                    else:
                        check(val, where)
            elif isinstance(node, list):
                for item in node:
                    check(item, where)

        for cloud in stacks.clouds():
            for stack in stacks.stacks():
                for c in stacks.join(cloud, stack):
                    with self.subTest(cloud=cloud, stack=stack, key=c.key):
                        check(c.values if isinstance(c, stacks.Chart) else c.manifests, c.key)

    def test_required_crd_versions_are_singular(self) -> None:
        # A RequiredCRD renders as one APIService observe Object, which
        # encodes exactly one group-version. join() fails closed on any
        # other count; this documents it against the real lists.
        for cloud in stacks.clouds():
            for stack in stacks.stacks():
                for c in stacks.join(cloud, stack):
                    for r in c.requires:
                        if isinstance(r, stacks.RequiredCRD):
                            with self.subTest(cloud=cloud, stack=stack, key=c.key, req=r.key):
                                self.assertEqual(1, len(r.versions))

    def test_existing_substrate_components_state_requirements(self) -> None:
        # Provided mode can only select Existing, and there every
        # substrate component must say what the cluster supplies in its
        # place, or state why nothing is checkable. join() fails closed
        # on this; the test documents it against the real lists.
        for stack in stacks.stacks():
            for c in stacks.join("Existing", stack):
                if c.role == "substrate":
                    with self.subTest(stack=stack, key=c.key):
                        self.assertTrue(c.requires or c.unchecked)

    def test_unknown_cloud_and_stack_fail_closed(self) -> None:
        # The Literal types reject these at type-checking time; this
        # exercises the runtime guard behind them, which catches the API
        # and the stacks package disagreeing on a value.
        with self.assertRaises(ValueError):
            stacks.join("Mars", "Standard")  # ty: ignore[invalid-argument-type]
        with self.assertRaises(ValueError):
            stacks.join("Nebius", "Turbo")  # ty: ignore[invalid-argument-type]


class TestWithNvLinkDisabled(unittest.TestCase):
    """with_nvlink_disabled scopes NVLink disable to the named pools."""

    def test_gpu_operator_switches_to_nvidia_driver_crd(self) -> None:
        # The chart's default NVIDIADriver (deployDefaultCR) keeps driving
        # pools the transform doesn't name, so flipping modes changes
        # nothing for them.
        got = civo.with_nvlink_disabled(stacks.join("Civo", "Standard"), ["h100-pool"])
        op = next(c for c in got if isinstance(c, stacks.Chart) and c.key == "gpu-operator")
        assert op.values is not None
        self.assertEqual({"enabled": True, "deployDefaultCR": True}, op.values["driver"]["nvidiaDriverCRD"])

    def test_each_pool_gets_its_own_driver(self) -> None:
        got = civo.with_nvlink_disabled(stacks.join("Civo", "Standard"), ["pool-a", "pool-b"])
        drivers = [c for c in got if isinstance(c, stacks.Manifests) and c.key.startswith("nvlink-disabled-driver-")]
        self.assertEqual(["nvlink-disabled-driver-pool-a", "nvlink-disabled-driver-pool-b"], [c.key for c in drivers])
        for c, pool in zip(drivers, ["pool-a", "pool-b"], strict=True):
            with self.subTest(pool=pool):
                # Ready entries keep to a single doc, and gate on the
                # operator-populated state so the DRA driver's install
                # gate orders on driver health.
                self.assertEqual(1, len(c.manifests))
                self.assertIsNotNone(c.ready)
                self.assertEqual(["gpu-operator", "nvlink-disable-config"], c.depends_on)
                spec = c.manifests[0]["spec"]
                self.assertEqual({"modelplane.ai/pool": pool}, spec["nodeSelector"])
                self.assertEqual({"name": "nvidia-kernel-config"}, spec["kernelModuleConfig"])

    def test_driver_pin_mirrors_the_chart(self) -> None:
        # One review moves both: the per-pool NVIDIADriver must install
        # the same driver the chart's default CR does.
        got = civo.with_nvlink_disabled(stacks.join("Civo", "Standard"), ["h100-pool"])
        op = next(c for c in got if isinstance(c, stacks.Chart) and c.key == "gpu-operator")
        driver = next(c for c in got if isinstance(c, stacks.Manifests) and c.key == "nvlink-disabled-driver-h100-pool")
        assert op.values is not None
        spec = driver.manifests[0]["spec"]
        self.assertEqual(op.values["driver"]["version"], spec["version"])
        self.assertEqual(op.values["driver"]["useOpenKernelModules"], spec["useOpenKernelModules"])

    def test_configmap_carries_the_module_option(self) -> None:
        got = civo.with_nvlink_disabled(stacks.join("Civo", "Standard"), ["h100-pool"])
        config = next(c for c in got if isinstance(c, stacks.Manifests) and c.key == "nvlink-disable-config")
        kinds = [doc["kind"] for doc in config.manifests]
        self.assertEqual(["Namespace", "ConfigMap"], kinds)
        self.assertEqual(
            {"nvidia.conf": "options nvidia NVreg_NvLinkDisable=1"},
            config.manifests[1]["data"],
        )

    def test_dra_driver_gates_on_pool_drivers(self) -> None:
        got = civo.with_nvlink_disabled(stacks.join("Civo", "Standard"), ["pool-a", "pool-b"])
        dra = next(c for c in got if isinstance(c, stacks.Chart) and c.key == "nvidia-dra-driver-gpu")
        self.assertEqual(
            ["gpu-operator", "nvlink-disabled-driver-pool-a", "nvlink-disabled-driver-pool-b"],
            dra.depends_on,
        )

    def test_join_is_not_mutated(self) -> None:
        # The transform must copy: the joined lists share the module-level
        # component objects, and mutating them would leak NVLink disable
        # into every later request.
        civo.with_nvlink_disabled(stacks.join("Civo", "Standard"), ["h100-pool"])
        joined = stacks.join("Civo", "Standard")
        op = next(c for c in joined if isinstance(c, stacks.Chart) and c.key == "gpu-operator")
        dra = next(c for c in joined if isinstance(c, stacks.Chart) and c.key == "nvidia-dra-driver-gpu")
        assert op.values is not None
        self.assertNotIn("nvidiaDriverCRD", op.values["driver"])
        self.assertEqual(["gpu-operator"], dra.depends_on)
