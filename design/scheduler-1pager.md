# Modelplane Scheduler & Capability Model — 1-pager

**Status:** Draft for Bassam review
**Author:** Dennis Ramdass
**Date:** 2026-05-06

## TL;DR

- **Modelplane is a Crossplane-native, multi-cloud inference control plane.**
- **Cluster scope** holds substrate (`InferenceCluster`s — customer K8s running KServe + DRA + Kueue + KEDA — and a singleton `CapabilityVocabulary`). **Namespace scope** is the lifecycle boundary (= environment): `ModelDeployment`, `ModelPlacement`, `ModelService`, `ModelEndpoint`.
- **Replica == placement.** One `ModelPlacement` per logical replica of a `ModelDeployment`. KEDA writes `MD.spec.replicas` via the K8s scale subresource; the composer reconciles MPs to match — no custom scaler.
- **`ModelService` is routing-only**, never a placement target. Matcher considers only `InferenceCluster`. (A separate concept for *placement* against dedicated SaaS endpoints is on Nic to define.)
- **Wedge:** fleet-level capabilities single-cluster platforms can't reach — fleet matching, geo + compliance routing, KV cache federation, sticky sessions, failover, cost-aware routing.
- **No new in-cluster scheduler.** K8s scheduler + DRA, Kueue, KEDA/HPA, Cluster Autoscaler each own their layer; we layer above.

## Problem

VRAM-divided-by-per-GPU-memory worked for Llama-8B on an L4. It can't deploy Kimi K2, DeepSeek V4, or Llama 4 Behemoth on heterogeneous fleets. There's no way to say *"16 H200s in 2×8 layout, NVLink-grouped, IB-400G-or-better, FP8-quantized, EAGLE speculative decoding, 5P3D disaggregation"* and no way for a cluster to say *"I have that."* Baseten has these capabilities; gated behind support tickets, on Baseten's GPUs. Modelplane delivers them declaratively on customer infrastructure.

## Design principles

1. **Clean separation, no enforcement.** Platform teams own substrate; ML/App teams own workloads. Same API split or unified.
2. **Fleet-wide by construction.** A `ModelDeployment` targets the fleet of `InferenceCluster`s, not a single cluster. `matchTrace` reports where it fits and why elsewhere doesn't. SaaS endpoints participate via `ModelEndpoint` routing, not placement.
3. **Plain Crossplane customization.** Catalogs, defaults, governance live in Compositions, RBAC, OPA — not Modelplane primitives.
4. **No new in-cluster scheduler.** We're a meta-scheduler. K8s scheduler + DRA, Kueue, KEDA/HPA, Cluster Autoscaler each own their layer.

## Architecture: control plane + fleet

