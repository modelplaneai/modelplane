---
title: Deploy a Model
weight: 10
description: Deploy a model to the fleet, from a single pod to disaggregated prefill and decode.
---
**API:** [`modelplane.ai/v1alpha1` · ModelDeployment]({{< ref "/reference/modeldeployments" >}})
<!-- vale write-good.Passive = NO -->
A `ModelDeployment` is the ML team's primary interface. You describe the model
you want served, the hardware it needs, and how many copies to run; Modelplane
schedules it onto matching clusters and keeps it running. You never name a
cluster.

Modelplane is unopinionated about the engine itself. You bring the container and
its flags, and Modelplane shapes a serving topology around it. The engine flags
you write carry parallelism, quantization, and KV transfer, never injected by
Modelplane.

A deployment's `spec.engines` describes its topology through two choices:

- **One pod or a gang**: whether an engine is a single `Standalone` pod or a
  `Leader` with one or more `Worker` pods coordinating across nodes.
- **Unified or disaggregated**: whether `spec.serving.mode` keeps prefill and
  decode together (`Unified`, the default) or splits them across two engines
  (`PrefillDecode`).

How many of each to run is a separate question, covered in
[Sizing a deployment](#sizing-a-deployment).

## Single-node

The default, and what the [getting started tour]({{< ref "/getting-started" >}})
deploys. One `Standalone` member is one pod on one node, claiming that node's
GPUs through its `nodeSelector`. It's usually the right choice when a model fits
on a single node. Within a node, tensor parallelism is an engine flag
(`--tensor-parallel-size`), not a Modelplane concept.

```yaml {nocopy=true}
engines:
- name: qwen
  members:
  - role: Standalone        # one pod, one node
```

## Multi-node

When a model is too large for one node's GPUs, make the engine a gang: a `Leader`
and a `Worker` whose `worker.nodes` expands to that many worker pods, one per
node. The pods serve the model together; how the model splits across them
(tensor, pipeline, data, or expert parallelism) is up to your engine flags.

A gang requires a [`ModelCache`]({{< ref "model-cache.md" >}}) via
`spec.modelCacheRef`, since every pod mounts the same weights.

```yaml {nocopy=true}
modelCacheRef:
  name: qwen3-coder         # required for gangs
engines:
- name: qwen3-coder
  members:
  - role: Leader
  - role: Worker
    worker:
      nodes: 1              # one worker pod per node
```

A member's `env` can read pod fields through `valueFrom.fieldRef`, like setting
vLLM's `VLLM_HOST_IP` from `status.podIP`, which multi-NIC RDMA nodes need so the
engine binds the right interface instead of guessing it.

## Disaggregated serving

The prefill and decode phases have opposite hardware profiles, and on one engine
a prefill burst stalls the decodes already running. Set
`spec.serving.mode: PrefillDecode` to run them as two engines, one marking
`phase: Prefill` and the other `phase: Decode`. Modelplane fronts the pair with
inference-aware routing that sequences prefill then decode, moving the KV cache
between them. Each phase can sit on the GPU class that suits it.

```yaml {nocopy=true}
serving:
  mode: PrefillDecode       # the two engines below are one P/D pair
engines:
- name: prefill
  phase: Prefill
- name: decode
  phase: Decode
```

Disaggregation pays off for large models under load with strict latency targets
and long context. For small models or low traffic, the KV-transfer overhead
outweighs the benefit, so unified serving is the default.

It requires an engine image that includes the **NIXL** KV-transfer runtime.
vLLM's `NixlConnector` (and SGLang's prefill/decode transfer) import the `nixl`
package, so disaggregated engines crash at startup with `NIXL is not available`
on an image that lacks it. Recent vanilla `vllm/vllm-openai` images include NIXL,
so pin a current tag rather than an old one. The engine image is yours to choose,
so this is a prerequisite Modelplane does not bundle for you.

## Requesting GPUs

You don't name a cluster or a GPU model. Instead each member's `nodeSelector`
lists the hardware its pods need, and Modelplane finds a node pool that has it.
The platform team publishes node pools as `InferenceClass` resources, each
describing the devices its nodes carry. Your request is matched against them.

A request names a device (`gpu`), how many of it each pod needs (`count`), and
one or more `selectors` the device must match:

```yaml {nocopy=true}
nodeSelector:
  devices:
  - name: gpu
    count: 1                # one GPU per pod
    selectors:
    - cel: |
        device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("40Gi")) >= 0
```

Each selector is a single line of CEL, a small expression language, that returns
true or false for one device. The part in brackets, `"gpu.nvidia.com"`, is the
GPU vendor's driver. The fields after it, like `memory` or `architecture`, are
what the platform team published for that device. This one says "match a GPU
whose memory is at least 40Gi." A device has to match every selector in the
request. Give two selectors to mean "Hopper, with at least 80Gi."

