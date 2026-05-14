# Nic 1:1 — Modelplane API alignment

## State
- PR #63 on branch `dennis/scheduler-1pager`: design 1-pager + 6 proposed XRDs + examples + reference clusters + lint script
- Bassam's review threads all addressed and resolved (16/16 closed)
- Aligned with Bassam's GPU-hardware-survey taxonomy (4 layers: Cluster / Pool / Device / dynamic)

---

## Six big decisions — want your nod or override
*(in the doc as a Decision / Lean / Alternatives table)*

1. **Default plugins.** `managed-kueue` + `managed-kserve` ship by default; BYO first-class (`kueue`/`kai`/`volcano`/`none` × `kserve`/`dynamo`/`raw-vllm`)
2. **Match path.** `matchLabels` primary; `matchAttributes` + CEL as break-glass
3. **DRA grounding.** Mandatory for BYOC w/ DRA, optional for MP-provisioned. Federation layer never emits DRA Kinds
4. **Rack-scale.** `cluster.scaleUnit: nvl72` attribute (not a new kind, not multi-pool model)
5. **Reference clusters.** Static YAML now → Crossplane provider polling SKU APIs later
6. **`ModelObjective` (SLO intent layer).** Punt past v2 — non-breaking layer above MD if/when needed

---

## Patterns established
- Replica == placement (one MP per replica; multi-region = multiple MDs + ME)
- Consumer-index discipline — every field has at least one named consumer (matcher / composer / backend adapter / gateway). No consumer → no field
- Borrow DRA *vocabulary* (typed attrs, CEL, domain-prefixed keys, `device.attributes[domain].name` access pattern); drop DRA *Kinds* (`DeviceClass` / `ResourceSlice` / `ResourceClaim`) at the federation layer
- Capability **sets**, not boolean columns (`capabilities: [fp8, fp4, mig]`)
- Architecture is metadata; capabilities do the matching work — keeps AMD eligible without naming `hopper`/`blackwell`
- Instance-type macros (`H100-NVL-8x`) + per-cloud SKU aliases (`aws:p5.48xlarge`)
- Namespace = environment (lifecycle scope)

## Anti-patterns dodged
- DRA Kinds at federation layer — different problem (runtime allocator ≠ pre-provisioning planner)
- "Claim" naming — implies allocation. Renamed to `clusterSelector` / `deviceSelector`
- `requiredEngineFeatures` as a user field — derive from declared config (`roles` → disagg, `engine.optimizations.*` → typed knobs, `adapters[]` → multi-lora). User declares what they want, not which features that needs
- Engine info on `InferenceCluster` — split substrate (IC) from runtime (KServeBackend)
- `ModelService` as placement target — routing-only

---

## Your call
- **`ModelService` shape.** It's a sketch — routing-only placeholder. Provisioning a dedicated Together / Baseten endpoint is your concept. Want my placeholder deferred or want to weigh in now?
- **Naming.** `clusterSelector` / `deviceSelector` / `ModelDeployment` / `ModelEndpoint` / `ModelService`. Anything that reads off?
- **Consumer-index discipline rule.** Too rigid? Or right call?

## Wedge story (commercial — lifted from the catalog work)
- Canonical vocabulary tracking (chip families, SKUs, engine versions, quantization formats)
- Reference InferenceClusters per known SKU, kept current
- **Continuous testing & benchmarking** of every reference cluster × every supported model family. Costly to maintain — exactly what customers pay for. Bassam validated this framing in Slack

---

## What's not in the PR (intentional)
- Real Compositions (those are implementation; XRDs are the API contract)
- Status schemas elaborated (matchTrace, drift, cold-start) — controller-code work
- Crossplane provider for SKU-based reference cluster generation — follow-up
- Pool-vs-Node split formalized in IC XRD — `nodeAttributes` covers it for now
- Fractional-GPU / MIG modeling in examples (vocab supports `parentProduct`)

## If we have time
- Reference clusters are BYO templates today (`source: Existing` + kubeconfig). Want to extend the IC XRD enum to `[GKE, EKS, AKS, OKE, CKS, Existing]` with per-cloud blocks for Modelplane-provisioned variants?
- v1 vs v2 themed scope — anything I should bump up or out?
