# Scheduling by free GPUs

**Status:** Draft
**Date:** October 2026
**Author:** Nic Cope

This document proposes that the fleet scheduler count free GPUs per node rather
than free nodes, using what a new agent on each cluster reports, so replicas can
share a node. It builds on the base design in [design.md](./design.md).

## Background

The fleet scheduler places a ModelReplica on a cluster when one of the cluster's
pools has room for it, and it measures room in nodes. The InferenceCluster
publishes the most nodes each pool can have. Every pod that claims a GPU charges
a whole node against that, however many of the node's GPUs it claims.

This leaves GPUs idle wherever a pod claims less than a whole node. Qwen3-8B
fits on one NVIDIA L4. A pool of two g6.12xlarge nodes, which have four L4s
each, has room for eight Qwen3-8B replicas, but the scheduler places only two.
The cluster's scheduler might even put both on one node. A replica that serves
a large model from one 8-GPU node, with prefill on four GPUs and decode on the
other four, charges two nodes, so a pool of one node can't host it.

The accounting is deliberately coarse. It leaves contention for devices to DRA
on the workload cluster. Finer accounting needs to know how a pool's free GPUs
spread across its nodes: four free GPUs on one node fit a pod claiming two, and
one free GPU on each of four nodes doesn't. The control plane can't see that
today.

## Goals

GPUs are scarce and expensive, so the fleet should use every one it can. This
design should:

- Let replicas that claim less than a node share one.
- Choose only a cluster and a pool, and leave the choice of node to the
  cluster's scheduler.
- Keep the scheduler a pure function of what clusters publish.
- Keep what each cluster publishes bounded, however many nodes its pools have.

It's not yet a goal to recover a replica placed on a pool that can't fit it.
This can happen in cases where the cluster's scheduler binds pods differently
than the fleet scheduler predicted, leaving some without room.

## Proposal

I propose a cluster agent that runs on every InferenceCluster and reports a
histogram of each pool's nodes by free devices. One bucket might count the nodes
with four free GPUs, and another the nodes with none. Where a pool's nodes also
have other kinds of DRA devices, such as EFA adapters, a bucket counts the nodes
with the same number free of each kind. The fleet scheduler fits replicas
against these histograms instead of against node counts.

Modelplane installs the agent as part of the serving stack, including on
clusters it didn't provision. The agent needs cluster-wide read access to nodes,
pods, events, and DRA's ResourceSlices, ResourceClaims and DeviceClasses. It
reports in the status of two custom resources:

- `PoolObservation`. The InferenceCluster creates one for each of its pools.
- `ReplicaObservation`. Each ModelReplica creates one, which selects the
  replica's engine pods by their deployment and replica index labels.

Provider-kubernetes watches both resources, and copies their status to the
control plane as it changes rather than on a poll. The InferenceCluster and
ModelReplica then publish what the agent reported in their own status.

For a pool of one g6.12xlarge node on a test EKS cluster, with three of its four
L4s allocated, the InferenceCluster and a ModelReplica with one of the L4s
publish:

```yaml
# The InferenceCluster.
status:
  schedulablePools:
  - name: gpu-l4x4-a
    nodes: 1          # The most nodes the pool can have.
    observedNodes: 1  # New. The nodes the pool has now, ready or not.
    lastAllocationTime: "2026-10-20T10:05:40.318204Z"  # New. See below.
    devices:
    - name: gpu
      claim: DRA
      driver: gpu.nvidia.com
      deviceClassName: gpu.nvidia.com
      count: 4
    nodesByFreeDevices:  # New. Schedulable nodes by free devices.
    - nodes: 1
      freeDevices:
        gpu.nvidia.com: 1
  # Omitted for brevity.
---
# The ModelReplica.
status:
  conditions:
  - type: ModelPodsScheduled  # New. See below.
    status: "True"
    reason: AllPodsScheduled
  # Omitted for brevity.
  pools:  # New. See below.
  - name: gpu-l4x4-a
    lastAllocationTime: "2026-10-20T10:02:13.482915Z"
```

`ModelPodsScheduled` is true once the cluster has bound every pod the replica
runs to a node, including a gang's workers and pods that claim nothing. The
agent must also have seen each of those pods' claims allocated. While it's
false, the condition gives the agent's reason, with the cluster scheduler's
message from when the reason last changed. A Pending replica's status then
changes only when its reason does.

The fleet scheduler still chooses only a cluster and a pool, as it does today.
The histogram shows which of a pool's nodes could fit a pod, but the cluster's
scheduler chooses which one it binds to. To check that a replica plausibly
fits, the fleet scheduler fits its pods against the histogram one at a time,
guessing that each binds to the node with the most of its devices free. Before
it places a new replica, the scheduler:

1. Starts from the published histogram. The agent leaves out cordoned and
   unready nodes, and nodes with taints Modelplane's GPU pods don't tolerate.
   It treats devices that other workloads claim as taken.
2. Adds the nodes the pool can still grow by, `nodes` minus `observedNodes`,
   with all of their devices free. A pool that a surge upgrade has taken past
   `nodes` can't grow.
3. Places the pods of earlier replicas that the histogram doesn't count yet.

A replica claiming one L4 fits the pool above, and one claiming two doesn't. The
replica from the background, with prefill on four GPUs and decode on four more,
fits one empty 8-GPU node. A pod claiming GPUs and EFA adapters fits only a node
with enough of both free. A node that's still booting counts toward
`observedNodes` but not the histogram, which leaves the pool looking one node
smaller until the node is ready. A pool with no published histogram gets
whole-node accounting, as every pool does today. So does a pool whose nodes span
more than 256 combinations of free devices, the histogram's limit.

