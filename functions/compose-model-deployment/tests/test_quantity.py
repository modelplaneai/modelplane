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

"""Tests for the quantity module.

The cases mirror upstream's quantity CEL surface so we catch regressions against
it: every example from the quantity.go doc comment and every applicable case
from k8s.io/apiserver/pkg/cel/library/quantity_test.go is represented as a row.

Expressions are evaluated end to end through a compiled CEL selector (the device
activation is built but unused), so each case reads like the CEL a user writes.
Value-returning expressions are wrapped in a `== <want>` comparison so the case
asserts a single bool, matching how the upstream table asserts equality.

A few upstream cases don't apply to our reimplementation and are noted inline:
compile-time overload errors (isQuantity([1,2,3])) - celpy doesn't type-check
overloads; and runtime-error cases (an invalid suffix, integer overflow) which
upstream raises but we treat as a non-match (a CEL eval error -> matches() is
False), exercised here through the parse layer and the selector layer.

These expected values come from running the inputs through the real Kubernetes
code, not from assertion by hand. When you change the quantity module or want to
add a case, derive its expected value with the parity oracle in ./oracle (see
oracle/README.md) rather than reasoning about it - upstream has surprises (e.g.
binary-suffix overflow saturates to int64-max, so 8Ei == 10Ei).
"""

import dataclasses
import unittest

from function import cel, quantity


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


