"""Kubernetes resource.Quantity, reimplemented for DRA CEL selectors.

Mirrors the apiserver Quantity CEL library (k8s.io/apiserver/pkg/cel/library/
quantity.go) and resource.Quantity parsing/comparison, so quantity() in a DRA
device selector behaves the same here as upstream. Parsed values are rounded up
to nano scale, as resource.ParseQuantity does, so comparisons match
resource.Quantity.Cmp.
"""

from __future__ import annotations

import decimal
import re

from celpy import celtypes

# Quantity suffix multipliers. Binary (power-of-two) and decimal (SI) suffixes,
# matching k8s.io/apimachinery/pkg/api/resource suffix.go. Order matters: check
# two-character binary suffixes before single-character decimal ones.
_BINARY = {
    "Ki": decimal.Decimal(2**10),
    "Mi": decimal.Decimal(2**20),
    "Gi": decimal.Decimal(2**30),
    "Ti": decimal.Decimal(2**40),
    "Pi": decimal.Decimal(2**50),
    "Ei": decimal.Decimal(2**60),
}
# Single-character decimal SI suffixes (suffix.go), including nano/micro.
_DECIMAL = {
    "n": decimal.Decimal(10) ** -9,
    "u": decimal.Decimal(10) ** -6,
    "m": decimal.Decimal(10) ** -3,
    "k": decimal.Decimal(10) ** 3,
    "M": decimal.Decimal(10) ** 6,
    "G": decimal.Decimal(10) ** 9,
    "T": decimal.Decimal(10) ** 12,
    "P": decimal.Decimal(10) ** 15,
    "E": decimal.Decimal(10) ** 18,
}

# int64 range. asInteger() fails (upstream: error) outside this, matching
# resource.Quantity.AsInt64.
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1

# resource.ParseQuantity rounds every value up to nano (10^-9) scale before
# storing/comparing, so two quantities equal at nano resolution compare equal.
# We round the same way for parity with resource.Quantity.Cmp. Go's inf.RoundUp
# rounds away from zero, which is decimal.ROUND_UP (not ROUND_CEILING).
_NANO = decimal.Decimal(10) ** -9

# A Quantity number: optional sign, then digits[.digits] | digits. | .digits.
# Matches k8s resource.Quantity's <signedNumber>: a trailing dot ("5.") is
# allowed, mirroring the upstream parser.
_QUANTITY_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")

# A decimal exponent suffix: 'e' or 'E' followed by a signed integer (e.g.
# "256e3"). resource.Quantity treats this as a DecimalExponent suffix.
_EXPONENT = re.compile(r"^[eE][+-]?\d+$")


def parse(s: str) -> decimal.Decimal:
    """Parse a Kubernetes Quantity string into a Decimal of base units.

    Mirrors k8s resource.ParseQuantity: a signed number followed by a binary SI
    suffix (Ki, Mi, Gi, Ti, Pi, Ei), a decimal SI suffix (n, u, m, k, M, G, T,
    P, E, or none), or a decimal exponent (e/E + signed int). The result is
    rounded up to nano scale, as resource.Quantity does, so comparisons match
    resource.Quantity.Cmp. Raises ValueError on input resource.Quantity would
    reject (e.g. capital 'K', a comma, a bare suffix) so a malformed device
    capacity surfaces as a non-match rather than a silently-wrong number. Unlike
    UnmarshalJSON, the quantity() CEL constructor does NOT trim whitespace, so
    neither do we.
    """
    s = str(s)
    # Binary SI suffix (two characters; check first so "Mi" is not read as "M").
    for suffix, mult in _BINARY.items():
        if s.endswith(suffix):
            return _round_nano(_number(s[: -len(suffix)], s) * mult)
    # Decimal exponent (e/E + signed int): the exponent is part of the number.
    for i, c in enumerate(s):
        if c in "eE" and i > 0:
            if not _EXPONENT.match(s[i:]):
                break
            return _round_nano(_number(s[:i], s) * (decimal.Decimal(10) ** int(s[i + 1 :])))
    # Single-character decimal SI suffix (n, u, m, k, M, G, T, P, E).
    if s and s[-1] in _DECIMAL:
        return _round_nano(_number(s[:-1], s) * _DECIMAL[s[-1]])
    # No suffix.
    return _round_nano(_number(s, s))


def _number(num: str, original: str) -> decimal.Decimal:
    """Parse the numeric part of a quantity, or raise referencing the original."""
    if not _QUANTITY_NUMBER.match(num):
        raise ValueError(f"invalid quantity: {original!r}")
    # A trailing dot ("5.") is valid upstream but not for Decimal; drop it.
    if num.endswith("."):
        num = num[:-1]
    return decimal.Decimal(num)


def _round_nano(value: decimal.Decimal) -> decimal.Decimal:
    """Round up to nano scale, matching resource.ParseQuantity (inf.RoundUp)."""
    return value.quantize(_NANO, rounding=decimal.ROUND_UP)


def _cmp(a: decimal.Decimal, b: decimal.Decimal) -> int:
    return (a > b) - (a < b)


class Quantity:
    """A Kubernetes Quantity as a nano-rounded Decimal of base units.

    Implements the comparison, arithmetic, and conversion methods of the
    apiserver Quantity CEL type. There are deliberately no bare </>= operators:
    upstream's Quantity type doesn't register them either, only the methods.
    """

    __slots__ = ("value",)

    def __init__(self, value: decimal.Decimal):
        self.value = value

    def __eq__(self, other) -> bool:
        return isinstance(other, Quantity) and self.value == other.value

    def __hash__(self) -> int:
        return hash(self.value)


def quantity(s) -> Quantity:
    """The CEL quantity(<string>) constructor."""
    return Quantity(parse(s))


def is_quantity(s) -> celtypes.BoolType:
    """The CEL isQuantity(<string>) predicate.

    Returns false for any parse failure, mirroring upstream (isQuantity is true
    iff quantity() would not error).
    """
    try:
        parse(s)
    except Exception:  # noqa: BLE001 - any parse failure is "not a quantity"
        valid = False
    else:
        valid = True
    return celtypes.BoolType(valid)


def compare_to(a: Quantity, b: Quantity) -> celtypes.IntType:
    return celtypes.IntType(_cmp(a.value, b.value))


def is_greater_than(a: Quantity, b: Quantity) -> celtypes.BoolType:
    return celtypes.BoolType(a.value > b.value)


def is_less_than(a: Quantity, b: Quantity) -> celtypes.BoolType:
    return celtypes.BoolType(a.value < b.value)


def sign(a: Quantity) -> celtypes.IntType:
    return celtypes.IntType(_cmp(a.value, decimal.Decimal(0)))


def is_integer(a: Quantity) -> celtypes.BoolType:
    return celtypes.BoolType(a.value == a.value.to_integral_value() and _INT64_MIN <= a.value <= _INT64_MAX)


def as_integer(a: Quantity) -> celtypes.IntType:
    if a.value != a.value.to_integral_value() or not (_INT64_MIN <= a.value <= _INT64_MAX):
        raise ValueError("cannot convert value to integer")
    return celtypes.IntType(int(a.value))


def as_approximate_float(a: Quantity) -> celtypes.DoubleType:
    return celtypes.DoubleType(float(a.value))


def add(a: Quantity, b) -> Quantity:
    return Quantity(a.value + _operand(b))


def sub(a: Quantity, b) -> Quantity:
    return Quantity(a.value - _operand(b))


def _operand(b) -> decimal.Decimal:
    """add/sub accept a Quantity or an integer (upstream has both overloads)."""
    if isinstance(b, Quantity):
        return b.value
    return decimal.Decimal(int(b))
