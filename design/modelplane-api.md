# Modelplane API Design — 1-pager

**Status:** Draft for Bassam review
**Author:** Dennis Ramdass
**Date:** 2026-05-07
**Scope:** API + scheduler + capability model. Heaviest on the scheduler/matching surface; touches the broader CRD shape and the adapter / plugin pattern that ties them together.

## TL;DR

- **Modelplane is a Crossplane-native, multi-cloud inference control plane.**
- **Cluster scope** holds substrate: `InferenceCluster`s (customer K8s) and the `InferenceClass` catalog (per-SKU hardware bundles, StorageClass-style). **Namespace scope** is the lifecycle boundary: `ModelDeployment`, `ModelPlacement`, `ModelService`, `ModelEndpoint`.
- **Replica == placement.** One `ModelPlacement` per logical replica of a `ModelDeployment`. KEDA writes `MD.spec.replicas` via the K8s scale subresource; the composer reconciles MPs to match — no custom scaler.
- **Two-stage scheduling.** Modelplane is a *federation planner* — it evaluates predicates against *declared* pool capacity to pick `(cluster, pool)` per replica, before nodes exist. Per-cluster scheduling (and runtime grounding via DRA, where the cluster has it) is delegated. We borrow DRA's *vocabulary* (typed attributes, domain-prefixed keys, CEL) but not its Kinds — `ResourceClaim` / `ResourceSlice` / `DeviceClass` belong to the runtime allocator, not the federation layer.
- **Labels-first matching.** `deviceSelector.matchLabels` works on any K8s cluster with labeled nodes (no DRA needed). Typed `matchAttributes` + CEL is the break-glass for richer constraints (NVLink-domain co-location, MIG, FP8 capability) — evaluated against declared pool attributes, not runtime `ResourceSlice`s.
- **Adapter / plugin substrate.** `managed-kserve` (backend) + `managed-kueue` (scheduler) ship by default. BYO contracts (`InferenceCluster.spec.{backend, scheduler}.type`) plug in KAI / Volcano / Dynamo / raw-vllm. `ModelPlacement` (the IR) is the seam.
- **`ModelService` is a rough sketch.** Routing-only placeholder, never a placement target. **Nic owns the real design** for dedicated-SaaS placement (provisioning a Together / Baseten dedicated endpoint); the `ModelService` shape here is a stand-in pending that work.
- **`InferenceClass` catalog as the wedge.** Default ships per-SKU hardware bundles (`h100-nvl-8x`, `b200-nvl-8x`, `mi300x-8x`, ...) — StorageClass-style, cluster-scoped. Customers author their own for bespoke hardware. Engine features live separately: derivation rules in matcher code, per-cluster supported set on `KServeBackend.spec.engine.features`, break-glass via `engine.advanced[]`. Keeping the class catalog current is high-leverage and bounded — Upbound-managed-offering candidate.
- **Wedge:** fleet-level capabilities single-cluster platforms can't reach — fleet matching, geo + compliance routing, KV cache federation, sticky sessions, failover, cost-aware routing.

## Design principles

1. **Clean separation, no enforcement.** Platform teams own substrate; ML/App teams own workloads. Same API split or unified.
2. **Fleet-wide by construction.** A `ModelDeployment` targets the fleet of `InferenceCluster`s, not a single cluster. `matchTrace` reports where it fits and why elsewhere doesn't. SaaS endpoints participate via `ModelEndpoint` routing, not placement.
3. **Plain Crossplane customization.** Catalogs, defaults, governance live in Compositions, RBAC, OPA — not Modelplane primitives.
4. **No new in-cluster scheduler.** We're a meta-scheduler. K8s scheduler + DRA, Kueue, KEDA/HPA, Cluster Autoscaler each own their layer.

## Architecture: control plane + fleet

A diagram of the API and an example fleet topology lives in [`proposed-modelplane-api/diagram.excalidraw`](proposed-modelplane-api/diagram.excalidraw) — Bassam's whiteboard.


