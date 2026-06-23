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

- A **cluster source**: `GKE` or `EKS` (Modelplane provisions the full cluster)
  or `Existing` (bring a cluster you manage yourself). See
  [Supported Providers]({{< ref "platform/providers.md" >}}) for the clouds and
  neoclouds Modelplane runs on.
- One or more **node pools**, each referencing an `InferenceClass` for its
  hardware capabilities and provisioning recipe.
- **Labels** for organizational metadata: tier, region, provider. These are the
  matching surface for `ModelDeployment.clusterSelector`.

Modelplane installs the serving stack it needs on every cluster it manages,
including existing clusters, which it assumes are solely for its use.

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

- **Provisioned (`GKE`, `EKS`).** Modelplane creates the cluster and its GPU node
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

## Examples

{{< tabs >}}
{{< tab "GKE" >}}
{{< manifests path="concepts/inference-cluster-gke.yaml" apply="false" >}}
{{< /tab >}}
{{< tab "EKS" >}}
{{< manifests path="concepts/inference-cluster-eks.yaml" apply="false" >}}
{{< /tab >}}
{{< tab "Existing" >}}
{{< manifests path="concepts/inference-cluster-existing.yaml" apply="false" >}}
{{< /tab >}}
{{< /tabs >}}

## Cache storage

On provisioned `GKE` and `EKS` clusters, the ReadWriteMany StorageClass
Modelplane uses for
[ModelCache]({{< ref "models/model-cache.md#custom-cache-backends" >}}) PVCs is
auto-provisioned. On an `Existing` cluster, name the StorageClass you created in
`cluster.existing.cache.storageClassName`. See
[Custom cache backends]({{< ref "models/model-cache.md#custom-cache-backends" >}})
for how storage is provided on each.
<!-- vale write-good.Passive = YES -->
