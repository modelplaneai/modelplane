# Modelplane Scheduling & Placement Design

**Status:** Draft for Bassam + Nic review
**Author:** Dennis Ramdass
**Date:** 2026-05-07
**Scope:** The scheduling and placement layer of Modelplane — federation matcher, in-cluster scheduling integration (KAI + Kueue), the plugin/adapter system that ties backends, schedulers, and provisioning modes together, and how the whole thing works under the hood for both managed clusters and BYOC.

> **API shape is owned by [#64](https://github.com/modelplaneai/modelplane/pull/64).** This doc references the user-facing CRDs (`InferenceCluster`, `ModelDeployment`, `ModelReplica`, etc.) by name and behavior; the field-by-field schemas land in #64. Where the two overlap (e.g. `ModelDeployment.spec.scaling`), the contract here describes what the scheduler *consumes*; the full schema is over there.

## TL;DR

- **Two stages, not one.** Modelplane is a federation planner: it picks `(cluster, pool)` per replica against *declared* pool capacity, **before nodes exist**. Per-cluster admission, gang scheduling, fractional GPU, NVLink-aware binding — delegated to KAI / Kueue / Volcano.
- **Replica == placement.** One `ModelReplica` per logical replica of a `ModelDeployment`. KEDA writes `MD.spec.replicas` via the K8s scale subresource; the composer reconciles MRs to match — no custom autoscaler.
- **Both KAI and Kueue are first-class.** `InferenceCluster.spec.scheduler.type: auto` resolves to `managed-kai` on NVIDIA pools (richer fleet signal — gang health, fair-share, hierarchical Projects, native MIG / time-slicing) and `managed-kueue` elsewhere. BYOC detects an existing install and uses it.
- **DRA is optional.** `device-plugin` mode (any K8s with the NVIDIA GPU operator) is the default for BYOC; `dra` mode (K8s 1.34+) is opt-in for stronger runtime grounding. Federation match is identical across modes — the matcher never reads runtime `ResourceSlice`s.
- **Plugin/adapter system.** Six adapter axes — cluster source, scheduler, backend, provisioning mode, capacity signal, autoscaler. Each axis has a typed contract and pluggable implementations (managed + BYO + detected). `ModelReplica` (the IR) is the seam between the matcher and the version-pinned backend adapter.
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
- **Federation matches against declared pool attributes, not runtime DRA.** `InferenceCluster.spec.nodePools[].{node,device}Attributes` are the source of truth at the federation layer. DRA `ResourceSlice`s ground predicates at the per-cluster scheduling stage (next section).
- **Two-level selector cascade**: `clusterSelector` (env-level) → `deviceSelector` (node + device). Labels are the primary path; typed `matchAttributes` + CEL is the break-glass.
- **In-cluster scheduling delegated.** Bin-packing, gang scheduling, fractional GPU, NVLink-aware placement, capacity tracking — KAI (NVIDIA default) / Kueue (elsewhere) / Volcano. Modelplane ships adapters for both KAI and Kueue (first-class); reads capacity signal back from each. See "In-cluster scheduling: KAI and Kueue".
- `ModelReplica` is the **intermediate representation (IR)** — the seam between the matcher and the version-pinned backend adapter. Renames the existing internal `ModelPlacement` CRD (`apis/modelplacements/` on main) to align with the "replica == placement" mental model. Pure rename + role expansion; not a new abstraction.
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
| Scheduler | **`auto`** → `managed-kai` (NVIDIA) or `managed-kueue` (other) | We ship adapters for both KAI and Kueue. Auto-resolves at IC reconcile: NVIDIA-only pool → `managed-kai` (richer fleet signal — gang health, fair-share, hierarchical projects, MIG/time-slicing native); other → `managed-kueue` (vendor-neutral, K8s-SIG-native). BYOC: detect existing install (`Project` CRD ⇒ KAI, `ClusterQueue` CRD ⇒ Kueue) and use it; else install `managed-kueue`. |
| Autoscaler | **KEDA** (operator-installed prerequisite) | Modelplane composes a `ScaledObject` from `ModelDeployment.spec.scaling` targeting the MD's scale subresource. KEDA writes `spec.replicas`; composer reconciles `ModelReplica`s. |

**Knobs we expose (and promise to honor across backends):**

- `parallelism.{tensor, pipeline, expert}` — backend adapter translates to KServe LWS / Dynamo graph / vLLM args
- `roles.{prefill, decode}` — disaggregated serving (xPyD); separate pod sets per role
- `engine.{quantization, speculation, optimizations, advanced[]}` — engine flags + matcher-derived feature requirements
- `adapters[]` — multi-LoRA load + LoRA-aware request routing
- `scaling.{signal, concurrency}` — KEDA `ScaledObject` template (Concurrency in scope; Utilization is **S** follow-up; SLO-driven TTFT/ITL is **M**)
- `replicas` (via scale subresource) — KEDA-managed dimension; composer reconciles MRs to match

If a backend can't honor a requested knob (e.g., a backend without expert-parallelism for an MoE workload), the matcher excludes it. Which knobs each backend supports lives on `KServeBackend.spec.engine.features` per cluster.

**Capacity signal — how the matcher avoids saturated clusters.** Each in-cluster scheduler exposes its own queue/quota status; a per-scheduler signal adapter normalizes them into a uniform shape on `InferenceCluster.status.capacity` (`pools[].resources[].{total, used, available}`). Sources by `scheduler.type`:

| Scheduler | Source |
|---|---|
| `managed-kueue` / `kueue` | `ClusterQueue.status.flavorsUsage[]` |
| `kai` | KAI `Queue.status` / `ResourcePool.status` |
| `volcano` | Volcano `Queue.status` |
| `none` | Direct cluster query (list nodes + sum allocatable − requests) |

Works the same for BYOC — kubeconfig already grants read access; the adapter just needs list/get on the scheduler's CRs. Capacity is eventually consistent (few-seconds stale acceptable; we don't reserve, we admit). Cross-cluster admission ordering when two MDs race on the same scarce pool is the existing Open Q. Follow-up (**S**): Prometheus / metrics-server integration for actual utilization (TTFT-bottlenecked, not just admission counts).

## Bring your own (BYO) matrix

Four axes — each independent. Mix and match.

| Axis | Field | Values | Examples |
|---|---|---|---|
| **Cluster** | `InferenceCluster.spec.cluster.source` | `GKE` · `EKS` · `AKS` · `Existing` | Modelplane-provisioned: [`managed-gke-a3.yaml`](./examples/clusters/managed-gke-a3.yaml). BYOC: [`byoc-coreweave-h200-dra.yaml`](./examples/clusters/byoc-coreweave-h200-dra.yaml). |
| **Scheduler** | `InferenceCluster.spec.scheduler.type` | `auto` (default) · `managed-kai` · `managed-kueue` · `kai` · `kueue` · `volcano` · `none` | Auto-resolved managed: [`managed-gke-a3.yaml`](./examples/clusters/managed-gke-a3.yaml) → KAI on NVIDIA, [`managed-gke-a3-kai.yaml`](./examples/clusters/managed-gke-a3-kai.yaml) explicit. BYO Kueue: [`byoc-coreweave-h200-dra.yaml`](./examples/clusters/byoc-coreweave-h200-dra.yaml). BYO KAI: [`byoc-coreweave-kai-h200.yaml`](./examples/clusters/byoc-coreweave-kai-h200.yaml). |
| **Backend** | `InferenceCluster.spec.backend.{type, version}` | `managed-kserve` (default) · `kserve` · `dynamo` · `raw-vllm` | `managed-kserve` = Modelplane installs at pinned version. Others = operator's existing install. In scope: KServe v0.16/v0.17/v0.18 adapters. Dynamo adapter is **M**; raw-vllm adapter is **S**; KAI/Volcano are scheduler axis (already first-class). |
| **InferenceProvider** (SaaS routing target) | `ModelEndpoint.routes[]` | `routes[].inferenceProvider.ref` (registered CR) or `routes[].external.url` (inline) | Registered CR: [`providers/together.yaml`](./examples/providers/together.yaml) referenced from [`endpoints/multi-region.yaml`](./examples/endpoints/multi-region.yaml). |

