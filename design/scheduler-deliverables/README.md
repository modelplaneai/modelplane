# Scheduler & Capability Model — design deliverables

This directory is a **design-time preview** of the CRDs and example resources described in [`../scheduler-1pager.md`](../scheduler-1pager.md). Once the design is aligned, these definitions move into [`apis/`](../../apis/) (one directory per CRD, alongside the matching Composition) and the examples move into [`examples/`](../../examples/) at the repo root. Until then, this directory is a self-contained sketch that platform engineers and reviewers can read end-to-end without scrolling between issues, comments, and code.

Nothing here is wired up yet — XRDs aren't installed by `up` packs, examples aren't run by CI. Treat this as a proposal in YAML form.

## Layout

```
scheduler-deliverables/
├── README.md                                 (this file)
├── xrds/                                     # proposed CompositeResourceDefinitions
│   ├── inferencecluster.yaml                 # cluster-scoped substrate (was InferenceEnvironment)
│   ├── capabilityvocabulary.yaml             # cluster-scoped vocab CR (singleton)
│   ├── modeldeployment.yaml                  # namespaced workload + scale subresource
│   ├── modelplacement.yaml                   # IR; one per logical replica (replica == placement)
│   ├── modelendpoint.yaml                    # namespaced weighted routing (per #60)
│   └── modelservice.yaml                     # namespaced routing-only target (was InferenceProvider)
└── examples/
    ├── inferencecluster-prod-coreweave.yaml  # cluster-scope substrate
    ├── capabilityvocabulary-default.yaml     # cluster-scoped vocab singleton
    ├── modelservice-together.yaml            # SaaS routing target
    ├── kimi-k2.yaml                          # frontier MoE, 5P3D disaggregation
    ├── qwen3-coder.yaml                      # n-gram speculation, multi-LoRA
    ├── gpt-oss-20b.yaml                      # small MoE, MXFP4-native, scale-to-zero
    └── assistant-endpoint.yaml               # weighted ModelEndpoint (Deployment + ModelService)
```

## Scopes

| Cluster scope | Namespace scope (= environment / lifecycle scope) |
|---|---|
| 0..N `InferenceCluster` | 0..1 `ModelEndpoint` |
| 1 `CapabilityVocabulary` (singleton, name: `default`) | 0..N `ModelDeployment` |
| | 0..N `ModelPlacement` (composed from MDs; one per replica) |
| | 0..N `ModelService` |

Namespace = environment. Each namespace is a lifecycle boundary (prod, staging, dev, per-team). Pushing a `ModelDeployment` revision triggers lifecycle reconciliation in that namespace.

`CapabilityVocabulary` is cluster-scoped because the `InferenceCluster`s that declare attributes against it are cluster-scoped — one cluster's hardware semantics shouldn't be evaluated differently from each namespace. Namespaces customize via Crossplane Compositions and user-defined `acme.example/*` keys (pass-through, not vocab-validated), not by redefining vocab.

## Claim cascade

Workloads use a two-level claim cascade: `clusterClaim` filters `InferenceCluster` candidates by env-level attributes (region, tier, compliance, billing); `deviceClaim` is DRA-shaped and filters devices in matched clusters' pools. The `deviceClaim` selector matches against both node-level attributes (e.g. `modelplane.ai/interNodeFabric`) and device-level attributes (e.g. `gpu.nvidia.com/architecture`) uniformly — device attrs are uniform across devices on a node, so the conceptual node/device distinction isn't load-bearing.

`requiredEngineFeatures` is a separate set-membership constraint matched against the cluster's `KServeBackend.spec.engine.features`.

`ModelService` is **not** a fleet-member candidate — it's routing-only, valid only as a `ModelEndpoint` route target. The matcher does not consider `ModelService` for placements; a separate concept for *placing* against dedicated SaaS endpoints (provisioning a Together / Baseten dedicated inference) is on Nic to define.

**In-cluster scheduler delegation.** Modelplane decides *which cluster* a workload runs on; bin-packing, gang-scheduling, fractional-GPU, NVLink-aware placement, and capacity tracking are the in-cluster scheduler's job. We ship Kueue as the default substrate (`managed-kueue` mode, like `managed-kserve`); BYO schedulers (KAI, Volcano, existing Kueue installs) are supported via a capacity-signal contract. The signal Modelplane reads is `ClusterQueue.status` for Kueue or the equivalent from BYO schedulers — Modelplane never replaces the in-cluster scheduling logic.

**Label-vs-DRA matching.** `deviceClaim.selector` supports two paths: `matchLabels` for plain node-label matching (no DRA required; cluster `provisioning.mode: device-plugin`) and `matchAttributes` for DRA-shaped typed selection (cluster `provisioning.mode: dra`). DRA stays optional — customers who don't want it can use plain labels. The richer constraints (NVLink-domain co-location, etc.) are only expressible via DRA matchAttributes.

## Replica == Placement (per Bassam's whiteboard)

One `ModelPlacement` per logical replica of a `ModelDeployment`. Each MP composes one `LLMInferenceService.spec.replicas: 1` onto a chosen `InferenceCluster`. Multi-node logical replicas (e.g. Kimi K2 with PP=2) are still one MP — multi-pod via LWS within one cluster.

KEDA wires in via the **scale subresource** on `ModelDeployment`: a stock `ScaledObject` targets the MD; KEDA writes `MD.spec.replicas`; the composer reconciles MPs to match. No custom KEDA scaler required.

Each replica is independently scheduled by the matcher against the parent MD's claims — placements may land on the same cluster or distribute across matching clusters. Multi-region spread can also be expressed explicitly via multiple `ModelDeployment`s + a `ModelEndpoint` that routes across them.

## What's deliberately incomplete

These XRDs cover the **spec shape** for design alignment. Things that are intentionally thin and will be filled in during the move to `apis/`:

- `status` schemas are minimal — just the conditions and a representative status field per resource. `matchTrace`, `compatibility`, and granular cold-start status will be elaborated when the controller code lands.
- Validation rules (CEL expressions on the schema, `oneOf` discriminator constraints, cross-field invariants) are sketched but not exhaustive.
- The corresponding Crossplane Compositions are **not** in this directory — those are implementation. The XRDs declare the API contract.
- `KServeBackend` (already an internal XR in `apis/kservebackends/`) is not duplicated here, but it's where engine + features land in the substrate / runtime split — `InferenceCluster` is hardware, `KServeBackend` is the inference stack on that cluster, and the matcher reads engine features from the latter.
- The "different concept for *placement* against dedicated SaaS" (Nic-owned) is intentionally absent. `ModelService` is rough-sketch routing-only.

## Where each piece ends up after alignment

| File here | Lands in |
|---|---|
| `xrds/inferencecluster.yaml` | `apis/inferenceclusters/definition.yaml` (replacing `apis/inferenceenvironments/`) |
| `xrds/modelservice.yaml` | `apis/modelservices/definition.yaml` |
| `xrds/capabilityvocabulary.yaml` | `apis/capabilityvocabularies/definition.yaml` |
| `xrds/modeldeployment.yaml` | `apis/modeldeployments/definition.yaml` (expanded) |
| `xrds/modelendpoint.yaml` | `apis/modelendpoints/definition.yaml` |
| `xrds/modelplacement.yaml` | `apis/modelplacements/definition.yaml` (expanded as the IR) |
| `examples/*.yaml` | `examples/` at repo root, or `examples/compositions/` for platform-team starters |
