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

Modelplane also expects the cluster to provide a recent Kubernetes version and
GPUs exposed through Dynamic Resource Allocation. On a provisioned cluster
Modelplane meets these requirements for you. On an existing cluster you meet them
yourself, as [Requirements for an existing
cluster](#requirements-for-an-existing-cluster) sets out.

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
  [Modelplane's requirements](#requirements-for-an-existing-cluster).

## Requirements for an existing cluster

An existing cluster must meet what Modelplane would otherwise set up for you:

- **Kubernetes 1.34.2 or newer.** Modelplane binds GPUs with the generally
  available Dynamic Resource Allocation API (`resource.k8s.io/v1`), first served
  in Kubernetes 1.34, and the DRA driver it installs wants 1.34.2. Prefer a
  [maintained release](https://kubernetes.io/releases/).
- **GPU nodes that already run the NVIDIA driver.** Modelplane installs NVIDIA's
  DRA driver, but not the GPU driver itself. Its
  [prerequisites](https://dra-driver-nvidia-gpu.sigs.k8s.io/docs/prerequisites/)
  call for NVIDIA GPU driver v565 or newer and NVIDIA Container Toolkit v1.18.0
  or newer, which provides the CDI the driver uses to expose GPUs to the
  container runtime. A provisioned cluster's node image includes this.
- **A `modelplane.ai/pool=<pool-name>` label on each pool's nodes**, matching the
  pool's `name`. Modelplane provisions no nodes here, so you apply the label
  yourself. The [scheduler pins each pool's pods to
  it]({{< ref "/architecture/scheduling.md#pinning-placement-to-a-pool" >}}), so
  worker pods stay Pending without it.
- **The `nvidia.com/gpu` taint key, if you taint GPU nodes.** Modelplane's GPU
  workloads tolerate that key. A different taint keeps them off the nodes.
- **A load balancer.** Modelplane exposes the cluster's serving gateway through a
  `LoadBalancer` Service, so the cluster needs one that assigns it an external
  address.
- **No conflicting Gateway controller.** Modelplane installs Envoy Gateway and
  owns its `GatewayClass`. Don't run another controller claiming the same class.
- **A `ReadWriteMany` StorageClass**, if you use a `ModelCache`. See
  [Cache storage](#cache-storage).
- **Any multi-node fabric you need.** For multi-node serving you provide and
  configure the RDMA or InfiniBand fabric and its drivers. Modelplane installs
  those only on the clouds it provisions.

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
