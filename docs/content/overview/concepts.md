---
title: Concepts
weight: 30
description: Key resources and how they relate.
aliases:
  - /concepts/
---
<!-- vale write-good.Passive = NO -->
Modelplane manages AI model inference across a fleet of GPU clusters. It draws a
boundary between two teams: [platform engineers]({{< ref "/platform/_index.md" >}})
who provision infrastructure and define hardware classes, and
[ML teams]({{< ref "/models/_index.md" >}}) who deploy models and get unified
endpoints.

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
        MC[ModelCache<br><i>kimi-k2</i>]
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
    MD -- "references" --> MC
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

## Glossary

**[InferenceGateway]({{< ref "/platform/inference-gateway.md" >}})**
The unified, OpenAI-compatible endpoint on the control plane cluster. Installs
Envoy Gateway and routes requests to model endpoints on remote inference clusters.
One per control plane, always named `default`.

**[InferenceClass]({{< ref "/platform/inference-class.md" >}})**
A hardware recipe for a GPU node pool. Includes GPU type, count, DRA device definitions,
and optional cloud provisioning config. Platform teams define one class per GPU
SKU and cloud combination.

**[InferenceCluster]({{< ref "/platform/inference-cluster.md" >}})**
A Kubernetes cluster registered with Modelplane for model serving. Can be
provisioned by Modelplane (GKE, EKS) or brought as-is (`Existing`). Modelplane
installs the inference stack on every registered cluster.

**[ModelDeployment]({{< ref "/models/model-deployment.md" >}})**
The ML team's primary resource. Declares the inference engines, replica count,
and optional model cache. The scheduler places each replica onto a ready cluster
with matching GPU capacity.

**[ModelCache]({{< ref "/models/model-cache.md" >}})**
Stages model weights on cluster storage ahead of serving. Composes a
ReadWriteMany PVC per cluster and hydrates it once from the configured source
(HuggingFace today). Required for multi-node deployments; optional for
single-node cold-start optimization.

**[ModelReplica]({{< ref "/models/model-replica.md" >}})**
One instance of a `ModelDeployment` placed on a specific cluster. Created
automatically by Modelplane. Don't create these directly.

**[ModelEndpoint]({{< ref "/models/model-endpoint.md" >}})**
A reachable inference endpoint. Modelplane composes one per `ModelReplica`.
ML teams can also create them manually to point a `ModelService` at an external
provider (Together, BaseTen).

**[ModelService]({{< ref "/models/model-service.md" >}})**
Exposes one or more `ModelEndpoints` as a single, OpenAI-compatible URL.
Selects endpoints by label and composes a Gateway API HTTPRoute that
load-balances across them.
<!-- vale write-good.Passive = YES -->
