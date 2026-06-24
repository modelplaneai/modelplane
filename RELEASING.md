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

Docs are versioned at the minor level. Each minor release is served from its own
subdomain (`v0-1.docs.modelplane.ai`, `v0-2.docs.modelplane.ai`, …) using a single
Vercel project with one branch domain per release. The `main` branch always serves
the latest docs at `docs.modelplane.ai`. Patch releases push to the existing release
branch and need no changes to `main`.

One-time DNS setup (already done): a wildcard CNAME `*.docs.modelplane.ai →
cname.vercel-dns.com` covers every future release subdomain automatically. No new
DNS record is needed per release.

To publish docs for a new minor release (e.g. `v0.1.0`):

1. On `release-0.1`, set `version = "0.1"` in `docs/hugo.toml`. Leave
   `latest = "main"` unchanged — the latest docs always live at the root.

2. In the Vercel dashboard, open the `modelplane-docs` project and add a branch
   domain for the release:
   - Go to Settings → Domains, add `v0-1.docs.modelplane.ai`, and assign it
     to the `release-0.1` branch.
   - Add a Production environment variable scoped to the `release-0.1` branch:
     `HUGO_BASEURL` = `https://v0-1.docs.modelplane.ai/`.
   - Trigger a redeployment of `release-0.1` and confirm the subdomain serves.

3. On `main`, add an entry to `docs/data/versions.yaml`, newest first:
   ```yaml
   versions:
     - version: "main"
       url: ""
     - version: "0.1"
       url: "https://v0-1.docs.modelplane.ai"
   ```

4. Merge the `versions.yaml` change to `main`. The version dropdown on all release
   builds now links to the new subdomain.

To fix a typo or update content in an archived version, push to the release branch.
The versioned deployment rebuilds automatically.

When a new minor ships (e.g. `v0.2.0`), repeat steps 1–4 for `release-0.2`,
adding the `v0.2` entry above `v0.1` in `versions.yaml`.
