# Modelplane Scheduler & Capability Model — 1-pager

**Status:** Draft for Bassam review
**Author:** Dennis Ramdass
**Date:** 2026-05-06

## TL;DR

- **Modelplane is a Crossplane-native, multi-cloud inference control plane.**
- **The fleet** = `InferenceClusters` (customer K8s clusters running KServe + DRA + Kueue + KEDA — *workload planes*) + `InferenceProviders` (SaaS or observed endpoints — *routing-only*). Same scheduler matches both.
- **Platform teams** own substrate (3 CRDs + Crossplane Compositions for org abstractions). **ML/App teams** write a single self-contained `ModelDeployment` (or instantiate a platform Composition that generates one).
- **Wedge:** fleet-level capabilities single-cluster platforms can't reach — fleet matching, geo + compliance routing, KV cache federation, sticky sessions, failover, cost-aware routing.
- **No new in-cluster scheduler.** K8s scheduler + DRA, Kueue, KEDA/HPA, Cluster Autoscaler each own their layer; we layer above.

## Problem

VRAM-divided-by-per-GPU-memory worked for Llama-8B on an L4. It can't deploy Kimi K2, DeepSeek V4, or Llama 4 Behemoth on heterogeneous fleets. There's no way to say *"16 H200s in 2×8 layout, NVLink-grouped, IB-400G-or-better, FP8-quantized, EAGLE speculative decoding, 5P3D disaggregation"* and no way for a cluster to say *"I have that."* Baseten has these capabilities; gated behind support tickets, on Baseten's GPUs. Modelplane delivers them declaratively on customer infrastructure.

## Design principles

1. **Clean separation, no enforcement.** Platform teams own substrate; ML/App teams own workloads. Same API split or unified.
2. **Fleet-wide by construction.** A `ModelDeployment` targets the fleet (clusters + providers), not a single member. `matchTrace` reports where it fits and why elsewhere doesn't.
3. **Plain Crossplane customization.** Catalogs, defaults, governance live in Compositions, RBAC, OPA — not Modelplane primitives.
4. **No new in-cluster scheduler.** We're a meta-scheduler. K8s scheduler + DRA, Kueue, KEDA/HPA, Cluster Autoscaler each own their layer.

## Architecture: control plane + fleet

```
            Modelplane Control Plane (Crossplane)
   pick (cluster, pool) → compose ModelPlacement (IR)
                          ↓
                       Fleet
       ┌──────────────────────────────────────────┐
       │  InferenceClusters  (workload planes)    │
       │   ├─ KServe + DRA + Kueue + KEDA         │
       │   └─ Modelplane composes objects here    │
       │                                          │
       │  InferenceProviders (observed)           │
       │   ├─ Together / Baseten / Bedrock /      │
       │   │  customer-run KServe                 │
       │   └─ Modelplane routes; no lifecycle     │
       └──────────────────────────────────────────┘
```

The matcher treats `InferenceCluster` and `InferenceProvider` uniformly — both expose environment-level attributes and engine features. The three-level cascade (environment → node pool → device) applies fully to clusters; for providers, only the environment level is evaluated.

**Key architectural decisions:**

- Meta-scheduler only — compose objects, never bind devices or actuate replicas.
- `ClusterModel` / `Model` deleted; workload spec self-contained on `ModelDeployment`.
- Declared attributes are authoritative for scheduling; runtime DRA is drift detection only.
- Three-level matching cascade: environment → node pool → device.
- `ModelPlacement` is the **intermediate representation (IR)** — one per matched fleet member; version-pinned adapters consume it and absorb KServe `LLMInferenceService` schema churn (v0.17 `args`→`command`; v0.18 storage migration).
- Failover (v2) is active-active or active-passive.

## Fleet-level capabilities

Single-cluster platforms (llm-d, KServe alone, Dynamo) optimize within a cluster. Operating at the fleet layer reaches across `InferenceClusters` and `InferenceProviders` together. Some capabilities land in v1 (matching, compatibility); more land in v2 once the substrate is in place.

