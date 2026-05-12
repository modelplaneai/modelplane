# Modelplane API Update — One-Pager

**Status:** Draft
**Date:** May 2026
**Author:** Nic Cope

## Summary

A simplified resource model for Modelplane that drops the ClusterModel/Model
catalog split, makes ModelDeployment self-contained, and aligns the resource
hierarchy with Kubernetes core: ModelDeployment → ModelReplica → ModelService
→ ModelEndpoint mirrors Deployment → Pod → Service → Endpoint.

Scaling happens at the replica boundary. Each `ModelReplica` is one complete
serving instance — possibly multi-node, possibly disaggregated prefill/decode —
composed as a single KServe `LLMInferenceService`. ModelDeployment exposes a
scale subresource on `spec.replicas`; autoscaling is opt-in via a separate
KEDA `ScaledObject`, the same pattern as Kubernetes Deployment + HPA.

Cluster matching uses standard Kubernetes labels. Pool matching uses
open-ended capabilities with CEL expressions. `InferenceClass` captures
hardware topology as a reusable named bundle, following the StorageClass
pattern. Composition fields (topology, engine config) stay structured so
the placement function can compose KServe LLMInferenceService correctly.

## Resource model

| Resource | Scope | Created by | Purpose |
|---|---|---|---|
| `InferenceGateway` | Cluster | Platform team | Control plane routing infrastructure |
| `InferenceClass` | Cluster | Platform team (or Modelplane defaults) | Named hardware topology bundle |
| `InferenceCluster` | Cluster | Platform team | A cluster in the inference fleet |
| `ModelDeployment` | Namespace | ML team | Self-contained model deployment spec |
| `ModelReplica` | Namespace | Modelplane (composed) | One complete serving instance of a deployment |
| `ModelService` | Namespace | ML team | Routing surface across endpoints |
| `ModelEndpoint` | Namespace | Modelplane (composed) or ML team | Reachable inference endpoint |

`ClusterModel` and `Model` are removed. Model identity, engine configuration,
and resource requirements all live on `ModelDeployment`.

## InferenceClass

A tested recipe for a GPU node pool. Each class bundles two things:
**capabilities** (what this hardware can do, used by the scheduler) and
optionally **provisioning** (how to create it on a specific cloud, used by
the composition function). Modelplane ships defaults for common cloud × SKU
combinations (`gke-h200-8x-a3-ib`, `gke-l4-1x-g2`, `eks-h100-8x-p5`, etc.)
as well as cloud-agnostic capabilities-only classes for BYO clusters
(`h200-8x-ib`, `h100-8x`, `l4-1x`, etc.). Platform teams can author custom
classes for bespoke hardware.

GKE H200 — provisioning recipe + capabilities:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceClass
metadata:
  # Class name is referenced from InferenceCluster.spec.nodePools[].class.
  name: gke-h200-8x-a3-ib
spec:
  description: "GKE a3-ultragpu-8g, 8x H200, GPUDirect-TCPX"

  # Optional — omit for BYO / capabilities-only classes. When present,
  # the composition function reads this to provision the pool on the
  # target cloud. provider is the discriminator; the sibling block (gke,
  # eks, aks) carries cloud-specific config.
  provisioning:
    provider: GKE
    gke:
      machineType: a3-ultragpu-8g
      accelerator:
        type: nvidia-h200-141gb
        count: 8
      diskSizeGb: 200
      networking:
        gpuDirectTCPX: true

  # Open-ended key-value map. ModelDeployment.spec.nodeSelector.cel
  # evaluates against these. Describes exactly what the provisioning
  # above produces. Plain YAML scalars and lists for the common case;
  # {type: ..., value: ...} for versions or anything YAML can't express
  # natively.
  capabilities:
    gpu.vendor: nvidia
    gpu.product: H200
    gpu.architecture: Hopper
    gpu.vramGiB: 141
    gpu.count: 8
    gpu.features: [fp8, bf16, transformer-engine, mig]
    interconnect.intraNode: nvswitch
    interconnect.intraNodeBandwidthGBs: 900
    network.interNode: gpudirect-tcpx
    network.interNodeBandwidthGbps: 200
    cuda.toolkit: {type: version, value: "12.4.0"}
