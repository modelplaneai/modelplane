#!/usr/bin/env python3
"""Generate docs/cli.html from the mp Click command tree.

This is the source of truth for the CLI documentation. It walks the
mp.main:cli Click group, extracts each command's help text, arguments,
and options, and renders an HTML page using a fixed template.

Static content (install instructions, the agents-and-scripting section,
configuration notes) lives in this file alongside the dynamic command
rendering. Per-command content is derived entirely from Click metadata
so it cannot drift from `mp --help`.

Run from the repo root:

    python3 docs/build.py

Outputs docs/cli.html. Idempotent — re-running with no changes to the
Click tree produces byte-identical output.
"""

from __future__ import annotations

import html
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import click

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI_SRC = REPO_ROOT / "cli" / "src"
OUTPUT = REPO_ROOT / "docs" / "cli.html"

# Native commands get full sections with options tables.
# Delegating commands get a compact note block referencing the kubectl command.
NATIVE_COMMANDS = {"init", "deploy", "status", "predict"}
DELEGATING_COMMANDS = {
    "logs": "kubectl logs -l modelplane.ai/deployment=&lt;name&gt; -n &lt;team&gt;",
    "models": "kubectl get clustermodels",
    "envs": "kubectl get inferenceenvironments",
    "deployments": "kubectl get modeldeployments -n &lt;team&gt;",
    "delete": "kubectl delete modeldeployment &lt;name&gt; -n &lt;team&gt;",
}

# Commands appear in this order on the page. Anything not listed is appended.
COMMAND_ORDER = ["init", "deploy", "status", "predict", "logs", "models", "envs", "deployments", "delete"]


@dataclass
class ParamDoc:
    flags: str       # e.g. "-f, --file"
    metavar: str     # e.g. "PATH" or ""
    help: str
    default: str     # rendered default ("1", "70", "from config", "")
    required: bool


@dataclass
class CommandDoc:
    name: str
    short_help: str
    long_help: str
    arguments: list[ParamDoc]
    options: list[ParamDoc]


def _format_default(param: click.Parameter) -> str:
    """Render a Click default for the docs."""
    default = param.default
    if param.required:
        return "(required)"
    if default is None or default is False:
        return ""
    if isinstance(default, bool):
        return "off"
    return str(default)


def _param_to_doc(param: click.Parameter) -> ParamDoc:
    if isinstance(param, click.Argument):
        flags = (param.name or "").upper()
        metavar = ""
    else:
        flags = ", ".join(param.opts + param.secondary_opts)
        metavar = param.make_metavar() if param.type.name not in ("boolean",) else ""
    return ParamDoc(
        flags=flags,
        metavar=metavar,
        help=(param.help or "").strip() if hasattr(param, "help") else "",
        default=_format_default(param),
        required=param.required,
    )


def _command_to_doc(cmd: click.Command) -> CommandDoc:
    arguments = [_param_to_doc(p) for p in cmd.params if isinstance(p, click.Argument)]
    options = [_param_to_doc(p) for p in cmd.params if not isinstance(p, click.Argument) and p.name != "help"]
    long_help = (cmd.help or "").strip()
    short_help = cmd.short_help or long_help.split("\n", 1)[0] if long_help else ""
    return CommandDoc(
        name=cmd.name or "",
        short_help=short_help,
        long_help=long_help,
        arguments=arguments,
        options=options,
    )


# --- HTML rendering ---------------------------------------------------------

