# Modelplane API Design — 1-pager

**Status:** Draft for Bassam review
**Author:** Dennis Ramdass
**Date:** 2026-05-07
**Scope:** API + scheduler + capability model. Heaviest on the scheduler/matching surface; touches the broader CRD shape and the adapter / plugin pattern that ties them together.

## TL;DR

- **Modelplane is a Crossplane-native, multi-cloud inference control plane.**
- **Cluster scope** holds substrate: `InferenceCluster`s (customer K8s) and the `InferenceClass` catalog (per-SKU hardware bundles, StorageClass-style). **Namespace scope** is the lifecycle boundary: `ModelDeployment`, `ModelPlacement`, `InferenceProvider`, `ModelEndpoint`.
- **Replica == placement.** One `ModelPlacement` per logical replica of a `ModelDeployment`. KEDA writes `MD.spec.replicas` via the K8s scale subresource; the composer reconciles MPs to match — no custom scaler.
- **Two-stage scheduling.** Modelplane is a *federation planner* — it evaluates predicates against *declared* pool capacity to pick `(cluster, pool)` per replica, before nodes exist. Per-cluster scheduling is delegated. **DRA is optional**, never required: the `device-plugin` mode (any K8s with the NVIDIA GPU operator) is the default; `dra` mode is opt-in for stronger runtime grounding. We borrow DRA's *vocabulary* (typed attributes, domain-prefixed keys, CEL) but not its Kinds — `ResourceClaim` / `ResourceSlice` / `DeviceClass` belong to the runtime allocator, not the federation layer.
- **Labels-first matching.** `deviceSelector.matchLabels` works on any K8s cluster with labeled nodes — see [`workloads/gpt-oss-20b.yaml`](./examples/workloads/gpt-oss-20b.yaml). Typed `matchAttributes` + CEL is the break-glass for richer constraints (NVLink-domain co-location, MIG, FP8 capability) — see [`workloads/kimi-k2.yaml`](./examples/workloads/kimi-k2.yaml).
- **Managed defaults.** `managed-kserve` (backend) + `managed-kueue` (scheduler) + KEDA `ScaledObject`s (autoscaler, prerequisite) ship under the hood — see [`clusters/managed-gke-a3.yaml`](./examples/clusters/managed-gke-a3.yaml). BYO contracts (`InferenceCluster.spec.{backend, scheduler}.type`) plug in KAI / Volcano / Dynamo / raw-vllm — see [`clusters/byoc-coreweave-kai-h200.yaml`](./examples/clusters/byoc-coreweave-kai-h200.yaml). `ModelPlacement` (the IR) is the seam.
- **`InferenceProvider` is a routing target** — see [`providers/together.yaml`](./examples/providers/together.yaml). External / SaaS endpoint registered with URL + auth + attributes. `ModelEndpoint` routes to it for SaaS spillover, regional preference, billing-model selection. Never a placement target — the matcher considers only `InferenceCluster`.
- **`InferenceClass` catalog as the wedge.** Default ships per-SKU hardware bundles (`h100-nvl-8x`, `b200-nvl-8x`, `mi300x-8x`, ...) — StorageClass-style, cluster-scoped. See [`inferenceclasses/`](./examples/inferenceclasses/). Customers author their own for bespoke hardware. Engine features live separately: derivation rules in matcher code, per-cluster supported set on `KServeBackend.spec.engine.features`, break-glass via `engine.advanced[]` — see [`workloads/acme-vllm-fork.yaml`](./examples/workloads/acme-vllm-fork.yaml). Keeping the class catalog current is high-leverage and bounded — Upbound-managed-offering candidate.
- **Wedge:** fleet-level capabilities single-cluster platforms can't reach — fleet matching, geo + compliance routing, KV cache federation, sticky sessions, failover, cost-aware routing.

## Design principles

1. **Clean separation, no enforcement.** Platform teams own substrate; ML/App teams own workloads. Same API split or unified.
2. **Fleet-wide by construction.** A `ModelDeployment` targets the fleet of `InferenceCluster`s, not a single cluster. `matchTrace` reports where it fits and why elsewhere doesn't. SaaS endpoints participate via `ModelEndpoint` routing, not placement.
3. **Plain Crossplane customization.** Catalogs, defaults, governance live in Compositions, RBAC, OPA — not Modelplane primitives.
4. **No new in-cluster scheduler.** We're a meta-scheduler. K8s scheduler + DRA, Kueue, KEDA/HPA, Cluster Autoscaler each own their layer.

