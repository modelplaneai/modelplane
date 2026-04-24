# Intent-Based Deployment for ModelPlane

**Authors:** Dennis Ramdass, with input from Pablo's original hypothesis

**Status:** Design exploration

**Date:** April 2026

---

**TL;DR.** Inspired by NVIDIA Dynamo's DGDR → DGD pattern, we propose a `ModelDeploymentRequest` CRD that compiles workload + SLA intent into a concrete `ModelDeployment` via **inference profiles** — curated, engine-recipe-sourced configs that a composition function resolves per backend and hardware. Backend capabilities are self-described on `InferenceEnvironment.status` — populated by the backend composition function, queried by the scheduler, invisible to users. Two new CRDs (`ModelDeploymentRequest`, `InferenceProfile`), zero new infrastructure CRDs. Users state intent, the system resolves everything else.

---

## Part 1: The Problem and the Parallel

### The configuration wall

Deploying an inference workload means choosing across ~10 interacting axes: tensor-parallel degree, pipeline-parallel degree, quantization, batch size, KV cache allocation, prefill/decode split, replica count, scaling thresholds, and engine flags. The right choices depend on hardware, traffic, model architecture, and engine version — and shift with every engine upgrade.

Some users want these knobs — they compete on getting the configuration right. But most don't. They know their domain, their budget, or their application — not the engine. For them, `engine.args` is a wall, not a feature.

### The CDN parallel

**Phase 1: Manual.** CDN customers configured origins, TTLs, edges by hand. This is where most inference deployments are — manual TP/PP/quant config per model per hardware.

**Phase 2: Single-provider automation.** CDNs absorbed invalidation, edge selection, failover. This is where KServe LLMInferenceService and Dynamo DGD sit — they manage serving lifecycle but the user picks engine config.

**Phase 3: Multi-provider orchestration.** Cloudflare moved the value above any single CDN. This is ModelPlane's position — the layer where "RAG workload, 500ms TTFT" compiles into the right backend-specific config across the fleet.

### What no one does today

**Workload intent** (user describes the pattern, system picks defaults) shows up partially at Together AI and Fireworks. **SLA intent** (user describes targets, system compiles config) shows up at Dynamo's DGDR. Nobody unifies both across multiple backends and engines.

---

## Part 2: How It Works

### CRDs

Two new resources. No new infrastructure CRDs — backend capabilities live on existing `InferenceEnvironment` status.

| CRD | Who creates it | What it does |
|---|---|---|
| **`ModelDeploymentRequest`** | ML team | States intent (workload, SLA, priority). Composition function compiles it into a concrete ModelDeployment. |
| **`InferenceProfile`** | Platform team | Hardware-specific profile data. May include capability preferences (e.g., `preferDisaggregated`) that the scheduler resolves against environment capabilities. |
| `ModelDeployment` *(unchanged)* | ML team directly OR compiled from Request | Concrete deployment spec. Break glass: create directly, skip the intent layer entirely. |
| `InferenceEnvironment` *(status extended)* | Platform team | New `status.capacity.capabilities` block — populated by the backend composition function, queried by the scheduler. |

### The CRD chain

```
ModelDeploymentRequest  →  ModelDeployment  →  ModelPlacement  →  backend-specific resources
     (intent)               (concrete)          (per-env)         (KServe / Dynamo / future)
```

The user writes intent. The system resolves profiles, matches environment capabilities, picks environments, and emits backend-specific resources. The backend is an implementation detail.

### Backend capabilities as environment status

Today, backend knowledge is scattered: `BACKEND_SCALING_SIGNALS` is a hardcoded Python dict, capability checks are `if` branches in composition functions. The `InferenceEnvironment` already represents "a cluster with a backend installed" — the composition function that installs the backend is the natural place to declare what it can do.

```yaml
# InferenceEnvironment status — capabilities populated by compose-kserve-backend
status:
  capacity:
    backend: KServe
    gpuPools:
    - acceleratorType: nvidia-h100-80gb
      countPerNode: 8
      nodes: 3
      memory: 80Gi
    capabilities:                    # NEW — written by backend composition function
      scalingSignals: [Fixed, Concurrency, WVA]
      engines: [vLLM, SGLang]
      multiNode: true
      disaggregatedPD: true          # true because llm-d is installed
      adapterServing: true
```