```

BYO H200 — capabilities only, no provisioning:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceClass
metadata:
  name: h200-8x-ib
spec:
  description: "8x H200, NVSwitch, InfiniBand 400Gbps (BYO)"
  # No provisioning block — capabilities only. For BYO clusters where
  # the pool already exists. The scheduler uses capabilities for
  # matching; the composition function doesn't provision anything.
  capabilities:
    gpu.vendor: nvidia
    gpu.product: H200
    gpu.architecture: Hopper
    gpu.vramGiB: 141
    gpu.count: 8
    gpu.features: [fp8, bf16, transformer-engine, mig]
    interconnect.intraNode: nvswitch
    interconnect.intraNodeBandwidthGBs: 900
    network.interNode: infiniband
    network.interNodeBandwidthGbps: 400
```

GKE L4 — simple provisioning recipe:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceClass
metadata:
  name: gke-l4-1x-g2
spec:
  description: "GKE g2-standard-4, 1x L4"
  provisioning:
    provider: GKE
    gke:
      machineType: g2-standard-4
      accelerator:
        type: nvidia-l4
        count: 1
  capabilities:
    gpu.vendor: nvidia
    gpu.product: L4
    gpu.architecture: Ada
    gpu.vramGiB: 24
    gpu.count: 1
    gpu.features: [fp8, bf16, int8]
    interconnect.intraNode: pcie
```

## InferenceCluster

A cluster in the fleet. Cluster-level metadata is captured in standard
Kubernetes labels. Each pool references an `InferenceClass` for its
hardware capabilities and (for provisioned clusters) its cloud-specific
provisioning recipe.

GKE-provisioned — the composition function reads the class's provisioning
config to create each pool:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceCluster
metadata:
  name: prod-gke-us-east
  # Labels are the cluster-level matching surface. ModelDeployment's
  # spec.clusterSelector.matchLabels matches against these.
  labels:
    modelplane.ai/tier: production
    cloud.provider: gcp
    cloud.region: us-east1
spec:
  # Cluster-level provisioning — where to create it. The class carries
  # the pool-level provisioning (machineType, accelerator, networking).
  cluster:
    source: GKE
    gke:
      project: acme-ml-platform
      region: us-east1
      kubernetesVersion: "1.35"

  nodePools:
  # Each pool references an InferenceClass. For GKE clusters, the class
  # provides both capabilities (scheduling) and provisioning config
  # (machineType, GPU). maxNodes and nodeCount are per-cluster sizing.
  - name: frontier
    class: gke-h200-8x-a3-ib
    maxNodes: 4
    nodeCount: 0

  - name: dev
    class: gke-l4-1x-g2
    maxNodes: 4
    nodeCount: 1
```

BYO — the class provides capabilities only, provisioning is ignored:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceCluster
metadata:
  name: prod-coreweave-us-east
  labels:
    modelplane.ai/tier: production
    cloud.provider: coreweave
    cloud.region: us-east-1
spec:
  cluster:
    source: Existing
    existing:
      secretRef:
        name: coreweave-kubeconfig
        key: kubeconfig

  nodePools:
  - name: frontier
    class: h200-8x-ib
    maxNodes: 4
