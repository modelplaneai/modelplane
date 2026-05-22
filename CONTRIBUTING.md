# Contributing to Modelplane

## Development setup

Modelplane uses [Nix](https://nixos.org) for builds, checks, and the
development environment. If you have Nix installed, `nix develop` (or `direnv
allow` if you use [direnv](https://direnv.net/)) drops you into a shell with
everything you need: `crossplane`, `kubectl`, `helm`, `kind`, Python, linters,
and formatters.

If you don't have Nix installed, [`nix.sh`](nix.sh) runs any Nix command inside
a Docker container. The first run downloads dependencies into a Docker volume.
Subsequent runs reuse the cache.

```bash
# With Nix installed:
nix develop

# Without Nix installed:
./nix.sh develop
```

## Running checks

`nix flake check` runs all of the project's checks — Python, shell, and Nix
linters and formatters, plus unit tests for every composition function —
inside the Nix sandbox. Run `nix flake show` to see what else is available.

```bash
nix flake check            # or: ./nix.sh flake check
```

`nix run .#fix` auto-fixes most lint and formatting issues. Run it before
opening a PR.

## Working on composition functions

Modelplane is a [Crossplane](https://crossplane.io/) project. The core logic
lives in Python composition functions under `functions/`. See
[`skills/crossplane-python-functions/SKILL.md`](skills/crossplane-python-functions/SKILL.md)
for a detailed guide.

Each function is a self-contained Python package, built as a hatch project and
managed in the workspace `uv.lock`:

```
functions/<name>/
  pyproject.toml      # Hatch package metadata; declares SDK and models deps
  function/
    __init__.py
    __version__.py
    main.py           # CLI entrypoint (boilerplate)
    fn.py             # FunctionRunner gRPC service and Composer logic
  tests/
    test_fn.py        # unittest-based tests for fn.py
```

The `Composer.compose()` method in `fn.py` reads the XR from the request,
composes resources into the response, and tracks readiness. `FunctionRunner`
is the gRPC service that wires `Composer` to the SDK's runtime. Functions use
generated Pydantic models (in `schemas/python/`) for type-safe access to XR
specs and status.

Each function is self-contained — there is no shared library. Common patterns
like setting conditions, updating status, and building child resource names
are provided by the
[Crossplane Python Function SDK](https://github.com/crossplane/function-sdk-python).
Helpers specific to a single function live in that function's `function/`
package alongside `fn.py`.

The Pydantic models in `schemas/python/` are generated from the XRDs under
`apis/` and the project's dependency CRDs. They're committed to git so tests
and type checking don't need to run the Crossplane CLI first. Regenerate them
after changing an XRD or bumping a dependency:

```bash
nix run .#generate
```

### Tests

Every function has tests under `functions/<name>/tests/test_fn.py`. Tests are
`unittest.IsolatedAsyncioTestCase` cases that build a typed `RunFunctionRequest`
from generated Pydantic models, call `FunctionRunner.RunFunction()`, and
compare the resulting `RunFunctionResponse` against an expected response via
`json_format.MessageToDict`.

Add new cases to the function's existing `test_fn.py`. Run `nix flake check`
to verify they pass.

## Submitting changes

Sign off your commits using `git commit -s`. This adds a `Signed-off-by` line
certifying you have the right to submit the code under the project's license
(the [Developer Certificate of Origin](https://developercertificate.org/)).

Before opening a PR, run `nix flake check` and make sure it passes. If you
changed a composition function, make sure there's a test covering the change.
