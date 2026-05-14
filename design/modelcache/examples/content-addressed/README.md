# Content-addressed examples (v0.2)

Three v0.2 previews on the content-addressed substrate. Same user-facing CRD shape across all of them; the difference vs the OSS PVC examples in the parent directory is `storage.backend`.

## Provider split

The substrate spec is one API shape with two providers:

| Provider | `storage.backend` | Status |
|---|---|---|
| **Upbound weight delivery** (commercial, hosted) | `ContentAddressed` | Managed service — fleet-scale dedup, cross-region delivery, Modal-class cold-start. |
| **BYO via webhook** (OSS extension point) | `Custom` | Customer or third-party CAS implements the webhook contract. Same shape, customer-run substrate. |

The examples below use `backend: ContentAddressed`. For BYO, swap to `backend: Custom` with the equivalent webhook target — the rest of the spec is identical.

## Examples

- `01-basic.yaml` — Llama 3.3 70B on the content-addressed substrate. Same model as `../01-basic-weights.yaml` on PVC. vLLM 95s → ~14s ([Modal benchmark](https://modal.com/blog/truly-serverless-gpus)).
- `02-lora-adapter.yaml` — base model + per-tenant LoRA. Multi-LoRA dedup across tenants.
- `03-compiled-engine.yaml` — TRT-LLM compiled engine keyed by `(model, hardware, config)`. Also covers NIM Mode 2b profile cache dirs and KitOps ModelKit bundles.
