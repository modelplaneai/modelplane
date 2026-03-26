# SaaS Inference Service Landscape

*March 2026*

## Overview

The Kubernetes inference landscape document covers projects that run models on
clusters you operate. This document covers the other side: services where you
don't get a cluster at all. You give them a model reference and hardware
preference, they give you an OpenAI-compatible endpoint. The question is which
of these services have APIs rich enough to programmatically deploy models, poll
for status, discover endpoints, and tear down deployments.

Two tiers exist:

- **Deploy-and-serve platforms:** You deploy a model via API, the service runs
  it on dedicated GPUs, you get an endpoint. Full deployment lifecycle (create,
  scale, stop, delete).
- **Hosted inference APIs:** The service hosts popular models on shared
  infrastructure. You send requests, you don't deploy anything. There's no
  deployment to manage — the model is already running.

The deploy-and-serve platforms are the interesting ones. They're the SaaS
equivalent of "KServe on a cluster someone else manages."

---

## Deploy-and-Serve Platforms

### Together AI

**The simplest API of the four. Model + hardware + autoscaling = endpoint.**

Together's dedicated endpoints API is three fields: a model name (HuggingFace-
style, e.g. `Qwen/Qwen3.5-9B-FP8`), a hardware string, and autoscaling config.

```
POST /v1/endpoints
{
  "model": "Qwen/Qwen3.5-9B-FP8",
  "hardware": "1x_nvidia_h100_80gb_sxm",
  "autoscaling": { "min_replicas": 1, "max_replicas": 5 },
  "inactive_timeout": 60,
  "availability_zone": "us-east-1a"
}
```

Hardware is a string encoding GPU count, model, memory, and interconnect:
`2x_nvidia_h100_80gb_sxm`. You can list compatible hardware for a model via
`together endpoints hardware --model <MODEL_ID>`. Endpoint states are linear:
`PENDING → STARTING → STARTED → STOPPING → STOPPED → ERROR`.

Inference uses the standard OpenAI-compatible API (`POST /v1/chat/completions`)
with the endpoint name as the `model` field. Endpoints auto-stop after a
configurable inactivity timeout (default 1 hour).

Key characteristics:

- No control over serving engine — Together picks it
- No engine config knobs (no vLLM flags, no quantization options beyond what the
  model weights already are)
- Custom models must be uploaded first via a separate API
- Only models Together explicitly supports on dedicated endpoints work — not
  every HuggingFace model
- Supports speculative decoding (on by default, can be disabled)
- Prompt caching always enabled, not configurable

The model name and resource requirements are the only things a caller controls.
Engine config is entirely the service's problem.

No existing Crossplane provider or Kubernetes operator.

---

### Fireworks AI

**Adds deployment shapes — pre-configured templates optimized for speed, throughput, or cost.**

Fireworks' core abstraction is the **deployment**, but they add an interesting
layer: **deployment shapes**. A shape is a pre-configured bundle of hardware,
quantization, and engine optimizations. You can deploy from a shape or from raw
hardware specs.

```bash
# Deploy from a shape
firectl deployment create accounts/fireworks/models/llama-v3p3-70b-instruct \
  --deployment-shape throughput --wait

# Deploy from raw hardware
firectl deployment create accounts/fireworks/models/deepseek-v3 \
  --accelerator-type NVIDIA_H200_141GB --accelerator-count 8
```

Resource naming is hierarchical: `accounts/<ACCOUNT_ID>/models/<MODEL_ID>`,
`accounts/<ACCOUNT_ID>/deployments/<DEPLOYMENT_ID>`. This is the most
"infrastructure-y" naming of the four — it feels like a cloud provider API.

Key characteristics:

- Deployment shapes abstract away hardware/engine tuning into named profiles
  (fast, throughput, minimal) — the platform team doesn't configure GPU types,
  they pick an optimization target
- Supports uploading custom models from HuggingFace
- LoRA addons can be deployed on top of a base model deployment
- Autoscaling with min/max replicas, scale-to-zero (default 1 hour inactivity)
- Deployments scaled to zero are automatically deleted after 7 days of no
  traffic
