from __future__ import annotations

"""mp envs — delegates to kubectl get inferenceenvironments."""

import subprocess
import sys

import click


@click.command("envs")
def envs() -> None:
    """List available inference environments.

    Delegates to: kubectl get inferenceenvironments
    """
    result = subprocess.run(["kubectl", "get", "inferenceenvironments"], capture_output=False)
    sys.exit(result.returncode)
