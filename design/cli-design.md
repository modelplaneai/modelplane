# ModelPlane CLI (`mp`) — Design Proposal

**Authors:** Dennis Ramdass

**Status:** Draft — For Team Review

**Date:** April 2026

**Related:** ModelPlane v0.1 Scope Document

---

## 1. Principles

The load-bearing rules. Every Decision in §5 cites the Principles it implements.

**P1. The CLI is a projection of the CRD, not a parallel API.** Every flag is a deterministic write to a known CRD field; everything outside the curated flag set is reachable via `mp deploy -f`. CRD validation is the only validation. This is the mechanism that lets the CLI promise long-term stability while the CRDs underneath are still evolving. *(See Decision 3.)*

**P2. No new abstractions to learn.** Configuration uses the same Model CRD YAML the platform team already knows. No custom config format, no translation layer, no second schema to document. *(See Decision 2.)*

**P3. Don't reimplement kubectl.** For listing, inspecting, and deleting resources, delegate to kubectl transparently. Own only the workflow gaps that kubectl can't fill: zero-config deploy, endpoint discovery, request formatting, log tailing scoped to a deployment. *(See Decision 1.)*

**P4. Agent-friendly by default.** AI agents and shell scripts are first-class callers from v0.1. Every data-producing command supports `--output json`, exit codes are documented, and prompts are TTY-gated. *(See Decision 9.)*

**P5. Docs are generated, not handwritten.** The CLI's Click command tree is the source of truth for `docs/cli.html`. Drift between `mp --help`, the published docs, and any agent's understanding of the CLI is impossible by construction. *(See Decision 9.)*

---

## 2. Goals

The outcomes we want. The Principles in §1 are how we get them.

**G1. ML-first UX for the 80% case.** An ML engineer can deploy a catalog model and test it in under 60 seconds, without writing YAML or knowing what a namespace is.

**G2. Long-term stability.** v0.1 ships the command surface ML teams will use long-term — a stability commitment, not a placeholder for a future redesign. P1 (projection) and P5 (generated docs) are how we keep that commitment as the platform evolves.

**G3. Industry-aligned UX.** ML engineers know Truss, HuggingFace CLI, and Cog. The CLI feels familiar — scaffold, edit, deploy, predict — not like a Kubernetes tool with ML branding.

**G4. v0.1 user journey support.** J1 (deploy first model) and J3 (cross-backend comparison) from the v0.1 scope doc are the acceptance criteria.

---

## 3. Non-Goals

**NG1. Replace kubectl for platform teams.** Infra engineers are comfortable with kubectl and GitOps. The CLI is not their primary tool.

**NG2. Full lifecycle management.** Observability dashboards, rollout strategies (canary, blue/green), and fleet-wide operations are out of scope for v0.1. The CLI covers deploy (including scaling intent), check, and test.

**NG3. Custom model packaging.** Unlike Truss or Cog, the CLI does not build containers or package model code. ModelPlane delegates model pulling to backends (KServe, Dynamo). The CLI deploys — it doesn't build.

**NG4. Multi-cluster orchestration from the CLI.** Placement across environments is handled by the ModelPlane control plane. The CLI submits intent; the scheduler does the rest.

---

## 4. Problem Statement

The v0.1 release doc identifies a clear gap: *"ML engineers are allergic to kubectl — even a thin wrapper adds meaningful value."* Today, deploying a model on ModelPlane requires writing Kubernetes YAML and using `kubectl apply`. This is a non-starter for ML teams who think in terms of models, not manifests.

But the answer isn't to reimplement kubectl behind a different name. ModelPlane's CRDs already have clean printer columns — `kubectl get clustermodels` works fine. The real gaps are the things kubectl *can't* do:

1. **Deploy a catalog model in one command** — no YAML, no `kubectl apply`
2. **Test a deployed model** — discover the endpoint, format the request, make the call
3. **Scaffold a Model YAML** — with all options as commented defaults
4. **Wire up a deployment to a file** — create Model + ModelDeployment in one step

The CLI focuses on these four capabilities and delegates everything else to kubectl.

---

## 5. Design Decisions

Each Decision header lists the Principles (P) and Goals (G) it implements.

### Decision 1: Own the workflow, delegate the CRUD *(P3)*

**What:** The CLI implements four capabilities that kubectl cannot provide. For listing, inspecting, and deleting resources, it delegates directly to kubectl — transparently, with the exact command shown in `--help`.

| Command | What it does | Implementation |
|---------|-------------|----------------|
| `mp init` | Scaffold Model YAML or set team context | **Native** — generates commented CRD template |
| `mp deploy` | Deploy from catalog or YAML file | **Native** — creates Model + ModelDeployment, resolves `--env` |
| `mp predict` | Send input to a deployed model | **Native** — endpoint discovery, request formatting, HTTP call |
| `mp status NAME` | Rich status with `--watch` and `--all-envs` | **Native** — polls CRD status, shows per-placement breakdown |
| `mp models` | List catalog models | **Delegates to** `kubectl get clustermodels` |
| `mp envs` | List inference environments | **Delegates to** `kubectl get inferenceenvironments` |
| `mp deployments` | List deployments | **Delegates to** `kubectl get modeldeployments -n <team>` |
| `mp status` (no name) | List deployments | **Delegates to** `kubectl get modeldeployments -n <team>` |
| `mp delete` | Delete a deployment | **Delegates to** `kubectl delete modeldeployment <name> -n <team>` |

**Why:** ModelPlane CRDs already define `additionalPrinterColumns` that give clean kubectl output. Reimplementing list/table formatting in Python adds code to maintain, introduces subtle output differences, and teaches users a format they can't use with kubectl. The delegating commands exist so ML engineers have a single tool to reach for (`mp`), but they're honest about being shortcuts.

**What this means in practice:** The delegating commands are thin shells around `subprocess`. Real logic lives in `deploy.py`, `predict.py`, `status.py`, and `init.py`.

### Decision 2: No custom config format — use CRD YAML directly *(P2)*

**What:** When ML teams define a custom model, they write a standard `Model` CRD YAML. The CLI scaffolds a well-commented template via `mp init`, and deploys it via `mp deploy -f`. There is no intermediate config format.

