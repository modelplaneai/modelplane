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

"""The types every serving stack component list is made of.

A stack file - hand-written, or written into clouds/generated/ by a
build-time generator (`nix run .#stacks`) - is a COMPONENTS list of
these entries. The list is the intermediate representation: a generator
is one producer of it, never the format, so a hand-written file and a
generated one are interchangeable. The function renders a Chart as a
provider-helm Release and a Manifests as provider-kubernetes Objects;
see design/serving-stack-generation.md.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

# The clouds and stacks the join can select - the values of ServingStack
# spec.cloud and spec.stack, so a wrong or unsupported value fails type
# checking at the caller.
Cloud = Literal["GKE", "EKS", "AKS", "Nebius", "Vultr", "Civo", "Existing"]
Stack = Literal["Standard", "Dynamo"]

# Who a component belongs to when the cluster provides the substrate
# (ServingStack spec.components: Provided). A substrate component is
# skipped there - the cluster already runs it - and its `requires`
# entries are checked in its place. A config component is Modelplane's
# own configuration on top of the substrate (the gateway namespace and
# EnvoyProxy, the KAI Queues, the ModelExpress server) and composes in
# every mode.
Role = Literal["substrate", "config"]


@dataclass
class RequiredCRD:
    """A CRD a provided cluster must serve in a substrate component's place.

    Checked through the aggregated APIService the API server registers
    for every served group-version (<version>.<group>), not the CRD
    itself: a CRD manifest carries its whole OpenAPI schema, too large
    and too deeply nested to haul through the composition pipeline on
    every reconcile. Served-API granularity is the whole checkable
    surface anyway - chart and controller versions aren't recoverable
    from a CRD either, so they belong in the component's `unchecked`
    notes. The generated docs still name the CRD itself.

    `versions` holds exactly one entry today: the APIService check
    encodes one group-version, and join() fails closed on more until a
    requirement actually needs any-of semantics.

    `key` suffixes the composed-resource key (see requirement_keys), so
    it only needs to be unique within one component's requires list.
    """

    key: str
    name: str  # <plural>.<group>, the CRD's metadata.name
    versions: list[str]  # served versions accepted


@dataclass
class RequiredObject:
    """A cluster-scoped object a provided cluster must already carry.

    For substrate whose footprint isn't a CRD: the NVIDIA DRA driver is
    checked through the gpu.nvidia.com DeviceClass it registers.
    Observing it also proves the cluster serves the object's API group,
    so a RequiredObject on a versioned core API doubles as a floor check
    (resource.k8s.io/v1 means Kubernetes 1.34). `ready` is an optional
    CEL query over the observed manifest, as on Manifests.
    """

    key: str
    api_version: str
    kind: str
    name: str
    ready: str | None = None


Requirement = RequiredCRD | RequiredObject


@dataclass
class Chart:
    """A Helm chart the serving stack installs.

    `key` names the composed resource and `release` the Helm release
    (via provider-helm's external-name). Both are Modelplane's, not the
    upstream catalog's, so a component's identity survives upstream
    renames: provider-helm upgrades in place only while the release name
    holds still, and the mp- prefix reserves a namespace so Modelplane
    can't adopt a same-named release a user already runs.

    `depends_on` names components this one needs, by key, resolved
    against the joined list for a cloud and stack. It drives ordering in
    both directions: teardown (a dependency outlives its dependents) and
    install (a dependent is first created once its dependencies report
    Ready).

    `wait` renders as provider-helm's wait (helm --wait): the release
    reports Ready only once its workloads roll out, not when Helm
    accepts the manifests. Set it on every chart another component
    depends on, so the install gate orders on health rather than
    deploy - the generator derives it from the dependency edges, and the
    hand-written files state it where a cross-half edge lands on them.

    `role`, `requires`, `unchecked` and `not_needed` describe the
    component when the cluster provides the substrate instead of
    Modelplane installing it (spec.components: Provided, Existing
    clusters only). `requires` is what gets checked in the component's
    place; `unchecked` is what a provided cluster must also supply but
    no observe Object can verify (controllers running, node drivers,
    values wiring), stated for the generated requirements docs; and
    `not_needed` is what the component would normally bring that
    Modelplane doesn't use, so users know what they can skip. Only the
    Existing halves carry them - the generated clouds never join in
    Provided mode.
    """

    key: str
    release: str
    namespace: str
    chart: str
    repository: str
    version: str
    wait: bool = False
    depends_on: list[str] = field(default_factory=list)
    values: dict[str, Any] | None = None
    role: Role = "substrate"
    requires: list[Requirement] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)
    not_needed: list[str] = field(default_factory=list)


@dataclass
class Manifests:
    """Raw manifests the serving stack applies.

    For the parts of the stack that have never been chart-shaped: CRDs
    vendored from upstream releases, the gateway objects, the
    kai-scheduler Queues, the ModelExpress server bundle. `key` and
    `depends_on` behave as on Chart.

    `ready` is an optional CEL query over the observed manifest,
    applied to every doc in the entry (see fn.py's _k8s_object): use it
    when readiness must reflect a controller-populated status field,
    and keep an entry to one doc when only that doc has one.

    `role`, `requires`, `unchecked` and `not_needed` behave as on
    Chart. Most Manifests entries are Modelplane's own configuration
    (role config); the substrate ones are the vendored CRD bundles a
    provided cluster brings itself.
    """

    key: str
    manifests: list[dict[str, Any]]
    depends_on: list[str] = field(default_factory=list)
    ready: str | None = None
    role: Role = "substrate"
    requires: list[Requirement] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)
    not_needed: list[str] = field(default_factory=list)


# A plain assignment rather than a `type` statement: the packages
# declare requires-python >=3.11, and the `type` keyword needs 3.12.
# Type checkers treat this as an implicit alias either way.
Component = Chart | Manifests


def doc_keys(component: Component) -> list[str]:
    """Composed-resource keys for a component, in manifest order.

    A Chart, and a Manifests entry with one doc, renders under the
    entry's key verbatim. A multi-doc Manifests renders one Object per
    doc, keyed `<key>-<metadata.name>`. Renaming a key deletes and
    recreates the composed resource - for an Object that deletes the
    remote object - so a bundle growing from one doc to several (or
    shrinking to one) renames its keys; keep that reviewable.
    """
    if isinstance(component, Chart) or len(component.manifests) == 1:
        return [component.key]
    return [f"{component.key}-{doc['metadata']['name']}" for doc in component.manifests]


def requirement_keys(component: Component) -> list[str]:
    """Composed-resource keys of a component's checks in Provided mode.

    One observe Object per requirement, keyed
    `require-<component.key>-<requirement.key>`. The same rename caveat
    as doc_keys applies in the harmless direction: renaming recreates
    the Object, but an observe-only Object touches nothing remote.
    """
    return [f"require-{component.key}-{r.key}" for r in component.requires]
