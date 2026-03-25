# Modelplane v0.1 — Design Document

**Status:** Draft  
**Date:** March 2026  
**Author:** Nic Cope

## Executive summary

Modelplane is an open-source, Crossplane-based inference platform from Upbound.
It gives platform teams a declarative API for inference infrastructure and gives
ML teams a self-service interface to deploy models. The resource model is built
for a world where inference backends are heterogeneous: KServe on a GKE cluster
today, NVIDIA Dynamo on CoreWeave tomorrow. An ML team deploys a model like
this:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: llama
  namespace: ml-team
spec:
  modelRef:
    kind: ClusterModel
    name: llama-8b-vllm
  environments: 1
```

The platform team pre-configures the inference environment and model catalog
that make this possible. Under the hood, each ModelDeployment composes
`ModelPlacements` (one per target environment) that handle the backend-specific
work of actually running the model. The ML team gets a unified OpenAI-compatible
endpoint; Modelplane handles per-environment composition, status aggregation,
and routing.

## Background

Open-weight inference is going to be the default for enterprises. Cost control,
governance, and data secrecy will push them away from hosted proprietary models
and toward running open-weight models on infrastructure they control. Kubernetes
is the primary substrate, with KServe, vLLM, and the broader ecosystem maturing
fast. But it won't be the only way enterprises run inference. GPU clouds like
CoreWeave and Lambda Labs, managed inference services like BaseTen and NVIDIA
NIM, and hyperscaler AI endpoints are all part of the picture.

Projects like KServe, KubeAI, OME, llm-d, and NVIDIA Dynamo are getting good at
running models on Kubernetes, but they're not platforms in the same way
Kubernetes isn't a platform. They're scoped to one cluster, they expose
infrastructure concepts to their consumers, and they have no opinions about
teams or organizational policy. A platform team that adopts KServe still has to
build the multi-cluster topology, the self-service abstraction layer, and the
governance model on top of it.

Platform teams at companies like Apple and JPMC already use Crossplane to do
this kind of work for cloud infrastructure: unifying AWS, GCP, and Azure behind
declarative APIs on a central control plane. These teams are now being asked to
provide inference infrastructure to internal ML teams. KServe and its peers are
to Modelplane what cloud provider APIs are to Crossplane: powerful backends that
need a platform layer above them to become self-service with guardrails.
Modelplane is an opinionated platform layer, built on Crossplane v2.

## Goals

v0.1 is a proof-of-concept. Its job is to demonstrate that a Crossplane-native
inference platform is viable and compelling — not to be production-complete.

v0.1 is successful if:

1. **End-to-end demo works:** A platform team can install Modelplane, create an
   InferenceEnvironment, register a model, and an ML team can create a
   ModelDeployment and get a working OpenAI-compatible endpoint.

2. **Multi-environment demo works:** A ModelDeployment targeting multiple
   InferenceEnvironments creates ModelPlacements on each. A unified endpoint
   routes requests across placements.

3. **The abstraction layer is credible:** The XR APIs are clean enough that an
   enterprise platform team can look at them and see how they'd extend to their
   requirements. The resource model makes intuitive sense.

4. **Modelplane feels like a real project:** Someone discovering Modelplane
   should see an open-source inference platform, not "just a Crossplane
   Configuration." That means its own identity (name, repo, docs, examples), a
   getting-started experience that doesn't require deep Crossplane knowledge,
   and enough polish that it's worth showing to people outside Upbound.

It's explicitly **not** a goal for v0.1 to:

- Support advanced policy (cost-based routing, advanced scheduling)
- Support non-LLM workloads (embeddings, speech-to-text, image generation)
- Support multiple cloud providers (GKE is the sole provider)
- Include LoRA adapter management

## Target personas

### Platform team (provider)

The platform team already operates Crossplane to manage infrastructure for the
wider engineering organization. They control which Kubernetes clusters exist,
what GPU hardware is available, and what policies govern resource usage. In the
Modelplane model, they:

- Install and configure Modelplane on their Crossplane control plane
- Define `InferenceEnvironment` resources that describe available inference
  targets and their capabilities
- Optionally curate a catalog of `ClusterModel` resources for approved
  open-weight models
- Set organizational defaults for engine configuration, resource limits, and
  security policies
- Monitor inference workloads across clusters

Their primary concern is operational: can they provide inference capacity
without becoming a bottleneck, while maintaining the guardrails their
organization requires?

### ML / application team (consumer)

The consumer team needs to run inference against open-weight models as part of
their product or research. They don't want to learn Kubernetes, Helm, KServe, or
Coreweave. In the Modelplane model, they:

- Create a `ModelDeployment` resource in their namespace specifying what model
  they want and optionally which environments to target
- Receive a unified OpenAI-compatible endpoint that routes across all target
  environments
- Optionally bring their own fine-tuned model weights via a `Model` resource
- Inspect per-environment `ModelPlacement` resources to debug issues on specific
  clusters

Their primary concern is velocity: how quickly can they go from "I need Llama 3
70B running" to "here's my endpoint"?

## Proposal

### API design

Modelplane defines six Crossplane composite resources (XRs). The API group is
`modelplane.ai`.

| CRD | Scope | Created by | Purpose |
|-----|-------|------------|---------|
| `InferenceGateway` | Cluster | Platform team | Control plane routing infrastructure |
| `InferenceEnvironment` | Cluster | Platform team | Inference environment with backend stack |
| `ClusterModel` | Cluster | Platform team | Model catalog |
| `Model` | Namespace | ML team | Private fine-tuned models |
| `ModelDeployment` | Namespace | ML team | Multi-environment deployment |
| `ModelPlacement` | Namespace | Typically composed by ModelDeployment | Per-environment deployment and routing |

`InferenceGateway`, `ClusterModel`, and `InferenceEnvironment` are
cluster-scoped (platform infrastructure). `Model`, `ModelDeployment`, and
`ModelPlacement` are namespace-scoped (team resources). This eliminates
cross-namespace references. Namespaced resources reference cluster-scoped
resources or resources in their own namespace.

### ClusterModel and Model

`ClusterModel` (cluster-scoped) and `Model` (namespace-scoped) share the same
schema. `ClusterModel` is for the platform team's curated catalog: approved
open-weight models available organization-wide. `Model` is for ML teams to
register their own fine-tuned models privately within their namespace.

#### ClusterModel (`clustermodels.modelplane.ai/v1alpha1`)

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ClusterModel
metadata:
  name: llama-3.1-70b-instruct-vllm  # No namespace — cluster-scoped
  labels:
    modelplane.ai/family: llama
    modelplane.ai/size: 70b
    modelplane.ai/engine: vLLM
spec:
  # Model identity
  model:
    name: meta-llama/Llama-3.1-70B-Instruct    # Model name passed to the serving engine

  # Where to download the model from (source and its config object are paired)
  source: HuggingFace                # HuggingFace | S3 | GCS | PVC
  huggingFace:
    repo: meta-llama/Llama-3.1-70B-Instruct
    revision: main                   # Git revision (branch, tag, commit)
    secretRef:                       # Optional: for gated models
      namespace: platform-system
      name: hf-token

  # Hardware requirements — what this model needs to run
  resources:
    gpu:
      type: nvidia.com/gpu           # GPU resource key
      count: 4                       # Minimum GPUs required
      memory: 80Gi                   # Per-GPU VRAM (informational, for scheduling)
    memory: "128Gi"                  # System memory (resource.Quantity string)
    cpu: "16"                        # CPU cores (resource.Quantity string, e.g. "16" or "500m")

  # Engine configuration — how this model must be served.
  engine: vLLM                       # vLLM | SGLang | ...
  vLLM:
    image: vllm/vllm-openai:latest
    maxModelLen: 32768               # Context window length
    prefixCaching: true              # KV cache prefix reuse
    gpuMemoryUtilization: 0.9        # Fraction of GPU VRAM to use (0.0–1.0)
    quantization: none               # none | awq | gptq | fp8
    parallelism:
      tensor: 4                      # Tensor parallelism across GPUs

  # Estimated model size on disk (used for cache PV provisioning)
  modelSize: 150Gi

status:
  conditions:
    - type: Ready                    # Summary condition: model is registered
      status: "True"
      reason: Available
      lastTransitionTime: "2026-03-03T10:00:00Z"
      observedGeneration: 1
```


