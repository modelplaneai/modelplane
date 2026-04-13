from __future__ import annotations

"""mp logs — tail logs from a deployment's pods (delegates to kubectl)."""

import subprocess
import sys

import click

from mp import config

LABEL_KEY_DEPLOYMENT = "modelplane.ai/deployment"


@click.command("logs")
@click.argument("name")
@click.option("-f", "--follow", is_flag=True, help="Stream new log lines as they arrive.")
@click.option("--since", default=None, help="Only logs newer than e.g. 5m, 1h, 2h30m.")
@click.option("--tail", default=None, type=int, help="Last N lines per pod.")
@click.option("--team", default=None, help="Override team.")
def logs(name: str, follow: bool, since: str | None, tail: int | None, team: str | None) -> None:
    """Tail logs from all pods serving a deployment.

    Delegates to: kubectl logs -l modelplane.ai/deployment=<name> -n <team>
    """
    team_ns = config.get_team(team)
    cmd = ["kubectl", "logs", "-l", f"{LABEL_KEY_DEPLOYMENT}={name}", "-n", team_ns]
    if follow:
        cmd.append("-f")
    if since:
        cmd.extend(["--since", since])
    if tail is not None:
        cmd.extend(["--tail", str(tail)])
    result = subprocess.run(cmd, capture_output=False)
    sys.exit(result.returncode)
