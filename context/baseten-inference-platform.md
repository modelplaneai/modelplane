# BaseTen: the inference platform shaping AI deployment

**BaseTen has emerged as one of the fastest-growing AI inference platforms, reaching a $5 billion valuation in January 2026 after tripling its valuation in under 12 months.** The company's core thesis — that inference, not training, is the defining infrastructure challenge of production AI — has proven prescient as enterprise AI adoption accelerates. BaseTen's value proposition sits above raw GPU compute: it provides a managed "model as a service" abstraction layer built on multi-cloud Kubernetes orchestration, the open-source Truss packaging framework, concurrency-based autoscaling with scale-to-zero, and deep inference runtime optimizations (TensorRT-LLM, custom CUDA kernels, KV cache-aware routing). For the Modelplane project, BaseTen represents the clearest commercial benchmark of what developers expect from an inference platform built on top of Kubernetes primitives.

---

## Founding story and rapid ascent to $5B

BaseTen was founded in **2019 in San Francisco** by four former Gumroad colleagues who had spent years wrestling with the gap between training ML models and operating them in production. The co-founders are **Tuhin Srivastava** (CEO, previously co-founded Shape, an HR analytics startup), **Amir Haghighat** (CTO, PhD in Mathematics, former Head of Engineering at Gumroad), **Philip Howes** (Chief Scientist, co-founded Shape with Srivastava), and **Pankaj Gupta** (former engineer at Uber and Twitter). The company name comes from "base ten" blocks used to teach arithmetic in Australia, where Srivastava and Howes grew up — a metaphor for simplifying complex systems.

