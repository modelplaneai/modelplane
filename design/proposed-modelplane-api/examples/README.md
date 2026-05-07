# Examples — index

Organized by resource type. Every YAML here is design-time preview;
nothing is wired up.

```
examples/
├── workloads/             ModelDeployment examples (ML/App team)
├── endpoints/             ModelEndpoint examples (routing)
├── providers/             InferenceProvider examples (SaaS routing targets)
├── clusters/              InferenceCluster examples
│   ├── managed-*.yaml     Modelplane-provisioned (cluster.source: GKE/EKS/AKS)
│   ├── byoc-*.yaml        BYO existing K8s (cluster.source: Existing)
│   └── reference/         Per-SKU templates customers copy
└── inferenceclasses/      Hardware-bundle CRs (StorageClass-style)
```

## Workloads (`workloads/`)

| File | What it shows |
|---|---|
| `kimi-k2.yaml` | Frontier MoE, 2× 8× H200, 5P3D disaggregation, FP8 weights + KV. matchAttributes break-glass over typed attribute vocabulary. |
| `kimi-k2-eu.yaml` | EU-region sibling — multi-region pattern (one MD per region; `cloud.region` pinning). |
| `qwen3-coder.yaml` | Single-node TP=8, n-gram speculation, 3 LoRA adapters, user-defined `acme.example/*` attributes for team affinity. |
| `gpt-oss-20b.yaml` | Small MoE, scale-to-zero, **labels-first match path** (matches NVIDIA GPU operator's `nvidia.com/gpu.family` node label — no DRA needed). |
| `acme-vllm-fork.yaml` | **Break-glass via `engine.advanced[]`** — required custom features for an engine fork. Demonstrates how a workload requires a feature not yet typed in `engine.optimizations`. |

## Endpoints (`endpoints/`)

| File | What it shows |
|---|---|
| `assistant.yaml` | Weighted ME across heterogeneous MDs + Together (InferenceProvider) for SaaS spillover. |
| `multi-region.yaml` | ME routing Kimi K2 across us-east-1 and eu-west-1 MDs. |

## Providers (`providers/`)

| File | What it shows |
|---|---|
| `together.yaml` | Together AI as an `InferenceProvider` — routing-only target, never a placement candidate. Auth via Secret. |

## Clusters (`clusters/`)

The BYO matrix in concrete form:

| File | Cluster source | Scheduler | Backend | DRA |
|---|---|---|---|---|
| `managed-gke-a3.yaml` | GKE (Modelplane-provisioned) | `managed-kueue` | `managed-kserve@v0.18.0` | yes |
| `byoc-coreweave-h200-dra.yaml` | Existing | `kueue` (BYO) | `kserve@v0.18.0` (BYO) | yes |
| `byoc-coreweave-kai-h200.yaml` | Existing | **`kai`** (BYO) | `kserve@v0.18.0` (BYO) | yes |
| `byoc-eks-h100-no-dra.yaml` | Existing | `kueue` (BYO) | `kserve@v0.18.0` (BYO) | **no — `device-plugin`** |
| `reference/*` | Existing (templates) | `managed-kueue` | `managed-kserve@v0.18.0` | yes |

`reference/` holds per-SKU templates customers copy — they show the
attribute shape per known hardware and reference an `InferenceClass`.
The `byoc-*` and `managed-*` files at the top level are concrete
configurations demonstrating different points in the BYO matrix.

## InferenceClasses (`inferenceclasses/`)

The default per-SKU hardware-class catalog — H100/H200/B200/B300/MI300X/L40S/A100,
in 8x and Grace-4x forms. See `inferenceclasses/README.md`.