```

## ModelDeployment — Mixtral 8x7B

Single-node, two GPUs per replica. The deployment itself just declares
`spec.replicas` — autoscaling is opt-in via a separate KEDA `ScaledObject`
shown below.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  # Model identity (passed to the engine and used by clients in OpenAI API
  # requests) is <namespace>/<name> — here, ml-team/mixtral-8x7b.
  name: mixtral-8x7b
  namespace: ml-team
spec:
  # Cluster-level filter. matchLabels against InferenceCluster.metadata.labels.
  # No CEL here — cluster-level matching is organizational metadata, string
  # equality is sufficient.
  clusterSelector:
    matchLabels:
      modelplane.ai/tier: production

  # Number of complete serving instances. Each replica is one pod with
  # 2 GPUs for this deployment. KEDA writes this field via the scale
  # subresource when a ScaledObject is present.
  replicas: 2

  # Node-level capability filter. CEL predicate over the pool's
  # InferenceClass capabilities — scheduler only considers pools where
  # this is true. DRA handles actual device binding at pod admission.
  nodeSelector:
    cel: |
      capabilities["gpu.vramGiB"] >= 80

  # Workers: how many workers per ModelReplica, and what topology each
  # has. count defaults to 1 (omitted here). Tensor: single-node TP.
  workers:
    topology:
      strategy: Tensor
      tensor: 2

  engine:
    name: vLLM
    image: vllm/vllm-openai:v0.8.5
    # Engine args pass through opaquely to the engine container. The
    # --model arg tells the engine where to fetch weights. Modelplane
    # doesn't interpret it — model fetching is the engine's concern.
    args:
    - "--model=mistralai/Mixtral-8x7B-Instruct-v0.1"
    - "--tensor-parallel-size=2"
    - "--max-model-len=32768"
    - "--gpu-memory-utilization=0.9"
```

Autoscaling is a separate concern. The deployer (or a Composition) creates a
KEDA `ScaledObject` that targets the ModelDeployment via its scale
subresource. Modelplane never owns autoscaling configuration directly —
ModelDeployment + ScaledObject mirrors Deployment + HPA.

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: mixtral-8x7b
  namespace: ml-team
spec:
  scaleTargetRef:
    apiVersion: modelplane.ai/v1alpha1
    kind: ModelDeployment
    name: mixtral-8x7b
  minReplicaCount: 2
  maxReplicaCount: 10
  cooldownPeriod: 300
  triggers:
  # Watch aggregate concurrency at the InferenceGateway. KEDA writes
  # ModelDeployment.spec.replicas based on the threshold.
  - type: prometheus
    metadata:
      serverAddress: http://prometheus.modelplane-system:9090
      query: |
        sum(envoy_cluster_upstream_rq_active{cluster="ml-team-mixtral-8x7b"})
      threshold: "32"
```

## ModelDeployment — Kimi K2

Multi-node frontier MoE. Each replica is 16 GPUs across 2 nodes, TP=8 PP=2,
FP8, tool calling. No `ScaledObject` means a fixed replica count.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: kimi-k2
  namespace: ml-team
spec:
  clusterSelector:
    matchLabels:
      modelplane.ai/tier: production

  replicas: 1

  # Node-level capability filter.
  nodeSelector:
    cel: |
      capabilities["gpu.vramGiB"] >= 141 &&
      "fp8" in capabilities["gpu.features"] &&
      capabilities["network.interNode"] == "infiniband" &&
      capabilities["network.interNodeBandwidthGbps"] >= 400

  # Workers: one worker per replica (count defaults to 1). TensorPipeline
  # — TP within nodes, PP across nodes. The scheduler derives the physical
  # shape: pipeline=2 → 2 nodes, tensor=8 → 8 GPUs per node, 16 total.
  workers:
    topology:
      strategy: TensorPipeline
      tensor: 8
      pipeline: 2

  engine:
    name: vLLM
    image: vllm/vllm-openai:v0.8.5
    # For gated models, inject the HF token via env. The engine uses it
    # to authenticate when downloading weights.
    env:
    - name: HF_TOKEN
      valueFrom:
        secretKeyRef:
          name: hf-token
          key: token
    args:
    - "--model=moonshotai/Kimi-K2-Instruct"
    - "--trust-remote-code"
    - "--max-model-len=65536"
    - "--gpu-memory-utilization=0.85"
    - "--enable-auto-tool-choice"
    - "--tool-call-parser=kimi_k2"
    - "--distributed-executor-backend=ray"
```

