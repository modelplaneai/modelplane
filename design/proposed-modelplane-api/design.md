# Modelplane Scheduling — Design

> Federation matcher + renderer composition functions. **API shape is owned by [#64](https://github.com/modelplaneai/modelplane/pull/64)** — this doc points at the implementation that consumes it.
>
> **Status:** sketch. The code under `functions/` doesn't run yet — it targets API protos that haven't been generated. The shape, dependencies, and use cases are real; the wiring is gated on #64 landing.

## Architecture

Two Crossplane composition functions, one IR between them.

```
                                                      cluster scope  ──────────────────────────────┐
                                                                                                    │
  ml team writes  ──▶  ModelDeployment  ┐                                                           │
                                         │                                                          │
                          ┌──────────────┴──────────────┐                                           │
                          │   compose-model-deployment  │  ◀─── reads ────  InferenceCluster (×N)   │
                          │   ── matcher + composer ──  │  ◀─── reads ────  InferenceClass (×M)     │
                          │                             │  ◀─── reads ────  ModelReplica owned (×R) │
                          └──────────────┬──────────────┘                                           │
                                         │                                                          │
                            ModelReplica × spec.replicas       (the IR — placement decisions)       │
                            ModelEndpoint × spec.replicas      (one per replica, per Nic's design)  │
                                         │                                                          │
                          ┌──────────────┴──────────────┐                                           │
                          │   compose-model-placement   │  ◀─── reads ────  matched InferenceCluster│
                          │   ── renderer ──            │  ◀─── reads ────  matched InferenceClass(es)
                          └──────────────┬──────────────┘                                           │
                                         │                                                          │
                                         ▼                                                          │
                              KServe LLMInferenceService    (on the target cluster, via            │
                              + DRA ResourceClaim(s)         remote-cluster Object provider)       │
                                                                                                    │
                                                                                                    └──
```

## What lives where

| Concern | File | What it does |
|---|---|---|
| **Federation matcher** | [`functions/compose-model-deployment/scheduling.py`](../../functions/compose-model-deployment/scheduling.py) | Pure-Python algorithm: filter ICs by `clusterSelector.matchLabels`; filter pools by `nodeSelector.cel` against class capabilities; capacity check with sticky-placement accounting; score and pick per replica. Plain dataclasses — runs without any of Modelplane's protos. |
| **Composer** (MD → MR set) | [`functions/compose-model-deployment/main.py`](../../functions/compose-model-deployment/main.py) | Crossplane glue: declares required-resources (clusters, classes, existing replicas), calls `scheduling.match()`, emits `ModelReplica` × `spec.replicas` + `ModelEndpoint` × `spec.replicas`, sets MD status conditions. |
| **Renderer** (MR → KServe) | [`functions/compose-model-placement/main.py`](../../functions/compose-model-placement/main.py) | Reads MR + matched IC + classes; builds a KServe `LLMInferenceService` (decode + optional prefill) + DRA `ResourceClaim`s + scheduler-companion objects on the target cluster via the kubeconfig provider; lifts cold-start conditions back. |
| **Scheduler dispatch** | [`functions/compose-model-placement/scheduler.py`](../../functions/compose-model-placement/scheduler.py) | Per-scheduler wrap: KAI (schedulerName + PodGroup), Kueue (queue label + suspend gate), none (pass-through). Pluggable via a single dispatch table. |
| **Capacity adapter** | [`lib/capacity_adapter/`](../../lib/capacity_adapter/) | Per-scheduler status pullers: `kai.py` (KAI Queue/ResourcePool → CapacitySnapshot), `kueue.py` (ClusterQueue.flavorsUsage → CapacitySnapshot), shared types in `common.py`. Runs as a separate controller, not a composition function. |

The matcher is deliberately isolated in `scheduling.py` so it can be tested with table-driven cases over `(IC fleet, MD selectors) → expected placements` without touching Crossplane.

## Dependencies — what each function reads / writes

**`compose-model-deployment`**

| Direction | Resource | Why |
|---|---|---|
| reads | `InferenceCluster` (all, cluster-scoped) | candidate fleet |
| reads | `InferenceClass` (referenced by pools) | capabilities for CEL eval |
| reads | `ModelReplica` (owned by this MD) | sticky placement + capacity used |
| writes | `ModelReplica` × `spec.replicas` | the IR |
| writes | `ModelEndpoint` × `spec.replicas` | reachable URL surface (per #64) |
| writes | MD status conditions | `Scheduled` / `ReplicasReady` / matchTrace |

**`compose-model-placement`**

| Direction | Resource | Why |
|---|---|---|
| reads | `InferenceCluster` (just the matched one) | kubeconfig + pool→class mapping |
| reads | `InferenceClass` × {decode, prefill} | derive DRA selector from capabilities |
| writes | `LLMInferenceService` (on target cluster) | the actual workload |
| writes | `ResourceClaim` × roles (on target cluster) | DRA device binding |
| writes | MR status conditions | `Ready` / `Pulling` / `LWSGangPending` / `EngineLoading` |

KEDA `ScaledObject`s are user-authored per Nic's design (mirroring Deployment + HPA) — not composed by Modelplane. Modelplane only exposes `MD.spec.replicas` via the scale subresource.

## KAI / Kueue integration

Stage-2 (in-cluster) scheduling. Two interception models, dispatched per-cluster.

**API extension to [#64](https://github.com/modelplaneai/modelplane/pull/64).** Nic's sketch doesn't model a scheduler axis. We propose adding `InferenceCluster.spec.scheduler.{type}` with values `auto` (default) · `managed-kai` · `managed-kueue` · `kai` · `kueue` · `none`. The renderer dispatches on this. `auto` resolves at IC onboarding by detecting CRDs (`Project` ⇒ KAI, `ClusterQueue` ⇒ Kueue, neither ⇒ install `managed-kueue`).

### What changes per scheduler

| | KAI | Kueue | none |
|---|---|---|---|
| **Pod-level** | `schedulerName: kai-scheduler` on every pod the LLM-IS produces | unchanged | unchanged |
| **Workload-level** | unchanged | `kueue.x-k8s.io/queue-name` label + `suspend: true` (Kueue ungates on admission) | unchanged |
| **Companion object** | `PodGroup` CRD wrapping the LWS gang (`minMember = total pods`); pods labeled with the matching `pod-group.scheduling.run.ai/name` | none — Kueue's webhook creates `Workload` from the queue label | none |
| **Capacity source** | `Queue.status` + `ResourcePool.status` per Project | `ClusterQueue.status.flavorsUsage[]` | direct node listing |

The matcher reads `IC.status.capacity` and is **agnostic** to which adapter populated it — same shape across schedulers.

### Where it's wired

```
ModelReplica
    │
    ▼
compose-model-placement/main.py
    │  build base LLM-IS spec (decode + optional prefill)
    │
    ├──▶ scheduler.wrap(IC.spec.scheduler.type, llmis_spec, ...)
    │       │
    │       ├─ wrap_kai     → set schedulerName, stamp pod label, emit PodGroup
    │       ├─ wrap_kueue   → stamp queue label, suspend: true
    │       └─ wrap_none    → pass-through
    │
    ▼
remote-cluster apply: LLM-IS + DRA ResourceClaims + scheduler companion objects
```

Adding a new scheduler (Volcano, etc.):
1. New `wrap_<name>` in `scheduler.py` (one function).
2. Add to `_DISPATCH` map.
3. New module under `lib/capacity_adapter/<name>.py` returning the same `CapacitySnapshot` shape.
4. Add to `IC.spec.scheduler.type` enum.

No matcher changes. No MD changes. The IR (`ModelReplica`) doesn't know which scheduler is involved.

### Capacity feedback loop

```
in-cluster scheduler           ── populates ──▶  Queue / ClusterQueue status
                                                          │
                                                          ▼
                                            lib/capacity_adapter/<scheduler>.py
                                            (controller-runtime watcher,
                                             one per IC, polls every ~5s)
                                                          │
                                                          ▼
                                            IC.status.capacity (normalized)
                                                          │
                                                          ▼
                                            federation matcher reads this
                                            on the next placement
```

A few seconds of staleness is fine — we don't reserve, we admit. If the matcher picks a saturated cluster, the in-cluster scheduler holds the workload Pending; next reconcile re-evaluates.

## Use cases — how each one flows through the code

### A. Single-node, single-GPU (gpt-oss-20b)

[`examples/workloads/gpt-oss-20b.yaml`](./examples/workloads/gpt-oss-20b.yaml) — `topology.strategy: Tensor, tensor: 1`.

```
MD.replicas: 2
  → compose-model-deployment
      scheduling.match():
        per replica index 0..1:
          filter ICs by clusterSelector.matchLabels (tier=production)
          for each pool: eval nodeSelector.cel (vramGiB >= 24)
            → pool fits if class.gpu_count >= 1 (Tensor 1)
            → free nodes >= 1 (1 node, 1 GPU)
          score by headroom + spread bonus
      emits 2 ModelReplica + 2 ModelEndpoint
  → compose-model-placement (per MR)
      Tensor strategy → workerSpec.replicas=1, single pod, 1 GPU
      ResourceClaim: 1 GPU against the matched class's CEL
```

### B. Multi-node TP+PP (Kimi K2)

[`examples/workloads/kimi-k2.yaml`](./examples/workloads/kimi-k2.yaml) — `strategy: TensorPipeline, tensor: 8, pipeline: 2`.

```
MD.replicas: 1 (no ScaledObject — fixed)
  → compose-model-deployment
      scheduling.match():
        Topology.shape() returns (2 nodes_per_inst, 8 gpus_per_node)
        node_selector_cel: vramGiB>=141 && fp8 in features && IB 400Gbps
        capacity check: pool.max_nodes - used >= 2
      emits 1 ModelReplica
  → compose-model-placement
      TensorPipeline → LWS group of size 2
      ResourceClaim: 8 GPUs per pod × 2 pods = 16 total
```

### C. Disaggregated P/D (Llama-405B style)

`prefill:` block at MD spec level; top-level fields are decode. Existing examples don't carry this shape yet (will be updated to match Nic's #64). The trace below shows what the matcher would do.

```
MD.replicas: 1, decode (TensorPipeline 8x2 instances=3),
              prefill (Tensor 1 instances=5)
  → compose-model-deployment
      scheduling.match():
        for each candidate IC:
          find decode pools (>=141 GiB + IB)
          find prefill pools (>=80 GiB + IB)  ← potentially same IC, different pool
          pair (decode_pool, prefill_pool) — same cluster (KV co-location)
          capacity check: decode needs 6 nodes (3*2), prefill 5 (5*1)
      emits 1 ModelReplica with both target.decodePool and target.prefillPool
  → compose-model-placement
      LLM-IS spec has spec.workerSpec (decode) + spec.prefill.workerSpec
      2 ResourceClaims (one per role)
      KV transfer config flows through engine.args opaquely
```

### D. Scale-up across the fleet

```
KEDA ScaledObject writes MD.spec.replicas: 1 → 4
  → compose-model-deployment re-runs:
      scheduling.match():
        replica 0: existing → sticky (no re-placement)
        replicas 1..3: new
          working set tracks decisions made *this pass* so replica 2
          doesn't double-count what replica 1 just consumed
      result: 4 ModelReplicas, possibly across multiple ICs
              (matcher picks based on capacity headroom)
  → compose-model-placement renders 3 new LLM-ISs across the chosen ICs
```

Cross-cluster spread is implicit — falls out of the capacity-headroom score, not a separate code path.

### E. Cluster degrades

```
External eviction controller (out of scope for this PR) writes annotation
  modelplane.ai/evict=true on affected ModelReplicas.
  → compose-model-deployment notices the annotation,
    drops those replicas from `existing`, treats their indices as new,
    matcher picks again excluding the degraded IC
  → compose-model-placement renders on the new target
  → old LLM-IS GC'd by Crossplane when the MR is deleted
```

## What's *not* in this PR

Listed so the surface is honest:

- The new XRDs themselves — that's Nic's territory in [#64](https://github.com/modelplaneai/modelplane/pull/64). Once they merge, we regenerate the protos under `functions/*/model/` and the `_load_md` / `_resolve_clusters` stubs in this code become real.
- KEDA `ScaledObject` composer — Nic's design has the user (or a higher-level Composition) author one, not Modelplane.
- ~~KAI scheduler integration~~ — sketched in this PR (`scheduler.py` dispatch + `lib/capacity_adapter/kai.py`). Requires a small extension to Nic's #64 (`IC.spec.scheduler.type`).
- ~~Kueue scheduler integration~~ — sketched in this PR (`scheduler.py` dispatch + `lib/capacity_adapter/kueue.py`).
- Per-version KServe adapter dispatch (v0.16 / v0.17 / v0.18) — sketched as a TODO comment in `_worker_spec`. Today we render v0.18 only.
- The eviction controller and the capacity-status puller — separate processes, not composition functions. Out of scope here.
- A real CEL evaluator — `scheduling.eval_cel` is a placeholder. Production wires `cel-python` or a Go shim.
- Tests — the matcher's pure-Python isolation makes it easy to add table-driven tests; deferred so this PR stays focused on *shape*.

## What this PR is for

A working sketch reviewers can read end-to-end:

- **the matcher logic** (in `scheduling.py`) — what the federation layer actually does
- **the dependency graph** (this doc + the headers on each `main.py`) — what each function reads and writes
- **how the use cases land** (above) — concrete tracing from MD YAML to running pods
- **why BYO-anything is cheap** — the IR (`ModelReplica`) is the only seam each backend / scheduler / version needs to honor

If those four read clean, the design is in good shape. The wiring follows.