Each inference engine (vLLM, SGLang, TensorRT-LLM) has its own configuration
knobs with engine-specific names and semantics. Engine config lives on
`ClusterModel` / `Model` because it makes each catalog entry a complete,
deployable unit. The platform team offers a model packaged for a use case:

- `llama-70b-fp16-interactive` (full precision, tuned for chatbot latency)
- `llama-70b-fp8-batch` (quantized, high throughput, cheaper GPUs)

The ML team picks from the catalog without needing to understand what the engine
flags mean. Advanced ML teams that want to tune engine flags for a model can do
so by creating a namespaced `Model`.


### InferenceGateway (`inferencegateways.modelplane.ai/v1alpha1`)

An `InferenceGateway` configures the control plane's routing infrastructure —
the gateway that sits between ML teams and inference environments. It's
cluster-scoped and singleton in practice (one gateway per control plane). It
parallels `InferenceEnvironment`: one is "where models run", the other is "how
you reach them."

The `backend` discriminator selects the gateway implementation. v0.1 supports
Envoy Gateway; future versions could add LiteLLM for intelligent routing
(cost-aware, performance-aware, model-name-aware). The nested `loadBalancer`
discriminator handles how the gateway gets an external address — MetalLB for
kind and bare-metal clusters, omitted for cloud environments where a native LB
controller is available.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceGateway
metadata:
  name: default