**KEDA is a prerequisite, not a BYO axis.** The autoscaler is required infrastructure; operator installs it once per cluster. (`managed-keda` could be added later if there's demand to bundle it.) Customers with existing scheduler / backend investments (KAI for training, Volcano for batch, Dynamo for orchestration) keep them; Modelplane sits above and adds the fleet layer.

## Two-stage scheduling: federation vs in-cluster

Modelplane and DRA solve different problems. DRA is a *runtime allocator* — drivers publish `ResourceSlice`s about real hardware; K8s scheduler matches `ResourceClaim`s against them. Modelplane's federation layer schedules against *declared* pool capacity, before nodes exist. Planning, not allocation.

We borrow DRA's vocabulary (typed attributes, domain-prefixed keys, CEL predicates, `device.attributes[domain].name` access pattern); we drop its Kinds (`DeviceClass` / `ResourceSlice` / `ResourceClaim`) at the federation layer.

**Two stages, in order:**

1. **Federation match** (Modelplane control plane, pre-provisioning). `clusterSelector` + `deviceSelector` predicates over declared pool attributes pick `(cluster, pool)` per replica → `ModelReplica`. **Identical whether the cluster has DRA or not** — federation never reads runtime `ResourceSlice`s.
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

## Federation-layer scheduling: what Modelplane builds

Stage 1 — what *we* own. Three Crossplane composition functions over the XRDs, plus a per-cluster signal adapter. The in-scope design is deliberately simple: no reservation, no preemption, no learning. Each "out of scope" item later in this section has an effort tag for what it'd take to add.

```
                ┌────────── Modelplane control plane (Crossplane) ──────────┐
                │                                                            │
  user writes → │  ModelDeployment    → COMPOSER  → ModelReplica × replicas  │
                │  (replicas: 3)         (1)         (one per logical rep)   │
                │                                       │                    │
                │                                       ▼                    │
                │                                    MATCHER (2)             │
                │                                       │ filter→score→pick  │
                │                                       │ (cluster, pool)    │
                │                                       ▼                    │
                │                              ModelReplica.spec.target      │
                │                                       │                    │
                │                                       ▼                    │
                │                              BACKEND ADAPTER (3)           │
                │                              ─ KServe v0.18 / v0.17 ─      │
                │                                       │                    │
                └───────────────────────────────────────┼────────────────────┘
                                                        ▼
                ┌─ on the target InferenceCluster ─────────────────────────┐
                │  LLMInferenceService → LWS → Pods                         │
                │                                                            │
                │  CAPACITY ADAPTER (4): polls ClusterQueue / KAI Queue,    │
                │  writes IC.status.capacity. Matcher reads this on the     │
                │  next placement.                                           │
                └────────────────────────────────────────────────────────────┘
```

### (1) Composer — replicas ↔ ModelReplicas

Watches `ModelDeployment.spec.replicas`. Maintains exactly N child `ModelReplica`s as a set, with stable `replicaIndex: 0..N-1`. Scale-up: append at the next free index. Scale-down: drop highest index first (oldest replicas survive longest — keeps the gateway endpoint set stable). KEDA writes `replicas`; this composition fires.

### (2) Matcher — pick (cluster, pool) per ModelReplica

Per-MR composition function. Pure, deterministic, runs at MR create + on attribute drift. The whole algorithm:

```
def match(mr: ModelReplica, md: ModelDeployment) -> (cluster, pool):
    # If already bound, keep it (sticky). Re-placement only on hard
    # eviction → handled out-of-band by the eviction controller.
    if mr.spec.target.name:
        return (mr.spec.target.name, mr.spec.target.pool)

    candidates = []
    for ic in list_inference_clusters():
        # Stage A: cluster-level predicates.
        if not eval(md.clusterSelector, ic.spec.attributes):
            trace(ic, reason="clusterSelector failed", details=...)
            continue

        # Stage B: per-pool predicates over declared deviceAttributes.
        for pool in ic.spec.nodePools:
            if not eval(md.deviceSelector, pool.deviceAttributes):
                trace(ic, pool, reason="deviceSelector failed", ...)
                continue

            # Stage C: required-feature set check.
            backend = get_kservebackend(ic)
            required = derive_features(md)               # roles, engine.*, adapters[]
            if not required.issubset(backend.spec.engine.features):
                trace(ic, pool, missingFeatures=required - backend.features)
                continue

            # Stage D: capacity headroom.
            head = headroom(ic.status.capacity, pool.name, md.deviceSelector.requests)
            if head <= 0:
                trace(ic, pool, reason="saturated", available=0)
                continue

            candidates.append(Candidate(ic, pool, score=score(head, ic, mr)))

    if not candidates:
        mr.status = NoMatch(matchTrace=trace.export())
        return

    winner = max(candidates, key=lambda c: c.score)
    mr.spec.target = (winner.ic.name, winner.pool.name)
    mr.spec.derivedFeatures = required
    mr.spec.kserveVersion = winner.ic.backend.version       # adapter pin
```

`score(head, ic, mr)` is intentionally trivial:

```
score = head_score                             # primary: how much room is left
      + spread_bonus(ic, mr)                   # tiny tie-break: prefer ICs the
                                               # parent MD hasn't placed on yet
      + stable_hash(mr.name, ic.name) % 100    # final tie-break: deterministic
```

That's it. Three multipliers, one tie-break. Not cost-aware, not latency-aware, not learning. Future scoring work plugs into this same function — schemas don't change.

### (3) Backend adapter — IR → upstream object

Per-MR composition function. Reads `MR.spec.target` to find the cluster, reads `MR.spec.kserveVersion` to pick the version-pinned adapter (KServe v0.16 / v0.17 / v0.18 today; Dynamo / raw-vllm later). Renders one `LLMInferenceService` (or backend equivalent) into the target cluster via the cluster's kubeconfig. Crossplane's remote-cluster provider applies it; the LLM-IS reconciler in the cluster takes over from there.

This is the seam that absorbs upstream schema churn — KServe v0.17→v0.18 (storage migration, args→command) is one adapter change, no user-facing changes, no matcher changes.

### (4) Capacity adapter — feedback signal

Per-IC controller (one per scheduler type). Polls the in-cluster scheduler's status CRDs every few seconds, normalizes into `InferenceCluster.status.capacity.pools[].resources[]` (`{name, total, used, available}`). Matcher reads this on the next placement.

| Scheduler | What we poll | Frequency |
|---|---|---|
| `managed-kueue` / `kueue` | `ClusterQueue.status.flavorsUsage[]` | 5s |
| `managed-kai` / `kai` | `Queue.status` + `ResourcePool.status` (per-Project) | 5s |
| `volcano` | `Queue.status` | 5s |
| `none` | `kubectl get nodes` + sum allocatable − requests | 15s |

Eventually consistent. We don't reserve — admission is the cluster's job. A few seconds of staleness is fine; if the matcher picks a saturated cluster, the in-cluster scheduler holds the workload Pending and the next reconcile re-evaluates.

### What's out of scope (and effort to add)

Effort sizing — **XS** ≈ days, **S** ≈ weeks, **M** ≈ a quarter, **L** ≈ multi-quarter, **XL** ≈ year+ / research. Uncertainty — **low** (we know how), **med** (some open questions), **high** (research-y).

| Out of scope | Effort | Uncertainty | Why we're not doing it now |
|---|---|---|---|
| Re-placement on cluster degradation | **XS** | low | Tiny eviction watcher writes an annotation; matcher already re-evaluates on annotation change. Order: **first follow-up** — needed for production fleet hygiene. |
| Spread/balance scoring (active anti-stacking, not just headroom) | **S** | low | One scorer term: penalize ICs that already host this MD. Order: **near-term** — improves scale-up behavior visibly. |
| Two-MD priority / fairness across the fleet | **M** | med | Needs an org-policy CR (`FleetPriority` style) plus matcher integration. Order: **after multi-tenant adoption signal** — big-customer ask, not foundational. |
| Cost-aware scoring | **M** | med | Algorithmically simple once cost is a known input; the input is the hard part (spot pricing, reserved-instance amortization, per-cluster discount tables). Order: **after a customer pulls** — keeps us from designing in a vacuum. |
| Cross-cluster preemption | **L** | high | We're not the cluster scheduler. To preempt, we'd need to evict an MR and have its cluster honor that preemption — invasive and races with the cluster scheduler. Order: **don't, prefer scale-out** unless capacity is genuinely capped. |
| Reservation / lock across clusters | **L** | high | Race-prone (KAI #848 class). Two-phase commit across cluster schedulers we don't own. Order: **avoid** — admit-and-let-cluster-handle is the pattern; if drift becomes a real problem, revisit with a hint protocol (advisory reservation, not authoritative). |
| Predictive scaling (forecast vs reactive) | **L** | high | Needs traffic history per MD plus a forecasting model; benefit is highest for cold-start-heavy workloads. Order: **after we have the data** — KEDA history exists but MD-shaped is per-customer. |
| Learned scoring (RL / online learning) | **XL** | high | Scoring as a learned function over (cluster state, workload, outcome). Research-y; brittle at K8s timescales. Order: **don't design for this** — keep the scorer hand-written and replaceable. |

### Why this is simple enough to ship

The whole federation matcher is one composition function reading existing CRs. No new control loop, no new reservation backend, no new state. Deterministic given the IC + capacity snapshot — same inputs, same output. Which makes it easy to test (table-driven cases over `(IC fleet, MD selectors) → expected MR.spec.target`) and easy to explain to reviewers.

The hard work is everywhere else: keeping `InferenceClass` and engine-features taxonomies current, version-pinned KServe adapters, capacity adapters per scheduler. The matcher itself is small and replaceable.

## In-cluster scheduling: KAI and Kueue, both first-class

Stage 2 (in-cluster admission + binding) is where the inference control plane meets reality — gang scheduling for multi-node placements, fractional GPU sharing, MIG/time-slicing knobs, fair-share across tenants. Modelplane ships **adapters for both KAI and Kueue**; either is a complete stack.

**`auto` is the default.** `InferenceCluster.spec.scheduler.type: auto` resolves at IC reconcile:

| Pool composition | Provisioning path | Resolves to | Reason |
|---|---|---|---|
| NVIDIA-only | Modelplane-provisioned | `managed-kai` | Native gang admission, MIG / time-slicing first-class, hierarchical Projects, richer status for the capacity signal |
| Non-NVIDIA (AMD, TPU, Trainium) | Modelplane-provisioned | `managed-kueue` | Vendor-neutral, K8s-SIG-native, scheduling-gate model composes cleanly with kube-scheduler |
| BYOC, KAI installed | Detected (`Project` CRD present) | `kai` | Use what's there; never replace the operator's scheduler |
| BYOC, Kueue installed | Detected (`ClusterQueue` CRD present) | `kueue` | Use what's there |
| BYOC, neither | Greenfield | `managed-kueue` | Safer default — Kueue layered above kube-scheduler is less invasive than KAI's webhook-redirect |

Operators can pin explicitly (`managed-kai` / `managed-kueue` / `kai` / `kueue` / `volcano` / `none`) to lock the choice — see [`managed-gke-a3-kai.yaml`](./examples/clusters/managed-gke-a3-kai.yaml).

**Two interception models — same MD spec lands on either.**

KAI replaces the K8s scheduler. Backend adapter sets `schedulerName: kai-scheduler` on rendered pods (and a mutating webhook does it for any pod that forgot); KAI's `PodGroup` CRD wraps the pod set for gang admission. KAI binds pods to nodes itself, evaluating gang feasibility, fair-share, MIG fragmentation, and NVLink topology in one pass.

Kueue layers above kube-scheduler. Backend adapter sets `spec.suspend: true` (or `kueue.x-k8s.io/queue-name` scheduling-gate) on the rendered Job / Deployment / LWS; Kueue's `Workload` CR wraps it. Once the `ClusterQueue` admits, Kueue ungates the workload; kube-scheduler binds pods normally. Gang-ness is enforced by the workload kind itself (LWS owners create N pods atomically) — Kueue admits the whole `Workload` or none of it.

**What the matcher reads back.**

| Scheduler | Capacity signal | Health signal |
|---|---|---|
| `managed-kai` / `kai` | `Queue.status` / `ResourcePool.status` (per-tenant + per-pool, includes pending gang count) | `PodGroup` conditions per replica |
| `managed-kueue` / `kueue` | `ClusterQueue.status.flavorsUsage[]` (per-flavor totals) | `Workload.status.conditions` per replica |
| `volcano` | `Queue.status` | `PodGroup.status` |
| `none` | List nodes + sum allocatable − requests | Pod conditions only |

Both adapters normalize into `InferenceCluster.status.capacity` so the federation matcher uses one shape. **Knob coverage** — every workload knob exposed by Modelplane (`parallelism`, `roles`, `engine.*`, MIG / time-slicing requests via `deviceSelector`) translates to both backends; the adapter owns the translation. Where coverage diverges (e.g. KAI's hierarchical Projects vs Kueue's `Cohort`), it's a fleet capability — not a per-MD knob — and lives on `InferenceCluster.spec.scheduler.<type>` blocks (**M** follow-up).

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

**Same matcher code paths**, same `ModelReplica` shape, same KServe v0.18 renderer. Only difference: who installed what. BYOC isn't a downgrade; it's an alternate set of detected adapters.

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

## BYOC: how scheduling works on a customer-owned cluster

The BYO matrix earlier shows what plugs in. This section walks through what the scheduler actually does on a BYOC cluster — including the edge cases.

### Onboarding flow

Operator points Modelplane at an existing cluster:

```
1. Operator creates InferenceCluster with cluster.source: Existing,
   cluster.existing.secretRef pointing at a kubeconfig secret.

2. Onboarding controller pings the cluster:
   - lists CRDs to detect scheduler / backend / DRA driver
   - reads a few node labels to validate declared deviceAttributes
   - writes IC.status.detected.{scheduler, backend, provisioning}

3. Operator either accepts the detection (leaves spec.scheduler.type: auto)
   or pins explicitly (spec.scheduler.type: kai, etc.).

4. The scheduler / backend / capacity adapters wire up. The matcher
   becomes willing to place MRs on this IC.

5. status.conditions[Ready] flips True. matcher includes IC in its
   candidate set.
```

No requirement for Modelplane to install anything on the cluster. The kubeconfig needs read access on the scheduler's CRs (`ClusterQueue` / `Project` / `Queue`) and write access on the backend's CR (`LLMInferenceService`). That's it.

### What "managed" means on BYOC

Each axis can be **installed** by Modelplane (managed-*) or **detected** as already-present (BYO). On BYOC, more axes are detected:

| Axis | Managed cluster | BYOC, greenfield | BYOC, has KAI installed | BYOC, has Kueue installed |
|---|---|---|---|---|
| Cluster | provisioned | existing | existing | existing |
| Scheduler | `managed-kai` (NVIDIA) / `managed-kueue` | `managed-kueue` (we install) | `kai` (detected, used) | `kueue` (detected, used) |
| Backend | `managed-kserve` | `managed-kserve` (we install) | detect KServe / Dynamo; pin version | same |
| Provisioning | `dra` | detect; default `device-plugin` | detect | detect |
| Capacity adapter | KAI / Kueue puller | Kueue puller | KAI puller | Kueue puller |

The matcher's behavior is identical across all four columns. Only the install / detection step differs.

### Edge cases the scheduler has to handle

- **No DRA driver on a BYOC cluster.** Federation match runs unchanged (`device-plugin` mode). The backend adapter emits `nodeSelector` + the device-plugin resource (`nvidia.com/gpu: 8`) instead of a `ResourceClaim`. Drift detection falls back to comparing declared `deviceAttributes` against NVIDIA GPU operator node labels (`nvidia.com/gpu.product`, `.gpu.memory`, `.compute.major`).
- **Multiple schedulers in the cluster.** Rare but real (KAI for training queues + Kueue for serving). `IC.spec.scheduler.type` can be set explicitly to pick which one Modelplane integrates with; the other continues to operate on its own workloads.
- **Cluster has KServe but the version isn't in our adapter set.** Matcher refuses placement on that cluster with a clear `IC.status.conditions[BackendCompatible]=False, reason=UnsupportedKServeVersion`. New adapters are **S** to add.
- **Kubeconfig has limited RBAC** (e.g. read-only on `ClusterQueue.status`, no write on `LLMInferenceService`). Onboarding reports the missing permissions on `IC.status.conditions[Ready]=False`. Operator fixes the role and re-reconciles. No silent failures.
- **BYOC cluster's GPU operator labels are stale or missing.** Drift detection raises `IC.status.conditions[CapabilityDrift]=True` but doesn't block placement (declared attributes are still authoritative for federation). The signal exists for the operator to fix; the matcher keeps working.

### Why BYOC works at all

Two architectural decisions make BYOC mechanical, not bespoke:

1. **The matcher reads declared substrate attributes, not runtime state.** Federation match runs against `IC.spec.attributes` and `nodePools[].{node,device}Attributes` — what the operator declared. Whether those attributes were generated by Modelplane (managed) or hand-authored (BYOC), the matcher doesn't care.
2. **Backend / scheduler / capacity adapters all have typed contracts.** Same interface, different implementation. The matcher consumes a `Backend.Render(MR) → object`, `Scheduler.Wrap(workload) → admitted-workload`, `Capacity.Snapshot() → capacity-shape` — none of those care whether the underlying tool was installed or detected.

This is where Crossplane pulls weight: the same Composition pattern that creates managed clusters also wraps existing clusters; the same composition function that renders KServe v0.18 objects works against any cluster running KServe v0.18.

## ModelDeployment placement walkthroughs

What actually happens when an MD lands. Each walkthrough traces: user writes `ModelDeployment` → matcher emits `ModelReplica`(s) → backend adapter renders upstream objects → in-cluster scheduler admits → pods run.

### A. Single-node, single-GPU — small open model on shared hardware

[`workloads/gpt-oss-20b.yaml`](./examples/workloads/gpt-oss-20b.yaml). 20B model, fits on one L40S, scale-to-zero.

```
MD (replicas: 0..3, deviceSelector: 1× L40S, parallelism: TP=1)
 ├─ matcher → 0..N MRs (one per replica; KEDA drives the count)
 │     clusterSelector.matchAttributes filters to clusters with L40S pools
 │     deviceSelector.matchLabels: nvidia.com/gpu.family=ada → labels-first path
 ├─ KServe adapter renders 1× LLMInferenceService per MR (single Deployment, 1 pod)
 ├─ in-cluster admission:
 │     KAI:    PodGroup{minMember:1} → admit → bind to L40S node
 │     Kueue:  Workload wrapping Deployment → ClusterQueue admit → ungate
 └─ pod runs, vLLM serves
```

Bin-packing happens here. Multiple gpt-oss-20b replicas on the same L40S node share the host (one container per GPU; CPU + RAM bin-packed by kube-scheduler scoring). Time-slicing or MIG is opt-in per-pool, not per-MD — see the multi-tenancy section.

### B. Single-node, multi-GPU TP — Llama-70B on 8× H100

70B model fits in one node's NVLink domain; tensor parallelism across 8 GPUs.

```
MD (replicas: 1, deviceSelector: 8× H100, parallelism: TP=8)
 ├─ matcher → 1 MR
 │     deviceSelector.matchAttributes: vramGiB>=80 && interconnect.type=nvswitch
 │     count=8, perNode=8 → must fit single node
 ├─ KServe adapter renders LLMInferenceService with workerSpec
 │     1 pod, 8× nvidia.com/gpu (or DRA ResourceClaim with same predicates)
 ├─ in-cluster admission:
 │     KAI:    PodGroup{minMember:1}, gang trivially of size 1
 │     Kueue:  Workload, single-pod admit
 └─ pod runs, vLLM with TP=8 over NVSwitch
```

Counter-intuitive: **TP=8 is still gang-ness of 1** (one pod, 8 GPUs). The gang scheduler's job is to ensure the pod gets all 8 atomically — `nodeSelector` + `nvidia.com/gpu: 8` does this for free; gang scheduling matters when there are *multiple* pods that must co-schedule.

### C. Multi-node, TP+PP via LeaderWorkerSet — Kimi K2 across 2× 8 H200

[`workloads/kimi-k2.yaml`](./examples/workloads/kimi-k2.yaml). Frontier MoE, doesn't fit one node — needs 16 GPUs split across 2 nodes (TP=8 within node, PP=2 across nodes).

```
MD (replicas: 1..N, deviceSelector: 16× H200, perNode: 8,
     parallelism: TP=8, PP=2, expert: enabled)
 ├─ matcher → 1 MR per replica
 │     deviceSelector.matchAttributes: vramGiB>=141 && capabilities contains fp8
 │                                     && interconnect.type=nvswitch
 │     deviceSelector.constraints: same NVLink domain (intra-node)
 │     network.bandwidthGbps>=400 (inter-node IB / RoCE for PP transfer)
 ├─ KServe adapter (v0.18+) renders LLMInferenceService with workerSpec
 │     emits a LeaderWorkerSet under the hood:
 │       - 1 leader pod (rank-0)
 │       - 1 worker pod (PP stage 2)
 │       - both with 8× H200 each
 │       - LWS guarantees co-creation, shared headless service, ordinal env
 ├─ in-cluster admission:
 │     KAI:   PodGroup{minMember:2} → admit only when 2 nodes free → bind atomically
 │            (failure mode: gang preempts incomplete groups)
 │     Kueue: Workload wraps the LWS; admits the LWS as one unit, kube-scheduler
 │            binds the 2 pods (LWS doesn't create them until admit)
 │            (failure mode: rare partial admission if pod template gates fail)
 └─ Both pods run; vLLM with TP=8/PP=2 + NIXL over the inter-node fabric
```

This is where **scheduler choice matters most**. Both work; KAI's PodGroup observability (gang-ready / partial / starved conditions) makes fleet operations easier — Modelplane surfaces it as `ModelReplica.status.gangHealth`. Kueue's `Workload` model is less granular but composes with anything.

### D. Disaggregated prefill / decode (P/D) — Llama-405B with xPyD

`roles.prefill` and `roles.decode` create separate sub-deployments — different parallelism, different scaling.

```
MD (replicas: 1, roles.prefill={replicas:5, deviceSelector: 8× H200, TP=8},
                  roles.decode={replicas:3,  deviceSelector: 8× H200, TP=8})
 ├─ matcher → 1 MR per replica
 │     emits 8 sub-pod-sets (5 prefill + 3 decode)
 │     all 8 sub-sets must land on the SAME cluster (KV cache transfer)
 ├─ KServe adapter renders 1 LLMInferenceService with disaggregation graph:
 │     prefill pool (5× 1-pod LWS) + decode pool (3× 1-pod LWS)
 │     NIXL endpoint between prefill and decode workers
 ├─ in-cluster admission:
 │     KAI:   one PodGroup per role (or one combined group); gang of 5 + 3
 │            both groups in same Project → fair-share is per-MD not per-role
 │     Kueue: 8 Workloads share one ClusterQueue; admit independently
 │            (rare partial: 5 prefill admit, decode pending → degraded mode
 │             until decode lands; matcher doesn't re-place)
 └─ Pods run; gateway routes prompt → prefill pool → KV → decode pool
```

The matcher does not split prefill / decode across clusters — KV transfer is too expensive over the WAN. The whole 8-pod-set lands on one cluster or none.

### E. Multi-replica autoscaling — KEDA + composer + matcher loop

How `replicas` actually goes up and down across the fleet. `scaling.signal: Concurrency, target: 32` is the simplest case; the same loop covers Utilization (vLLM `/metrics`, **S**) and SLO-driven (TTFT/ITL, **M**).

The four actors and what they each own:

| Actor | Loop period | Reads | Writes |
|---|---|---|---|
| **KEDA `ScaledObject`** | scaling window (default 60s) | the configured trigger (gateway concurrency, vLLM `/metrics`, custom Prometheus) | `MD.spec.replicas` via the scale subresource |
| **Composer** (Crossplane fn over MD) | event-driven on MD | `MD.spec.replicas`, child MR set | creates / deletes `ModelReplica`s to match |
| **Matcher** (Crossplane fn over MR) | event-driven on new MR | `MD.spec.{cluster,device}Selector`, `IC.status.capacity` | `MR.spec.target.{name, pool}`, `MR.spec.kserveVersion` |
| **Backend adapter** (Crossplane fn over MR) | event-driven on MR.spec.target | resolved MR | `LLMInferenceService` onto target cluster |

**Scale-up flow** (one new replica, idle fleet → loaded fleet):

```
T+0s   KEDA: window closes; concurrency 38 > target 32 → write replicas=4
T+0s   Composer: 3 MRs exist (replicaIndex 0..2); replicas=4 → create MR
       with replicaIndex=3
T+0s   Matcher (on new MR-3):
         - filter ICs by clusterSelector (3 candidates pass)
         - filter pools by deviceSelector (each IC has 1 eligible pool)
         - check derived features (all 3 backends support {fp8, kvCache})
         - score by IC.status.capacity headroom
              ic-us-east-1.pool-h200:  4 GPU free of 32 → score 4
              ic-eu-west-1.pool-h200:  16 GPU free of 32 → score 16   ← winner
              ic-ap-south-1.pool-h200: 0 GPU free → eliminated
         - write MR-3.spec.target = (ic-eu-west-1, pool-h200)
T+0s   Backend adapter: render LLMInferenceService onto eu-west-1
T+1s   In-cluster scheduler (KAI / Kueue) admits the LLM-IS
T+5s   LWS materializes; pods Pending if pool was at 0; Cluster Autoscaler
       provisions nodes (cold-start condition surfaced on MR.status)
T+90s  Pods Ready; gateway picks them up; concurrency drops back
T+150s KEDA: next window; concurrency 28 < target 32 → no change
```

**Cross-cluster spread is implicit, not a separate feature.** When ic-us-east-1 saturates, its capacity signal drops; the next MR's matcher scores ic-eu-west-1 higher; the new replica lands in EU. The MD never says "spread me across regions" — the spread is a consequence of the matcher reading capacity. ME-level routing handles user-facing region affinity ([`endpoints/multi-region.yaml`](./examples/endpoints/multi-region.yaml)).

**Scale-down flow** (load drops):

```
T+0s    KEDA: concurrency 8, scaleDownDelay (300s) elapsed → write replicas=2
T+0s    Composer: 4 MRs → drop highest replicaIndex (MR-3, MR-2)
T+0s    Backend adapter: garbage-collect the LLMInferenceServices
T+5s    Cluster Autoscaler reclaims empty nodes (per pool's autoscaling.min)
```

**Sticky placement.** Even if ic-us-east-1's capacity recovers later, the matcher does **not** repack MR-1 into us-east-1 from eu-west-1 just to consolidate. Re-placement is expensive (cold-start + KV cache loss + traffic shift) and not worth it without an explicit signal. Re-placement happens only on hard eviction:

| Trigger | Source | Action |
|---|---|---|
| Cluster degraded | `IC.status.conditions[Healthy]=False` | eviction controller marks affected MRs as `Evicted=True`; matcher re-picks |
| In-cluster scheduler reports `Unschedulable` for >5min | KAI `PodGroup.status` / Kueue `Workload.status` | eviction controller |
| Pool drained / removed | `IC.spec.nodePools[]` change | composer reschedules MRs on the removed pool |

The eviction controller is **XS** (one watcher); writes an annotation, matcher reacts on next reconcile. Listed under federation matcher follow-ups.

**KEDA writes are concurrency-safe.** The scale subresource patches `spec.replicas` only; the composer's MR-set reconcile is idempotent over `spec.replicas`. Two near-simultaneous KEDA writes either both observe the same MR set (one wins, second no-ops) or one observes the other's MRs (correct).

**One backend per cluster, multi-cluster fan-out via the matcher.** The autoscaler doesn't know about clusters. It writes a single number; the federation layer turns that number into placements. Clean separation: KEDA owns "how many"; matcher owns "where".

## Multi-tenancy: bin-packing, MIG, time-slicing

Three orthogonal sharing modes. Each is enabled at the **pool** layer (substrate decision), not the MD layer (workload decision) — workloads request capacity in units the pool advertises.

| Sharing mode | What it is | Where it's enabled | Who decides | When to use |
|---|---|---|---|---|
| **Bin-packing** | Multiple whole-GPU workloads on the same node, scheduler scores tighter packing | Always on (kube-scheduler default; KAI / Kueue / Volcano scoring) | In-cluster scheduler | Default for serving fleets — many small models |
| **MIG** | Hardware partition: one A100 / H100 / H200 advertised as N smaller "instances" (e.g. 7× 1g.10gb) | `nodePool.deviceAttributes.mig: {profile: "1g.10gb", count: 7}` (Modelplane provisions); NVIDIA GPU operator MIG strategy at the node level (BYOC) | Pool admin | Strict isolation between tenants, predictable VRAM |
| **Time-slicing** | Software multiplexing: one GPU advertised as N "replicas" of itself; workloads share via context-switch | `nodePool.deviceAttributes.timeSlicing: {replicas: 4}` + GPU operator timeslicing config | Pool admin | Best-effort dev / experimentation; inference workloads with long idle gaps |

**The MD never says "give me MIG" or "give me time-slicing".** It says "give me a device with vramGiB ≥ 24 and capabilities ⊇ {fp16}". The pool decides whether that device is a whole H100, a `2g.20gb` MIG slice on an H100, or a time-slice of an L40S. The federation matcher matches against `deviceAttributes` whatever they describe.

### Bin-packing in detail

The default. Multiple whole-GPU workloads share a node when CPU / RAM / GPU counts allow. Schedulers differ in **scoring** (which node they prefer when several fit):

- **kube-scheduler** (default): `MostAllocated` policy packs tightly; `LeastAllocated` spreads. Configurable per-cluster.
- **KAI**: `binpack` plugin scores by remaining-fragmentation. NVLink-aware — won't strand a 4-GPU workload on a node with only 2 free GPUs in the same NVLink domain.
- **Kueue**: relies on kube-scheduler scoring for binding; admission ordering (FIFO / fair-sharing) is Kueue-side.

Modelplane doesn't override scoring — that's the in-cluster scheduler's job. We just make sure the same MD lands deterministically: the matcher emits MRs with stable identity, the backend adapter renders pods with stable labels, the scheduler scores them.

**Bin-packing across replicas of the same MD** is intentional: 5 replicas of gpt-oss-20b can co-locate on one 4-GPU L40S node (using time-slicing) or each take a separate L40S in the pool. Cross-MD bin-packing on the same node is the same mechanism — different containers, same scheduler.

### MIG in detail

NVIDIA-specific hardware partitioning. An H100 SXM exposes profiles like `1g.10gb` (×7), `2g.20gb` (×3), `3g.40gb` (×2), `7g.80gb` (×1). Pools either declare a uniform MIG strategy or expose mixed profiles.

Pool side (declared on `InferenceCluster.spec.nodePools[].deviceAttributes`):

```yaml
deviceAttributes:
  vendor: nvidia
  product: H100
  vramGiB: 80                   # whole-GPU number
  mig:
    enabled: true
    profile: "2g.20gb"          # uniform: each device advertised as 3× this
    count: 3
  parentProduct: H100           # marks this as a fractional entry
  vramGiB: 20                   # the slice's effective VRAM
```

In-cluster:
- **DRA mode**: NVIDIA DRA driver publishes `ResourceSlice`s for each MIG instance; backend adapter emits `ResourceClaim` against the typed attributes.
- **Device-plugin mode**: GPU operator advertises `nvidia.com/mig-2g.20gb: 3` per node; backend adapter requests that resource.

Workload side: the MD doesn't change. `deviceSelector.matchAttributes: vramGiB >= 18` matches the slice; the cluster's pool advertises a `vramGiB: 20` slice; the matcher binds. **MIG is invisible at the MD level** — that's the whole point.

KAI's MIG support: native, evaluates fragmentation across slices (won't admit a workload requesting a profile that would fragment the node). Kueue's MIG support: via the standard device-plugin or DRA resources — Kueue counts them as resources in `ClusterQueue.flavors`, doesn't reason about fragmentation.

### Time-slicing in detail

Software-only, no hardware support needed. Pool advertises `nvidia.com/gpu: 4` on a 1-GPU node when `replicas: 4` is configured. CUDA contexts switch on the GPU; throughput, not isolation, is the goal.

```yaml
deviceAttributes:
  vendor: nvidia
  product: L40S
  vramGiB: 48
  timeSlicing:
    enabled: true
    replicas: 4                 # advertise 4× nvidia.com/gpu per physical L40S
```

Use cases (narrow): dev / experimentation / many tiny models with sparse traffic. **Not for production serving** — there's no VRAM isolation; one workload OOMing kills the whole GPU. We surface the mode in `InferenceCluster.status.capacity` so operators can quarantine time-sliced pools to non-prod tiers.

KAI's time-slicing: native scheduling primitive (slice-count-aware). Kueue's time-slicing: relies on the GPU operator config; Kueue counts the advertised replicas as flavored resources.

### Why this lives at the pool layer

Two reasons:

1. **Workloads are portable.** A 20B model declared with `vramGiB >= 24` runs unchanged on a whole L40S, a `2g.20gb` MIG slice, or a time-sliced fraction. Same MD spec, different cluster, different cost / isolation tradeoff.
2. **Sharing policy is platform policy.** Whether a cluster runs MIG, time-slicing, or whole-GPU is a substrate decision — driven by tenant isolation requirements, not workload characteristics. Pushing it into the MD leaks substrate into application code.

The break-glass for workloads that *do* want to dictate (e.g. "I require whole-GPU isolation, never a MIG slice"): `deviceSelector.matchAttributes: parentProduct: ""` (whole-GPU only) or `mig.enabled: false`.

## Fleet-level capabilities

Single-cluster platforms (llm-d, KServe alone, Dynamo) optimize within a cluster. Modelplane reaches across `InferenceCluster`s, with SaaS via `InferenceProvider` routes.

Effort tags here use the same scale as the federation matcher's out-of-scope table (XS ≈ days, S ≈ weeks, M ≈ quarter, L ≈ multi-quarter, XL ≈ year+).

| Capability | Effort | Uncertainty | Example / status |
|---|---|---|---|
| Fleet matching | **in scope** | low | [`workloads/kimi-k2.yaml`](./examples/workloads/kimi-k2.yaml) — multi-cluster eligibility + `matchTrace` |
| Hardware-heterogeneous routing | **in scope** | low | [`endpoints/assistant.yaml`](./examples/endpoints/assistant.yaml) — one ME weighted across MDs on different hardware |
| Geo + compliance routing | **in scope** | low | [`workloads/kimi-k2-eu.yaml`](./examples/workloads/kimi-k2-eu.yaml) + [`endpoints/multi-region.yaml`](./examples/endpoints/multi-region.yaml) |
| Cross-cluster replica scaling | **in scope** | low | Implicit in the matcher loop — see autoscaling walkthrough |
| Fleet overflow (#48) | **S** | low | Burst to a sibling cluster or `InferenceProvider` when local capacity exhausts. Already half-built — the matcher reads capacity; needs the burst-trigger condition + a route-priority knob on ME. |
| Fleet failover (active/passive) | **M** | med | Health signal exists per IC; needs cutover policy + traffic shift on ME (gateway concern, not matcher). |
| Aggregated fleet observability | **M** | low | Rolling up TTFT / ITL / cost / queue-depth per logical service. Mechanically straightforward — wire Prometheus federation + a dashboard. |
| Cost-aware routing | **M** | med | Algorithm is one scorer term; the hard part is sourcing cost (spot pricing, RI amortization). Same as the matcher cost row. |
| Fleet session affinity | **L** | med | Sticky sessions across regional ingresses; multi-turn chat lands on the same `(cluster, replica)`. Needs gateway-side state + a fleet-session protocol. |
| Fleet KV cache federation | **L** | high | G4-style networked cache as a global fabric; LMCache / KVBM. Most uncertain — depends on KV-cache-routing maturity in vLLM and friends. |

## Break-glass — scheduler-relevant escape hatches

UX details (`ApprovedModel`-style abstractions, custom Compositions, the full break-glass matrix) live in [#64](https://github.com/modelplaneai/modelplane/pull/64). The scheduler has three escape hatches a user might hit while placing a workload:

| Scenario | Path | What the matcher does |
|---|---|---|
| Constraint not expressible via labels (NVLink-domain co-location, MIG state, combined predicates) | `deviceSelector.matchAttributes` / `deviceSelector.cel` | Evaluates the predicate over declared pool attrs; if `dra` mode, emits the same predicate as a `ResourceClaim` for runtime grounding |
| Engine fork with a custom feature (`acme.com/turbo-mode`) | `engine.advanced[].name` | Unions name verbatim into required-feature set; filters ICs whose `KServeBackend.spec.engine.features` includes it; suggests fuzzy matches on miss |
| Modelplane's matcher policy doesn't fit (org-specific scoring, custom federation rules) | Replace the matcher composition function over the same XRDs | Your function emits MRs with `spec.target` set; backend adapter renders them. The IR is the seam |

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

## Engine features (matcher-side contract)

Engine-feature derivation, the per-cluster `KServeBackend.spec.engine.features` declaration, and the break-glass `engine.advanced[]` list are owned by [#64](https://github.com/modelplaneai/modelplane/pull/64). The scheduler-side rule is simple:

1. Matcher derives a required-feature set from the MD's declared config (`roles` present → `prefill-decode-disagg`; `engine.optimizations.kvCacheRouting: true` → `kv-cache-routing`; `adapters[]` non-empty → `multi-lora`; `engine.quantization.target` contains `kvCache` → `fp8-kv-cache`).
2. Matcher unions any explicit `engine.advanced[].name` entries verbatim (no catalog registration needed — `acme.com/turbo-mode` works as-is).
3. Matcher filters ICs by `KServeBackend.spec.engine.features ⊇ required`. Missing features land in `MR.status.matchTrace` per-cluster, with fuzzy-matched suggestions for typos.

Derivation rules live with the matcher (versioned with Modelplane releases). The canonical feature vocabulary is matcher code + `docs/engine-features.md`. There's no `EngineCatalog` CR.

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

## User-facing surface preview (Quickstart + Advanced)

Drafted to **gauge complexity** of the proposed design. If these read as straightforward, the scheduling layer is doing its job — federation, plugin/adapter system, IRs, lifecycle layers should all be invisible to the user except via `kubectl describe` when something goes wrong.

### Quickstart — minimum path to a curl

Goal: from "fresh control plane" to "I can curl an LLM" in 4 CRs and one cluster. Reuses an existing K8s cluster (managed-install path is the same; one CR field).

```bash
# 0. Install Modelplane on your Crossplane control plane
$ up xpkg install xpkg.upbound.io/modelplaneai/modelplane:v0.1
# (provider-google + KEDA prereq are dependencies; up resolves them)
```

```yaml
# 1. Register your first cluster (Existing source)
apiVersion: modelplane.ai/v1alpha1
kind: InferenceCluster
metadata:
  name: dev
spec:
  cluster:
    source: Existing
    existing:
      secretRef:
        namespace: platform-system
        name: dev-cluster-kubeconfig
        key: kubeconfig
  scheduler: { type: auto }              # detect; greenfield → managed-kueue
  backend:   { type: managed-kserve, version: v0.18.0 }
  attributes:
    cloud.region: us-east-1
    modelplane.ai/tier: dev
  nodePools:
    - { name: l40s, class: l40s-4x }
```

```yaml
# 2. Deploy a model
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: gpt-oss-20b
  namespace: app-team
spec:
  replicas: 1
  model:  { name: openai/gpt-oss-20b }
  source: HuggingFace
  huggingFace: { repo: openai/gpt-oss-20b }
  deviceSelector:
    requests:
      - { name: gpu, count: 1, matchLabels: { nvidia.com/gpu.family: ada } }
  engine: { name: vLLM, image: vllm/vllm-openai:v0.8.0 }
  scaling:
    signal: Concurrency
    concurrency: { minReplicas: 1, maxReplicas: 4, target: 32 }
```

```yaml
# 3. Route traffic
apiVersion: modelplane.ai/v1alpha1
kind: ModelEndpoint
metadata:
  name: gpt-oss
  namespace: app-team
spec:
  routes:
    - type: Deployment
      weight: 100
      deployment: { ref: { name: gpt-oss-20b } }
```

```bash
# 4. Curl
$ kubectl get modelendpoint gpt-oss -n app-team -o jsonpath='{.status.url}'
https://gpt-oss.app-team.modelplane.example/v1

$ curl https://gpt-oss.app-team.modelplane.example/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"gpt-oss-20b","messages":[{"role":"user","content":"hello"}]}'

# Inspect what happened under the hood
$ kubectl get modelreplicas -n app-team
NAME                READY   TARGET   KIND               AGE
gpt-oss-20b-0       True    dev      InferenceCluster   45s

$ kubectl describe modeldeployment gpt-oss-20b -n app-team | grep -A4 Status
Status:
  Conditions:    Ready=True
  Model Replicas:  total=1 ready=1
  Match Trace:   1 cluster considered, 1 eligible
```

**4 CRs, ~60 lines of YAML.** The matcher, composer, KEDA `ScaledObject`, KServe `LLMInferenceService`, capacity adapter, drift detector are all running but never appear in the user's manifests.

### Advanced — five common scenarios end-users will hit

Each scenario is a delta from the Quickstart. YAML is **abridged** here (full shapes in [#64](https://github.com/modelplaneai/modelplane/pull/64)) — the goal is to show the surface a real workload presents.

#### A. Multi-region weighted routing

Two clusters, two `ModelDeployment`s with the same labels, one `ModelEndpoint` weighted across them. Selector-based — bumping an MD revision doesn't break the URL.

```yaml
# Two MDs share labels — environment promotion pattern
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: kimi-k2-us
  namespace: app-team
  labels:
    modelplane.ai/model: kimi-k2
    modelplane.ai/region: us-east-1
    modelplane.ai/environment: production
spec:
  replicas: 2
  model: { name: moonshotai/Kimi-K2-Instruct }
  source: HuggingFace
  huggingFace: { repo: moonshotai/Kimi-K2-Instruct }
  clusterSelector:
    matchAttributes:
      cloud.region: us-east-1
      network.bandwidthGbps: ">=400"
  deviceSelector:
    requests:
      - name: gpus
        count: 16
        perNode: 8
        matchAttributes: { vramGiB: ">=141", capabilities: [fp8] }
  parallelism: { tensor: 8, pipeline: 2 }
  engine: { name: vLLM, image: vllm/vllm-openai:v0.8.0 }
---
# kimi-k2-eu: same shape, region: eu-west-1
---
apiVersion: modelplane.ai/v1alpha1
kind: ModelEndpoint
metadata:
  name: kimi-k2-global
  namespace: app-team
spec:
  routes:
    - type: Deployment
      weight: 50
      deployment:
        selector:
          matchLabels:
            modelplane.ai/model: kimi-k2
            modelplane.ai/region: us-east-1
            modelplane.ai/environment: production
    - type: Deployment
      weight: 50
      deployment:
        selector:
          matchLabels: { modelplane.ai/model: kimi-k2, modelplane.ai/region: eu-west-1 }
```

#### B. BYOC with KAI scheduler

Operator points Modelplane at an existing CoreWeave H200 cluster running KAI. **No install** — Modelplane detects KAI's `Project` CRD and uses it.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceCluster
metadata:
  name: cw-kai-h200
spec:
  cluster:
    source: Existing
    existing:
      secretRef: { namespace: platform-system, name: cw-kubeconfig, key: kubeconfig }
  scheduler: { type: auto }            # auto detects Project CRD → kai
  backend:   { type: kserve, version: v0.18.0 }   # detected, BYO
  provisioning: { mode: dra }
  attributes:
    cloud.provider: coreweave
    cloud.region: us-east-1
    network.fabric: ib
  nodePools:
    - { name: kai-pool-h200, class: h200-nvl-8x }
```

`kubectl describe inferencecluster cw-kai-h200` shows `Status.Detected: { scheduler: kai, backend: kserve@v0.18.0, dra: true }`. Onboarding flips Ready=True; the matcher starts placing MRs on this IC. Same `ModelDeployment` from scenario A would land here unchanged.

#### C. Disaggregated prefill / decode (xPyD)

Add `roles.prefill` and `roles.decode` to a `ModelDeployment`. Backend adapter renders separate sub-pod-sets that all land on the same cluster (KV cache transfer too expensive over WAN).

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata: { name: llama-405b, namespace: app-team }
spec:
  replicas: 1
  model: { name: meta/Llama-3.1-405B }
  source: HuggingFace
  huggingFace: { repo: meta-llama/Meta-Llama-3.1-405B }
  deviceSelector:
    requests:
      - { name: gpus, count: 8, perNode: 8, matchAttributes: { vramGiB: ">=141" } }
  parallelism: { tensor: 8 }
  roles:
    prefill: { replicas: 5 }            # 5 prefill pods, inherits root selector
    decode:  { replicas: 3 }            # 3 decode pods
  engine:
    name: vLLM
    image: vllm/vllm-openai:v0.8.0
    optimizations: { kvCacheRouting: true }
```

The MD doesn't say anything about NIXL / KV transfer / gang admission — backend adapter handles the wiring. `kubectl get modelreplicas` shows one MR; the cluster shows 8 pods (5 prefill + 3 decode) with co-location enforced by the in-cluster scheduler.

#### D. Custom hardware via `InferenceClass`

Bespoke AMD MI325X partition not in the default catalog. Cluster-scoped class declared once; clusters reference it by name.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceClass
metadata: { name: acme-mi325-2x }
spec:
  expands:
    vendor: amd
    product: MI325
    architecture: cdna3
    formFactor: oam
    vramGiB: 256
    capabilities: [fp8, fp4]
    gpuCount: 2
    interconnect.type: infinity-fabric
  aliases: [acme:internal-mi325-2x]
---
apiVersion: modelplane.ai/v1alpha1
kind: InferenceCluster
metadata: { name: acme-mi325 }
spec:
  cluster: { source: Existing, existing: { secretRef: ... } }
  scheduler: { type: auto }
  backend:   { type: managed-kserve, version: v0.18.0 }
  nodePools:
    - { name: mi325-pool, class: acme-mi325-2x }   # references the class
```

A workload requesting `vramGiB >= 200 && capabilities contains fp8` matches without any MD-level changes. Adding new SKUs is one CR, not a code change.

#### E. Spillover to a SaaS provider

Local cluster saturates → burst to Together AI. `InferenceProvider` for the SaaS endpoint, weighted route on the ME.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceProvider
metadata:
  name: together-prod
  namespace: app-team
  labels: { modelplane.ai/role: spillover }
spec:
  endpoint:
    url: https://api.together.xyz/v1
    auth: { secretRef: { namespace: platform-system, name: together-key, key: api-key } }
---
apiVersion: modelplane.ai/v1alpha1
kind: ModelEndpoint
metadata: { name: kimi-k2-with-burst, namespace: app-team }
spec:
  routes:
    - type: Deployment
      weight: 95
      deployment: { selector: { matchLabels: { modelplane.ai/model: kimi-k2 } } }
    - type: InferenceProvider
      weight: 5
      inferenceProvider: { selector: { matchLabels: { modelplane.ai/role: spillover } } }
```

When the matcher reports the local fleet at capacity (`InferenceCluster.status.capacity` saturated), the gateway shifts traffic to the spillover route. No `ModelDeployment` change.

### What this tells us about complexity

Reading the YAML for both surfaces together:

- **3 CRs the user always writes** (`InferenceCluster`, `ModelDeployment`, `ModelEndpoint`). 1 more for SaaS routes (`InferenceProvider`); 1 more for custom hardware (`InferenceClass`).
- **The MD is the only chunky resource.** ~30-50 lines for a typical workload. Most of that is engine config and selectors that are inherent to inference, not Modelplane-specific.
- **Advanced scenarios are *deltas***, not redesigns. Multi-region is "add a label, copy the MD, write the ME". Disagg is "add `roles`". BYOC with KAI is "set `source: Existing`". Spillover is one extra route entry.
- **No user touches**: `ModelReplica`, capacity status, `KServeBackend`, `ScaledObject`, `LLMInferenceService`, `LeaderWorkerSet`, `ResourceClaim`, `ClusterQueue`, `PodGroup`, scheduler / capacity adapters, the matcher. All internal mechanics.

If MD spec sprawl is the complexity risk, the mitigation is org-specific Compositions on top — `ApprovedModel`-style abstractions that compress 50 lines of MD into a 5-line claim. That's stock Crossplane and lives alongside Modelplane, not inside it.

The places where complexity *can* leak:

1. **`matchTrace` debugging.** When no IC matches, the user reads `MR.status.matchTrace`. The shape needs to be readable. Currently designed as structured per-cluster missing-features + suggestions; we'll iterate based on real misses.
2. **Cold-start ambiguity.** A `ModelDeployment` with `Ready=False` could mean "pulling image", "pool scaling from 0", "gang scheduling pending", "engine loading weights". We commit to granular conditions on the MR (`MR.status.conditions[Pulling]`, `[LWSGangPending]`, `[EngineLoading]`) so users see *which* cold-start stage they're in.
3. **`engine.advanced[]` typos.** `acme.com/turbo-mode` vs `acme.com/trubo-mode`. Fuzzy-match suggestions in `matchTrace.suggestions` cover this; users see the typo flagged.

These are the places to invest in UX once the foundation lands.