```
            Modelplane Control Plane (Crossplane)
   matcher: (cluster, pool) per replica → ModelPlacement (IR)
   backend adapter: ModelPlacement → upstream objects per cluster
                          ↓
   ─────────────── cluster scope ────────────────
   InferenceClusters (workload planes)
     scheduler (managed-kueue default) + backend (managed-kserve default)
     + DRA + KEDA on each cluster
   InferenceClass catalog (per-SKU bundles, cluster-scoped)

   ─────────────── namespace scope (= environment) ──
   per namespace: prod / staging / dev / team-A …
     ModelDeployment(s)    (workload spec; scale subresource)
     ModelPlacement(s)     (one per replica, IR)
     ModelService(s)       (routing-only target)
     ModelEndpoint         (weighted routing across MDs + MSes)
```

**Cluster scope** holds shared substrate: `InferenceCluster`s and the `InferenceClass` catalog. **Namespace scope** is the lifecycle boundary — each namespace is an environment (prod / staging / dev / per-team) holding workload, routing, and SaaS-target resources. The matcher considers only `InferenceCluster` candidates; `ModelService` is routing-only.

**Key architectural decisions:**

- Meta-scheduler only — compose objects, never bind devices or actuate replicas. `ClusterModel` / `Model` deleted; workload spec self-contained on `ModelDeployment`.
- **Replica == placement.** One `ModelPlacement` per logical replica. Each replica independently scheduled by the matcher against the MD's `clusterSelector`. KEDA writes `MD.spec.replicas` via the scale subresource; the composer reconciles MPs to match. No custom scaler.
- **Federation matches against declared pool attributes, not runtime DRA.** `InferenceCluster.spec.nodePools[].{node,device}Attributes` are the source of truth at the federation layer. DRA `ResourceSlice`s ground predicates at the per-cluster scheduling stage (next section).
- **Two-level selector cascade**: `clusterSelector` (env-level) → `deviceSelector` (node + device). Labels are the primary path; typed `matchAttributes` + CEL is the break-glass.
- **In-cluster scheduling delegated.** Bin-packing, gang scheduling, fractional GPU, NVLink-aware placement, capacity tracking — Kueue (default) / KAI / Volcano. Modelplane reads capacity signal back via `ClusterQueue.status.flavorsUsage[]`.
- `ModelPlacement` (existing CRD, `apis/modelplacements/`) is the **intermediate representation (IR)** — the seam between the matcher and the version-pinned backend adapter. Not a new abstraction; the role this existing CRD plays.
- Namespace = environment / lifecycle scope. Pushing a revision triggers lifecycle reconciliation in that namespace.

## Adapter / plugin substrate

Two layers are pluggable: the in-cluster scheduler and the inference backend. Default plugins ship; BYO via a contract on `InferenceCluster.spec`. `ModelPlacement` (the IR) is the seam.

| Layer | Default | BYO | Seam |
|---|---|---|---|
| In-cluster scheduler | **`managed-kueue`** (installs Kueue + `ClusterQueue` per pool) | `scheduler.type: kueue \| kai \| volcano \| none` | Admission CR (Kueue `Workload`, KAI/Volcano `PodGroup`) + capacity status (`ClusterQueue.status.flavorsUsage[]` or equivalent). |
| Inference backend | **`managed-kserve`** (installs KServe at pinned version + composes `KServeBackend`) | `backend.{type, version}: kserve \| dynamo \| raw-vllm` | Per-cluster adapter watches `ModelPlacement` and renders `LLMInferenceService` (KServe), `DynamoGraphDeployment` (Dynamo), or `Deployment+Service` (raw-vllm). Writes back to `ModelPlacement.status.rendered`. |

**Opinionated:** the IR schema, the matching logic, and the user-facing API. **Pluggable:** thin adapters per scheduler + per backend version. v1 ships Kueue + KServe (v0.16 / v0.17 / v0.18); KAI / Volcano / Dynamo are follow-ups. Customers keep existing investments (KAI for training, Volcano for batch, Dynamo for orchestration); Modelplane adds the fleet layer above.

## Two-stage scheduling: federation vs in-cluster

Modelplane and DRA solve different problems. DRA is a *runtime allocator* — drivers publish `ResourceSlice`s about real hardware, the K8s scheduler matches `ResourceClaim`s against them. Modelplane's federation layer schedules against *declared* pool capacity, before nodes exist. Planning, not allocation.

We borrow DRA's vocabulary, not its Kinds:

| Borrow (vocabulary) | Drop (Kinds) |
|---|---|
| Typed attributes, domain-prefixed keys, CEL predicates, `device.attributes[domain].name` access pattern | `DeviceClass` / `ResourceSlice` / `ResourceClaim` at the federation layer |

