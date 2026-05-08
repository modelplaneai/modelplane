# Modelplane — Scheduling & Placement Preview

This folder previews the **scheduling and placement** layer of Modelplane: federation matcher, in-cluster integration (KAI + Kueue), the plugin/adapter system, IRs, lifecycle, and the user-facing surface. The full API shape (XRD field-by-field) lives in [PR #64](https://github.com/modelplaneai/modelplane/pull/64).

## What to read

| If you are… | Start with |
|---|---|
| A first-time user | [quickstart.md](./quickstart.md) — 4 CRs to a working curl |
| Operating Modelplane (or evaluating it) | [advanced.md](./advanced.md) — 5 common scenarios as deltas from the quickstart |
| Wanting to understand *how* scheduling and placement actually work | [scheduling.md](./scheduling.md) — two-stage scheduling, federation matcher, KAI/Kueue, multi-tenancy, BYOC behavior, walkthroughs |
| Reviewing the architecture / design decisions | [design.md](./design.md) — principles, plugin/adapter system, IRs, Crossplane lifecycle layers, risks, open questions, roadmap |

Plus:

- [`examples/`](./examples/) — illustrative YAML for clusters, workloads, classes, endpoints, providers (referenced from all of the above)
- [`diagram.excalidraw`](./diagram.excalidraw) — API hierarchy + lifecycle diagram

## Status

Draft for Bassam + Nic review. The API shape has moved to [#64](https://github.com/modelplaneai/modelplane/pull/64); this PR is now scoped to scheduling, placement, and the adapter/IR system that ties them together.
