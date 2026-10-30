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

"""Check that no composition function puts a reference selector inside a list.

The managed resource CRDs declare no x-kubernetes-list-type on their lists,
which makes the lists atomic under server-side apply: an applier that includes
a list owns and replaces it whole. A matchControllerRef selector inside a list
item asks crossplane-runtime's reference resolver to write the resolved id
into that item, so the composition and the resolver take the whole list from
each other on every apply, and the object never settles - see issue #517,
where the churn replaced Nebius nodes every few minutes. Read the id from the
observed composed resource instead, and render it into the list item directly.

Where a list has a real need for a selector - the CRD declares it
x-kubernetes-list-type: map, say, so its items merge per key - an escape on
the line the checker reports, with a stated reason, accepts the selector:

    idSelector=v1beta1.IdSelector(matchControllerRef=True),  # noqa: MPF001  # items is listType: map.
"""

import ast
import io
import pathlib
import re
import sys
import tokenize
import typing

REPO = pathlib.Path(__file__).resolve().parents[1]

# An escape: noqa, its codes, then the reason, as in the docstring above. The
# codes are those ruff's noqa takes, so one comment can carry ruff's and ours.
NOQA = re.compile(r"#\s*noqa:\s*(?P<codes>[A-Z]+[0-9]+(?:[\s,]+[A-Z]+[0-9]+)*)(?P<reason>.*)")


class Violation(typing.NamedTuple):
    """A rule a function file breaks, at a line."""

    line: int
    code: str
    message: str


def parents(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    """Each node's parent, for walking up from a match."""
    out: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            out[child] = node
    return out


def selector_field(node: ast.AST, up: dict[ast.AST, ast.AST]) -> str | None:
    """The selector field node sits under, read off the nearest enclosing
    keyword argument or dict key named like one."""
    child = node
    while (parent := up.get(child)) is not None:
        if isinstance(parent, ast.keyword) and parent.arg and parent.arg.endswith("Selector"):
            return parent.arg
        if isinstance(parent, ast.Dict) and isinstance(child, ast.expr) and child in parent.values:
            key = parent.keys[parent.values.index(child)]
            if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value.endswith("Selector"):
                return key.value
        child = parent
    return None


def in_list(node: ast.AST, up: dict[ast.AST, ast.AST]) -> bool:
    """Whether node is written inside a list field.

    The nearest enclosing list - a literal or a comprehension's element - must
    be the value of a keyword argument or a dict key: that makes it a field of
    the resource, so a Kubernetes list. A bare list, [Release(spec=...)], just
    collects resources in Python, and a selector inside one is at no risk."""
    child = node
    while (parent := up.get(child)) is not None:
        if isinstance(parent, ast.List) or (isinstance(parent, ast.ListComp) and child is parent.elt):
            field = up.get(parent)
            return isinstance(field, ast.keyword) or (isinstance(field, ast.Dict) and parent in field.values)
        child = parent
    return False


def matches(tree: ast.Module) -> list[tuple[int, ast.AST]]:
    """Each matchControllerRef written as a keyword argument or a dict key,
    with its line."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            out += [(kw.value.lineno, node) for kw in node.keywords if kw.arg == "matchControllerRef"]
        if isinstance(node, ast.Dict):
            out += [
                (k.lineno, node) for k in node.keys if isinstance(k, ast.Constant) and k.value == "matchControllerRef"
            ]
    return out


def escapes(source: str) -> tuple[dict[int, set[str]], list[Violation]]:
    """The codes each line escapes, and the escapes that state no reason."""
    escaped: dict[int, set[str]] = {}
    unexplained = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type != tokenize.COMMENT or (match := NOQA.search(tok.string)) is None:
            continue
        codes = {c for c in re.split(r"[\s,]+", match["codes"]) if c.startswith("MPF")}
        if not codes:
            continue
        escaped[tok.start[0]] = codes
        if not match["reason"].strip(" #-:"):
            msg = "escape states no reason; write # noqa: MPFnnn  # why"
            unexplained.append(Violation(tok.start[0], "MPF001", msg))
    return escaped, unexplained


def check(source: str) -> list[Violation]:
    """The rules source breaks, other than those it escapes."""
    tree = ast.parse(source)
    up = parents(tree)
    found = []
    for line, node in matches(tree):
        if not in_list(node, up):
            continue
        field = selector_field(node, up) or "a selector"
        msg = (
            f"{field} puts matchControllerRef inside a list; the list is atomic "
            "under server-side apply, so the composition and the reference "
            "resolver replace each other's items (#517) - render the id read "
            "from the observed resource instead"
        )
        found.append(Violation(line, "MPF001", msg))
    escaped, unexplained = escapes(source)
    return sorted([v for v in found if v.code not in escaped.get(v.line, set())] + unexplained)


def main() -> int:
    """Check the files named on the command line, or every function's sources."""
    paths = [pathlib.Path(a).resolve() for a in sys.argv[1:]] or sorted(REPO.glob("functions/*/function/*.py"))
    count = 0
    for path in paths:
        shown = path.relative_to(REPO) if path.is_relative_to(REPO) else path
        for v in check(path.read_text()):
            print(f"{shown}:{v.line}: {v.code} {v.message}", file=sys.stderr)
            count += 1
    print(f"\nchecked {len(paths)} function file(s)")
    if count:
        print(f"{count} selector(s) inside atomic lists", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