PAGE_TEMPLATE = """<!DOCTYPE html>
<!--
  AUTO-GENERATED — do not edit by hand.
  Source: docs/build.py + cli/src/mp Click command tree.
  Regenerate: python3 docs/build.py
-->
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>mp — ModelPlane CLI</title>
  <style>{css}</style>
</head>
<body>

<h1><code>mp</code></h1>
<p class="subtitle">The ModelPlane CLI &mdash; deploy and run AI models without kubectl</p>

<div class="generated">
  <strong>Auto-generated</strong> from the <code>mp</code> Click command tree on every release. If anything here disagrees with <code>mp --help</code>, treat <code>--help</code> as authoritative and file a bug.
</div>

<nav class="toc">
  <strong>On this page</strong>
  <ul>
    <li><a href="#install">Install</a></li>
    <li><a href="#quick-start">Quick start</a></li>
    <li><a href="#commands">Commands</a></li>
{toc_links}
    <li><a href="#agents-and-scripting">Agents and scripting</a></li>
    <li><a href="#configuration">Configuration</a></li>
  </ul>
</nav>

<h2 id="install">Install</h2>
<pre><code>pip install modelplane-cli</code></pre>
<p>Requires Python 3.9+ and <code>kubectl</code> configured with access to a ModelPlane cluster.</p>

<h2 id="quick-start">Quick start</h2>
<pre><code><span class="comment"># Set your team (one-time)</span>
<span class="prompt">$</span> mp init --team ml-team
Team set to: ml-team

<span class="comment"># See what models the platform team has registered</span>
<span class="prompt">$</span> mp models
NAME              READY   MODEL                          VRAM   AGE
qwen-0.5b-vllm    True    Qwen/Qwen2.5-0.5B-Instruct     2Gi    5d
llama-8b-vllm     True    meta-llama/Llama-3-8B          24Gi   5d

<span class="comment"># Deploy one to a specific environment</span>
<span class="prompt">$</span> mp deploy qwen-0.5b-vllm --env prod-gpu-east
Deploying qwen-0.5b-vllm...
Deployment 'qwen-0.5b-vllm' created. Run `mp status qwen-0.5b-vllm` to check progress.

<span class="comment"># Wait for it to come up</span>
<span class="prompt">$</span> mp status qwen-0.5b-vllm --watch
Deployment:  qwen-0.5b-vllm
Status:      Ready
Replicas:    1/1
Endpoint:    http://172.18.255.200/ml-team/qwen-0.5b-vllm/v1

<span class="comment"># Test it</span>
<span class="prompt">$</span> mp predict qwen-0.5b-vllm -i "Explain attention in transformers"
Attention mechanisms allow models to weigh the relevance of different
parts of the input when producing each part of the output...</code></pre>

<p>For autoscaled deployments, add scaling flags:</p>
<pre><code><span class="prompt">$</span> mp deploy llama-8b-vllm --env prod-gpu-east --min 1 --max 6 --target 32</code></pre>

<hr>

<h2 id="commands">Commands</h2>

<p>The CLI has two kinds of commands: <strong>native commands</strong> that do things kubectl can't, and <strong>convenience shortcuts</strong> that delegate to kubectl transparently.</p>

<table>
  <thead><tr><th>Command</th><th>Purpose</th><th></th></tr></thead>
  <tbody>
{commands_table}
  </tbody>
</table>

<hr>

{command_sections}

<hr>

<h2 id="agents-and-scripting">Agents and scripting</h2>

<p>The CLI is usable from agents and shell scripts today. The properties below are what&rsquo;s in v0.1; the design proposal lays out additional affordances that land with their implementations.</p>

<h3>Exit codes</h3>
<table>
  <thead><tr><th>Code</th><th>Meaning</th></tr></thead>
  <tbody>
    <tr><td><code>0</code></td><td>Success</td></tr>
    <tr><td><code>1</code></td><td>Error (any failure today &mdash; cluster errors, missing resources, network failures)</td></tr>
    <tr><td><code>2</code></td><td>Usage error (Click&rsquo;s default for unknown flags or missing required arguments; also returned by <code>mp deploy</code> for inconsistent scaling flag combinations)</td></tr>
  </tbody>
</table>

<h3>Non-interactive deletes</h3>
<p><code>mp delete</code> confirms before deleting. In contexts without a TTY on stdin (CI, agents), pass <code>-y</code> to skip the prompt &mdash; otherwise the command aborts.</p>

<h3>Deterministic startup</h3>
<p>No telemetry. No version checks on startup. No auto-updates. Every invocation is a function of its arguments, environment, and cluster state &mdash; safe to run inside agents and CI without surprise network calls.</p>

<h3>Self-describing</h3>
<p><code>mp --help</code> and <code>mp &lt;command&gt; --help</code> are the canonical command reference. This page is generated from the same Click command tree, so anything <code>--help</code> shows is also documented here.</p>

<h3>Coming soon</h3>
<p>Per the <a href="https://github.com/modelplaneai/modelplane/blob/main/design/cli-design.md">design proposal</a>, the v0.1 target also includes <code>--output json</code> on every native command, <code>mp predict --stream</code>, finer-grained exit codes (3 not-found, 4 backend, 5 timeout), and newline-delimited JSON for <code>mp status --watch --output json</code>. These will appear here automatically once they land in the Click tree.</p>

<hr>

<h2 id="configuration">Configuration</h2>

<h3>Team context</h3>
<p>The CLI stores your team name in <code>~/.config/modelplane/config.yaml</code>. This maps to a Kubernetes namespace. Resolution order:</p>
<ol>
  <li><code>--team</code> flag on any command</li>
  <li><code>MP_TEAM</code> environment variable</li>
  <li>Saved config from <code>mp init --team</code></li>
  <li>Falls back to <code>default</code></li>
</ol>

<h3>Cluster access</h3>
<p>The CLI uses your existing <code>kubeconfig</code> &mdash; the same config kubectl uses. If <code>kubectl get clustermodels</code> works, the CLI will work.</p>

<h3>Environment variables</h3>
<table>
  <thead><tr><th>Variable</th><th>Effect</th></tr></thead>
  <tbody>
    <tr><td><code>MP_TEAM</code></td><td>Override the team for all commands</td></tr>
    <tr><td><code>MP_CONFIG_DIR</code></td><td>Override the config directory (default <code>~/.config/modelplane</code>)</td></tr>
    <tr><td><code>KUBECONFIG</code></td><td>Standard kubectl config path; honored as-is</td></tr>
  </tbody>
</table>

<hr>

<footer>
  ModelPlane CLI v{version} &mdash; <a href="https://github.com/modelplaneai/modelplane">github.com/modelplaneai/modelplane</a> &mdash; <a href="https://github.com/modelplaneai/modelplane/blob/main/design/cli-design.md">design proposal</a>
</footer>

</body>
</html>
"""

