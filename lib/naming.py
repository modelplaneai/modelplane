"""Name generation for composed resources."""

DNS_LABEL_MAX = 63


def replica_name(deployment_name: str, cluster_name: str) -> str:
    """Derive a deterministic ModelReplica name.

    Deterministic names are needed so the deployment function knows the
    LLMInferenceService name on the remote cluster for URL rewriting.
    """
    return f"{deployment_name}-{cluster_name}"[:DNS_LABEL_MAX]


def endpoint_name(deployment_name: str, cluster_name: str) -> str:
    """Derive a deterministic ModelEndpoint name.

    Modelplane composes one ModelEndpoint per ModelReplica. The name
    needs to be unique per (deployment, cluster) within a namespace.
    """
    return f"{deployment_name}-{cluster_name}"[:DNS_LABEL_MAX]


def llmis_name(deployment_name: str) -> str:
    """Derive the LLMInferenceService name on the remote cluster.

    Each replica composes one LLMInferenceService on its target cluster.
    The name is the deployment name (truncated to a DNS label), which is
    uniform across replicas so the control plane HTTPRoute can rewrite
    to the same path on every backend.
    """
    return deployment_name[:DNS_LABEL_MAX]