spec:
  backend: EnvoyGateway                  # EnvoyGateway | (LiteLLM in v0.2)
  envoyGateway:
    version: v1.3.0
    loadBalancer: MetalLB                # Optional — for kind/bare-metal
    metallb:
      addressPool: "172.18.255.200-172.18.255.250"
  gateway:
    port: 80

status:
  address: 34.56.129.3                   # External address of the gateway
```

The status contract is deliberately minimal: just `status.address`. No
gateway-specific fields like Gateway API names or namespaces — those are
implementation details of the Envoy Gateway backend. A LiteLLM backend would
surface the same `status.address` without any Gateway API concepts leaking
through.

ModelPlacements read the InferenceGateway to configure routing for their
environment. They compose backend-specific routing resources (e.g., Gateway API
HTTPRoutes for Envoy Gateway, or config entries for LiteLLM) so that the
gateway routes traffic to the remote cluster where the model runs.

### InferenceEnvironment (`inferenceenvironments.modelplane.ai/v1alpha1`)

An `InferenceEnvironment` represents a target where inference workloads can run.
It is cluster-scoped — InferenceEnvironments are infrastructure managed by
platform teams, not owned by any single namespace.

The `backend` discriminator is the top-level intent: "I want an inference
environment running KServe" (or Dynamo, or a managed inference API). Everything
else — including the cluster the backend runs on — is a property of the
backend's configuration. A KServe backend needs a Kubernetes cluster, so
`spec.kserve.cluster` carries a second discriminated union for cluster
provisioning (`source: GKE` paired with `gke: {...}`, or `source: Existing`
for bring-your-own). A hosted backend wouldn't have a `cluster` block at all
— just credentials and region config.

v0.1 assumes dedicated inference environments. The environment exists solely for
Modelplane workloads, with no shared scheduling or noisy-neighbor concerns. This
simplifies RBAC, resource accounting, and composition significantly.

The cloud-specific config is designed for progressive disclosure. The only
required GKE fields are `project` and `region` — if `nodePools` is omitted,
Modelplane provisions a default system pool (e2-standard-4, 2 nodes) and a
single GPU pool (g2-standard-4, 1x nvidia-l4). The resolved configuration — spec
with defaults filled in — is written to `status.resolved`.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceEnvironment
metadata:
  name: gpu-us-east                      # No namespace — cluster-scoped
  labels:
    modelplane.ai/tier: production
    modelplane.ai/region: us-east
spec:
  backend: KServe                        # KServe | (KubeAI in v0.2)
  kserve:
    version: v0.16.0

    # Cluster provisioning — not every backend needs a cluster. KServe does.
    # The source discriminator selects provisioning or bring-your-own.
    cluster:
      source: GKE                        # GKE | EKS | Existing
      gke:
        project: acme-ml-platform
        region: us-east1
        # nodePools is optional. If omitted, Modelplane provisions a default
        # system pool and a single L4 GPU pool. The resolved config (with
        # defaults filled in) appears in status.resolved.
        nodePools:
        - name: system
          machineType: e2-standard-4
          nodeCount: 2
        - name: gpu
          machineType: a3-highgpu-8g
          gpu:
            acceleratorType: nvidia-h100-80gb
            acceleratorCount: 8
          nodeCount: 2
          maxNodeCount: 8

  # Model caching — configure the LocalModelNodeGroup
  modelCache:
    enabled: true
    storageClass: local-nvme
    storageCapacity: 500Gi               # Per-node cache volume size

status:
  conditions:
    - type: Ready                        # Summary: environment is accepting deployments
      status: "True"
      reason: AllComponentsHealthy
    - type: ClusterReady                 # Underlying cluster is provisioned and healthy
      status: "True"
      reason: ClusterRunning
    - type: StackInstalled               # Backend and all dependencies are installed
      status: "True"
      reason: AllReleasesDeployed
    - type: GatewayReady                 # Envoy gateway is healthy and has an address
      status: "True"
      reason: AddressAssigned
    - type: ModelCacheReady              # LocalModelNodeGroup is operational
      status: "True"
      reason: NodeGroupCreated
  # Resolved configuration — the spec with defaults filled in. Same shape as
  # spec, same discriminated unions.
  resolved:
    backend: KServe
    kserve:
      version: v0.16.0
      cluster:
        provider: GKE
        gke:
          project: acme-ml-platform
          region: us-east1
          nodePools:
          - name: system
            machineType: e2-standard-4
            nodeCount: 2
          - name: gpu
            machineType: a3-highgpu-8g
            gpu:
              acceleratorType: nvidia-h100-80gb
              acceleratorCount: 8
            nodeCount: 2
            maxNodeCount: 8
  # Discovered capacity — populated by reading the provisioned cluster's
  # node/GPU info.
  capacity:
    gpuTypes:
      - type: nvidia.com/gpu
        model: H100
        vram: 80Gi
        available: 16
    nodeGroups:
      - name: gpu
  gateway:
    address: 10.0.1.50
```

