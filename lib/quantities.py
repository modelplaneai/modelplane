"""Kubernetes resource quantity parsing."""


def parse_quantity(q: str) -> int:
    """Parse a Kubernetes resource quantity string to bytes.

    Supports binary suffixes: Gi, Mi, Ti. Returns 0 for unparseable values.

    This does not implement the full Kubernetes quantity grammar — it doesn't
    handle decimal suffixes (m, k, M, G, T) or exponential notation. That's
    sufficient for this project where all quantities come from XRD fields that
    use binary suffixes.
    """
    if not q:
        return 0
    q = q.strip()
    if q.endswith("Gi"):
        return _parse_number(q[:-2]) * 1024 * 1024 * 1024
    if q.endswith("Mi"):
        return _parse_number(q[:-2]) * 1024 * 1024
    if q.endswith("Ti"):
        return _parse_number(q[:-2]) * 1024 * 1024 * 1024 * 1024
    try:
        return int(q)
    except ValueError:
        return 0


def _parse_number(s: str) -> int:
    """Parse a numeric string that may contain a decimal point.

    Rounds to the nearest integer. Handles both "40" and "40.5".
    """
    try:
        return int(round(float(s)))
    except ValueError:
        return 0
