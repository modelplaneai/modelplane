---
title: Define Hardware Classes
weight: 20
description: Hardware recipe defining GPU type, count, and provisioning for a node pool.
---
**API:** [`modelplane.ai/v1alpha1` · InferenceClass]({{< ref "/reference/inferenceclasses" >}})

<!-- vale write-good.Passive = NO -->
An `InferenceClass` is a tested recipe for a GPU node pool. It bundles:


- **Devices**: the node's hardware as a list of Dynamic Resource Allocation (DRA)
  style devices, each with a driver, count, typed attributes, and capacity. A
  `claim: DRA` device (a GPU) is bound to pods through a DRA `ResourceClaim`; a
  `claim: Synthetic` device (like an InfiniBand NIC) is described for
  scheduling only. The scheduler matches a member's `nodeSelector` against these
  devices.
- **Provisioning** (optional): how to create a node pool of this class on a
  specific cloud. Classes without provisioning are for existing clusters where
  the pool already exists.

Different clouds and GPU types imply different classes. A GKE L4 pool is
`gke-l4-1x-g2`. A bare-metal H100 pool is `h100-8x-byo` (no provisioning).

## DRA and synthetic devices

Each device sets a `claim` discriminator that says how Modelplane treats it:

- **`DRA`** (the default) emits the device as a request in the `ResourceClaim`
  the serving pods claim through, and DRA binds a matching device to the pod at
  admission time. Use it for hardware a real DRA driver exposes, today GPUs. A
  `DRA` device needs a `deviceClassName`, the cluster-scoped DRA `DeviceClass`
  the driver install creates.
- **`Synthetic`** describes a device for fleet scheduling only and never claims
  it. Use it for hardware that matters for placement but has no DRA driver yet,
  like an InfiniBand fabric. The scheduler enforces a synthetic device by pool
  selection alone, so it influences where a replica lands but adds nothing to the
  `ResourceClaim`.

## The device contract

A class's `driver`, attribute keys, and capacity keys are a contract between the
platform team who authors classes and the ML team who writes `nodeSelector`. The
keys are bare names (`architecture`, `memory`); the domain comes from the
device's `driver`, so a `nodeSelector` reads them back as
`device.attributes["gpu.nvidia.com"].architecture` and
`device.capacity["gpu.nvidia.com"].memory`.

For `claim: DRA` devices these should mirror what the DRA driver actually
publishes in its `ResourceSlice`, so the same `nodeSelector` that matched the
class at scheduling time also selects the right device at claim time. Publish a
device's real usable capacity, not its nominal spec: an `80GB` H100 reports about
`81559Mi` of usable memory, so a class that declares `80Gi` would let a
`nodeSelector` asking for `>= 80Gi` match the pool but then fail to bind the GPU.

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
