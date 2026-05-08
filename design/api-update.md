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
pattern. Composition fields (parallelism, engine config) stay structured so
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

Reusable hardware topology bundles. An `InferenceClass` captures the complete
hardware context for a node pool — GPU topology and inter-node networking.
Modelplane ships defaults (`h200-nvl-8x-ib`, `h100-nvl-8x-ib`, `h100-nvl-8x`,
`l4-1x`, `b200-nvl-8x`, `mi300x-8x`, etc.). Platform teams can author custom
classes for bespoke hardware.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceClass
metadata:
  # Class name is referenced from InferenceCluster.spec.nodePools[].class.
  name: h200-nvl-8x-ib
spec:
  description: "8x NVIDIA H200 SXM, NVLink Switch, InfiniBand 400Gbps"

  # Open-ended key-value map. ModelDeployment.serving[].poolSelector.cel
  # evaluates against these. Plain YAML scalars and lists for the common
  # case; {type: ..., value: ...} for versions or anything YAML can't
  # express natively.
  capabilities:
    gpu.vendor: nvidia
    gpu.product: H200
    gpu.architecture: Hopper
    gpu.vramGiB: 141
    gpu.count: 8
    gpu.features: [fp8, bf16, transformer-engine, mig]
    interconnect.intraNode: nvswitch
    interconnect.intraNodeBandwidthGBs: 900
    # Inter-node networking belongs to the class — it's a property of the
    # pool's hardware, not of the cluster as a whole. Different networking
    # implies a different class (h200-nvl-8x-ib vs h200-nvl-8x).
    network.interNode: infiniband
    network.interNodeBandwidthGbps: 400
    # Decorated value — version semantics, not bare string comparison.
    driver.version: {type: version, value: "535.129.03"}
```

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceClass
metadata:
  name: l4-1x
spec:
  description: "1x NVIDIA L4, 24 GiB GDDR6, PCIe"
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
Kubernetes labels. Hardware capabilities — including inter-node networking —
come from each pool's referenced `InferenceClass`.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceCluster
metadata:
  name: prod-coreweave-us-east
  # Labels are the cluster-level matching surface. ModelDeployment's
  # spec.clusterSelector.matchLabels matches against these — organizational
  # metadata like tier, region, provider. Hardware facts live on the
  # pool's InferenceClass, not here.
  labels:
    modelplane.ai/tier: production
    cloud.provider: coreweave
    cloud.region: us-east-1
spec:
  # BYO kubeconfig. Modelplane installs the inference stack but doesn't
  # provision the cluster.
  cluster:
    source: Existing
    existing:
      secretRef:
        name: coreweave-kubeconfig
        key: kubeconfig

  nodePools:
  # Each pool references an InferenceClass for its hardware capabilities.
  # maxNodes is the pool's capacity ceiling — used by the scheduler to
  # check whether a replica fits.
  - name: frontier
    class: h200-nvl-8x-ib
    maxNodes: 4
    nodeSelector:
      modelplane.ai/pool: frontier

  - name: general
    class: h100-nvl-8x
    maxNodes: 8
    nodeSelector:
      modelplane.ai/pool: general
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
  # Where to fetch model weights from. Source-specific config follows.
  source: HuggingFace
  huggingFace:
    repo: mistralai/Mixtral-8x7B-Instruct-v0.1

  # Cluster-level filter. matchLabels against InferenceCluster.metadata.labels.
  # No CEL here — cluster-level matching is organizational metadata, string
  # equality is sufficient.
  clusterSelector:
    matchLabels:
      modelplane.ai/tier: production

  # Number of complete serving instances. Each replica is a separate
  # ModelReplica targeting one InferenceCluster (possibly multi-node, possibly
  # disagg — for this profile it's just one pod with 2 GPUs). KEDA writes
  # this field via the scale subresource when a ScaledObject is present.
  replicas: 2

  serving:
  - name: default
    # Pool-level filter. count/perNode declare the physical shape; cel is
    # the capability predicate. Together they ask the scheduler for a pool
    # with at least 2 GPUs of >= 80GiB each, all on one node.
    poolSelector:
      count: 2
      perNode: 2
      cel: |
        capabilities["gpu.vramGiB"] >= 80

    engine:
      name: vLLM
      image: vllm/vllm-openai:v0.8.5
      # Engine args pass through opaquely to the engine container.
      args:
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
  source: HuggingFace
  huggingFace:
    repo: moonshotai/Kimi-K2-Instruct
    secretRef:
      name: hf-token

  clusterSelector:
    matchLabels:
      modelplane.ai/tier: production

  replicas: 1

  serving:
  - name: default
    # Multi-node shape: 16 total GPUs, 8 per node = 2 nodes per replica.
    # The CEL predicate filters pools by capability — H200-class memory,
    # FP8 support, and InfiniBand at 400Gbps for inter-node parallelism.
    poolSelector:
      count: 16
      perNode: 8
      cel: |
        capabilities["gpu.vramGiB"] >= 141 &&
        "fp8" in capabilities["gpu.features"] &&
        capabilities["network.interNode"] == "infiniband" &&
        capabilities["network.interNodeBandwidthGbps"] >= 400

    # Structured parallelism — placement function maps these to KServe's
    # LLMInferenceService.spec.parallelism. pipeline: 2 drives the
    # LeaderWorkerSet group size; tensor: 8 informs the engine.
    parallelism:
      tensor: 8
      pipeline: 2

    engine:
      name: vLLM
      image: vllm/vllm-openai:v0.8.5
      args:
      - "--trust-remote-code"
      - "--max-model-len=65536"
      - "--gpu-memory-utilization=0.85"
      - "--enable-auto-tool-choice"
      - "--tool-call-parser=kimi_k2"
      - "--distributed-executor-backend=ray"
```