## ModelDeployment — Qwen3-Coder-480B

Multi-node MoE coding model. 16 GPUs across 2 nodes, TP=8 PP=2, FP8, code
agent tool calling. Similar multi-node shape to Kimi K2 — different model,
different engine args.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: qwen3-coder
  namespace: ml-team
spec:
  clusterSelector:
    matchLabels:
      modelplane.ai/tier: production

  replicas: 1

  nodeSelector:
    cel: |
      capabilities["gpu.architecture"] == "Hopper" &&
      "fp8" in capabilities["gpu.features"] &&
      capabilities["network.interNode"] == "infiniband"

  workers:
    topology:
      strategy: TensorPipeline
      tensor: 8
      pipeline: 2

  engine:
    name: vLLM
    image: vllm/vllm-openai:v0.9.0
    # FP8 checkpoint — a different repo from the BF16 checkpoint. If you
    # wanted BF16, you'd create a separate ModelDeployment with
    # --model=Qwen/Qwen3-Coder-480B-A35B-Instruct instead.
    args:
    - "--model=Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8"
    - "--max-model-len=65536"
    - "--gpu-memory-utilization=0.9"
    - "--enable-auto-tool-choice"
    - "--tool-call-parser=hermes"
```

## Disaggregated prefill/decode

The top-level `nodeSelector`, `workers`, and `engine` fields on a
`ModelDeployment` are always the decode (or unified) settings. Adding a
`prefill` block makes the deployment disaggregated. The `prefill` block is
self-contained — it repeats all settings it needs rather than inheriting from
the root, because explicit repetition is easier to reason about than implicit
merge.

Converting a unified deployment to disagg is purely additive — add a
`prefill` block (and `workers.count` on the decode side), and the existing
top-level config becomes the decode config without any restructuring.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: llama-405b-disagg
  namespace: ml-team
spec:
  clusterSelector:
    matchLabels:
      modelplane.ai/tier: production

  replicas: 1

  # Top-level = decode settings. Same fields as a unified deployment.
  # The presence of the prefill block below is what makes this disagg.
  # workers.count is the "3" in "5P3D" — 3 decode workers.
  nodeSelector:
    cel: |
      capabilities["gpu.vramGiB"] >= 141 &&
      capabilities["network.interNode"] == "infiniband"
  workers:
    count: 3
    topology:
      strategy: TensorPipeline
      tensor: 8
      pipeline: 2
  engine:
    name: vLLM
    image: vllm/vllm-openai:v0.9.1
    args:
    - "--model=meta-llama/Llama-3.1-405B-Instruct"
    - "--max-model-len=131072"
    - "--gpu-memory-utilization=0.90"
    - '--kv-transfer-config={"kv_role":"kv_consumer"}'

  # Prefill: compute-bound, more workers, smaller GPUs, different KV
  # transfer role. Self-contained — repeats everything it needs.
  # workers.count is the "5" in "5P3D" — 5 prefill workers.
  prefill:
    nodeSelector:
      cel: |
        capabilities["gpu.vramGiB"] >= 80 &&
        capabilities["network.interNode"] == "infiniband"
    workers:
      count: 5
      topology:
        strategy: Tensor
        tensor: 1
    engine:
      name: vLLM
      image: vllm/vllm-openai:v0.9.1
      args:
      - "--model=meta-llama/Llama-3.1-405B-Instruct"
      - "--max-model-len=131072"
      - '--kv-transfer-config={"kv_role":"kv_producer"}'
```

Each `ModelReplica` for this deployment composes one KServe
`LLMInferenceService` with both decode and prefill workloads.
`workers.count` on each role maps to `LLMInferenceService.spec.replicas`
(decode) and `LLMInferenceService.spec.prefill.replicas` (prefill).
`workers.topology` describes the shape of each worker — the placement
function maps it to KServe's parallelism spec and LeaderWorkerSet group
size. Decode and prefill must land on the same `InferenceCluster` (KV cache
transfer requires co-location), but can target different pools within that
cluster. The scheduler verifies the cluster has capacity for both roles.

