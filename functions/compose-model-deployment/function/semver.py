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

"""Semantic versions, reimplemented for DRA CEL selectors.

Mirrors the apiserver Semver CEL library (k8s.io/apiserver/pkg/cel/library/
semverlib.go), which parses with github.com/blang/semver/v4. Parsing is strict
by default (full major.minor.patch, no "v" prefix, no leading zeros) with a
lenient normalize overload; comparison is precedence-ordered, so prerelease
identifiers are significant and sort before the corresponding release, while
build metadata is ignored. A version() device attribute is pre-parsed strictly,
exactly as upstream pre-parses VersionValue attributes.
"""

from __future__ import annotations

from celpy import celtypes

# Character classes for validation. Numeric identifiers (the release numbers and
# numeric prerelease identifiers) allow only digits; alphanumeric identifiers
# (non-numeric prerelease and build identifiers) also allow letters and the
# hyphen. We validate against explicit sets rather than a regex to mirror blang's
# byte-by-byte checks exactly, including its specific error cases.
_NUMBERS = set("0123456789")
_ALPHANUM = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789")

# A semver has exactly three dot-separated release components (major.minor.patch).
_SEMVER_PARTS = 3


def _cmp[T: (int, str)](a: T, b: T) -> int:
    # Three-way compare: -1, 0, or +1, like Go's cmp/blang's Compare. Python has
    # no built-in spaceship operator; (a > b) - (a < b) evaluates the two bools
    # to 0/1 and subtracts, yielding -1/0/+1 without branching.
    return (a > b) - (a < b)


def _has_leading_zeroes(s: str) -> bool:
    # A numeric identifier may not have a leading zero (semver spec: "1.01.0" is
    # invalid). "0" itself is fine - a single zero is not a "leading" zero - so we
    # only flag a leading '0' when there's more than one character.
    return len(s) > 1 and s[0] == "0"


def _contains_only(s: str, allowed: set[str]) -> bool:
    # True when s is non-empty and every character is in the allowed set. The
    # non-empty check matters: an empty identifier (e.g. the "" between two dots
    # in "1..0", or a trailing "-") is always invalid, never vacuously valid.
    return len(s) > 0 and all(c in allowed for c in s)


class _PRVersion:
    """A single prerelease identifier (one dot-separated piece of the prerelease).

    A prerelease like "rc.1" is a list of these: "rc" (alphanumeric) and "1"
    (numeric). Each identifier is one of two kinds, and the kind decides ordering:
    a purely numeric identifier sorts before an alphanumeric one, numeric
    identifiers compare as integers, and alphanumeric identifiers compare as ASCII
    strings (semver spec rule 11). We store the kind in is_num and keep the value
    in whichever of num/string applies; the other is left at its zero value.
    """

    # __slots__ avoids a per-instance __dict__: a prerelease can have several
    # identifiers and these are created on every parse, so the memory and
    # attribute-access savings are worth the rigidity.
    __slots__ = ("is_num", "num", "string")

    def __init__(self, s: str) -> None:
        # An empty identifier (e.g. from "1.0.0-" or "1.0.0-a..b") is invalid.
        if s == "":
            raise ValueError("prerelease is empty")
        # All-digits -> numeric identifier. Checked first because "123" is both
        # all-numeric and all-alphanumeric, and the numeric kind takes precedence.
        if _contains_only(s, _NUMBERS):
            if _has_leading_zeroes(s):
                raise ValueError(f"numeric prerelease must not contain leading zeroes: {s!r}")
            self.num = int(s)
            self.is_num = True
            self.string = ""
        # Otherwise it must be a valid alphanumeric identifier (letters, digits,
        # hyphen). num is unused for this kind, so it's parked at 0.
        elif _contains_only(s, _ALPHANUM):
            self.num = 0
            self.is_num = False
            self.string = s
        else:
            raise ValueError(f"invalid character(s) in prerelease: {s!r}")

    def compare(self, o: _PRVersion) -> int:
        if self.is_num and not o.is_num:
            return -1
        if not self.is_num and o.is_num:
            return 1
        if self.is_num and o.is_num:
            return _cmp(self.num, o.num)
        # Python's str comparison is codepoint order, which matches blang's byte
        # comparison over the ASCII subset a valid identifier is restricted to.
        return _cmp(self.string, o.string)


