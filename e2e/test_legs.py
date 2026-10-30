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

"""The legs stay true to the cluster compositions they mirror.

The placement tests catch the serving stack drifting from a leg, but a leg is
itself a copy of what its cloud's cluster composition applies, and a copy can
drift too: if compose-aks-cluster changed its taint value, the AKS stack's
Equal/true tolerations would miss real GPU nodes while the leg's fake ones
still matched. These tests need no cluster - they read the compositions'
source - so they live outside e2e/clouds/ (whose fixtures need a live
environment) and run in the e2e-legs flake check, where a change to a cluster
composition triggers them even when nothing under e2e/ changed.
"""

import ast
import pathlib

import pytest

from e2e import clouds

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The cluster composition whose GPU taint each leg copies. No GKE or
# Existing entry: GKE's managed taint comes from GKE itself, and
# Modelplane doesn't taint BYO pools.
COMPOSITIONS = {
    "eks": "compose-eks-cluster",
    "aks": "compose-aks-cluster",
    "civo": "compose-civo-cluster",
    "nebius": "compose-nebius-cluster",
    "vultr": "compose-vultr-cluster",
}


def constants(function: str) -> dict[str, str]:
    """Return a composition module's top-level string constants.

    Parsed from source rather than imported: every function package is
    named `function` and carries its own dependencies, so the modules
    can't be imported here side by side.
    """
    tree = ast.parse((ROOT / "functions" / function / "function" / "fn.py").read_text())
    return {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


@pytest.mark.parametrize("leg", sorted(COMPOSITIONS))
def test_taint_matches_composition(leg: str) -> None:
    """Each leg's fake GPU taint is the one its cluster composition applies."""
    consts = constants(COMPOSITIONS[leg])
    if "_GPU_TAINT_VALUE" in consts:
        key, value = consts["_GPU_TAINT_KEY"], consts["_GPU_TAINT_VALUE"]
    else:
        # compose-aks-cluster declares the whole taint as one
        # key=value:effect string.
        key, rest = consts["_GPU_TAINT"].split("=", 1)
        value = rest.split(":", 1)[0]
    assert key == "nvidia.com/gpu", f"{COMPOSITIONS[leg]} taints a key the legs don't fake"
    assert clouds.CLOUDS[leg].taint_value == value, (
        f"the {leg} leg fakes nvidia.com/gpu={clouds.CLOUDS[leg].taint_value}, "
        f"but {COMPOSITIONS[leg]} taints GPU pools with {value}"
    )


# The compositions whose CPU pool label-and-taint the legs' fake CPU nodes
# copy. GKE is included here, unlike COMPOSITIONS: its GPU taint comes from
# GKE itself, but compose-gke-cluster sets the CPU taint explicitly, since
# GKE only auto-taints GPU pools. Still no Existing entry: Modelplane
# doesn't taint BYO pools.
CPU_COMPOSITIONS = {**COMPOSITIONS, "gke": "compose-gke-cluster"}


@pytest.mark.parametrize("leg", sorted(CPU_COMPOSITIONS))
def test_cpu_taint_matches(leg: str) -> None:
    """The fake CPU pools' label and taint are the ones every cluster composition applies.

    The compositions share one CPU taint, so the legs share one CPU_TAINT
    rather than faking it per cloud - this pins that assumption. The label
    key is the one dra-driver-cpu's nodeSelector reads.
    """
    consts = constants(CPU_COMPOSITIONS[leg])
    if "_CPU_TAINT_VALUE" in consts:
        key, value = consts["_CPU_TAINT_KEY"], consts["_CPU_TAINT_VALUE"]
    else:
        # compose-aks-cluster declares the whole taint as one
        # key=value:effect string.
        key, rest = consts["_CPU_TAINT"].split("=", 1)
        value = rest.split(":", 1)[0]
    assert (key, value) == (clouds.CPU_TAINT["key"], clouds.CPU_TAINT["value"]), (
        f"the legs fake the CPU taint {clouds.CPU_TAINT['key']}={clouds.CPU_TAINT['value']}, "
        f"but {CPU_COMPOSITIONS[leg]} taints CPU pools with {key}={value}"
    )
    assert clouds.CLOUDS[leg].cpu_labels.get(consts["_LABEL_CPU"]) == "true", (
        f"{CPU_COMPOSITIONS[leg]} labels CPU pools {consts['_LABEL_CPU']}=true, "
        f"but the {leg} leg's fake CPU nodes carry {clouds.CLOUDS[leg].cpu_labels}"
    )
