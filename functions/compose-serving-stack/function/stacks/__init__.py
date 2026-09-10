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

from function.stacks import common, components, dynamo, standard
from function.stacks.clouds import existing, nebius, vultr, vultr_baremetal
from function.stacks.clouds.generated.aicr import aks, eks, gke
from function.stacks.components import AcceleratorVendor, Chart, Cloud, Component, Manifests, Stack

__all__ = [
    "ACCELERATOR_VENDORS",
    "AcceleratorVendor",
    "Chart",
    "Cloud",
    "Component",
    "Manifests",
    "Stack",
    "clouds",
    "components",
    "join",
    "stacks",
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
    "VultrBaremetal": vultr_baremetal.COMPONENTS,
    "Existing": existing.COMPONENTS,
}

_STACKS: dict[Stack, list[Component]] = {
    "Standard": standard.COMPONENTS,
    "Dynamo": dynamo.COMPONENTS,
}

# The accelerator vendors each cloud's serving stack can install: the
# vendors its component list carries a device stack for. A
# single-vendor cloud keeps its accelerator components untagged (there
# is nothing to filter); a multi-vendor cloud tags them and the join
# filters by ServingStack spec.accelerators. Existing is BYO: the
# cluster's operator manages the accelerator stack, so any vendor goes.
# compose-inference-cluster mirrors this table to reject unsupported
# pairings with a condition before a ServingStack is ever composed.
ACCELERATOR_VENDORS: dict[Cloud, list[AcceleratorVendor]] = {
    "EKS": ["NVIDIA"],
    "AKS": ["NVIDIA"],
    "GKE": ["NVIDIA"],
    "Nebius": ["NVIDIA"],
    "Vultr": ["NVIDIA"],
    "VultrBaremetal": ["AMD", "NVIDIA"],
    "Existing": ["AMD", "NVIDIA"],
}


def clouds() -> list[Cloud]:
    """The clouds a stack can be joined for."""
    return list(_CLOUDS)


def stacks() -> list[Stack]:
    """The stacks a stack can be joined for."""
    return list(_STACKS)


def join(cloud: Cloud, stack: Stack, accelerator_vendors: list[AcceleratorVendor] | None = None) -> list[Component]:
    """Join the component lists for a cloud and stack.

    Fails closed, at import or test time rather than on a cluster: on an
    unknown cloud or stack, on a key two lists both produce, on a
    depends_on edge naming a component the join didn't produce - which
    catches a generator allowlist that dropped something another
    component needs - and on a component depending on another vendor's
    accelerator stack, which vendor filtering could then remove from
    under it.

    accelerator_vendors filters the vendor-tagged components: a tagged
    component survives only when its vendor is listed, an untagged one
    always does. None (the field unset on the XR) disables filtering, so
    clouds that don't set it keep installing everything. The integrity
    checks run on the unfiltered join, so a broken list fails every join
    for its cloud, not just the vendor combination that trips it.
    """
    if cloud not in _CLOUDS:
        raise ValueError(f"unknown cloud {cloud!r}; known: {', '.join(_CLOUDS)}")
    if stack not in _STACKS:
        raise ValueError(f"unknown stack {stack!r}; known: {', '.join(_STACKS)}")
    for vendor in accelerator_vendors or []:
        if vendor not in ACCELERATOR_VENDORS[cloud]:
            raise ValueError(
                f"{cloud}: the serving stack has no {vendor} accelerator stack; "
                f"it installs {', '.join(ACCELERATOR_VENDORS[cloud])}"
            )

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
            # A dependency tagged for another vendor would be filtered
            # away while its dependent survives, leaving a dangling edge.
            dep_vendor = by_key[dep].accelerator_vendor
            if dep_vendor is not None and dep_vendor != c.accelerator_vendor:
                raise ValueError(
                    f"{cloud}/{stack}: {c.key} ({c.accelerator_vendor or 'untagged'}) depends on "
                    f"{dep!r}, which is tagged {dep_vendor}"
                )

    if accelerator_vendors is None:
        return joined
    return [c for c in joined if c.accelerator_vendor is None or c.accelerator_vendor in accelerator_vendors]