**Two stages, in order:**

1. **Federation match** (Modelplane control plane, pre-provisioning). `clusterSelector` filters `InferenceCluster` candidates; `deviceSelector` filters node pools within them. Output: `ModelPlacement` per replica, pinned to a `(cluster, pool)`. Fleet-aware, capacity-aware, cost-aware. Runs whether or not the cluster has DRA.
2. **In-cluster scheduling** (per-cluster, at pod admission). The backend adapter renders pods. *Optionally*, the adapter emits real `ResourceClaim`s carrying the same CEL predicates from `deviceSelector`; the DRA driver grounds them against actual hardware. Mismatch → pod Pending with a clear error.

**Grounding contract** (when the adapter emits `ResourceClaim`s):

- **BYOC + DRA**: emit. Pool attrs declared by the customer; independent grounding catches typos and drift.
- **Modelplane-provisioned**: optional. Attrs are derived from the SKU we asked for — drift is unlikely.
- **BYOC without DRA**: skip. Pod constraints fall back to `nodeSelector` + `topologySpreadConstraints`. Federation match still uses the same predicates.

User-facing API is unchanged across these. The decision is per-cluster, governed by `InferenceCluster.spec.provisioning.mode`.

## Fleet-level capabilities

Single-cluster platforms (llm-d, KServe alone, Dynamo) optimize within a cluster. Modelplane reaches across `InferenceCluster`s, with SaaS via `ModelService` routes.