**Why:** We evaluated the config-file patterns used by Truss, Cog, BentoML, and Modal. These tools invented custom formats because they have no backing Kubernetes CRD — the config file *is* their schema. ModelPlane already has a well-defined schema: the `Model` CRD.

Introducing a second format would mean a translation layer to maintain, two schemas to document, and drift when CRD fields are added (new backends, NIM, serving profiles). The Model CRD YAML is around 15 lines for the common case. The CLI adds value through *workflow* (scaffold → edit → deploy), not format translation.

**The actual YAML:**

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: Model
metadata:
  name: my-llama
spec:
  model:
    name: meta-llama/Meta-Llama-3-8B-Instruct
  source: HuggingFace
  huggingFace:
    repo: meta-llama/Meta-Llama-3-8B-Instruct
  resources:
    vram: 24Gi
  serving:
  - name: vllm-kserve
    backend: KServe
    engine:
      name: vLLM
      image: vllm/vllm-openai:latest
```

**Industry references:**
- Truss config.yaml: https://docs.baseten.co/reference/truss-configuration
- Cog cog.yaml: https://github.com/replicate/cog/blob/main/docs/yaml.md
- BentoML bentofile.yaml: https://docs.bentoml.com/en/latest/reference/bentoml/bento-build-options.html
- Modal decorators: https://modal.com/docs/guide/gpu

### Decision 3: The CLI is a projection of the CRD, not a parallel API *(P1, G2)*

This is the meta-principle that makes the rest of the proposal hold together — the Goal of the same name promises CLI stability while the CRDs evolve, and this Decision is how that promise is kept.

**What:** Every CLI flag is a deterministic write to a known CRD field. The CLI surfaces a curated subset of CRD fields as flags; everything else is reachable via `mp deploy -f model.yaml`. There is no CLI-only configuration, no validation logic the CRD doesn't enforce, and no command that writes anything outside the documented CRD schema.

**Why:** ModelPlane CRDs evolve as backends, engines, scaling signals, and source types are added. If the CLI maintains a parallel data model — flag combinations that translate via Python logic into CRD writes — that translation drifts every time the CRD changes. Users see flags that silently no-op, validation errors that don't match `kubectl apply`, and behavior that diverges from the YAML they read in docs. Projection avoids this entire class of drift.

**Practical rules:**

1. **Flags are curated, not generated.** Auto-generating a flag per CRD path would surface every nested field as `--scaling.concurrency.maxReplicas`, which is unusable. The CLI selects flags for fields ML engineers commonly set inline (`--env`, `--min`, `--max`, `--target`). New CRD fields default to YAML-only; promoting a field to a flag is a separate decision driven by observed usage, not automation.

2. **CRD validation is the only validation.** The CLI does a server-side dry-run before submitting a real apply. Whatever error the OpenAPI schema or admission webhook returns is what the user sees. The CLI does not duplicate `required:`, enums, or value bounds in Python — those would drift the moment the CRD changes.

3. **Flag stability survives CRD churn.** When a CRD field is renamed, the flag → field mapping updates silently and the user-facing flag name stays the same. When a CRD field is removed, the corresponding flag is deprecated for one release before removal. Within those rules, the platform team can evolve the CRD freely without breaking the CLI's command surface.

4. **Drift is caught in CI, not by users.** A small set of golden YAMLs — one per representative deploy shape (catalog, file, autoscaled, scale-to-zero, multi-env) — lives alongside the CLI tests. Each CI run renders the CLI's CRD output for each shape and dry-runs it against the live CRD schema. If a CRD change removes or renames a field a flag depends on, CI fails before any user does.

**What this rules out:** A CLI-side config schema. A "smart" CLI that pre-validates against a hardcoded schema. Magic flag combinations that map to multi-field CRD writes beyond a documented one-to-one or simple typed shorthand (e.g. `--scale-to-zero` → `minReplicas: 0`).

### Decision 4: Two deployment paths — catalog and file *(G1, G4)*

**What:** ML teams deploy in two ways:

1. **From catalog** (zero config): `mp deploy llama3-8b` — one command, no files
2. **From file** (full control): `mp deploy -f model.yaml` — CLI creates Model + ModelDeployment in one step

**Why:** The catalog path is the 80% case — platform teams curate approved models, ML teams pick one. The file path is the escape hatch for fine-tuned models or experimental configs. Most users should never need a file.

### Decision 5: Targeting and fan-out *(G4)*

**What:** Two orthogonal flags on `mp deploy`:
- `--env prod-gpu-east` targets a specific InferenceEnvironment by name (sets `environmentSelector.matchLabels`).
- `--envs 2` fans out across N environments, auto-scheduled. Maps to `spec.environments`.

**Why:** The v0.1 user journeys require both — J1 deploys to a specific environment, J3 deploys across backends for comparison.

### Decision 6: Scaling as deployment intent *(P1, G2)*

**What:** `ModelDeployment.spec.scaling` uses Crossplane's discriminated-union pattern — a `signal:` enum names the strategy and a sibling block holds its config. The same pattern is used elsewhere in ModelPlane (e.g. `InferenceEnvironment.spec.<backend>`); seeing it once means seeing it everywhere.

| Signal | Implemented by | Portable? | Notes |
|---|---|---|---|
| `Fixed` (default) | All backends | Yes | Static pod count per placement. |
| `Concurrency` | KServe + Dynamo | Yes | Autoscale on Envoy in-flight requests, mediated by KEDA + Prometheus. |
| `WVA` (planned) | KServe only | No | KServe Workload Variant Autoscaler — opting in pins the deployment to a KServe environment. |

**Portable vs backend-specific signals.** Some signals (Concurrency) have implementations on multiple backends; the scheduler can place a deployment using a portable signal anywhere with capacity. Other signals (WVA) are backend-specific — selecting one implicitly constrains placement to environments running that backend. Both are first-class CRD fields, but they signal different intent: *"I want autoscaling, you pick how"* vs *"I want WVA specifically, KServe only please."* This is a deliberate trade-off the user opts into; ModelPlane doesn't pretend the choice is neutral.

**CLI policy.** Portable signals get CLI flags. Backend-specific signals live in YAML — accessible via `mp deploy -f model.yaml`, not via flags. The CLI doesn't hide a backend abstraction the user has explicitly chosen to engage with: if you're picking WVA, you already know what KServe is and what WVA does. Surfacing `--signal WVA --min 1 --max 5` would suggest the choice is interchangeable with `--min 1 --max 5 --target 32`, which it isn't.

The CLI exposes portable scaling through `mp deploy` flags. There is no `mp scale` or `mp autoscale` verb — scaling is a property of the deployment, not a separate operation.

| Flag | Maps to | Notes |
|------|---------|-------|
| `--replicas N` | `scaling: {signal: Fixed, fixed: {replicas: N}}` | Per-placement pod count. Defaults to 1 when no scaling flags are given. |
| `--min N --max M --target T` | `scaling: {signal: Concurrency, concurrency: {minReplicas: N, maxReplicas: M, target: T}}` | All three required to opt into autoscaling. `target` is in-flight requests per replica — the CRD field name and the underlying Envoy metric. |
| `--scale-to-zero` | `scaling.concurrency.minReplicas: 0` | Shorthand for `--min 0`. Still requires `--max` and `--target`. |
| `--utilization P` | `scaling.concurrency.utilization: P` | Optional. Defaults to 70 (scale at 70% of target). |
| `--scale-down-delay S` | `scaling.concurrency.scaleDownDelay: S` | Optional. Defaults to 300s. |

**Why:** The CRD models scaling as a signal enum — Fixed and Concurrency today, with room for `Utilization`, `RPS`, custom metrics, and backend-specific variants like `WVA` later. The CLI mirrors the portable subset. To change scaling on a running deployment, edit the YAML and re-deploy or patch the CRD directly.

### Decision 7: Smart `predict` — one command for any model type *(G1, P4)*

**What:** `mp predict <name> -i "input"` auto-detects the input format and routes to the correct endpoint:
- Plain text → wrapped as chat completion
- JSON with `messages` → forwarded to `/chat/completions`
- JSON with `input` → forwarded to `/responses`

`--stream` switches to token-by-token output (server-sent events), pipeable into another process without buffering. `--output json` emits the full response as a single JSON object suitable for `jq` and agent loops.

**Why:** A single command handles all model types and future-proofs against new API shapes (OpenAI Responses API, embeddings, etc.) without adding new CLI commands.

### Decision 8: `mp init` scaffolds commented CRD YAML (like `truss init`) *(P2, G3)*

**What:** `mp init my-model` creates `my-model/model.yaml` — the actual Model CRD with every field visible as a comment. Users edit and deploy.

**Why:** One of Truss's best UX patterns. Users discover options by reading the scaffold, not by searching docs. `mp init --team` separately handles team context setup.

### Decision 9: Agent-friendly by default, with auto-generated docs *(P4, P5)*

**What:** The CLI is designed so that AI agents and shell scripts are first-class callers, not an afterthought:

1. **Structured output on demand.** Every data-producing command accepts `--output json` (alias `-o json`). The default remains human-readable; the JSON shape is documented and stable across minor versions.
2. **Stable, documented exit codes.** `0` success; `1` generic error; `2` usage error; `3` not found; `4` backend/cluster error; `5` timeout. Agents branch on exit codes without parsing stderr.
3. **TTY-aware interactivity.** Confirmation prompts (`mp delete`) and progress animations (`mp status --watch`) only render when stdout is a TTY. Non-TTY invocations either use a documented default or fail fast with exit `2`. The CLI never blocks on stdin in a non-interactive context.
4. **Streaming.** `mp predict --stream` writes tokens to stdout as they arrive — pipeable, no buffering. `mp status --watch --output json` emits newline-delimited JSON, one object per state transition, suitable for log shippers and agent loops.
5. **Deterministic startup.** No telemetry. No version checks on startup. No auto-updates. Every invocation is a function of its arguments, environment variables, and cluster state.

**Why:** Agent-driven inference workflows are already common and will be the default within a year. A CLI that requires a TTY, has unstable output formats, or signals state through stderr defeats both agents and scripts. Bolting on agent support later — the kubectl/git/docker pattern — leaves a permanent asymmetry between human and agent UX. Designing for both from v0.1 keeps the surface clean and means the same `--help` output and the same exit codes serve both audiences.

**Auto-generated docs (the corollary):** Because every flag, default, and exit code is part of the agent contract, the CLI's Click command tree is the source of truth for the documentation. `docs/cli.html` is rendered by `docs/build.py`, which walks the `mp.main:cli` Click group and emits HTML. Run `make -C docs` to regenerate; `make -C docs check` is the CI gate that fails when the committed `cli.html` doesn't match what would be generated. Static page sections (install, agents-and-scripting, configuration) live in the script alongside the dynamic command rendering. This is the same projection principle as Decision 3, applied one level up: one source of truth (the Click tree), projected outward to docs, `--help`, and any agent's understanding of the CLI. Drift is impossible by construction.

---

## 6. Command Reference

### Commands with real value (native implementation)

```
mp init NAME                                  # Scaffold a Model YAML template
mp init --team NAME                           # Set team context (one-time)
mp deploy MODEL [deploy-flags]                # Deploy from catalog
mp deploy -f model.yaml [deploy-flags]        # Deploy from YAML file
mp status NAME [--watch] [--all-envs]         # Rich status, polling, per-env breakdown
mp predict NAME -i "input" [--raw] [--stream] # Smart predict; --stream for token SSE
```

All native commands accept `--output json` (alias `-o json`) for machine-readable output. See Decision 9.

**`deploy-flags`** (apply to both catalog and file deploys):

```
  --env NAME                   Target a specific InferenceEnvironment
  --envs N                     Fan out across N environments (default 1)

  # Scaling (default: --replicas 1)
  --replicas N                 Fixed pod count per placement
  --min N --max M --target T   Autoscale on in-flight request concurrency
  --scale-to-zero              Shorthand for --min 0 (still needs --max, --target)
  --utilization P              Scale at P% of target (default 70)
  --scale-down-delay S         Seconds before removing replicas (default 300)
