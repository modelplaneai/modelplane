---
title: Register a Cluster
weight: 30
description: A Kubernetes cluster registered with Modelplane for model serving.
---
**API:** [`modelplane.ai/v1alpha1` · InferenceCluster]({{< ref "/reference/inferenceclusters" >}})
<!-- vale write-good.Passive = NO -->
An `InferenceCluster` represents a Kubernetes cluster configured for model
serving. Platform teams create these to provide GPU capacity.


Each cluster has:

- A **cluster source**: `GKE`, `EKS`, `AKS`, `Nebius` or `Vultr` (Modelplane provisions
  the full cluster) or `Existing` (bring a cluster you manage yourself). See
  [Supported Providers]({{< ref "platform/providers.md" >}}) for the clouds and
  neoclouds Modelplane runs on.
- One or more **node pools**, each referencing an `InferenceClass` for its
  hardware capabilities and provisioning recipe.
- **Labels** for organizational metadata: tier, region, provider. These are the
  matching surface for `ModelDeployment.clusterSelector`.

Modelplane installs a serving stack on every cluster it manages, including
existing clusters, which it assumes are solely for its use.

## Ownership and requirements

Modelplane assumes exclusive ownership of every `InferenceCluster`. The fleet
scheduler's capacity accounting relies on Modelplane being the only thing placing
GPU workloads on the cluster, so dedicate each cluster to Modelplane rather than
sharing it with other workloads.

Modelplane also has opinions about how a cluster is set up: its Kubernetes
version, the components it installs, and required features like DRA for binding
GPUs to pods. On provisioned clusters Modelplane handles this for you. On an
existing cluster the platform team must meet the requirements.

## Provisioned and existing clusters

The `cluster.source` discriminator picks one of two models:

- **Provisioned (`GKE`, `EKS`, `AKS`, `Nebius`, `Vultr`).** Modelplane creates the cluster and its GPU node
  pools from each pool's `InferenceClass`, labels the pool's nodes so the
  scheduler's placement is enforced, and provisions the storage class for model
  weights. It also injects a non-GPU **system pool** with opinionated defaults to
  run the inference stack, so you only declare the GPU pools you want.
- **Existing (`Existing`).** A kubeconfig `Secret` provides access to a cluster
  you run yourself. Modelplane installs the serving stack it needs but doesn't
  provision infrastructure, and each pool's `InferenceClass` provides hardware
  capabilities for scheduling only. You're responsible for the cluster meeting
  Modelplane's requirements, including labeling each pool's nodes
  `modelplane.ai/pool=<pool-name>` (see
  [how scheduling pins placement]({{< ref "/architecture/scheduling.md#pinning-placement-to-a-pool" >}})).

## Serving stack

`spec.stack` selects the serving layer the cluster runs: `Standard` (the default)
or `Dynamo`. The field is immutable, so recreate the cluster to change it.

Both stacks compose a single-node engine the same way, as a Deployment, and front
it the same way, with Gateway API and an endpoint picker. They differ in how they
run a multi-node gang and distribute its weights:

- **Standard** composes a gang as a LeaderWorkerSet. It has no gang scheduler, so
  the pods schedule independently.
- **Dynamo** installs NVIDIA's [Grove](https://github.com/ai-dynamo/grove) and the
  [KAI Scheduler](https://github.com/NVIDIA/KAI-Scheduler) in place of the
  LeaderWorkerSet controller, and composes a gang as a Grove `PodCliqueSet` that
  they gang-schedule all-or-nothing and topology-aware. It also runs a
  [ModelExpress](https://github.com/ai-dynamo/modelexpress) server that moves
  weights between replicas over the fabric, so a later replica pulls a model from
  a peer's GPU rather than reading storage again.

A `ModelDeployment` looks the same on either stack. On `Dynamo` an engine can
opt into peer-to-peer weight loading with `--load-format modelexpress`. An
engine that does this will work on `Standard` too, but won't load weights
peer-to-peer. See
[model caching]({{< ref "/models/model-cache.md#accelerating-with-modelexpress" >}})
for more details.

{{< hint "note" >}}
A multi-node gang on `Dynamo` derives its node rank from Grove's
`GROVE_PCLQ_POD_INDEX`, because Modelplane doesn't inject `$(MODELPLANE_RANK)`
there yet. This is a temporary gap, not by design:
[#418](https://github.com/modelplaneai/modelplane/issues/418) tracks closing it,
so a gang command reads the same on both stacks.
[Multi-node deployments]({{< ref "/models/model-deployment.md#multi-node" >}}) show
the rank a gang computes until then.
{{< /hint >}}

Composing a full Dynamo graph deployment, for its frontend and request routing,
is planned.

## Examples

{{< tabs >}}
{{< tab "GKE" >}}
{{< manifests path="concepts/inference-cluster-gke.yaml" apply="false" >}}
{{< /tab >}}
{{< tab "EKS" >}}
{{< manifests path="concepts/inference-cluster-eks.yaml" apply="false" >}}
{{< /tab >}}
{{< tab "AKS" >}}
{{< manifests path="concepts/inference-cluster-aks.yaml" apply="false" >}}
{{< /tab >}}
{{< tab "Nebius" >}}
{{< manifests path="concepts/inference-cluster-nebius.yaml" apply="false" >}}
{{< /tab >}}
{{< tab "Vultr" >}}
{{< manifests path="concepts/inference-cluster-vultr.yaml" apply="false" >}}
{{< /tab >}}
{{< tab "Existing" >}}
{{< manifests path="concepts/inference-cluster-existing.yaml" apply="false" >}}
{{< /tab >}}
{{< /tabs >}}

## Cache storage

A [ModelCache]({{< ref "/models/model-cache.md" >}}) stages model weights on a
`ReadWriteMany` (RWX) StorageClass on the workload cluster. Where that comes from
depends on the source:

<!-- vale Google.Acronyms = NO -->
- **`GKE`** (Filestore Enterprise), **`EKS`** (EFS), **`AKS`** (Azure Files),
  and **`Nebius`** (shared filesystem): auto-provisioned. Those classes are
  fixed; nothing for the admin to do.
- **`Vultr`**: none. VKE's built-in RWX class (Vultr File System) isn't
  usable on GPU nodes, and Modelplane doesn't provision an alternative.
  Deploy single-node engines without a `ModelCache` and let them pull
  weights directly from the model source.
- **`Existing`**: bring your own. Create an RWX StorageClass on the cluster, with
  any backend that supports automatic PVC provisioning (WekaIO, NetApp Trident,
  `FSx` for NetApp, and similar), and name it in
  `cluster.existing.cache.storageClassName`.
<!-- vale Google.Acronyms = YES -->

The ML team's `ModelCache` and `ModelDeployment` specs are the same regardless of
which backing storage a cluster uses.
<!-- vale write-good.Passive = YES -->
