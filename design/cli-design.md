# ModelPlane CLI (`mp`) — Design Proposal

**Authors:** Dennis Ramdass

**Status:** Draft — For Team Review

**Date:** April 2026

**Related:** ModelPlane v0.1 Scope Document

---

## 1. Goals

- **ML-first UX for the 80% case.** An ML engineer should be able to deploy a catalog model and test it in under 60 seconds, without writing YAML or knowing what a namespace is. The CLI is their primary interface to ModelPlane.
- **No new abstractions to learn.** When configuration is needed beyond the happy path, the CLI uses the same Model CRD YAML that the platform team already knows — not a custom format that drifts out of sync.
- **Don't reimplement kubectl.** For listing, inspecting, and deleting resources, delegate to kubectl transparently. Own only the workflow gaps that kubectl can't fill: zero-config deploy, endpoint discovery, request formatting.
- **Align with the best ML deployment UX in the industry.** ML engineers already know Truss, HuggingFace CLI, and Cog. The CLI should feel familiar — scaffold, edit, deploy, predict — not like a Kubernetes tool with ML branding.
- **Support the v0.1 user journeys.** J1 (deploy first model), J3 (cross-backend comparison), and the CLI section of the v0.1 scope doc are the acceptance criteria.

### Non-Goals

- **Replace kubectl for platform teams.** Infra engineers are comfortable with kubectl and GitOps. The CLI is not their primary tool.
- **Full lifecycle management.** Autoscaling policies, observability dashboards, and fleet-wide operations are out of scope for v0.1. The CLI covers deploy, check, and test.
- **Custom model packaging.** Unlike Truss or Cog, the CLI does not build containers or package model code. ModelPlane delegates model pulling to backends (KServe, Dynamo). The CLI deploys — it doesn't build.
- **Multi-cluster orchestration from the CLI.** Placement across environments is handled by the ModelPlane control plane, not the CLI. The CLI submits intent (`--env`, `--replicas`); the scheduler does the rest.

---

## 2. Problem Statement

The v0.1 release doc identifies a clear gap: *"ML engineers are allergic to kubectl — even a thin wrapper adds meaningful value."* Today, deploying a model on ModelPlane requires writing Kubernetes YAML and using `kubectl apply`. This is a non-starter for ML teams who think in terms of models, not manifests.

But the answer isn't to reimplement kubectl behind a different name. ModelPlane's CRDs already have clean printer columns — `kubectl get clustermodels` works fine. The real gaps are the things kubectl *can't* do:

1. **Deploy a catalog model in one command** — no YAML, no `kubectl apply`
2. **Test a deployed model** — discover the endpoint, format the request, make the call
3. **Scaffold a Model YAML** — with all options as commented defaults
4. **Wire up a deployment to a file** — create Model + ModelDeployment in one step

The CLI focuses on these four capabilities and delegates everything else to kubectl.

---

## 3. Design Decisions

### Decision 1: Own the workflow, delegate the CRUD

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

**What this means in practice:** The delegating commands are ~10 lines of Python each (run subprocess, exit with its return code). The total CLI is ~500 lines of real logic concentrated in `deploy.py`, `predict.py`, `status.py`, and `init.py`.

### Decision 2: No custom config format — use CRD YAML directly

**What:** When ML teams define a custom model, they write a standard `Model` CRD YAML. The CLI scaffolds a well-commented template via `mp init`, and deploys it via `mp deploy -f`. There is no intermediate config format.

**Why:** We evaluated the config-file patterns used by Truss, Cog, BentoML, and Modal. These tools invented custom formats because they have no backing Kubernetes CRD — the config file *is* their schema. ModelPlane already has a well-defined schema: the `Model` CRD.

Introducing a second format would mean a translation layer to maintain, two schemas to document, and drift when CRD fields are added (Dynamo, NIM, serving profiles). The Model CRD YAML is already ~12 lines for the common case. The CLI adds value through *workflow* (scaffold → edit → deploy), not format translation.

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
  engine: vLLM
  resources:
    vram: 24Gi
