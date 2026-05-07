#!/usr/bin/env python3
"""
Lint design-time examples against their proposed XRDs.

Walks every YAML under examples/ (recursive), looks up the matching XRD in
xrds/ by kind, and structurally validates against the OpenAPIV3Schema.
Catches typos, missing required fields, enum violations, type mismatches,
and unknown properties at points where the schema isn't permissive
(no x-kubernetes-preserve-unknown-fields).

No deps beyond PyYAML — runnable with plain `python3`.

Usage:
    python3 design/proposed-modelplane-api/lint.py

Exit code 0 if all examples validate, 1 otherwise.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
XRD_DIR = HERE / "xrds"
EX_DIR = HERE / "examples"


def load_yamls(p: Path) -> list[dict]:
    with p.open() as f:
        docs = list(yaml.safe_load_all(f))
    return [d for d in docs if d]


def index_xrds() -> dict[str, dict]:
    """kind -> openAPIV3Schema for the latest version."""
    by_kind: dict[str, dict] = {}
    for path in XRD_DIR.glob("*.yaml"):
        for doc in load_yamls(path):
            if doc.get("kind") != "CompositeResourceDefinition":
                continue
            kind = doc["spec"]["names"]["kind"]
            versions = doc["spec"]["versions"]
            schema = versions[-1]["schema"]["openAPIV3Schema"]
            by_kind[kind] = schema
    return by_kind


class LintError:
    def __init__(self, path: str, jsonpath: str, msg: str):
        self.path = path
        self.jsonpath = jsonpath
        self.msg = msg

    def __str__(self) -> str:
        return f"{self.path}: {self.jsonpath}: {self.msg}"


def is_preserve_unknown(schema: dict) -> bool:
    return bool(schema.get("x-kubernetes-preserve-unknown-fields"))


def type_of(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "null"


# Standard K8s top-level fields that aren't in OpenAPIV3Schema definitions.
ROOT_KUBE_FIELDS = {"apiVersion", "kind", "metadata", "status"}


def walk(value: object, schema: dict, jsonpath: str, errors: list[LintError], file: str) -> None:
    if schema is None or value is None:
        return

    # At the root, strip the standard K8s envelope fields before validating.
    # The XRD's openAPIV3Schema describes only spec/status; metadata/kind/
    # apiVersion are layered on by the K8s API.
    if jsonpath == "" and isinstance(value, dict):
        value = {k: v for k, v in value.items() if k not in ROOT_KUBE_FIELDS}

    expected_type = schema.get("type")
    actual_type = type_of(value)

    # Treat string/integer/number predicate strings (">=141Gi") as strings — don't
    # error if schema says integer but value is a string with operators. The
    # matcher resolves these at runtime.
    if expected_type and actual_type != expected_type:
        if expected_type == "integer" and isinstance(value, str) and any(value.startswith(op) for op in (">=", "<=", ">", "<", "==")):
            return
        errors.append(LintError(file, jsonpath, f"expected {expected_type}, got {actual_type} ({value!r})"))
        return

    if expected_type == "object":
        # x-kubernetes-preserve-unknown-fields: anything goes
        if is_preserve_unknown(schema):
            return
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        additional_props = schema.get("additionalProperties")

        for r in required:
            if r not in (value or {}):
                errors.append(LintError(file, jsonpath, f"missing required field '{r}'"))

        for k, v in (value or {}).items():
            child_path = f"{jsonpath}.{k}" if jsonpath else k
            if k in properties:
                walk(v, properties[k], child_path, errors, file)
            elif additional_props is True or (
                isinstance(additional_props, dict) and is_preserve_unknown(additional_props)
            ):
                pass  # accepted
            elif isinstance(additional_props, dict):
                walk(v, additional_props, child_path, errors, file)
            else:
                errors.append(LintError(file, jsonpath, f"unknown field '{k}'"))

    elif expected_type == "array":
        item_schema = schema.get("items")
        for i, v in enumerate(value or []):
            walk(v, item_schema, f"{jsonpath}[{i}]", errors, file)

    elif expected_type == "string":
        enum = schema.get("enum")
        if enum and value not in enum:
            errors.append(LintError(file, jsonpath, f"value {value!r} not in enum {enum}"))


def find_examples() -> Iterator[Path]:
    for p in EX_DIR.rglob("*.yaml"):
        yield p


def main() -> int:
    xrds = index_xrds()
    if not xrds:
        print(f"No XRDs found under {XRD_DIR}", file=sys.stderr)
        return 2

    all_errors: list[LintError] = []
    skipped: list[str] = []
    checked = 0
    for ex in sorted(find_examples()):
        rel = ex.relative_to(HERE)
        for doc in load_yamls(ex):
            kind = doc.get("kind")
            if not kind:
                continue
            if kind not in xrds:
                skipped.append(f"{rel}: kind {kind!r} has no XRD; skipping")
                continue
            walk(doc, xrds[kind], "", all_errors, str(rel))
            checked += 1

    if skipped:
        print("Skipped (no XRD for kind):")
        for s in skipped:
            print(f"  {s}")
        print()

    if all_errors:
        print(f"FAIL — {len(all_errors)} lint error(s) across {checked} example doc(s):")
        for e in all_errors:
            print(f"  {e}")
        return 1

    print(f"OK — {checked} example doc(s) validated against {len(xrds)} XRD(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