Platform teams with existing Kubernetes clusters can bring their own
infrastructure instead of having Modelplane provision it. The `cluster` block
accepts `source: Existing` as an alternative to a cloud provider:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceEnvironment
metadata:
  name: gpu-us-east-byo
spec:
  backend: KServe
  kserve:
    version: v0.16.0
    cluster:
      source: Existing
      existing:
        secretRef:
          name: gpu-cluster-kubeconfig
          key: kubeconfig
  modelCache:
    enabled: true
    storageClass: local-nvme
    storageCapacity: 500Gi
```

Modelplane still installs and manages the backend stack (KServe, gateway, model
caching) on the cluster. It just doesn't provision the cluster itself. This
covers enterprise platform teams that manage clusters via Terraform, Cluster
API, or their own Crossplane Compositions and don't want Modelplane recreating
them.

Labels on InferenceEnvironment serve a dual purpose: informational metadata and
selection targets. ModelDeployments can target environments by label selector
(e.g., `modelplane.ai/tier: production`), which is how multi-environment
deployment works without hard-coded environment references.

### ModelDeployment (`modeldeployments.modelplane.ai/v1alpha1`)

A `ModelDeployment` is the primary consumer-facing API. ML teams create one to
deploy a model across one or more InferenceEnvironments. Modelplane creates a
`ModelPlacement` for each matched environment and aggregates their status.

ModelDeployment is deliberately backend-agnostic. It handles environment
discovery, scheduling, and fan-out. It does not compose routing resources —
that responsibility belongs to ModelPlacement, which knows the backend type.

The simplest possible deployment:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: llama-70b-production
  namespace: ml-team-a
spec:
  # What model to deploy
  modelRef:
    kind: ClusterModel          # ClusterModel | Model
    name: llama-3.1-70b-instruct-vllm

  # How many environments to deploy to (required)
  environments: 1

status:
  conditions:
    - type: Ready                    # Summary: at least one placement is serving traffic
      status: "True"
      reason: PlacementsAvailable
  endpoint:
    url: https://llama-70b-production.inference.example.com
  placements:
    total: 1
    ready: 1
  model:
    name: meta-llama/Llama-3.1-70B-Instruct
```

Modelplane matches model requirements (GPU count, VRAM from Model
`spec.resources`) and engine compatibility (Model engine vs.
InferenceEnvironment backend) against available environments.

For power users who need to target specific environments, `environmentSelector`
is an optional escape hatch:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: llama-70b-global
  namespace: ml-team-a