The funding trajectory tells the growth story clearly. After modest seed and Series A rounds totaling **$20M** (led by Greylock's Sarah Guo), BaseTen raised three rounds in rapid succession: a **$75M Series C** in February 2025 at an $825M valuation, a **$150M Series D** in September 2025 at $2.15B (led by BOND), and a **$300M Series E** in January 2026 at $5B (with NVIDIA contributing $150M of the round). Total funding stands at **$585M** from investors including IVP, Spark Capital, CapitalG, Greylock, Conviction, and NVIDIA.

Revenue grew from essentially zero through 2022 to an estimated **$15.8M in 2025**, with the company reporting **10x revenue growth** in the 12 months preceding September 2025 and **100x inference volume growth** in the preceding year. The team grew from ~46 employees in late 2024 to approximately **186–200** by early 2026. BaseTen serves **100+ enterprises** and hundreds of smaller businesses, with near-zero customer churn as of February 2025.

---

## The Model → Deployment → Replica abstraction

BaseTen's core developer-facing abstraction treats **every model as a managed microservice** with its own HTTP endpoint, GPU resources, autoscaling policy, and deployment lifecycle. Understanding this abstraction hierarchy is critical for Modelplane's design.

**Model** is the top-level logical entity, identified by a stable `model_id`. Each model contains multiple **Deployments** — immutable builds representing specific versions of the model code and configuration. Deployments come in three flavors: *development* (single replica, live reload enabled for fast iteration), *production* (full autoscaling, promoted via CLI or API), and *published* (arbitrary deployments with custom autoscaling). Each deployment runs as one or more **Replicas** — individual container instances executing on GPUs.

**Environments** provide stable URL routing across deployment versions. A "production" environment always points to whichever deployment is currently promoted, so clients never change URLs during upgrades. Additional environments (staging, canary, shadow) enable progressive rollout patterns. Every deployed model gets a REST endpoint at `https://model-{model_id}.api.baseten.co/{environment}/predict`.

BaseTen offers three product tiers built on this abstraction:

- **Dedicated Deployments** give full control over GPU type, autoscaling parameters, model code, and inference engine — this is the core product for custom/fine-tuned models
- **Model APIs** provide pre-optimized OpenAI-compatible endpoints for popular open-source models (DeepSeek, Qwen, Llama, GLM) with per-token pricing and no deployment management
- **Chains** enable multi-model workflows where each step runs on independently scaled hardware with point-to-point RPC (no centralized orchestrator)

---

## API surface and developer experience in detail

BaseTen exposes a layered API surface that Modelplane should study carefully. The **Inference API** handles model invocation, while the **Management API** handles lifecycle operations.

### Inference endpoints

Every model deployment gets synchronous and asynchronous predict endpoints. Synchronous calls hit `POST /production/predict` (or `/development/predict`, `/environments/{env}/predict`, `/deployment/{id}/predict`). Async calls use `/async_predict` variants with webhook delivery, priority queuing, and configurable `max_time_in_queue_seconds` (10s to 72h). Rate limits are **200 requests/second** with up to **50,000 queued requests** per organization.

For LLM deployments using TensorRT-LLM or vLLM, BaseTen serves **OpenAI-compatible `/v1/chat/completions` endpoints**, making it a drop-in replacement for OpenAI SDK calls. SSE-based streaming delivers token-by-token output. WebSocket transport is available for real-time use cases. A dedicated transcription API handles both pre-recorded and streaming audio.

Authentication uses API keys in the format `Authorization: Api-Key abcd1234.abcd1234`. Every deployment also gets a **wake endpoint** to proactively warm models from scale-to-zero before user traffic arrives.

### Management API

The REST management API at `https://api.baseten.co/v1/` provides CRUD operations on models, deployments, chains, environments, and secrets. Key operations include listing all deployments for a model, promoting a deployment to production (with optional `scale_down_previous_production`), patching autoscaling settings mid-flight, and activating/deactivating deployments. The `GET /v1/instance_types` endpoint lists available GPU types — useful for dynamic resource selection.

Autoscaling settings are patchable per-deployment via API:

```json
{
  "min_replica": 1,
  "max_replica": 5,
  "concurrency_target": 10,
  "autoscaling_window": 30,
  "scale_down_delay": 600
}
```

### CLI and SDK workflow

The `truss` CLI (installed via `pip install truss`) is the primary developer interface. The deployment workflow is: `truss init` → implement `model.py` → `truss push` (creates dev deployment) → `truss watch` (live-reload code changes without rebuild) → `truss push --promote` (production). The `truss watch` command is a critical DX innovation — it patches code onto a running server, re-executes only the `load()` method, and skips the 3–30 minute full container rebuild cycle.

The **Chains SDK** (`truss_chains`) uses Python class inheritance and decorators to define multi-step workflows where each "Chainlet" specifies its own hardware, dependencies, and autoscaling via `RemoteConfig`. Chainlets call each other through direct RPC, with the framework handling serialization and network transport.

---

## Truss: the open-source packaging layer

**Truss is the deployment unit for BaseTen** — the bridge between local development and cloud execution. Licensed MIT, it lives at `github.com/basetenlabs/truss` with approximately **1,100 GitHub stars**, 91 forks, 59 contributors, and 564 releases. The monorepo includes `truss-chains`, `truss-train`, and `truss-transfer` sub-packages. BaseTen maintains **86 total repositories** on GitHub, including `truss-examples` (218 stars) with dozens of production-ready model configurations.

### Two-file contract

Every Truss project requires exactly two files: **`model/model.py`** and **`config.yaml`**. The Model class implements `load()` (called once at startup to load weights) and `predict()` (called per request). This minimal contract is framework-agnostic — PyTorch, TensorFlow, HuggingFace, scikit-learn, or any Python-based model works identically.

### config.yaml as the single source of truth

The `config.yaml` declaratively specifies everything about the runtime environment. Key sections that Modelplane should replicate:

- **`resources`**: GPU type and count (`accelerator: H100:4`), CPU, memory, multi-node (`node_count: 2`)
- **`requirements`** / **`system_packages`**: Python pip and apt dependencies
- **`model_cache`**: Pre-cache weights from HuggingFace, S3, GCS, or Azure at build time with pattern filtering
- **`runtime`**: Concurrency settings (`predict_concurrency`), transport type (HTTP/WebSocket/gRPC), health check configuration
- **`secrets`**: Declared by name in config, values stored securely in the platform
- **`docker_server`**: Deploy any HTTP server (vLLM, SGLang, Ollama) without writing a Model class — just specify start command, health endpoint, predict endpoint, and port
- **`trt_llm`**: Engine Builder configuration for automatic TensorRT-LLM compilation with quantization, speculative decoding, and chunked prefill
- **`base_image`**: Custom Docker base images with private registry authentication

The **Custom Docker Server** pattern (`docker_server` config key) is particularly important for Modelplane's design. It allows deploying any containerized inference server (vLLM, SGLang, TGI, Ollama, ComfyUI) with zero custom Python code — just specify the Docker image, start command, and endpoint mappings. This makes Truss an abstraction layer over arbitrary serving runtimes.

### How Truss relates to the commercial platform

Truss generates Docker containers, but the commercial platform adds everything above: GPU orchestration, multi-cloud capacity management, autoscaling, cold start optimizations, secrets management, monitoring dashboards, environment promotion, and compliance certifications. This separation is clean — Truss handles packaging and local testing; BaseTen handles operations. An open-source Modelplane could slot into this same separation point, replacing BaseTen's proprietary operations layer with Crossplane-managed Kubernetes resources.

---

## Infrastructure architecture under the hood

BaseTen operates an **asset-light model** — they own no GPUs. Instead, they aggregate compute from **10+ cloud service providers** across dozens of regions, creating a unified elastic GPU pool. The architectural centerpiece is their proprietary **Multi-Cloud Capacity Management (MCM)** system.

### Hub-and-spoke Kubernetes orchestration

MCM uses a **hub-and-spoke architecture built on top of Kubernetes**. A global control plane receives real-time event streams from every workload plane (individual Kubernetes clusters in different regions and clouds). The control plane makes globally optimal placement decisions and delegates execution to local clusters. This design was necessary because standard Kubernetes assumes sub-10ms latency between nodes, which breaks across geographic distances.

MCM treats GPUs across different clusters, regions, and clouds as **completely fungible** — an H100 in AWS us-east-1 is equivalent to an H100 in GCP us-west4. The system can provision thousands of GPUs within **less than five minutes** by drawing from the global pool. This enables active-active deployments across clouds for fault tolerance, with automatic failover during cloud provider outages occurring **within minutes**.

### Autoscaling mechanics

BaseTen's autoscaler is **concurrency-based**, not request-rate-based. It watches in-flight request counts per replica and adjusts replicas to maintain each near its concurrency target. The default configuration uses a **concurrency target of 1**, a **target utilization of 70%**, an **autoscaling window of 60 seconds**, and a **scale-down delay of 900 seconds** (15 minutes).

Scale-up is immediate when average utilization crosses the threshold within the autoscaling window. Scale-down uses **exponential backoff** — removing half the excess replicas, waiting, then removing half again — to prevent thrashing during bursty traffic. When `min_replica = 0`, models scale to zero, and incoming requests queue while new replicas spin up.

For Modelplane's Crossplane implementation, this translates to a custom controller watching request concurrency metrics and adjusting a ReplicaSet-like resource accordingly. The key parameters to expose: min/max replicas, concurrency target, utilization threshold, autoscaling window, and scale-down delay.

### Cold start optimization is multi-layered

BaseTen's cold start strategy combines several techniques that Modelplane should consider implementing:

- **b10cache**: A distributed filesystem cache acting as a CDN for model weights. Region-level, with unused files garbage-collected after 4 days. Hot-cached weights on the same physical node are shared across deployments
- **Parallelized byte-range downloads**: Achieves >1GB/s on 10Gbit ethernet by downloading model weights in parallel chunks
- **Image streaming with call-graph analysis**: On first deployment, runtime monitors file access patterns. Subsequent cold starts use call-graph data to prefetch only files needed at startup, streaming the rest lazily
- **Background Rust thread**: Downloads weights in a background thread during Python module imports, overlapping I/O with CPU-bound initialization
- **Wake endpoints**: Proactive warming before traffic arrives
- **torch.compile caching**: Caches compiled model artifacts across restarts

The results are significant: **Stable Diffusion XL cold-starts in ~9 seconds on A100**, and general models up to 20GB come online in under 10 seconds — down from 5+ minutes without optimization.

---

## GPU pricing and billing model

BaseTen uses **per-minute billing** for Dedicated Deployments and **per-token billing** for Model APIs. There is no charge for idle time when scaled to zero.

### Dedicated deployment GPU pricing

| GPU | VRAM | Per minute | Per hour (approx.) |
|-----|------|------------|-------------------|
| T4 | 16 GiB | $0.01052 | $0.63 |
| L4 | 24 GiB | $0.01414 | $0.85 |
| A10G | 24 GiB | $0.02012 | $1.21 |
| A100 | 80 GiB | $0.06667 | $4.00 |
| H100 MIG | 40 GiB | $0.06250 | $3.75 |
| H100 | 80 GiB | $0.10833 | $6.50 |
| B200 | 180 GiB | $0.16633 | $9.98 |

Multi-GPU pricing scales linearly (e.g., 8× H100 = $0.86667/min ≈ $52/hr). CPU-only instances start at $0.00058/min. BaseTen announced a **40% price reduction** across all instance types, passing on savings from better cloud provider deals.

Three plan tiers exist: **Basic** ($0/month, pay-as-you-go), **Pro** (volume discounts, priority GPU access, dedicated Slack/Zoom support), and **Enterprise** (custom SLAs, self-hosted deployments, data residency control, advanced RBAC). Self-hosted pricing starts at **$25,000/month per region** on Google Cloud Marketplace or **$5,000/month** on AWS Marketplace.

One competitive note: per-minute billing is less granular than per-second competitors. For Modelplane, supporting per-second billing granularity would be a differentiation opportunity.

---

## How BaseTen differentiates from every competitor category

BaseTen occupies a specific position in the inference landscape — more optimized than general compute platforms, more flexible than API-only providers, and more developer-friendly than cloud incumbents.

**Versus Modal** (closest competitor, $1.1B valuation): Modal is general-purpose serverless Python compute using Python decorators for infrastructure definition. BaseTen is inference-specialized with deeper runtime optimizations (TensorRT-LLM Engine Builder, KV cache-aware routing, custom CUDA kernels), enterprise compliance (HIPAA, SOC 2 Type II), and a dedicated model performance engineering team. Modal excels at batch processing and arbitrary compute; BaseTen excels at low-latency, high-throughput model serving.

**Versus Fireworks AI and Together AI**: These are primarily API-only providers optimized for token throughput on popular open-source models. BaseTen differentiates by supporting **custom/fine-tuned models** with dedicated GPU deployments, exposing more configuration "knobs" (GPU selection, autoscaling parameters, inference engine choice), and offering deployment flexibility (cloud, VPC, hybrid).

**Versus Replicate**: Replicate targets individual developers and prototyping with its Cog framework and community model library. BaseTen targets **production enterprise workloads** with governance, compliance certifications, and forward-deployed engineering support.

**Versus RunPod and CoreWeave**: These provide raw GPU compute with minimal abstraction. BaseTen adds the entire inference operations layer — autoscaling, model packaging, performance optimization, monitoring, and multi-cloud orchestration. The tradeoff is less control and higher cost per GPU-hour.

**Versus AWS SageMaker, GCP Vertex AI, Azure ML**: Cloud incumbents offer comprehensive MLOps but with significantly higher complexity and vendor lock-in. BaseTen differentiates through faster iteration cycles (minutes vs. hours), inference-specific optimizations, multi-cloud portability, and a streamlined developer experience. However, hyperscalers remain the most significant long-term competitive threat due to enterprise relationships and bundled cloud spend.

BaseTen's **unique differentiators** that are hardest for competitors to replicate: (1) forward-deployed engineering teams that embed with customers to co-own production outcomes, (2) multi-cloud capacity management across 10+ providers for reliability and GPU availability, and (3) the inference-specific optimization stack (Engine Builder, BEI, custom kernels, KV cache-aware routing).

---

## Customer base reveals the target profile

BaseTen's publicly disclosed customers paint a clear picture of their ideal user. **Cursor** (AI code editor), **Notion** (AI features in productivity), **Writer** (custom enterprise LLMs), **Superhuman** (AI email), **Patreon** (audio transcription), **Bland AI** (AI phone calls), **Zed** (code editor AI), **OpenEvidence** (medical AI), **Abridge** (clinical documentation), **Descript** (audio/video AI), **HeyGen** (video generation), **Clay** (data enrichment), **Retool** (AI in low-code), and **Gamma** (AI presentations).

The pattern: **AI-native companies building latency-sensitive, mission-critical products on custom or fine-tuned models**. These teams have ML expertise but don't want to manage GPU infrastructure. Key results customers report include **60% throughput improvement** (Writer), **80% lower P95 latency** (Superhuman), **sub-400ms response times** (Bland AI), **half the cost of OpenAI** (Patreon), and savings equivalent to **2 full-time ML engineers** (Patreon).

---

## What Modelplane should learn from BaseTen

For an open-source Crossplane-based alternative, BaseTen reveals several design principles worth encoding:

**The two-file contract works.** Truss's `model.py` + `config.yaml` pattern has proven sufficient for everything from simple scikit-learn models to 671B-parameter DeepSeek deployments. Modelplane should define equivalent CRDs (Custom Resource Definitions) that map cleanly to this abstraction — a `ModelDeployment` resource with spec fields mirroring config.yaml sections for resources, autoscaling, model caching, and runtime configuration.

**Config-driven inference engine selection is essential.** The `docker_server` pattern — deploying arbitrary HTTP servers (vLLM, SGLang, TGI) with just a config block specifying image, start command, and endpoint mappings — is the highest-leverage abstraction. It decouples the platform from any specific inference runtime.

**Autoscaling must be concurrency-aware, not just CPU/memory-based.** Standard Kubernetes HPA based on CPU utilization is inadequate for GPU inference workloads. BaseTen's concurrency-target-based autoscaling with configurable utilization thresholds, asymmetric scale-up/scale-down behavior, and scale-to-zero represents the minimum viable autoscaling for inference. Modelplane should implement a custom metrics-based HPA or KEDA scaler.

**Cold start optimization is a platform differentiator, not a nice-to-have.** BaseTen's multi-layered approach (distributed weight caching, parallel downloads, image streaming with call-graph analysis, wake endpoints) reduced cold starts from minutes to single-digit seconds. Modelplane should at minimum support persistent volume-based weight caching and proactive warm-up mechanisms.

**Environment promotion with stable URLs matters for production.** The Model → Deployment → Environment hierarchy, where environments provide stable endpoints across deployment versions, is a pattern that maps well to Kubernetes Services pointing at different Deployment resources via label selectors. This should be a first-class Crossplane composition.

**Multi-cloud is a moat, not a feature.** BaseTen's MCM system — treating GPUs across clouds as fungible — took their infrastructure team over six months to build. This global scheduler-over-local-Kubernetes-clusters pattern is architecturally similar to what Crossplane already provides. Modelplane has a natural advantage here if it can leverage Crossplane's existing multi-cloud resource management.
