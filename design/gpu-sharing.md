# Sharing a GPU between replicas

**Status:** Draft
**Date:** November 2026
**Author:** Tobias Kässer (@kaessert)

This document proposes that the fleet scheduler place replicas by the capacity
a GPU has free, where the GPU's DRA driver lets several claims share it, so two
small models can run on one GPU
([#644](https://github.com/modelplaneai/modelplane/issues/644)). It builds on
[free-gpu-scheduling.md](./free-gpu-scheduling.md).

## Background

DRA's consumable capacity
([KEP-5075](https://github.com/kubernetes/enhancements/issues/5075)) lets a
driver publish a device with `allowMultipleAllocations`, and a claim request a
share of its capacity with `capacity.requests`. The scheduler records what each
claim consumed in `status.allocation.devices.results[].consumedCapacity`, and
allocates the device to further claims until their shares add up to its
capacity. It's alpha behind the `DRAConsumableCapacity` feature gate in
Kubernetes 1.34 and 1.35, and on by default from 1.36. The NVIDIA DRA driver
publishes its GPUs this way with its `ConsumableShares` feature gate and a
`consumableShares` mode set. In its
`unlimited` mode a claim that requests no memory consumes none, and in its
`memory` mode it consumes the whole GPU.

On a k3s 1.35 cluster with the gate on, and NVIDIA's DRA driver v0.5.0 in
`unlimited` mode, a ModelDeployment's claim and a plain pod's claim were both
allocated the same GPU, each with a share of its own. A second ModelDeployment didn't schedule, reporting
`InsufficientCapacity`. The cluster agent counts a device as taken once any
claim has allocated it, so the pool's `nodesByFreeDevices` showed no GPU free.
A ModelDeployment also has no way to ask for a share, and an engine no way to
learn how much of the GPU's memory it may use.

## Proposal

A class marks the devices that several claims may share, and a deployment's
request states how much of each device it consumes, both in DRA's terms:

```yaml
# The InferenceClass.
spec:
  devices:
  - name: gpu
    driver: gpu.nvidia.com
    deviceClassName: gpu.nvidia.com
    count: 1
    allowMultipleAllocations: true  # New.
    capacity:
      memory: { value: "81559Mi" }
---
# A ModelDeployment member.
nodeSelector:
  devices:
  - name: gpu
    count: 1
    selectors:
    - cel: device.driver == "gpu.nvidia.com"
    capacity:                       # New.
      requests:
        memory: 24Gi
```

A request that sets `capacity.requests` matches only a device that allows
multiple allocations and declares at least the requested amount of each
capacity. A deployment doesn't name a class, so CEL on its XRD can't check
this, and the check is part of matching.

The cluster agent reports, in each bucket of a pool's histogram, the capacity
each shared device has left. A shared device is one that allows multiple
allocations and that a claim has allocated. Its free capacity is its capacity
less the claims' `consumedCapacity`, and none of it if a claim has no recorded
consumption. It still doesn't count in `freeDevices`, so `freeDevices` keeps
meaning devices no claim holds:

```yaml
nodesByFreeDevices:
- freeDevices:
    gpu.nvidia.com: 0
  sharedDevices:          # New.
    gpu.nvidia.com:
    - freeCapacity:
        memory: 56Gi
  nodes: 1
```

The scheduler fits a request that sets `capacity.requests` on `count` distinct
devices, each a shared device with that much free or a device in
`freeDevices`, which has the capacity its class declares. It takes shared
devices first, the fullest first, guessing that DRA packs a device before
starting another. A request that sets no `capacity.requests` takes whole
devices from `freeDevices`, as it does on any pool. That's stricter than DRA in
NVIDIA's `unlimited` mode, where such a claim consumes nothing, and the same as
DRA without a request policy or in `memory` mode. The scheduler can't see which
mode a driver runs in, and the stricter guess can't overcommit a device. A pool
the agent doesn't report on is still counted in whole nodes.

compose-model-deployment copies `capacity.requests` onto the ModelReplica's
device requests, and compose-model-replica into the `ResourceClaimTemplate`. So
each pod gets a claim of its own, and DRA places and accounts for each share.
When exactly one of a member's requests asks for memory, its engine container
gets `MODELPLANE_DEVICE_MEMORY_FRACTION`, the requested memory over the memory
the class declares, rounded down to three places. DRA accounts for a share but
doesn't confine an engine to it, so Modelplane tells the engine. Modelplane
doesn't write engine flags, so the variable is for the deployment's args to
reference, as in `--gpu-memory-utilization=$(MODELPLANE_DEVICE_MEMORY_FRACTION)`.

Turning sharing on stays with the platform team: the feature gate, and the
driver's sharing mode.

## Future improvements

The serving stack could configure the NVIDIA DRA driver's sharing mode from the
InferenceCluster on clusters Modelplane provisions.

Per-replica GPU metrics would need the share each replica's pods hold, which
the ReplicaObservation could report from the same `consumedCapacity`.

## Alternatives considered

### Share one ResourceClaim per pool

Every replica sharing a GPU could reference one ResourceClaim, which works
without consumable capacity. DRA would then account for nothing: each replica's
share would be Modelplane's bookkeeping alone, and every pod on the claim would
see the same devices and have to run on their node. The
`capacity.requests` field doesn't preclude adding it later for clusters before
1.36.

### Write the share into known engines' args

compose-model-replica could add `--gpu-memory-utilization` for vLLM and its
equivalent for other engines. Modelplane passes engine args through as written
and doesn't know any engine by name, and a variable keeps it that way.
