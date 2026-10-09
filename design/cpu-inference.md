# Serving models on CPUs

**Status:** Draft
**Date:** October 2026
**Author:** Nicholas Thomson

This document proposes how Modelplane provisions node pools with no GPU and
serves models on their CPUs, using
[dra-driver-cpu](https://github.com/kubernetes-sigs/dra-driver-cpu) to make
those CPUs claimable.

## Summary

I propose four changes, each usable on its own:

1. An InferenceClass may omit `accelerator` from its provisioning block. A pool
   whose class has none is composed with a new `CPU` role: a standard node
   image, no GPU driver and no GPU attachment.
2. `CPU` pools carry a taint of their own, which engine pods tolerate, so that
   system workloads stay off them as they stay off GPU pools today.
3. The serving stack installs dra-driver-cpu on a cluster when one of its pools
   uses a class with a `dra.cpu` device, and only on those pools.
4. Later, a device request may ask for part of a device's capacity, so that
   more than one replica can share a CPU node.

Changes 1 to 3 are enough to serve a model on CPUs on every provider. Change 4
is about packing, and can wait.

## Background

A small model can be served on CPUs alone, which makes a test target that
costs a fraction of a GPU node.

Modelplane places an engine only on a device it can claim through DRA. The
scheduler rejects an engine none of whose members match a `claim: DRA` device,
and a `Synthetic` device doesn't count. The only DRA driver a serving stack
installs is NVIDIA's, so a node with no GPU publishes nothing to claim.

[dra-driver-cpu](https://github.com/kubernetes-sigs/dra-driver-cpu) publishes a
node's CPUs as DRA devices, under the driver and DeviceClass `dra.cpu`. In its
default grouped mode it publishes one device per NUMA node, with the node's
allocatable CPUs as consumable capacity (`dra.cpu/cpu`). A claimed device pins
the container that references the claim through `resources.claims` to a
cpuset, and Modelplane's engine containers already reference their claim that
way. A request that names no capacity consumes the whole device (KEP-5075), so
the engine gets every allocatable CPU in the NUMA node.

The scheduler places an engine on such a device with no change. It evaluates a
member's CEL selectors against the devices a class declares whatever their
driver, and passes the matched device's `deviceClassName` through to the
replica's ResourceClaimTemplate.

What's missing is a way to provision a node without a GPU, and to install the
driver on it. Every `spec.provisioning.<provider>` block requires an
`accelerator` with `count >= 1`, and on GKE its type is passed to GCP, so a
class can't describe a node without one. `compose-inference-cluster` also
composes every user pool with `role: GPU`, so even on EKS and AKS, where the
block is informational, every pool gets a GPU image, the `nvidia.com/gpu`
taint and a `modelplane.ai/gpu` label.

## Goals

- Provision a node pool with no GPU on every provider Modelplane supports,
  from an InferenceClass that describes no accelerator.
- Make that pool's CPUs claimable with no step outside Modelplane.
- Keep system components off CPU pools, as they are kept off GPU pools.
- Change nothing for a class that has an accelerator.

Fractional CPU claims, and packing several replicas onto one node, are a
non-goal for the first three changes. Arm CPU pools are out of scope too: the
EKS non-GPU AMI is `AL2023_x86_64_STANDARD`, and choosing an image by
architecture is a separate change.

## Proposal

### InferenceClass

`accelerator` becomes optional in every `spec.provisioning.<provider>` block.
Its meaning is unchanged where it is set. A class with no accelerator must
still declare at least one device, since `spec.devices` keeps `minItems: 1`.
A CPU class declares its `dra.cpu` device:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceClass
metadata:
  name: gke-cpu-c3-standard-8
spec:
  provisioning:
    provider: GKE
    gke:
      machineType: c3-standard-8
      diskSizeGb: 100
  devices:
  - name: cpu
    claim: DRA
    driver: dra.cpu
    deviceClassName: dra.cpu
    count: 1
    capacity:
      cpu: { value: "6" }
```

The device's `capacity.cpu` is the node's vCPUs less those the driver reserves
(see [The driver](#the-driver)). It is what CEL selectors read, so a member can
require a floor:

```yaml
nodeSelector:
  devices:
  - name: cpu
    count: 1
    selectors:
    - cel: device.capacity["dra.cpu"].cpu.compareTo(quantity("4")) >= 0
```

### Node pool role

The cloud cluster XRDs (EKSCluster, GKECluster, AKSCluster, NebiusCluster,
VultrCluster, CivoCluster) gain a third role, `CPU`, beside `System` and `GPU`.
`compose-inference-cluster` composes a pool as `GPU`, with its `gpu` block,
when its class has an accelerator, and as `CPU` when it has none. Today it
hard-codes `role="GPU"` for every user pool and reads `prov.accelerator`
unconditionally, so each of those reads needs a guard, including the Civo
NVLink projection.

The cloud functions already compose a plain pool for any role other than
`GPU`: the standard AMI on EKS, COS without `guestAccelerator` on GKE, no
`gpuDriver` on AKS, and no `modelplane.ai/gpu` label or GPU taint anywhere.
Every existing `role == "GPU"` check keeps working. The new role exists so a
CPU pool is never mistaken for the injected system pool, which uses `System`
and is told apart from user pools only by its name.

The `has(zones)` rule in the cluster XRDs applies only to `GPU` pools, but GKE
requires `minItems: 1` when zones are set, and `compose-inference-cluster`
passes an empty list today. It should set zones only when the pool has some.

### Taint

System components (the gateway, Prometheus, cert-manager and so on) set no
node selector. They stay off inference nodes only because GPU pools carry the
`nvidia.com/gpu` taint. An untainted CPU pool would take them.

`CPU` pools carry the label `modelplane.ai/cpu=true` and the taint
`modelplane.ai/cpu=true:NoSchedule`. The label is what the CPU driver selects
on (see [The driver](#the-driver)). `place_pod` in
`compose-model-replica` already adds an `nvidia.com/gpu` toleration to every
engine pod; it adds this one beside it. Pinning system components to the
system pool would also work, but it touches every component in every stack,
while the taint touches one function per cloud and one in the replica.

### The driver

The serving stack function sees only its ServingStack, not the cluster's pools
or classes, so the cluster composition tells it which pools need the driver.
This follows the precedent of `spec.gpu.pools`, through which
`compose-inference-cluster` already projects per-pool NVLink settings for
Civo.

ServingStack gains `spec.cpu.pools`, a list keyed by pool name.
`compose-inference-cluster` lists every pool whose class has a `claim: DRA`
device with `driver: dra.cpu`, for every source including `Existing`:

```yaml
spec:
  cpu:
    pools:
    - name: cpu-8vcpu
```

The list decides only whether the stack installs the driver. Where it runs is
decided by a label every `CPU` pool carries (see [Taint](#taint)), so the
driver needs no per-pool placement however many CPU pools a cluster has.

`compose-serving-stack` applies a hand-written transform, beside the Civo one,
that appends a `Chart` when the list is non-empty:

| Field | Value |
|---|---|
| key | `dra-driver-cpu` |
| release | `mp-dra-driver-cpu` |
| chart | `dra-driver-cpu` from `oci://registry.k8s.io/dra-driver-cpu/charts`, 0.3.0 |
| `nodeSelector` | `modelplane.ai/cpu: "true"` |
| `driverConfig.reservedCPUs` | `0-1` |

The chart's other defaults suit Modelplane. It runs the driver in grouped mode,
and it installs the `dra.cpu` DeviceClass. `reservedCPUs` is set through
`driverConfig` because 0.3.0 deprecates the `args.*` form. Its default
tolerations accept every `NoSchedule` taint, including `modelplane.ai/cpu`, so
the `nodeSelector` is what keeps the driver off GPU pools and the system pool,
not their taints.

0.3.0 is the oldest chart the design supports. Earlier charts expose neither
`nodeSelector` nor `affinity`, so they can't keep the driver to `CPU` pools.

The driver is not part of the AICR-generated stacks. The generator only
classifies AICR's own components, and the `stacks-current` check would
overwrite a hand edit.

Unlike the NVIDIA driver, which every stack installs whatever its pools, this
one is conditional. A cluster with no CPU pool runs nothing new.

`reservedCPUs` decides what a class should declare as `capacity.cpu`: a node's
vCPUs less two. I propose a fixed value, documented with the InferenceClass,
over a per-pool one. A per-pool value would have to be kept in step with the
class by hand, which is the same problem in a less visible place.

### Pool status

InferenceCluster defines a `status.gpuPools` which declare the pools which can
be scheduled to. It lists every pool whose class declares a device, so it
will list CPU pools too, and nothing in an entry is specific to GPUs. I propose
renaming it to `status.schedulablePools`. `status.nodePools` would mirror
`spec.nodePools` more closely, but the schema generator already names the spec
item `NodePool`, and the status item would need a different name.

### Capacity requests

Today a member's device request has a name, a count and CEL selectors, and the
rendered ResourceClaimTemplate asks for whole devices. A `dra.cpu` claim
therefore takes the whole NUMA group, and one replica fills a node.

Sharing a node needs a capacity request on the device:

```yaml
nodeSelector:
  devices:
  - name: cpu
    count: 1
    capacity:
      requests:
        cpu: "4"
```

That means adding `capacity.requests` to the ModelDeployment and ModelReplica
device requests, carrying it through the scheduler into the
ResourceClaimTemplate's `exactly.capacity.requests`, and checking it against
the device's published capacity when matching.

That alone doesn't let replicas share a node, because the scheduler's ledger
charges a full node to every member that claims a device. It would need to
account for capacity on shared devices. The cluster also needs KEP-5075's
`DRAConsumableCapacity` feature, and I haven't checked which Kubernetes
versions enable it by default. That is enough work to deserve its own design.

## Other effects on CPU pools

These don't block serving, but behave differently on a CPU node:

- Engine pods set no CPU or memory requests, so they are BestEffort. The
  `/dev/shm` emptyDir is memory-backed with no size limit, which on a CPU node
  competes with the model's KV cache for the same RAM.
- DCGM metrics are empty. vLLM's own metrics still apply.
- The endpoint picker assumes a KV block size of 16 when the engine doesn't
  state one. vLLM's CPU backend may default to another size, which would
  degrade prefix-cache routing; recipes should pass `--block-size`.

## Testing

`e2e/run.sh` already installs a second DRA driver, the vendored
dra-example-driver, on kind. If dra-driver-cpu runs on kind nodes, which I
haven't tried, an e2e case can run a CPU pool on an `Existing` cluster: a
`dra.cpu` class, the driver installed through the serving stack, and a
deployment whose member selects on `dra.cpu` capacity.

Unit tests change in `compose-inference-cluster` (role and projection per
source, and the renamed status field), each cloud cluster function (the `CPU`
role and taint), `compose-serving-stack` (the transform, including that a
cluster with no CPU pool composes nothing new), `compose-model-replica` (the
toleration) and `compose-model-deployment` (the renamed status field).

## Alternatives considered

### Reusing the System role

`System` already composes a plain pool, so a CPU pool could use it with no XRD
change. But the injected system pool is distinguished from user pools only by
its name, and nothing reserves that name. A distinct role keeps the two apart
in every function that reads `role`.

### Synthetic CPU devices

A class could declare a `claim: Synthetic` CPU device and skip the driver. The
scheduler rejects an engine whose only matches are synthetic, because it would
schedule a pod with nothing binding it to the node's hardware. Relaxing that
rule for CPUs would give the engine no cpuset, and two engines on one node
would compete for the same cores.

## Open questions

- What is the minimum Kubernetes version dra-driver-cpu 0.3.0 supports, given
  it builds on 1.37, and does every provider Modelplane provisions offer it?
- Which Kubernetes versions on each provider enable `DRAConsumableCapacity`?
  That decides whether capacity requests can be relied on.