spec:
  modelRef:
    kind: ClusterModel
    name: llama-3.1-70b-instruct-vllm

  # Deploy to 2 of the matching environments
  environments: 2

  # Optional: target specific environments by label
  environmentSelector:
    matchLabels:
      modelplane.ai/tier: production

status:
  conditions:
    - type: Ready
      status: "True"
      reason: PlacementsAvailable
  endpoint:
    url: https://llama-70b-global.inference.example.com
  placements:
    total: 2
    ready: 2
  model:
    name: meta-llama/Llama-3.1-70B-Instruct
```

If a new InferenceEnvironment appears that matches the selector (or model
requirements, when no selector is specified), Modelplane automatically creates a
ModelPlacement for it.

The unified endpoint is the InferenceGateway's address. Traffic is routed by
model name — the `model` field in the OpenAI request body — not by URL path.
This means the endpoint is the same for every deployment:
`http://<gateway-address>/v1/chat/completions`. Each ModelPlacement registers
its environment with the routing layer, and the gateway load-balances across
placements serving the same model.

Individual placement endpoints are available on the ModelPlacement resources
for debugging, but the intended production pattern is to always go through
the unified gateway endpoint.

### ModelPlacement (`modelplacements.modelplane.ai/v1alpha1`)

A `ModelPlacement` is the resource that actually deploys a model and registers
it with the routing layer. When Modelplane creates a ModelPlacement, it
composes the backend-specific resources that run the model on a specific
InferenceEnvironment — for a KServe backend, that means creating an
`LLMInferenceService` on the target cluster. It also reads the
InferenceGateway and composes routing resources so the gateway can reach the
model — for an Envoy Gateway backend, that means a Backend and HTTPRoute on
the control plane.

ModelPlacement is the only function that knows about specific backends. It's
where backend-specific model serving and routing logic lives. Adding a new
backend means updating this one function. An InferenceEnvironment provisions
infrastructure and installs the backend stack, but no models are running until
a ModelPlacement targets it. A ClusterModel describes how a model should be
served, but it's inert until referenced by a ModelPlacement.

ModelDeployments create ModelPlacements automatically — one per matched
InferenceEnvironment. ML teams aren't intended to create them directly, but
nothing stops them from doing so.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelPlacement
metadata:
  name: llama-70b-global-us-east      # Generated by ModelDeployment composition
  namespace: ml-team-a
  labels:
    modelplane.ai/deployment: llama-70b-global
spec:
  # What to deploy
  modelRef:
    kind: ClusterModel
    name: llama-3.1-70b-instruct-vllm

  # Where to deploy it
  inferenceEnvironmentRef:
    name: gpu-cluster-us-east

status:
  conditions:
    - type: Ready                    # Model is serving traffic on this environment
      status: "True"
      reason: Available
    - type: Compatible               # Model engine is supported by this backend
      status: "True"
      reason: BackendSupportsEngine
    - type: ModelCached              # Weights were found in cache at startup
      status: "True"
      reason: CacheAvailable
    - type: EndpointAvailable        # Per-environment endpoint is reachable
      status: "True"
      reason: GatewayRouteConfigured
  resolvedEngine:
    engine: vLLM
    vLLM:
      image: vllm/vllm-openai:v0.16.0
      maxModelLen: 32768
      prefixCaching: true
      gpuMemoryUtilization: 0.9
      parallelism:
        tensor: 4
  model:
    name: meta-llama/Llama-3.1-70B-Instruct
  endpoint:
    internalURL: http://llama-70b-global.ml-team-a.svc.cluster.local:8000
