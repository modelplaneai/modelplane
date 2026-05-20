"""Name generation for composed resources.

All Kubernetes resource names must be valid DNS labels (at most 63
characters). Names built by combining user-supplied components (e.g.
deployment + cluster) can exceed this limit. Every function in this
module uses dns_name() to produce a safe name: the full string is used
when it fits; otherwise the name is truncated and a 5-character hash
suffix is appended to preserve uniqueness.
"""

import hashlib

DNS_LABEL_MAX = 63
_HASH_LEN = 5


def dns_name(*parts: str, sep: str = "-") -> str:
    """Build a DNS-safe name from one or more parts joined by sep.

    A deterministic hash suffix is always appended so that names are
    visually consistent regardless of length. The hash is the first
    _HASH_LEN hex characters of a SHA-256 digest of the full
    (untruncated) joined name. The prefix is truncated to fit within
    DNS_LABEL_MAX.
    """
    full = sep.join(parts)
    h = hashlib.sha256(full.encode()).hexdigest()[:_HASH_LEN]
    # Truncate, leaving room for "-" + hash suffix. Strip any trailing
    # hyphen left by the truncation so we don't get "foo---a1b2c".
    max_prefix = DNS_LABEL_MAX - _HASH_LEN - 1
    prefix = full[:max_prefix].rstrip("-")
    return f"{prefix}-{h}"


def replica_name(deployment_name: str, cluster_name: str) -> str:
    """Derive a deterministic ModelReplica name.

    Deterministic names are needed so the deployment function knows the
    LLMInferenceService name on the remote cluster for URL rewriting.
    """
    return dns_name(deployment_name, cluster_name)


def endpoint_name(deployment_name: str, cluster_name: str) -> str:
    """Derive a deterministic ModelEndpoint name.

    Modelplane composes one ModelEndpoint per ModelReplica. The name
    needs to be unique per (deployment, cluster) within a namespace.
    """
    return dns_name(deployment_name, cluster_name)


def llmis_name(deployment_name: str) -> str:
    """Derive the LLMInferenceService name on the remote cluster.

    Each replica composes one LLMInferenceService on its target cluster.
    The name is the deployment name, which is uniform across replicas so
    the control plane HTTPRoute can rewrite to the same path on every
    backend.
    """
    return dns_name(deployment_name)
