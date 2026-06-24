# Contributing to Modelplane

## Discuss first

Modelplane is a discuss-first project. For anything beyond a small bug fix, open
an issue before you write code. The point is to agree on the shape of a change
before you (or your agent) spend time and tokens building it, so nobody pours
effort into a contribution we then can't accept.

PRs are welcome for small, self-evident fixes: a typo, a broken link, a one-line
correction. For anything that changes behavior, an API, or a design, raise an
issue first and let's align on the approach. When in doubt, open an issue, not a
PR.

Using an agent is fine, but contributions still have to clear the bar the
Writing style section sets out. We may close low-effort issues and PRs,
including unreviewed AI-generated ones, without much discussion.

## Writing style

Using an agent to help write code, commits, PRs, or issues is fine. But the prose it
produces should be indistinguishable from something you'd write yourself. An
agent's first draft rarely is: it tends to pad, hedge, and decorate. Treat that
draft as a starting point and cut it hard, often by half or more. If a sentence
isn't carrying information a reader needs, delete it.

The tells to edit out:

- Filler and throat-clearing. "It's important to note that", "In order to",
  "This change serves to". Say the thing directly.
- Table stakes. "Updated the tests", "Ran the linter", "Ensured the code is
  well-formatted". These are expected, so reporting them is noise.
- Bragging. "Robust", "elegant", "comprehensive", "powerful", "seamless". A
  description should let the reader judge the change, not judge it for them.
- Decorative em dashes. Agents reach for an em dash whenever a sentence could
  use a comma, a colon, or a full stop. An occasional one is fine; a paragraph
  built on them reads like a machine wrote it.
- Restating the obvious. "This PR adds X" when the title says "Add X". Lead
  with the problem instead.
- Over-formatting. Bold, headers, and bullets sprinkled in to look organized or
  authoritative. Prose carries reasoning; reserve structure for content that's
  genuinely structured.

Never invent rationale. The "why" behind a change comes from the author's
intent, which a diff doesn't contain: a diff shows that a field was renamed, not
why. A plausible-sounding reason made up to fill the gap is worse than no reason
at all, because a reader can't tell it apart from a real one and it lands in the
permanent record as fact. If you don't know why a change was made, describe what
it does and stop.

The underlying goal is plain, dense prose that respects the reader's time. When
in doubt, shorter.

## Reporting issues

