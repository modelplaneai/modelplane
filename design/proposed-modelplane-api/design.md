# Modelplane Scheduling — Design

> Architectural decisions, IRs, plugin/adapter system, lifecycle layers, risks, open questions, roadmap.
>
> **Status:** Draft for Bassam + Nic review.
> **Author:** Dennis Ramdass.
> **Date:** 2026-05-07.
>
> **Scope:** the *why* and *what's pluggable*. For the *what the scheduler does* (operator's reference), see [scheduling.md](./scheduling.md). For the user surface, see [quickstart.md](./quickstart.md) and [advanced.md](./advanced.md).
>
> **API shape is owned by [#64](https://github.com/modelplaneai/modelplane/pull/64).** This doc references the user-facing CRDs (`InferenceCluster`, `ModelDeployment`, `ModelReplica`, etc.) by name and behavior; field-by-field schemas land there.

## TL;DR

- **Two stages, not one.** Modelplane is a federation planner: it picks `(cluster, pool)` per replica against *declared* pool capacity, **before nodes exist**. Per-cluster admission, gang scheduling, fractional GPU, NVLink-aware binding — delegated to KAI / Kueue / Volcano. (Behavior detail in [scheduling.md](./scheduling.md).)
- **Replica == placement.** One `ModelReplica` per logical replica of a `ModelDeployment`. KEDA writes `MD.spec.replicas`; the composer reconciles MRs to match — no custom autoscaler.
- **Both KAI and Kueue are first-class.** `auto` resolves to `managed-kai` on NVIDIA pools, `managed-kueue` elsewhere. BYOC detects an existing install and uses it.
- **Plugin/adapter system.** Two user-visible axes (scheduler, backend); four contingent / internal axes. Each axis has a typed contract and pluggable implementations (managed + BYO + detected). `ModelReplica` (the IR) is the seam between the matcher and the version-pinned backend adapter.
- **Three IRs, not one.** `ModelReplica` is explicit (placement). Cluster substrate IR + endpoint binding IR are implicit today, with deliberate trade-offs.
- **Crossplane manages at every layer with a meaningful lifecycle.** Each user-facing CR is its own XR — pause/resume per layer, GitOps drift per layer, RBAC at any layer, version skew handled per cluster.
- **BYOC is symmetric, not a downgrade.** Same matcher, same MR, same adapter selection — Modelplane detects what's installed instead of installing it.
- **Wedge:** the fleet-level capabilities a single-cluster platform can't reach — federated matching, geo + compliance routing, fleet overflow, multi-region replica spread, cost-aware routing (later).

## Design principles

1. **Clean separation, no enforcement.** Platform teams own substrate; ML/App teams own workloads. Same API split or unified.
2. **Fleet-wide by construction.** A `ModelDeployment` targets the fleet of `InferenceCluster`s, not a single cluster. `matchTrace` reports where it fits and why elsewhere doesn't. SaaS endpoints participate via `ModelEndpoint` routing, not placement.
3. **Plain Crossplane customization.** Catalogs, defaults, governance live in Compositions, RBAC, OPA — not Modelplane primitives.
4. **No new in-cluster scheduler.** We're a meta-scheduler. K8s scheduler + DRA, KAI / Kueue (both first-class), KEDA/HPA, Cluster Autoscaler each own their layer.

## Architecture: control plane + fleet

Modelplane is a Crossplane control plane that composes onto a fleet of `InferenceCluster`s. The cluster scope holds shared substrate; each namespace is a lifecycle environment.

```
            Modelplane Control Plane (Crossplane)
   matcher: (cluster, pool) per replica → ModelReplica (IR)
   backend adapter: ModelReplica → upstream objects per cluster
                          ↓
   ─────────────── cluster scope ────────────────
   InferenceClusters (workload planes)
     scheduler (managed-kai on NVIDIA / managed-kueue elsewhere)
     + backend (managed-kserve) + DRA + KEDA on each cluster
   InferenceClass catalog (per-SKU bundles, cluster-scoped)

   ─────────────── namespace scope (= environment) ──
   per namespace: prod / staging / dev / team-A …
     ModelDeployment(s)    (workload spec; scale subresource)
     ModelReplica(s)     (one per replica, IR)
     InferenceProvider(s)       (routing-only target)
     ModelEndpoint         (weighted routing across MDs + IPs)
```

**Cluster scope** holds shared substrate: `InferenceCluster`s and the `InferenceClass` catalog. **Namespace scope** is the lifecycle boundary — each namespace is an environment (prod / staging / dev / per-team) holding workload, routing, and SaaS-target resources. The matcher considers only `InferenceCluster` candidates; `InferenceProvider` is routing-only.

**Key architectural decisions:**

- Meta-scheduler only — compose objects, never bind devices or actuate replicas. This design proposal removes the existing `ClusterModel` / `Model` split (`apis/clustermodels/`, `apis/models/` on main) in favor of a self-contained `ModelDeployment`.
- **Replica == placement.** One `ModelReplica` per logical replica. Each replica independently scheduled by the matcher against the MD's `clusterSelector`. KEDA writes `MD.spec.replicas` via the scale subresource; the composer reconciles MRs to match. No custom scaler.
- **Federation matches against declared pool attributes, not runtime DRA.** `InferenceCluster.spec.nodePools[].{node,device}Attributes` are the source of truth at the federation layer. DRA `ResourceSlice`s ground predicates at the per-cluster scheduling stage.
- **Two-level selector cascade**: `clusterSelector` (env-level) → `deviceSelector` (node + device). Labels are the primary path; typed `matchAttributes` + CEL is the break-glass.
- **In-cluster scheduling delegated.** Bin-packing, gang scheduling, fractional GPU, NVLink-aware placement, capacity tracking — KAI (NVIDIA default) / Kueue (elsewhere) / Volcano. Modelplane ships adapters for both KAI and Kueue (first-class); reads capacity signal back from each.
- `ModelReplica` is the **intermediate representation (IR)** — the seam between the matcher and the version-pinned backend adapter. Renames the existing internal `ModelPlacement` CRD (`apis/modelplacements/` on main) to align with the "replica == placement" mental model.
- Namespace = environment / lifecycle scope. Pushing a revision triggers lifecycle reconciliation in that namespace.

## Stack & substrate

The workload plane is a stack of K8s primitives — Modelplane composes onto it, doesn't reinvent any layer.

```
┌─ Modelplane control plane (Crossplane) ──────────────────┐
│  matcher: (cluster, pool) per replica → ModelReplica   │
│  backend adapter: ModelReplica → upstream pod set      │
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
| Backend | **`managed-kserve`** | Installs KServe at the pinned version + composes the cluster's `KServeBackend`. Per-version adapter renders `LLMInferenceService` from `ModelReplica`. |
| Scheduler | **`auto`** → `managed-kai` (NVIDIA) or `managed-kueue` (other) | We ship adapters for both KAI and Kueue. Auto-resolves at IC reconcile: NVIDIA-only pool → `managed-kai`; other → `managed-kueue`. BYOC: detect existing install (`Project` CRD ⇒ KAI, `ClusterQueue` CRD ⇒ Kueue) and use it; else install `managed-kueue`. |
| Autoscaler | **KEDA** (operator-installed prerequisite) | Modelplane composes a `ScaledObject` from `ModelDeployment.spec.scaling` targeting the MD's scale subresource. KEDA writes `spec.replicas`; composer reconciles `ModelReplica`s. |

**Knobs we expose (and promise to honor across backends):**

- `parallelism.{tensor, pipeline, expert}` — backend adapter translates to KServe LWS / Dynamo graph / vLLM args
- `roles.{prefill, decode}` — disaggregated serving (xPyD); separate pod sets per role
- `engine.{quantization, speculation, optimizations, advanced[]}` — engine flags + matcher-derived feature requirements
- `adapters[]` — multi-LoRA load + LoRA-aware request routing
- `scaling.{signal, concurrency}` — KEDA `ScaledObject` template (Concurrency in scope; Utilization is **S** follow-up; SLO-driven TTFT/ITL is **M**)
- `replicas` (via scale subresource) — KEDA-managed dimension; composer reconciles MRs to match

If a backend can't honor a requested knob (e.g., a backend without expert-parallelism for an MoE workload), the matcher excludes it. Which knobs each backend supports lives on `KServeBackend.spec.engine.features` per cluster.

## Bring your own (BYO) matrix

Four axes — each independent. Mix and match.

| Axis | Field | Values | Examples |
|---|---|---|---|
| **Cluster** | `InferenceCluster.spec.cluster.source` | `GKE` · `EKS` · `AKS` · `Existing` | Modelplane-provisioned: [`managed-gke-a3.yaml`](./examples/clusters/managed-gke-a3.yaml). BYOC: [`byoc-coreweave-h200-dra.yaml`](./examples/clusters/byoc-coreweave-h200-dra.yaml). |
| **Scheduler** | `InferenceCluster.spec.scheduler.type` | `auto` (default) · `managed-kai` · `managed-kueue` · `kai` · `kueue` · `volcano` · `none` | Auto-resolved managed: [`managed-gke-a3.yaml`](./examples/clusters/managed-gke-a3.yaml) → KAI on NVIDIA, [`managed-gke-a3-kai.yaml`](./examples/clusters/managed-gke-a3-kai.yaml) explicit. BYO Kueue: [`byoc-coreweave-h200-dra.yaml`](./examples/clusters/byoc-coreweave-h200-dra.yaml). BYO KAI: [`byoc-coreweave-kai-h200.yaml`](./examples/clusters/byoc-coreweave-kai-h200.yaml). |
| **Backend** | `InferenceCluster.spec.backend.{type, version}` | `managed-kserve` (default) · `kserve` · `dynamo` · `raw-vllm` | `managed-kserve` = Modelplane installs at pinned version. Others = operator's existing install. In scope: KServe v0.16/v0.17/v0.18 adapters. Dynamo adapter is **M**; raw-vllm adapter is **S**; KAI/Volcano are scheduler axis (already first-class). |
| **InferenceProvider** (SaaS routing target) | `ModelEndpoint.routes[]` | `routes[].inferenceProvider.ref` (registered CR) or `routes[].external.url` (inline) | Registered CR: [`providers/together.yaml`](./examples/providers/together.yaml) referenced from [`endpoints/multi-region.yaml`](./examples/endpoints/multi-region.yaml). |

**KEDA is a prerequisite, not a BYO axis.** The autoscaler is required infrastructure; operator installs it once per cluster. (`managed-keda` could be added later if there's demand to bundle it.) Customers with existing scheduler / backend investments (KAI for training, Volcano for batch, Dynamo for orchestration) keep them; Modelplane sits above and adds the fleet layer.

## The plugin/adapter system

Modelplane's value isn't a single new mechanism — it's the **glue** that lets the same `ModelDeployment` work across very different substrates. The glue is a small set of adapter contracts, each pluggable. Same `ModelReplica` lands on any combination.

### How many axes really? — be honest

The current shape surfaces *six* axes, but the count isn't load-bearing — it's a function of where we drew composition seams today. A few will likely collapse:

- **Capacity adapter** is implied by scheduler choice — not user-visible. We could fold it under "scheduler". Surfacing it separately just makes BYO-scheduler easier to extend.
- **Autoscaler** is currently fixed (KEDA prerequisite); it's an axis only if we add HPA-only or vendor-specific autoscalers later.
- **Provisioning mode** could merge with **scheduler** if KAI's roadmap consolidates DRA handling — today they're independent.
- **Cluster source** is really a substrate decision (Crossplane provider) — not an inference choice. Listed here because IC.spec.cluster.source is part of the same XR.

The two **user-visible** axes today are **scheduler** and **backend**. Everything else is contingent — internal seams that simplify composition. If we collapse the count to four (or expand to seven) the matcher / IR / BYO story doesn't change. **Don't read into the number.**

```
                         ModelReplica (the IR — one per logical replica)
                                       │
        ┌──────────────────────────────┴──────────────────────────────┐
        │  user-visible:                                                │
        │    scheduler   backend                                        │
        │  contingent / internal:                                       │
        │    cluster-source · provisioning-mode · capacity · autoscaler │
        └───────────────────────────────────────────────────────────────┘
```

### The axes today

| Axis | Surface | What it picks | Implementations | BYOC detection |
|---|---|---|---|---|
| **Scheduler** | user-visible | what admits / gangs / binds workloads | `managed-kai`, `managed-kueue`; `kai`, `kueue`, `volcano`; `none` | `auto`: Project CRD ⇒ `kai`, ClusterQueue CRD ⇒ `kueue`, neither ⇒ install `managed-kueue` |
| **Backend** | user-visible | what renders pods from the IR | `managed-kserve` at version; `kserve`, `dynamo`, `raw-vllm`. Per-version KServe adapters (v0.16 / v0.17 / v0.18) | Detect KServe / Dynamo CRDs; pin adapter to installed version |
| Cluster source | substrate / contingent | how the K8s cluster comes into being | Crossplane providers per cloud; `Existing` uses a kubeconfig secret | Explicit `IC.spec.cluster.source` discriminator |
| Provisioning mode | substrate / contingent | how devices are exposed in-cluster | `device-plugin` (default for BYOC); `dra` (K8s 1.34+); `hybrid` | Detect `DeviceClass` / `ResourceSlice` presence; default to `device-plugin` |
| Capacity adapter | internal | how `IC.status.capacity` is populated | Per scheduler: ClusterQueue / KAI Queue / Volcano Queue / direct node listing | Implied by scheduler choice |
| Autoscaler | prerequisite | what writes `MD.spec.replicas` | KEDA `ScaledObject` (required) | Verified at IC reconcile, not a choice today |

### How the adapters are implemented

Each adapter is one of three things, picked for fit:

- **Crossplane composition function** — a pure function over the XR graph. Used for: composer (MD ↔ MR set), matcher (MR.spec.target), backend adapter (MR → upstream object), KEDA `ScaledObject` composer. Stateless, deterministic, easy to test (input XR → expected output resources).
- **Crossplane provider** — a controller that reconciles external state. Used for: cluster source (GKE/EKS/AKS), remote-cluster object application, the future cloud-SKU poller for reference clusters.
- **Sidecar / signal-puller controller** — a small controller-runtime pod. Used for: capacity adapter (per-IC, per-scheduler), drift detection (declared `deviceAttributes` vs node labels), eviction signal.

Why this matters: nothing in the plugin system is a Modelplane-specific invention. Composition functions are stock Crossplane; providers are stock Crossplane; controllers are stock controller-runtime. **No new framework.** Adding a backend (e.g. SGLang-server) is one composition function + a `KServeBackend.spec.engine.features` extension; adding a scheduler is one capacity adapter + an admission-gating policy.

### Version-pinned adapters: the seam that absorbs upstream churn

KServe v0.17 → v0.18 changed the worker pod spec (`size`/`template` wrapper → flat `containers`, args → command, storage migration). With one un-pinned adapter, every cluster on a different KServe version breaks on upgrade. With a per-version adapter, the matcher reads `IC.spec.backend.version` and dispatches:

```
ModelReplica  ──▶  matcher selects adapter ──▶  v0.16 renderer
                          │                  │   v0.17 renderer
                          │                  │   v0.18 renderer
                          │                  └─  Dynamo renderer
                          │                       ↓
                          ▼                  upstream object on target cluster
                 IC.spec.backend.version
                 IC.spec.backend.type
```

User-facing surface (the MD spec) doesn't change across KServe versions; the backend adapter absorbs it. Same pattern for Dynamo (graph IR), raw-vllm (Deployment + Service), and any future backend.

### What plugs in where, end-to-end

A worked example showing every axis at once — Modelplane-provisioned GKE A3 with NVIDIA pools:

```
Cluster source     → provider-google reconciles a GKE cluster
Scheduler          → auto → managed-kai; KAI installed via Helm composition
Backend            → managed-kserve at v0.18.0; KServeBackend composed
Provisioning mode  → dra (GKE has the NVIDIA DRA driver)
Capacity adapter   → KAI signal puller populates IC.status.capacity
Autoscaler         → KEDA prerequisite verified
```

Same example, BYOC (CoreWeave H200 with KAI already installed):

```
Cluster source     → kubeconfig-secret reconciler (Existing)
Scheduler          → auto detects Project CRD → kai (use existing)
Backend            → kserve detected at v0.18.0; pin adapter to v0.18
Provisioning mode  → dra detected (DeviceClass CRs present)
Capacity adapter   → KAI signal puller (same code as managed-kai)
Autoscaler         → KEDA prerequisite — operator installs themselves
```

**Same matcher code paths**, same `ModelReplica` shape, same KServe v0.18 renderer. Only difference: who installed what. BYOC isn't a downgrade; it's an alternate set of detected adapters. (BYOC behavior in [scheduling.md > BYOC](./scheduling.md#byoc-how-scheduling-works-on-a-customer-owned-cluster).)

## What we treat as IR (and why this matters for BYO-*)

`ModelReplica` is the IR everyone notices because it's the placement seam. But there's more than one IR in the system, and naming them is what makes BYO-* clean and lifecycle-ops tractable.

### Three IRs, one principle

| IR | What it represents | Producer | Consumer | Today |
|---|---|---|---|---|
| **`ModelReplica`** (placement IR) | one logical replica bound to `(cluster, pool)` plus resolved fields the renderer needs | matcher composition function | per-version backend adapter (KServe v0.16 / v0.17 / v0.18 / Dynamo / raw-vllm) | the explicit XR in `apis/modelreplicas/` |
| **Cluster substrate IR** | the per-cluster install set: scheduler / backend / capacity adapter / KEDA prereq | `InferenceCluster` Composition | per-cluster controllers + matcher (reads `IC.status.{capacity, conditions, detected}`) | implicit in the `InferenceCluster` Composition; no separate XR |
| **Endpoint binding IR** | per-cluster materialization of a `ModelEndpoint` (gateway routes, weights, header rules) | `ModelEndpoint` Composition | per-cluster gateway (Envoy / Istio / Inference Gateway) | implicit in the `ModelEndpoint` Composition today |

The principle behind all three: **stable user-facing CR → IR → version-pinned renderer**. The IR absorbs upstream churn and substrate variation; the user-facing CR doesn't change when KServe rolls a minor version, when a customer brings KAI instead of Kueue, or when we add an Envoy-Gateway target alongside Istio.

### The placement IR (`ModelReplica`) is explicit

This is the one we surface as a concrete CR because:

- The matcher's output is durable state worth inspecting (`kubectl get modelreplicas -n app-team` shows where every replica landed and why).
- Each MR has its own lifecycle (independently reconciled, evictable, sticky across MD revisions).
- The seam between matcher and version-pinned backend adapter is a natural conformance boundary — any future backend renders the same MR.

### The other two are implicit today (and that's a deliberate choice)

**Cluster substrate IR** — what the `InferenceCluster` Composition emits today (Helm releases for Kueue / KAI, KServeBackend XR, capacity-adapter Deployment, KEDA verification). It's an IR by behavior even without a name. We could split it out as `InferenceClusterRuntime` — separate XR for "what got installed on this cluster, at which version, with which detection result". Trades simplicity for finer-grained lifecycle:

| Stay implicit | Split out as `InferenceClusterRuntime` |
|---|---|
| One XR per cluster — simpler mental model | Two XRs per cluster — substrate vs runtime |
| Bumping KServe version in a cluster = mutating IC | Substrate untouched; bump only the runtime IR |
| Detection results live on `IC.status.detected` | Detection results live on the runtime IR's status |

We're not splitting it for now — keeps the BYO matrix readable. **If** version-skew across clusters becomes a real ops issue (e.g. fleet-wide KServe upgrades), promoting it to an explicit IR is one composition change.

**Endpoint binding IR** — same story. Today a `ModelEndpoint` directly composes gateway resources. When fleet-wide routing grows (multi-region weighted routing across N gateways with header-based affinity), splitting per-cluster bindings out as their own IR pays off — each cluster's gateway state becomes its own reconcilable XR with its own conditions.

### Why this matters for BYO-*

The IR pattern is what makes BYO-anything cheap:

- **BYO scheduler** → the scheduler axis picks a different admission-gating + capacity-adapter implementation; same MR, same matcher, same backend adapter. Adding KAI alongside Kueue cost us one capacity adapter + a `schedulerName`-aware MR renderer. No user-facing changes.
- **BYO backend / KServe version** → matcher dispatches to the version-pinned adapter; same MR shape. KServe v0.17→v0.18 storage migration is one adapter change; user MDs stay valid.
- **BYO cluster** → cluster source axis swaps `provider-google` for kubeconfig-secret; the cluster substrate IR detects vs installs. Same MR lands.
- **BYO gateway** (when we get there) → endpoint binding IR's renderer dispatches per gateway type. Same `ModelEndpoint`.

If we hadn't named the IRs, every BYO-* would be touching user-facing composition logic. With them, each BYO-* is one renderer + one detection rule.

## Crossplane lifecycle layers — what gets reconciled where

A second principle behind the design: **let Crossplane manage at every layer that has a meaningful lifecycle**. Each user-facing CR is its own XR, with its own status, conditions, and Composition. That sounds like trivia, but it's what makes ops tractable.

### The layers

| Layer | XR | Lifecycle ops it enables |
|---|---|---|
| **Cluster substrate** | `InferenceCluster` | Provision / decommission a cluster. Pause reconciliation during a maintenance window. Bump scheduler/backend version in one cluster without touching others. GitOps drift detection on cluster-level config. RBAC: only platform team can create. |
| **Hardware catalog** | `InferenceClass` | Add a new SKU bundle; cluster-scoped, shared. RBAC: catalog stewards. Drift detection on aliases / capabilities as cloud SKUs evolve. |
| **Workload** | `ModelDeployment` | Push a new model revision; pause autoscaling; transition env via labels (prod → staging). RBAC: per-namespace = per-team. Independent of substrate. |
| **Replica / placement** | `ModelReplica` | Each replica independently reconciled. Sticky placement survives MD reconciles. Eviction-controller annotation triggers re-pick on cluster degradation. `kubectl get modelreplicas` is the operator's source of truth for where things ran. |
| **Routing** | `ModelEndpoint` | Roll a canary (weight 5 → 95). Pause traffic to a region. RBAC: independent of MD ownership (gateway team can own the ME). |
| **External target** | `InferenceProvider` | Rotate credentials. Move SaaS spend across providers. Per-namespace registration. |

### Why splitting at these seams matters

- **Pause / resume per layer.** `kubectl annotate inferencecluster cw-h200 crossplane.io/paused=true` freezes substrate reconciliation while leaving workloads running. The same on a `ModelDeployment` freezes scaling without affecting the cluster.
- **GitOps drift, per layer.** A `ModelDeployment` is a small CR (model + selectors + scaling); diffing it in PRs is tractable. A combined "everything" CR would be hundreds of lines.
- **RBAC at any layer.** Platform team owns `InferenceCluster` and `InferenceClass`. App teams own `ModelDeployment` in their namespace. SREs own `ModelEndpoint` (traffic shaping). No layer needs cross-team RBAC.
- **Version skew handled per layer.** The matcher reads `IC.spec.backend.version` per cluster — different KServe versions in different clusters are not a problem. The MD doesn't care.
- **Status decomposes naturally.** A failing replica is `MR.status.conditions[Ready]=False, reason=NoSchedulableCluster`. A failing cluster is `IC.status.conditions[Healthy]=False`. A failing gateway is on `ME.status`. Each surface answers one question.
- **Crossplane's claim/composite split applies cleanly.** Per-namespace `Claim` (the user-facing CR) → cluster-scoped `Composite` (the XR Crossplane reconciles) → composed resources. We get this for free at every layer because each layer is its own XR.
- **Each layer is independently testable.** Composition functions for the matcher don't have to mock cluster state; they read declared substrate facts off `InferenceCluster`. The backend adapter doesn't have to know about MDs; it only sees `ModelReplica`. This is a direct consequence of having IRs at the seams.

### What we *don't* split out (and why)

| Not its own XR | Why |
|---|---|
| Per-replica pod set | KServe / LWS owns this; we'd be re-implementing what already exists. |
| Per-cluster KEDA install | KEDA is a hard prerequisite; checking is fine, owning is not. |
| Per-MD `ScaledObject` | Composed inline by the MD's Composition. KEDA's CR is already cluster-tracked; wrapping it would add layers without lifecycle benefit. |
| Per-cluster gateway resources | Today they're composed inline by `ModelEndpoint`. **Will** become an IR when fleet-wide routing grows (see endpoint binding IR above). |

The rule: an IR / XR is justified when there's a real lifecycle to manage at that layer (independent reconcile, RBAC boundary, version skew, drift). Splitting for purity's sake is overhead; we're explicit about which layers get one and which don't.

## API shape — owned by [#64](https://github.com/modelplaneai/modelplane/pull/64)

The full XRD shapes (`ModelDeployment`, `ModelReplica`, `InferenceCluster`, `InferenceClass`, `ModelEndpoint`, `InferenceProvider`) are landing under [PR #64](https://github.com/modelplaneai/modelplane/pull/64) — Nic owns that surface. This doc describes only the contract the **scheduler** has with the API:

| Schedule consumes | From | What it does with it |
|---|---|---|
| `ModelDeployment.spec.replicas` | scale subresource (KEDA-writable) | composer creates / deletes `ModelReplica` children |
| `ModelDeployment.spec.clusterSelector` | env-level predicates | matcher filters `InferenceCluster` candidates |
| `ModelDeployment.spec.deviceSelector` | node + device predicates | matcher filters `nodePools` within surviving ICs |
| `ModelDeployment.spec.parallelism`, `.roles`, `.engine.*`, `.adapters` | declared config | matcher derives required-feature set; backend adapter translates to upstream object |
| `ModelDeployment.spec.scaling` | KEDA template | composer renders a `ScaledObject` targeting the scale subresource |
| `InferenceCluster.spec.attributes`, `.nodePools[].{node,device}Attributes` | declared substrate facts | matcher predicates evaluate against these (federation never reads runtime DRA `ResourceSlice`s) |
| `InferenceCluster.status.capacity` | normalized capacity signal | matcher avoids saturated clusters |
| `KServeBackend.spec.engine.features` (proposed extension) | per-cluster supported features | matcher excludes ICs that don't support the MD's required features |

Field-by-field semantics (validation, defaults, status sub-shapes) are in #64. Where this doc says "matcher reads X", #64 is the source of truth on what X actually looks like.

**Replica == placement.** N replicas → N `ModelReplica`s, each scheduled independently. Multi-node logical replicas (Kimi K2 PP=2) are still ONE MR — multi-pod via LWS within one cluster. Multi-region spread = multiple MDs + multiple `ModelEndpoint` route entries.

**`InferenceProvider` is routing-only.** Never a placement target — the matcher considers only `InferenceCluster`. SaaS routes flow through `ModelEndpoint.routes[].inferenceProvider`.

## Hardware taxonomy — owned by [#64](https://github.com/modelplaneai/modelplane/pull/64)

Hardware vocabulary, `InferenceClass` (StorageClass-style per-SKU bundles), reference cluster templates, and the chip-families catalog land in [#64](https://github.com/modelplaneai/modelplane/pull/64) — grounded in Bassam's "GPU hardware survey and unified taxonomy" (2026-05-07). The scheduler-relevant facts are short:

- The matcher evaluates predicates over **declared pool attributes** at three layers: Cluster (env), Pool (per-host), Device (per-GPU). Same predicate engine whether the attributes were inherited from an `InferenceClass` or declared inline.
- Capability sets (`capabilities: [fp8, fp4, mig, transformer-engine]`) age better than boolean columns. Predicates (`vramGiB >= 141`) age better than equality.
- Federation never reads runtime DRA `ResourceSlice`s — declared attributes are the source of truth at this layer. DRA grounding kicks in at stage 2 (in-cluster), not here.
- The default `InferenceClass` catalog (H100/H200/B200/B300/MI300X/L40S/A100, in 8x and Grace-4x forms) is one of the highest-leverage Modelplane assets — bounded, ongoing, and the wedge for an Upbound-managed offering on top of the OSS default.

## Break-glass — scheduler-relevant escape hatches

UX details (`ApprovedModel`-style abstractions, custom Compositions, the full break-glass matrix) live in [#64](https://github.com/modelplaneai/modelplane/pull/64). The scheduler has three escape hatches a user might hit while placing a workload:

| Scenario | Path | What the matcher does |
|---|---|---|
| Constraint not expressible via labels (NVLink-domain co-location, MIG state, combined predicates) | `deviceSelector.matchAttributes` / `deviceSelector.cel` | Evaluates the predicate over declared pool attrs; if `dra` mode, emits the same predicate as a `ResourceClaim` for runtime grounding |
| Engine fork with a custom feature (`acme.com/turbo-mode`) | `engine.advanced[].name` | Unions name verbatim into required-feature set; filters ICs whose `KServeBackend.spec.engine.features` includes it; suggests fuzzy matches on miss |
| Modelplane's matcher policy doesn't fit (org-specific scoring, custom federation rules) | Replace the matcher composition function over the same XRDs | Your function emits MRs with `spec.target` set; backend adapter renders them. The IR is the seam |

## Fleet-level capabilities

Single-cluster platforms (llm-d, KServe alone, Dynamo) optimize within a cluster. Modelplane reaches across `InferenceCluster`s, with SaaS via `InferenceProvider` routes.

Effort tags here use the same scale as the federation matcher's roadmap (XS ≈ days, S ≈ weeks, M ≈ quarter, L ≈ multi-quarter, XL ≈ year+).

| Capability | Effort | Uncertainty | Example / status |
|---|---|---|---|
| Fleet matching | **in scope** | low | [`workloads/kimi-k2.yaml`](./examples/workloads/kimi-k2.yaml) — multi-cluster eligibility + `matchTrace` |
| Hardware-heterogeneous routing | **in scope** | low | [`endpoints/assistant.yaml`](./examples/endpoints/assistant.yaml) — one ME weighted across MDs on different hardware |
| Geo + compliance routing | **in scope** | low | [`workloads/kimi-k2-eu.yaml`](./examples/workloads/kimi-k2-eu.yaml) + [`endpoints/multi-region.yaml`](./examples/endpoints/multi-region.yaml) |
| Cross-cluster replica scaling | **in scope** | low | Implicit in the matcher loop — see [scheduling.md > Multi-replica autoscaling](./scheduling.md#e-multi-replica-autoscaling--keda--composer--matcher-loop) |
| Fleet overflow (#48) | **S** | low | Burst to a sibling cluster or `InferenceProvider` when local capacity exhausts. Already half-built — the matcher reads capacity; needs the burst-trigger condition + a route-priority knob on ME. |
| Fleet failover (active/passive) | **M** | med | Health signal exists per IC; needs cutover policy + traffic shift on ME (gateway concern, not matcher). |
| Aggregated fleet observability | **M** | low | Rolling up TTFT / ITL / cost / queue-depth per logical service. Mechanically straightforward — wire Prometheus federation + a dashboard. |
| Cost-aware routing | **M** | med | Algorithm is one scorer term; the hard part is sourcing cost (spot pricing, RI amortization). Same as the matcher cost row. |
| Fleet session affinity | **L** | med | Sticky sessions across regional ingresses; multi-turn chat lands on the same `(cluster, replica)`. Needs gateway-side state + a fleet-session protocol. |
| Fleet KV cache federation | **L** | high | G4-style networked cache as a global fabric; LMCache / KVBM. Most uncertain — depends on KV-cache-routing maturity in vLLM and friends. |

## Risks (scheduler-relevant)

**External dependencies — we don't control timing**

| Risk | Mitigation |
|---|---|
| KServe `LLMInferenceService` schema churn (v0.17 args→command; v0.18 storage migration) | `ModelReplica` IR + version-pinned adapter per KServe minor; conformance test suite |
| DRA coverage gap (1.30–1.33 BYO clusters; NIM Operator DRA still Tech Preview) | `provisioning.mode` discriminator on `InferenceCluster`; adapter emits `ResourceClaim` OR `nvidia.com/gpu` |
| Cluster Autoscaler not DRA-aware (pods stuck Pending) | Granular cold-start conditions; DRA-required pools fall back to non-autoscaling until autoscaler maturity catches up |
| KAI / Kueue / Volcano divergent capacity status shapes | Per-scheduler capacity adapter normalizes into one `IC.status.capacity` |
| `ResourceSlice` eventual consistency causes drift flapping | Quorum + 5min duration filter at the drift controller |

**Design tradeoffs — our choices**

| Risk | Mitigation |
|---|---|
| Capacity reservation races (KAI #848 class) | Don't reserve at federation; cluster admission is authoritative |
| Three-autoscaler conflict (KEDA + HPA + WVA) | One autoscaler per replica dimension; KEDA-only initially, WVA layered later |
| Sticky placement strands replicas on degraded clusters | Eviction controller writes annotation; matcher re-picks on annotation change |
| Cross-cluster bin-packing fragmentation | Don't move MRs once placed; let the cluster scheduler bin-pack within itself |

**Operational boundaries — contract with the cluster**

| Risk | Mitigation |
|---|---|
| CRD ownership conflict with KServe upgrades | `kserve` (BYO) and `managed-kserve` install modes; never modify CRDs we didn't author |
| Break-glass engine features no IC supports | `MR.status.matchTrace` carries per-IC missing features + fuzzy suggestions; `Ready=False NoMatchingEngineFeatures` |
| BYOC kubeconfig with insufficient RBAC | Onboarding controller surfaces missing permissions on `IC.status.conditions[Ready]=False` |

## Open questions (scheduler-side)

Scheduler-relevant decisions made and the alternatives reviewers can override. API-shape open questions live in [#64](https://github.com/modelplaneai/modelplane/pull/64).

| Decision | Lean | Alternatives |
|---|---|---|
| Default scheduler | `auto` → `managed-kai` on NVIDIA, `managed-kueue` elsewhere; BYOC detects existing install | Always `managed-kueue` (vendor-neutral); always `managed-kai` (single rich signal); no default (force pick) |
| DRA grounding | Optional, opt-in via `provisioning.mode: dra`. `device-plugin` is the default and works for BYOC without DRA | Always-on (require DRA on every cluster); federation-only (skip in-cluster grounding entirely even when DRA is available) |
| Cross-cluster admission ordering when two MDs race the same scarce pool | Don't reserve at federation; admit-and-let-cluster-handle | Advisory reservation hint (matcher annotates ICs, scheduler honors as preference); cross-cluster two-phase commit (rejected) |
| Re-placement on cluster degradation | Out-of-band eviction controller writes annotation; matcher re-picks on next reconcile | Inline in matcher (more coupling); operator-driven only (no automation) |
| Rack-scale (NVL72) modeling at the matcher | Treat the rack as one `nodePool`; `cluster.scaleUnit: nvl72` is an env attribute | Separate `RackInferenceCluster` kind; multi-pool spanning model |

## Roadmap by effort and order

Effort: **XS** ≈ days, **S** ≈ weeks, **M** ≈ a quarter, **L** ≈ multi-quarter, **XL** ≈ year+ / research.
Uncertainty: **low** (we know how), **med** (open questions), **high** (research-y).
Order: what we should do first / next / later, with the reasoning.

### Foundation — what we ship to make the API real

These are the prerequisites. Without them there's no product to sell. Order is mostly bottom-up: substrate → matcher → adapters → status, because each layer depends on the previous.

| Item | Effort | Uncertainty | Why this order |
|---|---|---|---|
| User-facing CRDs (`InferenceCluster`, `InferenceClass`, `ModelDeployment`, `ModelEndpoint`, `InferenceProvider`) + `ModelReplica` IR | **S** | low | XRD authoring is mechanical; designs already aligned. Land first — everything else watches them. |
| Composer (MD ↔ MR set, replicaIndex, scale-down ordering) | **S** | low | Tiny composition function; needed before anyone can scale. |
| Matcher (filter → score → pick, sticky placement, `matchTrace`) | **M** | low | Heart of the federation layer; algorithm is small but the per-attribute predicate evaluator + match trace export is real work. |
| Capacity adapters (Kueue + KAI signal pullers normalizing into `IC.status.capacity`) | **S** each | low | One per scheduler. Independent — ship them in parallel. |
| KServe v0.16 / v0.17 / v0.18 backend adapters | **M** combined | low | Three version-pinned renderers. Critical: the LWS shape changed v0.17→v0.18 (storage migration, args→command). One person can do all three sequentially. |
| `InferenceClass` default catalog (H100/H200/B200/B300/MI300X/L40S/A100, in 8x and Grace-4x forms) | **S** | low | Static YAML; one engineer can finish in days. Most leverage per hour of work. |
| KEDA `ScaledObject` composer (Concurrency signal) | **XS** | low | Stock template wrapping the MD's scale subresource. |
| DRA + device-plugin emission paths in the adapter | **S** | low | Both modes are required for BYOC. The two predicate→object translations are small; the testing matrix is the work. |
| Drift detection controller (declared `deviceAttributes` vs node labels) | **S** | low | Needed for the no-DRA path to be production-credible. Watch one resource, write one condition. |
| Granular cold-start status conditions (`ProvisioningPool` / `Pulling` / `LWSGangPending` / `EngineLoading`) | **S** | low | Low-risk surface area; biggest UX dividend per LOC. |
| Eviction controller (annotations → matcher re-pick) | **XS** | low | One watcher; supports re-placement on cluster degradation. |

### Near-term follow-ups — clear value, low risk

Things we know how to build, with a clear customer pull as soon as the foundation is out.

| Item | Effort | Uncertainty | When / why |
|---|---|---|---|
| Spread / anti-stacking scoring (don't pile every replica on one IC) | **S** | low | First scoring tweak after first multi-cluster customer. |
| Utilization-driven scaling (vLLM `/metrics`) | **S** | low | KEDA already supports it; just expose the trigger from `MD.spec.scaling`. |
| Prometheus capacity signal (utilization, not just admission counts) | **S** | low | Better signal than `ClusterQueue.flavorsUsage[]` for TTFT-bottlenecked workloads. |
| Fleet overflow (#48) — burst into `InferenceProvider` when local saturated | **S** | low | Half-built: matcher already reads capacity; needs a route-priority knob on ME and a burst-trigger condition. |
| `raw-vllm` backend adapter | **S** | low | Customers running plain vLLM without KServe. Same IR; smaller render. |
| Aggregated fleet observability (TTFT / ITL / cost / queue-depth roll-up) | **M** | low | Mechanically straightforward — Prometheus federation + dashboard. |
| Catalog automation: auto-import from `vllm-project/recipes` | **M** | low | Replaces hand-authored Compositions. |

### Medium-term — bigger lifts with clear demand

Larger pieces we'd build when the customer base demands them. Order is by demand pull, not technical dependency.

| Item | Effort | Uncertainty | Reasoning |
|---|---|---|---|
| SLO-driven scaling (TTFT / ITL targets, WVA integration) | **M** | med | Combined Concurrency + Utilization signals; needs SLO measurement plumbing. Most-asked-for after the foundation. |
| Cost-aware scoring | **M** | med | Algorithm is one term; the **input** is the work — sourcing spot pricing, RI amortization, per-cluster discount tables. Needs a customer to motivate the cost model. |
| Fleet failover (active/passive cutover) | **M** | med | Health signal exists per IC; gateway-side cutover policy is the new bit. Stalls until a customer has a real DR scenario. |
| Two-MD priority / fairness (cross-cluster) | **M** | med | Org-policy CR (`FleetPriority`-style) + matcher integration. Big-customer ask, not foundational. |
| Dynamo backend adapter | **M** | med | Different IR shape (graph vs single LLMInferenceService); needs collaboration with NVIDIA. |
| Compound AI: multi-deployment co-location on one cluster | **M** | med | `ModelDeployment.spec.affinity.coLocateWith` plus matcher logic. Demand from agentic workloads is real but inconsistent. |
| Reference-cluster generator (Crossplane provider polling cloud SKU APIs) | **M** | low | Replaces static YAML; no design unknowns. |

### Long-term — high value, high uncertainty

Things we'd do if it pans out, but the *what* and *how* are not decided.

| Item | Effort | Uncertainty | Reasoning |
|---|---|---|---|
| Fleet KV cache federation (G4 networked, LMCache / KVBM, fleet-wide prefix-aware routing) | **L** | high | The big multi-cluster wedge. Most uncertain because vLLM / SGLang KV-cache-routing maturity is moving fast — premature investment risks rebuilding. |
| Fleet session affinity (sticky sessions across regional ingresses) | **L** | med | Needs a fleet-session protocol + gateway-side state. Implementable today but the wire format is contested. |
| Predictive scaling (forecast vs reactive) | **L** | high | Highest payoff for cold-start-heavy workloads. Needs historical traffic per MD; only works once we have customers running long enough to build a model. |
| Modality expansion (embedding, ASR, TTS, image, video) | **L** combined | med | Each modality is **M** on its own. Schema-level we already cover most; backend adapters are the work. Order driven by customer mix. |
| `ModelObjective` intent layer (TTFT / ITL / cost ceiling, planner reconciles into MDs) | **XL** | high | Mirrors Dynamo DGDR / DGD. Non-breaking layer above MD. Defer until the planner has enough signal to be smarter than a human authoring MDs. |

### Things to keep avoiding

Listed because they're plausible-sounding traps, not because they're roadmap items.

| Anti-item | Why we don't do it |
|---|---|
| Cross-cluster reservation / two-phase commit | Race-prone (KAI #848 class). Cluster admission is authoritative. If drift becomes real, revisit with an *advisory* reservation hint, not authoritative locks. |
| Cross-cluster preemption | We're not the cluster scheduler; preemption-from-outside fights it. Prefer scale-out and capacity over preemption. |
| Learned scoring (RL / online learning) | Scoring as a learned function over (cluster state, workload, outcome). Brittle at K8s timescales; defies debuggability. Keep the scorer hand-written and replaceable. |
| Becoming an in-cluster scheduler | Out of charter. Even if KAI / Kueue both have gaps, our value is the federation layer above them. |

---

## Appendix: deliverables (scheduling-side)

XRDs and full API schemas are owned by [#64](https://github.com/modelplaneai/modelplane/pull/64). This directory keeps scheduling-relevant **examples** to anchor the discussion — illustrative YAML, not authoritative schema.

**Substrate examples — the BYO matrix in concrete form:**

- [`examples/clusters/managed-gke-a3.yaml`](./examples/clusters/managed-gke-a3.yaml) — Modelplane-provisioned GKE; `auto` scheduler resolves to `managed-kai`
- [`examples/clusters/managed-gke-a3-kai.yaml`](./examples/clusters/managed-gke-a3-kai.yaml) — same shape, scheduler pinned `managed-kai` explicitly (auditable)
- [`examples/clusters/byoc-coreweave-h200-dra.yaml`](./examples/clusters/byoc-coreweave-h200-dra.yaml) — BYOC; BYO `kueue` + BYO `kserve@v0.18.0` + DRA
- [`examples/clusters/byoc-coreweave-kai-h200.yaml`](./examples/clusters/byoc-coreweave-kai-h200.yaml) — BYOC; BYO `kai` + BYO `kserve` + DRA
- [`examples/clusters/byoc-eks-h100-no-dra.yaml`](./examples/clusters/byoc-eks-h100-no-dra.yaml) — BYOC; BYO `kueue` + BYO `kserve` + `device-plugin` (no DRA)

Workload examples, `InferenceClass` catalog, reference cluster templates, and the rest of the example tree live alongside in `examples/`; they cross-reference shapes that #64 owns.
