# llm-d v0.7 / GAIE API surface (reference for KServe removal)

Reference artifact for Task 0 of the drop-KServe effort. Pins the external API
(Helm chart coordinates, modelservice values keys, GAIE routing resources, in-cluster
gateway) that the llm-d backend (`functions/compose-model-replica/function/backends/llmd.py`)
and the cluster-install function (`compose-serving-stack`) depend on.

- **Date captured:** 2026-06-02
- **llm-d release in scope:** v0.7.0 (released 2026-05-12)
- Every fact below cites a primary source (URL). Facts that could not be confirmed
  from a primary source are flagged **UNCONFIRMED — verify at implementation**.

> ⚠️ The old `llm-d/llm-d-model-service` operator/CRD is archived/deprecated and is
> NOT documented here. The active surface is the `llm-d-incubation/llm-d-modelservice`
> **Helm chart** (no CRD).

---

## 0. Headline: what v0.7.0 actually pins (read this first)

The llm-d v0.7.0 release notes list a component/version table. Two entries matter and
carry a **surprise** for this effort:

| Component | v0.7.0 | Previous (v0.6.0) | Type |
|-----------|--------|-------------------|------|
| `kubernetes-sigs/gateway-api-inference-extension` (GAIE) | **`v1.5.0`** | `v1.4.0` | Helm Chart |
| `llm-d-incubation/llm-d-infra` | **N/A (Deprecated)** | `v1.4.0` | Helm Chart |
| `llm-d-incubation/llm-d-modelservice` | **N/A (Deprecated)** | `v0.4.9` | Helm Chart |

Infrastructure versions from the same release notes:

| Component | v0.7.0 | Previous |
|-----------|--------|----------|
| Gateway API | `v1.5.1` | `v1.4.0` |
| Istio | `1.29.1` | `1.28.1` |
| agentgateway (formerly kgateway) | `v2.2.1` | `v2.1.1` |

Source: <https://github.com/llm-d/llm-d/releases/tag/v0.7.0> (component summary table,
verified via `gh api repos/llm-d/llm-d/releases/tags/v0.7.0`).

