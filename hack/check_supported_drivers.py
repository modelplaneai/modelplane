#!/usr/bin/env python3
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

"""Check compose-inference-cluster's supported DRA drivers against the serving stack's.

compose-inference-cluster rejects a class claiming a driver its cloud can't
serve before it composes a ServingStack, using _SUPPORTED_DRA_DRIVERS, a
hand-kept copy of compose-serving-stack's derived supported_drivers(). Each
function's package is named `function`, so neither can import the other. This
runs with compose-serving-stack installed and reads the copy from
compose-inference-cluster's source.
"""

import ast
import pathlib
import sys

from function import stacks  # ty: ignore[unresolved-import]  # compose-serving-stack, installed in the check's venv.

REPO = pathlib.Path(__file__).resolve().parents[1]
MIRROR = REPO / "functions" / "compose-inference-cluster" / "function" / "fn.py"

# Existing never gates, so it has no entry in the copy.
UNGATED = {"Existing"}


def mirror() -> dict[str, frozenset[str]]:
    """Return compose-inference-cluster's _SUPPORTED_DRA_DRIVERS, read from its source."""
    tree = ast.parse(MIRROR.read_text())
    constants: dict[str, str] = {}
    table: ast.Dict | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            constants[target.id] = value.value
        if target.id == "_SUPPORTED_DRA_DRIVERS" and isinstance(value, ast.Dict):
            table = value
    if table is None:
        sys.exit(f"{MIRROR}: no _SUPPORTED_DRA_DRIVERS dict literal")

    # Each entry reads CLUSTER_SOURCE_<CLOUD>: frozenset({"driver", ...}).
    got: dict[str, frozenset[str]] = {}
    for key, value in zip(table.keys, table.values, strict=True):
        if not isinstance(key, ast.Name) or not isinstance(value, ast.Call) or len(value.args) != 1:
            sys.exit(f"{MIRROR}:{table.lineno}: unexpected _SUPPORTED_DRA_DRIVERS entry shape")
        got[constants[key.id]] = frozenset(ast.literal_eval(value.args[0]))
    return got


def main() -> int:
    """Report every cloud whose copied drivers differ from the serving stack's."""
    want = {cloud: stacks.supported_drivers(cloud) for cloud in stacks.clouds() if cloud not in UNGATED}
    got = mirror()
    drifted = [cloud for cloud in sorted(set(want) | set(got)) if want.get(cloud) != got.get(cloud)]
    for cloud in drifted:
        print(
            f"{cloud}: compose-serving-stack supports {sorted(want.get(cloud, []))}, "
            f"compose-inference-cluster's _SUPPORTED_DRA_DRIVERS has {sorted(got.get(cloud, []))}",
            file=sys.stderr,
        )
    return 1 if drifted else 0


if __name__ == "__main__":
    sys.exit(main())
