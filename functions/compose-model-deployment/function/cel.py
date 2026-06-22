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

"""DRA CEL evaluation for ModelDeployment.nodeSelector.

The design lets an ML team write CEL selectors that the fleet scheduler
evaluates against each candidate pool's devices, in the same dialect a user
would write in a DRA ResourceClaim ([KEP-4381]):

    device.attributes["gpu.nvidia.com"].cudaComputeCapability.isGreaterThan(semver("9.0.0")) &&
    device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("141Gi")) >= 0

This reimplements Kubernetes' DRA device-selector CEL surface
(k8s.io/dynamic-resource-allocation/cel) on top of the pure-Python celpy
evaluator, so an expression that selects a device upstream selects the same
device here. Each selector is evaluated against ONE device, exposed as `device`:

* device.driver                          -> the device's DRA driver (a string)
* device.attributes["<domain>"].<name>   -> a typed attribute under a domain
* device.capacity["<domain>"].<name>     -> a capacity Quantity

Upstream builds the `device` input by splitting each attribute/capacity
QualifiedName into (domain, id): a bare name defaults to the device's driver as
its domain, a "domain/id" name is split on the first "/". A single device can
therefore expose several domains. Looking up an UNKNOWN domain yields an empty
map (upstream's mapper.Find default), not an error; looking up an unknown id
within a domain is an error. We mirror both.

The quantity() and semver() function families live in the sibling `quantity`
and `semver` modules, which match the apiserver CEL libraries those names come
from.

Deliberate divergences we CANNOT match exactly (documented, not bugs):

* Extra base-environment libraries. A DRA selector is compiled against the
  apiserver base CEL env, which also registers URLs, Regex, IP, CIDR, Format,
  Sets, two-variable comprehensions, the apiserver Lists library (isSorted/sum/
  max/min/indexOf/...), and Strings v2. celpy provides the CEL standard macros
  and some of these but not the Kubernetes-specific ones. Selectors that use
  those functions will fail to compile here. Device selectors in practice use
  attribute/capacity comparisons with quantity()/semver(), which we cover.
* List-type attributes and the includes() function (KEP feature-gated,
  DRAListTypeAttributes, ~v1.36). We only model scalar attributes
  (string/bool/int/version), matching the default-off behavior.
* Runtime cost limits. Upstream caps a selector at CELSelectorExpressionMaxCost
  (1,000,000 cost units) and the expression at 10 KiB. celpy has no cost
  accounting, so we don't enforce the cost ceiling (the XRD bounds expression
  length).
* cel.bind / ext.Bindings is available upstream for domain reuse; celpy parses
  cel.bind, so simple uses work, but we don't guarantee parity with the full
  ext.Bindings semantics.
* Output type checking. Upstream rejects a selector that doesn't evaluate to a
  bool at compile time. celpy doesn't expose the output type, so we can't reject
  at compile; instead a non-bool result is treated as a non-match (never a
  truthy coercion), so a stray non-bool selector excludes devices rather than
  spuriously matching them.
* Member vs global call style. celpy dispatches x.method(y) to the function
  method(x, y), so it can't distinguish a member call from a global one. We
  therefore accept both spellings of every function, whereas upstream is strict
  per-overload: quantity()'s sign is a GLOBAL function (sign(q) only; q.sign()
  is a compile error upstream), while compareTo/isGreaterThan/isLessThan are
  member-only. This only makes us MORE permissive (an extra accepted spelling),
  never changing which devices a well-formed selector matches.
* has() on an index. cel-go's has() macro accepts only a field selection
  (has(x.y)), not an index (has(x["y"])), so has(device.attributes["domain"])
  is a compile error upstream; the domain-presence idiom is
  "domain" in device.attributes. celpy accepts the has(index) form too, so
  again we're more permissive. A selector that needs to test domain presence
  should use the in form, which compiles both here and upstream.

Operationally: a device that errors on evaluation (unknown id, malformed
quantity/version, type mismatch) is treated as a non-match rather than failing
the whole reconcile. A malformed expression (compile error) is a user error,
surfaced via CELCompileError.

[KEP-4381]: https://github.com/kubernetes/enhancements/tree/master/keps/sig-node/4381-dra-structured-parameters
"""