```

### Commands that delegate to kubectl

```
mp models                                     # → kubectl get clustermodels
mp envs                                       # → kubectl get inferenceenvironments
mp deployments [--team]                       # → kubectl get modeldeployments -n <team>
mp status (no name) [--team]                  # → kubectl get modeldeployments -n <team>
mp delete NAME [-y] [--team]                  # → kubectl delete modeldeployment <name> -n <team>
```

These exist so ML engineers have a single CLI to reach for. Each one shows the kubectl command it delegates to in `--help`.

---

## 7. Workflows

### A. Deploy from Catalog (ML Engineer — the 80% case)

```bash
$ mp init --team ml-team
Team set to: ml-team

$ mp models
NAME              READY   MODEL                          VRAM   AGE
qwen-0.5b-vllm    True    Qwen/Qwen2.5-0.5B-Instruct     2Gi    5d
llama-8b-vllm     True    meta-llama/Llama-3-8B          24Gi   5d

$ mp deploy qwen-0.5b-vllm --env prod-gpu-east
Deploying qwen-0.5b-vllm...
Deployment created. Run `mp status qwen-0.5b-vllm` to check progress.

$ mp status qwen-0.5b-vllm --watch
Deployment:  qwen-0.5b-vllm
Model:       Qwen/Qwen2.5-0.5B-Instruct
Status:      Ready
Replicas:    1/1
Endpoint:    http://172.18.255.200/ml-team/qwen-0.5b-vllm/v1

