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
non-match (driven through the parse layer in test_parse_rejects).
"""

import dataclasses

import pytest
from function import cel, semver


@dataclasses.dataclass
class SemverCase:
    """A test case for a semver CEL expression."""

    name: str
    expr: str
    want: bool


@dataclasses.dataclass
class ParseRejectsCase:
    """A test case for a version string semver.parse rejects."""

    name: str
    s: str
    want: str


SEMVER_CASES = [
    # parse + doc-comment examples. Upstream's parse row returns the version
    # itself, so wrapped to assert a bool it's the same expression as its
    # "compare equal" row below. Both are kept to mirror upstream's table.
    SemverCase(name="parse", expr='semver("1.2.3").compareTo(semver("1.2.3")) == 0', want=True),
    SemverCase(name="parse with prerelease", expr='semver("0.1.0-alpha.1").major() == 0', want=True),
    # isSemver strict.
    SemverCase(name="isSemver full", expr='isSemver("1.2.3-beta.1+build.1")', want=True),
    SemverCase(name="isSemver simple", expr='isSemver("1.0.0")', want=True),
    SemverCase(name="isSemver hello", expr='isSemver("hello")', want=False),
    SemverCase(name="isSemver empty false", expr='isSemver("")', want=False),
    SemverCase(name="isSemver v prefix false", expr='isSemver("v1.0.0")', want=False),
    SemverCase(name="isSemver v1.0 false", expr='isSemver("v1.0")', want=False),
    SemverCase(name="isSemver leading whitespace false", expr='isSemver(" 1.0.0")', want=False),
    SemverCase(name="isSemver inner whitespace false", expr='isSemver("1. 0.0")', want=False),
    SemverCase(name="isSemver trailing whitespace false", expr='isSemver("1.0.0 ")', want=False),
    SemverCase(name="isSemver leading zeros false", expr='isSemver("01.01.01")', want=False),
    SemverCase(name="isSemver major only false", expr='isSemver("1")', want=False),
    SemverCase(name="isSemver major minor only false", expr='isSemver("1.1")', want=False),
    SemverCase(name="isSemver 200K", expr='isSemver("200K")', want=False),
    SemverCase(name="isSemver Mi", expr='isSemver("Mi")', want=False),
    # isSemver normalize overload. Normalization does NOT trim whitespace.
    SemverCase(name="isSemver empty normalize false", expr='isSemver("", true)', want=False),
    SemverCase(name="isSemver leading whitespace normalize false", expr='isSemver(" 1.0.0", true)', want=False),
    SemverCase(name="isSemver inner whitespace normalize false", expr='isSemver("1. 0.0", true)', want=False),
    SemverCase(name="isSemver trailing whitespace normalize false", expr='isSemver("1.0.0 ", true)', want=False),
    SemverCase(name="isSemver v prefix normalize true", expr='isSemver("v1.0.0", true)', want=True),
    SemverCase(name="isSemver leading zeros normalize true", expr='isSemver("01.01.01", true)', want=True),
    SemverCase(name="isSemver major only normalize true", expr='isSemver("1", true)', want=True),
    SemverCase(name="isSemver major minor only normalize true", expr='isSemver("1.1", true)', want=True),
    # normalize equality and semver(...) examples.
    SemverCase(name="equality normalize", expr='semver("v01.01", true) == semver("1.1.0")', want=True),
    SemverCase(name="semver v prefix normalize major", expr='semver("v1.0.0", true).major() == 1', want=True),
    SemverCase(name="semver short normalize patch", expr='semver("1.0", true).patch() == 0', want=True),
    SemverCase(name="semver leading zeros normalize", expr='semver("01.01.01", true).minor() == 1', want=True),
    # equality / comparison.
    SemverCase(name="equality reflexivity", expr='semver("1.2.3") == semver("1.2.3")', want=True),
    SemverCase(name="inequality", expr='semver("1.2.3") == semver("1.0.0")', want=False),
    SemverCase(name="less", expr='semver("1.0.0").isLessThan(semver("1.2.3"))', want=True),
    SemverCase(name="less false", expr='semver("1.0.0").isLessThan(semver("1.0.0"))', want=False),
    SemverCase(name="greater", expr='semver("1.2.3").isGreaterThan(semver("1.0.0"))', want=True),
    SemverCase(name="greater false", expr='semver("1.0.0").isGreaterThan(semver("1.0.0"))', want=False),
    SemverCase(name="compare equal", expr='semver("1.2.3").compareTo(semver("1.2.3")) == 0', want=True),
    SemverCase(name="compare less", expr='semver("1.2.3").compareTo(semver("2.0.0")) == -1', want=True),
    SemverCase(name="compare greater", expr='semver("1.2.3").compareTo(semver("0.1.2")) == 1', want=True),
    # major / minor / patch.
    SemverCase(name="major", expr='semver("1.2.3").major() == 1', want=True),
    SemverCase(name="minor", expr='semver("1.2.3").minor() == 2', want=True),
    SemverCase(name="patch", expr='semver("1.2.3").patch() == 3', want=True),
    # A bad version is a runtime error upstream -> non-match here.
    SemverCase(name="bad version is non-match", expr='semver("v1.0").major() == 1', want=False),
]


@pytest.mark.parametrize("case", SEMVER_CASES, ids=lambda case: case.name)
def test_semver(case: SemverCase) -> None:
    """A semver CEL expression evaluates as it does upstream."""
    got = cel.Program(case.expr).matches({})
    assert got == case.want


PARSE_REJECTS_CASES = [
    ParseRejectsCase(name="v prefix", s="v1.0", want=r"no Major\.Minor\.Patch elements found"),
    ParseRejectsCase(name="major only", s="1", want=r"no Major\.Minor\.Patch elements found"),
    ParseRejectsCase(name="major minor only", s="1.1", want=r"no Major\.Minor\.Patch elements found"),
    ParseRejectsCase(name="leading zeros", s="01.01.01", want="major number must not contain leading zeroes: '01'"),
    ParseRejectsCase(name="leading whitespace", s=" 1.0.0", want=r"invalid character\(s\) in major number: ' 1'"),
    ParseRejectsCase(name="trailing whitespace", s="1.0.0 ", want=r"invalid character\(s\) in patch number: '0 '"),
    ParseRejectsCase(name="empty", s="", want="version string empty"),
    ParseRejectsCase(name="word", s="hello", want=r"no Major\.Minor\.Patch elements found"),
]


@pytest.mark.parametrize("case", PARSE_REJECTS_CASES, ids=lambda case: case.name)
def test_parse_rejects(case: ParseRejectsCase) -> None:
    """parse() (strict) rejects what blang/semver Parse rejects."""
    with pytest.raises(ValueError, match=case.want):
        semver.parse(case.s)
