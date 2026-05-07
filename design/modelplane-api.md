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
- **Adapter / plugin substrate.** Modelplane ships managed plugins for the common path — `managed-kserve` (backend) and `managed-kueue` (scheduler). BYO contracts (`InferenceCluster.spec.{backend, scheduler}.type`) let customers plug in KAI / Volcano / Dynamo / etc. The intermediate representation (`ModelPlacement`) is the seam.
- **`ModelService` is routing-only**, never a placement target. Matcher considers only `InferenceCluster`. (A separate concept for *placement* against dedicated SaaS endpoints is on Nic to define.)
- **CapabilityVocabulary as a managed canonical catalog.** Modelplane ships a default mapping of known cases (chip generations, engine versions, quantization formats, KV tiers, fabric ordering). Customers override per-cluster for bespoke hardware. Keeping the canonical catalog current as new chips / engines / formats land is high-leverage and bounded — candidate for an Upbound-managed offering.
- **Wedge:** fleet-level capabilities single-cluster platforms can't reach — fleet matching, geo + compliance routing, KV cache federation, sticky sessions, failover, cost-aware routing.
- **No new in-cluster scheduler.** K8s scheduler + DRA, Kueue (or KAI / Volcano), KEDA/HPA, Cluster Autoscaler each own their layer; we layer above.

## Problem

VRAM-divided-by-per-GPU-memory worked for Llama-8B on an L4. It can't deploy Kimi K2, DeepSeek V4, or Llama 4 Behemoth on heterogeneous fleets. There's no way to say *"16 H200s in 2×8 layout, NVLink-grouped, IB-400G-or-better, FP8-quantized, EAGLE speculative decoding, 5P3D disaggregation"* and no way for a cluster to say *"I have that."* Baseten has these capabilities; gated behind support tickets, on Baseten's GPUs. Modelplane delivers them declaratively on customer infrastructure.

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

**Cluster scope** holds substrate shared across the org: the `InferenceCluster`s and the singleton `CapabilityVocabulary`. The vocabulary is cluster-scoped because `InferenceCluster`s are — one cluster's hardware semantics shouldn't be evaluated differently from each namespace. Namespaces customize via Compositions and user-defined `acme.example/*` keys, not vocab redefinition. **Namespace scope** is the lifecycle boundary — each namespace is an "environment" (prod / staging / dev / per-team). All workload, routing, and SaaS-target resources live there.

The matcher considers only `InferenceCluster` candidates — `ModelService` is routing-only, never a placement target. A separate concept for *placement* against dedicated SaaS endpoints is on Nic to define.

**Key architectural decisions:**

- Meta-scheduler only — compose objects, never bind devices or actuate replicas.
- `ClusterModel` / `Model` deleted; workload spec self-contained on `ModelDeployment`.
- **Replica == placement** — one `ModelPlacement` per logical replica of a `ModelDeployment`. Each replica is independently scheduled by the matcher against the MD's `clusterSelector`. KEDA writes `MD.spec.replicas` via the scale subresource; the composer reconciles MPs to match. No custom KEDA scaler.
- **Federation matches against declared pool attributes, not runtime DRA.** `InferenceCluster.spec.nodePools[].{node,device}Attributes` are the source of truth at the federation layer. Real DRA `ResourceSlice`s (where present) are used for runtime grounding at the per-cluster scheduling stage — see the two-stage scheduling section below.
- Two-level matching cascade: `clusterSelector` (env-level attrs) → `deviceSelector`. **Labels are the primary match path** — `deviceSelector.matchLabels` works against any K8s cluster with node labels (no DRA needed). Typed `matchAttributes` + CEL is the **break-glass** for richer constraints (NVLink-domain co-location, FP8 capability, MIG state). Most workloads don't need it.
- **In-cluster scheduling delegated.** Modelplane decides which cluster; in-cluster scheduling — bin-packing, gang scheduling, fractional GPU, NVLink-aware placement, capacity tracking — is the in-cluster scheduler's job. We ship Kueue as the default substrate (`managed-kueue` mode, like `managed-kserve`); BYO schedulers (KAI, Volcano, existing Kueue installs) are supported via a capacity-signal contract.
- `ModelPlacement` (the existing CRD in `apis/modelplacements/`) plays the role of the **intermediate representation (IR)** — the seam between the matcher (which emits MPs, one per replica) and the version-pinned backend adapter (which consumes the MP and renders upstream objects, absorbing schema churn). The IR isn't a new abstraction; it's the role this existing CRD plays.
- Namespace = environment / lifecycle scope. Pushing a `ModelDeployment` revision triggers lifecycle reconciliation in that namespace.
- Failover modes are active-active or active-passive.

