from __future__ import annotations

"""mp models — delegates to kubectl get clustermodels."""

import subprocess
import sys

import click


@click.command("models")
def models() -> None:
    """List available models from the catalog.

    Delegates to: kubectl get clustermodels
    """
    result = subprocess.run(["kubectl", "get", "clustermodels"], capture_output=False)
    sys.exit(result.returncode)