Scaling `spec.replicas` from 1 to 2 creates a second complete 5P3D instance
— another full decode + prefill worker set, scheduled independently. The
P:D ratio (`workers.count` per role) is a topology parameter — fixed per
deployment, not a scaling knob.

## ModelEndpoint

A reachable inference endpoint. Composed by `ModelDeployment` (one per
`ModelReplica`) or created manually for break-glass routing to external
services like Together AI or BaseTen. Both shapes use the same schema —
`ModelService` doesn't care where they came from.

Composed (one per replica, created by Modelplane):

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelEndpoint
metadata:
  # Generated name — one ModelEndpoint per ModelReplica.
  name: kimi-k2-coreweave-us-east-0
  namespace: ml-team
  # Composition labels the endpoint with its parent deployment.
  # ModelService selects on this label.
  labels:
    modelplane.ai/deployment: kimi-k2
spec:
  url: http://10.0.1.50/ml-team/kimi-k2/
  api: OpenAI
```

Manual (created by the ML team for external routing):

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelEndpoint
metadata:
  name: together-kimi-k2
  namespace: ml-team
  # Manual endpoints can use the same deployment label to participate in
  # the same ModelService as composed endpoints, or use any label the
  # ModelService selects on.
  labels:
    modelplane.ai/deployment: kimi-k2
spec:
  url: https://api.together.xyz/v1
  api: OpenAI
  # Auth is optional. Composed endpoints don't need it (control plane
  # gateway routes plain HTTP to the remote cluster); manual endpoints
  # for SaaS providers usually do.
  auth:
    secretRef:
      name: together-api-key
```

The `api` field declares what protocol the endpoint speaks. `OpenAI` means
the standard OpenAI-compatible surface (`/v1/chat/completions`,
`/v1/embeddings`, etc.). Future values reserve room for non-OpenAI APIs.

## ModelService

A weighted routing surface across `ModelEndpoint`s. Always uses
`spec.endpoints` — a single-entry list for the simple case, multiple entries
with weights for canary, A/B, or hybrid SaaS routing.

Simple — one entry, all of a deployment's endpoints:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelService
metadata:
  name: kimi-k2
  namespace: ml-team
spec:
  # Single entry, no weight needed. Routes equally across all matching
  # ModelEndpoints — i.e., all replicas of the kimi-k2 deployment.
  endpoints:
  - selector:
      matchLabels:
        modelplane.ai/deployment: kimi-k2
```

Weighted — multiple deployments plus an external endpoint:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelService
metadata:
  name: assistant
  namespace: ml-team
spec:
  endpoints:
  # 70% of traffic to all replicas of kimi-k2 (round-robin across them).
  - weight: 70
    selector:
      matchLabels:
        modelplane.ai/deployment: kimi-k2

  # 25% of traffic to all replicas of qwen3-coder.
  - weight: 25
    selector:
      matchLabels:
        modelplane.ai/deployment: qwen3-coder

  # 5% to the manual external endpoint (e.g., Together AI fallback).
  - weight: 5
    selector:
      matchLabels:
        modelplane.ai/endpoint: together-kimi-k2
```

Each `endpoints[]` entry selects `ModelEndpoint` resources by label. Composed
endpoints carry the `modelplane.ai/deployment` label set by the deployment
composition; manual endpoints carry whatever labels the user puts on them.
A route with no `weight` defaults to weight 1 (equal weighting across routes).

## Composed resources

The Kubernetes parallel:

| Modelplane | Kubernetes |
|---|---|
| `ModelDeployment` | `Deployment` |
| `ModelReplica` | `Pod` |
| `ModelService` | `Service` |
| `ModelEndpoint` | `Endpoint` |