## Adapter / plugin substrate

Two layers under Modelplane are pluggable: the in-cluster scheduler (admission / quota) and the inference backend (orchestrator that renders pods). Both follow the same pattern — Modelplane ships a managed default plugin (the common path), customers BYO via a contract on `InferenceCluster.spec`. The intermediate representation (`ModelPlacement`) is the seam between Modelplane's matcher and whichever backend / scheduler the cluster runs.

| Layer | Default plugin (ships with Modelplane) | BYO declaration | What flows across the seam |
|---|---|---|---|
| In-cluster scheduler | **`managed-kueue`** (installs Kueue + `ClusterQueue` per pool) | `InferenceCluster.spec.scheduler.type: kueue \| kai \| volcano \| none` | (1) admission CR — `Workload` for Kueue, `PodGroup` for KAI / Volcano. (2) capacity-signal status field — `ClusterQueue.status.flavorsUsage[]` or scheduler-equivalent. |
| Inference backend | **`managed-kserve`** (installs KServe at the pinned version + composes `KServeBackend`) | `InferenceCluster.spec.backend.{type, version}: kserve \| dynamo \| raw-vllm` | An IR adapter watches `ModelPlacement` and renders backend-specific upstream objects per cluster: `LLMInferenceService` for KServe (per version), `DynamoGraphDeployment` for Dynamo, `Deployment+Service` for raw-vllm. Adapter writes back to `ModelPlacement.status.rendered`. |

**Opinionated:** the IR (`ModelPlacement`) — schema is Modelplane-controlled, plugins adapt to it. The matching logic (`clusterSelector` / `deviceSelector` + matcher-derived feature set) and the user-facing API (`ModelDeployment` / `ModelEndpoint` / `ModelService`) are universal — they don't change when a plugin swaps.

**Pluggable:** thin adapters per scheduler + per backend version. Modelplane ships Kueue + KServe (v0.16 / v0.17 / v0.18) by default; KAI / Volcano / Dynamo are future work. The IR contract is documented well enough that someone can write a Dynamo adapter without reverse-engineering us.

This way customers keep existing scheduler investments (KAI for training shops, Volcano for batch) and existing orchestration stacks (Dynamo) — Modelplane sits above and adds the fleet layer.

## Two-stage scheduling: federation vs in-cluster

Modelplane and DRA solve different problems. DRA is a *runtime allocator* — drivers publish `ResourceSlice`s about real hardware on real nodes, and the K8s scheduler matches `ResourceClaim`s against them. Modelplane's federation layer schedules against *declared* pool capacity, before any nodes exist. That's at the planning stage, not allocation.

We borrow from DRA's vocabulary, not its Kinds:

| Borrow (vocabulary) | Drop (Kinds) |
|---|---|
| Typed attributes (`string` / `version` / `quantity` / `int` / `bool`) | `DeviceClass` |
| Domain-prefixed keys (`gpu.nvidia.com/*`, `modelplane.ai/*`, `acme.example/*`) | `ResourceSlice` (no driver introspection at federation time) |
| CEL as the predicate language | `ResourceClaim` (no allocation happens at federation time) |
| The `device.attributes[domain].name` access pattern | "DRA-shaped" framing |

So the schema reads as `clusterSelector` / `deviceSelector` (selectors over declared attributes), evaluated by Modelplane against `InferenceCluster.spec.nodePools[].{node,device}Attributes`. Same predicate engine the K8s scheduler runs, different inputs.

**Two stages, in order:**

