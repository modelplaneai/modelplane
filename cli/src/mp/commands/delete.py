from __future__ import annotations

"""mp delete — confirmation prompt, then delegates to kubectl delete."""

import subprocess
import sys

import click

from mp import config


@click.command("delete")
@click.argument("name")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation.")
@click.option("--team", default=None, help="Override team.")
def delete(name: str, yes: bool, team: str | None) -> None:
    """Delete a model deployment.

    Delegates to: kubectl delete modeldeployment <name> -n <team>
    """
    team_ns = config.get_team(team)

    if not yes:
        click.confirm(f"Delete deployment '{name}' in team '{team_ns}'?", abort=True)

    result = subprocess.run(
        ["kubectl", "delete", "modeldeployment", name, "-n", team_ns],
        capture_output=False,
    )
    sys.exit(result.returncode)
