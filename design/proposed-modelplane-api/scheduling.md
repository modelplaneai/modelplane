# Scheduling & Placement

> How Modelplane places workloads across clusters, integrates with in-cluster schedulers (KAI / Kueue), exposes multi-tenancy modes (bin-packing / MIG / time-slicing), and behaves on BYOC clusters.
>
> Operator's reference for what the scheduler does. For *why* it's shaped this way (architecture, IRs, plugin/adapter system, risks, roadmap), see [design.md](./design.md). For the user-facing surface, see [quickstart.md](./quickstart.md) + [advanced.md](./advanced.md).

## TL;DR

- **Two stages, not one.** Modelplane picks `(cluster, pool)` per replica against *declared* pool capacity, **before nodes exist** (federation match). Per-cluster admission, gang scheduling, fractional GPU, NVLink-aware binding — delegated to KAI / Kueue / Volcano (in-cluster).
- **Replica == placement.** One `ModelReplica` per logical replica of a `ModelDeployment`. KEDA writes `MD.spec.replicas`; the composer reconciles MRs to match — no custom autoscaler.
- **Both KAI and Kueue are first-class.** `auto` resolves to `managed-kai` on NVIDIA pools, `managed-kueue` elsewhere. BYOC detects an existing install and uses it.
- **DRA is optional.** `device-plugin` mode is the default for BYOC; `dra` mode is opt-in for stronger runtime grounding. Federation match is identical across modes — the matcher never reads runtime `ResourceSlice`s.
- **Sharing modes are pool-layer decisions.** Bin-packing always on; MIG and time-slicing opt-in per pool. The MD never says "give me MIG".

## Two-stage scheduling: federation vs in-cluster

Modelplane and DRA solve different problems. DRA is a *runtime allocator* — drivers publish `ResourceSlice`s about real hardware; K8s scheduler matches `ResourceClaim`s against them. Modelplane's federation layer schedules against *declared* pool capacity, before nodes exist. **Planning, not allocation.**

We borrow DRA's vocabulary (typed attributes, domain-prefixed keys, CEL predicates, `device.attributes[domain].name` access pattern); we drop its Kinds (`DeviceClass` / `ResourceSlice` / `ResourceClaim`) at the federation layer.

**Two stages, in order:**

1. **Federation match** (Modelplane control plane, pre-provisioning). `clusterSelector` + `deviceSelector` predicates over declared pool attributes pick `(cluster, pool)` per replica → `ModelReplica`. **Identical whether the cluster has DRA or not** — federation never reads runtime `ResourceSlice`s.
2. **In-cluster scheduling** (per-cluster, at pod admission). Backend adapter renders pods. K8s scheduler binds them.

**DRA is optional, never required.** Federation match runs against declared pool attributes — same logic whether the cluster has DRA or not. Pick per cluster on `InferenceCluster.spec.provisioning.mode`:

| Mode | When | What in-cluster scheduling does | Example |
|---|---|---|---|
| `device-plugin` | Default for BYOC without DRA. Works on any K8s with the device-plugin model (1.24+). | Backend adapter constrains pods via `nodeSelector` (from `deviceSelector.matchLabels`) + the device-plugin resource (`nvidia.com/gpu: <count>`). Runtime grounding via labels. | [`byoc-eks-h100-no-dra.yaml`](./examples/clusters/byoc-eks-h100-no-dra.yaml) |
| `dra` | K8s 1.34+ with a DRA driver (NVIDIA / ROCm / TPU) — opt-in. | Adapter emits real `ResourceClaim`s carrying the same CEL predicates from `deviceSelector`. DRA driver grounds them against runtime `ResourceSlice`s — catches typos / drift / mis-config at pod admission. | [`byoc-coreweave-h200-dra.yaml`](./examples/clusters/byoc-coreweave-h200-dra.yaml) |
| `hybrid` | Cluster has DRA available but some pools stay on device-plugin | Per-pool selection. | — |

**Trust / drift detection without DRA.** The `device-plugin` mode doesn't lose anything load-bearing — federation already evaluated the same predicates against declared attrs. For drift detection (declared vs actual hardware), three paths in order of effort:

1. **Trust the `InferenceClass`.** If the pool references a class (`h100-nvl-8x`, `mi300x-8x`) and the cluster's `cloud.instanceType` resolves through the class's SKU aliases, the hardware is implied. No introspection needed.
2. **Read standard K8s labels.** The NVIDIA GPU operator (and AMD / NFD equivalents) labels nodes with `nvidia.com/gpu.product`, `nvidia.com/gpu.memory`, `nvidia.com/gpu.compute.major`, etc. A drift controller compares these against the pool's declared `deviceAttributes` and surfaces `CapabilityDrift` conditions on the `InferenceCluster`. No DRA driver required.
3. **Emit DRA `ResourceClaim`s** (mode = `dra`). Strongest grounding; what (1) and (2) approximate. Worth opting into when the cluster already runs a DRA driver.

User-facing API (`clusterSelector` / `deviceSelector`, `engine.*`, `parallelism`, ...) is identical across all modes.

## Federation-layer scheduling: what Modelplane builds

Stage 1 — what *we* own. Three Crossplane composition functions over the XRDs, plus a per-cluster signal adapter. The in-scope design is deliberately simple: no reservation, no preemption, no learning. (For the design rationale and effort sizing of what we're *not* building, see [design.md > Roadmap](./design.md#roadmap-by-effort-and-order).)

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

That's it. Three multipliers, one tie-break. Not cost-aware, not latency-aware, not learning. Future scoring work plugs into this same function — schemas don't change. (Design rationale for why we kept the scorer this minimal: [design.md > Roadmap](./design.md#roadmap-by-effort-and-order).)

### (3) Backend adapter — IR → upstream object

Per-MR composition function. Reads `MR.spec.target` to find the cluster, reads `MR.spec.kserveVersion` to pick the version-pinned adapter (KServe v0.16 / v0.17 / v0.18 today; Dynamo / raw-vllm later). Renders one `LLMInferenceService` (or backend equivalent) into the target cluster via the cluster's kubeconfig. Crossplane's remote-cluster provider applies it; the LLM-IS reconciler in the cluster takes over from there.

This is the seam that absorbs upstream schema churn — KServe v0.17→v0.18 (storage migration, args→command) is one adapter change, no user-facing changes, no matcher changes. (Why this seam exists: [design.md > IRs](./design.md#what-we-treat-as-ir-and-why-this-matters-for-byo-).)

### (4) Capacity adapter — feedback signal

Per-IC controller (one per scheduler type). Polls the in-cluster scheduler's status CRDs every few seconds, normalizes into `InferenceCluster.status.capacity.pools[].resources[]` (`{name, total, used, available}`). Matcher reads this on the next placement.

| Scheduler | What we poll | Frequency |
|---|---|---|
| `managed-kueue` / `kueue` | `ClusterQueue.status.flavorsUsage[]` | 5s |
| `managed-kai` / `kai` | `Queue.status` + `ResourcePool.status` (per-Project) | 5s |
| `volcano` | `Queue.status` | 5s |
| `none` | `kubectl get nodes` + sum allocatable − requests | 15s |

Eventually consistent. We don't reserve — admission is the cluster's job. A few seconds of staleness is fine; if the matcher picks a saturated cluster, the in-cluster scheduler holds the workload Pending and the next reconcile re-evaluates.

### Why this is simple enough to ship

The whole federation matcher is one composition function reading existing CRs. No new control loop, no new reservation backend, no new state. Deterministic given the IC + capacity snapshot — same inputs, same output. Easy to test (table-driven cases over `(IC fleet, MD selectors) → expected MR.spec.target`); easy to explain to reviewers.

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

Both adapters normalize into `InferenceCluster.status.capacity` so the federation matcher uses one shape. **Knob coverage** — every workload knob exposed by Modelplane (`parallelism`, `roles`, `engine.*`, MIG / time-slicing requests via `deviceSelector`) translates to both backends; the adapter owns the translation. Where coverage diverges (e.g. KAI's hierarchical Projects vs Kueue's `Cohort`), it's a fleet capability — not a per-MD knob — and lives on `InferenceCluster.spec.scheduler.<type>` blocks (follow-up).

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

## BYOC: how scheduling works on a customer-owned cluster

What the scheduler does on a BYOC cluster — and the edge cases.

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
- **Cluster has KServe but the version isn't in our adapter set.** Matcher refuses placement on that cluster with a clear `IC.status.conditions[BackendCompatible]=False, reason=UnsupportedKServeVersion`. New adapters are small to add.
- **Kubeconfig has limited RBAC** (e.g. read-only on `ClusterQueue.status`, no write on `LLMInferenceService`). Onboarding reports the missing permissions on `IC.status.conditions[Ready]=False`. Operator fixes the role and re-reconciles. No silent failures.
- **BYOC cluster's GPU operator labels are stale or missing.** Drift detection raises `IC.status.conditions[CapabilityDrift]=True` but doesn't block placement (declared attributes are still authoritative for federation). The signal exists for the operator to fix; the matcher keeps working.

### Why BYOC works at all

Two architectural decisions make BYOC mechanical, not bespoke:

1. **The matcher reads declared substrate attributes, not runtime state.** Federation match runs against `IC.spec.attributes` and `nodePools[].{node,device}Attributes` — what the operator declared. Whether those attributes were generated by Modelplane (managed) or hand-authored (BYOC), the matcher doesn't care.
2. **Backend / scheduler / capacity adapters all have typed contracts.** Same interface, different implementation. The matcher consumes a `Backend.Render(MR) → object`, `Scheduler.Wrap(workload) → admitted-workload`, `Capacity.Snapshot() → capacity-shape` — none of those care whether the underlying tool was installed or detected.

This is where Crossplane pulls weight: the same Composition pattern that creates managed clusters also wraps existing clusters; the same composition function that renders KServe v0.18 objects works against any cluster running KServe v0.18. (Why the IR pattern enables this so cheaply: [design.md > IRs](./design.md#what-we-treat-as-ir-and-why-this-matters-for-byo-).)

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

Bin-packing happens here. Multiple gpt-oss-20b replicas on the same L40S node share the host (one container per GPU; CPU + RAM bin-packed by kube-scheduler scoring). Time-slicing or MIG is opt-in per-pool, not per-MD — see the multi-tenancy section above.

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

How `replicas` actually goes up and down across the fleet. `scaling.signal: Concurrency, target: 32` is the simplest case; the same loop covers Utilization (vLLM `/metrics`) and SLO-driven (TTFT/ITL).

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

The eviction controller is small (one watcher); writes an annotation, matcher reacts on next reconcile.

**KEDA writes are concurrency-safe.** The scale subresource patches `spec.replicas` only; the composer's MR-set reconcile is idempotent over `spec.replicas`. Two near-simultaneous KEDA writes either both observe the same MR set (one wins, second no-ops) or one observes the other's MRs (correct).

**One backend per cluster, multi-cluster fan-out via the matcher.** The autoscaler doesn't know about clusters. It writes a single number; the federation layer turns that number into placements. Clean separation: KEDA owns "how many"; matcher owns "where".

## Engine features (matcher-side contract)

Engine-feature derivation, the per-cluster `KServeBackend.spec.engine.features` declaration, and the break-glass `engine.advanced[]` list are owned by [#64](https://github.com/modelplaneai/modelplane/pull/64). The scheduler-side rule is simple:

1. Matcher derives a required-feature set from the MD's declared config (`roles` present → `prefill-decode-disagg`; `engine.optimizations.kvCacheRouting: true` → `kv-cache-routing`; `adapters[]` non-empty → `multi-lora`; `engine.quantization.target` contains `kvCache` → `fp8-kv-cache`).
2. Matcher unions any explicit `engine.advanced[].name` entries verbatim (no catalog registration needed — `acme.com/turbo-mode` works as-is).
3. Matcher filters ICs by `KServeBackend.spec.engine.features ⊇ required`. Missing features land in `MR.status.matchTrace` per-cluster, with fuzzy-matched suggestions for typos.

Derivation rules live with the matcher (versioned with Modelplane releases). The canonical feature vocabulary is matcher code + `docs/engine-features.md`. There's no `EngineCatalog` CR.
