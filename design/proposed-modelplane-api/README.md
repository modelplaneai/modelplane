# Modelplane — Scheduling & Placement (sketch)

This folder is a sketch of the scheduling and placement layer. The actual code lives under [`functions/`](../../functions/) at the repo root; this folder hosts the design pointer + examples.

- **API shape** is owned by [PR #64](https://github.com/modelplaneai/modelplane/pull/64).
- **Implementation sketch** lives in:
  - [`functions/compose-model-deployment/`](../../functions/compose-model-deployment/) — federation matcher + composer (MD → ModelReplicas + ModelEndpoints)
  - [`functions/compose-model-placement/`](../../functions/compose-model-placement/) — renderer (ModelReplica → KServe LLMInferenceService + DRA ResourceClaims)
- **Design doc (one-pager)**: [design.md](./design.md) — points at the code, dependencies, use cases.
- **Examples**: [`examples/`](./examples/) — illustrative YAML exercising the use cases the matcher / renderer handle.

The code under `functions/` doesn't run yet — it targets API protos that haven't been generated. The shape, dependencies, and use cases are real; wiring is gated on #64 landing.