## ModelDeployment — Qwen3-Coder-480B

Multi-node MoE coding model. Demonstrates a profile fallback: prefer H200s
with PP=2, fall back to H100s with PP=4 if no H200 pool matches. The
scheduler tries profiles in order; the first one with a matching cluster and
pool wins.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: qwen3-coder
  namespace: ml-team
spec:
  source: HuggingFace
  huggingFace:
    repo: Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8

  clusterSelector:
    matchLabels:
      modelplane.ai/tier: production

  replicas: 1

  serving:
  # First choice: 2 nodes of 8 H200s with pipeline parallelism = 2.
  - name: h200-preferred
    poolSelector:
      count: 16
      perNode: 8
      cel: |
        capabilities["gpu.product"] == "H200" &&
        "fp8" in capabilities["gpu.features"] &&
        capabilities["network.interNode"] == "infiniband"

    parallelism:
      tensor: 8
      pipeline: 2

    engine:
      name: vLLM
      image: vllm/vllm-openai:v0.9.0
      args:
      - "--max-model-len=65536"
      - "--gpu-memory-utilization=0.9"
      - "--enable-auto-tool-choice"
      - "--tool-call-parser=hermes"

  # Fallback: 4 nodes of 8 H100s with pipeline parallelism = 4. More nodes,
  # smaller GPUs, longer pipeline. Lower max context to fit the working set.
  - name: h100-fallback
    poolSelector:
      count: 32
      perNode: 8
      cel: |
        capabilities["gpu.product"] == "H100" &&
        "fp8" in capabilities["gpu.features"] &&
        capabilities["network.interNode"] == "infiniband"

    parallelism:
      tensor: 8
      pipeline: 4

    engine:
      name: vLLM
      image: vllm/vllm-openai:v0.9.0
      args:
      - "--max-model-len=32768"
      - "--gpu-memory-utilization=0.9"
      - "--enable-auto-tool-choice"
      - "--tool-call-parser=hermes"
```

## Disaggregated prefill/decode

A serving profile is a discriminated union: either unified (root-level
`poolSelector`, `parallelism`, `engine`) or disaggregated (explicit `decode`
and `prefill` blocks, each self-contained). Disagg blocks repeat all settings
they need rather than inheriting from the root — repetition over indirection.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: llama-405b-disagg
  namespace: ml-team
spec:
  source: HuggingFace
  huggingFace:
    repo: meta-llama/Llama-3.1-405B-Instruct

  clusterSelector:
    matchLabels:
      modelplane.ai/tier: production

  replicas: 1

  serving:
  - name: disagg
    # Disagg profiles use explicit decode/prefill blocks instead of root
    # poolSelector/parallelism/engine. No inheritance — each block is
    # self-contained.

    # Decode: memory-bandwidth-bound. Big GPUs, fewer pods, more parallelism.
    decode:
      pods: 3
      poolSelector:
        count: 24
        perNode: 8
        cel: |
          capabilities["gpu.vramGiB"] >= 141 &&
          capabilities["network.interNode"] == "infiniband"
      parallelism:
        tensor: 8
        pipeline: 2
      engine:
        name: vLLM
        image: vllm/vllm-openai:v0.9.1
        args:
        - "--max-model-len=131072"
        - "--gpu-memory-utilization=0.90"
        - '--kv-transfer-config={"kv_role":"kv_consumer"}'

    # Prefill: compute-bound. More, smaller pods. Cheaper GPUs are fine.
    # Different KV transfer role.
    prefill:
      pods: 5
      poolSelector:
        count: 5
        perNode: 1
        cel: |
          capabilities["gpu.vramGiB"] >= 80 &&
          capabilities["network.interNode"] == "infiniband"
      parallelism:
        tensor: 1
      engine:
        name: vLLM
        image: vllm/vllm-openai:v0.9.1
        args:
        - "--max-model-len=131072"
        - '--kv-transfer-config={"kv_role":"kv_producer"}'
```

