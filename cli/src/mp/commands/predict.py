from __future__ import annotations

"""mp predict — send input to a deployed model (smart routing)."""

import json
import sys

import click
import httpx

from mp import config, kube, output, resources as res


def _build_request(input_data: str, model_name: str) -> tuple[str, dict]:
    """Build the request body and endpoint path suffix.

    Returns (path_suffix, body) where path_suffix is appended to the
    deployment's base endpoint URL.
    """
    # Try parsing as JSON first
    try:
        parsed = json.loads(input_data)
        if isinstance(parsed, dict):
            # JSON with "messages" → chat completions, forward as-is
            if "messages" in parsed:
                parsed.setdefault("model", model_name)
                return "/chat/completions", parsed

            # JSON with "input" → responses API, forward as-is
            if "input" in parsed:
                parsed.setdefault("model", model_name)
                return "/responses", parsed

    except (json.JSONDecodeError, TypeError):
        pass

    # Plain text or unrecognized JSON → wrap as chat completion
    body = {
        "model": model_name,
        "messages": [{"role": "user", "content": input_data}],
    }
    return "/chat/completions", body


def _extract_response_text(data: dict) -> str:
    """Extract the response text from an OpenAI-compatible response."""
    # Chat completions format
    choices = data.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        return message.get("content", "")

    # Responses API format
    output_list = data.get("output", [])
    if output_list:
        for item in output_list:
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        return content.get("text", "")

    return json.dumps(data, indent=2)


def _read_input(input_str: str) -> str:
    """Read input from string or @file reference."""
    if input_str.startswith("@"):
        filepath = input_str[1:]
        try:
            with open(filepath) as f:
                return f.read().strip()
        except FileNotFoundError:
            click.echo(f"Error: File not found: {filepath}", err=True)
            sys.exit(1)
    return input_str


@click.command("predict")
@click.argument("name")
@click.option("-i", "--input", "input_data", required=True, help='Prompt text, JSON string, or @file.json.')
@click.option("--raw", is_flag=True, help="Print full JSON response.")
@click.option("--team", default=None, help="Override team.")
def predict(name: str, input_data: str, raw: bool, team: str | None) -> None:
    """Send input to a deployed model and get a response.

    NAME is the deployment name (see `mp deployments`).
    """
    team_ns = config.get_team(team)

    dep = kube.get_namespaced_resource(res.MODEL_DEPLOYMENTS, name, team_ns)
    if not dep:
        click.echo(f"Error: Deployment '{name}' not found in team '{team_ns}'.", err=True)
        sys.exit(1)

    # Check if deployment is ready
    if output.condition_ready(dep) != "Ready":
        click.echo(f"Error: Deployment '{name}' is not ready yet.", err=True)
        click.echo(f"Run `mp status {name}` to check progress.", err=True)
        sys.exit(1)

    endpoint = dep.get("status", {}).get("endpoint", {}).get("url", "")
    if not endpoint:
        click.echo("Error: No endpoint available for this deployment.", err=True)
        sys.exit(1)

    model_name = dep.get("status", {}).get("model", {}).get("name", "")
    input_text = _read_input(input_data)
    path_suffix, body = _build_request(input_text, model_name)

    # The endpoint URL may already include a path — append the API route
    url = endpoint.rstrip("/") + path_suffix

    try:
        resp = httpx.post(url, json=body, timeout=120.0)
        resp.raise_for_status()
    except httpx.ConnectError:
        click.echo(f"Error: Could not connect to endpoint: {url}", err=True)
        click.echo("Make sure the deployment is accessible from your network.", err=True)
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        click.echo(f"Error: {e.response.status_code} from model endpoint.", err=True)
        click.echo(e.response.text, err=True)
        sys.exit(1)

    data = resp.json()

    if raw:
        click.echo(json.dumps(data, indent=2))
    else:
        text = _extract_response_text(data)
        click.echo(text)