1. **Federation match (Modelplane control plane, pre-provisioning).** `clusterSelector` filters `InferenceCluster` candidates by env-level attributes; `deviceSelector` filters node pools within matching clusters. Output: `ModelPlacement` per logical replica, each pinned to a `(cluster, pool)`. No real hardware involved. This stage is fleet-aware, capacity-aware (via Kueue `ClusterQueue.status` signal), and cost-aware. It runs whether or not the target cluster has a DRA driver.
2. **In-cluster scheduling (per-cluster, at pod admission).** The backend adapter on the chosen cluster renders pods (KServe `LLMInferenceService`, Dynamo `DynamoGraphDeployment`, etc). The K8s scheduler binds them. *Optionally*, the adapter emits real `ResourceClaim`s here, carrying the same CEL predicates from `deviceSelector`. The DRA driver's runtime `ResourceSlice`s ground those predicates against actual hardware. Mismatch → pod stays Pending with a clear error.

**When the adapter emits DRA claims (the grounding contract):**

- **BYOC clusters with DRA**: emit them. Pool attributes were declared by the customer; independent grounding via the DRA driver catches typos, drift, and mis-config at scheduling time. The cost (one extra `ResourceClaim` per pod) is small; the safety win is real.
- **Modelplane-provisioned clusters**: optional. Modelplane created the pool from a known SKU, so attributes are derived from what we asked for — no drift between declaration and reality at provisioning. We can still emit claims for runtime verification (belt-and-suspenders), but it's not load-bearing.
- **BYOC clusters without DRA**: skip — the adapter constrains pods via labels (`nodeSelector`, `topologySpreadConstraints`) instead. Federation-stage match still uses the same `deviceSelector` predicates; just no runtime grounding.

The user-facing API doesn't change across these — same `clusterSelector` / `deviceSelector` shape, same vocabulary. The decision to emit `ResourceClaim`s is a per-cluster property of the backend adapter, governed by `InferenceCluster.spec.provisioning.mode`.

## Fleet-level capabilities

Single-cluster platforms (llm-d, KServe alone, Dynamo) optimize within a cluster. Operating at the fleet layer reaches across `InferenceClusters`, with SaaS routes via `ModelService`.