$ mp predict qwen-0.5b-vllm -i "Explain attention in transformers"
Attention mechanisms allow models to weigh the relevance of different
parts of the input when producing each part of the output...
```

### B. Deploy Custom Model (ML Engineer — fine-tuned / experimental)

```bash
$ mp init my-llama
Created my-llama/model.yaml
Edit it, then run: mp deploy -f my-llama/model.yaml

# User edits model.yaml: fills in HF repo, adjusts VRAM, adds engine args
$ mp deploy -f my-llama/model.yaml --env prod-gpu-east
Deploying my-llama...
Deployment created. Run `mp status my-llama` to check progress.
```

### C. Cross-Backend Comparison (MLOps)

```bash
$ mp deploy llama3-8b --env prod-gpu-east        # KServe environment
$ mp deploy llama3-8b --env coreweave-research   # Dynamo environment

$ mp status llama3-8b --all-envs
Deployment:  llama3-8b
Model:       meta-llama/Meta-Llama-3-8B-Instruct
Status:      Ready
Placements:  2/2

  ENV                  STATUS   ENDPOINT
  prod-gpu-east        Ready    http://10.0.0.1/.../v1
  coreweave-research   Ready    http://10.0.0.2/.../v1
```

### D. Autoscale a Production Deployment (ML Engineer — bursty traffic)

```bash
# Deploy with autoscaling: 1 baseline replica, burst up to 6, target 32 in-flight requests/replica
$ mp deploy llama-8b-vllm --env prod-gpu-east --min 1 --max 6 --target 32
Deploying llama-8b-vllm with autoscaling (1-6 replicas, target 32 concurrent req/replica)...
Deployment created. Run `mp status llama-8b-vllm` to check progress.

$ mp status llama-8b-vllm --watch
Deployment:  llama-8b-vllm
Model:       meta-llama/Llama-3-8B
Status:      Ready
Scaling:     Concurrency (1-6 replicas, target 32, util 70%)
Replicas:    2/2 (scaled from 1 at 14:32:08)
Endpoint:    http://172.18.255.200/ml-team/llama-8b-vllm/v1
```

### E. Scale to Zero (ML Engineer — bursty / dev workloads)

```bash
# Dev model that should idle to zero between bursts
$ mp deploy qwen-0.5b-vllm --env prod-gpu-east --scale-to-zero --max 4 --target 16
Deploying qwen-0.5b-vllm with autoscaling (0-4 replicas, scale-to-zero enabled)...

# First request after idle period incurs a cold start
$ mp predict qwen-0.5b-vllm -i "hello"
(scaling up from 0 — this may take a moment...)
Hello! How can I help you today?
```

---

## 8. Model Lifecycle Operations — Future Direction

v0.1 covers deploy, check, and test. But the ML workflow doesn't end at deployment — teams quantize models to fit smaller GPUs, fine-tune base models on domain data, swap LoRA adapters per use case, benchmark latency against SLAs, and promote models through staging gates. This section proposes how the CLI extends to cover these operations without violating the core design principles: no new abstractions, CRD YAML as the config format, and own the workflow while delegating the CRUD.

The guiding principle: **ModelPlane is a control plane, not a build system.** Quantization, fine-tuning, and optimization produce artifacts (model weights, adapters, engine configs). ModelPlane deploys and manages those artifacts — it doesn't run the training jobs or compilation pipelines. The CLI makes it easy to declare what you want; the backends and engines do the work.

### 8.1 Quantization

**The landscape:** Quantization reduces model precision to lower VRAM requirements and improve throughput. The ecosystem has converged on two patterns: (a) pre-quantized model checkpoints downloaded from HuggingFace (GPTQ, AWQ, GGUF formats), and (b) serving-time quantization flags where the engine quantizes on load (vLLM's `--quantization fp8`, TensorRT-LLM's FP8 pipeline). Baseten's Engine Builder is the most polished platform integration — users set `quantization_type: fp8` in config YAML and the platform handles everything.

**The ModelPlane approach:** Quantization is a declarative property of the Model CRD, not a CLI pipeline. The engine handles it at serve time or the user points to pre-quantized weights. The CLI exposes this through `mp init` scaffolding and deploy-time flags.

**CRD extension — `spec.serving[].engine.args` (already supported):**

For v0.1, quantization is already expressible through the opaque `engine.args` array. A vLLM serving profile with FP8 quantization:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: Model
metadata:
  name: llama3-8b-fp8
spec:
  model:
    name: meta-llama/Meta-Llama-3-8B-Instruct
  source: HuggingFace
  huggingFace:
    repo: meta-llama/Meta-Llama-3-8B-Instruct
  resources:
    vram: 12Gi          # FP8 cuts VRAM roughly in half
  serving:
  - name: vllm-kserve
    backend: KServe
    engine:
      name: vLLM
      image: vllm/vllm-openai:latest
      args:
      - --quantization=fp8
      - --dtype=auto
      - --max-model-len=8192
```