## Architecture: control plane + fleet

Modelplane is a Crossplane control plane that composes onto a fleet of `InferenceCluster`s. The cluster scope holds shared substrate; each namespace is a lifecycle environment.

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
     InferenceProvider(s)       (routing-only target)
     ModelEndpoint         (weighted routing across MDs + IPs)
```

**Cluster scope** holds shared substrate: `InferenceCluster`s and the `InferenceClass` catalog. **Namespace scope** is the lifecycle boundary — each namespace is an environment (prod / staging / dev / per-team) holding workload, routing, and SaaS-target resources. The matcher considers only `InferenceCluster` candidates; `InferenceProvider` is routing-only.

**Key architectural decisions:**

- Meta-scheduler only — compose objects, never bind devices or actuate replicas. This design proposal removes the existing `ClusterModel` / `Model` split (`apis/clustermodels/`, `apis/models/` on main) in favor of a self-contained `ModelDeployment`.
- **Replica == placement.** One `ModelPlacement` per logical replica. Each replica independently scheduled by the matcher against the MD's `clusterSelector`. KEDA writes `MD.spec.replicas` via the scale subresource; the composer reconciles MPs to match. No custom scaler.
- **Federation matches against declared pool attributes, not runtime DRA.** `InferenceCluster.spec.nodePools[].{node,device}Attributes` are the source of truth at the federation layer. DRA `ResourceSlice`s ground predicates at the per-cluster scheduling stage (next section).
- **Two-level selector cascade**: `clusterSelector` (env-level) → `deviceSelector` (node + device). Labels are the primary path; typed `matchAttributes` + CEL is the break-glass.
- **In-cluster scheduling delegated.** Bin-packing, gang scheduling, fractional GPU, NVLink-aware placement, capacity tracking — Kueue (default) / KAI / Volcano. Modelplane reads capacity signal back via `ClusterQueue.status.flavorsUsage[]`.
- `ModelPlacement` (existing CRD, `apis/modelplacements/`) is the **intermediate representation (IR)** — the seam between the matcher and the version-pinned backend adapter. Not a new abstraction; the role this existing CRD plays.
- Namespace = environment / lifecycle scope. Pushing a revision triggers lifecycle reconciliation in that namespace.

## Stack & substrate

The workload plane is a stack of K8s primitives — Modelplane composes onto it, doesn't reinvent any layer.

```
┌─ Modelplane control plane (Crossplane) ──────────────────┐
│  matcher: (cluster, pool) per replica → ModelPlacement   │
│  backend adapter: ModelPlacement → upstream pod set      │
└──────────────────────────────────────────────────────────┘
        ↓
