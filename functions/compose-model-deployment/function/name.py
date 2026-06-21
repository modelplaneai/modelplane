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

"""Derive the identifiers a ModelDeployment uses for its replicas.

One place owns every name a replica's resources are known by, so the
composed ModelReplica, its ModelEndpoint, and the backend workloads all
agree. Two kinds of identifier live here:

* Object names (replica): the metadata.name of a replica's ModelReplica and
  ModelEndpoint - a DNS-label-safe, stable, opaque handle. The replica name is
  also the per-placement routing key baked into the endpoint URL, so both the
  replica and the endpoint must derive it identically.
* Desired-resource keys (replica_key/endpoint_key): function-local handles into
  the desired-resources map. Not Kubernetes names, so they need no DNS-safety -
  they only have to be distinct per co-located replica and deterministic from
  observed state.
"""

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from function import scheduling

# DNS label limit and hash suffix length for opaque child names, matching
# crossplane's resource.child_name so names stay valid 63-char DNS labels.
_DNS_LABEL_MAX = 63
_HASH_LEN = 5


def opaque_name(visible: str, *discriminators: str) -> str:
    """A DNS-label-safe name that reads as visible-<hash>.

    Like crossplane's resource.child_name, but the discriminators are folded
    into the hash WITHOUT appearing in the readable prefix. child_name joins all
    of its parts into both the prefix and the hashed value, so it can't hide a
    part; we hash the visible name plus the discriminators together, then keep
    only the visible name in the prefix. The hash makes co-located replicas
    distinct and stable; identity lives in labels, not the name (like a Pod's
    name doesn't encode its node).
    """
    full = "-".join((visible, *discriminators))
    h = hashlib.sha256(full.encode()).hexdigest()[:_HASH_LEN]
    max_prefix = _DNS_LABEL_MAX - _HASH_LEN - 1
    prefix = visible[:max_prefix].rstrip("-")
    return f"{prefix}-{h}"


def replica(deployment_name: str, candidate: "scheduling.Candidate") -> str:
    """The opaque, DNS-safe name for a replica's resources.

    Hashed from (deployment, cluster, index) so co-located replicas get distinct
    names. Cluster and index are not exposed in the readable prefix - identity
    lives in labels, the name is just a stable handle (matching Crossplane's
    opaque-name convention, and how a Pod's name doesn't encode its node).
    """
    return opaque_name(deployment_name, candidate.name, str(candidate.index))


def replica_key(candidate: "scheduling.Candidate") -> str:
    """Function-local desired-resource handle for a replica's (cluster, index).

    Distinct per co-located replica so two replicas on one cluster don't share a
    desired-resource slot. Deterministic from observed state, so a replica
    placed this reconcile maps back to the same handle once observed.
    """
    return f"replica-{candidate.name}-{candidate.index}"


def endpoint_key(candidate: "scheduling.Candidate") -> str:
    """Function-local desired-resource handle for a replica's ModelEndpoint."""
    return f"endpoint-{candidate.name}-{candidate.index}"