```
            Modelplane Control Plane (Crossplane)
   pick (cluster, pool) per replica → compose ModelPlacement (IR)
                          ↓
   ─────────────── cluster scope ────────────────
   InferenceClusters (workload planes)
     KServe + DRA + Kueue + KEDA
     Modelplane composes LLMIS objects here
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
- **Replica == placement** — one `ModelPlacement` per logical replica of a `ModelDeployment`. Each replica is independently scheduled by the matcher against the MD's `clusterClaim`. KEDA writes `MD.spec.replicas` via the scale subresource; the composer reconciles MPs to match. No custom KEDA scaler.
- Declared attributes are authoritative for scheduling; runtime DRA is drift detection only.
- Two-level matching cascade: `clusterClaim` (env-level attrs) → `deviceClaim`. The `deviceClaim.selector` supports two paths: `matchLabels` for plain node-label matching (pre-DRA simple path; cluster `provisioning.mode: device-plugin`) and `matchAttributes` for DRA-shaped typed selection (cluster `provisioning.mode: dra`). DRA stays optional — customers who don't want it can use labels.
- **In-cluster scheduling delegated.** Modelplane decides which cluster; in-cluster scheduling — bin-packing, gang scheduling, fractional GPU, NVLink-aware placement, capacity tracking — is the in-cluster scheduler's job. We ship Kueue as the default substrate (`managed-kueue` mode, like `managed-kserve`); BYO schedulers (KAI, Volcano, existing Kueue installs) are supported via a capacity-signal contract.
- `ModelPlacement` is the **intermediate representation (IR)** — version-pinned adapters consume it and absorb KServe `LLMInferenceService` schema churn.
- Namespace = environment / lifecycle scope. Pushing a `ModelDeployment` revision triggers lifecycle reconciliation in that namespace.
- Failover modes are active-active or active-passive.

## Pluggable substrate: scheduler and backend

Two layers under Modelplane are pluggable: the in-cluster scheduler (admission / quota) and the inference backend (orchestrator that renders pods). Both follow the same pattern — opinionated default install, BYO contract for customers with existing investments.

| Layer | Default install | BYO declaration on `InferenceCluster.spec` | What Modelplane consumes |
|---|---|---|---|
| In-cluster scheduler | `managed-kueue` (installs Kueue + `ClusterQueue` per pool) | `scheduler.type: kueue \| kai \| volcano \| none` | (1) admission CR shape per scheduler — `Workload` for Kueue, `PodGroup` for KAI / Volcano. (2) capacity-signal status field — `ClusterQueue.status.flavorsUsage[]` for Kueue, equivalents elsewhere |
| Inference backend | `managed-kserve` (installs KServe + composes `KServeBackend`) | `backend.{type, version}: kserve \| dynamo \| raw-vllm`, e.g. `version: v0.18.0` | IR adapter renders backend-specific upstream objects per cluster: `LLMInferenceService` for KServe, `DynamoGraphDeployment` for Dynamo, `Deployment+Service` for raw-vllm. Adapter writes back to `ModelPlacement.status.rendered` |

What stays opinionated: the IR (`ModelPlacement`) — its schema is Modelplane-controlled; backends adapt to it, not vice versa. The matching logic (`clusterClaim` / `deviceClaim` / `requiredEngineFeatures`) is universal across backends and schedulers. The user-facing API (`ModelDeployment` / `ModelEndpoint` / `ModelService`) never changes when scheduler or backend swaps.

What's pluggable: thin adapters per scheduler and per backend version. v1 ships Kueue + KServe (v0.16 / v0.17 / v0.18). KAI / Volcano / Dynamo adapters are future work — community, vendor, or our follow-up. Modelplane's contract is documented well enough that someone can write a Dynamo adapter without reverse-engineering us.

## Fleet-level capabilities

Single-cluster platforms (llm-d, KServe alone, Dynamo) optimize within a cluster. Operating at the fleet layer reaches across `InferenceClusters`, with SaaS routes via `ModelService`.

| Capability | What it does |
|---|---|
| Fleet matching | A single `ModelDeployment` finds eligible `InferenceCluster`s across regions, clouds, vendors; `matchTrace` shows why each fits or doesn't |
| Hardware-heterogeneous routing | One `ModelEndpoint` weighting across multiple `ModelDeployment`s on different hardware, plus `ModelService` routes for SaaS spillover |
| Geo + compliance routing | EU traffic to EU clusters; SOC 2 traffic only to certified clusters; data-residency-aware placement via `clusterClaim` |
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
- Internal CRD: **`ModelPlacement` — the IR.** One per *logical replica*, owned by `ModelDeployment`. Adapter functions watch it and emit upstream objects.
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
| `requiredEngineFeatures` custom feature names | Platform team (declares on `KServeBackend`) + ML/App team (uses) | Engine forks add features Modelplane vocabulary doesn't ship |
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

# Two-level claim cascade, filters InferenceCluster only:
clusterClaim:                     # env-level attrs (region, tier, compliance)
  selector: { matchLabels, matchAttributes, cel }
deviceClaim:                      # selector dual-path: matchLabels (no DRA) or
  requests:                       # matchAttributes (DRA). Composer picks based
    - name, count, perNode        # on cluster.provisioning.mode.
      selector: { matchLabels, matchAttributes, cel }
      constraints: [{ matchAttribute, requests }]   # DRA-only
requiredEngineFeatures: [string]  # set-membership against cluster's KServeBackend

# Deployment shape (not claims):
parallelism: { tensor, pipeline, expert }
roles:                            # disaggregated serving (xPyD)
  prefill: { deviceClaim, parallelism, replicas }   # any unset inherits root
  decode:  { deviceClaim, parallelism, replicas }

engine:
  name, image, args
  quantization: { precision, target }
  speculation:
    type: EAGLE | DraftTarget | Medusa | NGram | Lookahead
  advanced: [{ name, config }]    # break-glass

scaling:                          # composer turns this into a stock KEDA ScaledObject
  signal: Concurrency | Utilization | Both
  concurrency: { minReplicas, maxReplicas, target, window, scaleDownDelay }
adapters: [{ name, source }]      # multi-LoRA + LoRA-aware routing
```