┌─ Per InferenceCluster (workload plane) ──────────────────┐
│  Backend orchestrator                                    │
│    KServe / Dynamo / raw-vllm                            │
│    renders pods; multi-node LWS; pod-level lifecycle     │
│  Autoscaler                                              │
│    KEDA                                                  │
│    writes ModelDeployment.spec.replicas via scale        │
│    subresource based on the configured signal            │
│  Scheduler / admission                                   │
│    Kueue / KAI / Volcano / (none)                        │
│    gates Workloads, quota, gang scheduling, fractional   │
│  K8s scheduler                                           │
│    binds pods to nodes (or KAI replaces it)              │
│  DRA driver (optional)                                   │
│    runtime grounding when provisioning.mode: dra         │
└──────────────────────────────────────────────────────────┘
```

These layers are **stacked, not alternatives**. KServe is the orchestrator; Kueue is the queue; KEDA is the autoscaler; the K8s scheduler binds pods. Each cluster needs all four (or substitutes — KAI can replace both Kueue and the scheduler; Dynamo replaces KServe; etc.).

**Managed defaults — what we ship under the hood:**

| Layer | Default | What Modelplane does |
|---|---|---|
| Backend | **`managed-kserve`** | Installs KServe at the pinned version + composes the cluster's `KServeBackend`. Per-version adapter renders `LLMInferenceService` from `ModelPlacement`. Example: [`clusters/managed-gke-a3.yaml`](./examples/clusters/managed-gke-a3.yaml). |
| Scheduler | **`managed-kueue`** | Installs Kueue + composes `ClusterQueue` per pool. Reads `ClusterQueue.status.flavorsUsage[]` for capacity signal. |
| Autoscaler | **KEDA** (operator-installed prerequisite) | Modelplane composes a `ScaledObject` from `ModelDeployment.spec.scaling` targeting the MD's scale subresource. KEDA writes `spec.replicas`; composer reconciles `ModelPlacement`s. |

**Knobs we expose (and promise to honor across backends):**

- `parallelism.{tensor, pipeline, expert}` — backend adapter translates to KServe LWS / Dynamo graph / vLLM args
- `roles.{prefill, decode}` — disaggregated serving (xPyD); separate pod sets per role
- `engine.{quantization, speculation, optimizations, advanced[]}` — engine flags + matcher-derived feature requirements
- `adapters[]` — multi-LoRA load + LoRA-aware request routing
- `scaling.{signal, concurrency}` — KEDA `ScaledObject` template (Concurrency today; Utilization, SLO-driven in v2)
- `replicas` (via scale subresource) — KEDA-managed dimension; composer reconciles MPs to match

If a backend can't honor a requested knob (e.g., a backend without expert-parallelism for an MoE workload), the matcher excludes it. Which knobs each backend supports lives on `KServeBackend.spec.engine.features` per cluster.

**Capacity signal — how the matcher avoids saturated clusters.** Each in-cluster scheduler exposes its own queue/quota status; a per-scheduler signal adapter normalizes them into a uniform shape on `InferenceCluster.status.capacity` (`pools[].resources[].{total, used, available}`). Sources by `scheduler.type`:

| Scheduler | Source |
|---|---|
| `managed-kueue` / `kueue` | `ClusterQueue.status.flavorsUsage[]` |
| `kai` | KAI `Queue.status` / `ResourcePool.status` |
| `volcano` | Volcano `Queue.status` |
| `none` | Direct cluster query (list nodes + sum allocatable − requests) |

Works the same for BYOC — kubeconfig already grants read access; the adapter just needs list/get on the scheduler's CRs. Capacity is eventually consistent (few-seconds stale acceptable; we don't reserve, we admit). Cross-cluster admission ordering when two MDs race on the same scarce pool is the existing Open Q. Optional v2: Prometheus / metrics-server integration for actual utilization (TTFT-bottlenecked, not just admission counts).

## Bring your own (BYO) matrix

Four axes — each independent. Mix and match.

| Axis | Field | Values | Examples |
|---|---|---|---|
| **Cluster** | `InferenceCluster.spec.cluster.source` | `GKE` · `EKS` · `AKS` · `Existing` | Modelplane-provisioned: [`managed-gke-a3.yaml`](./examples/clusters/managed-gke-a3.yaml). BYOC: [`byoc-coreweave-h200-dra.yaml`](./examples/clusters/byoc-coreweave-h200-dra.yaml). |
| **Scheduler** | `InferenceCluster.spec.scheduler.type` | `managed-kueue` (default) · `kueue` · `kai` · `volcano` · `none` | Managed: [`managed-gke-a3.yaml`](./examples/clusters/managed-gke-a3.yaml). BYO Kueue: [`byoc-coreweave-h200-dra.yaml`](./examples/clusters/byoc-coreweave-h200-dra.yaml). BYO KAI: [`byoc-coreweave-kai-h200.yaml`](./examples/clusters/byoc-coreweave-kai-h200.yaml). |
| **Backend** | `InferenceCluster.spec.backend.{type, version}` | `managed-kserve` (default) · `kserve` · `dynamo` · `raw-vllm` | `managed-kserve` = Modelplane installs at pinned version. Others = operator's existing install. v1 ships KServe v0.16/v0.17/v0.18 adapters; KAI/Volcano/Dynamo are follow-ups. |
| **InferenceProvider** (SaaS routing target) | `ModelEndpoint.routes[]` | `routes[].inferenceProvider.ref` (registered CR) or `routes[].external.url` (inline) | Registered CR: [`providers/together.yaml`](./examples/providers/together.yaml) referenced from [`endpoints/multi-region.yaml`](./examples/endpoints/multi-region.yaml). |

**KEDA is a prerequisite, not a BYO axis.** The autoscaler is required infrastructure; operator installs it once per cluster. (`managed-keda` could be added later if there's demand to bundle it.) Customers with existing scheduler / backend investments (KAI for training, Volcano for batch, Dynamo for orchestration) keep them; Modelplane sits above and adds the fleet layer.

## Two-stage scheduling: federation vs in-cluster

Modelplane and DRA solve different problems. DRA is a *runtime allocator* — drivers publish `ResourceSlice`s about real hardware; K8s scheduler matches `ResourceClaim`s against them. Modelplane's federation layer schedules against *declared* pool capacity, before nodes exist. Planning, not allocation.

We borrow DRA's vocabulary (typed attributes, domain-prefixed keys, CEL predicates, `device.attributes[domain].name` access pattern); we drop its Kinds (`DeviceClass` / `ResourceSlice` / `ResourceClaim`) at the federation layer.

**Two stages, in order:**

1. **Federation match** (Modelplane control plane, pre-provisioning). `clusterSelector` + `deviceSelector` predicates over declared pool attributes pick `(cluster, pool)` per replica → `ModelPlacement`. **Identical whether the cluster has DRA or not** — federation never reads runtime `ResourceSlice`s.
2. **In-cluster scheduling** (per-cluster, at pod admission). Backend adapter renders pods. K8s scheduler binds them.

**DRA is optional, never required.** Federation match runs against declared pool attributes — same logic whether the cluster has DRA or not. Pick per cluster on `InferenceCluster.spec.provisioning.mode`:

| Mode | When | What in-cluster scheduling does | Example |
|---|---|---|---|
| `device-plugin` | Default for BYOC without DRA. Works on any K8s with the device-plugin model (1.24+). | Backend adapter constrains pods via `nodeSelector` (from `deviceSelector.matchLabels`) + the device-plugin resource (`nvidia.com/gpu: <count>`). Runtime grounding via labels (next paragraph). | [`byoc-eks-h100-no-dra.yaml`](./examples/clusters/byoc-eks-h100-no-dra.yaml) |
| `dra` | K8s 1.34+ with a DRA driver (NVIDIA / ROCm / TPU) — opt-in. | Adapter emits real `ResourceClaim`s carrying the same CEL predicates from `deviceSelector`. DRA driver grounds them against runtime `ResourceSlice`s — catches typos / drift / mis-config at pod admission. Belt-and-suspenders on top of label-based grounding. | [`byoc-coreweave-h200-dra.yaml`](./examples/clusters/byoc-coreweave-h200-dra.yaml) |
| `hybrid` | Cluster has DRA available but some pools stay on device-plugin | Per-pool selection. | — |

**Trust / drift detection without DRA.** The `device-plugin` mode doesn't lose anything load-bearing — federation already evaluated the same predicates against declared attrs. For drift detection (declared vs actual hardware), Modelplane has three paths, in order of effort:

1. **Trust the `InferenceClass`.** If the pool references a class (`h100-nvl-8x`, `mi300x-8x`) and the cluster's `cloud.instanceType` resolves through the class's SKU aliases, the hardware is implied. No introspection needed.
2. **Read standard K8s labels.** The NVIDIA GPU operator (and AMD / NFD equivalents) labels nodes with `nvidia.com/gpu.product`, `nvidia.com/gpu.memory`, `nvidia.com/gpu.compute.major`, etc. A drift controller compares these against the pool's declared `deviceAttributes` and surfaces `CapabilityDrift` conditions on the `InferenceCluster`. No DRA driver required.
3. **Emit DRA `ResourceClaim`s** (mode = `dra`). Strongest grounding; what (1) and (2) approximate. Worth opting into when the cluster already runs a DRA driver.

So — DRA is a nice-to-have for BYOC, not a requirement. [`byoc-eks-h100-no-dra.yaml`](./examples/clusters/byoc-eks-h100-no-dra.yaml) shows the full no-DRA path; works on any K8s with the NVIDIA GPU operator. User-facing API (`clusterSelector` / `deviceSelector`, `engine.*`, `parallelism`, ...) is identical across all modes.

## Fleet-level capabilities

Single-cluster platforms (llm-d, KServe alone, Dynamo) optimize within a cluster. Modelplane reaches across `InferenceCluster`s, with SaaS via `InferenceProvider` routes.

| Capability | What it does | Example |
|---|---|---|
| Fleet matching | One `ModelDeployment` finds eligible clusters across regions, clouds, vendors; `matchTrace` shows why each fits or doesn't | [`workloads/kimi-k2.yaml`](./examples/workloads/kimi-k2.yaml) |
| Hardware-heterogeneous routing | One `ModelEndpoint` weighting across MDs on different hardware, plus `InferenceProvider` routes for SaaS spillover | [`endpoints/assistant.yaml`](./examples/endpoints/assistant.yaml) |
| Geo + compliance routing | EU traffic to EU clusters; SOC 2 traffic only to certified clusters — via `clusterSelector` predicates | [`workloads/kimi-k2-eu.yaml`](./examples/workloads/kimi-k2-eu.yaml) + [`endpoints/multi-region.yaml`](./examples/endpoints/multi-region.yaml) |
| Cross-cluster replica scaling | Replicas of one MD spread across matching clusters; matcher picks per replica from capacity signal | — |
| Fleet KV cache federation | G4 networked cache as a global fabric; route to whichever cluster has the prefix | v2 |
| Fleet session affinity | Sticky sessions across regional ingresses; multi-turn chat lands on the same `(cluster, replica)` | v2 |
| Fleet failover | Active-active / active-passive cutover when a cluster degrades | v2 |
| Cost-aware routing | Cheapest fleet member that fits; blend reserved / on-demand / spot / per-token | v2 |
| Fleet overflow | Burst to a sibling cluster or `InferenceProvider` when local capacity exhausts (#48) | v2 |
| Aggregated fleet observability | TTFT / ITL / cost / queue-depth rolled up across the fleet for one logical service | v2 |

What ships in v1 vs v2 is in the project plan section.

## How users consume it

**ML/App day-one.** Write a `ModelDeployment` (or instantiate a platform Composition like `ApprovedModel` that generates one). Matcher picks an `InferenceCluster` and emits one `ModelPlacement` per replica; the version-pinned adapter renders each MP to one upstream pod set. KEDA writes `MD.spec.replicas`; composer reconciles MPs. Endpoint reachable via `ModelEndpoint`. `matchTrace` shows what was considered and why excluded.

**Platform day-one.** Install Modelplane on the Crossplane control plane → install (or BYO) workload-plane substrate per cluster → create one `InferenceCluster` per cluster (or copy from `examples/clusters/reference/`, each pool referencing an `InferenceClass`) → create `InferenceProvider`s for any SaaS endpoints → optionally author bespoke `InferenceClass`es and Compositions.

## Break-glass scenarios

Where the typed / managed path doesn't fit, the escape hatches:

| Scenario | Break-glass path | Example |
|---|---|---|
| Custom hardware (bespoke AMD partition, internal accelerator) not in the default `InferenceClass` catalog | Author your own `InferenceClass` with the right `expands` attributes; reference from `InferenceCluster.spec.nodePools[].class`. | [`inferenceclasses/`](./examples/inferenceclasses/) (default catalog to copy from) |
| Engine fork with a custom feature (e.g. `acme.com/turbo-mode`) | Add the name to `ModelDeployment.spec.engine.advanced[].name`. Matcher unions it into the required-feature set verbatim — no catalog registration. The cluster's `KServeBackend.spec.engine.features` is the source of truth for support; `matchTrace.suggestions` flags typos via fuzzy-match. | [`workloads/acme-vllm-fork.yaml`](./examples/workloads/acme-vllm-fork.yaml) |
| Constraint not expressible via `matchLabels` (NVLink-domain co-location, MIG state, combined predicates like `vramGiB >= 141 && capabilities contains fp8`) | `deviceSelector.matchAttributes` over the typed attribute vocabulary; `deviceSelector.cel` for full CEL. Federation evaluates against declared pool attrs; in-cluster grounding (where DRA available) emits a real `ResourceClaim`. | [`workloads/kimi-k2.yaml`](./examples/workloads/kimi-k2.yaml) |
| Org-specific match dimension (cost center, team, security clearance) | User-defined `acme.example/*` keys on `InferenceCluster.spec.attributes` + `clusterSelector.matchAttributes`. Pass-through, unvalidated. | [`workloads/qwen3-coder.yaml`](./examples/workloads/qwen3-coder.yaml) |
| Engine flag we don't model | `engine.args` opaque pass-through — CLI flags forwarded as-is to the engine binary. | [`workloads/kimi-k2.yaml`](./examples/workloads/kimi-k2.yaml) |
| Modelplane's matcher / composer policy doesn't fit | Replace via custom Crossplane composition function over the same XRDs. The IR (`ModelPlacement`) is the seam — your function emits MPs; the backend adapter renders them. | — |
| New cloud / SaaS not supported by built-in providers | Custom Crossplane provider that reconciles `InferenceCluster` (new cloud) or `InferenceProvider` (new SaaS). | — |
| Org-specific abstractions (`ApprovedModel`, `ProductionCluster`, governance, defaults) | Crossplane Compositions over `ModelDeployment` and substrate CRDs. | — |

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

**`InferenceProvider` is routing-only.** Never a placement target — the matcher considers only `InferenceCluster`. SaaS routes (Together, Bedrock, Baseten, customer-run KServe) flow through `ModelEndpoint.routes[].inferenceProvider.ref`. One-off URLs go through `routes[].external.url` without registering a CR.

**Namespace = environment.** 0..N of each user-facing resource (`ModelEndpoint`, `ModelDeployment`, `InferenceProvider`, `ModelPlacement`) per namespace. Pushing an MD revision triggers lifecycle reconciliation there. `InferenceClass`es are cluster-scoped — shared infrastructure-level catalog.

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

Workloads match against attributes (e.g. `capabilities contains fp8 && vramGiB >= 141`) — same predicate engine whether the attribute came from a class or was declared inline. Modelplane ships a default catalog under [`examples/inferenceclasses/`](./examples/inferenceclasses/); customers author their own for bespoke hardware. **Decision after 1:1 with Nic** — aligns with K8s class patterns (StorageClass / IngressClass / DeviceClass). The earlier `CapabilityVocabulary` singleton conflated three jobs (ontology + macros + features) into one CR; decomposing it gives each piece its natural home.

**Reference InferenceClusters from cloud SKUs.** Pre-generated `InferenceCluster` templates live under [`examples/clusters/reference/`](./examples/clusters/reference/) — AWS p5, GKE A3 Mega, OCI MI300X, CoreWeave GB300 NVL72. Each `nodePools[].class` references an `InferenceClass`; inline overrides carry the per-cluster host shape (cpu, memory, nics, the provider SKU string). Concrete BYOC and managed configurations (`byoc-*.yaml`, `managed-*.yaml`) sit alongside in `examples/clusters/`. Static today; the follow-up is a Crossplane provider that polls cloud SKU APIs and generates these programmatically.

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

**`KServeBackend.spec.engine` is a proposed extension.** The existing internal XR ([`apis/kservebackends/`](../../apis/kservebackends/)) installs the KServe stack on a cluster but doesn't expose engine-feature declarations today. This design adds a small `spec.engine.features` list — declarative for `byo-kserve` (operator authors), composed by Modelplane for `managed-kserve`. Full proposed shape (mirror of the existing XRD plus the new field) is in [`./xrds/kservebackend.yaml`](./xrds/kservebackend.yaml). Sketch:

```yaml
spec:
  engine:
    features:
      - chunked-prefill
      - prefix-caching
      - multi-lora
      - fp8-kv-cache
      - prefill-decode-disagg
      # ... feature names are the canonical Modelplane vocabulary,
      # plus custom acme.com/* for engine forks
```

The `features` list is the union across whatever engines (vLLM / SGLang / TRT-LLM / TGI) are installed in this cluster — keeps federation matching to set-membership. Lands in `apis/kservebackends/definition.yaml` alongside this design.

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
| `ModelDeployment` chunky for ML/App teams | Crossplane Compositions for org-specific abstractions (`ApprovedModel`-style) — Compositions are implementation, not part of this design preview |

## Open questions

Decisions made and the alternatives Nic can override:

| Decision | Lean | Alternatives |
|---|---|---|
| Default scheduler + backend | `managed-kueue` + `managed-kserve` as defaults, BYO first-class | No defaults (force pick); KAI default for NVIDIA-shop bias |
| Selector dual-path | `matchLabels` (primary) + `matchAttributes` / CEL (break-glass) | Labels only (simpler); attributes only (richer) |
| DRA grounding | Optional, opt-in via `provisioning.mode: dra`. `device-plugin` is the default and works for BYOC without DRA. | Always-on (require DRA on every cluster); federation-only (skip in-cluster grounding entirely even when DRA is available) |
| Rack-scale (NVL72) | Env-level attribute (`cluster.scaleUnit: nvl72`); rack-spanning placements treat the rack as one `nodePool` | Separate `RackInferenceCluster` kind; multi-pool model |
| Reference-cluster rollout | Static YAML now → Crossplane provider polling SKU APIs later | Provider-first; never (operator hand-authors) |
| Hardware ontology | `InferenceClass` per-class CR (StorageClass-style); engine features in matcher code + `KServeBackend` | Singleton `CapabilityVocabulary` (earlier proposal — dropped after 1:1 with Nic); strings in code only (no class CR) |
| `ModelObjective`-style intent layer | Punt past v2 — non-breaking layer above MD if/when needed | Ship in v1 (mirrors Dynamo DGDR/DGD); never |


## What ships v1 vs v2 (themed)

**v1 — Foundation**

| Theme | Scope |
|---|---|
| Substrate | 5 user-facing CRDs (`InferenceCluster`, `InferenceClass`, `ModelDeployment`, `ModelEndpoint`, `InferenceProvider`) + `ModelPlacement` IR; cluster + pool + device attribute layers on `InferenceCluster`; `InferenceClass` catalog (per-SKU bundles); `managed-kueue` install |
| Matching | Two-level selector cascade (`clusterSelector` + `deviceSelector`) over declared pool attributes; typed `matchAttributes` + CEL escape; `matchTrace`; optional DRA grounding for BYOC |
| Workload API | Self-contained `ModelDeployment`; replica == placement (`spec.replicas` + scale subresource); `roles.{prefill, decode}` for xPyD disaggregation; `engine.{quantization, speculation, optimizations, advanced[]}`; five-factor `scaling`; `adapters[]` |
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

Full proposed XRDs and example resources live in [`./`](./). The directory is a **design-time preview**: nothing there is wired up yet — XRDs aren't installed by `up` packs, examples aren't run by CI. Once we align on the API, XRDs move into [`apis/`](../../apis/) (one directory per CRD, alongside the matching Composition) and examples move into the repo-root `examples/`.

**XRDs** (proposed CompositeResourceDefinitions):

- [`xrds/inferencecluster.yaml`](./xrds/inferencecluster.yaml) — cluster-scoped substrate; `nodePools[].class` references an `InferenceClass`
- [`xrds/inferenceclass.yaml`](./xrds/inferenceclass.yaml) — cluster-scoped hardware-bundle class (StorageClass-style); per-SKU
- [`xrds/inferenceprovider.yaml`](./xrds/inferenceprovider.yaml) — namespace-scoped SaaS / external routing target
- [`xrds/modeldeployment.yaml`](./xrds/modeldeployment.yaml) — namespace-scoped workload, K8s scale subresource for KEDA, structured `status.matchTrace`
- [`xrds/modelendpoint.yaml`](./xrds/modelendpoint.yaml) — namespace-scoped weighted routing across `Deployment` / `InferenceProvider` / `External`
- [`xrds/modelplacement.yaml`](./xrds/modelplacement.yaml) — existing CRD playing the role of the intermediate representation (IR); one per logical replica (replica == placement)
- [`xrds/kservebackend.yaml`](./xrds/kservebackend.yaml) — proposed extension to the existing internal `KServeBackend` XR adding `spec.engine.features`

**Substrate examples — clusters** (the BYO matrix in concrete form):

- [`examples/clusters/managed-gke-a3.yaml`](./examples/clusters/managed-gke-a3.yaml) — Modelplane-provisioned GKE; `managed-kueue` + `managed-kserve` + DRA mode
- [`examples/clusters/byoc-coreweave-h200-dra.yaml`](./examples/clusters/byoc-coreweave-h200-dra.yaml) — BYOC; BYO `kueue` + BYO `kserve@v0.18.0` + DRA mode; pool references `h200-nvl-8x` class
- [`examples/clusters/byoc-coreweave-kai-h200.yaml`](./examples/clusters/byoc-coreweave-kai-h200.yaml) — BYOC; BYO **`kai`** scheduler + BYO `kserve` + DRA (NVIDIA NeMo-stack pattern)
- [`examples/clusters/byoc-eks-h100-no-dra.yaml`](./examples/clusters/byoc-eks-h100-no-dra.yaml) — BYOC; BYO `kueue` + BYO `kserve` + **`device-plugin`** mode (no DRA)
- [`examples/clusters/reference/`](./examples/clusters/reference/) — per-SKU templates (AWS p5, GKE A3 Mega, OCI MI300X, CoreWeave GB300 NVL72) customers copy
- [`examples/inferenceclasses/`](./examples/inferenceclasses/) — default `InferenceClass` catalog (H100/H200/B200/B300/MI300X/L40S/A100, in 8x and Grace-4x forms)
- [`examples/providers/together.yaml`](./examples/providers/together.yaml) — Together AI as an `InferenceProvider` routing target

**Workload examples** (ML/App team deployments):

- [`examples/workloads/kimi-k2.yaml`](./examples/workloads/kimi-k2.yaml) — frontier MoE, multi-node (2× 8 H200), 5P3D disaggregation, FP8 weights + KV; typed-attribute predicates
- [`examples/workloads/kimi-k2-eu.yaml`](./examples/workloads/kimi-k2-eu.yaml) — EU-region sibling; multi-region pattern
- [`examples/workloads/qwen3-coder.yaml`](./examples/workloads/qwen3-coder.yaml) — code completion, n-gram speculation, 3 LoRA adapters, user-defined `acme.example/*` attributes
- [`examples/workloads/gpt-oss-20b.yaml`](./examples/workloads/gpt-oss-20b.yaml) — small MoE, scale-to-zero; **labels-first match path** (NVIDIA GPU operator's `nvidia.com/gpu.family` node label)
- [`examples/workloads/acme-vllm-fork.yaml`](./examples/workloads/acme-vllm-fork.yaml) — **`engine.advanced[]` break-glass** for an engine fork with custom features (`acme.com/turbo-mode`)
- [`examples/endpoints/assistant.yaml`](./examples/endpoints/assistant.yaml) — `ModelEndpoint` weighted across deployments + Together routing
- [`examples/endpoints/multi-region.yaml`](./examples/endpoints/multi-region.yaml) — `ModelEndpoint` routing Kimi K2 across us-east-1 + eu-west-1 MDs with Together spillover

**What's deliberately incomplete** (will be filled in during the move to `apis/`):

- `status` schemas are minimal — just conditions + a representative status field per resource. `matchTrace`, `compatibility`, and granular cold-start status will be elaborated when the controller code lands.
- Validation rules (CEL on the schema, `oneOf` discriminator constraints, cross-field invariants) are sketched but not exhaustive.
- The corresponding Crossplane Compositions are not in this directory — those are implementation. The XRDs declare the API contract.
- `KServeBackend` (already an internal XR in `apis/kservebackends/`) is not duplicated here, but `spec.engine.{name, version, features}` is a proposed extension that lands alongside this design — see the "Engine features" section.
- `InferenceProvider` is routing-only by design — the matcher never considers it as a placement candidate.

**Where each XRD lands after alignment:**

| File here | Lands in |
|---|---|
| `xrds/inferencecluster.yaml` | `apis/inferenceclusters/definition.yaml` (replacing `apis/inferenceenvironments/`) |
| `xrds/inferenceclass.yaml` | `apis/inferenceclasses/definition.yaml` |
| `xrds/inferenceprovider.yaml` | `apis/inferenceproviders/definition.yaml` |
| `xrds/modeldeployment.yaml` | `apis/modeldeployments/definition.yaml` (expanded) |
| `xrds/modelendpoint.yaml` | `apis/modelendpoints/definition.yaml` |
| `xrds/modelplacement.yaml` | `apis/modelplacements/definition.yaml` (expanded as the IR) |
| `xrds/kservebackend.yaml` | `apis/kservebackends/definition.yaml` (extended with `spec.engine.features`) |
| `examples/**` | `examples/` at repo root |
