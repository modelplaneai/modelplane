# Modelplane Scheduling — Design

> Federation scheduler + renderer composition functions. **API shape is owned by [#64](https://github.com/modelplaneai/modelplane/pull/64)** — this doc points at the implementation that consumes it.
>
> **Scope of this MR:** the federation scheduling algorithm, the IR boundary between scheduler and renderer, KServe LLM-IS rendering, DRA `ResourceClaim` derivation, and **managed-kai** as the in-cluster scheduler. The plugin/dispatch system (Kueue, Volcano, none) and per-scheduler capacity adapters land in a follow-up MR — kept out of here so the algorithm + IR review is focused.
>
> **Status:** sketch. The code under `functions/` doesn't run yet — it targets API protos that haven't been generated. Algorithm, dependencies, and use cases are real; wiring is gated on #64 landing.

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

The composition functions are split into **pure modules** (algorithm, dict-builders, dispatch tables — no Crossplane imports) and an **orchestrator** `main.py` that glues phases together with required-resources + status writes. The boundary keeps the algorithm testable in isolation and makes "what's Crossplane logic vs scheduling logic" obvious in a glance.

### Composer — `compose-model-deployment/`

| File | Pure? | What it does |
|---|---|---|
| [`scheduling.py`](../../functions/compose-model-deployment/scheduling.py) | ✓ | Federation matcher algorithm. `match(md, clusters, existing) → MatchResult`. Plain dataclasses, no I/O. |
| [`adapters.py`](../../functions/compose-model-deployment/adapters.py) | boundary | Proto / observed-XR ⇄ scheduling dataclasses. Three load functions: `load_md`, `load_clusters`, `load_existing`. |
| [`emitters.py`](../../functions/compose-model-deployment/emitters.py) | ✓ | Pure dict builders for composed `ModelReplica` / `ModelEndpoint` resources. |
| [`main.py`](../../functions/compose-model-deployment/main.py) | orchestrator | Crossplane glue — six phases (REQUIRE → LOAD → MATCH → BUILD → EMIT → STATUS), each clearly banner-commented. State machine for `Scheduled` / `ReplicasReady` conditions. |

### Renderer — `compose-model-placement/`

| File | Pure? | What it does |
|---|---|---|
| [`rendering.py`](../../functions/compose-model-placement/rendering.py) | ✓ | Build KServe LLM-IS spec + DRA `ResourceClaim` spec + selector CEL from class capabilities + `with_kai_gang()` for KAI integration. |
| [`adapters.py`](../../functions/compose-model-placement/adapters.py) | boundary | Proto / observed-MR ⇄ rendering dataclasses. |
| [`main.py`](../../functions/compose-model-placement/main.py) | orchestrator | Verb-named methods (`resolve_inputs`, `compose_llmis`, `compose_resource_claims`, `derive_conditions`). Conditions: `Ready` with cold-start sub-states `Pulling` / `LWSGangPending` / `EngineLoading`. |

## Tests

```bash
# One-time setup (until repo's nix toolchain is wired):
uv venv .venv-test
uv pip install --python .venv-test/bin/python pytest ruff pyright

# Run unit tests
.venv-test/bin/python -m pytest tests/unit -v
# Lint
.venv-test/bin/ruff check functions/ lib/
```

| Layer | Files | Coverage today |
|---|---|---|
| **Static** | `pyproject.toml` configures `ruff` (linting) + `pyright` (typing) over `functions/` and `lib/`. | All clean. |
| **Pure unit tests** | [`tests/unit/`](../../tests/unit/) — 49 tests covering `scheduling.py` (topology, filtering, capacity, sticky placement, disagg, trace) and `rendering.py` (LLM-IS shape + DRA selector CEL + KAI gang wrap + PodGroup `minMember` sizing). | 49/49 green; runs in ~20ms. |
| **Composition tests** | Existing `tests/test-*/` pattern (Upbound `up` CLI). New shapes wired once #64's protos land — `tests/test-model-deployment-v2/`, `tests/test-model-replica-{kai,kueue}/`. | Deferred. |
| **E2E** | Real cluster running KAI or Kueue. | Out of scope for this PR. |

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
| writes | `PodGroup` (on target cluster) | KAI gang admission |
| writes | `ResourceClaim` × roles (on target cluster) | DRA device binding |
| writes | MR status conditions | `Ready` / `Pulling` / `LWSGangPending` / `EngineLoading` |

KEDA `ScaledObject`s are user-authored per Nic's design (mirroring Deployment + HPA) — not composed by Modelplane. Modelplane only exposes `MD.spec.replicas` via the scale subresource.

## KAI integration (in-cluster, this MR)

This MR wires **managed-kai** as the only in-cluster scheduler so the federation algorithm + IR boundary are easy to review. KAI integration is small enough to inline in the renderer without abstracting; the per-scheduler dispatch + capacity adapters land in a follow-up MR.

What the renderer does for KAI, after building the base LLM-IS spec:

1. Stamp `schedulerName: kai-scheduler` on every pod template the LLM-IS produces (decode `workerSpec` and prefill `workerSpec` for disagg).
2. Stamp `pod-group.scheduling.run.ai/name: <mr>-gang` on the pod templates so KAI binds them to the right gang.
3. Emit a `PodGroup` CRD with `minMember` = total pod count (LWS group × instances, summed across decode + prefill if disagg).

```
ModelReplica
    │
    ▼
compose-model-placement/main.py
    │  rendering.build_llmis_spec(...)        — base KServe v0.18 LLM-IS
    │  rendering.with_kai_gang(spec, ...)     — schedulerName + PodGroup
    │  rendering.build_resource_claim_spec(...) — DRA per role
    │
    ▼
remote-cluster apply: LLMInferenceService + PodGroup + ResourceClaim(s)
```

The federation scheduler is **agnostic** to which in-cluster scheduler is in use — it reads `IC.status.capacity` whichever adapter populated it. That's why the dispatch + capacity adapter can land separately without touching `scheduling.py`.

### Follow-up MR (plugin/dispatch system)

What the next MR will add on top of this:

- `IC.spec.scheduler.type` enum (`auto` · `managed-kai` · `managed-kueue` · `kai` · `kueue` · `none`) — proposed extension to [#64](https://github.com/modelplaneai/modelplane/pull/64).
- A `scheduler.py` dispatch table that branches the renderer's wrap per type (KAI's `with_kai_gang` becomes one entry).
- Kueue: stamp `kueue.x-k8s.io/queue-name` + `suspend: true`; Kueue's webhook creates the `Workload`.
- Volcano: similar shape, different CRDs.
- `none`: pass-through; kube-scheduler best-effort.
- A per-scheduler **capacity adapter** controller that reads each scheduler's status CRDs (KAI `Queue` / `ResourcePool`, Kueue `ClusterQueue.flavorsUsage[]`) and writes the normalized `IC.status.capacity` shape the federation scheduler consumes.
- Cluster onboarding controller that auto-detects which scheduler is installed (`Project` CRD ⇒ KAI, `ClusterQueue` CRD ⇒ Kueue, neither ⇒ install `managed-kueue`).

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

## Scheduler properties

Pinning down what the scheduler actually does, in K8s SIG-Scheduling terms — the load-bearing properties reviewers should sanity-check.

| Property | What |
|---|---|
| **Mental model** | Per-replica fleet scheduler. One `schedule()` call binds `(cluster, pool)` for each of `spec.replicas`. |
| **Capacity input** | `IC.status.capacity` — populated by the per-scheduler capacity adapter (KAI for this MR; Kueue/Volcano in the follow-up). Scheduler is agnostic to which adapter wrote it. |
| **Pool eligibility** | CEL predicate over typed `InferenceClass.capabilities` (vendor, product, vramGiB, features, interconnect, …). Open vocabulary; new capabilities don't need schema changes. |
| **Topology** | Discriminated union: `Tensor` / `TensorPipeline` / `DataExpert`. Each strategy resolves to `(nodes_per_inst, gpus_per_node)`; multiplied by `instances` for the role's footprint. |
| **Disaggregation** | First-class. Decode + prefill are separate roles, scheduled together but to (potentially) different pools, **same cluster** (KV cache transfer requires co-location). |
| **Engine config** | Pass-through. `engine.{name, image, args}` flows from MD → MR → renderer; the scheduler never inspects engine internals. |
| **Scaling** | Out of scope. KEDA `ScaledObject` is user-authored (mirrors Deployment + HPA); the scheduler reads `spec.replicas` and reconciles MRs. |
| **Stickiness** | Per-replica `replicaIndex`. An existing MR keeps its target across reconciles; re-placement only on hard eviction (annotation-driven). |
| **Multi-replica accounting** | Filter → Score → Bind passes reserve consumed capacity in a working set so subsequent replicas in the same `schedule()` call don't double-count. |
| **Algorithm structure** | Explicit Filter → Score → Bind phases (matches K8s SIG-Scheduling). Each phase is its own helper; tests parametrize against each. |
| **Result shape** | `ScheduleResult(placements: list[Placement], trace: list[MatchTrace])`. Decisions and per-(cluster, pool, reason) rejection trace are separate. |
| **`matchTrace`** | Surfaced on `MD.status.matchTrace`. Users see exactly which cluster + pool failed which predicate, with detail strings (`"4/8 free"` etc.). |
| **In-cluster integration** | This MR: managed-kai only — `schedulerName: kai-scheduler` on pods + `PodGroup` CRD with `minMember = total pods`. Per-scheduler dispatch is a follow-up MR. |
| **Module structure** | Pure algorithm in `scheduling.py`. Crossplane glue in `main.py`. Adapters + emitters separate. 49 unit tests over the pure modules. |

This block is where the design is opinionated. Anything not in this table is not a property the scheduler guarantees.

## What this PR is for

A working sketch reviewers can read end-to-end:

- **the matcher logic** (in `scheduling.py`) — what the federation layer actually does
- **the dependency graph** (this doc + the headers on each `main.py`) — what each function reads and writes
- **how the use cases land** (above) — concrete tracing from MD YAML to running pods
- **why BYO-anything is cheap** — the IR (`ModelReplica`) is the only seam each backend / scheduler / version needs to honor

If those four read clean, the design is in good shape. The wiring follows.