**Why status, not a separate CRD:** capabilities vary per environment, not per backend type. A KServe environment with llm-d has `disaggregatedPD: true`. One without it doesn't. The composition function that installs the backend knows what it installed — it writes capabilities the same way it writes GPU pool capacity. Per-environment truth, not a global declaration.

The scheduler already reads IE status for GPU capacity. Reading `capabilities` is one more field on the same resource. The `BACKEND_SCALING_SIGNALS` hardcoded dict becomes a query against `env.status.capacity.capabilities.scalingSignals`.

**Adding a new backend:** write a new IE composition function that installs the backend and populates capabilities in status. No new CRD, no enum extension.

### How backend selection works (user never decides)

```
User:       workload: rag, sla.ttft: 500ms
                    ↓
Profile:    preferDisaggregated: true, scaling: Concurrency, target: 8
                    ↓
Scheduler:  reads IE status.capacity.capabilities across all environments
            → gke-dynamo has disaggregatedPD: true ✓
            → gke-kserve has disaggregatedPD: true ✓ (llm-d installed)
            → picks by capacity/cost
                    ↓
User sees:  endpoint URL, status, scaling. Never a backend name.
```

Backend names appear in `ModelPlacement.status.servingProfile.backend` for observability only.

### Mapping to user archetypes

We're developing behavioral archetypes that describe how different users consume inference infrastructure (see Pablo's user archetypes discovery doc — hypotheses under validation). The architecture doesn't depend on where archetype boundaries land.

| User pattern | Primary CRD | Why |
|---|---|---|
| Knows the workload and SLA, not the engine flags | `ModelDeploymentRequest` | Intent is their natural language. |
| Cares about cost and governance, not TP degree | `ModelDeploymentRequest` | Intent + cost priority is enough. |
| Wants an endpoint that works, builds on top | `ModelDeploymentRequest` | Closest thing to "just deploy it." |
| Wants every knob, competes on serving perf | `ModelDeployment` directly | Compiled output would be wrong for their edge cases. |
| Curates the catalog and profiles for others | Creates `InferenceProfile` + `InferenceBackend`; uses both | Bridges the two paths. |
| Fine-tunes models, needs training-specific config | `ModelDeployment` directly | Engine args tied to training. Intent can't capture this. |

### Day 0 / Day 1 / Day 2 operations

