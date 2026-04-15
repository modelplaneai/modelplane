# Concepts

Modelplane manages AI model inference as declarative infrastructure. It draws a
boundary between two teams: platform teams who provision infrastructure and
curate a model catalog, and ML teams who deploy from that catalog.

This page explains the key resources and how they relate.

## Resource model

```mermaid
graph TD
    subgraph "Platform team"
        IG[InferenceGateway]
        IE1[InferenceEnvironment<br><i>gke-us-central</i>]
        IE2[InferenceEnvironment<br><i>byo-us-east</i>]
        CM[ClusterModel<br><i>qwen-2-5-0-5b</i>]
    end

    subgraph "ML team"
        MD[ModelDeployment<br><i>qwen-demo</i>]
    end

    subgraph "Created by Modelplane"
        MP1[ModelPlacement<br><i>qwen-demo-gke-us-central</i>]
        MP2[ModelPlacement<br><i>qwen-demo-byo-us-east</i>]
    end

    MD -- "references" --> CM
    MD -- "scheduler selects" --> IE1
    MD -- "scheduler selects" --> IE2
    MD -. "creates" .-> MP1
    MD -. "creates" .-> MP2
    MP1 -- "deploys to" --> IE1
    MP2 -- "deploys to" --> IE2
    IG -- "routes traffic to" --> MP1
    IG -- "routes traffic to" --> MP2
```

## InferenceGateway

The InferenceGateway creates a unified, OpenAI-compatible endpoint on the
control plane cluster. It installs [Envoy
Gateway](https://gateway.envoyproxy.io) and creates a Gateway that routes
requests to model placements on remote inference environments.

Create one InferenceGateway per control plane. It must be named `default`. When
running the control plane in kind, set `loadBalancer: MetalLB` to get a
LoadBalancer IP inside the Docker network.

Once ready, the gateway's external address is available in the resource's
status:

```bash
kubectl get ig default
```

## InferenceEnvironment

An InferenceEnvironment represents a Kubernetes cluster with an inference
backend installed. Platform teams create these to provide GPU capacity for model
serving.

Each environment has:

- A **cluster source**: `GKE` (Modelplane provisions the full cluster) or
  `Existing` (bring a cluster you manage yourself).
- An **inference backend**: `KServe` or `Dynamo`. Modelplane installs the
  backend and its dependencies (cert-manager, Envoy Gateway, Prometheus, KEDA)
  on the cluster.
- One or more **GPU node pools** describing the available accelerators.

The environment's GPU capacity is used by the scheduler when placing models. For
`GKE` clusters, the capacity is computed from the node pool configuration. For
`Existing` clusters, you describe the node pools so the scheduler knows what's
available.

Environments must have the label `modelplane.ai/environment: "true"` to be
discoverable by the scheduler.

## ClusterModel and Model

A ClusterModel (cluster-scoped) or Model (namespaced) registers a model in the
platform catalog. It describes:

- Where to download weights from (currently HuggingFace).
- How much VRAM the model needs.
- One or more **serving profiles**, each specifying a backend (KServe or Dynamo)
  and an engine (vLLM or SGLang) with a container image.

Serving profiles are listed in priority order. When the scheduler places a model
on an environment, it walks the list and picks the first profile whose backend
matches the environment's backend.

ML teams don't need to know about serving profiles. They reference a catalog
model by name and the platform decides how to serve it.

## ModelDeployment

A ModelDeployment is the ML team's interface. It says "deploy this model to N
environments" and produces a working endpoint.

When a ModelDeployment is created, the scheduler:

1. Discovers all InferenceEnvironments with the `modelplane.ai/environment`
   label.
2. Applies any `environmentSelector` label filter from the deployment.
3. Matches the model's serving profiles against each environment's backend.
4. Checks GPU capacity (model VRAM vs available pool VRAM, minus other
   placements).
5. Creates a ModelPlacement for each selected environment.
6. Creates an HTTPRoute on the control plane gateway to route traffic to the
   placements.

The deployment's endpoint URL follows this pattern:

``` http://<gateway-address>/<namespace>/<deployment-name>/v1/chat/completions
```

### Scaling

ModelDeployments support two scaling modes:

- **Fixed**: a static number of replicas per placement.
- **Concurrency**: autoscaling based on active concurrent requests per replica,
  using KEDA and Prometheus. Supports scale-to-zero when `minReplicas` is 0.

The default is fixed scaling with 1 replica.

## ModelPlacement

A ModelPlacement is created by the ModelDeployment's composition function. Users
don't create these directly.

Each placement represents a model deployed to a specific environment. It
resolves the serving profile, computes how many GPUs the model needs, and
creates the backend-specific resources:

- **KServe**: an `LLMInferenceService` on the remote cluster.
- **Dynamo**: a `DynamoGraphDeployment` on the remote cluster.

The placement also creates an Envoy Gateway `Backend` on the control plane to
route traffic from the gateway to the remote cluster's inference endpoint.
