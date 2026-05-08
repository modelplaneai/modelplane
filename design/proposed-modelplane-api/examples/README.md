# Examples

Illustrative YAML for the use cases the matcher + renderer handle. The shapes track Nic's [#64](https://github.com/modelplaneai/modelplane/pull/64) where they overlap; some examples are still in the older preview shape and will be touched up when #64 lands.

## Workloads — what the federation matcher handles

| File | Use case | Code path |
|---|---|---|
| [`workloads/gpt-oss-20b.yaml`](./workloads/gpt-oss-20b.yaml) | Single-node, single-GPU. `Tensor 1`. Smallest path through the matcher. | matcher: cluster filter + single-pool capacity check. renderer: 1 pod, no LWS |
| [`workloads/kimi-k2.yaml`](./workloads/kimi-k2.yaml) | Multi-node TP+PP — `TensorPipeline 8/2` over IB. | matcher: `Topology.shape() → (2, 8)`, capacity for 2 nodes. renderer: LWS group of 2 |
| [`workloads/kimi-k2-eu.yaml`](./workloads/kimi-k2-eu.yaml) | EU-region sibling of kimi-k2 — multi-region pattern. | matcher: `clusterSelector.matchLabels` filters to EU ICs |
| [`workloads/qwen3-coder.yaml`](./workloads/qwen3-coder.yaml) | Multi-node FP8 — `TensorPipeline 8/2` with FP8 capability filter. | matcher: pool-level CEL on `gpu.features` |
| [`workloads/acme-vllm-fork.yaml`](./workloads/acme-vllm-fork.yaml) | Engine fork — `engine.image` + opaque args. | renderer: pass-through engine args |

Disaggregated workloads (top-level decode + `spec.prefill` block) come with [#64](https://github.com/modelplaneai/modelplane/pull/64).

## Substrate — InferenceClusters that the matcher walks

| File | What it shows | Notes for the matcher |
|---|---|---|
| [`clusters/managed-gke-a3.yaml`](./clusters/managed-gke-a3.yaml) | Modelplane-provisioned GKE A3 with NVIDIA pool | `auto` scheduler resolves to `managed-kai` (per pre-#64 design — Nic's #64 doesn't model a scheduler axis) |
| [`clusters/managed-gke-a3-kai.yaml`](./clusters/managed-gke-a3-kai.yaml) | Same but scheduler pinned to `managed-kai` explicitly | — |
| [`clusters/byoc-coreweave-h200-dra.yaml`](./clusters/byoc-coreweave-h200-dra.yaml) | BYOC, DRA, BYO Kueue | `cluster.source: Existing` — matcher consumes labels + pool classes |
| [`clusters/byoc-coreweave-kai-h200.yaml`](./clusters/byoc-coreweave-kai-h200.yaml) | BYOC, DRA, BYO KAI | — |
| [`clusters/byoc-eks-h100-no-dra.yaml`](./clusters/byoc-eks-h100-no-dra.yaml) | BYOC, no DRA, device-plugin mode | (Nic's #64 simplifies to DRA-required; this example reflects the pre-#64 design) |
| [`clusters/reference/`](./clusters/reference/) | Per-SKU reference clusters | Not exercised by the matcher; reference templates customers copy |
| [`inferenceclasses/`](./inferenceclasses/) | StorageClass-style hardware bundles | Resolved into pool capabilities the matcher's CEL evaluates against |

## Routing — endpoints + providers

| File | What it shows |
|---|---|
| [`endpoints/assistant.yaml`](./endpoints/assistant.yaml) | Weighted ME across multiple MDs + an `InferenceProvider` for Together AI spillover |
| [`endpoints/multi-region.yaml`](./endpoints/multi-region.yaml) | ME routing across regional MDs (us-east + eu-west) with Together fallback |
| [`providers/together.yaml`](./providers/together.yaml) | SaaS routing target — never a placement target |

Per Nic's #64, the routing surface is `ModelService` (with `spec.endpoints[]`) and `ModelEndpoint` is the per-replica composed resource. These examples currently use the older `ModelEndpoint`-as-routing shape and will be migrated.

## Reading order

If you want to trace a request from YAML to running pod, read in this order:

1. [`workloads/kimi-k2.yaml`](./workloads/kimi-k2.yaml) — the user's MD
2. [`../design.md`](../design.md) — what the matcher does, with this exact MD as the trace
3. [`../../../functions/compose-model-deployment/scheduling.py`](../../../functions/compose-model-deployment/scheduling.py) — the matcher logic
4. [`../../../functions/compose-model-placement/main.py`](../../../functions/compose-model-placement/main.py) — the renderer that lands the LLM-IS on the chosen cluster