**CRD extension — first-class `quantization` field (post-v0.1):**

As quantization becomes table stakes, a first-class field avoids forcing users to know engine-specific flag syntax:

```yaml
spec:
  serving:
  - name: vllm-kserve
    backend: KServe
    engine:
      name: vLLM
      image: vllm/vllm-openai:latest
      quantization: fp8     # Engine-aware: vLLM → --quantization fp8
```

The composition function translates `quantization: fp8` into the correct engine args for vLLM, SGLang, or Dynamo. This is the Baseten pattern — one field, the platform handles the rest.

**Supported quantization methods** (by engine):

| Method | vLLM | SGLang | TensorRT-LLM | Notes |
|--------|------|--------|---------------|-------|
| FP8 (W8A8) | `--quantization fp8` | `--quantization fp8` | Native | Best for Hopper/Ada GPUs |
| AWQ (INT4) | `--quantization awq` | `--quantization awq` | Supported | Requires pre-quantized checkpoint |
| GPTQ (INT4) | `--quantization gptq` | `--quantization gptq` | Supported | Requires pre-quantized checkpoint |
| bitsandbytes | `--quantization bitsandbytes` | — | — | INT4/INT8, good for memory-constrained |

**CLI integration:**

The `mp init` scaffold includes quantization as a commented field. No new CLI commands — quantization is a property of the model, not a separate operation:

```bash
# Deploy a pre-quantized model from catalog (platform team pre-registered it)
$ mp deploy llama3-8b-awq --env prod-gpu-east

# Deploy from file with quantization in the YAML
$ mp deploy -f llama3-fp8.yaml --env prod-gpu-east
```

**Why no `mp quantize` command:** ModelPlane doesn't build containers or package model code (Design Decision, Non-Goals). Quantization follows the same principle. Offline quantization tools (llm-compressor, AutoGPTQ, llama.cpp) produce checkpoints that get uploaded to HuggingFace or object storage. Serving-time quantization is an engine flag. The CLI's job is to make both paths easy to declare, not to run the quantization itself.

### 8.2 Fine-Tuning

**The landscape:** Every major platform follows the same workflow: upload dataset → configure training → train → deploy result. The tools split into CLI-driven (Together AI's `together fine-tuning create`, Fireworks' `firectl sftj create`) and YAML-config-driven (Axolotl, LlamaFactory). LoRA (Low-Rank Adaptation) dominates: adapters are 50-100MB vs. 14GB+ for full fine-tuned weights, train in hours instead of days, and can be hot-swapped at inference time.

**The ModelPlane approach:** Fine-tuning is a compute job, not an inference operation. ModelPlane's control plane manages inference — running training jobs is out of scope for the same reason container building is. But deploying the *result* of fine-tuning — a full model or a LoRA adapter — is squarely in scope. The CLI should make the deploy-after-train step seamless.

**Deploying a fine-tuned model (full weights):**

No different from any custom model. The fine-tuned weights live in HuggingFace (or a private registry), and the Model CRD points to them:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: Model
metadata:
  name: llama3-8b-medical
spec:
  model:
    name: my-org/llama3-8b-medical-v2
  source: HuggingFace
  huggingFace:
    repo: my-org/llama3-8b-medical-v2
    revision: v2.1                       # Pin to a specific version
    secretRef:
      name: hf-token
      namespace: ml-team
      key: token
  resources:
    vram: 24Gi
  serving:
  - name: vllm-kserve
    backend: KServe
    engine:
      name: vLLM
      image: vllm/vllm-openai:latest
```

```bash
$ mp init my-medical-model
# Edit model.yaml: point to fine-tuned HF repo
$ mp deploy -f my-medical-model/model.yaml --env prod-gpu-east
```

**Why no `mp finetune` command:** Fine-tuning is a training job — it needs a dataset, GPU hours, hyperparameter tuning, experiment tracking (W&B, MLflow). These are the domain of training platforms (Anyscale, Modal, SageMaker, or raw PyTorch on Kubernetes). ModelPlane adding a training orchestrator would violate the "narrow by design" positioning. The right integration point is the output: fine-tuned weights land in HuggingFace or object storage, and ModelPlane deploys them.

**Future integration consideration:** If design partners consistently request a tighter train→deploy loop, the CLI could support a `--from-training-job` flag that polls a training job (via MLflow, W&B, or a Kubernetes Job) and automatically creates a Model CRD when it completes. This is orchestration glue, not a training engine.

### 8.3 LoRA Adapter Management

**The landscape:** LoRA adapter serving is becoming a first-class capability in inference engines. vLLM supports multi-LoRA natively (`--enable-lora --lora-modules name=path`), with dynamic loading via REST API and per-request adapter selection via the `model` field in the OpenAI-compatible API. TensorRT-LLM uses a two-level LoRA cache with per-request `task_id` routing. llm-d (CNCF Sandbox) adds cache-aware adapter routing at the gateway level. The pattern is clear: one base model serves many adapters, selected per-request.

**The ModelPlane approach:** LoRA adapters are a deployment-level configuration on a base model, not separate Model resources. A single base model deployment can serve multiple adapters, with per-request routing through the existing OpenAI-compatible API.

**CRD extension — `spec.serving[].adapters` (post-v0.1):**

The field is intentionally named `adapters`, not `lora` — LoRA is the dominant adapter technique today, but the CRD should express intent (serve multiple adapted variants of this base model) without encoding mechanism (use LoRA specifically). See Section 8.6 on ecosystem velocity risk.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: Model
metadata:
  name: llama3-8b-multitenant
spec:
  model:
    name: meta-llama/Meta-Llama-3-8B-Instruct
  source: HuggingFace
  huggingFace:
    repo: meta-llama/Meta-Llama-3-8B-Instruct
  resources:
    vram: 28Gi          # Base model + adapter overhead
  serving:
  - name: vllm-kserve
    backend: KServe
    engine:
      name: vLLM
      image: vllm/vllm-openai:latest
    adapters:
      enabled: true
      maxAdapters: 8
      modules:
      - name: medical-qa
        source: my-org/llama3-medical-lora
        # HuggingFace adapter repo
      - name: legal-summarize
        source: my-org/llama3-legal-lora
      - name: french-translation
        source: my-org/llama3-french-lora
```