The agent records when it first saw each claim allocated, to the microsecond
and later than any time it has already published. It reports the latest of
those times as `lastAllocationTime`, for each pool and for each pool a replica
is on. It computes a pool's histogram and time from one read of its cache, and
that read includes every claim still allocated that the agent saw before the
latest one in it. So a pool time no earlier than a replica's means the
histogram counts the replica's claims on that pool. The scheduler places a
replica's pods on a pool in the third step until the replica's
`ModelPodsScheduled` condition is true and its time on that pool is no later
than the pool's.

If the agent stops reporting, the InferenceCluster goes on publishing its last
observations, and pools tend to look fuller than they are. New replicas can't
count as observed against a frozen pool. The scheduler keeps charging them, and
GPUs that replicas free still count as taken. A pool can look emptier than it
is only after a change whole-node accounting misses too, such as a node failing.

Because it guesses which node each pod binds to, the fleet scheduler can
mispredict. When the pods it places on a pool all claim identical devices,
which node each binds to doesn't change how many fit. When they claim different
devices, the cluster's scheduler can place them differently than the fleet
scheduler predicted. Deployments that place replicas on a pool at the same
moment, or an autoscaled pool that fails to grow, can overcommit it too, as
they can today. A mispredicted replica's pods stay Pending until room frees up,
which on a full pool that can't grow may be never. Its ModelDeployment reports
it not ready, and its `ModelPodsScheduled` condition gives the cluster
scheduler's reason.

## Future improvements

If mispredictions turn out to be common, I'd revisit how the fleet scheduler
places replicas rather than move them after the fact. Each cluster could decide
whether a replica fits, the way MultiKueue offers a workload to every worker
cluster and keeps the first that admits it. Mispredictions would probably become
common under GPU scarcity, when more workloads wait than there are GPUs to run
them. Many replicas would then compete for the same few freed devices at once.

Modelplane doesn't request CPU or memory for engine pods unless a deployment
sets them, so replicas that share a node would compete for both, and the kubelet
evicts them first under memory pressure. Requesting a share of the node's CPU
and memory in proportion to the pod's share of its GPUs would let the cluster's
scheduler account for them.

The scheduler could also take Prometheus metrics, like those the
[telemetry design](./telemetry.md) proposes collecting, into account when it
places replicas. The agent could then export its histogram as a metric rather
than in a PoolObservation's status.

## Alternatives considered

### Count GPUs against each pool's total

The scheduler could count a pool's GPUs as its nodes times GPUs per node, and
subtract what placed replicas claim. That needs nothing new on the cluster: it's
a change to arithmetic the scheduler already does, on state it already reads. It
can't see fragmentation though. It would mispredict whenever no node has enough
free GPUs for a pod, so it would need to move mispredicted replicas from the
start. It can't see nodes that are down either.

### Publish every node's free devices

The InferenceCluster could publish each node's free devices, which would tell
the scheduler exactly where each pod could go. A pool's status would then grow
with its node count. Whether a pod fits depends on how many devices each node
has free, not on which node it is. The histogram gives the same fit at a size
bounded by devices per node.

### Pin pods to nodes

The fleet scheduler could choose a node for each pod and pin the pod to it. Its
predictions would then be exact. Pods of different sizes couldn't strand a
replica, and no gang could end up with only some of its pods started.

The cluster's scheduler would then have nothing left to decide, and Modelplane
would become the node-level scheduler for every cluster in the fleet. It would
take on the gang scheduling, topology, priority and preemption that clusters
run kube-scheduler, Volcano, KAI or Kueue for, and it would have to do that
across a fleet potentially far larger than the largest cluster. Splitting the
decision lets each level scale. The fleet scheduler chooses among pools, and
each cluster's scheduler chooses among its own nodes from live state. Pinning
also couldn't use nodes an autoscaled pool hasn't created yet.

### Fall back on stale observations

The agent could refresh a time on each PoolObservation every minute, and the
scheduler could fall back to whole-node accounting for a pool whose time is a
few minutes old. That would stop the scheduler trusting a crashed agent's view.
Stale observations err toward pools looking full though, and
falling back would trade that for forgetting what the agent last saw, such as
GPUs that other workloads hold. Each refresh would also change the
InferenceCluster's status, which reconciles the XRs that require it.

### Publish capacity on its own kind

The InferenceCluster could compose an XR for each pool to publish its free
devices. Only the scheduler reads capacity, but a change to an
InferenceCluster's status reconciles the XRs that require it, including every
InferenceGateway, MetricMapping and ModelRoute. Capacity changes only when
allocations or nodes do though, which is rare outside a rollout. Because only
the scheduler reads it, moving it to its own kind later wouldn't change anyone's
manifests.

### Charge replicas for a while after they schedule

The scheduler could keep charging a replica for a fixed time, say a minute,
after its `ModelPodsScheduled` condition turns true, to give the pool's
observation time to catch up. The agent wouldn't need to record allocation
times. The time would be a guess though. If the observation took longer, the
scheduler would stop charging GPUs the histogram doesn't count yet, and if it
took less, it would count them twice. The scheduler would also depend on the
clock, and on a periodic reconcile to notice the time passing.

### Keep whole-node accounting

Whole-node accounting is simple, and it costs nothing when every pod claims a
whole node. It leaves GPUs idle whenever a pod claims less, and it charges two
nodes for prefill and decode on one node.