**Interpretation / IMPORTANT for downstream tasks:**
- v0.7.0 marks `llm-d-infra` and `llm-d-modelservice` as **Deprecated** with **no
  version pinned for v0.7.0**. The v0.7.0 well-lit path moves to a docs/YAML-driven
  install (GAIE's own `inferencepool` Helm chart + a gateway) rather than the
  llm-d-infra/modelservice chart pair. The release also introduced a default
  "standalone mode" using a generic GAIE-conformant proxy instead of a full gateway.
- The **last versions the project itself referenced** are `llm-d-modelservice v0.4.9`
  and `llm-d-infra v1.4.0` (both as the v0.6.0 baseline, now deprecated).
- The **latest published chart versions** (still maintained on `main`) are
  `llm-d-modelservice v0.4.12` and `llm-d-infra v1.4.0`.
- **Decision needed at implementation:** the modelservice chart still works and is the
  pragmatic choice for composing a Release per ModelReplica, but it is on a deprecation
  path. Pin a concrete version (`v0.4.12` recommended, or `v0.4.9` to match the last
  llm-d-referenced version) rather than a floating tag. **UNCONFIRMED — verify at
  implementation** whether the team wants to follow llm-d's new YAML/standalone path
  instead of the modelservice chart.

---

## 1. Chart coordinates

### 1a. modelservice chart (per-model install — composed per ModelReplica)

- **Chart name:** `llm-d-modelservice`
  Source: `Chart.yaml` `name:` — `gh api repos/llm-d-incubation/llm-d-modelservice/contents/charts/llm-d-modelservice/Chart.yaml`
- **Helm repo URL:** `https://llm-d-incubation.github.io/llm-d-modelservice/`
  Source: <https://llm-d-incubation.github.io/llm-d-modelservice/>
- **Recommended chart version:** `v0.4.12` (latest; published 2026-04-22).
  - `Chart.yaml`: `version: "v0.4.12"`, `appVersion: "v0.4.0"`.
  - Note tags use the prefixed form `llm-d-modelservice-v0.4.12`; the chart `version`
    field itself is `v0.4.12`.
  - Alternative: `v0.4.9` (2026-03-17) is the version the llm-d v0.6.0 release referenced.
  - Source: `gh api repos/llm-d-incubation/llm-d-modelservice/tags` and `/releases`,
    `Chart.yaml`.
- **Chart dependency:** Bitnami `common` `2.27.0` (`https://charts.bitnami.com/bitnami`).
- **No fixed compatibility matrix** with llm-d core versions is published; the project
  explicitly does not guarantee compatibility with legacy versions.
  Source: <https://llm-d-incubation.github.io/llm-d-modelservice/>

### 1b. llm-d-infra chart (once-per-cluster prerequisite install)

- **Chart name:** `llm-d-infra`
  Source: `Chart.yaml` `name:` — `gh api repos/llm-d-incubation/llm-d-infra/contents/charts/llm-d-infra/Chart.yaml`
- **Helm repo URL:** `https://llm-d-incubation.github.io/llm-d-infra/`
  Install: `helm repo add llm-d-infra https://llm-d-incubation.github.io/llm-d-infra/`
  Source: <https://github.com/llm-d-incubation/llm-d-infra/blob/main/charts/llm-d-infra/README.md>
- **Latest chart version:** `v1.4.0` (`Chart.yaml`: `version: v1.4.0`, `appVersion: v0.4.0`).
  Source: `gh api repos/llm-d-incubation/llm-d-infra/tags` (latest tag `v1.4.0`), `Chart.yaml`.
- **kubeVersion:** `>= 1.28.0-0`. Dep: Bitnami `common` `2.27.0`.
- **Prerequisites** (from infra README): Kubernetes 1.30+ (OpenShift 4.17+), Helm 3.10+,
  Gateway API v1.3.0+, and a GAIE-conformant gateway (kgateway/agentgateway or Istio)
  installed in-cluster.
  Source: <https://github.com/llm-d-incubation/llm-d-infra/blob/main/charts/llm-d-infra/README.md>

> ⚠️ Both charts are **Deprecated as of llm-d v0.7.0** (see §0). They function but the
> upstream is steering toward a YAML/GAIE-chart-based well-lit path. Decision deferred to
> implementation — see §0.

---

## 2. modelservice values keys (exact paths)

All paths below are quoted from the chart `values.yaml` at tag `llm-d-modelservice-v0.4.12`.
Source (verbatim): `gh api "repos/llm-d-incubation/llm-d-modelservice/contents/charts/llm-d-modelservice/values.yaml?ref=llm-d-modelservice-v0.4.12"`
(also browsable at <https://github.com/llm-d-incubation/llm-d-modelservice/blob/main/charts/llm-d-modelservice/values.yaml>).

### 2a. Model artifact URI and accepted forms

- **`modelArtifacts.name`** — the model name used as the `model` parameter in OpenAI
  requests (this is the public model name surfaced to clients). Required.
  Example: `random/model`.
- **`modelArtifacts.uri`** — the model artifact URI. Default `"hf://{{ .Values.modelArtifacts.name }}"`.
  Accepted forms (verbatim from values.yaml comment):
  - `hf://model/name` — model as defined on Hugging Face
  - `pvc://pvc_name/path/to/model` — model on existing persistent volume
  - `oci://` — **not yet supported** (comment: "oci:// not yet supported")
- **`modelArtifacts.authSecretName`** — secret holding credentials (e.g. `HF_TOKEN`).
- **`modelArtifacts.mountPath`** — default `/model-cache`.
- **`modelArtifacts.size`** — volume size for the model (default `5Mi`).
- **`modelArtifacts.readOnly`** — default `true`; set `false` for `pvc+hf://` when HF
  cache writes are needed.

> **CORRECTION vs. an early draft:** there is **no `oci://` support today**, and there is
> a `pvc+hf://` hybrid scheme mentioned in `readOnly` docs. Treat `oci://` as
> unsupported by v0.4.12.

### 2b. Parallelism keys

Per-role under `decode.parallelism.*` and `prefill.parallelism.*`:
- **`<role>.parallelism.tensor`** — tensor parallelism. Default `1`.
- **`<role>.parallelism.data`** — data parallelism. Default `1`.
- **`<role>.parallelism.dataLocal`** — local data parallelism. Default `1`.
- **`<role>.parallelism.workers`** — workers. Default `1`.

> **IMPORTANT CORRECTION:** there is **NO `pipeline` parallelism key** in v0.4.12
> values.yaml. The keys are `tensor`, `data`, `dataLocal`, `workers`. If pipeline
> parallelism is required it must be passed via vLLM container `args`
> (`--pipeline-parallel-size`), not a dedicated values key. **UNCONFIRMED — verify at
> implementation** whether a `pipeline` key exists in any newer chart version.

### 2c. Multinode flag (LeaderWorkerSet vs Deployment)

- **`multinode`** (top-level boolean, default `false`).
  Verbatim comment: "When true, a LeaderWorkerSet is used instead of a Deployment".
  - `false` → role pods rendered as a `Deployment`.
  - `true`  → role pods rendered as a `LeaderWorkerSet` (LWS); enables the
    `subGroupPolicy`, `subGroupExclusiveToplogy`, `hostIPC`, `hostPID` knobs which are
    "Only an option for LWS (multinode)".

### 2d. Prefill / decode replica keys

- **`decode.replicas`** — default `1`. `decode.create` (bool, default `true`).
- **`prefill.replicas`** — default `0`. `prefill.create` (bool, default `true`).
- Autoscaling toggles exist: `decode.autoscaling.enabled`, `prefill.autoscaling.enabled`
  (both default `false`).

### 2e. Engine container image / args / env

Containers are a list under each role: `decode.containers[]` and `prefill.containers[]`.
The first/primary container is conventionally named `vllm`.
- **`<role>.containers[].image`** — engine image. Default `"ghcr.io/llm-d/llm-d-inference-sim:latest"`
  (the simulator; for real serving use a `ghcr.io/llm-d/llm-d-cuda:v0.7.0` etc. image —
  see llm-d v0.7.0 image table in §0).
- **`<role>.containers[].modelCommand`** — one of `vllmServe` (chart prepends
  `vllm serve`), `imageDefault` (use image default; the chart default), or `custom`
  (use `command`).
- **`<role>.containers[].command`** — required when `modelCommand: custom`.
- **`<role>.containers[].args`** — list of strings (e.g. vLLM flags). Default `[]`.
- **`<role>.containers[].env`** — list of EnvVar objects. Default `[]`.
- **`<role>.containers[].resources.limits` / `.requests`** — accelerator/CPU/memory.
- **`<role>.containers[].mountModelVolume`** — default `true`; creates the model volume mount.
- **`<role>.containers[].ports`** — container ports (used for metrics scraping).

Accelerator selection: top-level **`accelerator.type`** (default `nvidia`; supports
`nvidia, intel-i915, intel-xe, intel-gaudi, amd, google, cpu, rebellions-atom`) and
**`accelerator.dra`** (Dynamic Resource Allocation, default `false`).
The GPU resource is derived from `accelerator.resources.<type>` (e.g. `nvidia.com/gpu`).

### 2f. Routing / vLLM serving port and sidecar

- **`routing.servicePort`** — port the inference engine (vLLM) listens on. Default `8000`.
  Must match the GAIE InferencePool `targetPorts[].number` (see §3).
- **`routing.proxy.enabled`** — routing sidecar (`llm-d-routing-sidecar`), default `true`.
- **`routing.proxy.image`** — default `ghcr.io/llm-d/llm-d-routing-sidecar:latest`
  (pin to `v0.8.0` for llm-d v0.7.0 per §0 image table).
- **`routing.proxy.targetPort`** — port vLLM listens on behind the sidecar, default `8200`.
- **`routing.proxy.connector`** — KV connector, default `nixlv2`.

### 2g. Pod labels (so GAIE selectors match) — KEY INTEGRATION POINT

- **`modelArtifacts.labels`** — map of labels added to the serving pods. Default:
  ```yaml
  modelArtifacts:
    labels:
      llm-d.ai/inference-serving: "true"
  ```
  Verbatim comment: "Labels that will be added to the pods serving the model. These
  should match the labels of any associated InferencePool."
- In addition the chart **automatically adds** the label **`llm-d.ai/role`** with value
  `prefill` or `decode` depending on the pod's role.
- **Integration contract:** the GAIE `InferencePool.spec.selector` (see §3) MUST select
  on a subset of `modelArtifacts.labels`. The backend must set the *same* label map on
  both the modelservice values and the InferencePool selector. Note that
  `llm-d.ai/inference-serving: "true"` alone matches **both** prefill and decode; to
  pin a pool to one role add `llm-d.ai/role: decode`.

---

## 3. GAIE routing resources (v1.5.0 — the version llm-d v0.7.0 pins)

All Go-type / chart facts verified against tag `v1.5.0` of
`kubernetes-sigs/gateway-api-inference-extension`
(`gh api repos/kubernetes-sigs/gateway-api-inference-extension/...?ref=v1.5.0`).
Docs site: <https://gateway-api-inference-extension.sigs.k8s.io/api-types/inferencepool/>.

### 3a. InferencePool — GRADUATED TO v1

- **apiVersion:** **`inference.networking.k8s.io/v1`** (kind `InferencePool`).
  - Confirmed: `api/v1/zz_generated.register.go` → `GroupName = "inference.networking.k8s.io"`,
    `GroupVersion{..., Version: "v1"}`.
  - This is the `k8s.io` group (GA), **NOT** the older `inference.networking.x-k8s.io`
    alpha group.
- **`spec` shape (from `api/v1/inferencepool_types.go`):**
  ```yaml
  apiVersion: inference.networking.k8s.io/v1
  kind: InferencePool
  spec:
    selector:                     # LabelSelector — which Pods are pool members
      matchLabels:
        llm-d.ai/inference-serving: "true"
    targetPorts:                  # LIST of ports (NOT a scalar targetPortNumber)
      - number: 8000              # 1..65535; must match routing.servicePort
    endpointPickerRef:            # the EPP reference (see 3c)
      name: <epp-service-name>
      port:
        number: 9002
      failureMode: FailOpen       # FailOpen | FailClose
  ```
  - **IMPORTANT FIELD-NAME CORRECTIONS** for v1:
    - The port field is **`targetPorts`** (a list of `{ number: <int> }`), **not**
      `targetPortNumber` (singular scalar) and **not** `targetPortNumber` from older drafts.
    - The extension reference is **`endpointPickerRef`** in v1, **not** `extensionRef`.
      (`extensionRef` was the field name in the older v1alpha2 spec; the GAIE docs site
      prose still says "extensionRef" generically, but the v1 Go type and the rendered
      chart template both use `endpointPickerRef`.)
  - `endpointPickerRef` fields: `name`, optional `kind`/`group` (defaults to a Service),
    `port.number`, and `failureMode` (`FailOpen` default, or `FailClose`).
  - Optional `appProtocol` on spec (`http` or `kubernetes.io/h2c` for gRPC).
  - Rendered example from GAIE's own chart template
    (`config/charts/inferencepool/templates/inferencepool.yaml`, ref `v1.5.0`) confirms
    all of the above verbatim.

### 3b. InferenceObjective — renamed from InferenceModel, now v1alpha2

- **apiVersion:** **`inference.networking.x-k8s.io/v1alpha2`** (kind `InferenceObjective`).
  - NOTE the **`x-k8s.io`** group (still alpha) — different group from InferencePool's
    GA `k8s.io` group. Confirmed: types live in `apix/v1alpha2/inferenceobjective_types.go`;
    CRD file `config/crd/bases/inference.networking.x-k8s.io_inferenceobjectives.yaml`.
- **`spec` shape (from `apix/v1alpha2/inferenceobjective_types.go`):**
  ```yaml
  apiVersion: inference.networking.x-k8s.io/v1alpha2
  kind: InferenceObjective
  spec:
    priority: 0          # *int — flow-control priority; unset == 0.
                         # Higher served first (e.g. 10 before 0 before -10).
    poolRef:             # reference to the InferencePool in the SAME namespace
      name: <pool-name>
  ```
- **IMPORTANT — model-name mapping moved:** InferenceObjective is the rename of the old
  `InferenceModel`, but in v1.5.0/v1alpha2 its spec **only** carries `priority` + `poolRef`.
  There is **NO `modelName` field and NO `criticality` enum** anymore (criticality was the
  old InferenceModel field; it is now the integer `priority`). The public model-name →
  pool mapping is therefore **NOT** done by InferenceObjective. It is done by:
  1. the model name the engine advertises (`modelArtifacts.name` in §2a), and
  2. HTTP routing (an `HTTPRoute` whose backendRef is the InferencePool).
  Source: `inferenceobjective_types.go` (spec has `Priority *int` + `PoolRef`),
  docs note "InferenceObjective currently houses only `Priority`"
  (<https://gateway-api-inference-extension.sigs.k8s.io/api-types/inferenceobjective/>).
  - **UNCONFIRMED — verify at implementation:** exact `poolRef` sub-fields (`group`/`kind`
    default to the InferencePool GVK). Confirm against the rendered
    `config/charts/inferencepool/templates/inferenceobjectives.yaml` when wiring it.

### 3c. EPP (endpoint picker) — per-pool, rendered by GAIE's chart, NOT by modelservice

- The InferencePool's `endpointPickerRef` points at an **EPP** (endpoint-picker /
  ext-proc) Service. The EPP watches pool-member metrics (KV-cache utilization, queue
  length) and picks the endpoint per request.
- **Shared vs per-pool:** the well-lit path uses a **per-pool EPP** — GAIE's
  `inferencepool` Helm chart renders one EPP `Deployment`+`Service` **per InferencePool**
  (per chart release), wired to that pool. (Chart deps: `inferencepool` → `inferenceExtension`
  subchart `epplib`; templates `inferenceextension.yaml` render the EPP, `inferencepool.yaml`
  renders the pool, `inferenceobjectives.yaml` the objectives, `httproute.yaml` the route.)
  Source: `gh api repos/kubernetes-sigs/gateway-api-inference-extension/contents/config/charts/inferencepool/...?ref=v1.5.0`.
- **Who renders the EPP:** the **`llm-d-modelservice` chart does NOT render the EPP, the
  InferencePool, the InferenceObjective, or the HTTPRoute.** It only renders the
  model-serving `Deployment`/`LeaderWorkerSet` (plus routing sidecar). The InferencePool +
  EPP + InferenceObjective + HTTPRoute are installed **separately** — either via GAIE's
  own `inferencepool` Helm chart or as standalone manifests.
  Source: <https://github.com/llm-d-incubation/llm-d-modelservice/blob/main/README.md>
  ("To do this, the Kubernetes Gateway API Inference Extension (GAIE) Helm charts can be
  used"; "HTTPRoute creation is not part of either chart").
- **Implication for the Modelplane llm-d backend:** the backend must emit, in addition to
  the modelservice `Release`, the GAIE objects itself (InferencePool v1, optional
  InferenceObjective v1alpha2, an EPP Deployment/Service, and an HTTPRoute) OR compose a
  second `Release` of GAIE's `inferencepool` chart. **Decision deferred to the llm-d
  backend task.** The label contract in §2g is the join key.
- GAIE `inferencepool` chart `endpointPickerRef` defaults (from template): `port.number`
  `9002` (extProcPort), `failureMode: FailOpen`.

---

## 4. In-cluster gateway

**Modelplane installs Envoy Gateway today.** llm-d v0.7.0 has moved away from Envoy:

- llm-d v0.7.0 release notes: a UX change makes the **default** deployment use
  "standalone mode" with a "generic [GAIE-conformant] proxy instead of the more feature
  full gateway" (still recommends a full gateway for production). PRs in the release:
  "[Docs] Envoy Proxy -> GAIE-Conformant Proxy" (#1162) and "[Docs] Remove envoy
  reference" (#1225). Source: <https://github.com/llm-d/llm-d/releases/tag/v0.7.0>.
- The v0.7.0 well-lit path / gateway guidance:
  - **kgateway is deprecated in llm-d** and slated for removal; **agentgateway**
    (the renamed kgateway, CNCF/Linux-Foundation, Rust dataplane, AI-focused) is the
    **preferred** GAIE-conformant gateway for new self-installed inference deployments.
  - **Istio** is also a fully supported / default-capable GAIE-conformant option.
  - Source: <https://llm-d.ai/docs/usage/customizing-your-gateway> (via search;
    page is JS-rendered — see UNCONFIRMED note below).

### Recommendation

- **Pin agentgateway `v2.2.1`** (the version llm-d v0.7.0's infra table pins) as the
  GAIE-conformant in-cluster gateway, OR **Istio `1.29.1`** (also pinned by v0.7.0) if
  the team prefers a service-mesh-capable gateway. Either is GAIE-conformant for
  InferencePool v1.
- Also pin **Gateway API `v1.5.1`** and **GAIE `v1.5.0`** CRDs/charts to match v0.7.0.
- **Modelplane should drop Envoy Gateway** from the serving stack and replace it with
  agentgateway (recommended) or Istio. Envoy Gateway is no longer the llm-d path.
  - **UNCONFIRMED — verify at implementation:** whether Envoy Gateway *can* still serve
    as a GAIE-conformant gateway for InferencePool v1 (it historically supported GAIE).
    If Modelplane wants minimal disruption, confirm Envoy Gateway's InferencePool v1
    conformance against
    <https://gateway-api-inference-extension.sigs.k8s.io/implementations/gateways/>
    before committing. The clean, llm-d-aligned choice is agentgateway/Istio.

---

## Source list (primary)

- llm-d v0.7.0 release notes (component + infra version tables):
  <https://github.com/llm-d/llm-d/releases/tag/v0.7.0>
- modelservice repo / chart: <https://github.com/llm-d-incubation/llm-d-modelservice>,
  Helm repo <https://llm-d-incubation.github.io/llm-d-modelservice/>,
  `Chart.yaml` + `values.yaml` @ tag `llm-d-modelservice-v0.4.12` (via `gh api`).
- modelservice README (chart does not render GAIE/EPP/HTTPRoute):
  <https://github.com/llm-d-incubation/llm-d-modelservice/blob/main/README.md>
- llm-d-infra repo / chart: <https://github.com/llm-d-incubation/llm-d-infra>,
  README <https://github.com/llm-d-incubation/llm-d-infra/blob/main/charts/llm-d-infra/README.md>,
  `Chart.yaml` @ `main` (via `gh api`).
- GAIE InferencePool v1 types: `api/v1/inferencepool_types.go`,
  `api/v1/zz_generated.register.go` @ tag `v1.5.0` (via `gh api`).
  Docs: <https://gateway-api-inference-extension.sigs.k8s.io/api-types/inferencepool/>
- GAIE InferenceObjective v1alpha2 types: `apix/v1alpha2/inferenceobjective_types.go`
  @ tag `v1.5.0` (via `gh api`).
  Docs: <https://gateway-api-inference-extension.sigs.k8s.io/api-types/inferenceobjective/>
- GAIE `inferencepool` Helm chart (renders InferencePool + EPP + InferenceObjective +
  HTTPRoute): `config/charts/inferencepool/` @ tag `v1.5.0` (via `gh api`).
- llm-d gateway guidance: <https://llm-d.ai/docs/usage/customizing-your-gateway>,
  GAIE gateways: <https://gateway-api-inference-extension.sigs.k8s.io/implementations/gateways/>