The composition function translates this into the engine-specific flags — for vLLM today, that's `--enable-lora --lora-modules medical-qa=/path legal-summarize=/path french-translation=/path --max-loras 8`. If a future engine uses a different adapter mechanism, the composition function adapts; the CRD field stays the same.

**CLI integration:**

```bash
# Deploy base model with adapters defined in YAML
$ mp deploy -f llama3-multitenant.yaml --env prod-gpu-east

# Predict using a specific adapter — uses the model field in the OpenAI API
$ mp predict llama3-8b-multitenant -i "Summarize this contract..." --model legal-summarize

# Predict using the base model (no adapter)
$ mp predict llama3-8b-multitenant -i "Hello, world"
```

The `--model` flag on `mp predict` maps to the `model` field in the OpenAI-compatible chat completions request, which is how vLLM and other engines route to a specific LoRA adapter. No new API surface — this is the standard multi-LoRA serving pattern.

**Dynamic adapter management (post-v0.2):**

For teams that add and remove adapters frequently, updating the Model CRD YAML every time is friction. A future `mp adapters` command could manage adapters on a running deployment:

```bash
$ mp adapters list llama3-8b-multitenant
NAME                SOURCE                          STATUS
medical-qa          my-org/llama3-medical-lora       Loaded
legal-summarize     my-org/llama3-legal-lora         Loaded
french-translation  my-org/llama3-french-lora        Loaded

$ mp adapters add llama3-8b-multitenant spanish-qa my-org/llama3-spanish-lora
Adapter 'spanish-qa' added. Loading...

$ mp adapters remove llama3-8b-multitenant french-translation
Adapter 'french-translation' removed.
```

Under the hood, this patches the Model CRD's `adapters.modules` array and the control plane reconciles. The CRD remains the source of truth — the CLI command is a convenience wrapper around `kubectl patch`.

### 8.4 Model Evaluation and Benchmarking

**The landscape:** Two distinct evaluation dimensions: *quality* (does the model give correct answers?) and *performance* (does it meet latency and throughput SLAs?). For quality, EleutherAI's lm-evaluation-harness is the standard — 60+ academic benchmarks, used by the HuggingFace Open LLM Leaderboard. For performance, vLLM's benchmarking CLI measures TTFT (Time to First Token), TPOT (Time Per Output Token), ITL (Inter-Token Latency), and throughput at configurable request rates.

**The ModelPlane approach:** The CLI should make it trivial to benchmark a *deployed* model. This is a natural extension of `mp predict` — instead of one request, send many and report metrics. Quality evaluation is delegated to lm-evaluation-harness (which already supports remote endpoints); the CLI provides the endpoint wiring.

**CLI commands — `mp bench` (post-v0.1):**

```bash
# Performance benchmark — measure latency and throughput
$ mp bench llama3-8b --prompts 500 --rate 10 --output json
Benchmarking llama3-8b at 10 req/s...
  TTFT (p50/p90/p99):     42ms / 68ms / 120ms
  TPOT (p50/p90/p99):     12ms / 18ms / 31ms
  Throughput:              847 tokens/s
  Completed:              500/500 requests
  Errors:                 0

# Compare across environments
$ mp bench llama3-8b --all-envs --prompts 200 --rate 5
  ENV                  TTFT p50   TTFT p99   THROUGHPUT
  prod-gpu-east        42ms       120ms      847 tok/s
  coreweave-research   38ms       95ms       1024 tok/s

# Quality evaluation — generate the command to run lm-evaluation-harness
$ mp eval llama3-8b --tasks mmlu,gsm8k,hellaswag
Running: lm_eval --model local-completions \
  --model_args model=llama3-8b,base_url=http://172.18.255.200/ml-team/llama3-8b/v1/completions \
  --tasks mmlu,gsm8k,hellaswag \
  --output_path ./eval-results
```

**Design decisions:**

- `mp bench` is **native** — it owns the HTTP client, metrics collection, and reporting. This is the same pattern as `mp predict` (endpoint discovery + request formatting) but with load generation and statistical aggregation.
- `mp eval` **delegates** to lm-evaluation-harness — it discovers the endpoint, constructs the correct `lm_eval` command, and either runs it or prints it for the user. This follows the "own the workflow, delegate the CRUD" principle. lm-evaluation-harness is the standard; reimplementing benchmark tasks would be a maintenance burden with no upside.
- `--output json` enables CI integration — teams can run `mp bench` in pipelines and fail on latency regressions.
- `--all-envs` reuses the same pattern as `mp status --all-envs` for cross-backend comparison (user journey J3).

**Standard metrics:**

| Metric | What it measures | Why it matters |
|--------|-----------------|----------------|
| TTFT | Time to first token | User-perceived responsiveness |
| TPOT | Time per output token | Streaming speed |
| ITL | Inter-token latency (p50/p90/p99) | Consistency of streaming |
| Throughput | Output tokens/second | Capacity planning |
| Error rate | Failed requests / total | Reliability |

### 8.5 Model Versioning and Promotion

**The landscape:** Two competing models exist: stage-based (MLflow's None → Staging → Production → Archived) and version-based (HuggingFace Hub's git tags and revisions). MLflow's model is more opinionated, fitting teams with formal release processes. HuggingFace's model is simpler — just git semantics. For Kubernetes-native platforms, versioning maps naturally to CRD annotations and labels.

**The ModelPlane approach:** The Model CRD already supports `huggingFace.revision` for pinning to a specific checkpoint version. For model promotion (staging → production), this is a deployment-level concern, not a model-level one — the same model version gets deployed to different environments via `--env`. No new abstractions needed.

**Version pinning (already supported):**

```yaml
spec:
  huggingFace:
    repo: my-org/llama3-medical
    revision: v2.1    # Pin to tag, branch, or commit SHA
```

```bash
# Deploy a specific version
$ mp deploy -f model.yaml --env staging

# Promote to production: same model, different environment
$ mp deploy -f model.yaml --env production
```

**Rolling updates (post-v0.1):**

