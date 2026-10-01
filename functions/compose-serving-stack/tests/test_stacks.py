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
ones, once mapped - without involving fn.py. The rest are properties
every joined stack must hold, each checked over the whole stack at once
so a failure lists every component that breaks it.
"""

import dataclasses

import pytest
from function import stacks


@dataclasses.dataclass
class Case:
    """A test case for stacks.join."""

    name: str
    cloud: str
    stack: str
    want: str


@pytest.mark.parametrize("stack", stacks.stacks())
@pytest.mark.parametrize("cloud", stacks.clouds())
def test_every_cloud_and_stack_joins(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
    """Every cloud and stack pair joins into a non-empty stack."""
    assert stacks.join(cloud, stack), "a joined stack can't be empty"


@pytest.mark.parametrize("stack", stacks.stacks())
@pytest.mark.parametrize("cloud", stacks.clouds())
def test_charts_have_reserved_release_names(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
    """Every Chart's Helm release is named mp-<chart>."""
    charts = [c for c in stacks.join(cloud, stack) if isinstance(c, stacks.Chart)]
    assert {c.key: c.release for c in charts} == {c.key: f"mp-{c.chart}" for c in charts}, (
        "release names are mp-<chart>: stable across upgrades, reserved to Modelplane"
    )


@pytest.mark.parametrize("stack", stacks.stacks())
@pytest.mark.parametrize("cloud", stacks.clouds())
def test_manifests_are_populated(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
    """Every Manifests entry carries at least one manifest."""
    empty = [c.key for c in stacks.join(cloud, stack) if isinstance(c, stacks.Manifests) and not c.manifests]
    assert empty == [], "a Manifests entry can't be empty"


@pytest.mark.parametrize("stack", stacks.stacks())
@pytest.mark.parametrize("cloud", stacks.clouds())
def test_multi_doc_manifests_derive_per_doc_keys(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
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
def test_ready_entries_are_single_doc(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
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
def test_depended_on_charts_wait(cloud: stacks.Cloud, stack: stacks.Stack) -> None:
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
    # manifests alike.
    wildcards = []

    def check(node: object, where: str) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key == "tolerations" and isinstance(val, list):
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
    Case(name="unknown cloud", cloud="Mars", stack="Standard", want="unknown cloud 'Mars'"),
    Case(name="unknown stack", cloud="Nebius", stack="Turbo", want="unknown stack 'Turbo'"),
]


@pytest.mark.parametrize("case", JOIN_FAILS_CLOSED_CASES, ids=lambda case: case.name)
def test_join_fails_closed(case: Case) -> None:
    """join rejects a cloud or stack it doesn't know."""
    with pytest.raises(ValueError, match=case.want):
        stacks.join(case.cloud, case.stack)  # ty: ignore[invalid-argument-type]  # cases pass values outside the Literals