`ModelReplica` is composed by `ModelDeployment` — one per `spec.replicas`.
Each replica is one complete serving instance: a single KServe
`LLMInferenceService` on a chosen `InferenceCluster`, containing all the
pods needed for that instance (one for single-node, multiple via
LeaderWorkerSet for multi-node, both decode and prefill workloads for
disaggregated serving). The fleet scheduler picks
`(InferenceCluster, pool)` per replica independently — replicas of the same
deployment can land on different clusters or on the same cluster depending
on capacity and policy.

`ModelEndpoint` is composed by `ModelDeployment` — one per `ModelReplica`,
labeled with `modelplane.ai/deployment: <md-name>`. Manual `ModelEndpoint`s
can also be created to route to external services, using the same schema.

## Key design decisions

- **`ClusterModel` and `Model` removed.** `ModelDeployment` is self-contained.
  Organizations that want a curated catalog build a Crossplane Composition
  over `ModelDeployment`.
- **Model identity is `<namespace>/<name>`.** The ModelDeployment's namespace
  and name form the served model identifier used by clients in OpenAI API
  requests. The composition function injects `--served-model-name` with
  this value.
- **No `source` or `huggingFace` on ModelDeployment.** Model fetching is
  the engine's concern, not Modelplane's. The engine's `--model` arg tells
  it where to fetch weights (HuggingFace repo, local path, etc.). For
  gated models, `engine.env` injects credentials (e.g., `HF_TOKEN` via
  `secretKeyRef`). For fleet-level weight staging (pre-caching weights to
  nodes before deployment), a future `ModelCache` resource provides the
  right abstraction — scoped to the fleet, not to individual deployments.
- **Replicas are the only scaling axis.** Each `ModelReplica` is a
  complete, fixed-topology serving instance. Scaling `spec.replicas` adds
  or removes whole instances; Modelplane's scheduler decides where each
  lands. No in-cluster pod autoscaling — KServe's
  `LLMInferenceService.spec.replicas` is always set to 1 by the placement
  function. This mirrors BaseTen's model: replicas are the unit of
  scaling, not pods within a replica. KServe scales LeaderWorkerSet groups
  the same way (whole groups added, never resized), so the granularity is
  identical to in-cluster scaling — Modelplane just adds fleet-awareness.
- **Autoscaling is opt-in via KEDA `ScaledObject`.** The ModelDeployment
  XRD declares a Kubernetes scale subresource (`specReplicasPath:
  .spec.replicas`, `statusReplicasPath: .status.replicas`). The deployer
  (or a Composition) creates a `ScaledObject` targeting the
  ModelDeployment to enable autoscaling; KEDA writes `spec.replicas`
  based on its triggers. No autoscaling configuration on ModelDeployment
  itself — the pattern mirrors Kubernetes Deployment + HPA. Bare
  ModelDeployments have fixed replicas.
- **Two-level matching, two mechanisms.** Cluster-level matching uses
  `spec.clusterSelector.matchLabels` against standard Kubernetes labels on
  `InferenceCluster` (organizational metadata: tier, region, provider).
  Node-level matching uses `spec.nodeSelector.cel` against the typed
  `capabilities` bundled by `InferenceClass` (hardware and networking
  facts).
- **`InferenceClass` is a tested recipe.** Each class bundles capabilities
  (for scheduling) and optionally cloud-specific provisioning config (for
  cluster composition). GPU topology and inter-node networking both live
  on the class. Different clouds or networking imply different classes
  (`gke-h200-8x-a3-ib` vs `h200-8x-ib`). For provisioned clusters, the
  composition function reads `class.provisioning` to create the pool. For
  BYO clusters, provisioning is omitted and only capabilities are used.
  Modelplane ships a default catalog of classes for common cloud × SKU
  combinations.
- **Open-ended capabilities with CEL matching.** Pool capabilities are
  key-value maps; pool selectors are CEL expressions. New capabilities don't
  require schema changes.
- **Optional type decoration.** Plain YAML values for the common case
  (string, integer, boolean, list); `{type: ..., value: ...}` wrapper for
  versions, quantities, and any type YAML can't express natively.
