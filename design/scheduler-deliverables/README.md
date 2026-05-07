# Scheduler & Capability Model — design deliverables

This directory is a **design-time preview** of the CRDs and example resources described in [`../scheduler-1pager.md`](../scheduler-1pager.md). Once the design is aligned, these definitions move into [`apis/`](../../apis/) (one directory per CRD, alongside the matching Composition) and the examples move into [`examples/`](../../examples/) at the repo root. Until then, this directory is a self-contained sketch that platform engineers and reviewers can read end-to-end without scrolling between issues, comments, and code.

Nothing here is wired up yet — XRDs aren't installed by `up` packs, examples aren't run by CI. Treat this as a proposal in YAML form.

## Layout

```
scheduler-deliverables/
├── README.md                                 (this file)
├── xrds/                                     # proposed CompositeResourceDefinitions
│   ├── inferencecluster.yaml                 # cluster-scoped substrate (was InferenceEnvironment)
│   ├── capabilityvocabulary.yaml             # namespace-scoped vocab CR (default in modelplane-system)
│   ├── modeldeployment.yaml                  # namespaced workload + scale subresource
│   ├── modelplacement.yaml                   # IR; one per logical replica (replica == placement)
│   ├── modelendpoint.yaml                    # namespaced weighted routing (per #60)
│   └── modelservice.yaml                     # namespaced routing-only target (was InferenceProvider)
└── examples/
    ├── inferencecluster-prod-coreweave.yaml  # cluster-scope substrate
    ├── capabilityvocabulary-default.yaml     # cluster-wide default in modelplane-system
    ├── modelservice-together.yaml            # SaaS routing target
    ├── kimi-k2.yaml                          # frontier MoE, 5P3D disaggregation
    ├── qwen3-coder.yaml                      # n-gram speculation, multi-LoRA
    ├── gpt-oss-20b.yaml                      # small MoE, MXFP4-native, scale-to-zero
    └── assistant-endpoint.yaml               # weighted ModelEndpoint (Deployment + ModelService)
```

## Scopes (per Bassam's whiteboard)

| Cluster scope | Namespace scope (= environment / lifecycle scope) |
|---|---|
| 0..N `InferenceCluster` | 0..1 `ModelEndpoint` |
| 0..1 default `CapabilityVocabulary` (in `modelplane-system`) | 0..N `ModelDeployment` |
| | 0..N `ModelPlacement` (composed from MDs; one per replica) |
| | 0..N `ModelService` |
| | 0..1 override `CapabilityVocabulary` |

Namespace = environment. Each namespace is a lifecycle boundary (prod, staging, dev). Pushing a `ModelDeployment` revision triggers lifecycle reconciliation in that namespace.

## Claim cascade (per #56)

Workloads use a three-level claim cascade: `clusterClaim` filters `InferenceCluster` candidates; `nodeClaim` filters node pools within matched clusters; `deviceClaim` is DRA-shaped and filters devices within matched pools. `requiredEngineFeatures` is a separate set-membership constraint matched against the cluster's `KServeBackend.spec.engine.features`.

`ModelService` is **not** a fleet-member candidate — it's routing-only, valid only as a `ModelEndpoint` route target. The matcher does not consider `ModelService` for placements; a separate concept for *placing* against dedicated SaaS endpoints (provisioning a Together / Baseten dedicated inference) is on Nic to define.

## Replica == Placement (per Bassam's whiteboard)

One `ModelPlacement` per logical replica of a `ModelDeployment`. Each MP composes one `LLMInferenceService.spec.replicas: 1` onto a chosen `InferenceCluster`. Multi-node logical replicas (e.g. Kimi K2 with PP=2) are still one MP — multi-pod via LWS within one cluster.

KEDA wires in via the **scale subresource** on `ModelDeployment`: a stock `ScaledObject` targets the MD; KEDA writes `MD.spec.replicas`; the composer reconciles MPs to match. No custom KEDA scaler required.

**v1 constraint**: all MPs of a single MD land on the same cluster (matcher decision made on the first MP, reused). v2 drops this for cross-cluster scaling. Multi-region today = multiple `ModelDeployment`s, each with N MPs on a different cluster, routed via `ModelEndpoint`.

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
