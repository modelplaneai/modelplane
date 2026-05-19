# Concepts

Modelplane manages AI model inference across a fleet of GPU clusters. It draws a
boundary between two teams: platform teams who provision infrastructure and
define hardware classes, and ML teams who deploy models and get unified
endpoints.

This page explains the key resources and how they relate.

## Resource model

```mermaid
graph TD
    subgraph "Platform team"
        IG[InferenceGateway]
        ICL[InferenceClass<br><i>gke-l4-1x-g2</i>]
        IC1[InferenceCluster<br><i>prod-gke-us-central</i>]
        IC2[InferenceCluster<br><i>prod-byo-us-east</i>]
    end

    subgraph "ML team"
        MD[ModelDeployment<br><i>qwen-demo</i>]
        MS[ModelService<br><i>qwen</i>]
    end

    subgraph "Created by Modelplane"
        MR1[ModelReplica<br><i>qwen-demo-prod-gke-us-central</i>]
        MR2[ModelReplica<br><i>qwen-demo-prod-byo-us-east</i>]
        ME1[ModelEndpoint<br><i>qwen-demo-prod-gke-us-central</i>]
        ME2[ModelEndpoint<br><i>qwen-demo-prod-byo-us-east</i>]
    end

    IC1 -- "references" --> ICL
    MD -. "creates" .-> MR1
    MD -. "creates" .-> MR2
    MD -. "creates" .-> ME1
    MD -. "creates" .-> ME2
    MR1 -- "deploys to" --> IC1
    MR2 -- "deploys to" --> IC2
    MS -- "selects" --> ME1
    MS -- "selects" --> ME2
    MS -. "routing" .-> IG
```

## InferenceGateway

The InferenceGateway creates a unified, OpenAI-compatible endpoint on the
control plane cluster. It installs [Envoy
Gateway](https://gateway.envoyproxy.io) and creates a Gateway that routes
requests to model endpoints on remote inference clusters.

Create one InferenceGateway per control plane. It must be named `default`. When
running the control plane in kind, set `loadBalancer: MetalLB` to get a
LoadBalancer IP inside the Docker network.

Once ready, the gateway's external address is available in the resource's
status:

```bash
kubectl get ig default
```

## InferenceClass

An InferenceClass is a tested recipe for a GPU node pool. It bundles:

- **Resources**: what hardware the class exposes (GPU count, memory). Used by
  the scheduler to match deployments to clusters.
- **Provisioning** (optional): how to create a node pool of this class on a
  specific cloud. Classes without provisioning are for existing clusters where
  the pool already exists.

Different clouds and GPU types imply different classes. A GKE L4 pool is
`gke-l4-1x-g2`. A bare-metal H100 pool is `h100-8x-ib` (no provisioning).

## InferenceCluster

An InferenceCluster represents a Kubernetes cluster configured for model
serving. Platform teams create these to provide GPU capacity.

Each cluster has:

- A **cluster source**: `GKE` (Modelplane provisions the full cluster) or
  `Existing` (bring a cluster you manage yourself).
- One or more **node pools**, each referencing an `InferenceClass` for its
  hardware capabilities and provisioning recipe.
- **Labels** for organizational metadata: tier, region, provider. These are the
  matching surface for `ModelDeployment.clusterSelector`.

Modelplane installs an inference stack (including cert-manager, Envoy Gateway,
KServe, Prometheus, and KEDA) on every cluster it manages. For provisioned
clusters Modelplane handles this directly. For existing clusters the platform
team is responsible for meeting the cluster prerequisites.

The cluster's GPU capacity is derived from its node pools' InferenceClasses and
used by the scheduler when placing models. A cluster must be `Ready` and have a
gateway address to be eligible for scheduling.

## ModelDeployment

A ModelDeployment is the ML team's interface. It carries everything needed to
deploy a model to the fleet: the worker template, hardware topology, and replica
count.

When a ModelDeployment is created, the scheduler:

1. Discovers all ready InferenceClusters (filtered by `clusterSelector` labels
   if set).
2. Derives the physical shape from `workers.topology`: GPUs per node (tensor)
   and nodes per worker (pipeline, default 1).
3. Checks GPU capacity: does the cluster have a pool with enough GPUs per node
   and enough available nodes?
4. Creates a `ModelReplica` for each selected cluster.
5. Creates a `ModelEndpoint` for each replica, carrying the URL and rewrite path
   for routing.

The worker template is a curated subset of `PodTemplateSpec`. The container
named `engine` is the inference engine; additional containers pass through as
sidecars. A CEL validation rule on the XRD enforces that exactly one container
named `engine` exists.

### Scaling

Replicas are the only scaling axis. Each `ModelReplica` is a complete,
fixed-topology serving instance. Scaling `spec.replicas` adds or removes whole
instances. There's no in-cluster pod autoscaling.

The `ModelDeployment` XRD declares a Kubernetes scale subresource. Autoscaling
is opt-in via a separate KEDA `ScaledObject` or similar, following the same
pattern as Kubernetes Deployment + HPA.

## ModelReplica

A ModelReplica is created by the ModelDeployment's composition function. Users
don't create these directly.

Each replica represents a model deployed to a specific cluster. It reads the
worker template and topology, finds the engine container, and composes a KServe
`LLMInferenceService` on the remote cluster. For multi-node serving (pipeline >
1), it uses LeaderWorkerSet via KServe.

## ModelEndpoint

A ModelEndpoint is a reachable inference endpoint. Modelplane composes one per
ModelReplica, but ML teams can also create them manually for external SaaS
providers (Together, BaseTen).

Each endpoint composes an Envoy Gateway `Backend` on the control plane. The
Backend's name is surfaced in `status.routing.backendName` so that ModelService
can reference it in its HTTPRoute.

## ModelService

A ModelService exposes one or more ModelEndpoints via a unified, OpenAI-
compatible endpoint. It selects endpoints by label and composes a Gateway API
`HTTPRoute` that load-balances across them.

Each backendRef in the HTTPRoute carries its own `URLRewrite` filter derived
from the endpoint's `spec.rewritePath`, so endpoints from different deployments
or external providers with different path layouts coexist correctly.

The service's public address is available in `status.address`:

```bash
kubectl get ms qwen -n ml-team -o jsonpath='{.status.address}'
```
