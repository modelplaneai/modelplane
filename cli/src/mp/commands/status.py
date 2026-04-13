from __future__ import annotations

"""mp status — rich deployment status with --watch and --all-envs."""

import subprocess
import sys
import time

import click

from mp import config, kube, output, resources as res

LABEL_KEY_DEPLOYMENT = "modelplane.ai/deployment"


def _print_status(dep: dict, placements: list[dict] | None = None) -> None:
    """Print deployment status as key-value pairs."""
    name = dep["metadata"]["name"]
    status = dep.get("status", {})
    spec = dep.get("spec", {})

    model_name = status.get("model", {}).get("name", spec.get("modelRef", {}).get("name", ""))
    ready_status = output.condition_ready(dep)
    placement_status = status.get("placements", {})
    total = placement_status.get("total", spec.get("environments", 0))
    ready_count = placement_status.get("ready", 0)
    endpoint = status.get("endpoint", {}).get("url", "")

    pairs = [
        ("Deployment", name),
        ("Model", model_name),
        ("Status", ready_status),
        ("Replicas", f"{ready_count}/{total}"),
    ]
    if endpoint:
        pairs.append(("Endpoint", endpoint))

    output.kv(pairs)

    # Per-placement breakdown
    if placements:
        click.echo()
        headers = ["ENV", "STATUS", "ENDPOINT"]
        rows = []
        for p in placements:
            p_spec = p.get("spec", {})
            p_status = p.get("status", {})
            env_name = p_spec.get("inferenceEnvironmentRef", {}).get("name", "")
            p_ready = output.condition_ready(p)
            p_endpoint = p_status.get("endpoint", {}).get("url", "")
            rows.append([env_name, p_ready, p_endpoint])
        output.table(headers, rows)


@click.command("status")
@click.argument("name", required=False)
@click.option("--watch", "-w", is_flag=True, help="Poll until ready.")
@click.option("--all-envs", is_flag=True, help="Show per-environment placement details.")
@click.option("--team", default=None, help="Override team.")
def status(name: str | None, watch: bool, all_envs: bool, team: str | None) -> None:
    """Check deployment status.

    \b
    Without a name, delegates to: kubectl get modeldeployments -n <team>
    With a name, shows rich status with --watch and --all-envs support.
    """
    team_ns = config.get_team(team)

    # No name → delegate to kubectl
    if not name:
        result = subprocess.run(["kubectl", "get", "modeldeployments", "-n", team_ns], capture_output=False)
        sys.exit(result.returncode)

    dep = kube.get_namespaced_resource(res.MODEL_DEPLOYMENTS, name, team_ns)
    if not dep:
        click.echo(f"Error: Deployment '{name}' not found in team '{team_ns}'.", err=True)
        sys.exit(1)

    placements = None
    if all_envs:
        all_mp = kube.list_namespaced_resources(res.MODEL_PLACEMENTS, team_ns)
        placements = [
            p for p in all_mp if (p.get("metadata", {}).get("labels", {}).get(LABEL_KEY_DEPLOYMENT) == name)
        ]

    _print_status(dep, placements)

    if watch:
        while output.condition_ready(dep) != "Ready":
            time.sleep(5)
            dep = kube.get_namespaced_resource(res.MODEL_DEPLOYMENTS, name, team_ns)
            if not dep:
                click.echo("Deployment disappeared.", err=True)
                return
            click.clear()
            if all_envs:
                all_mp = kube.list_namespaced_resources(res.MODEL_PLACEMENTS, team_ns)
                placements = [
                    p
                    for p in all_mp
                    if (p.get("metadata", {}).get("labels", {}).get(LABEL_KEY_DEPLOYMENT) == name)
                ]
            _print_status(dep, placements)

        click.echo("\nDeployment is ready!")