CSS = """
:root {
  --bg: #ffffff;
  --fg: #1a1a1a;
  --muted: #6b7280;
  --border: #e5e7eb;
  --code-bg: #f3f4f6;
  --accent: #2563eb;
  --accent-light: #eff6ff;
  --success: #059669;
  --warn-bg: #fef3c7;
  --warn-fg: #92400e;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111827;
    --fg: #f9fafb;
    --muted: #9ca3af;
    --border: #374151;
    --code-bg: #1f2937;
    --accent: #60a5fa;
    --accent-light: #1e293b;
    --success: #34d399;
    --warn-bg: #422006;
    --warn-fg: #fcd34d;
  }
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  color: var(--fg); background: var(--bg); line-height: 1.7;
  max-width: 820px; margin: 0 auto; padding: 3rem 1.5rem;
}
h1 { font-size: 2rem; margin-bottom: 0.25rem; }
h1 code { font-size: 2rem; color: var(--accent); }
.subtitle { color: var(--muted); font-size: 1.1rem; margin-bottom: 2.5rem; }
h2 { font-size: 1.4rem; margin-top: 3rem; margin-bottom: 1rem; padding-bottom: 0.4rem; border-bottom: 1px solid var(--border); }
h3 { font-size: 1.1rem; margin-top: 1.8rem; margin-bottom: 0.6rem; }
p { margin-bottom: 1rem; }
code {
  font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
  font-size: 0.9em; background: var(--code-bg);
  padding: 0.15em 0.4em; border-radius: 4px;
}
pre {
  background: var(--code-bg); border: 1px solid var(--border); border-radius: 8px;
  padding: 1rem 1.25rem; overflow-x: auto; margin-bottom: 1.25rem; line-height: 1.5;
}
pre code { background: none; padding: 0; font-size: 0.85rem; }
.command-block {
  background: var(--accent-light); border: 1px solid var(--accent); border-radius: 8px;
  padding: 1rem 1.25rem; margin-bottom: 1.25rem;
}
.command-block .sig {
  font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
  font-size: 0.95rem; font-weight: 600; color: var(--accent); margin-bottom: 0.4rem;
}
.command-block .desc { color: var(--muted); font-size: 0.9rem; }
table { width: 100%; border-collapse: collapse; margin-bottom: 1.25rem; font-size: 0.9rem; }
th, td { text-align: left; padding: 0.5rem 0.75rem; border-bottom: 1px solid var(--border); vertical-align: top; }
th { font-weight: 600; color: var(--muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; }
.badge {
  display: inline-block; font-size: 0.75rem; font-weight: 600;
  padding: 0.1em 0.5em; border-radius: 4px;
  text-transform: uppercase; letter-spacing: 0.03em;
}
.badge-native { background: #dbeafe; color: #1d4ed8; }
.badge-kubectl { background: #f3f4f6; color: #6b7280; }
@media (prefers-color-scheme: dark) {
  .badge-native { background: #1e3a5f; color: #93c5fd; }
  .badge-kubectl { background: #374151; color: #9ca3af; }
}
.note {
  background: var(--accent-light); border-left: 3px solid var(--accent);
  padding: 0.75rem 1rem; margin-bottom: 1.25rem; border-radius: 0 6px 6px 0; font-size: 0.9rem;
}
.generated {
  background: var(--warn-bg); color: var(--warn-fg); border-left: 3px solid var(--warn-fg);
  padding: 0.75rem 1rem; margin-bottom: 1.5rem; border-radius: 0 6px 6px 0; font-size: 0.9rem;
}
.prompt { color: var(--success); user-select: none; }
.comment { color: var(--muted); }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
hr { border: none; border-top: 1px solid var(--border); margin: 2.5rem 0; }
footer { margin-top: 3rem; color: var(--muted); font-size: 0.85rem; text-align: center; }
nav.toc { background: var(--code-bg); border: 1px solid var(--border); border-radius: 8px; padding: 1rem 1.25rem; margin-bottom: 2rem; font-size: 0.9rem; }
nav.toc ul { list-style: none; column-count: 2; column-gap: 1.5rem; }
nav.toc li { margin-bottom: 0.3rem; break-inside: avoid; }
"""