from __future__ import annotations

import typing

import celpy
from celpy import celtypes

from function import quantity, semver


class CELCompileError(Exception):
    """Raised when a nodeSelector CEL expression fails to compile."""


# compareTo/isGreaterThan/isLessThan are shared method names: a CEL selector
# calls them on either a Quantity or a Semver, so dispatch on the operand type.
# Both operands are always the same kind (the selector compares like to like).
# isinstance narrows only the first operand, so cast the second to match; a
# mixed-type comparison is a malformed selector that compilation should reject.
def _compare_to(a: semver.Semver | quantity.Quantity, b: semver.Semver | quantity.Quantity) -> celtypes.IntType:
    if isinstance(a, semver.Semver):
        return semver.compare_to(a, typing.cast("semver.Semver", b))
    return quantity.compare_to(a, typing.cast("quantity.Quantity", b))


def _is_greater_than(a: semver.Semver | quantity.Quantity, b: semver.Semver | quantity.Quantity) -> celtypes.BoolType:
    if isinstance(a, semver.Semver):
        return semver.is_greater_than(a, typing.cast("semver.Semver", b))
    return quantity.is_greater_than(a, typing.cast("quantity.Quantity", b))


def _is_less_than(a: semver.Semver | quantity.Quantity, b: semver.Semver | quantity.Quantity) -> celtypes.BoolType:
    if isinstance(a, semver.Semver):
        return semver.is_less_than(a, typing.cast("semver.Semver", b))
    return quantity.is_less_than(a, typing.cast("quantity.Quantity", b))


# Registered into every celpy program. celpy dispatches method-call syntax
# x.method(y) to the function method(x, y), so the same entries serve both the
# method-call and free-function styles.
_FUNCTIONS = {
    "quantity": quantity.quantity,
    "isQuantity": quantity.is_quantity,
    "sign": quantity.sign,
    "isInteger": quantity.is_integer,
    "asInteger": quantity.as_integer,
    "asApproximateFloat": quantity.as_approximate_float,
    "add": quantity.add,
    "sub": quantity.sub,
    "semver": semver.semver,
    "isSemver": semver.is_semver,
    "major": semver.major,
    "minor": semver.minor,
    "patch": semver.patch,
    "compareTo": _compare_to,
    "isGreaterThan": _is_greater_than,
    "isLessThan": _is_less_than,
}


class _DefaultMap(celtypes.MapType):
    """A map whose missing-key lookup returns an empty map.

    Mirrors upstream's newStringInterfaceMapWithDefault: device.attributes and
    device.capacity return an empty map for an UNKNOWN domain (not an error).
    Looking up an unknown id within a (known or defaulted-empty) domain still
    errors, because the inner maps are plain MapTypes.
    """

    def __missing__(self, key: celtypes.MapKeyTypes) -> celtypes.MapType:
        return celtypes.MapType()