When `revision` is updated in the Model CRD, the control plane should reconcile — rolling out the new version to all placements. The CLI simplifies this:

```bash
# Update the model version in a running deployment
$ mp update llama3-medical --revision v2.2
Updating llama3-medical to revision v2.2...
Rolling update in progress. Run `mp status llama3-medical --watch` to track.
```

Under the hood, this patches `spec.huggingFace.revision` on the Model CRD. The control plane handles the rollout strategy (rolling, blue-green, canary) based on deployment policy.

**Canary deployments (post-v0.2):**

For teams that need gradual rollouts — especially when deploying new fine-tunes or quantized variants — the ModelDeployment CRD could support traffic splitting:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: llama3-medical
spec:
  modelRef:
    kind: Model
    name: llama3-medical
  environments: 1
  rollout:
    strategy: canary
    canary:
      weight: 10            # 10% traffic to new version
      modelRef:
        kind: Model
        name: llama3-medical-v3
```

```bash
$ mp deploy llama3-medical-v3 --canary 10 --baseline llama3-medical
Canary deployment: 10% → llama3-medical-v3, 90% → llama3-medical
Run `mp bench llama3-medical --canary` to compare versions.
```

This composites naturally with `mp bench --all-envs` for data-driven promotion decisions.

### 8.6 Risk: Ecosystem Velocity and Technique Churn

**The risk is real.** The ML inference ecosystem moves faster than any CRD schema process can. In the last 18 months: FP8 quantization went from experimental to default, LoRA went from a research paper to a production multi-tenant serving primitive, speculative decoding became a standard latency optimization, and entirely new serving architectures (disaggregated prefill/decode, KV cache–aware routing) emerged. The techniques enumerated in this section — FP8, AWQ, GPTQ, LoRA, the specific lm-evaluation-harness benchmarks — are a snapshot of mid-2026. Some will be table stakes in a year. Others may be superseded by techniques that don't exist yet (MX4 quantization, new adapter architectures beyond LoRA, reasoning-model-specific serving patterns).

If ModelPlane bakes today's techniques into first-class CRD fields too eagerly, three things happen: (1) new techniques require CRD schema changes — which are versioned API contracts with upgrade implications, (2) the CLI accumulates technique-specific commands (`mp adapters`, `mp quantize`) that become dead weight when the technique evolves, and (3) documentation and catalog models reference specific formats that age out.

**The mitigation is architectural, not procedural.** ModelPlane already has the right pattern — the key is to apply it deliberately:

**Layer 1: Opaque passthrough (`engine.args`) — always works, day zero.** The existing `engine.args` array passes CLI flags directly to the engine container without ModelPlane interpreting them. When a new quantization format, serving optimization, or adapter technique ships in vLLM or SGLang, users can use it immediately by adding the flag to `engine.args`. No CRD change, no ModelPlane release. This is the escape hatch that ensures ModelPlane never blocks adoption of a new technique.

```yaml
# Day-zero support for a hypothetical new technique:
engine:
  args:
  - --speculative-decoding=draft-model
  - --kv-cache-routing=locality-aware
  - --quantization=mx4       # New format, no CRD change needed
