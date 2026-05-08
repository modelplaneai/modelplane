"""Pytest configuration for unit tests of pure modules.

Wires up imports for `scheduling.py`, `scheduler.py`, `rendering.py`,
and `lib/capacity_adapter/*` so test files can import them directly.

The composition-function directories (`functions/compose-model-deployment/`,
`functions/compose-model-placement/`) have hyphens in their names so
they're not importable as Python packages. We add each to sys.path so
the *modules inside* are importable by their plain names: `scheduling`,
`scheduler`, `rendering`. (`lib/` already works as a package.)
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Make the pure modules importable by name.
for sub in (
    "functions/compose-model-deployment",
    "functions/compose-model-placement",
):
    sys.path.insert(0, str(REPO_ROOT / sub))

# Top-level lib/ already works as a package; add repo root for `from lib...`.
sys.path.insert(0, str(REPO_ROOT))
