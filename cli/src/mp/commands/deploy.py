from __future__ import annotations

"""mp deploy — deploy a model from catalog or YAML file."""

import sys

import click
import yaml

from mp import config, kube, resources as res


@click.command("deploy")
@click.argument("model", required=False)
@click.option("-f", "--file", "filepath", default=None, type=click.Path(exists=True), help="Path to a Model YAML file.")
@click.option("--env", default=None, help="Target a specific InferenceEnvironment by name.")
@click.option("--envs", "envs_count", default=1, show_default=True, help="Number of environments to fan out across.")
@click.option("--name", "deployment_name", default=None, help="Deployment name (defaults to model name).")
@click.option("--team", default=None, help="Override team.")
# Scaling — pick one mode (default: --replicas 1, signal: Fixed)
@click.option("--replicas", default=None, type=int, help="Fixed pod count per placement (signal: Fixed). Default 1.")
@click.option("--min", "min_replicas", default=None, type=int, help="Min replicas for autoscaling (signal: Concurrency).")
@click.option("--max", "max_replicas", default=None, type=int, help="Max replicas for autoscaling (signal: Concurrency).")
@click.option("--target", default=None, type=int, help="Target in-flight requests per replica (signal: Concurrency).")
@click.option("--scale-to-zero", is_flag=True, help="Shorthand for --min 0 (still requires --max and --target).")
@click.option("--utilization", default=None, type=int, help="Scale at this percent of --target. Default 70.")
@click.option("--scale-down-delay", default=None, type=int, help="Seconds before removing replicas. Default 300.")
def deploy(
    model: str | None,
    filepath: str | None,
    env: str | None,
    envs_count: int,
    deployment_name: str | None,
    team: str | None,
    replicas: int | None,
    min_replicas: int | None,
    max_replicas: int | None,
    target: int | None,
    scale_to_zero: bool,
    utilization: int | None,
    scale_down_delay: int | None,
) -> None:
    """Deploy a model from the catalog or from a YAML file.

    \b
    From catalog:    mp deploy llama3-8b [--env prod-gpu-east]
    From YAML file:  mp deploy -f model.yaml [--env prod-gpu-east]

    \b
    Autoscale:       mp deploy llama3-8b --min 1 --max 6 --target 32
    Scale to zero:   mp deploy llama3-8b --scale-to-zero --max 4 --target 16
    """
    if filepath and model:
        click.echo("Error: Provide either a MODEL name or -f/--file, not both.", err=True)
        sys.exit(1)
    if not filepath and not model:
        click.echo("Error: Provide a MODEL name or -f/--file.", err=True)
        click.echo("  mp deploy <MODEL>        # from catalog", err=True)
        click.echo("  mp deploy -f model.yaml  # from YAML file", err=True)
        sys.exit(1)

    team_ns = config.get_team(team)

    # Build the scaling block from flags (or None for the CRD default of Fixed/1).
    scaling = _build_scaling(
        replicas=replicas,
        min_replicas=min_replicas,
        max_replicas=max_replicas,
        target=target,
        scale_to_zero=scale_to_zero,
        utilization=utilization,
        scale_down_delay=scale_down_delay,
    )

    # Resolve environment selector if --env is specified
    env_selector = None
    if env:
        env_obj = kube.get_cluster_resource(res.INFERENCE_ENVIRONMENTS, env)
        if not env_obj:
            click.echo(f"Error: Environment '{env}' not found.", err=True)
            click.echo("Run `mp envs` to see available environments.", err=True)
            sys.exit(1)
        env_labels = env_obj.get("metadata", {}).get("labels", {})
        if env_labels:
            env_selector = {"matchLabels": env_labels}
        envs_count = 1  # targeting a specific env means 1 placement

    if filepath:
        _deploy_from_file(filepath, team_ns, deployment_name, envs_count, env_selector, scaling)
    else:
        _deploy_from_catalog(model, team_ns, deployment_name, envs_count, env_selector, scaling)


