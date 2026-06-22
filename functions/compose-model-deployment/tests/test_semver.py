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

"""Tests for the semver module.

The cases mirror upstream's semver CEL surface so we catch regressions against
it: every example from the semverlib.go doc comment and every applicable case
from k8s.io/apiserver/pkg/cel/library/semver_test.go is represented as a row.

Expressions are evaluated end to end through a compiled CEL selector (the device
activation is built but unused). Value-returning expressions are wrapped in a
`== <want>` comparison so each case asserts a single bool.

Upstream cases that don't apply: the compile-time overload error
(isSemver([1,2,3])) - celpy doesn't type-check overloads; and the runtime parse
error for semver("v1.0") - upstream raises, we treat a bad version as a
non-match (driven through the parse layer in TestParseRejects).
"""

import dataclasses
import unittest

from function import cel, semver


def _eval(expr: str) -> bool:
    """Compile and evaluate a deviceless boolean CEL expression."""
    return cel.Program(expr).matches({})


@dataclasses.dataclass
class Case:
    name: str
    expr: str
    want: bool


@dataclasses.dataclass
class ParseErrCase:
    name: str
    input: str


class TestSemverCEL(unittest.TestCase):
    """Mirrors semver_test.go TestSemver (and the doc-comment examples)."""

    def test_semver(self) -> None:
        cases = [
            # parse + doc-comment examples.
            Case(name="parse", expr='semver("1.2.3").compareTo(semver("1.2.3")) == 0', want=True),
            Case(name="parse with prerelease", expr='semver("0.1.0-alpha.1").major() == 0', want=True),
            # isSemver strict.
            Case(name="isSemver full", expr='isSemver("1.2.3-beta.1+build.1")', want=True),
            Case(name="isSemver simple", expr='isSemver("1.0.0")', want=True),
            Case(name="isSemver hello", expr='isSemver("hello")', want=False),
            Case(name="isSemver empty false", expr='isSemver("")', want=False),
            Case(name="isSemver v prefix false", expr='isSemver("v1.0.0")', want=False),
            Case(name="isSemver v1.0 false", expr='isSemver("v1.0")', want=False),
            Case(name="isSemver leading whitespace false", expr='isSemver(" 1.0.0")', want=False),
            Case(name="isSemver inner whitespace false", expr='isSemver("1. 0.0")', want=False),
            Case(name="isSemver trailing whitespace false", expr='isSemver("1.0.0 ")', want=False),
            Case(name="isSemver leading zeros false", expr='isSemver("01.01.01")', want=False),
            Case(name="isSemver major only false", expr='isSemver("1")', want=False),
            Case(name="isSemver major minor only false", expr='isSemver("1.1")', want=False),
            Case(name="isSemver 200K", expr='isSemver("200K")', want=False),
            Case(name="isSemver Mi", expr='isSemver("Mi")', want=False),
            # isSemver normalize overload. Normalization does NOT trim whitespace.
            Case(name="isSemver empty normalize false", expr='isSemver("", true)', want=False),
            Case(name="isSemver leading whitespace normalize false", expr='isSemver(" 1.0.0", true)', want=False),
            Case(name="isSemver inner whitespace normalize false", expr='isSemver("1. 0.0", true)', want=False),
            Case(name="isSemver trailing whitespace normalize false", expr='isSemver("1.0.0 ", true)', want=False),
            Case(name="isSemver v prefix normalize true", expr='isSemver("v1.0.0", true)', want=True),
            Case(name="isSemver leading zeros normalize true", expr='isSemver("01.01.01", true)', want=True),
            Case(name="isSemver major only normalize true", expr='isSemver("1", true)', want=True),
            Case(name="isSemver major minor only normalize true", expr='isSemver("1.1", true)', want=True),
            # normalize equality and semver(...) examples.
            Case(name="equality normalize", expr='semver("v01.01", true) == semver("1.1.0")', want=True),
            Case(name="semver v prefix normalize major", expr='semver("v1.0.0", true).major() == 1', want=True),
            Case(name="semver short normalize patch", expr='semver("1.0", true).patch() == 0', want=True),
            Case(name="semver leading zeros normalize", expr='semver("01.01.01", true).minor() == 1', want=True),
            # equality / comparison.
            Case(name="equality reflexivity", expr='semver("1.2.3") == semver("1.2.3")', want=True),
            Case(name="inequality", expr='semver("1.2.3") == semver("1.0.0")', want=False),
            Case(name="less", expr='semver("1.0.0").isLessThan(semver("1.2.3"))', want=True),
            Case(name="less false", expr='semver("1.0.0").isLessThan(semver("1.0.0"))', want=False),
            Case(name="greater", expr='semver("1.2.3").isGreaterThan(semver("1.0.0"))', want=True),
            Case(name="greater false", expr='semver("1.0.0").isGreaterThan(semver("1.0.0"))', want=False),
            Case(name="compare equal", expr='semver("1.2.3").compareTo(semver("1.2.3")) == 0', want=True),
            Case(name="compare less", expr='semver("1.2.3").compareTo(semver("2.0.0")) == -1', want=True),
            Case(name="compare greater", expr='semver("1.2.3").compareTo(semver("0.1.2")) == 1', want=True),
            # major / minor / patch.
            Case(name="major", expr='semver("1.2.3").major() == 1', want=True),
            Case(name="minor", expr='semver("1.2.3").minor() == 2', want=True),
            Case(name="patch", expr='semver("1.2.3").patch() == 3', want=True),
            # A bad version is a runtime error upstream -> non-match here.
            Case(name="bad version is non-match", expr='semver("v1.0").major() == 1', want=False),
        ]
        for case in cases:
            with self.subTest(case.name):
                self.assertEqual(case.want, _eval(case.expr), f"{case.name}: -want, +got")


class TestParseRejects(unittest.TestCase):
    """parse() (strict) rejects what blang/semver Parse rejects."""

    def test_parse_rejects(self) -> None:
        cases = [
            ParseErrCase(name="v prefix", input="v1.0"),
            ParseErrCase(name="major only", input="1"),
            ParseErrCase(name="major minor only", input="1.1"),
            ParseErrCase(name="leading zeros", input="01.01.01"),
            ParseErrCase(name="leading whitespace", input=" 1.0.0"),
            ParseErrCase(name="trailing whitespace", input="1.0.0 "),
            ParseErrCase(name="empty", input=""),
            ParseErrCase(name="word", input="hello"),
        ]
        for case in cases:
            with self.subTest(case.name), self.assertRaises(ValueError):
                semver.parse(case.input)


if __name__ == "__main__":
    unittest.main()
