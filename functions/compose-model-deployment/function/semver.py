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

_NUMBERS = set("0123456789")
_ALPHANUM = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789")

# A semver has exactly three dot-separated release components.
_SEMVER_PARTS = 3


def _cmp(a, b) -> int:
    return (a > b) - (a < b)


def _has_leading_zeroes(s: str) -> bool:
    return len(s) > 1 and s[0] == "0"


def _contains_only(s: str, allowed: set[str]) -> bool:
    return len(s) > 0 and all(c in allowed for c in s)


class _PRVersion:
    """A prerelease identifier: numeric (sorts first) or alphanumeric."""

    __slots__ = ("is_num", "num", "string")

    def __init__(self, s: str):
        if s == "":
            raise ValueError("prerelease is empty")
        if _contains_only(s, _NUMBERS):
            if _has_leading_zeroes(s):
                raise ValueError(f"numeric prerelease must not contain leading zeroes: {s!r}")
            self.num = int(s)
            self.is_num = True
            self.string = ""
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
        # Both alphanumeric: lexical (ASCII) comparison.
        return _cmp(self.string, o.string)


class Semver:
    """A semantic version parsed per github.com/blang/semver/v4.

    Comparison is precedence-ordered: major, minor, patch, then prerelease
    (a version with no prerelease outranks one with a prerelease; identifiers
    compare element-wise; a longer prerelease list wins when its prefix is
    equal). Build metadata is ignored for ordering.
    """

    __slots__ = ("major", "minor", "patch", "pre")

    def __init__(self, major: int, minor: int, patch: int, pre: list[_PRVersion]):
        self.major = major
        self.minor = minor
        self.patch = patch
        self.pre = pre

    def compare(self, o: Semver) -> int:
        for a, b in ((self.major, o.major), (self.minor, o.minor), (self.patch, o.patch)):
            if a != b:
                return _cmp(a, b)
        if not self.pre and not o.pre:
            return 0
        if not self.pre:
            return 1
        if not o.pre:
            return -1
        for pa, pb in zip(self.pre, o.pre, strict=False):
            c = pa.compare(pb)
            if c != 0:
                return c
        # Prefix-equal: the longer prerelease list is greater.
        return _cmp(len(self.pre), len(o.pre))

    def __eq__(self, other) -> bool:
        return isinstance(other, Semver) and self.compare(other) == 0

    def __hash__(self) -> int:
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


def semver(s, normalize=None) -> Semver:
    """The CEL semver(<string>[, <bool>]) constructor."""
    if normalize is not None and bool(normalize):
        return _normalize_and_parse(s)
    return parse(s)


def is_semver(s, normalize=None) -> celtypes.BoolType:
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