- GPU options: A100 80GB, H100 80GB, H200 141GB
- `firectl` CLI is the primary interface; the REST API is less well-documented
- Inference via standard OpenAI-compatible API

Deployment shapes are an interesting abstraction. The caller expresses an
optimization target (latency vs throughput vs cost), not hardware specifics.
Shapes are opaque — you don't see what hardware/config they resolve to without
querying the shape API.

No existing Crossplane provider or Kubernetes operator.

---

### Hugging Face Inference Endpoints

**The closest match to Modelplane's model. You pick the model, cloud, region, instance, engine, and scaling.**

HF Inference Endpoints is the most configurable of the four. You create an
endpoint by specifying a HuggingFace repo, cloud provider, region, instance
type, inference engine, and scaling config.

Configuration knobs:

- **Model:** Any HuggingFace repo ID + optional commit revision
- **Cloud provider:** AWS, Azure, or GCP
- **Region:** Per-provider (e.g., us-east-1 for AWS)
- **Instance type:** Pre-defined SKUs per cloud/region with GPU type, count,
  memory, and per-hour pricing
- **Engine:** vLLM, TGI (Text Generation Inference), SGLang, TEI (Text
  Embeddings Inference), llama.cpp, or custom container
- **Scaling:** Min/max replicas, scale-to-zero timeout, autoscaling strategy
  (hardware utilization or pending request count)
- **Security:** Public, private (HF token), or AWS PrivateLink
- **Environment variables and secrets**

The engine choice is what makes this interesting for Modelplane. HF is the only
SaaS backend where ClusterModel's engine field actually transfers — you could
deploy the same model on vLLM via HF Inference Endpoints or via KServe on GKE,
and the engine choice is meaningful in both cases.

Key characteristics:

- The `huggingface_hub` Python library is the best-documented interface, with
  `create_inference_endpoint()` / `delete_inference_endpoint()` methods
- The REST API is at `api.endpoints.huggingface.cloud`
- Model must be on HuggingFace — no S3/GCS sources
- Supports custom container images for unsupported models
- Multi-cloud (AWS, Azure, GCP) with per-provider instance types and pricing

No existing Crossplane provider or Kubernetes operator. The `huggingface_hub`
Python library provides programmatic endpoint management.

---

### Baseten

**The most complex. Deployment is a build-then-serve pipeline, not a simple API call.**

Baseten's deployment model is fundamentally different from the other three. The
deployment unit is a **Truss** — a packaging format with a `config.yaml` and
optional custom Python code. You push a Truss, Baseten builds a container
(optionally compiling the model with TensorRT-LLM), and deploys it. Deployments
are then promoted to **environments** (production, staging, custom). Each
environment has its own stable endpoint URL and autoscaling settings.

```yaml
# config.yaml — the deployment unit
model_name: Llama-3.1-70B
resources:
  instance_type: "H100:4"
trt_llm:
  build:
    base_model: decoder
    checkpoint_repository:
      source: HF
      repo: "meta-llama/Llama-3.1-70B-Instruct"
    quantization_type: fp8
    tensor_parallel_count: 4
```

Instance types are strings encoding GPU type and count: `H100:2` (2x H100),
`A100:4x48x576` (4x A100, 48 vCPU, 576GB RAM), `L4:4x16` (L4, 4 vCPU, 16GB
RAM). The hardware range is broad — from CPU-only instances ($0.00058/min) up to
8x B200 ($1.33/min).

Key characteristics:

- Two-phase deployment: build (TensorRT-LLM compilation, 10–30 minutes) then
  deploy — significantly slower than the other three
- The `truss push` CLI is the first-class deployment path, not the REST API
- Management REST API supports full CRUD on models, deployments, and
  environments, plus autoscaling configuration, rolling updates, and
  activate/deactivate