def _signature(cmd: CommandDoc) -> str:
    """Compose a usage line: mp <name> [args] [options]."""
    parts = ["mp", cmd.name]
    for arg in cmd.arguments:
        token = f"&lt;{arg.flags.lower()}&gt;"
        if not arg.required:
            token = f"[{token}]"
        parts.append(token)
    if cmd.options:
        parts.append("[options]")
    return " ".join(parts)


def _split_help(help_text: str) -> tuple[str, list[str]]:
    """Split a Click docstring into (description, [code_blocks]).

    Click uses '\\b' as a no-rewrap marker for code-like blocks (examples,
    indented snippets). Paragraphs not preceded by '\\b' are prose; those
    preceded by '\\b' are rendered as preformatted code.
    """
    import textwrap

    # Normalize: split on blank lines, preserving '\b' markers at the start of blocks.
    raw_paragraphs = help_text.split("\n\n")
    description_parts: list[str] = []
    code_blocks: list[str] = []

    for raw in raw_paragraphs:
        stripped = raw.strip("\n")
        if not stripped.strip():
            continue
        if stripped.lstrip().startswith("\b"):
            # Code block — strip the \b marker, dedent.
            body = stripped.lstrip().removeprefix("\b").lstrip("\n")
            code_blocks.append(textwrap.dedent(body).rstrip())
        else:
            description_parts.append(textwrap.dedent(stripped).strip())

    description = "\n\n".join(description_parts)
    return description, code_blocks


