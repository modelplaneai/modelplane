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

An engine in a recipe is also checked against what the built-in MetricMappings
read: a pinned engine version or a missing flag that leaves the engine's
modelplane_* series empty fails here rather than on a user's cluster. A
pre-release SGLang build, a non-release tag pinned by digest, gets the checks
that don't depend on the version.
"""

from __future__ import annotations

import importlib
import re
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

# Importable only once the path above is set.
from models.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1  # noqa: E402
from models.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1  # noqa: E402

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


# Each recipe's engines must publish what the built-in mappings in
# compose-serving-stack's stacks/metrics.py read, so keep the two in step.
_TAG = re.compile(r":v(\d+)\.(\d+)\.(\d+)")
_VLLM_IMAGE = re.compile(r"(^|/)vllm/vllm-openai[:@]")
_SGLANG_IMAGE = re.compile(r"(^|/)lmsysorg/sglang[:@]")
# A pre-release SGLang build: a non-release tag pinned by digest, such as
# lmsysorg/sglang:dev-kimi-k31-pro@sha256:...
_PRERELEASE = re.compile(r":(?!v\d)[^:@/]+@sha256:")


def _has_flag(argv: str, flag: str) -> bool:
    """Whether argv passes flag as a whole token, bare or as flag=value."""
    return re.search(rf"(?<!\S){re.escape(flag)}(?:=\S+)?(?!\S)", argv) is not None


def _version(image: str) -> tuple[int, int, int] | None:
    m = _TAG.search(image)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _engines(obj: pydantic.BaseModel) -> list:
    """The engines a ModelDeployment or ModelReplica runs, or none for any other kind."""
    if isinstance(obj, mdv1alpha1.ModelDeployment):
        return obj.spec.template.spec.engines
    if isinstance(obj, mrv1alpha1.ModelReplica):
        return obj.spec.engines
    return []


def _container_problems(container: pydantic.BaseModel) -> list[str]:
    """Why one engine container wouldn't publish what the built-in mappings read."""
    image = container.image
    argv = " ".join((container.command or []) + (container.args or []))
    version = _version(image)
    is_vllm = _VLLM_IMAGE.search(image) is not None
    is_sglang = _SGLANG_IMAGE.search(image) is not None
    if not (is_vllm or is_sglang):
        return []
    if version is None and not (is_sglang and _PRERELEASE.search(image)):
        return [f"pin {image} to a vX.Y.Z tag, so its engine can be checked against the built-in mappings"]
    if is_vllm:
        if version < (0, 9, 2):
            return [
                "vLLM before v0.9.2 doesn't publish vllm:kv_cache_usage_perc "
                "or the prefix cache counters the built-in mappings read"
            ]
        return []
    problems = []
    if not _has_flag(argv, "--enable-metrics"):
        problems.append("SGLang serves no /metrics without --enable-metrics")
    # A pre-release build (a model's day-0 image) postdates v0.5.12: only the
    # checks that don't depend on the version apply.
    if version is not None and version < (0, 5, 12) and not _has_flag(argv, "--collect-tokens-histogram"):
        problems.append("SGLang before v0.5.12 publishes no token histograms without --collect-tokens-histogram")
    return problems


def engine_metrics_problems(obj: pydantic.BaseModel) -> list[str]:
    """Why an engine in a ModelDeployment or ModelReplica wouldn't publish what the built-in mappings read."""
    return [
        f"engine {engine.name}: {problem}"
        for engine in _engines(obj)
        for member in engine.members
        for c in member.template.spec.containers
        if c.name == "engine"
        for problem in _container_problems(c)
    ]


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
                obj = model.model_validate(doc)
                checked += 1
            except pydantic.ValidationError as e:
                errors.append(f"{rel}: {kind} {doc.get('metadata', {}).get('name', '?')}\n{e}")
                continue
            errors.extend(
                f"{rel}: {kind} {doc.get('metadata', {}).get('name', '?')}: {problem}"
                for problem in engine_metrics_problems(obj)
            )

    for err in errors:
        print(f"INVALID  {err}", file=sys.stderr)
    print(f"\nvalidated {checked} Modelplane manifest(s), skipped {skipped} non-Modelplane")
    if errors:
        print(f"{len(errors)} manifest(s) failed validation", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
