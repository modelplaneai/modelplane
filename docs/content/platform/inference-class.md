---
title: Define Hardware Classes
weight: 20
description: Hardware recipe defining GPU type, count, and provisioning for a node pool.
---
**API:** [`modelplane.ai/v1alpha1` · InferenceClass]({{< ref "/reference/inferenceclasses" >}})

<!-- vale write-good.Passive = NO -->
An `InferenceClass` is a tested recipe for a GPU node pool. It bundles:


- **Devices**: the node's hardware as a list of Dynamic Resource Allocation (DRA)
  style devices, each with a driver, count, typed attributes, and capacity. The
  scheduler matches a member's `nodeSelector` against these devices, and GPUs
  bind to pods through DRA.
- **Provisioning** (optional): how to create a node pool of this class on a
  specific cloud. Classes without provisioning are for existing clusters where
  the pool already exists.

Different clouds and GPU types imply different classes. A GKE L4 pool is
`gke-l4-1x-g2`. A bare-metal H100 pool is `h100-8x-byo` (no provisioning).

## Describing devices

A class's `devices` follow Kubernetes
[Dynamic Resource Allocation](https://kubernetes.io/docs/concepts/scheduling-eviction/dynamic-resource-allocation/)
(DRA), the mechanism modern Kubernetes uses to match GPUs to pods. Each device
has a `driver` (the vendor that owns it, such as `gpu.nvidia.com`), a `count`
(how many a node has), typed `attributes` (such as `architecture`), and
`capacity` (quantities, such as `memory`). This mirrors the shape the GPU's DRA
driver publishes on a real node, so what you declare here is what an ML team's
`nodeSelector` matches against and what DRA binds at runtime.

You author the attribute and capacity keys, and there's no fixed list. Pick the
ones an ML team would reasonably select on, the GPU memory, the architecture, the
compute capability, using the same names the driver reports.

## DRA and synthetic devices

Each device sets a `claim` discriminator:

- **`DRA`** (the default) is hardware a real DRA driver exposes, today GPUs.
  Modelplane both schedules against it and binds it to pods.
- **`Synthetic`** is described for scheduling only, never claimed. Use it for
  hardware that matters for placement but has no DRA driver yet, like an
  InfiniBand fabric.

## The device contract

The `driver`, attribute keys, and capacity keys a class declares are a contract
with the ML team: a `ModelDeployment`'s `nodeSelector` matches a pool only if the
class publishes the attributes and capacity it asks for. For GPUs, these keys
should mirror what the DRA driver reports, so the same selector that places a
deployment on the pool also binds the right device.

Publish a device's real usable capacity, not its nominal spec. An `80GB` H100
reports about `81559Mi` of usable memory, so a class that declares `80Gi` would
let a `nodeSelector` asking for `>= 80Gi` match the pool but then fail to bind the
GPU.

## Examples

{{< tabs >}}
{{< tab "GKE L4" >}}
{{< manifests "concepts/inference-class-gke-l4.yaml" >}}
{{< /tab >}}
{{< tab "EKS L4" >}}
{{< manifests "concepts/inference-class-eks-l4.yaml" >}}
{{< /tab >}}
{{< tab "H100 bare-metal" >}}
{{< manifests "concepts/inference-class-h100-byo.yaml" >}}
{{< /tab >}}
{{< /tabs >}}
<!-- vale write-good.Passive = YES -->