class Semver:
    """A semantic version parsed per github.com/blang/semver/v4.

    Comparison is precedence-ordered: major, minor, patch, then prerelease
    (a version with no prerelease outranks one with a prerelease; identifiers
    compare element-wise; a longer prerelease list wins when its prefix is
    equal). Build metadata is ignored for ordering.
    """

    # Build metadata is intentionally NOT stored: it's parsed and validated (a
    # malformed build is a parse error) but ignored for ordering and equality, so
    # there's nothing to keep. pre is the list of _PRVersion identifiers, empty for
    # a release version with no prerelease.
    __slots__ = ("major", "minor", "patch", "pre")

    def __init__(self, major: int, minor: int, patch: int, pre: list[_PRVersion]) -> None:
        self.major = major
        self.minor = minor
        self.patch = patch
        self.pre = pre

    def compare(self, o: Semver) -> int:
        # Major, then minor, then patch; return at the first that differs.
        for a, b in ((self.major, o.major), (self.minor, o.minor), (self.patch, o.patch)):
            if a != b:
                return _cmp(a, b)
        # Release numbers equal - the prerelease breaks the tie. No prerelease
        # outranks a prerelease (1.0.0 > 1.0.0-rc1).
        if not self.pre and not o.pre:
            return 0
        if not self.pre:
            return 1
        if not o.pre:
            return -1
        # strict=False is REQUIRED, not a lazy default: the lists are expected to
        # differ in length (the longer-list rule below), and strict=True would
        # raise instead of stopping at the end of the shorter one.
        for pa, pb in zip(self.pre, o.pre, strict=False):
            c = pa.compare(pb)
            if c != 0:
                return c
        # Shared identifiers all equal, so the longer prerelease list wins
        # (1.0.0-rc.1 < 1.0.0-rc.1.1).
        return _cmp(len(self.pre), len(o.pre))

    # Equality and hashing are defined together (an object that defines __eq__
    # without __hash__ becomes unhashable) so a Semver can be a dict key / set
    # member, and so == agrees with precedence comparison.
    def __eq__(self, other: object) -> bool:
        # Equal iff precedence-equal. Note this means build metadata doesn't
        # affect equality, consistent with it being ignored for ordering.
        return isinstance(other, Semver) and self.compare(other) == 0

    def __hash__(self) -> int:
        # Hash over the same fields compare() looks at, so equal versions hash
        # equal. Each _PRVersion is reduced to its (is_num, num, string) triple
        # because _PRVersion has no __hash__ of its own; the outer tuple makes the
        # whole thing hashable.
        return hash((self.major, self.minor, self.patch, tuple((p.is_num, p.num, p.string) for p in self.pre)))