```

```
$ kubectl get modelplacements -n ml-team-a
NAME                        ENVIRONMENT          READY
llama-70b-global-us-east    gpu-cluster-us-east  True
llama-70b-global-us-west    gpu-cluster-us-west  True
```

ModelPlacement's spec is just `modelRef` and `inferenceEnvironmentRef`. Engine
config comes from the Model, scaling is determined by the backend of the
`InferenceEnvironment`. The resolved engine config — the Model's engine settings
as actually applied to the backend — lives in `status.resolvedEngine`. It's
computed output, not user intent.

### Composition architecture

Each XRD has a corresponding Composition powered by a Python composition
function. 

The following diagram shows how the six public resources relate to each other
and to the internal XRs that Modelplane composes under the hood:

```mermaid
flowchart TD
    subgraph platform["Platform team creates"]
        IG["InferenceGateway"]
        CM["ClusterModel\n<i>per model + engine</i>"]
        IE1["InferenceEnvironment A"]
        IE2["InferenceEnvironment B"]
    end

    subgraph ml["ML team creates"]
        MD["ModelDeployment"]
    end

    subgraph composed["Modelplane composes"]
        MP1["ModelPlacement A"]
        MP2["ModelPlacement B"]
        GKE1["GKECluster A"]
        GKE2["GKECluster B"]
        KS1["KServeStack A"]
        KS2["KServeStack B"]
    end

    MD -- "modelRef" --> CM
    MD --> MP1
    MD --> MP2
    MP1 --> IE1
    MP1 -. "routing" .-> IG
    MP2 --> IE2
    MP2 -. "routing" .-> IG
    IE1 --> GKE1
    IE1 --> KS1
    IE2 --> GKE2
    IE2 --> KS2
    MP1 -. "LLMInferenceService" .-> GKE1
    MP2 -. "LLMInferenceService" .-> GKE2
