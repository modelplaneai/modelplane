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

"""Names for the objects a ModelService composes on a gateway's cluster.

Everything lands in one namespace there, so a name has to carry the
control-plane namespace it came from or two services called "assistant" in
different namespaces would collide. Names are derived rather than generated
because the objects reference each other: an AIGatewayRoute's backendRefs name
AIServiceBackends, which name Backends, and a BackendSecurityPolicy names an
AIServiceBackend back.
"""

import hashlib

# Kubernetes object names are limited to 253 characters. A ModelService's
# namespace, its name, and an endpoint's name can each be 253 on their own, so
# a joined name can overflow and has to be shortened deterministically.
_MAX_NAME = 253

# Enough of a SHA-256 to make a collision between two truncated names
# implausible, while leaving most of the readable prefix intact.
_HASH_LEN = 8


def _fit(name: str) -> str:
    """Shorten a name to fit Kubernetes' limit, keeping it unique and stable.

    Truncating alone would map two long names onto one object, so a hash of the
    whole name replaces the tail. The hash is of the untruncated name, so the
    result is stable across reconciles and identical in every function that
    derives it.
    """
    if len(name) <= _MAX_NAME:
        return name
    digest = hashlib.sha256(name.encode()).hexdigest()[:_HASH_LEN]
    return f"{name[: _MAX_NAME - _HASH_LEN - 1]}-{digest}"


def route(namespace: str, service: str) -> str:
    """The AIGatewayRoute for a ModelService.

    Also the name of the HTTPRoute the AI Gateway generates from it, and so of
    the Envoy route the access log reports.
    """
    return _fit(f"{namespace}-{service}")


def backend(namespace: str, service: str, endpoint: str) -> str:
    """The Backend, AIServiceBackend and BackendSecurityPolicy for one endpoint
    of one ModelService.

    Scoped to the service rather than the endpoint alone. Two ModelServices
    selecting one endpoint each get their own copies, which costs a little
    duplicated config and avoids two composites owning one object.
    """
    return _fit(f"{namespace}-{service}-{endpoint}")


def credential(namespace: str, service: str, endpoint: str) -> str:
    """The propagated Secret holding one endpoint's backend credential."""
    return _fit(f"{namespace}-{service}-{endpoint}-credential")


def model(namespace: str, service: str) -> str:
    """The name a caller passes as the request's model.

    Namespaced, so two services can't collide and the namespace serving a
    caller is legible in what it passes. Not run through _fit: this is a value
    in a request body and a header match, not an object name, and shortening it
    would make the name a caller uses depend on a hash.
    """
    return f"{namespace}/{service}"


def cluster_ca(cluster: str) -> str:
    """The ConfigMap holding one cluster gateway's CA certificate.

    Named for the cluster rather than the service, because a CA certificate is a
    fact about a cluster and identical for every service that reaches it. Several
    ModelServices composing the same ConfigMap with the same content is
    server-side apply converging, not a conflict.
    """
    return _fit(f"cluster-ca-{cluster}")