**Replica == placement.** Each `ModelDeployment` has N `ModelPlacement`s — one per logical replica. The matcher picks `(cluster, pool)` for each MP independently; the version-pinned KServe adapter renders one `LLMInferenceService.spec.replicas: 1` per MP. Multi-node logical replicas (Kimi K2 PP=2) are still ONE MP — multi-pod via LWS within one cluster. KEDA writes `MD.spec.replicas` via the scale subresource; the composer reconciles MPs to match. No custom KEDA scaler. Multi-region spread = multiple MDs + multiple `ModelEndpoint` route entries.

**`ModelService` is routing-only.** It's not a fleet-member candidate; the matcher only considers `InferenceCluster`. Workloads requiring engine features that no `InferenceCluster` exposes are excluded with field-level reasons in `matchTrace`. A separate concept for *placement* against dedicated SaaS endpoints is on Nic to define.

**Namespace = environment.** Each namespace is the lifecycle scope: 0..1 `ModelEndpoint`, 0..N `ModelDeployment` / `ModelService`, 0..N `ModelPlacement`. Pushing a `ModelDeployment` revision triggers lifecycle reconciliation in that namespace. `CapabilityVocabulary` is cluster-scoped (single source of truth for hardware semantics).

## Capability vocabulary tiers

| Tier | Source | Governance |
|---|---|---|
| Vendor (`gpu.nvidia.com/*`, `gpu.amd.com/*`, `tpu.google.com/*`) | DRA drivers | Consume, never define |
| K8s standards (`resource.kubernetes.io/*`) | WG-Device-Management | Track and alias as KEPs land |
| `modelplane.ai/*` | `CapabilityVocabulary` CR | Updates without CRD bumps; closed enums for stable keys, ordered for hardware, G1–G4 KV tiers |
| User (`acme.example/*`) | User | Pass-through, unvalidated; first-class match via `<level>Claim.selector.matchAttributes` |

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
| CRD ownership conflict with KServe upgrades | `byo-kserve` and `managed-kserve` install modes; never modify CRDs we didn't author |
| Break-glass features no fleet member supports | `matchTrace` field-level failure; `Ready=False NoMatchingEngineFeatures` |

**User experience**

| Risk | Mitigation |
|---|---|
| `ModelDeployment` chunky for ML/App teams | Crossplane Compositions; starter Compositions in `examples/` |

## Open questions (Nic to call)