### Requesting more than one device

`devices` is a list, so a member can ask for distinct kinds of hardware at once,
each its own entry with its own `count` and `selectors`. A node pool matches the
member only when it satisfies every entry. This is how you ask for both a GPU and
a fast NIC on the same node:

```yaml {nocopy=true}
nodeSelector:
  devices:
  - name: gpu
    count: 8
    selectors:
    - cel: device.attributes["gpu.nvidia.com"].architecture == "Hopper"
  - name: nic
    count: 1
    selectors:
    - cel: device.attributes["nic.nvidia.com"].linkType == "infiniband"
```

### What you can match on

Each selector is evaluated against one device and must return a boolean. The
device exposes three things:

- `device.driver`: the device's driver, a string.
- `device.attributes["<driver>"].<name>`: a typed attribute (string, bool, int,
  or version), such as `architecture` or `cudaComputeCapability`.
- `device.capacity["<driver>"].<name>`: a capacity quantity, such as `memory`.

Two helpers build comparable values: `quantity()` parses Kubernetes quantities
like `"40Gi"`, and `semver()` parses versions like `"9.0.0"`. Both support
`compareTo` (which orders two values), `isGreaterThan`, and `isLessThan`. Combine
selectors with the usual CEL operators (`==`, `!=`, `>=`, `&&`, `||`).

```yaml {nocopy=true}
selectors:
# Capacity: at least 40Gi of GPU memory. >= 0 reads as "left is at least right".
- cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("40Gi")) >= 0
# Attribute equality: a specific architecture.
- cel: device.attributes["gpu.nvidia.com"].architecture == "Hopper"
# Version attribute: a minimum CUDA compute capability.
- cel: device.attributes["gpu.nvidia.com"].cudaComputeCapability.isGreaterThan(semver("8.9.0"))
# Driver: match any device from a given driver.
- cel: device.driver == "gpu.nvidia.com"
# Presence: only match a device that publishes a given domain.
- cel: '"gpu.nvidia.com" in device.attributes'
# Two conditions in one selector.
- cel: |
    device.attributes["gpu.nvidia.com"].architecture == "Hopper" &&
    device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0
```

This is the Kubernetes DRA device selector expression surface. The
Kubernetes-specific CEL extension libraries (such as regular expressions and IP
address helpers) aren't available. Selectors in practice are attribute and
capacity comparisons like those above.

### Seeing what's available

To see what you can match against, list the classes the platform team has
published and look at the devices each one declares:

```bash
kubectl get inferenceclass
kubectl describe inferenceclass gke-l4-1x-g2
```

The `describe` output shows each device's driver, attributes (like
`architecture`), and capacity (like `memory`), which are exactly the keys your
selectors read. If a selector asks for something no published class offers, the
deployment won't schedule.

## Sizing a deployment

Three independent numbers control how many pods a deployment runs:

- **`spec.replicas`** stamps out whole copies of the entire topology. Each
  replica is a complete serving instance, and replicas usually land on different
  clusters. This is the scaling axis (see [Scaling](#scaling)).
- **`engines[].copies`** runs several identical copies of one engine within a
  replica, on the same cluster. It's a fixed number, sized once, never
  autoscaled. Use it to run many copies of a small engine without one replica
  each, or to set the prefill-to-decode ratio in disaggregated serving.
- **`worker.nodes`** sets how many nodes one gang spans: a `Leader` plus that
  many `Worker` pods. It's how big a single multi-node engine is.

## Scaling

`spec.replicas` is the only scaling axis. Each replica is a complete,
fixed-shape serving instance, so scaling adds or removes whole instances across
the fleet. Because the deployment exposes the Kubernetes scale subresource,
`kubectl scale` and KEDA work without anything extra. There's no in-cluster pod
autoscaling.

## Choosing a topology

| Topology | Use when | How you set it |
|----------|----------|----------------|
| Single-node | The model fits on one node's GPUs | One `Standalone` member (the default) |
| Multi-node | The model is too large for one node | A `Leader` and one or more `Worker` members, plus `modelCacheRef` |
| Disaggregated serving | Large model, heavy load, strict latency, long context | `serving.mode: PrefillDecode` with two phase engines |

## Examples

{{< tabs >}}
{{< tab "Single-node" >}}
{{< manifests "concepts/model-deployment.yaml" >}}
{{< /tab >}}
{{< tab "Multi-node" >}}
{{< manifests "concepts/model-deployment-multinode.yaml" >}}
{{< /tab >}}
{{< /tabs >}}
<!-- vale write-good.Passive = YES -->