class Program:
    """A compiled DRA CEL selector, reusable across devices."""

    def __init__(self, expr: str) -> None:
        env = celpy.Environment()
        # Any compile-time failure is a malformed expression - a user error.
        # celpy raises CELParseError for syntax errors, but we catch broadly so
        # no compile failure escapes as an unhandled reconcile crash; the caller
        # turns CELCompileError into an InvalidNodeSelector condition.
        try:
            ast = env.compile(expr)
            # celpy types extension functions as returning only its base value
            # union, which excludes the Quantity and Semver types our functions
            # return, so it rejects _FUNCTIONS despite celpy supporting them.
            self._prgm = env.program(ast, functions=_FUNCTIONS)  # ty: ignore[invalid-argument-type]
        except Exception as e:
            raise CELCompileError(str(e)) from e

    def matches(self, device: dict) -> bool:
        """Evaluate the selector against one device.

        `device` is the raw dict from a pool's status.gpuPools devices
        entry, shaped like:

            device = {
                "driver": "<driver>",
                "count": <int>,
                "attributes": {"<name>": {"string"|"version"|...: <v>}},
                "capacity":   {"<name>": {"value": "<Quantity>"}},
            }

        Attribute/capacity names may be qualified ("domain/id"); a bare name
        uses the driver as its domain. A device that errors (unknown id,
        malformed quantity/version, type mismatch) is a non-match.
        """
        try:
            activation = {"device": _device_activation(device)}
            result = self._prgm.evaluate(activation)
        except Exception:  # noqa: BLE001 - any eval failure is a non-match, never a crash
            # Unknown id, malformed quantity/version, type mismatch, etc. The
            # device simply does not match. We catch broadly (not just
            # CELEvalError/ValueError) because celpy can surface other error
            # types for some expressions, and arbitrary user CEL evaluated
            # against arbitrary device data must never crash the whole reconcile
            # - a non-match is always the safe outcome.
            return False
        # Enforce the bool output type the module docstring describes: a non-bool
        # result is a non-match, never a truthy coercion. isinstance, not ==:
        # celpy's BoolType.__eq__ raises on a cross-type compare.
        return isinstance(result, (bool, celtypes.BoolType)) and bool(result)


def _split_qualified(name: str, default_domain: str) -> tuple[str, str]:
    """Split a QualifiedName into (domain, id), defaulting the domain.

    Mirrors upstream parseQualifiedName: split on the FIRST "/"; a name without
    "/" uses the device's driver as its domain.
    """
    sep = name.find("/")
    if sep == -1:
        return default_domain, name
    return name[:sep], name[sep + 1 :]


def _device_activation(device: dict) -> celtypes.MapType:
    """Build the `device` activation map for one device.

    Groups attributes and capacity into per-domain maps keyed by the qualified
    name's domain (driver-defaulted), exactly as upstream DeviceMatches does, so
    a device can expose several domains. Unknown-domain lookups return an empty
    map. Raises ValueError on a malformed Quantity or version attribute.
    """
    # attributes/capacity are optional, so a device without them simply omits the
    # key (the dicts come from model_dump(exclude_none=True), so a present value
    # is never None). driver is XRD-required on a real device, but default it so
    # an expression that never reads device.driver still evaluates.
    driver = device.get("driver", "")

    out = celtypes.MapType()
    out[celtypes.StringType("driver")] = celtypes.StringType(driver)

    attributes = _DefaultMap()
    for name, raw in device.get("attributes", {}).items():
        domain, ident = _split_qualified(name, driver)
        bucket = typing.cast(celtypes.MapType, attributes.setdefault(celtypes.StringType(domain), celtypes.MapType()))
        # celpy's MapType value type excludes the Semver extension type.
        bucket[celtypes.StringType(ident)] = _attribute_value(raw)  # ty: ignore[invalid-assignment]
    out[celtypes.StringType("attributes")] = attributes

    capacity = _DefaultMap()
    for name, raw in device.get("capacity", {}).items():
        value = raw.get("value")
        if value is None:
            continue
        domain, ident = _split_qualified(name, driver)
        bucket = typing.cast(celtypes.MapType, capacity.setdefault(celtypes.StringType(domain), celtypes.MapType()))
        # celpy's MapType value type excludes the Quantity extension type.
        bucket[celtypes.StringType(ident)] = quantity.quantity(value)  # ty: ignore[invalid-assignment]
    out[celtypes.StringType("capacity")] = capacity

    return out


def _attribute_value(entry: dict) -> semver.Semver | celtypes.Value:
    """Convert one typed attribute value object to its CEL value.

    A version attribute is pre-parsed to a Semver (strict), matching upstream
    which pre-parses VersionValue attributes; the selector compares it with
    semver() directly. Other typed values (string/bool/int) pass through as
    their natural CEL type. The XRD enforces exactly one field is set.
    """
    if entry.get("version") is not None:
        return semver.semver(entry["version"])
    for field in ("string", "bool", "int"):
        if entry.get(field) is not None:
            return celpy.json_to_cel(entry[field])
    # No supported value: upstream returns "unsupported attribute value" error.
    raise ValueError("unsupported attribute value")
