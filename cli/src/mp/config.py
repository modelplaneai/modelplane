from __future__ import annotations

"""Team configuration management.

Stores team context in ~/.config/modelplane/config.yaml so ML users
don't need to pass --team on every command.
"""

import os
from pathlib import Path

import yaml

CONFIG_DIR = Path(os.environ.get("MP_CONFIG_DIR", Path.home() / ".config" / "modelplane"))
CONFIG_FILE = CONFIG_DIR / "config.yaml"


def load() -> dict:
    """Load config from disk. Returns empty dict if no config exists."""
    if CONFIG_FILE.exists():
        return yaml.safe_load(CONFIG_FILE.read_text()) or {}
    return {}


def save(config: dict) -> None:
    """Write config to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(yaml.dump(config, default_flow_style=False))


def get_team(explicit_team: str | None = None) -> str:
    """Resolve team name. Priority: explicit flag > MP_TEAM env > config file > 'default'."""
    if explicit_team:
        return explicit_team
    env_team = os.environ.get("MP_TEAM")
    if env_team:
        return env_team
    cfg = load()
    return cfg.get("team", "default")
