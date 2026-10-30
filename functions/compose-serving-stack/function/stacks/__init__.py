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

"""The serving stack component lists, and the join that selects them.

A cloud and a stack name the lists to join: the cloud half from
clouds/ (written into clouds/generated/ by a build-time generator
where one covers the cloud, hand-written at the package's top where
none does), common.py for the components on every stack, and the
stack's own file. See design/serving-stack-generation.md.
"""

from function.stacks import common, components, dynamo, metrics, standard
from function.stacks.clouds import civo, existing, nebius, vultr
from function.stacks.clouds.generated.aicr import aks, eks, gke
from function.stacks.components import Chart, Cloud, Component, Manifests, Stack
from function.stacks.metrics import BUILTIN_MAPPINGS

__all__ = [
    "BUILTIN_MAPPINGS",
    "Chart",
    "Cloud",
    "Component",
    "Manifests",
    "Stack",
    "clouds",
    "components",
    "join",
    "metrics",
    "stacks",
    "supported_drivers",
]

# The cloud halves, keyed by the InferenceCluster's source values. EKS,
# AKS and GKE come from clouds/generated/aicr/, written by
# `nix run .#stacks`; the rest are hand-written in clouds/.
_CLOUDS: dict[Cloud, list[Component]] = {
    "EKS": eks.COMPONENTS,
    "AKS": aks.COMPONENTS,
    "GKE": gke.COMPONENTS,
    "Nebius": nebius.COMPONENTS,
    "Vultr": vultr.COMPONENTS,
    "Civo": civo.COMPONENTS,
    "Existing": existing.COMPONENTS,
}

_STACKS: dict[Stack, list[Component]] = {
    "Standard": standard.COMPONENTS,
    "Dynamo": dynamo.COMPONENTS,
}

# DRA drivers a cloud supports that arrive outside the serving stack, so
# the filter installs nothing for them. The generated modules' constants
# are emitted by generate.py, where the EKS dra.net declaration lives
# beside the aws-efa DROP entry that records why. Existing is the
# open-ended case, special-cased in join() instead - its docstring
# states the rule.
_EXTERNAL_DRIVERS: dict[Cloud, frozenset[str]] = {
    "EKS": frozenset(eks.EXTERNAL_DRIVERS),
    "AKS": frozenset(aks.EXTERNAL_DRIVERS),
    "GKE": frozenset(gke.EXTERNAL_DRIVERS),
    "Nebius": frozenset(nebius.EXTERNAL_DRIVERS),
    "Vultr": frozenset(vultr.EXTERNAL_DRIVERS),
    "Civo": frozenset(civo.EXTERNAL_DRIVERS),
    "Existing": frozenset(existing.EXTERNAL_DRIVERS),
}


def clouds() -> list[Cloud]:
    """The clouds a stack can be joined for."""
    return list(_CLOUDS)


def stacks() -> list[Stack]:
    """The stacks a stack can be joined for."""
    return list(_STACKS)


def supported_drivers(cloud: Cloud) -> frozenset[str]:
    """The DRA drivers a cloud's serving stack supports.

    Derived rather than declared: the union of the dra_drivers tags
    across the cloud's component list, common.py and every stack half,
    plus the drivers the cloud declares installed outside the stack
    (_EXTERNAL_DRIVERS), so the table can't drift from what the lists
    record. compose-inference-cluster mirrors this by hand in its
    _SUPPORTED_DRA_DRIVERS to reject unsupported pairings with a
    condition before a ServingStack is ever composed, and the
    supported-drivers-mirror flake check holds the two in sync.
    Existing's tags are returned like any cloud's, but a driver beyond
    them is not unsupported there: the user owns the cluster, so join()
    never rejects on Existing.
    """
    halves = [_CLOUDS[cloud], common.COMPONENTS, *_STACKS.values()]
    tagged = {driver for half in halves for c in half for driver in c.dra_drivers}
    return frozenset(tagged) | _EXTERNAL_DRIVERS[cloud]


def join(cloud: Cloud, stack: Stack, dra_drivers: list[str] | None = None) -> list[Component]:
    """Join the component lists for a cloud and stack.

    Fails closed, at import or test time rather than on a cluster: on an
    unknown cloud or stack, on a key two lists both produce, on a
    depends_on edge naming a component the join didn't produce - which
    catches a generator allowlist that dropped something another
    component needs - and on a dependency that is neither untagged nor
    tagged with every driver its dependent carries, which filtering
    could then remove from under it.

    dra_drivers filters the tagged components: a tagged component
    survives only when one of its drivers is listed, an untagged one
    always does. None disables filtering, for the tests and integrity
    checks that inspect the full join; the function always passes a
    list. The integrity checks run on the unfiltered join, so a broken
    list fails every join for its cloud, not just the driver
    combination that trips it.

    A listed driver the cloud neither tags components with nor declares
    external raises - except on Existing, where the user owns the
    cluster and may have installed any driver, so an unlisted one is
    treated as externally installed: it installs nothing and is never
    rejected, and a typo surfaces when its ResourceClaim can't
    allocate. This docstring is where both that rule and the
    dependency-tagging one above are stated; the other mentions point
    here.
    """
    if cloud not in _CLOUDS:
        raise ValueError(f"unknown cloud {cloud!r}; known: {', '.join(_CLOUDS)}")
    if stack not in _STACKS:
        raise ValueError(f"unknown stack {stack!r}; known: {', '.join(_STACKS)}")

    joined = [*_CLOUDS[cloud], *common.COMPONENTS, *_STACKS[stack]]

    keys = [c.key for c in joined]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise ValueError(f"{cloud}/{stack}: duplicate component keys {duplicates}")

    # The composed-resource keys a component renders under (one per
    # manifest for a multi-doc bundle) must be unique across the join
    # too, or two components would fight over one desired resource.
    rendered = [k for c in joined for k in components.doc_keys(c)]
    duplicates = sorted({k for k in rendered if rendered.count(k) > 1})
    if duplicates:
        raise ValueError(f"{cloud}/{stack}: duplicate composed-resource keys {duplicates}")

    by_key = {c.key: c for c in joined}
    for c in joined:
        for dep in c.depends_on:
            if dep not in by_key:
                raise ValueError(f"{cloud}/{stack}: {c.key} depends on {dep!r}, which the join did not produce")
            # The docstring's dependency-tagging rule. The
            # `c.dra_drivers and` leg: an untagged dependent is kept by
            # every filter, which no tagged dependency survives.
            dep_drivers = by_key[dep].dra_drivers
            if dep_drivers and not (c.dra_drivers and set(c.dra_drivers) <= set(dep_drivers)):
                raise ValueError(
                    f"{cloud}/{stack}: {c.key} (drivers {c.dra_drivers or 'untagged'}) depends on "
                    f"{dep!r} (drivers {dep_drivers}), which filtering could remove from under it"
                )

    if dra_drivers is None:
        return joined

    if cloud != "Existing":
        unknown = set(dra_drivers) - supported_drivers(cloud)
        if unknown:
            raise ValueError(
                f"{cloud}/{stack}: no device stack serves {', '.join(sorted(unknown))}; "
                f"the {cloud} serving stack supports {', '.join(sorted(supported_drivers(cloud)))}"
            )

    wanted = set(dra_drivers)
    return [c for c in joined if not c.dra_drivers or wanted & set(c.dra_drivers)]