Open an issue with the bug report or feature request template. The Writing style
section above applies here too: lead with the problem, be specific, and don't pad
or invent. Don't hard-wrap the body; like a PR, GitHub renders it as Markdown and
reflows to the viewport, so write each paragraph as one line and let it wrap.

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
Python, shell, and Nix linters and formatters, the [ty](https://docs.astral.sh/ty)
type checker on every composition function, plus unit tests for every function.
Run `nix flake show` to see what else is available.

```bash
nix flake check            # or: ./nix.sh flake check
```

`nix run .#fix` auto-fixes most lint and formatting issues. Run it before
opening a PR.

## Submitting changes

Before opening a PR, run `nix flake check` and make sure it passes. If you
changed a composition function, make sure there's a test covering the change.

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

### Work from a fork

Open every PR from a branch on your own [fork][fork], never a branch of the main
repo. This holds for maintainers too, and for any agent working on your behalf:
point it at your fork.

[fork]: https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/working-with-forks/fork-a-repo

## Working on composition functions

Modelplane is a [Crossplane](https://crossplane.io/) project. The core logic
lives in Python composition functions under `functions/`.
`compose-inference-gateway/function/fn.py` is a good reference implementation to
model new functions on.

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
type checking don't need to run the Crossplane CLI first. Regenerate them by
building after you change an XRD or bump a dependency:

```bash
nix run .#build
```

The build deletes and recreates the whole `schemas/python/` tree, so models for
XRDs or dependencies you've removed don't linger.

### Tests

Every function has tests under `functions/<name>/tests/test_fn.py`. The
canonical form is a table of `Case`s, each running the function on a
`RunFunctionRequest` and comparing the whole `RunFunctionResponse` against an
expected one — not asserting on individual fields. `compose-usages` is a clean
example; `compose-model-cache` shows the same form scaled up to a multi-pass
reconcile. The skeleton:

```python
@dataclasses.dataclass
class Case:
    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:
        cases = [
            Case(
                name="describes what this case exercises",
                req=fnv1.RunFunctionRequest(...),
                want=fnv1.RunFunctionResponse(...),
            ),
        ]
        for case in cases:
            with self.subTest(case.name):
                got = await self.runner.RunFunction(case.req, None)
                self.assertEqual(
                    json_format.MessageToDict(case.want),
                    json_format.MessageToDict(got),
                    "-want, +got",
                )
```

Build the XR with
`resource.dict_to_struct(xr.model_dump(exclude_none=True, mode="json"))` from a
generated Pydantic model; build other observed, desired, and required resources
as plain dicts. Because `want` is the whole response, it must include the parts
the function always emits: `meta.ttl` (60s), an empty `context`, and any
conditions, results, and requirements. Give observed conditions a fixed
`lastTransitionTime` so the input is deterministic. Protobuf maps
(`desired.resources`, `requirements.resources`) compare order-independently, but
repeated fields (`conditions`, `results`, status arrays) must match the order
the function emits.

Some existing tests (`compose-serving-stack`, the second method in
`compose-eks-cluster`) predate this form and assert on individual fields. Don't
model new tests on them. Add new cases to the function's `test_fn.py` and run
`nix flake check` to verify they pass.

### Running locally

`nix run .#run` builds the project and runs it on a local development control
plane: a [KIND](https://kind.sigs.k8s.io/) cluster with its own OCI registry,
created and managed by the Crossplane CLI. It builds the functions, loads the
packages into the local registry, installs the Configuration, and points
`kubectl` at the cluster. Iterate by editing a function and rerunning it; tear
down with `nix run .#stop`.

```bash
nix run .#run
```

This needs a running Docker-compatible container runtime (for KIND and the local
registry). It works on Linux and macOS with no emulation: the function images
are Linux images assembled entirely from data — a prebuilt Python interpreter
and dependency wheels plus our own source — so there's no cross-compilation. The
same is true of `nix run .#build`.

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

### Manifest shortcodes

Annotated YAML manifests live under `docs/manifests/`, one subtree per docs
section: `getting-started/` backs the getting started guide, `concepts/` backs
the platform and model concept pages, and `examples/` backs the Examples page.
A page references only manifests from its own section's subtree. Two shortcodes
render them in content pages.

`manifests` renders the file inline with syntax highlighting, followed by
a `kubectl apply -f <url>` block pointing to the published file:

```markdown
{{</* manifests "concepts/inference-gateway.yaml" */>}}
```

Optional named args:

| Arg | Default | Effect |
|---|---|---|
| `apply="false"` | — | omit the kubectl block |
| `command="kubectl delete -f"` | `kubectl apply -f` | override the verb |

Hugo forbids mixing positional and named arguments in one shortcode call, so a
call that passes `apply=` or `command=` must pass the path as `path=` too:

```markdown
{{</* manifests path="concepts/inference-gateway.yaml" apply="false" */>}}
```

`manifest-url` emits just the absolute URL of the file, for use inside
an existing code fence:

```markdown
kubectl delete -f {{</* manifest-url "concepts/inference-gateway.yaml" */>}}
```

Both shortcodes take a path relative to `docs/manifests/` and fail the build
with a clear error if the file doesn't exist.

The `docs-manifests` flake check validates every Modelplane manifest the docs
show — all the files under `docs/manifests/`, including the API-reference
examples under `docs/manifests/reference/` — against the generated Pydantic
models in `schemas/python/`. It runs the models with `extra="forbid"` at every
level, so an example that drifts from the live API schema fails CI: a missing
required field, a wrong type, a bad enum, or any field the schema doesn't define
(a typo, or a field the API renamed or dropped). The docs can't show a manifest
the current API would reject. Resources from other API groups (provider configs,
core Kubernetes, Crossplane packages) have no model and are skipped. The
validator is `docs/utils/validate/validate_manifests.py`.

### Linting and link checking

Docs prose is linted with [Vale](https://vale.sh) and internal links are checked
with [htmltest](https://github.com/wjdp/htmltest). Both run as flake checks, so
run them with the rest of CI:

```bash
nix flake check
```

Custom Modelplane rules live in `docs/utils/vale/styles/Modelplane/`.

Vale flags brand names, acronyms, API types, and technical terms it doesn't
recognise. Add them to
`docs/utils/vale/styles/config/vocabularies/Modelplane/accept.txt` — that is
the single place for all Vale exceptions. Entries are case-sensitive regular
expressions, one per line.

CI runs them on every pull request via the same check (see
[`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

### Deployment

The site deploys to [Vercel](https://vercel.com/). Vercel builds it with the
same `nix build .#docs` derivation that `nix flake check` verifies, so what
ships matches what CI checks. `vercel.json` points the build at
[`docs/vercel-build.sh`](docs/vercel-build.sh), which installs Nix into
Vercel's build image, runs the build, and writes the static site to `public/`.
Vercel's GitHub app drives deploys as usual: preview URLs on pull requests
(including from forks) and production on merge to `main`.

## Releasing

Cutting a release and versioning the docs is a maintainer task; see
[RELEASING.md](RELEASING.md).
