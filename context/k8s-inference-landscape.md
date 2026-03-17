# Kubernetes-Native LLM Inference Orchestration Landscape

*February 2026*

## Overview

The Kubernetes inference orchestration space has converged around a shared set of problems — model-to-hardware mapping, KV-cache-aware routing, prefill/decode disaggregation, and autoscaling — but with different architectural approaches and optimization targets. This document surveys the major open source projects, inference engines, and the shared routing substrate that's emerging via the Gateway API.

Two tiers of abstraction exist across these projects:

- **Low-level (infra-facing):** AIBrix StormService, Kthena ModelServing. You spell out containers, commands, NIXL configs. Platform engineers build and maintain these.
- **High-level (user-facing):** KServe LLMInferenceService, OME InferenceService. You say "serve this model" and the system figures out the rest. ML engineers consume these.
- **Routing layer:** Gateway API Inference Extension sits orthogonally as the shared routing substrate.

---

## Inference Orchestration Projects

### KServe

**CNCF project. The most mature inference control plane.**

KServe has adopted a dual-track strategy: `InferenceService` (v1beta1) for predictive AI (sklearn, tensorflow, pytorch), and the new `LLMInferenceService` (v1alpha1) purpose-built for generative AI. For LLMs, you're meant to use LLMInferenceService.

LLMInferenceService is clean and relatively high-level. You declare a model URI, replica count, resource limits, and a `router` section. The router section is the interesting design choice — you declare that you want a gateway, route, and scheduler, and KServe creates the Gateway API resources, HTTPRoute, InferencePool, and Endpoint Picker (EPP) deployment for you automatically.

Key features:

- P/D disaggregation via a `prefill` section — when present, the controller creates a separate deployment for prompt processing
- Multi-node inference via a `worker` section for distributed models (uses LeaderWorkerSet)
- `LLMInferenceServiceConfig` acts as a shared base template — multiple LLMInferenceServices can inherit from it via `baseRefs`, so you define your vLLM image/version/defaults once
- Built on llm-d architecture for scheduling and routing (KV-cache aware, prefix-aware)
- Manages model versioning, canary rollouts, and traffic splitting

Example CR:

```yaml
apiVersion: serving.kserve.io/v1alpha1
kind: LLMInferenceService
metadata:
  name: llama-3-8b
  namespace: default
spec:
  model:
    uri: hf://meta-llama/Llama-3.1-8B-Instruct
    name: meta-llama/Llama-3.1-8B-Instruct
  replicas: 3
  template:
    containers:
    - name: main
      image: vllm/vllm-openai:latest
      resources:
        limits:
          nvidia.com/gpu: "1"
          cpu: "8"
          memory: 32Gi
  router:
    gateway: {}    # Managed gateway
    route: {}      # Managed HTTPRoute
    scheduler: {}  # Managed EPP (Endpoint Picker)
```

---

### AIBrix

**ByteDance. Composable, cloud-native LLM inference infrastructure.**

AIBrix is the most "infrastructure-engineer facing" API of the bunch. Its core CRD is `StormService` (v1alpha1), which uses a three-layer hierarchy: StormService → RoleSet → Pods. You spell out container commands, NIXL connector configs, and labels for AIBrix's Envoy Gateway plugins to discover your models.

The design gives fine-grained control over P/D ratios and rolling updates, but there's no model abstraction — the model is just a vLLM CLI argument. Routing and gateway are separate concerns managed by AIBrix's Envoy Gateway plugins, not declared in the StormService CR.

StormService supports two deployment modes:

- **Replica mode** (replicas > 1): Each RoleSet is an independent replica. Scaling adds/removes entire RoleSets.
- **Pooled mode** (replicas = 1): Roles within a RoleSet form shared pools, independently scalable.

Key features:

- P/D disaggregation with fine-grained lifecycle management
- KVCache V1 Connector with RDMA support, L1/L2 cache hierarchies
- Multi-engine support (vLLM, SGLang)
- Dynamic LoRA adapter loading and management
- Built-in autoscaler, metrics, and Prometheus integration
- Cold Start Manager for model artifact tracking across DRAM, local storage, and cloud storage

Example CR:

```yaml
apiVersion: orchestration.aibrix.ai/v1alpha1
kind: StormService
metadata:
  name: vllm-1p1d
spec:
  replicas: 1
  updateStrategy:
    type: InPlaceUpdate
  stateful: true
  selector:
    matchLabels:
      app: vllm-1p1d
  template:
    metadata:
      labels:
        app: vllm-1p1d
    spec:
      roles:
      - name: prefill
        replicas: 1
        stateful: true
        template:
          metadata:
            labels:
              model.aibrix.ai/name: deepseek-r1-distill-llama-8b
              model.aibrix.ai/port: "8000"
              model.aibrix.ai/engine: vllm
          spec:
            containers:
            - name: prefill
              image: aibrix/vllm-openai:v0.9.2-cu128-nixl-v0.4.1
              command: ["sh", "-c"]
              args:
              - |
                python3 -m vllm.entrypoints.openai.api_server \
                  --host "0.0.0.0" --port "8000" \
                  --model deepseek-ai/DeepSeek-R1-Distill-Llama-8B \
                  --served-model-name deepseek-r1-distill-llama-8b \
                  --kv-transfer-config '{"kv_connector":"NixlConnector","kv_role":"kv_both"}'
              resources:
                limits:
                  nvidia.com/gpu: "1"
      - name: decode
        replicas: 1
        stateful: true
        template:
          # Similar structure with kv_role: "kv_both"
```

---

### OME (Open Model Engine)

**LMSYS (Chatbot Arena team). The most model-driven approach.**

OME takes the highest-level API design. Models, runtimes, and accelerators are all separate first-class CRD resources. You don't specify images, container commands, or GPU counts in the InferenceService — the control plane resolves the model → runtime match via architecture and size metadata, figures out the right parallelism and GPU requirements automatically.

Key CRDs:

- `ClusterBaseModel` / `BaseModel`: Define model sources and metadata. Automatic parsing of architecture, parameters, and capabilities from model files (safetensors headers).
- `FineTunedWeight`: LoRA adapters and fine-tuned weights extending base models.
- `ClusterServingRuntime` / `ServingRuntime`: Define how models are served. Match models by architecture and parameter size range with `autoSelect` and `priority` fields.
- `InferenceService`: Connects models to runtimes. Presence of `decoder` section enables P/D disaggregation.
- `AcceleratorClass`: Define GPU hardware classes with capabilities, discovery patterns, and cost information. Enables intelligent scheduling with policies like BestFit, Cheapest, or MostCapable.
- `BenchmarkJob`: Measures model performance under different workloads.

Example CRs:

```yaml
apiVersion: ome.io/v1beta1
kind: ClusterBaseModel
metadata:
  name: llama-3-70b-instruct
spec:
  vendor: meta
  modelType: llama
  modelArchitecture: LlamaForCausalLM
  modelParameterSize: "70B"
  quantization: fp16
  storage:
    storageUri: "hf://meta-llama/Llama-3.3-70B-Instruct"
    path: "/models"
---
apiVersion: ome.io/v1beta1
kind: ClusterServingRuntime
metadata:
  name: sglang-llama-70b
spec:
  supportedModelFormats:
  - modelFormat:
      name: safetensors
    modelArchitecture: LlamaForCausalLM
    modelSizeRange:
      min: "65B"
      max: "75B"
    autoSelect: true
    priority: 100
---
apiVersion: ome.io/v1beta1
kind: InferenceService
metadata:
  name: production-chat-service
spec:
  model:
    name: llama-3-70b-instruct
  engine:
    minReplicas: 2
    maxReplicas: 10
  decoder:    # Presence enables P/D disaggregation
    minReplicas: 4
    maxReplicas: 20
  router:
    replicas: 2
```

Key features:

- First-class SGLang integration (cache-aware load balancing, multi-node, P/D, multi-LoRA)
- Also supports vLLM and Triton
- Kueue integration for gang scheduling of multi-pod workloads
- LeaderWorkerSet for resilient multi-node deployments
- KEDA for advanced custom metrics-based autoscaling
- Gateway API integration for routing
- Web console for managing models and services
- Supports 80+ model families (Llama, Qwen, DeepSeek, Gemma, Phi, etc.)

OME's roadmap mentions a future "management cluster orchestrating across multiple worker clusters" architecture, but this is not yet implemented.

---

### llm-d

**Red Hat / Google. Composable stack, not monolithic.**

llm-d is a Kubernetes-native framework for scalable LLM serving that takes a building-blocks approach rather than providing a monolithic solution. It provides the scheduling and runtime layer that KServe's LLMInferenceService builds on top of.

Key capabilities:

- KV-cache aware scheduling via the Endpoint Picker (EPP)
- Prefix-cache aware routing (routes requests to replicas with relevant cache state)
- P/D disaggregation with NIXL connector support
- Distributed inference using LeaderWorkerSet
- Designed to run under KServe's control plane in a Leader/Worker pattern
- The scheduler can be embedded in Envoy or deployed independently
- Makes real-time routing decisions based on cache and load information

llm-d isn't used directly by end users — it's the infrastructure that KServe's LLMInferenceService manages. The relationship is: KServe provides the CRD and lifecycle management, llm-d provides the intelligent scheduling and routing, vLLM provides the engine.

---

### Kthena

**Volcano sub-project. Gang scheduling and topology-aware placement.**

Kthena's `ModelServing` CRD (v1alpha1) has a three-tier hierarchy similar to AIBrix (ModelServing → ServingGroup → Role → Pods). The key differentiator is `schedulerName: volcano` — the whole point is integration with Volcano's gang scheduling and HyperNode topology-aware placement.

Kthena also has a separate `ModelBooster` CRD that's higher-level with automatic hardware detection and communication backend configuration (NCCL/HCCL), meant for simpler deployments.

Example CR:

```yaml
apiVersion: workload.serving.volcano.sh/v1alpha1
kind: ModelServing
metadata:
  name: PD-sample
  namespace: default
spec:
  schedulerName: volcano
  replicas: 1
  recoveryPolicy: ServingGroupRecreate
  template:
    restartGracePeriodSeconds: 60
    roles:
    - name: prefill
      replicas: 1
      entryTemplate:
        spec:
          initContainers:
          - name: downloader
            image: ghcr.io/volcano-sh/downloader:latest
            args:
            - --source
            - Qwen/Qwen3-8B
            - --output-dir
            - /models/Qwen3-8B/
          containers:
          - name: prefill
            image: aibrix/vllm-openai:v0.10.0-cu128-nixl-v0.4.1
            command: ["sh", "-c"]
            args:
            - |
              python3 -m vllm.entrypoints.openai.api_server \
                --host "0.0.0.0" --port "8000" \
                --model /models/Qwen3-8B \
                --served-model-name qwen3-8B \
                --kv-transfer-config '{"kv_connector":"NixlConnector","kv_role":"kv_both"}'
            resources:
              limits:
                nvidia.com/gpu: "1"
    - name: decode
      replicas: 1
      # Similar structure
```

Key features:

- Gang scheduling ensures all pods in a multi-GPU deployment are placed simultaneously
- HyperNode topology awareness for optimal GPU interconnect (NVLink, NVSwitch)
- Recovery policies for fault tolerance
- PodGroup integration for scheduling alignment

---

### NVIDIA Dynamo

**NVIDIA. Hardware-optimized inference framework.**

Dynamo is NVIDIA's purpose-built framework optimized for their hardware stack. It includes Model Express for fast model distribution and caching on nodes, and deep integration with NVIDIA's GPU topology and interconnect features. It's the most hardware-vendor-specific option and primarily targets NVIDIA-only deployments.

---

## Routing Layer

### Gateway API Inference Extension

**Kubernetes SIG. Shared routing substrate.**

The Gateway API Inference Extension is not a deployment CRD — it's the routing layer that sits in front of all the inference orchestration projects. It's becoming the shared substrate that multiple projects (KServe, AIBrix, OME) integrate with.

Key CRDs:

**`InferencePool` (v1)** — "A Service, but for LLM pods." It knows about KV cache utilization, queue depth, and LoRA adapters via the Endpoint Picker (EPP). The EPP is a sidecar that communicates with inference pods to make intelligent routing decisions.

**`InferenceModel` (v1alpha2)** — Enables canary/blue-green between model versions or LoRA adapters, plus criticality-based priority (Critical requests shed Best Effort ones under load). Supports weighted traffic splitting between target models.

**`InferenceObjective` (v1alpha2)** — For priority without model routing. Sets priority levels for requests without the full model routing machinery.

Example CRs:

```yaml
apiVersion: inference.networking.k8s.io/v1
kind: InferencePool
metadata:
  name: vllm-llama3-8b-instruct
spec:
  targetPorts:
  - number: 8000
  selector:
    app: vllm-llama3-8b-instruct
  extensionRef:
    name: vllm-llama3-8b-instruct-epp
    port: 9002
    failureMode: FailClose
---
apiVersion: inference.networking.x-k8s.io/v1alpha2
kind: InferenceModel
metadata:
  name: inferencemodel-llama2
spec:
  modelName: llama2
  criticality: Critical
  poolRef:
    name: vllm-llama2-7b-pool
  targetModels:
  - name: vllm-llama2-7b-2024-11-20
    weight: 75
  - name: vllm-llama2-7b-2025-03-24
    weight: 25
```

