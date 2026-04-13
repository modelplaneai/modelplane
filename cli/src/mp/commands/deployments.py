from __future__ import annotations

"""mp deployments — delegates to kubectl get modeldeployments."""

import subprocess
import sys

import click

from mp import config


@click.command("deployments")
@click.option("--team", default=None, help="Override team.")
def deployments(team: str | None) -> None:
    """List all model deployments for your team.

    Delegates to: kubectl get modeldeployments -n <team>
    """
    team_ns = config.get_team(team)
    result = subprocess.run(["kubectl", "get", "modeldeployments", "-n", team_ns], capture_output=False)
    sys.exit(result.returncode)