- **No serving profiles.** ModelDeployment carries one configuration, not a
  priority-ordered array of fallbacks. Different hardware targets or
  quantization variants are separate ModelDeployments behind one
  ModelService. This is simpler, avoids the pinning/migration problem
  (when do you move from fallback back to preferred?), and honest about
  the fact that different quantization variants reference different model
  weight checkpoints (different HuggingFace repos) — they're genuinely
  different deployments. If preferential scheduling is needed later, it
  would be a coordination mechanism between MDs, not inline profiles.
- **Workers: count + topology.** `workers` groups two concerns: how many
  workers per role (`count`, default 1) and the compute shape of each
  worker (`topology`). `topology.strategy` is a required discriminator:
  `Tensor` (single-node TP), `TensorPipeline` (TP within nodes, PP across
  nodes), or `DataExpert` (DP+EP across nodes). Each strategy determines
  which sibling fields are required and how the scheduler derives the
  physical shape per worker. `nodeSelector` and `engine` stay alongside
  `workers` as separate concerns.

  | Strategy | Required fields | Nodes per worker | GPUs per node | Total GPUs per worker |
  |---|---|---|---|---|
  | `Tensor` | `tensor` | 1 | `tensor` | `tensor` |
  | `TensorPipeline` | `tensor`, `pipeline` | `pipeline` | `tensor` | `tensor * pipeline` |
  | `DataExpert` | `tensor`, `data`, `dataLocal` | `data / dataLocal` | `dataLocal * tensor` | `data * tensor` |

  Multiply by `workers.count` for the total per-role footprint within one
  ModelReplica. The scheduler checks: does the matched pool's
  `InferenceClass` have `gpu.count` >= GPUs-per-node, and does the pool
  have enough available nodes for all workers across all roles?
- **DRA required on all InferenceClusters.** No device-plugin fallback.
  Modelplane always emits DRA `ResourceClaim`s for device binding. This
  simplifies pool declarations (no `nodeSelector` labels needed) and the
  composition function (one code path). Requires K8s 1.31+ with a DRA
  driver on every cluster.
- **Disagg is additive.** Top-level `nodeSelector`, `workers`, and
  `engine` are always the decode (or unified) settings. Adding a `prefill`
  block makes the deployment disaggregated — no restructuring needed. The
  `prefill` block is self-contained (repeats all settings it needs, no
  inheritance). The P:D ratio is expressed via `workers.count` on each
  role — it's a topology parameter (fixed per deployment), not a scaling
  knob. Decode and prefill must land on the same `InferenceCluster` (KV
  cache transfer needs co-location) but can target different pools.
- **Anti-affinity for replica spread.** When multiple replicas land on the
  same cluster, the scheduler spreads them across different node groups
  where capacity allows, to limit blast radius from node failures.
- **Fleet scheduling, opinionated about Kubernetes features.** Modelplane
  picks `(InferenceCluster, pool)` per replica based on declared
  capabilities and capacity. DRA is the device binding mechanism on every
  cluster — the composition function emits `ResourceClaim`s derived from
  the matched pool's `InferenceClass` capabilities.
- **Kubernetes-native resource hierarchy.** `ModelDeployment` →
  `ModelReplica` → `ModelService` → `ModelEndpoint` mirrors `Deployment` →
  `Pod` → `Service` → `Endpoint`.
- **One `ModelEndpoint` schema, two creation paths.** `ModelDeployment`
  composes one `ModelEndpoint` per `ModelReplica`. The ML team can also
  create `ModelEndpoint`s manually to point at external services (Together,
  BaseTen, Bedrock). Both look the same to `ModelService` — `spec.url` and
  `spec.api` describe the endpoint, `auth` is optional for endpoints that
  need credentials.
- **`ModelService` always uses `spec.endpoints`.** No separate path for the
  simple case versus weighted routing. Single-entry list for one deployment,
  multi-entry with weights for canary, A/B, or SaaS overflow. Each entry
  selects `ModelEndpoint`s by label — Kubernetes-native, no special
  endpointRef syntax.