---

## Inference Engines

### vLLM

**UC Berkeley. The "Linux" of inference engines.**

vLLM's core insight was PagedAttention — treating KV cache memory like virtual memory pages, eliminating wasted GPU memory from pre-allocated contiguous blocks. It's become the default engine in the ecosystem through breadth: wide hardware support (NVIDIA, AMD, TPU, Gaudi, CPU), wide model support, and being the engine that every orchestration project integrates with first.

Strengths:

- Widest hardware support (NVIDIA, AMD MI300X, Intel Gaudi, Google TPU, CPU)
- Widest model support
- Most battle-tested P/D disaggregation in production (ByteDance via AIBrix, Red Hat via llm-d)
- OpenAI-compatible API
- INT8/FP8 quantization
- Multiple parallelism modes (TP, PP, DP)
- Largest contributor base

Relative weaknesses:

- Structured output / constrained decoding (SGLang consistently ahead here)
- Prefix cache was added later, not architecturally native
- Cache-aware load balancing relies on external systems (llm-d EPP, AIBrix gateway)

### SGLang

**LMSYS. Program-level optimization.**

SGLang (Structured Generation Language) came from the Chatbot Arena team. Its founding insight was optimizing at the program level, not just the request level. LLM calls aren't isolated — they're part of programs with branching, loops, and multiple calls that share prefixes.

SGLang's runtime was designed from the start around RadixAttention, which uses a radix tree to automatically detect and reuse KV cache across requests sharing common prefixes. Its router natively understands prefix cache state across replicas and routes for cache hits — this is what OME leans into with its "deep native integration."

Strengths:

- Structured output / constrained decoding (compressed FSM for JSON schema / regex-guided generation)
- Multi-request program optimization (shared prefix computed once across call sequences)
- Cache-aware load balancing native to the router
- Speculative decoding (EAGLE-style via SpecForge)
- Multi-turn and structured output workload performance

Relative weaknesses:

- Primarily NVIDIA-focused (limited AMD, Intel, TPU support)
- Smaller production deployment base for P/D disaggregation
- Smaller contributor community

### Head-to-Head

Raw throughput on standard benchmarks flip-flops release to release. Neither has a decisive lead on pure token throughput for simple completions. SGLang tends to win multi-turn and structured output; vLLM tends to match or win on straightforward single-turn high-throughput scenarios.

For a platform targeting multiple backends: the engine choice should be a property of the serving runtime, not a global decision. SGLang for structured output / agentic workloads, vLLM for multi-cloud / multi-hardware deployments.

---

## Single-Cluster Projects (Not Orchestration Platforms)

### Kaito (Microsoft)

The closest thing to a "higher level" inference tool. Provisions GPU nodes AND deploys models, which is more than KServe does. But single-cluster, Azure-centric, and lacks platform team / app team separation or multi-cloud capabilities. Essentially "KServe that also calls the Azure API to get you GPUs."

### KubeAI

Simplifies model serving but is single-cluster with no infrastructure provisioning layer.

### Ray Serve

Can orchestrate inference across a Ray cluster, but it's a completely different paradigm — not Kubernetes-native, no declarative CRDs, no multi-cluster support.

### Kubeflow

The broader MLOps platform that includes KServe. Not trying to be a global inference control plane — it's a collection of ML lifecycle tools (training, notebooks, pipelines, serving).

---

## Convergence Patterns

Several patterns are converging across these projects despite different implementations:

- **Gateway API Inference Extension** as shared routing substrate (KServe, AIBrix, OME all integrating)
- **vLLM as default engine** with SGLang as specialized alternative
- **LeaderWorkerSet** for multi-node inference (used by KServe, OME, referenced by Kthena)
- **NIXL connectors** for KV cache transfer in P/D disaggregation (AIBrix, Kthena, llm-d)
- **P/D disaggregation** supported by all projects, though production-readiness varies
- **OpenAI-compatible API** as the standard inference protocol

The key gap across all these projects: none of them answer "I have clouds, budgets, teams, and models — how do I provide self-service inference as a platform?" They all assume a cluster exists with GPUs present. The multi-cloud, multi-cluster, self-service platform layer above them remains unaddressed by open source tooling.
