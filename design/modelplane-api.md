# Modelplane API Design — 1-pager

**Status:** Draft for Bassam review
**Author:** Dennis Ramdass
**Date:** 2026-05-07
**Scope:** API + scheduler + capability model. Heaviest on the scheduler/matching surface; touches the broader CRD shape and the adapter / plugin pattern that ties them together.

## TL;DR

- **Modelplane is a Crossplane-native, multi-cloud inference control plane.**
- **Cluster scope** holds substrate (`InferenceCluster`s — customer K8s — plus a singleton `CapabilityVocabulary`). **Namespace scope** is the lifecycle boundary (= environment): `ModelDeployment`, `ModelPlacement`, `ModelService`, `ModelEndpoint`.
- **Replica == placement.** One `ModelPlacement` per logical replica of a `ModelDeployment`. KEDA writes `MD.spec.replicas` via the K8s scale subresource; the composer reconciles MPs to match — no custom scaler.
- **Two-stage scheduling.** Modelplane is a *federation planner* — it evaluates predicates against *declared* pool capacity to pick `(cluster, pool)` per replica, before nodes exist. Per-cluster scheduling (and runtime grounding via DRA, where the cluster has it) is delegated. We borrow DRA's *vocabulary* (typed attributes, domain-prefixed keys, CEL) but not its Kinds — `ResourceClaim` / `ResourceSlice` / `DeviceClass` belong to the runtime allocator, not the federation layer.
- **Labels-first matching.** `deviceSelector.matchLabels` works on any K8s cluster with labeled nodes (no DRA needed). Typed `matchAttributes` + CEL is the break-glass for richer constraints (NVLink-domain co-location, MIG, FP8 capability) — evaluated against declared pool attributes, not runtime `ResourceSlice`s.
- **Adapter / plugin substrate.** `managed-kserve` (backend) + `managed-kueue` (scheduler) ship by default. BYO contracts (`InferenceCluster.spec.{backend, scheduler}.type`) plug in KAI / Volcano / Dynamo / raw-vllm. `ModelPlacement` (the IR) is the seam.
- **`ModelService` is a rough sketch.** Routing-only placeholder, never a placement target. **Nic owns the real design** for dedicated-SaaS placement (provisioning a Together / Baseten dedicated endpoint); the `ModelService` shape here is a stand-in pending that work.
- **CapabilityVocabulary as a managed canonical catalog.** Default ships chip generations, engine versions, quantization formats, KV tiers, fabric ordering, plus per-SKU instance-type macros. Customers override for bespoke hardware. Keeping it current is high-leverage and bounded — Upbound-managed-offering candidate.
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
   CapabilityVocabulary (singleton, name: default)

   ─────────────── namespace scope (= environment) ──
   per namespace: prod / staging / dev / team-A …
     ModelDeployment(s)    (workload spec; scale subresource)
     ModelPlacement(s)     (one per replica, IR)
     ModelService(s)       (routing-only target)
     ModelEndpoint         (weighted routing across MDs + MSes)