| Capability | What it does |
|---|---|
| Fleet matching | A single `ModelDeployment` finds eligible `InferenceCluster`s across regions, clouds, vendors; `matchTrace` shows why each fits or doesn't |
| Hardware-heterogeneous routing | One `ModelEndpoint` weighting across multiple `ModelDeployment`s on different hardware, plus `ModelService` routes for SaaS spillover |
| Geo + compliance routing | EU traffic to EU clusters; SOC 2 traffic only to certified clusters; data-residency-aware placement via `clusterSelector` |
| Cross-cluster replica scaling | Replicas of one MD spread across matching clusters; matcher picks per replica based on capacity signal |
| Fleet KV cache federation | G4-tier networked cache as a global fabric across `InferenceClusters`; route to whichever already has the prefix |
| Fleet session affinity | Sticky sessions across regional ingresses; multi-turn chat lands on the same `(cluster, replica)` |
| Fleet failover | Active-active or active-passive cutover when a cluster degrades |
| Cost-aware routing | Pick the cheapest fleet member that fits; blend reserved / on-demand / spot / per-token |
| Fleet overflow | Burst from a primary `InferenceCluster` to a sibling or to a `ModelService` when local capacity exhausts (#48) |
| Aggregated fleet observability | TTFT / ITL / cost / queue-depth rolled up across the fleet for one logical service |

What ships when is in the project plan section at the bottom — these are the design-level capabilities the architecture supports.

## Who owns what

**ML/App team — workload authors**

- `ModelDeployment` — self-contained workload spec (or instantiates a platform Composition that generates one).
- `ModelEndpoint` — weighted routing across `ModelDeployment`s.

**Platform team — substrate authors (cluster + namespace scope)**

Cluster scope:

- `InferenceCluster` resources — one per managed K8s cluster; declares attributes, node pools, `provisioning.mode`. Engine features live on the associated `KServeBackend`.
- Default `CapabilityVocabulary` in `modelplane-system` — well-known attribute keys, ordering, KV tiers, engine feature names.
- Workload-plane substrate — KServe, DRA driver, Kueue, KEDA on each `InferenceCluster` (BYO or via `managed-kserve` mode).

Namespace scope (per environment — prod / staging / dev / per-team):

- `ModelService` resources — one per SaaS endpoint (Together, Baseten, Bedrock, customer-run KServe). Routing-only target on `ModelEndpoint`; never a placement target.
- Crossplane Compositions over `ModelDeployment` — for org-specific abstractions (`ApprovedModel`-style XRs), governance, defaults.

**Modelplane — what the project ships**

- User-facing CRDs: `InferenceCluster` (cluster), `CapabilityVocabulary` (namespace), `ModelDeployment` (namespace, with scale subresource), `ModelService` (namespace), `ModelEndpoint` (namespace).
- Internal CRD: **`ModelPlacement`** (existing in `apis/modelplacements/`) — the **intermediate representation (IR)**. One per *logical replica*, owned by `ModelDeployment`. Backend adapters watch it and emit upstream objects.
- Crossplane composition functions — the matcher (emits `ModelPlacement`s and a stock KEDA `ScaledObject`) and the version-pinned KServe adapter (consumes the IR).
- Drift detection controller — reads runtime DRA `ResourceSlice`s, surfaces `CapabilityDrift` conditions.
- Default `CapabilityVocabulary` install — well-known keys, engine features, KV tiers, aliases.
- Starter Compositions in `examples/compositions/` derived from vLLM recipes.
- KServe install manifests pinned per supported version (`managed-kserve` mode).

## How users consume it

**ML/App team day-one.** Write a `ModelDeployment` (or instantiate a platform-team Composition like `ApprovedModel` that generates one). Modelplane's matcher picks an `InferenceCluster` and emits one `ModelPlacement` per logical replica (`spec.replicas`); the version-pinned adapter renders each MP to `LLMInferenceService.spec.replicas: 1`. KServe provisions pods. DRA binds devices. KEDA writes `MD.spec.replicas` via the scale subresource as load changes; the composer reconciles MPs to match. The endpoint is reachable via `ModelEndpoint`, which can also route to a `ModelService` for SaaS spillover. `matchTrace` shows which clusters were considered and why excluded.

**Platform team day-one.**

1. Install Modelplane on the Crossplane control plane (CRDs + composition functions).
2. Install workload-plane substrate on each managed K8s cluster (KServe + DRA + Kueue + KEDA), or use `managed-kserve` mode where Modelplane installs a pinned KServe.
3. Create one `InferenceCluster` per managed cluster; declare attributes and engine features.
4. Create one `ModelService` per SaaS endpoint (or install a Crossplane provider that creates them programmatically).
5. (Optional) extend `CapabilityVocabulary`; ship Crossplane Compositions abstracting `ModelDeployment` for org governance.

## Extensibility points

| Extension | Owner | Why use it |
|---|---|---|
| Crossplane Compositions over `ModelDeployment` | Platform team | Org-specific abstractions, approved-model catalogs, defaults, shorter API for ML/App teams |
| Crossplane Compositions over substrate CRDs | Platform team | Wrap `InferenceCluster` / `ModelService` with org-specific provisioning (e.g., a `ProductionCluster` XR that sets attributes + RBAC + monitoring) |
| `CapabilityVocabulary` extension | Platform team | Add org-specific keys (compliance levels, custom hardware), engine feature names for forks, KV tier overrides — without a CRD bump |
| `engine.args` opaque pass-through | ML/App team or Composition author | Engine flags Modelplane doesn't model |
| `engine.advanced[]` typed-name break-glass | ML/App team or Composition author | Novel knobs the IR doesn't model yet, with structure for adapters |
| `<level>Claim.selector.cel` CEL escape hatch | ML/App team | Boolean / set-arithmetic constraints not expressible in typed `matchAttributes` |
| `<level>Claim.selector.matchAttributes` user-defined keys | Both teams | Org-specific match dimensions (cost center, team, security clearance) |
| `engine.advanced[]` named feature break-glass | Platform team (declares on `KServeBackend`) + ML/App team (uses) | Engine forks add features Modelplane's typed `engine.optimizations` doesn't ship yet — promote to typed over time |
| Custom composition functions | Platform team / community / vendors | Replace Modelplane's matcher / composer with custom placement policy, cost model, or IR adapter — without forking the project |
| Custom Crossplane providers | Platform team / community / vendors | Programmatically create `InferenceCluster` (new cloud) or `ModelService` (new SaaS) from external systems |
| Forking the project | Community / vendors | Needs that don't fit the upstream roadmap; ship a derivative with different defaults, additional CRDs, alternative engine support |

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

**Replica == placement.** Each `ModelDeployment` has N `ModelPlacement`s — one per logical replica. The matcher picks `(cluster, pool)` for each MP independently; the version-pinned KServe adapter renders one `LLMInferenceService.spec.replicas: 1` per MP. Multi-node logical replicas (Kimi K2 PP=2) are still ONE MP — multi-pod via LWS within one cluster. KEDA writes `MD.spec.replicas` via the scale subresource; the composer reconciles MPs to match. No custom KEDA scaler. Multi-region spread = multiple MDs + multiple `ModelEndpoint` route entries.

**`ModelService` is routing-only.** It's not a fleet-member candidate; the matcher only considers `InferenceCluster`. Workloads requiring engine features that no `InferenceCluster` exposes are excluded with field-level reasons in `matchTrace`. A separate concept for *placement* against dedicated SaaS endpoints is on Nic to define.

**Namespace = environment.** Each namespace is the lifecycle scope: 0..1 `ModelEndpoint`, 0..N `ModelDeployment` / `ModelService`, 0..N `ModelPlacement`. Pushing a `ModelDeployment` revision triggers lifecycle reconciliation in that namespace. `CapabilityVocabulary` is cluster-scoped (single source of truth for hardware semantics).

**Consumer-index discipline.** Every field on the user-facing API has at least one named consumer (matcher / composer / backend adapter / gateway). Each XRD spec carries a `Field-level consumer index` block at the top documenting *who reads what for what purpose*. If a field has no real consumer, it gets removed. This is why `requiredEngineFeatures` is gone — every required feature is derivable from a more concrete declaration (`roles` → disagg, `engine.optimizations.kvCacheRouting` → kv-cache-routing, `adapters[]` → multi-lora, etc.), and the matcher unions them at federation time. Single source of truth: declare *what you want*; matcher derives *what backend features that requires*.

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

Customers match on either dimension — the high-level string (`cloud.instanceType: H100-NVL-8x`) for the common case, or unpacked attributes for unusual constraints (`vramGiB >= 141 && capabilities contains fp8`). Platform teams declare `instanceType` once on a pool and the vocab implies the rest. Same predicate engine, no new code path.

**Reference InferenceClusters from cloud SKUs.** Modelplane ships pre-generated `InferenceCluster` definitions for known cloud / on-prem SKUs in [`proposed-modelplane-api/examples/reference-clusters/`](proposed-modelplane-api/examples/reference-clusters/) — `aws-p5-48xlarge.yaml`, `gke-a3-mega-8g.yaml`, `oci-bm-gpu-mi300x-8.yaml`, `coreweave-gb300-nvl72.yaml` to start. Customers copy or compose these instead of authoring `nodePools[]` from scratch. Two ways to ship this:

1. **Static artifacts** (in the repo today): low effort, high signal. Updated as new SKUs land.
2. **Crossplane provider** (follow-up): polls cloud SKU APIs (`gcloud compute machine-types list`, AWS `DescribeInstanceTypes`, Azure equivalent) and generates `InferenceCluster` resources programmatically. Removes the "keep labels up-to-date by hand" burden Viktor flagged.

**Commercial-offering framing.** The canonical-catalog work is the wedge:

- **Tracking** new chip families, instance-type taxonomy, per-cloud SKU mappings, engine versions, quantization formats — bounded, ongoing, high-leverage.
- **Reference clusters** continuously kept current across NVIDIA / AMD / TPU / Trainium / Maia and across AWS / GCP / Azure / OCI / CoreWeave / Crusoe / Lambda / Nebius / on-prem-DGX.
- **Continuous testing & benchmarking.** Each reference cluster paired with a tested, benchmarked workload run on every supported model family (Llama, Qwen, DeepSeek, Kimi, GPT-OSS, etc.) so customers can rely on the catalog rather than just consume YAML. Costly to maintain — and exactly the kind of capability customers will pay for. Natural fit for an Upbound-managed offering layered above the OSS default.

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

## Open questions (Nic to call)

- **Default scheduler / backend.** Lean: `managed-kueue` + `managed-kserve` as opinionated defaults, BYO contracts for KAI / Volcano / Dynamo / raw-vllm. Confirm, or pick different defaults?
- **Label-vs-attribute matching path.** `deviceSelector` supports both `matchLabels` (primary) and `matchAttributes` + CEL (break-glass over declared attributes). Confirm dual-path is right, or commit to one?
- **DRA grounding contract.** When the chosen `InferenceCluster` has a DRA driver, the backend adapter emits real `ResourceClaim`s carrying the same predicates (mandatory for BYOC, optional for Modelplane-provisioned). Confirm this is the right split, or always-on for both?
- **Rack-scale as its own unit.** GB200 / GB300 NVL72 is 72 GPUs in one NVLink domain — the addressable unit isn't the node, it's the rack. Captured today via `cluster.scaleUnit: nvl72` as an env-level attribute. Open: should the matcher reason about the whole rack as one capacity unit for placements that span the NVLink domain (very large MoE with PP across the rack), or treat the rack as multiple `nodePool` entries?
- **Reference-cluster generation.** Static reference clusters today (committed YAML in `examples/reference-clusters/`); follow-up is a Crossplane provider that polls cloud SKU APIs and generates these programmatically. Confirm this is the right ordering, or push the provider sooner?
- **Dedicated-SaaS placement.** `ModelService` is routing-only; "provision a dedicated Together / Baseten endpoint" is a placement concept Nic owns. Rough sketch only here pending Nic's design.
- **`ModelObjective`-style intent layer.** Optional CR above `ModelDeployment` for SLO targets (TTFT, ITL, cost ceiling), reconciled by a planner — mirrors Dynamo's DGDR / DGD pattern. Worth a layer, or punt?
- **vLLM recipe consumption.** Reference `recipes.vllm.ai` from `ModelDeployment.spec.recipe` (compose-time resolution) vs. fork into a Modelplane catalog repo. PM-shaped call.
- **WG-Device-Management engagement.** Concrete deliverable (e.g. KEP-5316 comment with Modelplane's federation perspective by Q3) or hold?

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
- [`examples/reference-clusters/`](proposed-modelplane-api/examples/reference-clusters/) — pre-generated `InferenceCluster` definitions for known cloud SKUs (AWS p5, GKE A3 Mega, OCI MI300X, CoreWeave GB300 NVL72). Anchors the managed-catalog commercial offering.

**Workload examples** (ML/App team deployments):

- [`examples/kimi-k2.yaml`](proposed-modelplane-api/examples/kimi-k2.yaml) — frontier MoE, multi-node, 5P3D disaggregation, FP8 weights + KV; demonstrates the DRA `matchAttributes` break-glass path (NVLink-domain co-location)
- [`examples/qwen3-coder.yaml`](proposed-modelplane-api/examples/qwen3-coder.yaml) — code completion, n-gram speculation, 3 LoRA adapters, 256K context; DRA path
- [`examples/gpt-oss-20b.yaml`](proposed-modelplane-api/examples/gpt-oss-20b.yaml) — small MoE, scale-to-zero; demonstrates the labels-first match path (no DRA needed)
- [`examples/assistant-endpoint.yaml`](proposed-modelplane-api/examples/assistant-endpoint.yaml) — `ModelEndpoint` weighted across the three deployments + Together routing

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