def _build_scaling(
    *,
    replicas: int | None,
    min_replicas: int | None,
    max_replicas: int | None,
    target: int | None,
    scale_to_zero: bool,
    utilization: int | None,
    scale_down_delay: int | None,
) -> dict | None:
    """Translate scaling flags into a ModelDeployment.spec.scaling block.

    Returns None if no scaling flags were given (the CRD defaults to
    {signal: Fixed, fixed: {replicas: 1}}). Raises a usage error if the
    flag combination is inconsistent.
    """
    autoscale_flags_set = any(
        v is not None for v in (min_replicas, max_replicas, target, utilization, scale_down_delay)
    ) or scale_to_zero

    # Fixed mode
    if replicas is not None:
        if autoscale_flags_set:
            click.echo("Error: --replicas is for fixed scaling; cannot combine with autoscaling flags.", err=True)
            sys.exit(2)
        if replicas < 1:
            click.echo("Error: --replicas must be >= 1.", err=True)
            sys.exit(2)
        return {"signal": "Fixed", "fixed": {"replicas": replicas}}

    # No scaling flags → let the CRD apply its default
    if not autoscale_flags_set:
        return None

    # Concurrency mode — requires --max and --target
    effective_min = 0 if scale_to_zero else (min_replicas if min_replicas is not None else 1)
    if max_replicas is None or target is None:
        click.echo("Error: --min/--max/--target are required together for autoscaling.", err=True)
        click.echo("       --scale-to-zero also requires --max and --target.", err=True)
        sys.exit(2)
    if max_replicas < max(effective_min, 1):
        click.echo("Error: --max must be >= --min (and >= 1).", err=True)
        sys.exit(2)

    concurrency: dict = {"minReplicas": effective_min, "maxReplicas": max_replicas, "target": target}
    if utilization is not None:
        concurrency["utilization"] = utilization
    if scale_down_delay is not None:
        concurrency["scaleDownDelay"] = scale_down_delay
    return {"signal": "Concurrency", "concurrency": concurrency}


def _deploy_from_catalog(
    model: str,
    team_ns: str,
    deployment_name: str | None,
    envs_count: int,
    env_selector: dict | None,
    scaling: dict | None,
) -> None:
    """Deploy from a pre-registered ClusterModel."""
    name = deployment_name or model

    model_obj = kube.get_cluster_resource(res.CLUSTER_MODELS, model)
    if not model_obj:
        click.echo(f"Error: Model '{model}' not found in the catalog.", err=True)
        click.echo("Run `mp models` to see available models.", err=True)
        sys.exit(1)

    _create_deployment(name, team_ns, "ClusterModel", model, envs_count, env_selector, scaling)


def _deploy_from_file(
    filepath: str,
    team_ns: str,
    deployment_name: str | None,
    envs_count: int,
    env_selector: dict | None,
    scaling: dict | None,
) -> None:
    """Deploy from a Model CRD YAML file — creates Model + ModelDeployment."""
    try:
        with open(filepath) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        click.echo(f"Error: Invalid YAML in {filepath}: {e}", err=True)
        sys.exit(1)

    if not isinstance(data, dict):
        click.echo(f"Error: {filepath} must be a YAML document.", err=True)
        sys.exit(1)

    # Validate it looks like a Model CRD
    kind = data.get("kind", "")
    if kind != "Model":
        click.echo(f"Error: Expected kind: Model, got: {kind}", err=True)
        sys.exit(1)

    model_name = data.get("metadata", {}).get("name", "")
    if not model_name:
        click.echo("Error: metadata.name is required.", err=True)
        sys.exit(1)

    name = deployment_name or model_name

    kube.ensure_namespace(team_ns)

    # Set the namespace to the team namespace
    data.setdefault("metadata", {})["namespace"] = team_ns

    # Create or replace the namespace-scoped Model
    existing_model = kube.get_namespaced_resource(res.MODELS, model_name, team_ns)
    if existing_model:
        click.echo(f"Model '{model_name}' already exists, replacing...")
        kube.delete_namespaced_resource(res.MODELS, model_name, team_ns)

    kube.create_namespaced_resource(res.MODELS, team_ns, data)

    _create_deployment(name, team_ns, "Model", model_name, envs_count, env_selector, scaling)


def _create_deployment(
    name: str,
    team_ns: str,
    model_kind: str,
    model_name: str,
    envs_count: int,
    env_selector: dict | None,
    scaling: dict | None,
) -> None:
    """Create a ModelDeployment resource."""
    existing = kube.get_namespaced_resource(res.MODEL_DEPLOYMENTS, name, team_ns)
    if existing:
        click.echo(f"Error: Deployment '{name}' already exists in team '{team_ns}'.", err=True)
        click.echo("Use a different --name or run `mp delete` first.", err=True)
        sys.exit(1)

    spec: dict = {
        "modelRef": {
            "kind": model_kind,
            "name": model_name,
        },
        "environments": envs_count,
    }
    if env_selector:
        spec["environmentSelector"] = env_selector
    if scaling:
        spec["scaling"] = scaling

    body = {
        "apiVersion": f"{res.GROUP}/{res.VERSION}",
        "kind": "ModelDeployment",
        "metadata": {
            "name": name,
            "namespace": team_ns,
        },
        "spec": spec,
    }

    kube.create_namespaced_resource(res.MODEL_DEPLOYMENTS, team_ns, body)

    click.echo(f"Deploying {model_name}...")
    click.echo(f"Deployment '{name}' created. Run `mp status {name}` to check progress.")
