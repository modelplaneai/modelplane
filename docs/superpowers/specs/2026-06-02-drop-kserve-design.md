# Drop KServe — dispatch to native + llm-d (Dynamo-ready)

**Status:** Draft
**Date:** 2026-06-02
**Tracking issue:** [#65](https://github.com/modelplaneai/modelplane/issues/65)
**Related:** [#64](https://github.com/modelplaneai/modelplane/issues/64) (engine block), [#66](https://github.com/modelplaneai/modelplane/issues/66) (ModelCache), [#72](https://github.com/modelplaneai/modelplane/issues/72) (KVOffloadTier)

## Summary

Modelplane currently composes KServe at two points: the per-replica serving
workload (a KServe `LLMInferenceService`, emitted by `compose-model-replica`)
and the per-cluster install (`KServeBackend`, which installs cert-manager,
Gateway API, Envoy Gateway, LeaderWorkerSet, KServe, KEDA, and Prometheus).

This design removes the KServe dependency at both points. In its place,
`compose-model-replica` becomes a dispatcher that picks the lightest serving
path per replica:

| Topology | Backend |
|---|---|
| Single self-contained pod | Native Kubernetes `Deployment` + `Service` |
| Multi-pod (LWS gang, disaggregated P/D, multi-node DP) | **llm-d** (default) |
| Multi-pod requesting a Dynamo-only capability on NVIDIA hardware | **Dynamo** (designed-for; not built in v0.1) |

KServe's `LLMInferenceService` is itself built on llm-d. Composing llm-d
directly removes a redundant wrapper layer — the central argument of #65.

### Scope

**v0.1 ships two backends:** native (single-pod) and llm-d (multi-pod). The
dispatch abstraction is designed so **Dynamo** slots in later as a third
multi-pod path without reworking dispatch. Dynamo, `KVOffloadTier` (#72), and
any in-cluster autoscaling are out of scope for v0.1.

## Revision 2026-06-02 — llm-d v0.7 spike outcomes

The Task 0 verification spike (`docs/superpowers/notes/llm-d-v0.7-surface.md`)
established facts that supersede some assumptions below. Where this section
conflicts with text further down, **this section wins.**

**Decisions:**
- **llm-d workload is rendered as provider-kubernetes `Object`s, not a Helm
  chart.** llm-d v0.7.0 **deprecated** both the `llm-d-modelservice` and
  `llm-d-infra` charts. So `llmd.py` composes the serving `Deployment` (or
  `LeaderWorkerSet` for multi-node) plus the GAIE routing (`InferencePool` +
  EPP + `HTTPRoute`) directly as `Object`s — the same style as `native.py`. No
  `provider-helm` is used for the multi-pod workload.
- **Keep Envoy Gateway**, pending verification that it is GAIE `InferencePool`
  v1 conformant. `ServingStack` adds GAIE **v1.5.0** CRDs/controller and bumps
  Gateway API to **v1.5.1**; it does **not** install the deprecated
  llm-d-infra/modelservice charts.

**Mechanical corrections (apply throughout):**
- `InferencePool` is GA at **`inference.networking.k8s.io/v1`**; its spec uses
  **`targetPorts[].number`** (a list) and **`endpointPickerRef`** (name + `port.number`
  + `failureMode`) — **not** `targetPortNumber` / `extensionRef`.
- `InferenceObjective` (`inference.networking.x-k8s.io/v1alpha2`) carries only
  `priority` + `poolRef` — no `modelName`, no `criticality`. The public
  model-name → pool mapping is the engine's advertised model name plus the
  `HTTPRoute` whose `backendRef` is the `InferencePool`. **v0.1 omits
  `InferenceObjective`** (priority is the only thing it adds — YAGNI); routing is
  `InferencePool` + per-pool EPP + `HTTPRoute`.
- llm-d serving pods carry the label `llm-d.ai/inference-serving: "true"` (+
  `llm-d.ai/role`); the `InferencePool.spec.selector` must select that label.
- The EPP is **per-pool** (GAIE's endpoint-picker image), referenced by the
  pool's `endpointPickerRef` (default `port.number: 9002`, `failureMode: FailOpen`).
- Weight loading for the llm-d path is identical to native (we render the pod):
  no-cache → `--model=org/model` direct fetch with `engine.env` creds; cache →
  mount the PVC and `--model=/mnt/models`. There is no chart `modelArtifacts.uri`.
- `ComposedResource` stays `Object | Release`, but in v0.1 **both** replica
  backends (native, llm-d) return only `Object`s; `Release` remains used by
  `ServingStack` for the per-cluster component installs.

## Dispatch model

`compose-model-replica` selects a backend from topology and capabilities. There
is **no user-facing backend field** — consistent with the "Multiple inference
orchestrators" alternative the v0.1 design doc explicitly rejected (it creates a
lowest-common-denominator API). Users describe what they want to run; Modelplane
picks the lightest composition path that satisfies it.

```
select_backend(replica):
    if single_self_contained_pod(replica):
        return native
    if requests_dynamo_only_capability(replica) and nvidia_compatible_engine(replica):
        return dynamo        # dormant in v0.1: no such capability is wired yet
    return llmd              # default for all multi-pod

single_self_contained_pod(replica) =
        nodes_per_worker(replica) == 1     # pipeline * (data / dataLocal) == 1
    AND replica.spec.prefill is None        # not disaggregated
    AND not multi_node_data_parallel(replica)
```

`nodes_per_worker` is derived from `workers.topology`. A replica is "just
Kubernetes" only when it is exactly one pod with no cross-pod coordination.

> **v0.1 implemented topology.** The current `ModelReplica` / `ModelDeployment`
> CRDs implement only `topology.tensor` and `topology.pipeline` — there is **no
> `prefill` block and no `data`/`dataLocal`** (those appear in the aspirational
> `design/design.md` but are not yet in the schema). So in v0.1
> `nodes_per_worker == pipeline`, and the predicate reduces to **`pipeline == 1`
> → native, else llm-d**. The dispatcher implements this as a
> `needs_cross_pod_coordination(replica)` function whose body is `pipeline > 1`
> today, with documented extension points for the `prefill` and
> `data`/`dataLocal` clauses below so they drop in unchanged when those fields
> are added.

When the richer topology lands, the predicate extends to also route to llm-d:
- **disaggregated prefill/decode** (a `prefill` block is inherently multi-pod
  even when each role is `tensor: 1`, `pipeline: 1`), and
- **multi-node data parallelism** (`data > dataLocal` with `pipeline == 1`).

This is a known correctness improvement the dispatcher is shaped for, not a bug
fixable on the v0.1 schema (which cannot yet express those topologies).

### Why Dynamo selection is capability-driven

The issue says "Dynamo when distinctive Dynamo features are needed." With no
backend field, the trigger is a **requested capability that only Dynamo
satisfies** (e.g. a KVBM-backed `KVOffloadTier` per #72, or ModelExpress weight
streaming) **and** an NVIDIA-compatible engine/hardware match. In v0.1 no such
capability is wired, so the Dynamo branch is dormant and live dispatch is purely
topology → {native, llm-d}. The branch exists in the dispatcher as the
documented extension point.

## Composition architecture

One composition function, one reconcile loop. `compose-model-replica` keeps
owning all shared concerns; backends own only the emitted workload. This avoids
re-introducing the "two reconciliation loops doing similar work" redundancy that
#65 criticizes KServe for — there is no intermediate per-backend XR.

```
functions/compose-model-replica/function/
    fn.py              # resolve InferenceCluster, extract the `engine` container,
                       # wire caches (#66), select backend, call backend.build(...),
                       # populate ModelEndpoint.url, derive conditions
    backends/
        base.py        # interface (see below)
        native.py      # Deployment + Service + HTTPRoute
        llmd.py        # llm-d-modelservice Helm Release + GAIE routing
        dynamo.py      # stub in v0.1: not selectable; raises if reached
```

### Backend interface

The interface returns a list of **composed resources**, where each is either a
provider-kubernetes `Object` or a provider-helm `Release`. This mixed return is
exactly what `compose-kserve-backend` already produces today, so no new provider
is introduced.

```python
# base.py (illustrative)
ComposedResource = Object | Release   # provider-kubernetes Object or provider-helm Release

class Backend(Protocol):
    def build(self, replica: ModelReplica, cluster: InferenceCluster) -> list[ComposedResource]: ...
```

`fn.py` is responsible for the cross-cutting work so the backends stay focused:
- resolving the referenced `InferenceCluster` (and its `providerConfigRef`),
- extracting the container named `engine` from `workers.template`,
- resolving `ModelCache` references (#66) into a model URI / mount,
- selecting the backend,
- populating `ModelEndpoint.url` in the unchanged shape
  `http://<gateway>/<namespace>/<deployment>/`,
- deriving `ModelAccepted` / `ModelReady` conditions from the composed
  resources' observed state.

### Backends

**native.py** — single self-contained pod. Emits provider-kubernetes `Object`s:
a `Deployment` (the engine container built from `workers.template`, GPU limits
from `topology.tensor`, shm sizing, readiness probe, env / `imagePullSecrets`
passthrough), a `Service`, and an `HTTPRoute` binding the Service to the
cluster's inference Gateway. Weights load per the weight-loading contract below.

**llmd.py** — all multi-pod. Composes:
1. a provider-helm `Release` of the **`llm-d-modelservice`** chart
   (`llm-d-incubation/llm-d-modelservice`), with Helm values mapped from the
   ModelReplica: `modelArtifacts.uri`, parallelism (tensor / pipeline / data /
   dataLocal), prefill vs decode replica counts, engine image/args/env, and the
   `multinode` flag. The chart renders the `Deployment`/`LeaderWorkerSet`,
   `Service`s, DRA `ResourceClaim`s, and model-download init-containers — so
   Modelplane does **not** hand-roll any of that for the multi-pod path.
2. the per-model GAIE routing as provider-kubernetes `Object`s: an
   `InferencePool` (v1) selecting the model's pods, an `InferenceObjective`
   mapping the public model name into the pool, and the EPP (endpoint picker)
   that the pool's `extensionRef` points at.

The per-cluster GAIE CRDs/controller and the Gateway itself are installed once
by `ServingStack` (below); only the per-model routing instances are composed
here, co-located with the workload that needs them.

> **Naming note.** llm-d's "ModelService" is now only a **Helm chart name**
> (the former `llm-d/llm-d-model-service` operator and its `ModelService` CRD
> were archived 2025-07-24). It is not a Kubernetes kind, so the collision with
> Modelplane's `ModelService` resource is purely nominal. The spec and code
> refer to it as "the `llm-d-modelservice` chart" to avoid confusion.

**dynamo.py** — future. When built, emits a single provider-kubernetes `Object`
wrapping a `DynamoGraphDeployment` (`nvidia.com/v1alpha1`), reconciled by the
NVIDIA Dynamo operator installed (behind a flag) by `ServingStack`. In v0.1 the
module is a stub that is never selected; if reached it raises, surfaced as a
`ModelReplica` condition.

## Cluster install: `KServeBackend` → `ServingStack`

`KServeBackend` is replaced by a backend-neutral substrate XR,
`ServingStack`, in `infrastructure.modelplane.ai`. There is one install per
cluster, and a single cluster can host native + llm-d (+ future Dynamo)
workloads simultaneously, because dispatch is capability-driven. The install
provides the union substrate the dispatch paths depend on.

| Component | v0.1 | Notes |
|---|---|---|
| LeaderWorkerSet | keep | bump **v0.7.0 → v0.8.0** |
| Gateway API | keep | bump to **v1.5.1** |
| Gateway API Inference Extension (GAIE) | **add** | **v1.5.0** CRDs + controller; `InferencePool` GA `v1` |
| Envoy Gateway | keep | **verify GAIE `InferencePool` v1 conformance** (open item) |
| cert-manager | keep | webhooks |
| Prometheus (kube-prometheus-stack) | keep | **observability** (distinct from autoscaling) |
| llm-d-infra / llm-d-modelservice charts | **not installed** | deprecated in v0.7; `llmd.py` renders the workload as `Object`s |
| KServe | **drop** | replaced by direct composition |
| KEDA | **drop** | autoscaling is fleet-level on the control plane |
| NVIDIA Dynamo operator | future, flag | added when the Dynamo path lands |

The install continues to compose Helm `Release`s and provider-kubernetes
`Object`s targeting the remote cluster, the same mechanism `compose-kserve-backend`
uses today.

## Connective tissue Modelplane now owns

KServe's `LLMInferenceService` absorbed connective tissue (#65 comment) that
Modelplane must now handle. Most of it resolves cleanly given the decisions
above.

### Weight loading

KServe's storage initializer (driven by `model.uri`, the `hf://` hack in the
current code) is removed. Weights are handled by two mechanisms behind one user
contract:

- **No `ModelCache` referenced:** the engine pulls directly from its source.
  - native: `--model=org/model` passed through; vLLM/SGLang fetches at startup
    using credentials from `engine.env` / `imagePullSecrets` (e.g. `HF_TOKEN`).
  - llm-d: `modelArtifacts.uri = hf://org/model`; the chart's init-container
    fetches.
- **`ModelCache` referenced (#66, opt-in):** the staged RWX PVC is mounted.
  - native: `--model=/mnt/models`.
  - llm-d: `modelArtifacts.uri = pvc://<claim>/<path>`.

The `hf://` `model.uri` hack and its surrounding `--model=` stripping logic are
**deleted**.

> **Must document for users.** Direct-fetch is a new user-facing contract that
> KServe previously hid. The getting-started / concepts docs must state that
> without a `ModelCache`, the engine fetches weights at startup and the
> deployment is responsible for providing source credentials (e.g. `HF_TOKEN`
> via `engine.env`), and that the engine image must support the source.

### Multi-node bootstrap

The hand-rolled `_VLLM_MULTI_NODE_BOOTSTRAP` (Ray leader/worker init) added to
`compose-model-replica` during the ModelCache rebase is **deleted**. All
multi-node now routes to llm-d, and the `llm-d-modelservice` chart owns
leader/worker coordination via LeaderWorkerSet (`multinode` flag). The native
path is single-pod only and needs no Ray bootstrap.

### In-cluster routing

KServe emitted the in-cluster `HTTPRoute`. Now each backend owns its routing:
native emits `Service` + `HTTPRoute`; llm-d composes the GAIE routing
(`InferencePool` + `InferenceObjective` + EPP). Both populate
`ModelEndpoint.url` in the unchanged shape, so the fleet routing layer
(`compose-model-service` / `compose-model-endpoint`) is untouched.

### Pod-spec hooks (#64 engine block)

`env`, `imagePullSecrets`, shm sizing, and readiness probes are built directly
from `workers.template` — by `native.py` for the native path, and via Helm
values for the llm-d path.

### KV offload (#72)

Out of scope for v0.1. The Dynamo-only-capability hook in the dispatcher is
where a KVBM-backed `KVOffloadTier` eventually triggers the Dynamo path.

## Verified dependency versions (June 2026)

| Dependency | Version | Notes |
|---|---|---|
| llm-d | v0.7.0 | reference release; workload rendered as `Object`s |
| `llm-d-modelservice` / `llm-d-infra` charts | — | **deprecated in v0.7; not used** |
| Gateway API Inference Extension | **v1.5.0** | `InferencePool` GA `v1`; `InferenceObjective` `v1alpha2` (omitted in v0.1) |
| Gateway API | **v1.5.1** | bump |
| LeaderWorkerSet | v0.8.0 | from v0.7.0 |
| NVIDIA Dynamo | v1.0 | `DynamoGraphDeployment` `nvidia.com/v1alpha1` (future) |
| cert-manager | (carry current pin) | webhooks |
| Envoy Gateway | (carry current pin) | **verify InferencePool v1 conformance** |
| kube-prometheus-stack | (carry current pin) | observability |

Spike-recorded alternatives considered for the gateway: **agentgateway v2.2.1**
(llm-d v0.7's preferred GAIE-conformant gateway) and **Istio 1.29.1**. We keep
Envoy Gateway for v0.1 to minimize disruption, pending the conformance check.

llm-d requires Kubernetes 1.30+; GAIE requires a recent Gateway API. These are
within the cluster requirements Modelplane already imposes.

## Migration & blast radius

**Hard cutover, no dual-run.** Modelplane is pre-release; KServe is removed
rather than run alongside llm-d. *(Assumption to confirm at review.)*

Rename/replace touches:
- `apis/kservebackends/` → `apis/servingstacks/` (XRD + composition)
- `functions/compose-kserve-backend/` → `functions/compose-serving-stack/`
- `schemas/python/models/ai/modelplane/infrastructure/kservebackend/` regen
- `apis/inferenceclusters/` + `compose-inference-cluster` (references the backend XR)
- `functions/compose-model-replica/` (dispatcher + backends; delete KServe
  emission, `hf://` hack, and Ray bootstrap)
- `examples/`, demo manifests, `docs/concepts.md`, `docs/getting-started.md`

## Testing strategy

- **Dispatch predicate:** table-driven unit test over `(topology, prefill, dp)`
  → expected backend, covering the single-pod / disaggregated / multi-node-DP /
  multi-node-PP cases (including the previously-misrouted ones).
- **Backends:** each backend module unit-tested in isolation — `build(replica,
  cluster)` → expected composed resources (the same `test_fn.py` golden-style
  pattern the functions already use). native asserts the `Deployment` / `Service`
  / `HTTPRoute`; llmd asserts the `Release` values and the GAIE `Object`s.
- **No KServe references** remain in code, schemas, examples, or docs (grep gate).

## Out of scope / future

- **Dynamo backend** — CUDA runtime images, Grove/NVL72 topology, KVBM; emitted
  as `DynamoGraphDeployment`. The dispatcher's capability hook is the entry point.
- **`KVOffloadTier` (#72)** — the first concrete Dynamo-only capability.
- **In-cluster autoscaling** — autoscaling stays fleet-level (control-plane KEDA
  on the `ModelDeployment` scale subresource).

## Alternatives considered

- **All three backends in v0.1 (incl. Dynamo).** Rejected: Dynamo is
  NVIDIA-coupled (CUDA images, Grove, KVBM) and pulls in hardware-specific
  surface that is not demo-critical; building the abstraction against native +
  llm-d validates it without that cost.
- **Abstraction boundary only (each backend a separate spec).** Rejected: risks
  an abstraction designed against zero concrete backends.
- **Per-backend intermediate XRs** (`LLMDBackend` / `DynamoBackend`, or a
  per-replica serving XR). Rejected: adds a reconcile loop per replica — the
  exact redundancy #65 removes from KServe.
- **User-selectable backend field.** Rejected: reopens the
  lowest-common-denominator API problem the v0.1 design doc already rejected.
- **Keep `pipeline > 1` as the multi-node predicate.** Rejected: provably
  misroutes disaggregated P/D and multi-node data parallelism.
- **`ModelCache` mandatory / re-implement a storage-initializer init-container.**
  Rejected: forces #66 to be a v0.1 blocker, or rebuilds what #66 already does.
- **llm-d via the `ModelService` operator/CRD.** Rejected: that project is
  archived/deprecated; the maintained path is the `llm-d-modelservice` Helm chart.

## Open items (for the implementation plan)

- **llm-d v0.7 routing recipe.** Confirm the exact GAIE resource set and EPP
  topology (shared vs per-pool EPP) that llm-d's current well-lit path expects,
  and the precise `llm-d-modelservice` values schema, against v0.7 — a bounded
  spike before `llmd.py` lands.
- **Gateway choice.** Confirm Envoy Gateway is the GAIE-conformant gateway we
  keep (vs Istio/kgateway used in some llm-d recipes) and pin a conformant version.
- **cert-manager necessity** for the GAIE/llm-d webhooks; drop if unneeded.
