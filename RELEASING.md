# Releasing Modelplane

This is the maintainer process for cutting a Modelplane release and versioning
the docs. Contributing changes is covered in [CONTRIBUTING.md](CONTRIBUTING.md);
this file is only for the small set of people who publish releases.

## Releasing

Releases are cut from a release branch and published by the `CI` workflow. To
release a new minor version, e.g. `v0.1.0`:

1. From the GitHub UI, create a `release-0.1` branch from `main`.
2. Create a GitHub release targeting that branch, and let the release create the
   tag `v0.1.0`.
3. Run the `CI` workflow (Actions → CI → Run workflow) against the `v0.1.0` tag,
   setting the `tag` input to `v0.1.0`.

The `tag` input makes the workflow push the package with that exact version
rather than the dev version it derives from git metadata on ordinary runs.
Patch releases (e.g. `v0.1.1`) reuse the existing `release-0.1` branch: cut the
release from it and run the workflow against the new tag.

## Versioning the docs

Docs are versioned at the minor level, and the versions are this repo's own
`release-X.Y` branches: whatever is on `release-0.2` is what the 0.2 docs say.
Cutting that branch in step 1 above is this repo's whole part in publishing a
version.

The rest happens in the docs site repo,
[docs-site](https://github.com/modelplaneai/docs-site). It builds every version
from its own `main` into one deployment — the latest release at the root, older
releases under `/vX.Y/`, and this repo's `main` under `/main/` — and it is the
one place that decides which release is latest. Publishing a new version is one
entry added to its version list; see that repo's README.

To fix a typo in a released version, push the fix to that `release-X.Y` branch
here.