Each `ModelReplica` for this deployment composes one KServe
`LLMInferenceService` containing all decode and prefill pods. Decode and
prefill must land on the same `InferenceCluster` (KV cache transfer requires
co-location), but can target different pools within that cluster. The
scheduler verifies the cluster has capacity for both roles.

Scaling `replicas` from 1 to 2 creates a second complete instance — another
3 decode + 5 prefill pod set, scheduled independently.

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
  and name form the served model identifier passed to the engine and used by
  clients in OpenAI API requests. The HuggingFace repo (or other source) is
  purely where weights are fetched from, not the model's identity.
- **Replicas are the only scaling axis.** Each `ModelReplica` is a
  complete, fixed-topology serving instance. Scaling `spec.replicas` adds
  or removes whole instances; Modelplane's scheduler decides where each
  lands. No in-cluster pod autoscaling — KServe's
  `LLMInferenceService.spec.replicas` is always set to 1 by the placement
  function. This mirrors BaseTen's model: replicas are the unit of
  scaling, not pods within a replica. KServe scales LeaderWorkerSet groups
  the same way (whole groups added, never resized), so the granularity is
  identical to in-cluster scaling — Modelplane just adds fleet-awareness.
- **Autoscaling is opt-in via KEDA `ScaledObject`.** ModelDeployment exposes
  a scale subresource on `spec.replicas`. The deployer (or a Composition)
  creates a `ScaledObject` targeting the ModelDeployment to enable
  autoscaling; KEDA writes `spec.replicas` based on its triggers. No
  autoscaling configuration on ModelDeployment itself — the pattern mirrors
  Kubernetes Deployment + HPA. Bare ModelDeployments have fixed replicas.
- **Two-level matching, two mechanisms, two homes.** Cluster-level matching
  is deployment-level — `spec.clusterSelector.matchLabels` against standard
  Kubernetes labels on `InferenceCluster` (organizational metadata: tier,
  region, provider). Pool-level matching is per-profile —
  `serving[].poolSelector.cel` against the typed `capabilities` bundled by
  `InferenceClass` (hardware and networking facts). Cluster selection is the
  deployment intent; pool selection is the hardware adaptation strategy.
- **`InferenceClass` is the complete hardware context.** GPU topology and
  inter-node networking both live on the class. Different networking implies
  a different class (`h200-nvl-8x-ib` vs `h200-nvl-8x`). Networking belongs
  to the pool that uses it, not to the cluster.
- **Open-ended capabilities with CEL matching.** Pool capabilities are
  key-value maps; pool selectors are CEL expressions. New capabilities don't
  require schema changes.
- **Optional type decoration.** Plain YAML values for the common case
  (string, integer, boolean, list); `{type: ..., value: ...}` wrapper for
  versions, quantities, and any type YAML can't express natively.
- **Structured parallelism.** `parallelism: {tensor, pipeline, expert}` on
  the serving profile maps directly to KServe's
  `LLMInferenceService.spec.parallelism`. Engine args remain opaque and pass
  through to the engine container.
- **Serving as an array of fallbacks.** Profiles are tried in order; the
  first one with a matching cluster and pool wins. Different profiles can
  adapt to different hardware (H200 with PP=2, H100 with PP=4). The common
  case is a single entry.
- **Disagg as a discriminated union.** A serving profile is either unified
  (root `poolSelector`, `parallelism`, `engine`) or disaggregated (explicit
  `decode` and `prefill` blocks). The disagg blocks are self-contained — no
  inheritance from the root — because explicit repetition is easier to
  reason about than implicit merge. Decode and prefill must land on the
  same `InferenceCluster` (KV cache transfer needs co-location) but can
  target different pools.
- **Anti-affinity for replica spread.** When multiple replicas land on the
  same cluster, the scheduler spreads them across different node groups
  where capacity allows, to limit blast radius from node failures.
- **Fleet scheduling, opinionated about Kubernetes features.** Modelplane
  picks `(InferenceCluster, pool)` per replica based on declared
  capabilities and capacity. DRA is detected at runtime where available
  and used for device binding; device-plugin is the fallback. The deployer
  never configures this — it's an implementation detail of how Modelplane
  composes pods.
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