```

**Layer 2: Composition functions — engine-aware translation, independently updatable.** When a technique stabilizes and warrants a first-class field, the composition function handles the translation from a portable CRD field (`quantization: fp8`) to engine-specific flags (`--quantization fp8` for vLLM, different syntax for SGLang or Dynamo). Composition functions are shipped as a Crossplane Configuration package and can be updated independently of the CRD schema version. Adding support for a new quantization format in the translation layer doesn't require a CRD version bump.

**Layer 3: First-class CRD fields — only for stabilized, cross-engine concepts.** A technique earns a first-class CRD field when it meets three criteria: (a) supported by at least two engines/backends, (b) stable across at least two engine major versions, and (c) the user-facing semantics are portable (e.g., "fp8" means roughly the same thing to vLLM and SGLang). This is a high bar by design. Most techniques should live in `engine.args` for 6-12 months before promotion to a first-class field.

**Layer 4: CLI commands — only for workflow gaps, not technique wrappers.** `mp bench` is a workflow command (send requests, collect metrics) that works regardless of what model architecture or optimization is underneath. `mp adapters` is riskier — it's tied to the LoRA serving pattern specifically. If adapter architectures change fundamentally, the command becomes dead weight. The mitigation: `mp adapters` should be framed generically as adapter management (not LoRA management), and should only be added when the adapter-per-request pattern is clearly durable — not before.

**What this means for the proposals in this section:**

| Proposal | Risk level | Rationale |
|----------|-----------|-----------|
| Quantization via `engine.args` | Low | Passthrough, no opinion embedded |
| First-class `quantization` field | Medium | FP8/AWQ/GPTQ are stable today but new formats will emerge. Field should accept strings, not an enum, so new formats don't require schema changes |
| LoRA config in CRD | Medium | LoRA is dominant now but adapter architectures may evolve. Field should be named `adapters`, not `lora`, for portability |
| `mp bench` | Low | Performance benchmarking is architecture-agnostic |
| `mp eval` delegating to lm-eval-harness | Low | Delegation means we inherit their updates |
| `mp adapters` command | High | Technique-specific CLI surface. Defer until adapter serving pattern proves durable |
| Canary deployments | Low | Traffic management is model-architecture-agnostic |
| `scaling` field with `signal: Fixed\|Concurrency` (v0.1) | Low | Signal-based design is engine-agnostic. New signals extend the enum without breaking existing fields. The Envoy in-flight metric works across backends, decoupling intent from engine specifics. |
| Backend-specific signal variants (e.g. `signal: WVA`) | Medium | First-class CRD fields, but intentionally non-portable — selecting one constrains placement. Discriminated-union design contains the risk: each variant evolves independently, removing one doesn't affect the others. The CLI deliberately doesn't expose backend-specific signals as flags, so the abstraction-leak is opt-in via YAML. See Decision 6. |

**The principle:** ModelPlane's CRD schema should express *intent* (I want this model quantized, I want adapters served, I want this version deployed) without encoding *mechanism* (use this specific quantization algorithm, use this specific adapter format). The composition functions and engines handle mechanism. Intent is stable; mechanism churns.

This is the same principle Crossplane applies to cloud infrastructure: the XRD expresses "I want a database with 100GB storage" — not "create an RDS instance with gp3 EBS volumes." When AWS ships a new storage type, the Composition updates; the XRD doesn't change.

### 8.7 Summary — What Ships When

| Capability | v0.1 | v0.2 | v0.3+ |
|-----------|------|------|-------|
| Quantization via `engine.args` | Yes | Yes | Yes |
| First-class `quantization` field | — | Yes | Yes |
| Deploy fine-tuned models from HF | Yes | Yes | Yes |
| Adapter config in CRD (`adapters`) | — | Yes | Yes |
| Dynamic adapter management (`mp adapters`) | — | — | Yes |
| Fixed scaling (`--replicas`) | Yes | Yes | Yes |
| Concurrency autoscaling (`--min/--max/--target`) | Yes | Yes | Yes |
| Scale-to-zero (`--scale-to-zero` / `--min 0`) | Yes | Yes | Yes |
| Streaming predict (`mp predict --stream`) | Yes | Yes | Yes |
| JSON output (`--output json`) on all native commands | Yes | Yes | Yes |
| Auto-generated docs from Click command tree | Yes | Yes | Yes |
| Performance benchmarking (`mp bench`) | — | Yes | Yes |
| Quality evaluation (`mp eval`) | — | — | Yes |
| Version pinning via `revision` | Yes | Yes | Yes |
| Rolling updates (`mp update`) | — | Yes | Yes |
| Canary deployments | — | — | Yes |

The principle throughout: lifecycle operations that are **properties of the model or deployment** (quantization, adapters, version, scaling) live in the CRD as declarative fields and are surfaced as `mp deploy` flags — not as separate verbs. Operations that **act on a deployment** (benchmark, evaluate, update) are CLI commands. Operations that **produce artifacts** (fine-tuning, offline quantization, compilation) are out of scope — the CLI deploys the result, not the pipeline.

---

## 9. Open Questions for Team Discussion

1. **Command naming:** The v0.1 doc uses `modelplane model deploy` (noun-verb). This proposal uses `mp deploy` (flat verbs). Flat is faster to type and more Truss-like. Hierarchical is more discoverable. Preference?

2. **Python SDK:** The v0.1 doc flags a Python SDK as desirable. Since the CLI uses standard CRD YAML, a Python SDK could use the same schema via the `kubernetes` client. Should it mirror the CLI or use a Pythonic API (like Modal's decorator pattern)?

3. **NIM model source:** The Model CRD supports `source: HuggingFace`. NIM models are container microservices — a fundamentally different pattern. How should this be represented in the CRD?

4. **`--env` label convention:** `--env` works by reading the target environment's labels. Should we formalize a `modelplane.ai/env-name` label convention for robustness?

5. **Scale-to-zero ergonomics:** Opting into scale-to-zero requires `--scale-to-zero --max M --target T` since the CRD requires `maxReplicas` and `target`. Should the CLI infer reasonable defaults when `--scale-to-zero` is the only scaling flag, or keep them required to mirror CRD validation? The latter is more honest; the former is more ergonomic.

6. **`mp scale` convenience verb:** There is no `mp scale NAME --max 10` command — users re-run `mp deploy` or patch the YAML. Industry tools (kubectl, knative) all have a scale verb. Worth adding as a thin wrapper around `kubectl patch`, or does it muddy the "scaling is a property" model?

7. **When do backend-specific fields earn first-class CRD treatment?** Decision 6 introduces a `WVA` scaling signal that pins a deployment to KServe — a deliberate non-portable variant in an otherwise portable union. As more backend-specific features accumulate (TensorRT-LLM compile cache, Dynamo router knobs, KServe-specific deployment options), what's the threshold for promoting them from `engine.args` passthrough to first-class CRD variants? Strawman: a backend-specific feature earns a first-class CRD variant when (a) it's stable across two engine major versions, (b) at least one customer asks for it, and (c) it's important enough that ML teams would otherwise be writing the engine flag by hand. Below that bar, `engine.args` is the right home.

8. **CLI policy for backend-specific signals.** Decision 6 says portable signals get CLI flags and backend-specific signals stay YAML-only. Is that the right cut, or should we also expose backend-specific signals via flags (with a `--backend-pin` warning, or by namespacing them like `--kserve-wva-min N`)? The YAML-only stance keeps the CLI surface small but means anyone using WVA writes YAML.

---

## References

| Resource | URL |
|----------|-----|
| Truss CLI docs | https://docs.baseten.co/reference/cli/truss/overview |
| Truss config.yaml reference | https://docs.baseten.co/reference/truss-configuration |
| Cog (Replicate) cog.yaml | https://github.com/replicate/cog/blob/main/docs/yaml.md |
| BentoML build options | https://docs.bentoml.com/en/latest/reference/bentoml/bento-build-options.html |
| Modal GPU docs | https://modal.com/docs/guide/gpu |
| HuggingFace CLI reference | https://huggingface.co/docs/huggingface_hub/en/package_reference/cli |
| Click (Python CLI framework) | https://click.palletsprojects.com/ |
| vLLM quantization docs | https://docs.vllm.ai/en/latest/features/quantization/ |
| vLLM LoRA serving | https://docs.vllm.ai/en/latest/features/lora/ |
| vLLM benchmark CLI | https://docs.vllm.ai/en/latest/benchmarking/cli/ |
| Baseten Engine Builder config | https://docs.baseten.co/performance/engine-builder-config |
| Together AI fine-tuning | https://docs.together.ai/reference/finetune |
| Fireworks AI fine-tuning | https://docs.fireworks.ai/fine-tuning/fine-tuning-models |
| HuggingFace AutoTrain | https://huggingface.co/docs/autotrain/en/tasks/llm_finetuning |
| lm-evaluation-harness | https://github.com/EleutherAI/lm-evaluation-harness |
| llm-compressor (vLLM) | https://github.com/vllm-project/llm-compressor |
| MLflow Model Registry | https://mlflow.org/docs/latest/ml/model-registry/workflow/ |
| llm-d architecture | https://llm-d.ai/docs/architecture |
| ModelPlane v0.1 scope doc | (internal) |
