from __future__ import annotations

"""Table formatting and output helpers."""

import click
from tabulate import tabulate


def table(headers: list[str], rows: list[list[str]]) -> None:
    """Print a formatted table."""
    click.echo(tabulate(rows, headers=headers, tablefmt="plain"))


def kv(pairs: list[tuple[str, str]]) -> None:
    """Print key-value pairs aligned."""
    if not pairs:
        return
    max_key = max(len(k) for k, _ in pairs)
    for key, value in pairs:
        click.echo(f"{key + ':':<{max_key + 2}} {value}")


def condition_ready(resource: dict) -> str:
    """Extract Ready condition status from a resource."""
    conditions = resource.get("status", {}).get("conditions", [])
    for c in conditions:
        if c.get("type") == "Ready":
            return "Ready" if c.get("status") == "True" else c.get("reason", "Not Ready")
    return "Unknown"