| Capability | What it does |
|---|---|
| Fleet matching | One `ModelDeployment` finds eligible clusters across regions, clouds, vendors; `matchTrace` shows why each fits or doesn't |
| Hardware-heterogeneous routing | One `ModelEndpoint` weighting across MDs on different hardware, plus `ModelService` routes for SaaS spillover |
| Geo + compliance routing | EU traffic to EU clusters; SOC 2 traffic only to certified clusters — via `clusterSelector` predicates |
| Cross-cluster replica scaling | Replicas of one MD spread across matching clusters; matcher picks per replica from capacity signal |
| Fleet KV cache federation | G4 networked cache as a global fabric; route to whichever cluster has the prefix |
| Fleet session affinity | Sticky sessions across regional ingresses; multi-turn chat lands on the same `(cluster, replica)` |
| Fleet failover | Active-active / active-passive cutover when a cluster degrades |
| Cost-aware routing | Cheapest fleet member that fits; blend reserved / on-demand / spot / per-token |
| Fleet overflow | Burst to a sibling cluster or `ModelService` when local capacity exhausts (#48) |
| Aggregated fleet observability | TTFT / ITL / cost / queue-depth rolled up across the fleet for one logical service |

What ships in v1 vs v2 is in the project plan section.

## How users consume it

**ML/App day-one.** Write a `ModelDeployment` (or instantiate a platform Composition like `ApprovedModel` that generates one). Matcher picks an `InferenceCluster` and emits one `ModelPlacement` per replica; the version-pinned adapter renders each MP to one upstream pod set. KEDA writes `MD.spec.replicas`; composer reconciles MPs. Endpoint reachable via `ModelEndpoint`. `matchTrace` shows what was considered and why excluded.

**Platform day-one.** Install Modelplane on the Crossplane control plane → install (or BYO) workload-plane substrate per cluster → create one `InferenceCluster` per cluster (or copy from `examples/reference-clusters/`, each pool referencing an `InferenceClass`) → create `ModelService`s for any SaaS endpoints → optionally author bespoke `InferenceClass`es and Compositions.

## Extensibility points

| Extension | Owner | Why use it |
|---|---|---|
| Crossplane Compositions over `ModelDeployment` / substrate CRDs | Platform team | Org abstractions (`ApprovedModel`, `ProductionCluster`), approved-model catalogs, defaults, governance |
| Custom `InferenceClass` | Platform team | Bespoke hardware (custom AMD partitions, internal accelerators) — author a class with the right `expands`, point pools at it |
| `engine.args` opaque pass-through | ML/App team | Engine flags Modelplane doesn't model |
| `engine.advanced[]` named feature break-glass | ML/App team + platform team (declares on `KServeBackend`) | Novel features not yet in `engine.optimizations` — promote to typed over time |
| `<level>Selector.matchAttributes` / `cel` | Both teams | Org-specific match dimensions (cost center, team, clearance) and constraints not expressible in `matchLabels` |
| Custom composition functions | Platform / community / vendors | Replace matcher / composer / adapter with custom placement policy, cost model, or backend |
| Custom Crossplane providers | Platform / community / vendors | Programmatic creation of `InferenceCluster` (new cloud) or `ModelService` (new SaaS) |
| Forking | Community / vendors | Needs that don't fit the upstream roadmap |

## API shape

`ModelDeployment.spec` field skeleton (namespace-scoped; carries the K8s scale subresource so KEDA targets it directly):

```yaml
replicas: <int>                   # KEDA writes here; composer reconciles MPs to match
model: { name }                   # engine-facing identity
source: HuggingFace | S3 | GCS | PVC
huggingFace: { repo, revision, secretRef }

# Two-level selector cascade, filters InferenceCluster only:
clusterSelector:                  # env-level attrs (region, tier, compliance)
  matchLabels: {...}
  matchAttributes: [...]
  cel: ...
deviceSelector:                   # node + device attrs over declared pool capacity
  requests:
    - name, count, perNode
      matchLabels: {...}          # primary path (no DRA needed)
      matchAttributes: [...]      # break-glass; typed attribute predicates
      cel: ...
      constraints: [{ matchAttribute, requests }]   # NVLink-domain co-location, etc
# Deployment shape (Modelplane-canonical, backend adapter translates):
parallelism: { tensor, pipeline, expert }
roles:                            # disaggregated serving (xPyD)
  prefill: { deviceSelector, parallelism, replicas }   # any unset inherits root
  decode:  { deviceSelector, parallelism, replicas }

engine:
  name, image, args
  quantization: { precision, target }
  speculation:
    type: EAGLE | DraftTarget | Medusa | NGram | Lookahead
  optimizations: { chunkedPrefill, prefixCaching, kvCacheRouting }   # typed knobs
  advanced: [{ name, config }]    # named break-glass — promote to optimizations over time

scaling:                          # composer turns this into a stock KEDA ScaledObject
  signal: Concurrency | Utilization | Both
  concurrency: { minReplicas, maxReplicas, target, window, scaleDownDelay }
adapters: [{ name, source }]      # multi-LoRA + LoRA-aware routing
```

**Replica == placement.** N replicas → N `ModelPlacement`s, each scheduled independently. Multi-node logical replicas (Kimi K2 PP=2) are still ONE MP — multi-pod via LWS within one cluster. Multi-region spread = multiple MDs + multiple `ModelEndpoint` route entries.

**`ModelService` is a sketch.** Routing-only target on `ModelEndpoint`; never a placement target. **Nic owns the dedicated-SaaS placement design** — provisioning a Together / Baseten dedicated endpoint is a separate concept; this CR is a placeholder pending that work.

**Namespace = environment.** 0..N of each user-facing resource (`ModelEndpoint`, `ModelDeployment`, `ModelService`, `ModelPlacement`) per namespace. Pushing an MD revision triggers lifecycle reconciliation there. `InferenceClass`es are cluster-scoped — shared infrastructure-level catalog.

**Consumer-index discipline.** Every field on the user-facing API has at least one named consumer (matcher / composer / backend adapter / gateway), spelled out in a `Field-level consumer index` block at the top of each XRD. No consumer → no field. The matcher derives the required-feature set from declared config (`roles` → disagg, `engine.optimizations.*` → typed knobs, `adapters[]` → multi-lora) and unions it; the user declares what they want, not which features that needs.

## Hardware taxonomy & InferenceClass

Grounded in Bassam's "GPU hardware survey and unified taxonomy" (2026-05-07). Four logical layers organized by what changes together:

| Layer | What it describes | Examples |
|---|---|---|
| **Cluster** | facts about the whole environment | `cloud.provider`, `cloud.region`, `network.fabric`, `network.bandwidthGbps`, `network.airgapped`, `cluster.scaleUnit` |
| **Pool** | per-host shape (per `nodePool`) | `cloud.instanceType`, `gpuCount`, `interconnect.{type, bandwidthGBs}`, `cpu.{vendor, cores, platform}`, `memoryGiB`, `nics.{count, bandwidthGbps}`, `host.virtualization` |
| **Device** | per-GPU attributes | `vendor`, `product`, `architecture`, `formFactor`, `vramGiB`, `mig`, `capabilities` (set), `parentProduct` (for fractional / MIG) |
| Dynamic state | runtime (health, allocation, MIG state) | tracked separately, not part of the vocab |

Load-bearing design choices:

- **Capability sets, not boolean columns.** `capabilities: [fp8, fp4, mig, transformer-engine]` ages better than separate flags — new formats are entries, not a schema migration.
- **Predicates over equality.** `vramGiB >= 141` matches H200/B200/B300/MI300X; equality only matches H200.
- **Architecture is metadata; capabilities do the matching work.** Hardcoding `architecture in [hopper, blackwell]` excludes AMD MI300X. Capability flags are the durable expression.
- **Rack-scale: `cluster.scaleUnit`.** `independent-nodes` for normal cloud SKUs; `nvl72` for GB200/GB300 (72 GPUs in one NVLink domain); `superpod` for DGX SuperPOD.
- **RoCE vs IB are distinct fabrics.** Same physical NICs (ConnectX) can run either protocol — OCI runs RoCE on Quantum-2 hardware AWS runs as native IB.

**`InferenceClass` — StorageClass-style hardware bundles.** A per-class, cluster-scoped CR that names a hardware shape and the typed attributes it implies. `InferenceCluster.spec.nodePools[].class` references one; the matcher inherits `class.expands` into the pool's effective attributes. Per-cloud SKU strings (`aws:p5.48xlarge`) resolve to a class via `class.aliases[]`.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceClass
metadata:
  name: h100-nvl-8x
spec:
  expands:
    vendor: nvidia
    product: H100
    architecture: hopper
    formFactor: sxm
    vramGiB: 80
    mig: true
    capabilities: [fp8, transformer-engine, mig]
    gpuCount: 8
    interconnect.type: nvswitch
    interconnect.bandwidthGBs: 900
  aliases:
    - aws:p5.48xlarge
    - gcp:a3-megagpu-8g
    - oci:BM.GPU.H100.8
    - coreweave:gd-8xh100ib-i128
    - dgx:DGX-H100
```

Workloads match against attributes (e.g. `capabilities contains fp8 && vramGiB >= 141`) — same predicate engine whether the attribute came from a class or was declared inline. Modelplane ships a default catalog under [`examples/inferenceclasses/`](proposed-modelplane-api/examples/inferenceclasses/); customers author their own for bespoke hardware. **Decision after 1:1 with Nic** — aligns with K8s class patterns (StorageClass / IngressClass / DeviceClass). The earlier `CapabilityVocabulary` singleton conflated three jobs (ontology + macros + features) into one CR; decomposing it gives each piece its natural home.

**Reference InferenceClusters from cloud SKUs.** Pre-generated `InferenceCluster` definitions live under [`examples/reference-clusters/`](proposed-modelplane-api/examples/reference-clusters/) — AWS p5, GKE A3 Mega, OCI MI300X, CoreWeave GB300 NVL72, EKS H100 (no-DRA path). Each `nodePools[].class` references an `InferenceClass`; inline overrides carry the per-cluster host shape (cpu, memory, nics, the provider SKU string). Static today; the follow-up is a Crossplane provider that polls cloud SKU APIs and generates these programmatically.

**Commercial-offering framing.** The canonical-catalog work is the wedge:

- **`InferenceClass` catalog tracking** — chip families, per-cloud SKU mappings. Bounded, ongoing, high-leverage.
- **Reference clusters** kept current across NVIDIA / AMD / TPU / Trainium / Maia × AWS / GCP / Azure / OCI / CoreWeave / Crusoe / Lambda / Nebius / on-prem-DGX.
- **Continuous testing & benchmarking** — each reference cluster paired with a tested, benchmarked workload across every supported model family. Costly to maintain; what customers actually pay for. Natural fit for an Upbound-managed offering above the OSS default.

## Engine features

Workloads imply required engine features through declared config; clusters declare what their backend supports; the matcher unions the implied set, adds anything from the user's `engine.advanced[]` break-glass, and filters candidates accordingly.

- **Derivation rules live with the matcher** (versioned with Modelplane releases). Examples: `roles` present → `prefill-decode-disagg`; `engine.optimizations.kvCacheRouting: true` → `kv-cache-routing`; `adapters[]` non-empty → `multi-lora`; `engine.quantization.target` contains `kvCache` → `fp8-kv-cache`.
- **Cluster-side declaration is `KServeBackend.spec.engine.features`** — per-cluster, per-backend-version. Single source of truth for what a cluster can serve.
- **Break-glass is `ModelDeployment.spec.engine.advanced[]`** — a typed-name list. Each entry's `name` is unioned into the required-feature set verbatim. Custom features (`acme.com/turbo-mode`) work without any catalog registration. Promote frequently-used names to typed `engine.optimizations` over time.
- **Misses are explained.** When no cluster matches, `status.matchTrace` carries `requiredFeatures.{derived, explicit}`, per-cluster `missingFeatures`, and `suggestions` (fuzzy-matched against the matcher's well-known list — catches typos like `chunked-prfill` → `chunked-prefill`). User sees exactly which features failed and where.

There's no `EngineCatalog` CR — the canonical feature list is matcher code + `docs/engine-features.md`, the per-cluster supported set is `KServeBackend`, and break-glass needs no registration.

**Vocabulary tiers (where keys come from):**

| Tier | Source | Governance |
|---|---|---|
| Vendor (`gpu.nvidia.com/*`, `gpu.amd.com/*`, `tpu.google.com/*`) | DRA drivers | Consume, never define |
| K8s standards (`resource.kubernetes.io/*`) | WG-Device-Management | Track and alias as KEPs land |
| Modelplane (`vendor`, `product`, `vramGiB`, `capabilities`, `cloud.region`, `network.fabric`, ...) | Conventions in matcher code + docs | Updated with Modelplane releases |
| User (`acme.example/*`) | User | Pass-through, unvalidated; first-class via `<level>Selector.matchAttributes` |

## Risks (categorized)

**External dependencies — we don't control timing**

| Risk | Mitigation |
|---|---|
| DRA coverage gap (1.30–1.33 BYO clusters; NIM Operator DRA still Tech Preview) | `provisioning.mode` discriminator; emits `ResourceClaim` OR `nvidia.com/gpu` |
| KServe `LLMInferenceService` schema churn (v0.17 args→command; v0.18 storage migration) | `ModelPlacement` IR + version-pinned adapter per KServe minor; conformance test suite |
| Cluster Autoscaler not DRA-aware (pods stuck Pending) | Granular cold-start conditions; DRA-required pools fall back to non-autoscaling until autoscaler maturity catches up |
| `ResourceSlice` eventual consistency causes drift flapping | Quorum + 5min duration filter |

**Design tradeoffs — our choices**

| Risk | Mitigation |
|---|---|
| Capacity reservation races (KAI #848 class) | Delegate to Kueue `ClusterQueue`; never own the counter |
| Three-autoscaler conflict (KEDA + HPA + WVA) | One autoscaler per replica dimension; KEDA-only initially, WVA layered later |
| Compound AI multi-deployment co-location | Future: `ModelDeployment.spec.affinity.coLocateWith` |

**Operational boundaries — contract with the cluster**

| Risk | Mitigation |
|---|---|
| CRD ownership conflict with KServe upgrades | `kserve` (BYO) and `managed-kserve` install modes; never modify CRDs we didn't author |
| Break-glass features no fleet member supports | `matchTrace` field-level failure; `Ready=False NoMatchingEngineFeatures` |

**User experience**

| Risk | Mitigation |
|---|---|
| `ModelDeployment` chunky for ML/App teams | Crossplane Compositions; starter Compositions in `examples/` |

## Open questions

Decisions made and the alternatives Nic can override:

| Decision | Lean | Alternatives |
|---|---|---|
| Default scheduler + backend | `managed-kueue` + `managed-kserve` as defaults, BYO first-class | No defaults (force pick); KAI default for NVIDIA-shop bias |
| Selector dual-path | `matchLabels` (primary) + `matchAttributes` / CEL (break-glass) | Labels only (simpler); attributes only (richer) |
| DRA grounding | Mandatory for BYOC w/ DRA, optional for Modelplane-provisioned | Always-on; never (federation-only) |
| Rack-scale (NVL72) | Env-level attribute (`cluster.scaleUnit: nvl72`); rack-spanning placements treat the rack as one `nodePool` | Separate `RackInferenceCluster` kind; multi-pool model |
| Reference-cluster rollout | Static YAML now → Crossplane provider polling SKU APIs later | Provider-first; never (operator hand-authors) |
| Hardware ontology | `InferenceClass` per-class CR (StorageClass-style); engine features in matcher code + `KServeBackend` | Singleton `CapabilityVocabulary` (earlier proposal — dropped after 1:1 with Nic); strings in code only (no class CR) |
| `ModelObjective`-style intent layer | Punt past v2 — non-breaking layer above MD if/when needed | Ship in v1 (mirrors Dynamo DGDR/DGD); never |

Nic-owned (not for this PR):

- **Dedicated-SaaS placement.** `ModelService` here is routing-only and a sketch pending Nic's design for provisioning a dedicated Together / Baseten endpoint.

## What ships v1 vs v2 (themed)

**v1 — Foundation**

| Theme | Scope |
|---|---|
| Substrate | 5 user-facing CRDs (`InferenceCluster`, `InferenceClass`, `ModelDeployment`, `ModelEndpoint`, `ModelService`) + `ModelPlacement` IR; cluster + pool + device attribute layers on `InferenceCluster`; `InferenceClass` catalog (per-SKU bundles); `managed-kueue` install |
| Matching | Two-level selector cascade (`clusterSelector` + `deviceSelector`) over declared pool attributes; typed `matchAttributes` + CEL escape; `matchTrace`; optional DRA grounding for BYOC |
| Workload API | Self-contained `ModelDeployment`; replica == placement (`spec.replicas` + scale subresource); `roles.{prefill, decode}` for xPyD disaggregation; `engine.{quantization, speculation, advanced}`; five-factor `scaling`; `adapters` |
| Composition | Matcher → `ModelPlacement` IR → version-pinned KServe adapter; DRA + device-plugin emission |
| Delegation | Kueue for quota; KEDA-only autoscaling on concurrency |
| Fleet routing | Hardware-heterogeneous + geo + compliance routing via `clusterSelector` and `deviceSelector`; multi-region spread via multiple `ModelDeployment`s + `ModelEndpoint` |
| Status & drift | Granular cold-start conditions; drift detection controller |
| Catalog content | Starter Compositions hand-authored from vLLM recipes |

**v2 — Fleet behaviors and breadth**

| Theme | Scope |
|---|---|
| Fleet routing intelligence | Fleet overflow (#48); active-active / active-passive failover; cost-aware fleet member selection; predictive autoscaling |
| Fleet KV cache federation | G4 networked tier as global cache fabric; LMCache / KVBM integration; fleet-wide prefix-aware routing |
| Fleet session affinity | Sticky sessions across regional ingresses; multi-turn chat lands on the same `(cluster, replica)` |
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

Full proposed XRDs and example resources live in [`proposed-modelplane-api/`](proposed-modelplane-api/). The directory is a **design-time preview**: nothing there is wired up yet — XRDs aren't installed by `up` packs, examples aren't run by CI. Once we align on the API, XRDs move into [`apis/`](../apis/) (one directory per CRD, alongside the matching Composition) and examples move into the repo-root `examples/`.

**XRDs** (proposed CompositeResourceDefinitions):

- [`xrds/inferencecluster.yaml`](proposed-modelplane-api/xrds/inferencecluster.yaml) — cluster-scoped substrate; `nodePools[].class` references an `InferenceClass`
- [`xrds/inferenceclass.yaml`](proposed-modelplane-api/xrds/inferenceclass.yaml) — cluster-scoped hardware-bundle class (StorageClass-style); per-SKU
- [`xrds/modelservice.yaml`](proposed-modelplane-api/xrds/modelservice.yaml) — namespace-scoped routing-only target (rough sketch — Nic owns the dedicated-SaaS placement concept)
- [`xrds/modeldeployment.yaml`](proposed-modelplane-api/xrds/modeldeployment.yaml) — namespace-scoped workload, K8s scale subresource for KEDA, structured `status.matchTrace`
- [`xrds/modelendpoint.yaml`](proposed-modelplane-api/xrds/modelendpoint.yaml) — namespace-scoped weighted routing across `Deployment` / `ModelService` / `External`
- [`xrds/modelplacement.yaml`](proposed-modelplane-api/xrds/modelplacement.yaml) — existing CRD playing the role of the intermediate representation (IR); one per logical replica (replica == placement)

**Substrate examples** (platform-team setup):

- [`examples/inferencecluster-prod-coreweave.yaml`](proposed-modelplane-api/examples/inferencecluster-prod-coreweave.yaml) — production Coreweave H200 cluster; BYO `kueue` + BYO `kserve@v0.18.0`; pool references `h200-nvl-8x` class
- [`examples/modelservice-together.yaml`](proposed-modelplane-api/examples/modelservice-together.yaml) — Together AI as a routing target
- [`examples/inferenceclasses/`](proposed-modelplane-api/examples/inferenceclasses/) — default `InferenceClass` catalog (one per SKU: H100/H200/B200/B300/MI300X/L40S/A100, in 8x and Grace-4x forms)
- [`examples/reference-clusters/`](proposed-modelplane-api/examples/reference-clusters/) — pre-generated `InferenceCluster` definitions per SKU, each `nodePools[].class` references an `InferenceClass`: AWS p5 (DRA), GKE A3 Mega (DRA), OCI MI300X (DRA + AMD), CoreWeave GB300 NVL72 (rack-scale), EKS H100 no-DRA (labels-first BYOC). Anchors the managed-catalog commercial offering.

**Workload examples** (ML/App team deployments):

- [`examples/kimi-k2.yaml`](proposed-modelplane-api/examples/kimi-k2.yaml) — frontier MoE, multi-node (2× 8 H200), 5P3D disaggregation, FP8 weights + KV; typed-attribute predicates (`vramGiB >= 141`, `capabilities: [fp8]`, `interconnect.type: nvswitch`)
- [`examples/kimi-k2-eu.yaml`](proposed-modelplane-api/examples/kimi-k2-eu.yaml) — EU-region sibling; multi-region pattern (one MD per region, pinned via `cloud.region`)
- [`examples/qwen3-coder.yaml`](proposed-modelplane-api/examples/qwen3-coder.yaml) — code completion, n-gram speculation, 3 LoRA adapters, 256K context; user-defined `acme.example/*` attributes for team affinity
- [`examples/gpt-oss-20b.yaml`](proposed-modelplane-api/examples/gpt-oss-20b.yaml) — small MoE, scale-to-zero; labels-first match path (NVIDIA GPU operator's `nvidia.com/gpu.family` node label)
- [`examples/assistant-endpoint.yaml`](proposed-modelplane-api/examples/assistant-endpoint.yaml) — `ModelEndpoint` weighted across the three deployments + Together routing
- [`examples/multi-region-endpoint.yaml`](proposed-modelplane-api/examples/multi-region-endpoint.yaml) — `ModelEndpoint` routing Kimi K2 across us-east + eu-west MDs with Together spillover

**What's deliberately incomplete** (will be filled in during the move to `apis/`):

- `status` schemas are minimal — just conditions + a representative status field per resource. `matchTrace`, `compatibility`, and granular cold-start status will be elaborated when the controller code lands.
- Validation rules (CEL on the schema, `oneOf` discriminator constraints, cross-field invariants) are sketched but not exhaustive.
- The corresponding Crossplane Compositions are not in this directory — those are implementation. The XRDs declare the API contract.
- `KServeBackend` (already an internal XR in `apis/kservebackends/`) is not duplicated here, but it's where engine + features land in the substrate / runtime split.
- Dedicated-SaaS placement (Nic-owned) is intentionally absent. `ModelService` is rough-sketch routing-only.

**Where each XRD lands after alignment:**

| File here | Lands in |
|---|---|
| `xrds/inferencecluster.yaml` | `apis/inferenceclusters/definition.yaml` (replacing `apis/inferenceenvironments/`) |
| `xrds/inferenceclass.yaml` | `apis/inferenceclasses/definition.yaml` |
| `xrds/modelservice.yaml` | `apis/modelservices/definition.yaml` |
| `xrds/modeldeployment.yaml` | `apis/modeldeployments/definition.yaml` (expanded) |
| `xrds/modelendpoint.yaml` | `apis/modelendpoints/definition.yaml` |
| `xrds/modelplacement.yaml` | `apis/modelplacements/definition.yaml` (expanded as the IR) |
| `examples/*.yaml` | `examples/` at repo root, or `examples/compositions/` for platform-team starters |
