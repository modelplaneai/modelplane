# Scheduler & Capability Model — design deliverables

This directory is a **design-time preview** of the CRDs and example resources described in [`../scheduler-1pager.md`](../scheduler-1pager.md). Once the design is aligned, these definitions move into [`apis/`](../../apis/) (one directory per CRD, alongside the matching Composition) and the examples move into [`examples/`](../../examples/) at the repo root. Until then, this directory is a self-contained sketch that platform engineers and reviewers can read end-to-end without scrolling between issues, comments, and code.

Nothing here is wired up yet — XRDs aren't installed by `up` packs, examples aren't run by CI. Treat this as a proposal in YAML form.

## Layout

```
scheduler-deliverables/
├── README.md                                 (this file)
├── xrds/                                     # proposed CompositeResourceDefinitions
│   ├── inferencecluster.yaml                 # renamed from InferenceEnvironment
│   ├── inferenceprovider.yaml                # new — observed/SaaS endpoints
│   ├── capabilityvocabulary.yaml             # new — singleton vocabulary CR
│   ├── modeldeployment.yaml                  # expanded — self-contained workload
│   ├── modelendpoint.yaml                    # new — weighted routing (per #60)
│   └── modelplacement.yaml                   # repurposed as the IR
└── examples/                                 # sample CRs that exercise the API
    ├── inferencecluster-prod-coreweave.yaml  # platform-team substrate
    ├── inferenceprovider-together.yaml       # platform-team SaaS registration
    ├── capabilityvocabulary-default.yaml     # default vocabulary install
    ├── kimi-k2.yaml                          # frontier MoE, 5P3D disaggregation
    ├── qwen3-coder.yaml                      # n-gram speculation, multi-LoRA
    ├── gpt-oss-20b.yaml                      # small MoE, MXFP4-native, scale-to-zero
    └── assistant-endpoint.yaml               # weighted ModelEndpoint
```

## What's deliberately incomplete

These XRDs cover the **spec shape** for design alignment. Things that are intentionally thin and will be filled in during the move to `apis/`:

- `status` schemas are minimal — just the conditions and a representative status field per resource. `matchTrace`, `compatibility`, and granular cold-start status will be elaborated when the controller code lands.
- Validation rules (CEL expressions on the schema, `oneOf` discriminator constraints, cross-field invariants) are sketched but not exhaustive.
- The corresponding Crossplane Compositions are **not** in this directory — those are implementation. The XRDs declare the API contract.
- `KServeBackend` (already an internal XR in `apis/kservebackends/`) is not duplicated here, but it's where engine + features land in the substrate / runtime split — `InferenceCluster` is hardware, `KServeBackend` is the inference stack on that cluster, and the matcher reads engine features from the latter.
- Internal types used by the IR adapter pipeline beyond `ModelPlacement` (e.g. per-engine sub-types) are TBD.

## Where each piece ends up after alignment

| File here | Lands in |
|---|---|
| `xrds/inferencecluster.yaml` | `apis/inferenceclusters/definition.yaml` (replacing `apis/inferenceenvironments/`) |
| `xrds/inferenceprovider.yaml` | `apis/inferenceproviders/definition.yaml` |
| `xrds/capabilityvocabulary.yaml` | `apis/capabilityvocabularies/definition.yaml` |
| `xrds/modeldeployment.yaml` | `apis/modeldeployments/definition.yaml` (expanded) |
| `xrds/modelendpoint.yaml` | `apis/modelendpoints/definition.yaml` |
| `xrds/modelplacement.yaml` | `apis/modelplacements/definition.yaml` (expanded as the IR) |
| `examples/*.yaml` | `examples/` at repo root, or `examples/compositions/` for platform-team starters |
