"""DNS label sanitization and name generation."""

import re

DNS_LABEL_MAX = 63


def to_dns_label(s: str) -> str:
    """Sanitize a string to a valid DNS-1035 label.

    DNS-1035 labels must be lowercase, start with a letter, end with an
    alphanumeric, contain only [a-z0-9-], and be at most 63 characters.
    """
    s = s.lower()
    s = re.sub(r"[^a-z0-9-]", "-", s)  # Replace invalid chars with hyphens
    s = re.sub(r"-+", "-", s)  # Collapse consecutive hyphens
    s = s.strip("-")
    s = f"model-{s}"
    return s[:DNS_LABEL_MAX]


def placement_name(deployment_name: str, ie_name: str) -> str:
    """Derive a deterministic ModelPlacement name.

    Deterministic names are needed so the deployment function knows the
    LLMInferenceService name on the remote cluster for URL rewriting.
    """
    return f"{deployment_name}-{ie_name}"[:DNS_LABEL_MAX]