def parse(s: str) -> Semver:
    """Strict parse per blang/semver Parse: full major.minor.patch.

    This is the form used to pre-parse version() device attributes.
    """
    s = str(s)
    if s == "":
        raise ValueError("version string empty")
    parts = s.split(".", 2)
    if len(parts) != _SEMVER_PARTS:
        raise ValueError("no Major.Minor.Patch elements found")

    def num(component: str, label: str) -> int:
        if not _contains_only(component, _NUMBERS):
            raise ValueError(f"invalid character(s) in {label} number: {component!r}")
        if _has_leading_zeroes(component):
            raise ValueError(f"{label} number must not contain leading zeroes: {component!r}")
        return int(component)

    major = num(parts[0], "major")
    minor = num(parts[1], "minor")

    # The third dotted component carries the patch plus the optional suffixes:
    # patch[-prerelease][+build] (major and minor are bare numbers). Peel them off
    # to leave a clean patch number. Build is split off BEFORE prerelease and on
    # the FIRST delimiter, both deliberately: build metadata may itself contain
    # '-' (e.g. 3+build-7), so removing "+build" first stops that '-' being
    # mistaken for the prerelease delimiter; and the first '-' that remains is the
    # prerelease delimiter, while any later '-' belongs to a prerelease identifier
    # (e.g. 3-alpha-beta -> "alpha-beta"). Each suffix is optional, so find()
    # returning -1 leaves the corresponding string empty.
    patch_str = parts[2]
    build = ""
    pre_str = ""
    bi = patch_str.find("+")
    if bi != -1:
        build = patch_str[bi + 1 :]
        patch_str = patch_str[:bi]
    pi = patch_str.find("-")
    if pi != -1:
        pre_str = patch_str[pi + 1 :]
        patch_str = patch_str[:pi]
    patch = num(patch_str, "patch")

    pre = [_PRVersion(p) for p in pre_str.split(".")] if pre_str else []

    # Validate build metadata identifiers (ignored for ordering, but a malformed
    # one is a parse error upstream).
    if build:
        for ident in build.split("."):
            if ident == "" or not _contains_only(ident, _ALPHANUM):
                raise ValueError(f"invalid build metadata: {ident!r}")

    return Semver(major, minor, patch, pre)


def _normalize_and_parse(s: str) -> Semver:
    """Parse per the DRA library's normalizeAndParse (lenient).

    Like blang ParseTolerant but does NOT trim whitespace: strips a leading "v",
    splits into <=3 parts, strips leading zeros per part, fills missing trailing
    parts with "0" (a shortened version may not carry prerelease/build), then
    parses strictly.
    """
    s = str(s)
    if s.startswith("v"):
        s = s[1:]
    parts = s.split(".", 2)
    for i, part in enumerate(parts):
        if len(part) > 1:
            stripped = part.lstrip("0")
            if len(stripped) == 0 or stripped[0] not in "0123456789":
                stripped = "0" + stripped
            parts[i] = stripped
    if len(parts) < _SEMVER_PARTS:
        if any(c in "+-" for c in parts[-1]):
            raise ValueError("short version cannot contain PreRelease/Build meta data")
        while len(parts) < _SEMVER_PARTS:
            parts.append("0")
    return parse(".".join(parts))


def semver(s: str, normalize: celtypes.BoolType | None = None) -> Semver:
    """The CEL semver(<string>[, <bool>]) constructor."""
    if normalize is not None and bool(normalize):
        return _normalize_and_parse(s)
    return parse(s)


def is_semver(s: str, normalize: celtypes.BoolType | None = None) -> celtypes.BoolType:
    """The CEL isSemver(<string>[, <bool>]) predicate.

    Returns false for any parse failure, mirroring upstream (isSemver is true
    iff semver() would not error).
    """
    try:
        semver(s, normalize)
    except Exception:  # noqa: BLE001 - any parse failure is "not a semver"
        valid = False
    else:
        valid = True
    return celtypes.BoolType(valid)


def compare_to(a: Semver, b: Semver) -> celtypes.IntType:
    return celtypes.IntType(a.compare(b))


def is_greater_than(a: Semver, b: Semver) -> celtypes.BoolType:
    return celtypes.BoolType(a.compare(b) == 1)


def is_less_than(a: Semver, b: Semver) -> celtypes.BoolType:
    return celtypes.BoolType(a.compare(b) == -1)


def major(a: Semver) -> celtypes.IntType:
    return celtypes.IntType(a.major)


def minor(a: Semver) -> celtypes.IntType:
    return celtypes.IntType(a.minor)


def patch(a: Semver) -> celtypes.IntType:
    return celtypes.IntType(a.patch)
