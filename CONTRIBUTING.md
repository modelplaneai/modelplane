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

To install Nix itself, the [Determinate Systems
installer](https://github.com/DeterminateSystems/nix-installer) is the easiest
option. It enables flakes by default and supports a clean uninstall:

```bash
curl -fsSL https://install.determinate.systems/nix | sh -s -- install
```

## Running checks

`nix flake check` runs all of the project's checks inside the Nix sandbox:
Python, shell, and Nix linters and formatters, plus unit tests for every
composition function. Run `nix flake show` to see what else is available.

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
composes resources into the response, and tracks readiness. `FunctionRunner` is
the gRPC service that wires `Composer` to the SDK's runtime. Functions use
generated Pydantic models (in `schemas/python/`) for type-safe access to XR
specs and status.

Each function is self-contained; there is no shared library. Common patterns
like setting conditions, updating status, and building child resource names are
provided by the [Crossplane Python Function
SDK](https://github.com/crossplane/function-sdk-python). Helpers specific to a
single function live in that function's `function/` package alongside `fn.py`.

The Pydantic models in `schemas/python/` are generated from the XRDs under
`apis/` and the project's dependency CRDs. They're committed to git so tests and
type checking don't need to run the Crossplane CLI first. Regenerate them after
changing an XRD or bumping a dependency:

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

## Working on the docs site

The documentation site under `docs/` is a [Hugo](https://gohugo.io/) project.
`nix flake check` builds it as one of its checks, so a broken site fails CI.

Run the commands below from the repository root, not from `docs/`. They're flake
apps (`nix run .#...`), so they resolve against the flake at the root regardless
of which file you're editing.

Preview it locally with live reload:

```bash
nix run .#docs-serve            # http://localhost:1313
```

`nix build .#docs` produces the production site in `result/`. The production
build compiles the theme's SCSS and runs it through PostCSS to strip unused
CSS, sort media queries, and minify. Those Node dependencies are pinned in
`docs/package-lock.json` and built reproducibly; the local preview skips them.

The site's JavaScript bundle is built by webpack and committed to git under the
theme's assets. Rebuild it after changing anything under
`docs/utils/webpack/src/` and commit the result:

```bash
nix run .#docs-generate
```

### Linting and link checking

Docs prose is linted with [Vale](https://vale.sh). Lint docs content:

```bash
nix run .#docs-vale
```

Vale downloads its style packages (`Google`, `write-good`) on first run into
`docs/utils/vale/styles/`. Those directories are gitignored. Custom Modelplane rules
live in `docs/utils/vale/styles/Modelplane/` and are committed.

**Handling suggestions.** CI only fails on warnings and errors. Suggestions are
informational. Two common ones need judgment:

- **Passive voice.** If passive voice is genuinely the clearest way to express
  something (the subject is unknown, irrelevant, or awkward to name), keep it
  and suppress the suggestion with a Vale ignore comment:

  ```markdown
  <!-- vale write-good.Passive = NO -->
  The request is forwarded to the next available replica.
  <!-- vale write-good.Passive = YES -->
  ```

- **Acronyms.** Vale flags acronyms it considers unfamiliar. Either spell the
  term out on first use (`Dynamic Resource Allocation (DRA)`), or — if the
  acronym is standard for the Modelplane audience — add it to
  `docs/utils/vale/styles/config/vocabularies/Modelplane/accept.txt` so Vale
  stops flagging it everywhere.

Internal links are checked with [htmltest](https://github.com/wjdp/htmltest)
against the built Hugo site. Build the site and check links:

```bash
nix run .#docs-htmltest
```

Both run automatically on pull requests that touch `docs/` via
`.github/workflows/docs.yml`.

### Deployment

The site deploys to [Vercel](https://vercel.com/). Vercel builds it with the
same `nix build .#docs` derivation that `nix flake check` verifies, so what
ships matches what CI checks. `vercel.json` points the build at
[`docs/vercel-build.sh`](docs/vercel-build.sh), which installs Nix into
Vercel's build image, runs the build, and writes the static site to `public/`.
Vercel's GitHub app drives deploys as usual: preview URLs on pull requests
(including from forks) and production on merge to `main`.

## Submitting changes

Before opening a PR, run `nix flake check` and make sure it passes. If you
changed a composition function, make sure there's a test covering the change.

### Writing style

Using an agent to help write code, commits, PRs, or issues is fine. But the prose it
produces should be indistinguishable from something you'd write yourself. An
agent's first draft rarely is: it tends to pad, hedge, and decorate. Treat that
draft as a starting point and cut it hard, often by half or more. If a sentence
isn't carrying information a reader needs, delete it.

The tells to edit out:

- **Filler and throat-clearing.** "It's important to note that", "In order to",
  "This change serves to". Say the thing directly.
- **Table stakes.** "Updated the tests", "Ran the linter", "Ensured the code is
  well-formatted". These are expected, so reporting them is noise.
- **Bragging.** "Robust", "elegant", "comprehensive", "powerful", "seamless". A
  description should let the reader judge the change, not judge it for them.
- **Decorative em dashes.** Agents reach for an em dash whenever a sentence could
  use a comma, a colon, or a full stop. An occasional one is fine; a paragraph
  built on them reads like a machine wrote it.
- **Restating the obvious.** "This PR adds X" when the title says "Add X". Lead
  with the problem instead.

Never invent rationale. The "why" behind a change comes from the author's
intent, which a diff doesn't contain: a diff shows that a field was renamed, not
why. A plausible-sounding reason made up to fill the gap is worse than no reason
at all, because a reader can't tell it apart from a real one and it lands in the
permanent record as fact. **If you don't know why a change was made, describe
what it does and stop.**

The underlying goal is plain, dense prose that respects the reader's time.
**When in doubt, shorter.**

### Commit messages

Each commit is a self-contained, logical layer of the change. The history tells
the story of *what the code does and why*, not how you developed it. Fold
incremental work into the commit it belongs to; don't leave behind "address
review feedback", "fix tests", WIP, or rebase-merge commits.

Write the subject in the imperative mood, naming specifically what the change
does, so it's legible at a glance in a log: "Order KServe CRD deletion after the
resources that use it", not "fix deletion" or "update composition". Keep it under
~70 characters, capitalized, with no trailing period and no typed prefix like
`feat:`, `fix:`, or `chore:`.

Write a body for every commit except the most mechanical, like a schema regen.
Lead with the problem: what was wrong, missing, or how things behaved before.
Then give the change and why it's right; the reasoning is what a future reader
needs and can't recover from the diff. Note trade-offs honestly. Use prose, not
a list recounting which files you touched; the diff already shows what changed,
so the message is for what it doesn't show. Reserve bullets for genuinely
parallel items. Wrap at ~72 characters, and reference issues with a terse
trailer at the end: `Fixes #96.`, `Towards #92.`, `Depends on crossplane/cli#24.`

Sign off every commit with `git commit -s`. This adds a `Signed-off-by` line
certifying you have the right to submit the code under the project's license
(the [Developer Certificate of Origin](https://developercertificate.org/)).

A representative commit:

```
Order KServe CRD deletion after the resources that use it

compose-kserve-backend installs KServe as two Helm releases: kserve-crds
provides the CRDs, and kserve-controller installs custom resources of those
CRDs.

Nothing ordered their deletion. On teardown the CRD release could uninstall
first, removing a CRD while the resources release still owned CRs of that kind.
The resources release's uninstall then failed and hung indefinitely, blocking
the rest of the teardown.

This adds a Usage holding the CRD release until the resources release is gone,
so the CRs are deleted while their CRD still exists.

Signed-off-by: Nic Cope <nicc@rk0n.org>
```

### Pull requests

A PR description carries the same problem-first story as a commit, scaled up to
the whole change. Open with the issue it resolves — `Fixes #95.` on its own line,
one per issue — then describe what was wrong or missing, the change, and why it's
the right one. Be honest about trade-offs, breaking changes, and anything still
unresolved.

Don't single out parts of the diff as noteworthy or risky to fill a "things to
review" section. Flag something only when you actually know it warrants a closer
look; a list of callouts chosen just to have one reads as signal while carrying
none.

Don't hard-wrap the body. GitHub renders it as Markdown and reflows to the
viewport, so write each paragraph as one line and let it wrap; manual breaks show
up as ragged lines in the rendered view. (This is the opposite of commit
messages, which are read raw and so are wrapped at ~72.)

Be selective. A description isn't a summary of every commit; it's the headline
of the change with enough context to review it. Lead with what matters, and let
preparatory or secondary commits fall to a sentence or drop out entirely — the
reviewer has the commits and the diff for the rest. Resist giving each commit its
own `###` section; reach for subheadings only when one genuinely large change has
distinct parts worth separating. When the change is small, a paragraph or two is
the whole description.

Reach for the things a diff can't show: before/after YAML for an API change, a
plainly stated breaking-change note, or how you validated the change end to end.
Use bullets only for genuinely parallel items.

A representative description:

```
Fixes #45.

The ModelDeployment scheduler considered all InferenceEnvironments as scheduling candidates regardless of their readiness. An environment still provisioning its cluster would be selected if its computed capacity matched, creating a ModelPlacement that targets an environment that can't serve traffic yet. The user then sees errors until the environment becomes Ready.

This makes the scheduler skip environments without a `Ready=True` condition. When a new environment finishes provisioning, the next reconcile picks it up.

The pool-fitting logic moves into a `_best_pool_fit` helper to keep `schedule()` under the branch-count lint threshold. A new `test-model-deployment-not-ready-env` case covers the skip behavior.
```

## Reporting issues

Open an issue with the bug report or feature request template. The Writing style
section above applies here too: lead with the problem, be specific, and don't pad
or invent.

For a bug, the title should name the symptom, and the root cause if you know it:
"InferenceGateway never becomes ready on a fresh control plane: Gateway API CRDs
not installed". Describe what you observed before what you think causes it, and
back it with evidence a reader can't reconstruct themselves — the actual error
message, status condition, or log output in a fenced block, and a link to the
offending code with line numbers if you found it. Give numbered, copy-pasteable
reproduction steps, and a workaround if you have one. List the versions of
everything involved: Modelplane, Crossplane, the inference backend, the cluster
and its provider, and Kubernetes.

For a feature request, describe the problem or limitation before any solution; a
well-framed problem is worth more than a proposed fix. If you do have a shape in
mind, show it concretely — the YAML, CLI, or API a user would write — and note
the trade-offs and the alternatives you considered.
