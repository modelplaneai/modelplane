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

"""Validate docs example manifests against the generated Pydantic models.

Every Modelplane manifest the docs show — the raw YAML files under
docs/manifests/, including the API-reference examples under
docs/manifests/reference/ — is parsed and validated against the model generated
from its XRD, before it reaches the published docs.

Validation runs the models in strict mode: extra="forbid" at every nesting
level. So beyond the usual checks (a missing required field, a wrong type, a bad
enum or a failed pattern), this also rejects a field the schema doesn't define —
a typo, or a field the API renamed or dropped. An example can't carry anything
the current API wouldn't accept. The flip side: if the model generator ever
dropped a field the API still has, a valid manifest using it would fail here and
flag the gap, which is the behaviour we want.

Only Modelplane's own API groups have generated models, so resources from other
groups (provider ClusterProviderConfigs, core Namespaces, RBAC, Crossplane
packages) are skipped — they aren't ours to validate here.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pydantic
import yaml

REPO = Path(__file__).resolve().parents[3]

# Prefer the installed crossplane-models package (as in the Nix check venv); fall
# back to the in-tree schemas so the script also runs from a plain checkout.
try:
    import models  # noqa: F401
except ModuleNotFoundError:
    sys.path.insert(0, str(REPO / "schemas" / "python"))

# API groups we generate models for, mapped to their model package segment.
MODELPLANE_GROUPS = {
    "modelplane.ai": "ai.modelplane",
    "infrastructure.modelplane.ai": "ai.modelplane.infrastructure",
}

# Modules already hardened to extra="forbid", so we do it once per module.
_hardened: set[str] = set()


def _harden(module: ModuleType) -> None:
    """Rebuild every model class in a module with extra="forbid".

    The generated models default to extra="ignore", which would let an example
    carry a field the API doesn't define. We set extra="forbid" on every model
    class the module defines (not the shared k8s/Crossplane types it imports) so
    an unknown field at any depth fails validation.

    Setting the config alone isn't enough: a parent's compiled core schema
    embeds its children's, so a child must rebuild before its parents pick up the
    change. Rebuilding the whole set to a fixed point propagates the config up
    every nesting level regardless of class definition order.
    """
    classes = [
        obj
        for name in dir(module)
        if isinstance(obj := getattr(module, name), type)
        and issubclass(obj, pydantic.BaseModel)
        and obj.__module__ == module.__name__
    ]
    for cls in classes:
        cls.model_config = pydantic.ConfigDict(extra="forbid")
    for _ in range(len(classes) + 1):
        changed = False
        for cls in classes:
            before = cls.__pydantic_core_schema__
            cls.model_rebuild(force=True)
            if cls.__pydantic_core_schema__ is not before:
                changed = True
        if not changed:
            break


def model_for(api_version: str, kind: str) -> type[pydantic.BaseModel] | None:
    """Return the Pydantic model class for a Modelplane kind, or None to skip."""
    group = api_version.rsplit("/", 1)[0] if "/" in api_version else ""
    version = api_version.rsplit("/", 1)[1] if "/" in api_version else api_version
    pkg = MODELPLANE_GROUPS.get(group)
    if pkg is None:
        return None
    module = f"models.{pkg}.{kind.lower()}.{version}"
    mod = importlib.import_module(module)
    if mod.__name__ not in _hardened:
        _harden(mod)
        _hardened.add(mod.__name__)
    return getattr(mod, kind)


def docs_from_file(path: Path) -> Iterator[dict]:
    """Yield each manifest dict from a (possibly multi-document) YAML file."""
    for doc in yaml.safe_load_all(path.read_text()):
        if doc:
            yield doc


def main() -> int:
    targets = sorted((REPO / "docs" / "manifests").rglob("*.yaml"))

    errors: list[str] = []
    checked = skipped = 0

    for path in targets:
        rel = path.relative_to(REPO)
        for doc in docs_from_file(path):
            kind = doc.get("kind")
            api_version = doc.get("apiVersion", "")
            if not kind:
                continue
            try:
                model = model_for(api_version, kind)
            except ModuleNotFoundError as e:
                errors.append(f"{rel}: no model for {api_version}/{kind}: {e}")
                continue
            if model is None:
                skipped += 1
                continue
            try:
                model.model_validate(doc)
                checked += 1
            except pydantic.ValidationError as e:
                errors.append(f"{rel}: {kind} {doc.get('metadata', {}).get('name', '?')}\n{e}")

    for err in errors:
        print(f"INVALID  {err}", file=sys.stderr)
    print(f"\nvalidated {checked} Modelplane manifest(s), skipped {skipped} non-Modelplane")
    if errors:
        print(f"{len(errors)} manifest(s) failed validation", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