| Capability | What it does | When |
|---|---|---|
| Fleet matching | A single `ModelDeployment` finds eligible clusters and providers across regions, clouds, vendors; `matchTrace` shows why each one fits or doesn't | v1 |
| Hardware-heterogeneous routing | Route the same logical model to FP8/H200 for premium traffic, A100 for batch, Together for spillover, behind one `ModelEndpoint` | v1 |
| Geo + compliance routing | EU traffic to EU clusters; SOC 2 traffic only to certified clusters or compliant providers; data-residency-aware placement | v1 |
| Fleet KV cache federation | G4-tier networked cache as a global fabric across `InferenceClusters`; route to whichever already has the prefix | v2 |
| Fleet session affinity | Sticky sessions across regional ingresses; multi-turn chat lands on the same `(cluster, replica)` or the same provider endpoint | v2 |
| Fleet failover | Active-active or active-passive cutover when a cluster degrades or a provider rate-limits | v2 |
| Cost-aware routing | Pick the cheapest fleet member that fits; blend reserved / on-demand / spot / per-token across providers | v2 |
| Fleet overflow | Burst from primary `InferenceCluster` to a sibling or to an `InferenceProvider` when local capacity exhausts (#48) | v2 |
| Aggregated fleet observability | TTFT / ITL / cost / queue-depth rolled up across the fleet for one logical service | v2 |

## Who owns what

**ML/App team — workload authors**

- `ModelDeployment` — self-contained workload spec (or instantiates a platform Composition that generates one).
- `ModelEndpoint` — weighted routing across `ModelDeployment`s.

**Platform team — fleet authors**

- `InferenceCluster` resources — one per managed K8s cluster; declares attributes, node pools, engine features, `provisioning.mode`.
- `InferenceProvider` resources — one per SaaS endpoint (Together, Baseten, Bedrock, customer-run KServe). Created directly *or* composed by a Crossplane provider/Composition. Exposes engine features and attributes the same way clusters do, so the fleet scheduler matches against it uniformly.
- `CapabilityVocabulary` — singleton vocabulary CR; extends Modelplane's default with org-specific keys, ordering, KV tiers, engine features, aliases.
- Crossplane Compositions over `ModelDeployment` — for org-specific abstractions (`ApprovedModel`-style XRs), governance, defaults.
- Workload-plane substrate — KServe, DRA driver, Kueue, KEDA on each `InferenceCluster` (BYO or via `managed-kserve` mode).

**Modelplane — what the project ships**

- User-facing CRDs: `InferenceCluster`, `InferenceProvider`, `CapabilityVocabulary`, `ModelDeployment`, `ModelEndpoint`.
- Internal CRD: **`ModelPlacement` — the IR.** One per matched fleet member, owned by `ModelDeployment`. Adapter functions watch it and emit upstream objects. Platform-internal-by-convention.
- Crossplane composition functions — the matcher (emits `ModelPlacement`s) and the version-pinned KServe adapter (consumes them).
- Drift detection controller — reads runtime DRA `ResourceSlice`s, surfaces `CapabilityDrift` conditions.
- Default `CapabilityVocabulary` install — well-known keys, engine features, KV tiers, aliases.
- Starter Compositions in `examples/compositions/` derived from vLLM recipes.
- KServe install manifests pinned per supported version (`managed-kserve` mode).

## How users consume it

**ML/App team day-one.** Write a `ModelDeployment` (or instantiate a platform-team Composition like `ApprovedModel` that generates one). Modelplane's matcher emits one `ModelPlacement` per matched fleet member; the version-pinned adapter renders it to `LLMInferenceService` on the chosen `InferenceCluster` (or routes to the chosen `InferenceProvider`). KServe provisions pods. DRA binds devices. KEDA scales replicas. The endpoint is reachable via `ModelEndpoint`. `matchTrace` shows which fleet members were considered and why excluded.

**Platform team day-one.**

1. Install Modelplane on the Crossplane control plane (CRDs + composition functions).
2. Install workload-plane substrate on each managed K8s cluster (KServe + DRA + Kueue + KEDA), or use `managed-kserve` mode where Modelplane installs a pinned KServe.
3. Create one `InferenceCluster` per managed cluster; declare attributes and engine features.
4. Create one `InferenceProvider` per SaaS endpoint (or install a Crossplane provider that creates them programmatically).
5. (Optional) extend `CapabilityVocabulary`; ship Crossplane Compositions abstracting `ModelDeployment` for org governance.

## Extensibility points

| Extension | Owner | Why use it |
|---|---|---|
| Crossplane Compositions over `ModelDeployment` | Platform team | Org-specific abstractions, approved-model catalogs, defaults, shorter API for ML/App teams |
| Crossplane Compositions over substrate CRDs | Platform team | Wrap `InferenceCluster` / `InferenceProvider` with org-specific provisioning (e.g., a `ProductionCluster` XR that sets attributes + RBAC + monitoring) |
| `CapabilityVocabulary` extension | Platform team | Add org-specific keys (compliance levels, custom hardware), engine feature names for forks, KV tier overrides — without a CRD bump |
| `engine.args` opaque pass-through | ML/App team or Composition author | Engine flags Modelplane doesn't model |
| `engine.advanced[]` typed-name break-glass | ML/App team or Composition author | Novel knobs the IR doesn't model yet, with structure for adapters |
| `requires.matches.cel` CEL escape hatch | ML/App team | Boolean / set-arithmetic constraints not expressible in typed `requires` |
| `requires.attributes` user-defined keys | Both teams | Org-specific match dimensions (cost center, team, security clearance) |
| `requires.engineFeatures` custom feature names | Platform team (declares) + ML/App team (uses) | Engine forks add features Modelplane vocabulary doesn't ship |
| Custom composition functions | Platform team / community / vendors | Replace Modelplane's matcher / composer with custom placement policy, cost model, or IR adapter — without forking the project |
| Custom Crossplane providers | Platform team / community / vendors | Programmatically create `InferenceCluster` (new cloud) or `InferenceProvider` (new SaaS) from external systems |
| Forking the project | Community / vendors | Needs that don't fit the upstream roadmap; ship a derivative with different defaults, additional CRDs, alternative engine support |

## API shape

`ModelDeployment.spec` field skeleton:

```yaml
model:                            # engine-facing identity
  name
source: HuggingFace | S3 | GCS | PVC
huggingFace: { repo, revision, secretRef }     # paired with source
topology:
  nodes, devicesPerNode
  parallelism: { tensor, pipeline, expert }
  roles:                          # disaggregated serving (xPyD)
    prefill: { nodes, devicesPerNode, parallelism, replicas }   # any unset field inherits root
    decode:  { nodes, devicesPerNode, parallelism, replicas }
requires:
  minVramPerDevice, architecture
  fabric, interNode               # intra-node + inter-node interconnect
  precision
  engineFeatures: [string]
  attributes: { string: string }
  matches.cel                     # break-glass
engine:
  name, image, args
  quantization: { precision, target }
  speculation:
    type: EAGLE | DraftTarget | Medusa | NGram | Lookahead
  advanced: [{ name, config }]    # break-glass
environments, environmentSelector
scaling:
  signal: Concurrency             # v1; Utilization / Both in v2
  concurrency: { minReplicas, maxReplicas, target, window, scaleDownDelay }
adapters: [{ name, source }]      # multi-LoRA + LoRA-aware routing
```

`InferenceCluster.spec.engine.features` and `InferenceProvider.spec.engine.features` declare capabilities; matching is `requires.engineFeatures ⊆ <member>.engine.features`. `ModelDeployment.status.matchTrace` reports per-member compatibility across the fleet with field-level reasons; granular cold-start conditions (`NodesProvisioning | ImagePulling | WeightsDownloading | EngineStarting`) replace catch-all status.

## Capability vocabulary tiers

| Tier | Source | Governance |
|---|---|---|
| Vendor (`gpu.nvidia.com/*`, `gpu.amd.com/*`, `tpu.google.com/*`) | DRA drivers | Consume, never define |
| K8s standards (`resource.kubernetes.io/*`) | WG-Device-Management | Track and alias as KEPs land |
| `modelplane.ai/*` | `CapabilityVocabulary` CR | Updates without CRD bumps; closed enums for stable keys, ordered for hardware, G1–G4 KV tiers |
| User (`acme.example/*`) | User | Pass-through, unvalidated; first-class match via `requires.attributes` |

## Risks (categorized)

**External dependencies — we don't control timing**

| Risk | Mitigation |
|---|---|
| DRA coverage gap (1.30–1.33 BYO clusters; NIM Operator DRA still Tech Preview) | `provisioning.mode` discriminator; emits `ResourceClaim` OR `nvidia.com/gpu` |
| KServe `LLMInferenceService` schema churn (v0.17 args→command; v0.18 storage migration) | `ModelPlacement` IR + version-pinned adapter per KServe minor; conformance test suite |
| Cluster Autoscaler not DRA-aware (pods stuck Pending) | Granular cold-start conditions; v1 falls back to non-autoscaling for DRA-required pools |
| `ResourceSlice` eventual consistency causes drift flapping | Quorum + 5min duration filter |

**Design tradeoffs — our choices**

| Risk | Mitigation |
|---|---|
| Capacity reservation races (KAI #848 class) | Delegate to Kueue `ClusterQueue`; never own the counter |
| Three-autoscaler conflict (KEDA + HPA + WVA) | One autoscaler per replica dimension; v1 is KEDA-only |
| Compound AI multi-deployment co-location | v2: `ModelDeployment.spec.affinity.coLocateWith` |

**Operational boundaries — contract with the cluster**

| Risk | Mitigation |
|---|---|
| CRD ownership conflict with KServe upgrades | `byo-kserve` and `managed-kserve` install modes; never modify CRDs we didn't author |
| Break-glass features no fleet member supports | `matchTrace` field-level failure; `Ready=False NoMatchingEngineFeatures` |

**User experience**

| Risk | Mitigation |
|---|---|
| `ModelDeployment` chunky for ML/App teams | Crossplane Compositions; starter Compositions in `examples/` |

## What ships v1 vs v2 (themed)

**v1 — Foundation**

| Theme | Scope |
|---|---|
| Substrate | Six CRDs (5 user-facing + `ModelPlacement` IR); three-level declared attributes |
| Matching | Three-level cascade; typed `requires`; CEL escape; `matchTrace` |
| Workload API | Self-contained `ModelDeployment`; `topology.roles` (xPyD); `engine.{quantization, speculation, advanced}`; five-factor `scaling`; `adapters` |
| Composition | Matcher → `ModelPlacement` IR → version-pinned KServe adapter; DRA + device-plugin emission |
| Delegation | Kueue for quota; KEDA-only autoscaling on concurrency |
| Fleet routing (v1 subset) | Hardware-heterogeneous + geo + compliance routing via `environmentSelector` and `requires` |
| Status & drift | Granular cold-start conditions; drift detection controller |
| Catalog content | Starter Compositions hand-authored from vLLM recipes |

**v2 — Fleet behaviors and breadth**

| Theme | Scope |
|---|---|
| Fleet routing intelligence | Fleet overflow (#48); active-active / active-passive failover; cost-aware fleet member selection; predictive autoscaling |
| Fleet KV cache federation | G4 networked tier as global cache fabric; LMCache / KVBM integration; fleet-wide prefix-aware routing |
| Fleet session affinity | Sticky sessions across regional ingresses; multi-turn chat lands on the same `(cluster, replica)` or provider endpoint |
| SLO-driven scaling | TTFT/ITL targets; WVA integration; combined Concurrency + Utilization signals |
| Aggregated fleet observability | Roll-up TTFT / ITL / cost / queue depth into one logical service |
| Catalog automation | Auto-import controller for `vllm-project/recipes` |
| Compound AI | Multi-deployment co-location on one cluster |
| Modality expansion | Embedding, ASR, TTS, image, video |
| Standards migration | DRANET / `HyperNode` for inter-node fabric |
| Protocol expansion | `ModelEndpoint` WebSockets / gRPC for non-LLM modalities |

**Beyond v2 (post-roadmap):** an optional intent layer — a `ModelObjective`-style CR above `ModelDeployment` carrying SLO targets (TTFT, ITL, cost ceiling) and reconciled by a planner over the fleet. Mirrors NVIDIA Dynamo's DGDR / DGD pattern. Non-breaking layer; existing users keep writing `ModelDeployment`.

---

## Appendix: deliverables

Full proposed XRDs and example resources live in [`scheduler-deliverables/`](scheduler-deliverables/). That directory is a **design-time preview**: nothing there is wired up yet, and once we align on the API the XRDs move into [`apis/`](../apis/) and examples move into the repo-root `examples/`. See [`scheduler-deliverables/README.md`](scheduler-deliverables/README.md) for the full layout and what's deliberately incomplete.

**XRDs** (proposed CompositeResourceDefinitions):

- [`xrds/inferencecluster.yaml`](scheduler-deliverables/xrds/inferencecluster.yaml) — workload plane, three-level attributes, `provisioning.mode`
- [`xrds/inferenceprovider.yaml`](scheduler-deliverables/xrds/inferenceprovider.yaml) — observed/SaaS endpoint, env-level attributes only
- [`xrds/capabilityvocabulary.yaml`](scheduler-deliverables/xrds/capabilityvocabulary.yaml) — singleton vocabulary CR
- [`xrds/modeldeployment.yaml`](scheduler-deliverables/xrds/modeldeployment.yaml) — self-contained workload spec
- [`xrds/modelendpoint.yaml`](scheduler-deliverables/xrds/modelendpoint.yaml) — weighted routing (per #60)
- [`xrds/modelplacement.yaml`](scheduler-deliverables/xrds/modelplacement.yaml) — the IR; one per matched fleet member

**Substrate examples** (platform-team setup):

- [`examples/inferencecluster-prod-coreweave.yaml`](scheduler-deliverables/examples/inferencecluster-prod-coreweave.yaml) — production Coreweave H200 cluster, DRA-enabled
- [`examples/inferenceprovider-together.yaml`](scheduler-deliverables/examples/inferenceprovider-together.yaml) — Together AI registered as a fleet member
- [`examples/capabilityvocabulary-default.yaml`](scheduler-deliverables/examples/capabilityvocabulary-default.yaml) — the default vocabulary Modelplane installs

**Workload examples** (ML/App team deployments):

- [`examples/kimi-k2.yaml`](scheduler-deliverables/examples/kimi-k2.yaml) — frontier MoE, multi-node, 5P3D disaggregation, FP8 weights + KV
- [`examples/qwen3-coder.yaml`](scheduler-deliverables/examples/qwen3-coder.yaml) — code completion, n-gram speculation, 3 LoRA adapters, 256K context
- [`examples/gpt-oss-20b.yaml`](scheduler-deliverables/examples/gpt-oss-20b.yaml) — small MoE, MXFP4-native on Blackwell, scale-to-zero, fan-out across 2 regions
- [`examples/assistant-endpoint.yaml`](scheduler-deliverables/examples/assistant-endpoint.yaml) — `ModelEndpoint` weighted across all three (70 / 25 / 5)
