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
from typing import Any


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
    against the joined list for a cloud and stack. It drives teardown
    ordering (a dependency outlives its dependents); installs stay
    concurrent and rely on Helm retrying.
    """

    key: str
    release: str
    namespace: str
    chart: str
    repository: str
    version: str
    depends_on: list[str] = field(default_factory=list)
    values: dict[str, Any] | None = None


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
    """

    key: str
    manifests: list[dict[str, Any]]
    depends_on: list[str] = field(default_factory=list)
    ready: str | None = None


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