- Multiple inference engines: Engine-Builder-LLM (TensorRT-LLM), BIS-LLM
  (MoE-optimized), custom vLLM, custom SGLang, or arbitrary Docker containers
- Environment promotion model adds a lifecycle layer — deployment →
  (promote) → environment — that doesn't exist in the other services
- Baseten's engine config (`trt_llm`, `engine-builder-llm`, `bis-llm`) is its
  own format — it doesn't map to vLLM or SGLang flags
- OpenAI-compatible endpoints for engine-based deployments
- Regional environments for data residency requirements

Baseten is the hardest to map to a Modelplane InferenceEnvironment. A Crossplane
provider would need to generate a Truss config from ClusterModel's resource
requirements, push it via the API, wait for the build + deploy, and promote to
an environment. The build step makes the feedback loop much slower than the
other services, and the Truss packaging format is a different abstraction than
"deploy these weights with this hardware."

No existing Crossplane provider or Kubernetes operator.

---

## GPU Cloud Providers

A note on CoreWeave, Lambda Labs, and similar GPU cloud providers: these are
IaaS, not inference platforms. CoreWeave gives you a Kubernetes cluster (CKS)
with GPU nodes. Lambda gives you VMs with GPUs. You bring your own inference
stack. They're in the same category as GKE — a place to run KServe, Dynamo, or
whatever else — not a managed inference service.

CoreWeave's CKS API (`POST /v1beta1/cks/clusters`) supports programmatic
cluster creation with GPU node pools, and they have a Terraform provider. The
API is simple enough that a controller could provision a cluster, add node
pools, and get a kubeconfig — same pattern as GKE, different API.

---

## Hosted Inference APIs (No Custom Deployment)

These services host popular models on shared infrastructure. You send inference
requests, you don't deploy anything. They could back a limited kind of
InferenceEnvironment where ModelPlacement verifies the model is available and
configures routing, rather than creating a deployment.

**Groq** — LPU-based inference, very fast. Only models they host. No custom
deployment. Interesting for latency-sensitive workloads but the model catalog is
fixed.

**Cerebras** — Wafer-scale inference, extremely fast. Same limitation — you use
their hosted models. Their speed advantage is substantial enough that routing to
Cerebras for specific models could be valuable, but there's no deployment
lifecycle to manage.

**NVIDIA NIM (hosted via API Catalog)** — NVIDIA hosts optimized NIM endpoints.
Fast inference for their catalog of models. Self-hosted NIM is different — that's
a container you run on your own infrastructure (closer to a KServe backend than
a SaaS service).

**SambaNova Cloud** — Fast inference for hosted models. Same pattern.

The interesting question is whether these are useful as inference targets at all
given the lack of a deployment lifecycle. The model isn't deployed, it's already
running. The only operation is "verify this model is available on this service
and route to it."

---

## Convergence Patterns

Several patterns are shared across the deploy-and-serve platforms:

- **OpenAI-compatible API** as the standard inference protocol. All four
  services expose `POST /v1/chat/completions` with the same request/response
  schema.
- **HuggingFace model names** as the common model identifier. Together, Fireworks,
  and HuggingFace all use HuggingFace repo names (`meta-llama/Llama-3.1-70B-
  Instruct`) as model identifiers. Baseten uses HuggingFace repos as the weight
  source in its config.
- **Autoscaling with scale-to-zero.** All four support min/max replicas with
  configurable inactivity timeouts. The economics of inference make scale-to-zero
  important — GPU time is expensive when idle.
- **No engine exposure.** Together and Fireworks give you no control over the
  serving engine. HuggingFace lets you choose. Baseten has its own engine
  format. The trend is toward the engine being the service's problem, not the
  user's.
- **Hardware as a string or SKU.** Each service has its own format, but the
  concept is the same: you pick a GPU type and count, not a machine type or
  instance family.

The key gap: none of these services have a multi-model, multi-environment,
multi-team abstraction. Each deployment is independent. There's no "deploy this
model to 3 regions with unified routing and platform-team-defined policy."
