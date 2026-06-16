---
title: Register a Cluster
weight: 30
description: A Kubernetes cluster registered with Modelplane for model serving.
---
**API:** [`modelplane.ai/v1alpha1` · InferenceCluster]({{< ref "reference#crd-inferencecluster" >}})
<!-- vale write-good.Passive = NO -->
An `InferenceCluster` represents a Kubernetes cluster configured for model
serving. Platform teams create these to provide GPU capacity.


Each cluster has:

- A **cluster source**: `GKE` or `EKS` (Modelplane provisions the full cluster)
  or `Existing` (bring a cluster you manage yourself).
- One or more **node pools**, each referencing an `InferenceClass` for its
  hardware capabilities and provisioning recipe.
- **Labels** for organizational metadata: tier, region, provider. These are the
  matching surface for `ModelDeployment.clusterSelector`.

Modelplane installs an inference stack (LeaderWorkerSet, llm-d, Dynamo, Envoy
Gateway, etc.) on every cluster it manages. This includes existing clusters,
which Modelplane assumes are solely for its use.

## Examples

{{< tabs >}}
{{< tab "GKE" >}}
{{< manifests "platform/inference-cluster-gke.yaml" >}}
{{< /tab >}}
{{< tab "EKS" >}}
{{< manifests "platform/inference-cluster-eks.yaml" >}}
{{< /tab >}}
{{< tab "Existing" >}}
{{< manifests "platform/inference-cluster-existing.yaml" >}}
{{< /tab >}}
{{< /tabs >}}

## Cache storage

To override the default ReadWriteMany StorageClass Modelplane uses for
[ModelCache]({{< ref "models/model-cache.md#custom-cache-backends" >}}) PVCs on
this cluster, set `cluster.<source>.cache.storageClassName`. See
[Custom cache backends]({{< ref "models/model-cache.md#custom-cache-backends" >}})
for details.
<!-- vale write-good.Passive = YES -->