```

**Industry references:**
- Truss config.yaml: https://docs.baseten.co/reference/truss-configuration
- Cog cog.yaml: https://github.com/replicate/cog/blob/main/docs/yaml.md
- BentoML bentofile.yaml: https://docs.bentoml.com/en/latest/reference/bentoml/bento-build-options.html
- Modal decorators: https://modal.com/docs/guide/gpu

### Decision 3: Two deployment paths — catalog and file

**What:** ML teams deploy in two ways:

1. **From catalog** (zero config): `mp deploy llama3-8b` — one command, no files
2. **From file** (full control): `mp deploy -f model.yaml` — CLI creates Model + ModelDeployment in one step

**Why:** The catalog path is the 80% case — platform teams curate approved models, ML teams pick one. The file path is the escape hatch for fine-tuned models or experimental configs. Most users should never need a file.

### Decision 4: `--env` for environment targeting, `--replicas` for fan-out

**What:** Two orthogonal flags:
- `--env prod-gpu-east` targets a specific InferenceEnvironment by name
- `--replicas 2` deploys across N environments (auto-scheduled)

**Why:** The v0.1 user journeys require both: J1 deploys to a specific environment, J3 deploys across backends for comparison. Under the hood, `--env` reads the target environment's labels and uses them as `environmentSelector.matchLabels`.

### Decision 5: Smart `predict` — one command for any model type

**What:** `mp predict <name> -i "input"` auto-detects the input format and routes to the correct endpoint:
- Plain text → wrapped as chat completion
- JSON with `messages` → forwarded to `/chat/completions`
- JSON with `input` → forwarded to `/responses`

**Why:** A single command handles all model types and future-proofs against new API shapes (OpenAI Responses API, embeddings, etc.) without adding new CLI commands.

### Decision 6: `mp init` scaffolds commented CRD YAML (like `truss init`)

**What:** `mp init my-model` creates `my-model/model.yaml` — the actual Model CRD with every field visible as a comment. Users edit and deploy.

**Why:** One of Truss's best UX patterns. Users discover options by reading the scaffold, not by searching docs. `mp init --team` separately handles team context setup.

---

## 4. Command Reference

### Commands with real value (native implementation)

```
mp init NAME                                  # Scaffold a Model YAML template
mp init --team NAME                           # Set team context (one-time)
mp deploy MODEL [--env ENV] [--replicas N]    # Deploy from catalog
mp deploy -f model.yaml [--env ENV]           # Deploy from YAML file
mp status NAME [--watch] [--all-envs]         # Rich status, polling, per-env breakdown
mp predict NAME -i "input" [--raw]            # Smart predict
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

## 5. Workflows

### A. Deploy from Catalog (ML Engineer — the 80% case)

```bash
$ mp init --team ml-team
Team set to: ml-team

$ mp models
NAME              READY   MODEL                          ENGINE   VRAM   AGE
qwen-0.5b-vllm   True    Qwen/Qwen2.5-0.5B-Instruct    vLLM     2Gi    5d
llama-8b-vllm     True    meta-llama/Llama-3-8B          vLLM     24Gi   5d

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
Replicas:    2/2

  ENV                  STATUS   ENDPOINT
  prod-gpu-east        Ready    http://10.0.0.1/.../v1
  coreweave-research   Ready    http://10.0.0.2/.../v1
```

---

## 6. Open Questions for Team Discussion

1. **Command naming:** The v0.1 doc uses `modelplane model deploy` (noun-verb). This proposal uses `mp deploy` (flat verbs). Flat is faster to type and more Truss-like. Hierarchical is more discoverable. Preference?

2. **Python SDK:** The v0.1 doc flags a Python SDK as desirable. Since the CLI uses standard CRD YAML, a Python SDK could use the same schema via the `kubernetes` client. Should it mirror the CLI or use a Pythonic API (like Modal's decorator pattern)?

3. **NIM model source:** The Model CRD supports `source: HuggingFace`. NIM models are container microservices — a fundamentally different pattern. How should this be represented in the CRD?

4. **`--env` label convention:** `--env` works by reading the target environment's labels. Should we formalize a `modelplane.ai/env-name` label convention for robustness?

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
| ModelPlane v0.1 scope doc | (internal) |