class TestQuantityCEL(unittest.TestCase):
    """Mirrors quantity_test.go TestQuantity (and the doc-comment examples)."""

    def test_quantity(self) -> None:
        cases = [
            # parse + isQuantity.
            Case(name="parse", expr='quantity("12Mi").compareTo(quantity("12Mi")) == 0', want=True),
            Case(name="isQuantity int string", expr='isQuantity("20")', want=True),
            Case(name="isQuantity megabytes", expr='isQuantity("20M")', want=True),
            Case(name="isQuantity mebibytes", expr='isQuantity("20Mi")', want=True),
            Case(name="isQuantity invalid suffix", expr='isQuantity("20Mo")', want=False),
            Case(name="isQuantity passing regex bad suffix", expr='isQuantity("10Mm")', want=False),
            # resource.Quantity accepts decimal exponents and nano/micro suffixes.
            Case(name="isQuantity exponent lowercase", expr='isQuantity("256e3")', want=True),
            Case(name="isQuantity exponent uppercase", expr='isQuantity("1E3")', want=True),
            Case(name="exponent value", expr='quantity("256e3").compareTo(quantity("256000")) == 0', want=True),
            Case(name="isQuantity nano", expr='isQuantity("100n")', want=True),
            Case(name="isQuantity micro", expr='isQuantity("100u")', want=True),
            Case(name="isQuantity trailing dot", expr='isQuantity("5.")', want=True),
            # The quantity() constructor does NOT trim whitespace.
            Case(name="isQuantity leading whitespace false", expr='isQuantity(" 5Gi")', want=False),
            Case(name="isQuantity trailing whitespace false", expr='isQuantity("5Gi ")', want=False),
            # Values equal at nano resolution compare equal (resource.Quantity.Cmp
            # rounds to nano).
            Case(
                name="nano rounding equality",
                expr='quantity("0.0000000004").compareTo(quantity("0.000000001")) == 0',
                want=True,
            ),
            # doc-comment isQuantity examples.
            Case(name="isQuantity 1.3G", expr='isQuantity("1.3G")', want=True),
            Case(name="isQuantity 1.3Gi", expr='isQuantity("1.3Gi")', want=True),
            Case(name="isQuantity comma", expr='isQuantity("1,3G")', want=False),
            Case(name="isQuantity 10000k", expr='isQuantity("10000k")', want=True),
            Case(name="isQuantity capital K", expr='isQuantity("200K")', want=False),
            Case(name="isQuantity Three", expr='isQuantity("Three")', want=False),
            Case(name="isQuantity bare suffix", expr='isQuantity("Mi")', want=False),
            # equality.
            Case(name="equality reflexivity", expr='quantity("200M") == quantity("200M")', want=True),
            Case(
                name="equality symmetry",
                expr='quantity("200M") == quantity("0.2G") && quantity("0.2G") == quantity("200M")',
                want=True,
            ),
            Case(
                name="equality transitivity",
                expr=(
                    'quantity("2M") == quantity("0.002G") && quantity("2000k") == quantity("2M") && '
                    'quantity("0.002G") == quantity("2000k")'
                ),
                want=True,
            ),
            Case(name="inequality", expr='quantity("200M") == quantity("0.3G")', want=False),
            # isLessThan / isGreaterThan.
            Case(name="less", expr='quantity("50M").isLessThan(quantity("50Mi"))', want=True),
            Case(name="less obvious", expr='quantity("50M").isLessThan(quantity("100M"))', want=True),
            Case(name="less false", expr='quantity("100M").isLessThan(quantity("50M"))', want=False),
            Case(name="greater", expr='quantity("50Mi").isGreaterThan(quantity("50M"))', want=True),
            Case(name="greater obvious", expr='quantity("150Mi").isGreaterThan(quantity("100Mi"))', want=True),
            Case(name="greater false", expr='quantity("50M").isGreaterThan(quantity("100M"))', want=False),
            # compareTo.
            Case(name="compare equal", expr='quantity("200M").compareTo(quantity("0.2G")) == 0', want=True),
            Case(name="compare less", expr='quantity("50M").compareTo(quantity("50Mi")) == -1', want=True),
            Case(name="compare greater", expr='quantity("50Mi").compareTo(quantity("50M")) == 1', want=True),
            # add / sub (quantity and int overloads).
            Case(name="add quantity", expr='quantity("50k").add(quantity("20")) == quantity("50.02k")', want=True),
            Case(name="add int not less", expr='quantity("50k").add(20).isLessThan(quantity("50020"))', want=False),
            Case(name="sub quantity", expr='quantity("50k").sub(quantity("20")) == quantity("49.98k")', want=True),
            Case(name="sub int", expr='quantity("50k").sub(20) == quantity("49980")', want=True),
            Case(
                name="arith chain 1",
                expr='quantity("50k").add(20).sub(quantity("100k")).asInteger() == -49980',
                want=True,
            ),
            Case(
                name="arith chain 2",
                expr='quantity("50k").add(20).sub(quantity("100k")).sub(-50000).asInteger() == 20',
                want=True,
            ),
            # sign (doc comment). Upstream declares sign as a GLOBAL function
            # (sign(q)), not a member (q.sign()); the global form is the parity
            # surface. celpy can't tell the two call styles apart, so we accept
            # both, but the test asserts the upstream-correct global form (see
            # cel.py's documented divergences).
            Case(name="sign positive", expr='sign(quantity("50k")) == 1', want=True),
            Case(name="sign negative", expr='sign(quantity("-50k")) == -1', want=True),
            Case(name="sign zero", expr='sign(quantity("0")) == 0', want=True),
            # Binary-suffix overflow saturates to int64-max, keeping sign, so
            # 8Ei/10Ei/100Ei all compare equal to int64-max (resource.Quantity
            # stores BinarySI in an int64). Confirmed against resource.Quantity.
            Case(
                name="Ei saturates to int64 max",
                expr='quantity("8Ei").compareTo(quantity("9223372036854775807")) == 0',
                want=True,
            ),
            Case(name="8Ei equals 10Ei", expr='quantity("8Ei").compareTo(quantity("10Ei")) == 0', want=True),
            Case(name="10Ei equals 100Ei", expr='quantity("10Ei").compareTo(quantity("100Ei")) == 0', want=True),
            Case(
                name="negative Ei saturates",
                expr='quantity("-10Ei").compareTo(quantity("-9223372036854775807")) == 0',
                want=True,
            ),
            # 7Ei is below int64-max, so it does NOT saturate and stays less.
            Case(name="7Ei below saturation", expr='quantity("7Ei").isLessThan(quantity("8Ei"))', want=True),
            # Large DECIMAL-path values do not saturate (only the binary path
            # does) and must not raise on nano-rounding. isQuantity must be true
            # and the value must round-trip.
            Case(name="isQuantity 256E", expr='isQuantity("256E")', want=True),
            Case(name="isQuantity 10E", expr='isQuantity("10E")', want=True),
            Case(
                name="256E value", expr='quantity("256E").compareTo(quantity("256000000000000000000")) == 0', want=True
            ),
            Case(name="256E greater than 1Ei", expr='quantity("256E").isGreaterThan(quantity("1Ei"))', want=True),
            # asInteger / isInteger.
            Case(name="as integer", expr='quantity("50k").asInteger() == 50000', want=True),
            Case(name="is integer true small", expr='quantity("50").isInteger()', want=True),
            Case(name="is integer true big magnitude", expr='quantity("50000000G").isInteger()', want=True),
            Case(
                name="is integer false overflow",
                expr='quantity("9999999999999999999999999999999999999G").isInteger()',
                want=False,
            ),
            # asInteger overflow is a runtime error upstream -> non-match here.
            Case(
                name="as integer overflow is non-match",
                expr='quantity("9999999999999999999999999999999999999G").asInteger() > 0',
                want=False,
            ),
            # asApproximateFloat.
            Case(name="as approximate float", expr='quantity("50.703k").asApproximateFloat() == 50703.0', want=True),
            # An invalid suffix is a runtime error upstream -> non-match here.
            # (Uses a member method upstream accepts, isGreaterThan, so the
            # non-match is the parse failure, not a rejected call form.)
            Case(
                name="invalid suffix is non-match",
                expr='quantity("10Mo").isGreaterThan(quantity("1"))',
                want=False,
            ),
        ]
        for case in cases:
            with self.subTest(case.name):
                self.assertEqual(case.want, _eval(case.expr), f"{case.name}: -want, +got")


class TestParseRejects(unittest.TestCase):
    """parse() rejects what resource.Quantity rejects (drives the non-matches above).

    The bare-suffix row ("Mi") is a DELIBERATE divergence, not parity: upstream
    parses most bare suffixes as 0 but inconsistently errors on a few (see
    parse()'s docstring). We reject every bare suffix; no device capacity is
    ever a bare suffix.
    """

    def test_parse_rejects(self) -> None:
        cases = [
            ParseErrCase(name="invalid suffix Mo", input="10Mo"),
            ParseErrCase(name="passing regex bad suffix Mm", input="10Mm"),
            ParseErrCase(name="capital K", input="200K"),
            ParseErrCase(name="comma", input="1,3G"),
            ParseErrCase(name="word", input="Three"),
            ParseErrCase(name="bare suffix (deliberate divergence)", input="Mi"),
            ParseErrCase(name="empty", input=""),
        ]
        for case in cases:
            with self.subTest(case.name), self.assertRaises(ValueError):
                quantity.parse(case.input)


if __name__ == "__main__":
    unittest.main()
