---
title: Serving Topologies
weight: 15
description: The shapes a model can take across the fleet, from a single pod to disaggregated prefill and decode.
---
**API:** [`modelplane.ai/v1alpha1` · ModelDeployment]({{< ref "/reference/modeldeployments" >}})
<!-- vale write-good.Passive = NO -->
A serving topology is the shape an engine takes on the fleet: how many pods it
spans, and how serving is split across them. Modelplane is unopinionated about
the engine itself. You bring the container and its flags; Modelplane shapes the
topology around it and schedules it onto matching hardware. The engine flags you
write carry parallelism, quantization, and KV transfer; Modelplane never injects
them.

A [`ModelDeployment`]({{< ref "model-deployment.md" >}}) sets its topology through
three choices:

- **One pod or a gang**: whether an engine is a single `Standalone` pod or a
  `Leader` with one or more `Worker` pods coordinating across nodes.
- **Unified or disaggregated**: whether `spec.serving.mode` keeps prefill and
  decode together (`Unified`) or splits them across two engines
  (`PrefillDecode`).
- **How many replicas**: `spec.replicas` runs whole copies of the topology;
  scaling adds or removes complete instances, never pods within one.

## Single-node

The default. One `Standalone` member is one pod on one node, claiming that node's
GPUs. Use it whenever the model fits on a single node. Within a node, tensor
parallelism is an engine flag (`--tensor-parallel-size`), not a Modelplane concept.

```yaml {nocopy=true}
engines:
- name: qwen
  members:
  - role: Standalone        # one pod, one node
```

## Multi-node

When a model is too large for one node's GPUs, make the engine a gang: a `Leader`
and a `Worker` whose `worker.nodes` expands to that many worker pods, one per
node. Modelplane composes a LeaderWorkerSet that serves the model together; the
workers join the leader through the `MODELPLANE_LEADER_ADDRESS` it injects. How
the model splits across the gang (tensor, pipeline, data, or expert parallelism)
is up to your engine flags.

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

## Disaggregated serving

The prefill and decode phases have opposite hardware profiles, and on one engine
a prefill burst stalls the decodes already running.
Set `spec.serving.mode: PrefillDecode` to run them as two engines, one marking
`phase: Prefill` and the other `phase: Decode`. Modelplane fronts the pair with
inference-aware routing that sequences prefill then decode, moving the KV cache
between them over NIXL. Each phase can sit on the GPU class that suits it.

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
outweighs the benefit, so unified serving is the default. It requires an engine
image that includes the **NIXL** KV-transfer runtime; see
[Deploy a Model]({{< ref "model-deployment.md" >}}) for the prerequisite detail.

## Choosing a topology

| Topology | Use when | How you set it |
|----------|----------|----------------|
| Single-node | The model fits on one node's GPUs | One `Standalone` member (the default) |
| Multi-node | The model is too large for one node | A `Leader` and one or more `Worker` members, plus `modelCacheRef` |
| Disaggregated serving | Large model, heavy load, strict latency, long context | `serving.mode: PrefillDecode` with two phase engines |

The engine flags you write handle parallelism within and across nodes (tensor,
pipeline, data, or expert); it composes with any of these topologies, independent
of what Modelplane schedules.

For the full field reference and runnable examples, see
[Deploy a Model]({{< ref "model-deployment.md" >}}).
<!-- vale write-good.Passive = YES -->