def _render_native_section(cmd: CommandDoc) -> str:
    """Render a full section for a native command."""
    out = [f'<h2 id="mp-{cmd.name}"><code>mp {cmd.name}</code></h2>']

    if cmd.long_help:
        description, code_blocks = _split_help(cmd.long_help)
        if description:
            for para in description.split("\n\n"):
                out.append(f'<p>{html.escape(para)}</p>')
        for block in code_blocks:
            out.append(f'<pre><code>{html.escape(block)}</code></pre>')

    out.append('<div class="command-block">')
    out.append(f'  <div class="sig">{_signature(cmd)}</div>')
    out.append('</div>')

    if cmd.options:
        out.append("<h3>Options</h3>")
        out.append('<table>')
        out.append('  <thead><tr><th>Flag</th><th>Description</th><th>Default</th></tr></thead>')
        out.append('  <tbody>')
        for opt in cmd.options:
            flag_html = f'<code>{html.escape(opt.flags)}</code>'
            if opt.metavar and opt.metavar not in ("BOOLEAN", "[no-]"):
                flag_html += f' <span class="comment">{html.escape(opt.metavar)}</span>'
            help_html = html.escape(opt.help) if opt.help else ""
            default_html = f'<code>{html.escape(opt.default)}</code>' if opt.default else ""
            out.append(f'    <tr><td>{flag_html}</td><td>{help_html}</td><td>{default_html}</td></tr>')
        out.append('  </tbody>')
        out.append('</table>')

    return "\n".join(out)


def _render_delegating_section(cmd: CommandDoc, kubectl_cmd: str) -> str:
    """Render a compact section for a delegating command."""
    out = [f'<h2 id="mp-{cmd.name}"><code>mp {cmd.name}</code></h2>']
    out.append(f'<div class="note">Delegates to <code>{kubectl_cmd}</code></div>')
    if cmd.long_help:
        first_line = cmd.long_help.split("\n", 1)[0].strip()
        out.append(f'<p>{html.escape(first_line)}</p>')
    if cmd.options:
        out.append('<table>')
        out.append('  <thead><tr><th>Flag</th><th>Description</th></tr></thead>')
        out.append('  <tbody>')
        for opt in cmd.options:
            flag_html = f'<code>{html.escape(opt.flags)}</code>'
            help_html = html.escape(opt.help) if opt.help else ""
            out.append(f'    <tr><td>{flag_html}</td><td>{help_html}</td></tr>')
        out.append('  </tbody>')
        out.append('</table>')
    return "\n".join(out)


def _commands_table_row(name: str, short_help: str, native: bool) -> str:
    badge = '<span class="badge badge-native">Native</span>' if native else '<span class="badge badge-kubectl">kubectl</span>'
    return f'    <tr><td><code>mp {name}</code></td><td>{html.escape(short_help)}</td><td>{badge}</td></tr>'


def _ordered(commands: dict[str, CommandDoc]) -> Iterable[CommandDoc]:
    seen: set[str] = set()
    for name in COMMAND_ORDER:
        if name in commands:
            seen.add(name)
            yield commands[name]
    for name, cmd in commands.items():
        if name not in seen:
            yield cmd


def render(cli: click.Group, version: str) -> str:
    commands = {name: _command_to_doc(cli.commands[name]) for name in cli.commands}
    ordered = list(_ordered(commands))

    toc_links = "\n".join(
        f'    <li><a href="#mp-{cmd.name}"><code>mp {cmd.name}</code></a></li>'
        for cmd in ordered
    )

    table_rows = []
    for cmd in ordered:
        is_native = cmd.name in NATIVE_COMMANDS
        table_rows.append(_commands_table_row(cmd.name, cmd.short_help, is_native))
    commands_table = "\n".join(table_rows)

    sections = []
    for cmd in ordered:
        if cmd.name in NATIVE_COMMANDS:
            sections.append(_render_native_section(cmd))
        elif cmd.name in DELEGATING_COMMANDS:
            sections.append(_render_delegating_section(cmd, DELEGATING_COMMANDS[cmd.name]))
        else:
            # Unknown — render as native by default
            sections.append(_render_native_section(cmd))
    command_sections = "\n\n<hr>\n\n".join(sections)

    return PAGE_TEMPLATE.format(
        css=CSS,
        toc_links=toc_links,
        commands_table=commands_table,
        command_sections=command_sections,
        version=version,
    )


def main() -> int:
    sys.path.insert(0, str(CLI_SRC))
    import mp  # noqa: E402
    from mp.main import cli  # noqa: E402

    html_out = render(cli, version=mp.__version__)
    OUTPUT.write_text(html_out)
    print(f"Wrote {OUTPUT.relative_to(REPO_ROOT)} ({len(html_out):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
