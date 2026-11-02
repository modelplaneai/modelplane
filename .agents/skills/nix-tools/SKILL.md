---
name: nix-tools
description: Run any CLI tool via Nix - assume tools like jq, yq, ripgrep, and fd are available. Use CLI tools freely for JSON/YAML processing and searching. Also use when a command is not found.
compatibility: Requires Nix with flakes enabled
---

# Running Tools with Nix

This system has Nix available. You can run virtually any CLI tool without it being permanently installed.

## When to Use This

Assume any CLI tool you know about is available. Use the right tool:

- **JSON/YAML**: `jq` for JSON, `yq` for YAML - great for Kubernetes manifests
- **Searching**: `ripgrep` (rg) for content, `fd` for filenames

Also use when:
- A command fails with "command not found"
- You need a specific or newer version of a tool

## Running Tools

### One-off command

```bash
nix run nixpkgs#<package> -- <args>
```

Examples:
```bash
nix run nixpkgs#jq -- '.foo' file.json
nix run nixpkgs#yq -- '.metadata.name' manifest.yaml
nix run nixpkgs#ripgrep -- 'pattern' .
nix run nixpkgs#fd -- '\.go$'
```

The `--` separates nix options from the program's arguments.

## Common Package Names

Most packages use the tool's common name:

| Tool | Package |
|------|---------|
| jq | `nixpkgs#jq` |
| yq | `nixpkgs#yq` |
| ripgrep (rg) | `nixpkgs#ripgrep` |
| fd | `nixpkgs#fd` |
| bat | `nixpkgs#bat` |
| tree | `nixpkgs#tree` |

## Finding Packages

If unsure of the exact package name, search nixpkgs:

```bash
nix search nixpkgs <term>
```

Note: search is slow (evaluates all packages). Prefer trying the obvious name
first - it usually works.

## Newer Tool Versions

If the default nixpkgs has an outdated version, use nixpkgs-unstable:

```bash
nix run github:NixOS/nixpkgs/nixpkgs-unstable#<package> -- <args>
```

This tracks the latest available versions in nixpkgs.