- **Default in-cluster scheduler.** Lean: ship Kueue as `managed-kueue` (KServe pattern) + BYO support for KAI / Volcano via the capacity-signal contract. Confirm or pick a different default?
- **BYO contract details.** Each adapter (one per scheduler, one per backend version) is a thin reconciler that watches the IR / cluster declaration and renders the backend-specific objects. v1 ships Kueue + KServe; KAI / Volcano / Dynamo are future. Confirm contract shape (`InferenceCluster.spec.scheduler.type`, `InferenceCluster.spec.backend.{type, version}`) is right, or restructure?
- **Label-vs-DRA matching path.** `deviceClaim.selector` supports both `matchLabels` (no DRA needed) and `matchAttributes` (DRA-typed). Composer picks output based on cluster `provisioning.mode`. Confirm dual-path is the right shape, or commit to one?
- **`requires.engineFeatures` rename.** It's implicitly cluster-only (matched against `KServeBackend`). Rename to make that explicit, or leave?
- **Dedicated-SaaS placement.** `ModelService` is routing-only; "create a dedicated Together / Baseten endpoint" is a placement concept Nic owns. Rough sketch only here pending Nic's design.
- **`ModelObjective`-style intent layer.** Optional CR above `ModelDeployment` for SLO targets (TTFT, ITL, cost ceiling) reconciled by a planner — Dynamo's DGDR / DGD pattern. Worth a layer, or punt?
- **vLLM recipe consumption.** Reference `recipes.vllm.ai` from `ModelDeployment.spec.recipe` (compose-time resolution) vs. fork into a Modelplane catalog repo. PM-shaped call.
- **WG-Device-Management engagement.** Concrete deliverable (e.g. KEP-5316 comment with Modelplane's federation perspective by Q3) or hold?

## What ships v1 vs v2 (themed)

**v1 — Foundation**

| Theme | Scope |
|---|---|
| Substrate | Six CRDs (5 user-facing + `ModelPlacement` IR); env + node + device attributes on `InferenceCluster`; `managed-kueue` install on `InferenceCluster` |
| Matching | Two-level claim cascade (`clusterClaim` + DRA-shaped `deviceClaim`); typed `matchAttributes` shorthand + CEL escape; `matchTrace` |
| Workload API | Self-contained `ModelDeployment`; replica == placement (`spec.replicas` + scale subresource); `roles.{prefill, decode}` for xPyD disaggregation; `engine.{quantization, speculation, advanced}`; five-factor `scaling`; `adapters` |
| Composition | Matcher → `ModelPlacement` IR → version-pinned KServe adapter; DRA + device-plugin emission |
| Delegation | Kueue for quota; KEDA-only autoscaling on concurrency |
| Fleet routing | Hardware-heterogeneous + geo + compliance routing via `clusterClaim` and `deviceClaim`; multi-region spread via multiple `ModelDeployment`s + `ModelEndpoint` |
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

Full proposed XRDs and example resources live in [`scheduler-deliverables/`](scheduler-deliverables/). That directory is a **design-time preview**: nothing there is wired up yet, and once we align on the API the XRDs move into [`apis/`](../apis/) and examples move into the repo-root `examples/`. See [`scheduler-deliverables/README.md`](scheduler-deliverables/README.md) for the full layout and what's deliberately incomplete.

**XRDs** (proposed CompositeResourceDefinitions):

- [`xrds/inferencecluster.yaml`](scheduler-deliverables/xrds/inferencecluster.yaml) — cluster-scoped substrate, env + node + device attributes, `provisioning.mode`
- [`xrds/modelservice.yaml`](scheduler-deliverables/xrds/modelservice.yaml) — namespace-scoped routing-only target (rough sketch — Nic owns the dedicated-SaaS placement concept)
- [`xrds/capabilityvocabulary.yaml`](scheduler-deliverables/xrds/capabilityvocabulary.yaml) — cluster-scoped vocab CR (singleton, name: `default`)
- [`xrds/modeldeployment.yaml`](scheduler-deliverables/xrds/modeldeployment.yaml) — namespace-scoped workload, K8s scale subresource for KEDA
- [`xrds/modelendpoint.yaml`](scheduler-deliverables/xrds/modelendpoint.yaml) — namespace-scoped weighted routing across `Deployment` / `ModelService` / `External`
- [`xrds/modelplacement.yaml`](scheduler-deliverables/xrds/modelplacement.yaml) — the IR; one per logical replica (replica == placement)

**Substrate examples** (platform-team setup):

- [`examples/inferencecluster-prod-coreweave.yaml`](scheduler-deliverables/examples/inferencecluster-prod-coreweave.yaml) — production Coreweave H200 cluster, DRA-enabled
- [`examples/modelservice-together.yaml`](scheduler-deliverables/examples/modelservice-together.yaml) — Together AI as a routing target
- [`examples/capabilityvocabulary-default.yaml`](scheduler-deliverables/examples/capabilityvocabulary-default.yaml) — the default vocabulary Modelplane installs

**Workload examples** (ML/App team deployments):

- [`examples/kimi-k2.yaml`](scheduler-deliverables/examples/kimi-k2.yaml) — frontier MoE, multi-node, 5P3D disaggregation, FP8 weights + KV
- [`examples/qwen3-coder.yaml`](scheduler-deliverables/examples/qwen3-coder.yaml) — code completion, n-gram speculation, 3 LoRA adapters, 256K context
- [`examples/gpt-oss-20b.yaml`](scheduler-deliverables/examples/gpt-oss-20b.yaml) — small MoE, MXFP4-native on Blackwell, scale-to-zero, fan-out across 2 regions
- [`examples/assistant-endpoint.yaml`](scheduler-deliverables/examples/assistant-endpoint.yaml) — `ModelEndpoint` weighted across all three (70 / 25 / 5)