```

Five composition functions, one per concern:

| Function | Responsibility |
|----------|---------------|
| `function-modelplane-gateway` | Composes the control plane routing infrastructure. Dispatches on gateway backend (Envoy Gateway, LiteLLM). Surfaces `status.address` for ModelPlacements. |
| `function-modelplane-env` | Dispatches on inference backend and cloud provider discriminators. Composes `GKECluster` and `KServeStack` XRs, wires them together, populates `status.capacity`. Adding a new backend or cloud provider means adding a branch here. |
| `function-modelplane-model` | Validates model catalog entries for both `ClusterModel` and `Model`. Registration and validation only — caching is an environment concern. |
| `function-modelplane-deploy` | Backend-agnostic fan-out from ModelDeployment to ModelPlacements. Resolves target environments (by selector or automatic matching), stamps placements, aggregates status. Does not compose routing resources. |
| `function-modelplane-placement` | The only function that knows about specific backends. Reads the referenced Model, InferenceEnvironment, and InferenceGateway. Composes backend-specific model serving resources (LLMInferenceService for KServe) and routing resources (HTTPRoute + Backend for Envoy Gateway). Adding a new inference or routing backend means updating this one function. |

The `GKECluster` and `KServeStack` XRs are internal implementation details —
they have their own XRDs and composition functions but are not part of
Modelplane's public API. They provide clean boundaries: the env function
delegates to specialist XRs and wires them together.

The composition functions rely on Crossplane v2's **required resources**
mechanism to read across XR boundaries. `function-modelplane-deploy` requests
InferenceEnvironments for fan-out. `function-modelplane-placement` requests the
referenced Model, InferenceEnvironment, and InferenceGateway for backend
composition and routing.

## Alternatives considered

### Engine and ClusterEngine CRs

I considered separate Engine and ClusterEngine CRs that would separate model
identity from serving configuration. The ML team's ModelDeployment would
reference both a model and an engine profile. This has real strengths:

- The platform team maintains models and engine profiles as independent catalogs
  (N models + M engine profiles instead of N×M ClusterModels)
- Engine config updates happen in one place rather than per ClusterModel

I decided to keep engine config on ClusterModel/Model. The bundled approach
makes each catalog entry a complete, tested, deployable unit. The platform team
blesses specific model+engine combinations rather than letting any model pair
with any engine profile and discovering incompatibilities at placement time. It
keeps the consumer API minimal (one ref, not two) and the information model
simpler (five CRDs, not seven).

### Namespace-as-environment

Using Kubernetes namespaces as the environment boundary has appeal (clean RBAC,
GitOps-friendly) but conflates the organizational boundary (teams and stages)
with the infrastructure boundary (GPU clusters). In practice, one GPU cluster
serves multiple teams. Cluster-scoped InferenceEnvironments shared by namespaced
ModelDeployments matches how enterprise platform teams actually manage shared
infrastructure.

### Custom InferenceEnvironment Compositions

I considered letting platform teams provide their own Compositions for
InferenceEnvironment (or for the internal GKECluster and KServeStack XRs) as
the customization mechanism for bring-your-own infrastructure. This is how
Crossplane customization normally works — the XRD is the contract, the
Composition is swappable.

The problem is that Modelplane's composition functions cross resource
boundaries. The deploy function reads InferenceEnvironments to decide where to
place models. The placement function reads the InferenceEnvironment to figure
out how to deploy a model on its backend. A custom InferenceEnvironment
Composition doesn't help if the platform team wants an unsupported backend —
they'd also need a custom placement function, and potentially a custom deploy
function. That's too much surface area to customize from outside the project.

The bring-your-own-cluster path (`source: Existing` with a kubeconfig Secret)
covers the realistic customization need: the platform team controls cluster
provisioning, Modelplane controls the backend stack and model deployment.
Adding new backends (Dynamo, KubeAI) is a contribution to Modelplane, not a
per-platform-team customization.

## Future work

Capabilities deferred from v0.1, ordered roughly by how soon I think they'll be
needed.

**Immutable deployments and propagation control.** v0.1 follows Crossplane's
continuous reconciliation. Changes to Model config propagate to running
placements automatically. 

**Intelligent routing.** v0.1 uses Envoy Gateway for basic model-name routing
across environments. Future versions should support cost-aware and
performance-aware routing — selecting backends based on GPU pricing, latency,
throughput, and queue depth. LiteLLM is a natural candidate for the
InferenceGateway backend here: it supports load balancing across
OpenAI-compatible endpoints with cost tracking, rate limiting, and fallbacks.
The InferenceGateway API's `backend` discriminator is designed for this
evolution — adding `backend: LiteLLM` would mean ModelPlacement composes
LiteLLM config entries instead of Gateway API HTTPRoutes. The routing
concern stays in ModelPlacement either way.

**Policy.** Typed policy resources (PlacementPolicy, ResourcePolicy,
RoutingPolicy, ModelPolicy, etc.). These could be namespace-scoped, one per
namespace. . The namespace is the enforcement boundary, following the LimitRange
/ PodSecurity pattern.

**`modelplane` CLI.** `modelplane deploy`, `modelplane status`, `modelplane
logs`. Out of scope for v0.1 where `kubectl` suffices.

**LoRA adapter orchestration.** Dynamic adapter loading, routing, and lifecycle
management.

**Canary deployments.** Progressive rollout with traffic splitting. Depends on
immutable deployments and the Gateway API Inference Extension.

**Cost estimation and observability.** Estimated GPU cost per ModelDeployment,
Prometheus/Grafana dashboards for inference metrics.

## Open questions

**Autoscaling complexity:** KEDA + Prometheus + vLLM custom metrics is a lot of
machinery for the placement function to compose. Should v0.1 ship with simpler
backend-managed scaling (fixed replicas + HPA on a basic metric) and defer KEDA
integration to v0.2?

**InferenceEnvironment lifecycle coupling:** If a platform team deletes an
InferenceEnvironment, what happens to ModelPlacements targeting it?
ModelPlacements are owned by ModelDeployments, not InferenceEnvironments. The
deploy function would need to detect the missing environment and remove the
orphaned placement. The placement function would need to handle the unresolvable
ref with a clear status condition rather than failing silently.

**Inferring model resource requirements:** `ClusterModel` and `Model` currently
require the platform team or ML team to specify `spec.resources` (GPU count,
memory, CPU) and `modelSize` manually. In principle these could be inferred from
the model source — HuggingFace model cards include parameter counts and
safetensors headers include tensor shapes and dtypes, which is enough to
estimate VRAM requirements and disk size. OME does this via automatic parsing of
safetensors headers. The question is whether v0.1 should require explicit
resource specs (simpler, no source-specific logic in the model function) or
infer them when possible and let the spec override.

**Environment capacity and scheduling for BYO clusters:** When Modelplane
provisions a cluster, it knows the GPU capacity from the node pool config. For
bring-your-own clusters, capacity is unknown — and even for provisioned
clusters, node autoscaling means declared capacity may not reflect reality. The
deploy function currently uses capacity to match models to compatible
environments. For BYO clusters this information isn't available. One option is
to make capacity purely informational and have the deploy function only check
engine/backend compatibility, letting the actual scheduling happen at the
Kubernetes level when the ModelPlacement composes an LLMInferenceService. If the
cluster can't schedule the pods, the placement reports that via status
conditions rather than being rejected upfront.
