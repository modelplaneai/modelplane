---
title: Define Hardware Classes
weight: 20
description: Hardware recipe defining GPU type, count, and provisioning for a node pool.
---
**API:** [`modelplane.ai/v1alpha1` · InferenceClass]({{< ref "reference#crd-inferenceclass" >}})

<!-- vale write-good.Passive = NO -->
An `InferenceClass` is a tested recipe for a GPU node pool. It bundles:


- **Devices**: the node's hardware as a list of Dynamic Resource Allocation (DRA)
  style devices, each with a driver, count, typed attributes, and capacity. A
  `claim: DRA` device (a GPU) is bound to pods through a DRA `ResourceClaim`; a
  `claim: Synthetic` device (an InfiniBand NIC, for example) is described for
  scheduling only. The scheduler matches a member's `nodeSelector` against these
  devices.
- **Provisioning** (optional): how to create a node pool of this class on a
  specific cloud. Classes without provisioning are for existing clusters where
  the pool already exists.

Different clouds and GPU types imply different classes. A GKE L4 pool is
`gke-l4-1x-g2`. A bare-metal H100 pool is `h100-8x-ib` (no provisioning).

## Examples

{{< tabs >}}
{{< tab "GKE L4" >}}
{{< manifests "platform/inference-class-gke-l4.yaml" >}}
{{< /tab >}}
{{< tab "EKS L4" >}}
{{< manifests "platform/inference-class-eks-l4.yaml" >}}
{{< /tab >}}
{{< tab "H100 bare-metal" >}}
{{< manifests "platform/inference-class-h100-byo.yaml" >}}
{{< /tab >}}
{{< /tabs >}}
<!-- vale write-good.Passive = YES -->