| Scenario | Who | What happens | Details |
|---|---|---|---|
| **Day 0: Set up environments** | Platform team | Create InferenceEnvironments. The backend composition function installs KServe/Dynamo and populates `status.capacity.capabilities` automatically. | — |
| **Day 0: Set up catalog** | Platform team | Register ClusterModels. Import engine recipes. Create InferenceProfiles for hardware. | [A.1](#a1-inferenceprofile), [A.2](#a2-import-commands) |
| **Day 1: Deploy with intent** | ML team | Create a ModelDeploymentRequest. Profile resolves, scheduler matches environment capabilities, emits ModelDeployment. | [A.3](#a3-modeldeploymentrequest), [A.4](#a4-compiled-modeldeployment) |
| **Day 1: Deploy without intent** | ML team | Create a ModelDeployment directly. Works exactly as today. | [A.5](#a5-explicit-modeldeployment) |
| **Day 1: Profile not found** | ML team | `ProfileNotFound` condition. ModelDeployment compiled from ClusterModel only. | — |
| **Day 1: No environment supports profile needs** | ML team | `PlacementsScheduled: False` — no environment's capabilities match the profile's preferences. | — |
| **Day 2: Profile update** | Platform team / OSS | Profiles or InferenceProfiles update → Request recompiles → ModelDeployment updates. | — |
| **Day 2: Pin** | Platform team | Delete Request, keep ModelDeployment. Concrete spec frozen. | — |
| **Day N: Add a third backend** | Platform team | Write a new IE composition function that installs the backend and populates capabilities. No new CRD. | — |

### Resolution cascade

1. **InferenceProfile CRD match** — finds a profile matching `(workload, priority, modelTier, hardware)`.
2. **Built-in profile** — if no CRD matches, fall back to Configuration package defaults.
3. **No match** — compile minimal ModelDeployment. `ProfileNotFound` condition.

Profile may set capability preferences (e.g., `preferDisaggregated: true`). The scheduler matches these against InferenceBackend capabilities when selecting environments. This is internal — the user didn't specify capabilities.

### Inference profiles and the engine recipe ecosystem

Inference engines publish structured deployment knowledge — vLLM's [recipes repo](https://github.com/vllm-project/recipes) (30+ model families), SGLang docs, TRT-LLM NIM profiles. InferenceProfiles codify the same knowledge as a Kubernetes resource.

| Engine recipe concept | Maps to InferenceProfile | Example |
|---|---|---|
| Architecture (dense/moe) | Parallelism strategy | `moe` → prefer EP over TP |
| Precision + VRAM | Quantization + scheduling | `fp8`, `805GB` |
| Base args + hardware overrides | `engineArgs` | Blackwell → `--attention-backend FLASHINFER_MLA` |
| Compatible strategies | Capability preferences | `[single_node_tp, pd_cluster]` |
| P/D strategy overrides | `preferDisaggregated` | Scheduler matches against InferenceBackend |

`mp import-recipe` generates InferenceProfile CRDs from engine recipes. Engine-agnostic.

### Promoting engine.args to first-class fields

| Concept | Escape hatch today | Proposed field | Why |
|---|---|---|---|
| **Quantization** | `--quantization=fp8` | `spec.quantization: fp8` | Every engine. Most common arg. |
| **Context length** | `--max-model-len=32768` | `spec.contextLength: 32768` | Every engine. Affects KV cache + VRAM. |
| **TP degree** | `--tensor-parallel-size=4` | `spec.parallelism.tensor: 4` | Portable. Override for multi-node/latency. |
| **Architecture** | Profile hint | `spec.architecture: disaggregated` | Portable concept, backend-specific impl. |

### Treadmill avoidance

Profiles encode heuristics, not precise tuning. Backend-native compilers (Dynamo DGDR, KServe OME) handle hardware-specific optimization when available. Engine recipes are the upstream.

---

## Part 3: Industry Positioning and Commercial Opportunity

### Relationship to Dynamo's DGDR → DGD pattern

The two-CRD model is directly inspired by DGDR → DGD. What ModelPlane adds:

| | Dynamo DGDR | ModelPlane Request |
|---|---|---|
| **Intent types** | SLA only | SLA + workload |
| **Backends** | Dynamo only | Any backend (KServe, Dynamo, future) — capabilities self-described on IE status |
| **Backend selection** | N/A (Dynamo only) | Automatic — scheduler matches profile preferences to environment capabilities |
| **Scope** | Single cluster | Multi-environment fleet |
| **Compiler** | AIConfigurator (profiling) | InferenceProfiles (heuristic) + delegates to AIConfigurator on Dynamo |
| **Break glass** | Create DGD directly | Create ModelDeployment directly |

On Dynamo environments, ModelPlane composes a DGDR — delegating engine-level compilation to AIConfigurator. On KServe environments, ModelPlane compiles using InferenceProfiles. On a future backend, ModelPlane emits whatever that backend needs — guided by the InferenceBackend capability declaration.

### Competitive landscape

| Player | What they do | What they don't |
|---|---|---|
| **Dynamo DGDR** | SLA → AIConfigurator compiles DGD | Single-cluster, Dynamo-only. No workload intent. |
| **KServe LLMInferenceService** | Orchestration-aware (routing, P/D, gateway) | Low-level on config. No intent compilation. |
| **Together AI** | Use-case catalog | Managed only. No SLA compiler. |
| **Fireworks** | Deployment shapes | Managed only. |
| **vLLM recipes / SGLang docs** | Structured deployment knowledge | Documentation, not operational. |
| **NIM profiles** | Pre-compiled per model × GPU | Engine-level, single-cluster. |

**The open position:** compile intent across heterogeneous backends at the fleet level, with automatic backend selection based on capability matching.

### Commercial model

The seam is the profile data, not the code.

**OSS:** built-in profiles, backend composition functions (KServe + Dynamo) that populate capabilities, baseline governance.

**Upbound:** hardware-validated InferenceProfile CRDs. Profiled on specific GPUs. Re-profiled each engine release. Same composition function, different profile data.

### Risks and mitigations

| Risk | Mitigation | Alternative |
|---|---|---|
| **LCD for precise-control users** | ModelDeployment is the break-glass CRD. Full control. | N/A — this IS the escape hatch. |
| **Profile rot** | Heuristics not precise tuning. Import from engine recipes. Delegate to backend compilers. | Require explicit config. Blocks 80% of users. |
| **Capability schema on IE status** | No new CRD — extends existing IE status. Populated by composition function that already installs the backend. | Keep hardcoded dicts. Works for 2 backends; doesn't scale. |
| **"Just copied DGDR"** | Acknowledge lineage. Differentiate: cross-backend with automatic selection, fleet scope, community profiles. | Different pattern. Worse design to avoid optics. |
| **Engine vendors build orchestration** | Profiles are engine-agnostic, cross-backend. | Specialize on one engine. |
| **DGDR coupling** | Opt-in per backend. Falls back to profile-derived ModelDeployment. | Compile to ModelDeployment always. |

---

## Appendix: YAML Examples

<a id="a1-inferenceprofile"></a>
### A.1 InferenceProfile (platform team creates)

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceProfile
metadata:
  name: rag-latency-medium-h100
spec:
  workload: rag
  priority: latency
  modelTier: medium
  hardware: nvidia-h100-80gb
  scaling:
    signal: Concurrency
    concurrency:
      target: 6
      maxReplicas: 6
  engineArgs:
    - --max-model-len=65536
    - --quantization=fp8
  preferDisaggregated: true
```

<a id="a2-import-commands"></a>
### A.2 Import commands (Day 0)

```bash
mp import-recipe deepseek-ai/DeepSeek-V3.2 --variant fp8 --hardware hopper
mp import-docs --source https://docs.sglang.ai/references/supported_models.html \
  --model Qwen/Qwen3-32B --engine SGLang
```

<a id="a3-modeldeploymentrequest"></a>
### A.3 ModelDeploymentRequest (ML team creates)

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeploymentRequest
metadata:
  name: support-bot
  namespace: ml-team
spec:
  modelRef:
    name: llama-3.1-70b-instruct
  environments: 2
  intent:
    workload: rag
    sla:
      ttft: 500ms
      itl: 40ms
    priority: latency
```

<a id="a4-compiled-modeldeployment"></a>
### A.4 Compiled ModelDeployment (output of Request)

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: support-bot
  namespace: ml-team
  labels:
    modelplane.ai/compiled-from: support-bot-request
spec:
  modelRef:
    name: llama-3.1-70b-instruct
  environments: 2
  scaling:
    signal: Concurrency
    concurrency:
      minReplicas: 1
      maxReplicas: 6
      target: 6
```

<a id="a5-explicit-modeldeployment"></a>
### A.5 Explicit ModelDeployment (no intent)

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: support-bot
  namespace: ml-team
spec:
  modelRef:
    name: llama-3.1-70b-instruct
  environments: 1
  scaling:
    signal: Concurrency
    concurrency:
      maxReplicas: 4
      target: 16
```

### A.6 Built-in profile data model (fallback)

```python
# lib/profiles.py — shipped with Configuration package

@dataclass
class InferenceProfile:
    scaling_signal: str | None = None
    min_replicas: int | None = None
    max_replicas: int | None = None
    target: int | None = None
    engine_args: list[str] | None = None
    prefer_disaggregated: bool = False

PROFILES = {
    ("rag", "latency", "medium"): InferenceProfile(
        scaling_signal="Concurrency", target=8, max_replicas=6,
        engine_args=["--max-model-len=32768", "--quantization=fp8"],
        prefer_disaggregated=True,
    ),
    ("chat", "latency", "small"): InferenceProfile(
        scaling_signal="Concurrency", target=32, max_replicas=8,
    ),
    ("batch", "throughput", "medium"): InferenceProfile(
        scaling_signal="Concurrency", min_replicas=0, target=64,
        max_replicas=8, engine_args=["--max-num-seqs=256"],
    ),
}
```