```

**Cluster scope** holds shared substrate (`InferenceCluster`s + the singleton `CapabilityVocabulary`). **Namespace scope** is the lifecycle boundary — each namespace is an environment (prod / staging / dev / per-team) holding workload, routing, and SaaS-target resources. The matcher considers only `InferenceCluster` candidates; `ModelService` is routing-only.

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

**Platform day-one.** Install Modelplane on the Crossplane control plane → install (or BYO) workload-plane substrate per cluster → create one `InferenceCluster` per cluster (or copy from `examples/reference-clusters/`) → create `ModelService`s for any SaaS endpoints → optionally extend `CapabilityVocabulary` and author Compositions.

## Extensibility points

| Extension | Owner | Why use it |
|---|---|---|
| Crossplane Compositions over `ModelDeployment` / substrate CRDs | Platform team | Org abstractions (`ApprovedModel`, `ProductionCluster`), approved-model catalogs, defaults, governance |
| `CapabilityVocabulary` extension | Platform team | Org-specific keys (compliance levels, custom hardware), engine feature names for forks, KV tier overrides — no CRD bump |
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

**Namespace = environment.** 0..N of each user-facing resource (`ModelEndpoint`, `ModelDeployment`, `ModelService`, `ModelPlacement`) per namespace. Pushing an MD revision triggers lifecycle reconciliation there. `CapabilityVocabulary` is cluster-scoped — single source of truth for hardware semantics.

**Consumer-index discipline.** Every field on the user-facing API has at least one named consumer (matcher / composer / backend adapter / gateway), spelled out in a `Field-level consumer index` block at the top of each XRD. No consumer → no field. The matcher derives the required-feature set from declared config (`roles` → disagg, `engine.optimizations.*` → typed knobs, `adapters[]` → multi-lora) and unions it; the user declares what they want, not which features that needs.

## Hardware taxonomy & reference clusters

The vocabulary above is grounded in a survey of GPU hardware across major inference platforms, GPU clouds, hyperscalers, and on-prem systems (Bassam's "GPU hardware survey and unified taxonomy", 2026-05-07). Four logical layers, organized by what changes together:

| Layer | What it describes | Examples |
|---|---|---|
| **Cluster** | facts about the whole environment | `cloud.provider`, `cloud.region`, `network.fabric`, `network.bandwidthGbps`, `network.airgapped`, `cluster.scaleUnit` |
| **Pool** | the homogeneous-group host shape (per `nodePool`) | `cloud.instanceType`, `gpuCount`, `interconnect.{type, bandwidthGBs}`, `cpu.{vendor, cores, platform}`, `memoryGiB`, `nics.{count, bandwidthGbps}`, `host.virtualization` |
| **Device** | per-GPU attributes (uniform across the pool's devices) | `vendor`, `product`, `architecture`, `formFactor`, `vramGiB`, `mig`, `capabilities` (set), `parentProduct` (for fractional / MIG) |
| Dynamic state | what changes at runtime (health, allocation, MIG state) | tracked separately, not part of the vocab |

A few load-bearing design choices in the vocabulary:

- **Capability sets, not boolean columns.** `capabilities: [fp8, fp4, mig, transformer-engine]` ages better than separate boolean fields. New formats are entries in the set, not a schema migration.
- **Predicates over equality.** `vramGiB >= 141` matches H200, B200, B300, MI300X. `vramGiB == 141` matches only H200. The former is what you usually want.
- **Architecture is metadata; capabilities do the matching work.** Hardcoding `architecture in [hopper, blackwell]` excludes AMD MI300X. The capability flags (`fp8`, `fp4`) are the durable expression.
- **Rack-scale needs `cluster.scaleUnit`.** `independent-nodes` for normal cloud SKUs; `nvl72` for GB200 / GB300 rack-scale (72 GPUs in one NVLink domain — the addressable unit isn't the node, it's the rack); `superpod` for NVIDIA DGX SuperPOD topology.
- **RoCE vs IB are distinct fabrics.** Same physical NICs (ConnectX) can run either protocol. OCI runs RoCE on Quantum-2 hardware AWS runs as native IB. The schema distinguishes via `network.fabric: ib | roce | efa | gvnic | standard` plus a separate `network.bandwidthGbps`.
- **OCPU vs vCPU.** Oracle prices in OCPU (physical core); most clouds use vCPU (hyperthread). Convert at provider import time and store physical `cpu.cores` consistently.
- **Fractional GPUs as separate entries.** H100 MIG instances and L4 1/8-slice (g6f) are separate device entries with reduced `vramGiB` and a `parentProduct` pointer to the underlying GPU. Cleaner than "0.5 of an H100."

**Instance-type macros wire SKUs into the taxonomy.** Each macro names a canonical shape and expands to typed attributes:

```yaml
instanceTypes:
  - name: H100-NVL-8x
    expands:
      vendor: nvidia
      product: H100
      formFactor: sxm
      vramGiB: 80
      capabilities: [fp8, transformer-engine, mig]
      gpuCount: 8
      interconnect.type: nvswitch
      interconnect.bandwidthGBs: 900
    aliases:                                   # per-cloud SKU strings
      - aws:p5.48xlarge
      - gcp:a3-megagpu-8g
      - oci:BM.GPU.H100.8
      - coreweave:gd-8xh100ib-i128
      - dgx:DGX-H100
