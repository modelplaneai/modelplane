---
name: nix-flake
description: Develop projects that use a Nix flake. Use when building, testing, linting, running dev commands, or entering a dev shell for a project that has a flake.nix. NOT for running ad-hoc CLI tools - use nix-tools for that.
compatibility: Requires Nix with flakes enabled and a project with flake.nix
---

# Developing with Nix Flakes

## When to Use This (vs nix-tools)

Use **this skill** when the project has a `flake.nix` and you need to:
- Build, test, lint, or run project-specific dev commands
- Enter or reference the project's dev shell
- Run flake checks (CI-style validation)

Use **nix-tools** instead when you need to:
- Run an ad-hoc CLI tool (jq, ripgrep, etc.) that isn't project-specific
- Handle a "command not found" error for a general-purpose tool

## Discovery

Before running anything, read `flake.nix` to understand what's available. Flakes
expose a standard set of output types. Check which ones the project defines:

| Output | Purpose | Command |
|--------|---------|---------|
| `packages` | Build artifacts | `nix build` or `nix build .#<name>` |
| `checks` | CI checks (tests, lints) | `nix flake check` |
| `apps` | Runnable dev commands | `nix run .#<name>` |
| `devShells` | Development environment | `nix develop` |

To list what's available without reading Nix code:

```bash
nix flake show          # Tree of all outputs
```

## Common Workflows

### Building

```bash
nix build               # Build the default package
nix build .#<name>      # Build a specific package
```

`nix build` symlinks build results to `./result`.

### Running Checks

```bash
nix flake check         # Run ALL checks (tests, lints, etc.)
```

You can't run individual checks in isolation via `nix flake check`. If the
project exposes the same functionality as apps (e.g., `nix run .#test`), prefer
that for faster feedback during development. Reserve `nix flake check` for full
CI-style validation.

### Running Dev Commands

```bash
nix run .#<name>        # Run a specific app
nix run .#<name> -- <args>  # Pass arguments to the app
```

Apps are project-specific commands (test, lint, generate, etc.) defined in the
flake. They run outside the Nix sandbox with full filesystem and network access.

### Dev Shell

```bash
nix develop             # Enter the default dev shell
```

The dev shell provides all project tools (compilers, linters, CLI tools) in a
reproducible environment. It often prints available commands on entry.

You don't typically need to enter the dev shell yourself. The `nix run` and
`nix build` commands already use the flake's dependencies. The dev shell is for
interactive use by the developer.

## Passing Arguments

Use `--` to separate Nix flags from program arguments:

```bash
nix run .#test -- -v -run TestFoo
```

## Debugging Failures

If a command fails:

1. Check `nix flake show` to verify the output exists
2. Read the relevant section of `flake.nix` to understand what the command does
3. For build failures, `nix log` shows the build log of the last failed derivation
4. For apps, the source is usually a shell script in a `nix/` directory - read it
   to understand what it runs under the hood

## Key Concepts

- **Flake outputs are hermetic**: builds and checks run in a sandbox with no
  network access and only declared dependencies. Don't expect them to behave
  like running commands directly.
- **Apps are not sandboxed**: unlike builds and checks, apps have full system
  access. They're wrappers around normal development commands.
- **`nix flake check` runs everything**: it evaluates all checks for the current
  system. There's no built-in way to run one check in isolation.
