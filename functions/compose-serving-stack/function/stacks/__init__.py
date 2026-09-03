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
clouds/ (generated into clouds/generated/aicr/ where AICR covers the
cloud, hand-written at the package's top where it doesn't), common.py
for the components on every stack, and the stack's own file. See
design/serving-stack-generation.md.

clouds/amd.py is a cloud half for a non-NVIDIA accelerator - one more
value of the cloud axis. No cluster type maps to it yet, so it's absent
from the join tables.
"""

from function.stacks import common, dynamo, standard
from function.stacks.clouds import existing, nebius, vultr
from function.stacks.clouds.generated.aicr import aks, eks, gke
from function.stacks.components import Chart, Component, Manifests, doc_keys

__all__ = [
    "Chart",
    "Component",
    "Manifests",
    "clouds",
    "components",
    "doc_keys",
    "stacks",
]

# The cloud halves, keyed by the InferenceCluster's source values. EKS,
# AKS and GKE come from clouds/generated/aicr/, written by
# `nix run .#stacks`; the rest are hand-written in clouds/.
_CLOUDS: dict[str, list[Component]] = {
    "EKS": eks.COMPONENTS,
    "AKS": aks.COMPONENTS,
    "GKE": gke.COMPONENTS,
    "Nebius": nebius.COMPONENTS,
    "Vultr": vultr.COMPONENTS,
    "Existing": existing.COMPONENTS,
}

_STACKS: dict[str, list[Component]] = {
    "Standard": standard.COMPONENTS,
    "Dynamo": dynamo.COMPONENTS,
}


def clouds() -> list[str]:
    """The clouds a stack can be joined for."""
    return list(_CLOUDS)


def stacks() -> list[str]:
    """The stacks a stack can be joined for."""
    return list(_STACKS)


def components(cloud: str, stack: str) -> list[Component]:
    """Join the component lists for a cloud and stack.

    Fails closed, at import or test time rather than on a cluster: on an
    unknown cloud or stack, on a key two lists both produce, and on a
    depends_on edge naming a component the join didn't produce - which
    catches a generator allowlist that dropped something another
    component needs.
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
    rendered = [k for c in joined for k in doc_keys(c)]
    duplicates = sorted({k for k in rendered if rendered.count(k) > 1})
    if duplicates:
        raise ValueError(f"{cloud}/{stack}: duplicate composed-resource keys {duplicates}")

    known = set(keys)
    for c in joined:
        for dep in c.depends_on:
            if dep not in known:
                raise ValueError(f"{cloud}/{stack}: {c.key} depends on {dep!r}, which the join did not produce")

    return joined