```

Customers match on the macro string (`cloud.instanceType: H100-NVL-8x`) for the common case or on unpacked attributes for unusual constraints (`vramGiB >= 141 && capabilities contains fp8`). Same predicate engine, no new code path.

**Reference InferenceClusters from cloud SKUs.** Pre-generated `InferenceCluster` definitions per known SKU live in [`proposed-modelplane-api/examples/reference-clusters/`](proposed-modelplane-api/examples/reference-clusters/) — AWS p5, GKE A3 Mega, OCI MI300X, CoreWeave GB300 NVL72, EKS H100 (no-DRA path). Customers copy or compose. Static today; the natural follow-up is a Crossplane provider that polls cloud SKU APIs and generates these programmatically.

**Commercial-offering framing.** The canonical-catalog work is the wedge:

- **Vocabulary tracking** — chip families, instance-type taxonomy, per-cloud SKU mappings, engine versions, quantization formats. Bounded, ongoing, high-leverage.
- **Reference clusters** kept current across NVIDIA / AMD / TPU / Trainium / Maia × AWS / GCP / Azure / OCI / CoreWeave / Crusoe / Lambda / Nebius / on-prem-DGX.
- **Continuous testing & benchmarking** — each reference cluster paired with a tested, benchmarked workload across every supported model family. Costly to maintain; what customers actually pay for. Natural fit for an Upbound-managed offering above the OSS default.

**Vocabulary tiers (where keys come from):**

| Tier | Source | Governance |
|---|---|---|
| Vendor (`gpu.nvidia.com/*`, `gpu.amd.com/*`, `tpu.google.com/*`) | DRA drivers | Consume, never define |
| K8s standards (`resource.kubernetes.io/*`) | WG-Device-Management | Track and alias as KEPs land |
| `modelplane.ai/*` | `CapabilityVocabulary` CR (Modelplane default + customer overrides) | The managed canonical catalog |
| User (`acme.example/*`) | User | Pass-through, unvalidated; first-class match via `<level>Selector.matchAttributes` |

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
| `ModelObjective`-style intent layer | Punt past v2 — non-breaking layer above MD if/when needed | Ship in v1 (mirrors Dynamo DGDR/DGD); never |

Nic-owned (not for this PR):

- **Dedicated-SaaS placement.** `ModelService` here is routing-only and a sketch pending Nic's design for provisioning a dedicated Together / Baseten endpoint.

## What ships v1 vs v2 (themed)

**v1 — Foundation**

| Theme | Scope |
|---|---|
| Substrate | Six CRDs (5 user-facing + `ModelPlacement` IR); env + node + device attributes on `InferenceCluster`; `managed-kueue` install on `InferenceCluster` |
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

- [`xrds/inferencecluster.yaml`](proposed-modelplane-api/xrds/inferencecluster.yaml) — cluster-scoped substrate, env + node + device attributes, `provisioning.mode`, `scheduler.type`, `backend.{type, version}`
- [`xrds/modelservice.yaml`](proposed-modelplane-api/xrds/modelservice.yaml) — namespace-scoped routing-only target (rough sketch — Nic owns the dedicated-SaaS placement concept)
- [`xrds/capabilityvocabulary.yaml`](proposed-modelplane-api/xrds/capabilityvocabulary.yaml) — cluster-scoped vocab CR (singleton, name: `default`)
- [`xrds/modeldeployment.yaml`](proposed-modelplane-api/xrds/modeldeployment.yaml) — namespace-scoped workload, K8s scale subresource for KEDA
- [`xrds/modelendpoint.yaml`](proposed-modelplane-api/xrds/modelendpoint.yaml) — namespace-scoped weighted routing across `Deployment` / `ModelService` / `External`
- [`xrds/modelplacement.yaml`](proposed-modelplane-api/xrds/modelplacement.yaml) — existing CRD playing the role of the intermediate representation (IR); one per logical replica (replica == placement)

**Substrate examples** (platform-team setup):

- [`examples/inferencecluster-prod-coreweave.yaml`](proposed-modelplane-api/examples/inferencecluster-prod-coreweave.yaml) — production Coreweave H200 cluster; BYO `kueue` + BYO `kserve@v0.18.0`
- [`examples/modelservice-together.yaml`](proposed-modelplane-api/examples/modelservice-together.yaml) — Together AI as a routing target
- [`examples/capabilityvocabulary-default.yaml`](proposed-modelplane-api/examples/capabilityvocabulary-default.yaml) — the default vocabulary Modelplane installs (aligned with Bassam's hardware-survey taxonomy)
- [`examples/reference-clusters/`](proposed-modelplane-api/examples/reference-clusters/) — pre-generated `InferenceCluster` definitions for known SKUs: AWS p5 (DRA), GKE A3 Mega (DRA), OCI MI300X (DRA + AMD), CoreWeave GB300 NVL72 (rack-scale), EKS H100 no-DRA (labels-first BYOC). Anchors the managed-catalog commercial offering.

**Workload examples** (ML/App team deployments):

- [`examples/kimi-k2.yaml`](proposed-modelplane-api/examples/kimi-k2.yaml) — frontier MoE, multi-node (2× 8 H200), 5P3D disaggregation, FP8 weights + KV; demonstrates the typed-attribute predicate path (`vramGiB >= 141`, `capabilities: [fp8]`, `interconnect.type: [nvswitch, xgmi]`)
- [`examples/kimi-k2-eu.yaml`](proposed-modelplane-api/examples/kimi-k2-eu.yaml) — EU-region sibling of `kimi-k2.yaml`; demonstrates the multi-region pattern (one MD per region; pinned via `cloud.region` + `modelplane.ai/compliance`)
- [`examples/qwen3-coder.yaml`](proposed-modelplane-api/examples/qwen3-coder.yaml) — code completion, n-gram speculation, 3 LoRA adapters, 256K context; user-defined `acme.example/*` attributes for team affinity
- [`examples/gpt-oss-20b.yaml`](proposed-modelplane-api/examples/gpt-oss-20b.yaml) — small MoE, scale-to-zero; demonstrates the labels-first match path (no DRA needed; matches on the NVIDIA GPU operator's `nvidia.com/gpu.family` node label)
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
| `xrds/modelservice.yaml` | `apis/modelservices/definition.yaml` |
| `xrds/capabilityvocabulary.yaml` | `apis/capabilityvocabularies/definition.yaml` |
| `xrds/modeldeployment.yaml` | `apis/modeldeployments/definition.yaml` (expanded) |
| `xrds/modelendpoint.yaml` | `apis/modelendpoints/definition.yaml` |
| `xrds/modelplacement.yaml` | `apis/modelplacements/definition.yaml` (expanded as the IR) |
| `examples/*.yaml` | `examples/` at repo root, or `examples/compositions/` for platform-team starters |
