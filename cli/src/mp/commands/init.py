from __future__ import annotations

"""mp init — scaffold a Model YAML or set team context."""

import sys
from pathlib import Path

import click

from mp import config, kube

MODEL_TEMPLATE = """\
# Modelplane Model
# Deploy with: mp deploy -f {path}
apiVersion: modelplane.ai/v1alpha1
kind: Model
metadata:
  name: {name}
spec:
  model:
    name: organization/model-name         # Model name as the engine knows it

  source: HuggingFace
  huggingFace:
    repo: organization/model-name         # HuggingFace repo ID
    # revision: main                      # Git revision (branch, tag, or commit)
    # secretRef:                          # For gated models
    #   name: hf-token
    #   namespace: ml-team
    #   key: token

  resources:
    vram: 24Gi                            # Total GPU memory needed
    # cpu: "4"                            # CPU per pod (default: 4)
    # memory: 16Gi                        # Memory per pod (default: 16Gi)

  serving:                                # Serving profiles (one per backend)
  - name: vllm-kserve
    backend: KServe                       # KServe or Dynamo
    engine:
      name: vLLM                          # vLLM or SGLang
      image: vllm/vllm-openai:latest
      # args:                             # Opaque args passed to the engine
      #   - --max-model-len=32768
      #   - --quantization=fp8
"""


@click.command("init")
@click.argument("name", required=False)
@click.option("--team", default=None, help="Set team context instead of scaffolding a model.")
def init(name: str | None, team: str | None) -> None:
    """Scaffold a new model YAML, or set your team context.

    \b
    Scaffold a model:    mp init my-model
    Set team context:    mp init --team ml-team
    """
    if team:
        _set_team(team)
    elif name:
        _scaffold_model(name)
    else:
        click.echo("Usage: mp init NAME (scaffold a model) or mp init --team NAME (set team)")
        raise SystemExit(1)


def _set_team(team: str) -> None:
    """Set team context."""
    try:
        kube.get_current_namespace()
    except Exception:
        click.echo(
            "Warning: Could not connect to cluster. Commands will fail until cluster access is configured.", err=True
        )

    cfg = config.load()
    cfg["team"] = team
    config.save(cfg)

    click.echo(f"Team set to: {team}")
    click.echo(f"Config saved to: {config.CONFIG_FILE}")


def _scaffold_model(name: str) -> None:
    """Scaffold a Model CRD YAML."""
    directory = Path.cwd() / name
    filepath = directory / "model.yaml"

    if filepath.exists():
        click.echo(f"Error: {filepath} already exists.", err=True)
        sys.exit(1)

    directory.mkdir(parents=True, exist_ok=True)
    filepath.write_text(MODEL_TEMPLATE.format(name=name, path=f"{name}/model.yaml"))

    click.echo(f"Created {filepath}")
    click.echo(f"Edit it, then run: mp deploy -f {name}/model.yaml")
