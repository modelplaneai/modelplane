# Unopinionated ModelDeployments

**Status:** Draft
**Date:** June 2026
**Author:** Nic Cope

This document proposes a revision to how a `ModelDeployment` describes its
engines and serving, so the API stays unopinionated about the inference engine
and the parallelism topology. The API describes a deployment's shape, not how
the model is run. It iterates on and supersedes parts of the base design in
[design.md](./design.md).

## Summary

I propose a ModelDeployment describe two things: the *shape* of its inference
engines (`spec.engines`) and how those engines are *served* at the cluster edge
(`spec.serving`).

`spec.engines` specifies one or more engines. An engine may have either one
`Standalone` member, or one `Leader` and one or more `Worker` members. Each
engine may have `copies`: the _engine_ can be stamped out N times.

The optional `spec.serving` block specifies how the `InferenceCluster` exposes
the engines as an OpenAI compatible inference URL, suitable for the Modelplane
control plane to use as a `ModelEndpoint`. Its only field is `mode`: `Unified`
(the default) or `PrefillDecode`, for disaggregated serving. Under
`PrefillDecode` you must mark each engine's `phase` as `Prefill` or `Decode`.

A small model on a single GPU shows this API at its simplest:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: qwen3-8b
  namespace: ml-team
spec:
  replicas: 1
  clusterSelector:
    matchLabels:
      modelplane.ai/tier: production
  engines:
  - name: qwen3-8b
    members:
    - role: Standalone
      nodeSelector:
        devices:
        - name: gpu
          count: 1
          selectors:
          - cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("16Gi")) >= 0
      template:
        spec:
          containers:
          - name: engine
            image: vllm/vllm-openai:v0.11.0
            args:
            - --model=Qwen/Qwen2.5-7B-Instruct
```


## Background

Today `spec.workers` carries a `topology` block of parallelism axes:

```yaml
spec:
  workers:
    count: 1
    topology:
      tensor: 8
      pipeline: 2
    template: {...}
```

The [design](./design.md) also calls for two other topologies that we haven't
implemented yet: data parallelism (`topology.data`, `topology.dataLocal`) and
prefill/decode disaggregation (`spec.prefill`).

This proposal began as an attempt to implement `data` and `dataLocal`, so the
API could express the data-parallel and mixture-of-experts deployments that
frontier models like Kimi K2 and DeepSeek V3 need. Along the way I started to
feel `topology` is the wrong abstraction.

The four axes do two things each: they shape the workload (pods and nodes), and
they name an engine flag Modelplane injects.


| Axis | Effect on shape | Flag injected |
|---|---|---|
| `tensor` | × GPUs per node | `--tensor-parallel-size` |
| `dataLocal` | × GPUs per node | `--data-parallel-size-local` |
| `pipeline` | × nodes per worker | `--pipeline-parallel-size` |
| `data` | × nodes per worker | `--data-parallel-size` |


The shape a `topology` produces has only two degrees of freedom: GPUs per node
and nodes per worker. But it has four axes, because each pairs a shape effect
with a parallelism strategy. `pipeline` and `data` are the same shape lever, as
are `tensor` and `dataLocal`; the axes are distinct only in the flag they imply.
A `worker` itself is a Deployment when it's a single node and a LeaderWorkerSet
when it spans several. The scheduler gates on node count, never GPUs; per-node
GPU count is a `nodeSelector` concern, which `tensor` duplicates.

Everywhere else Modelplane passes the user's `args` through untouched. This is a
strong property: a new engine or flag needs no change to Modelplane. `topology`
breaks that. It derives `--tensor-parallel-size` and `--pipeline-parallel-size`
from `tensor` and `pipeline` and injects them. Those flags are engine-specific,
spelled differently by vLLM, SGLang, and TensorRT-LLM, so deriving them takes on
the per-engine knowledge Modelplane was trying to avoid. It also creates two
sources of truth for the same fact: the user still writes the parallelism flags
in `args`, and Modelplane derives them again from `topology`, with nothing
reconciling the two.

At the same time, [#34](https://github.com/modelplaneai/modelplane/issues/34)
and [#124](https://github.com/modelplaneai/modelplane/pull/124) attempt to add
support for prefill/decode (P/D). P/D is another type of topology. Like the
other topologies it requires a certain shape and certain engine flags, but it
also requires a certain _serving_ configuration. Without inference-aware request
routing between the InferenceCluster edge and the workers you won't see much
benefit from P/D disaggregation.

## Goals

My goal with this design is to allow ModelDeployment to model (heh) tensor
parallelism, data parallelism, expert parallelism, and disaggregated serving
while honoring these two principles:

**Modelplane shouldn't model engine behaviour.** It shouldn't understand or
inject engine flags. Parallelism (tensor, pipeline, data, expert), quantization,
KV transfer, and disaggregation mode all live in the engine's flags, written by
the user. A new engine, or a new form of parallelism released tomorrow, should
just work.

**The workload and serving mechanisms are an implementation detail.** What
Modelplane composes from a ModelDeployment (Deployment, LeaderWorkerSet,
Service, InferencePool, the orchestrator that gangs multi-node pods) isn't named
in the API. The user describes what they want; Modelplane chooses the mechanism.
We can move from LeaderWorkerSet to another gang scheduler, or from a Service to
an InferencePool, without an API change.

## Proposal

I propose a ModelDeployment describe two things: the *shape* of its inference
engines (`spec.engines`) and how they are *served* at an InferenceCluster's edge
(`spec.serving`). Modelplane composes a ModelReplica per `spec.replicas` and the
fleet scheduler places each on a cluster.

Under this design inference engine configuration is opaque to Modelplane, and
engines are deployed in flexible shapes.

As a result of these choices:

- Modelplane can broadly and automatically support new inference engines.
- Modelplane can broadly and automatically support new inference topologies.
- Modelplane is loosely coupled to any particular serving stack (LWS,
  InferencePool, llm-d, etc).

The tradeoff is verbosity. Instead of specifying "topology: tensor-parallel",
the ModelDeployment author must describe the shape of the topology and the flags
for each member.

This section covers the engine shape, the serving config, how a ModelReplica is
scheduled, and a worked example for every topology we expect to support.

### Engines

`spec.engines` describes a ModelReplica's topology as an array of engines.
An engine is one serving unit: a standalone pod, or a gang of pods coordinating
across nodes. Each engine may be copied (but not autoscaled) within a
ModelReplica.

- `name`: identifies the engine.
- `copies`: how many identical copies of this engine to run per `ModelReplica`.
- `phase`: the engine's phase in a disaggregated deployment, `Prefill` or
  `Decode`. Only set when `spec.serving.mode` is `PrefillDecode`.
- `members`: the engine's pods. An engine must either have a single `Standalone`
  member or a `Leader` and a `Worker`.

Each member has:

- `role`: `Standalone` (default), `Leader`, or `Worker`.
- `nodeSelector`: the member's per-node device request
- `worker.nodes`: how many nodes the member spans, for a `Worker` only. Each
  node runs one worker pod, so this is how big the gang is: a leader plus
  `worker.nodes` workers. Defaults to 1.
- `template`: a curated PodTemplateSpec carrying the engine container, its image,
  and its command and args.

A `Standalone` engine composes to a Deployment. A `Leader`/`Worker` engine composes
to a LeaderWorkerSet whose gang size is one leader plus its `worker.nodes`
workers. Which workload kind backs each is an implementation detail.

Modelplane expects the user to provide all the engine commands and flags needed
to form a topology. Some of those commands need to find other pods in the engine:
in a multi-node tensor+pipeline gang, for instance, the Ray followers need the
Ray head's address. Modelplane injects a small set of `MODELPLANE_` env vars
into the engine containers for this (today just `MODELPLANE_LEADER_ADDRESS`).
For the LWS backend it aliases the variable to `LWS_LEADER_ADDRESS`.

Note the relationship between `ModelDeployment` replicas, engine copies,
and worker nodes:

- `ModelDeployment` replicas specifies how many replicas of the entire model
  topology and serving apparatus should be stamped out. Replicas often run on
  different InferenceClusters. This is the scaling axis: Modelplane scales a
  model by adding and removing whole replicas.
- Engine copies specifies how many identical copies of an engine each
  `ModelReplica` stamps out - a fixed number, sized once, never autoscaled.
  Always within the same InferenceCluster, but potentially on different pools.
- Worker nodes specifies how big each copy of an engine's gang is - how many
  worker nodes it has.

I think the need for `ModelDeployment` replicas is obvious. It's how Modelplane
autoscales a model, and how it spreads replicas across multiple clusters. So is
worker nodes: it's how you size a model that can't fit on a single node. Engine
copies is more subtle. I toyed with not exposing it at all, but I think we
need it for two things.

The first reason we need engine copies is disaggregated serving. With a prefill
engine and a decode engine, engine copies are how you control the
prefill-to-decode ratio.

The second reason is sizing the ModelReplica itself. Engine copies maps to the
underlying Deployment or LeaderWorkerSet's replicas. Without it, running ten
copies of a single-node model means ten ModelReplicas of one pod each.
Modelplane would then schedule many pod-sized ModelReplicas across the fleet,
placing individual pods on clusters: the Kubernetes scheduler's job, done with
less information than the Kubernetes scheduler has. Engine copies lets one
ModelReplica hold many pods, so the fleet scheduler picks a cluster and leaves
placing pods on nodes to that cluster's scheduler.

### Serving

`spec.serving` specifies how an InferenceCluster should serve a ModelReplica:
how it should expose it as a usable ModelEndpoint target. It's optional, and if
omitted defaults to:

```yaml
spec:
  serving:
    mode: Unified
```

Modelplane has two layers of inference request routing. The InferenceGateway
runs on the Modelplane control plane, offering an OpenAI compatible inference
URL per ModelService.

The central InferenceGateway can only route to the InferenceCluster edge. Each
InferenceCluster also runs a gateway, which is responsible for routing from
cluster edge to the actual model engines (vLLM etc). This is what `spec.serving`
configures.

Modelplane is pretty opinionated about this layer. For example, we consider
which inference gateway we use an implementation detail - like using LWS or
llm-d is an implementation detail. Where possible we infer this layer's
configuration from the shape of the `engines` block.

By default Modelplane assumes:

- Every Standalone or Leader member exposes an OpenAI endpoint on port 8000.
- Every Standalone or Leader member should be part of one Kubernetes Service.
- The Kubernetes Service should be exposed by a Gateway API HTTPRoute.

This is `Unified` serving in the example above (i.e. not disaggregated).

The only other valid mode is `PrefillDecode`:

```yaml
spec:
  serving:
    mode: PrefillDecode
```

Under `PrefillDecode` each engine marks its phase:

```yaml
spec:
  engines:
  - name: prefill
    phase: Prefill
    ...
  - name: decode
    phase: Decode
    ...
```

`mode` is the single explicit statement that a deployment is disaggregated;
`phase` marks which engine plays which part. Validation ties them together:
`PrefillDecode` requires exactly two engines, one `Prefill` and one `Decode`,
and `phase` may not be set under any other mode

The mode enum leaves room for other topologies with special serving needs (e.g.
`EncodePrefillDecode` for multimodal models, where a third engine would mark
`phase: Encode`).

This tells Modelplane to configure inference-aware routing optimized for
disaggregated serving. With `mode: PrefillDecode` modelplane assumes:

- Every Standalone or Leader member exposes an OpenAI endpoint on port 8000.
- Every Standalone or Leader member should be part of one GAIE InferencePool.
- The InferencePool should be exposed by a Gateway API HTTPRoute.

Disaggregated serving requires an endpoint picker (EPP) to pick a decode and a
prefill worker for each request. The decode worker runs a sidecar that dispatches
prefill to the chosen worker; the engines themselves transfer the KV cache over
their configured connector. Modelplane injects the sidecar, labels the pods as
either prefill or decode, and configures the endpoint picker accordingly.

### Scheduling

The fleet scheduler places each ModelReplica on one InferenceCluster. However
under this design the scheduler is really co-scheduling a set of engine
members to a single cluster. The unit of pool placement is the member: each
member may have a different (even disjoint) nodeSelector, and therefore may
need to be scheduled to a different node pool.

A member's cost is counted in nodes:

```
nodes = pods × engine copies
pods = 1 (Standalone or Leader), or Worker nodes
```

A member with no nodeSelector costs zero nodes. Its pods claim no devices, so
they don't occupy a node the way a pod that claims all of a node's GPUs does;
the cluster's scheduler packs them onto the gang's nodes alongside the pods
that do. At least one engine member must have a nodeSelector.

When placing an engine, the scheduler prefers one pool that satisfies every
member, with enough free nodes for all of them. A gang's members most likely
want to talk over the pool's fabric so splitting a gang across pools can
silently degrade interconnect. Only when no single pool satisfies all members
does the scheduler place each member on its own pool. A member with no
nodeSelector matches every pool, so it always lands with its gang.

A member's shape determines what a pool must provide to host it: enough nodes,
each with enough GPUs. The following table works this through for each
topology. Every row is one member. A ModelReplica with multiple engines
(disaggregation) or heterogeneous members spans multiple rows. All of a
ModelReplica's members must be co-scheduled onto one cluster.


| ModelReplica | Engine | Member | Pods | Copies | Required Nodes | Required GPUs/Node |
|---|---|---|---|---|---|---|
| Single GPU | main | Standalone | 1 | 1 | 1 | 1 |
| Single-node TP=4 | main | Standalone | 1 | 1 | 1 | 4 |
| Throughput | main | Standalone | 1 | 4 | 4 | 1 |
| Multi-node TP+PP | main | Leader | 1 | 1 | 1 | 8 |
| Multi-node TP+PP | main | Worker | 1 | 1 | 1 | 8 |
| Replicated gang | main | Leader | 1 | 3 | 3 | 8 |
| Replicated gang | main | Worker | 1 | 3 | 3 | 8 |
| API-server-only leader | main | Leader | 1 | 1 | 0 | 0 |
| API-server-only leader | main | Worker | 1 | 1 | 1 | 8 |
| Disagg | prefill | Standalone | 1 | 3 | 3 | 1 |
| Disagg | decode | Standalone | 1 | 2 | 2 | 2 |


The scheduler's pool choice is enforced, not advisory. Every pod carries a
Kubernetes nodeSelector on the `modelplane.ai/pool` node label, which Modelplane
stamps on every node it provisions (operators of BYO clusters must label their
nodes). Without it the cluster's scheduler could place a pod on any pool whose
devices satisfy its DRA claim, and the fleet scheduler's per-pool accounting
would drift from where pods actually run.

That accounting is node-granular and deliberately coarse. It assumes a member's
pods each occupy one node, which holds when a pod claims all of a node's
devices. A pod claiming fewer (say 1 GPU of 8) still charges a whole node, so
the scheduler may refuse a placement that would physically fit. It never
overcommits; device-level contention is left to DRA admission on the workload
cluster, which is authoritative.


### Examples

A worked `spec` for every topology we expect to support. Each notes how it
schedules, so the shape, the serving surface, and the placement are all visible
in one place.

#### Single GPU

A small model on one GPU. One engine, one member, one pod. Composes to a
Deployment fronted by a Service.

**Schedules as:** 1 node, 1 GPU.

```yaml
spec:
  serving:
    mode: Unified
  engines:
  - name: qwen3-8b
    members:
    - role: Standalone
      nodeSelector:
        devices:
        - name: gpu
          count: 1
          selectors:
          - cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("16Gi")) >= 0
      template:
        spec:
          containers:
          - name: engine
            image: vllm/vllm-openai:v0.11.0
            args:
            - --model=Qwen/Qwen2.5-7B-Instruct
```

#### Single-node tensor parallel

A model sharded across several GPUs on one node. Still one engine, one member,
one pod; the pod just requests more GPUs. Tensor parallelism is an engine flag.
The `nodeSelector` device `count` and `--tensor-parallel-size` agree because the
user keeps them consistent.

**Schedules as:** 1 node, 4 GPUs.

```yaml
spec:
  serving:
    mode: Unified
  engines:
  - name: llama-70b
    members:
    - role: Standalone
      nodeSelector:
        devices:
        - name: gpu
          count: 4
          selectors:
          - cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("40Gi")) >= 0
      template:
        spec:
          containers:
          - name: engine
            image: vllm/vllm-openai:v0.11.0
            args:
            - --model=meta-llama/Llama-3.3-70B-Instruct
            - --tensor-parallel-size=4
```

#### Multi-node tensor + pipeline parallel

A model too large for one node: tensor-parallel within each node, pipeline-
parallel across two. One engine with a Leader and one Worker composes to a
LeaderWorkerSet of two pods. The leader runs the engine's coordination
head and serves; the follower joins it, addressing the leader through
`MODELPLANE_LEADER_ADDRESS`. The asymmetry between running the head and joining
it lives in the two members' commands, which the user writes. Both members
want the same GPUs, so they repeat the same `nodeSelector` and the scheduler
places the whole gang on one pool.

**Schedules as:** 2 nodes, 8 GPUs each.

```yaml
spec:
  serving:
    mode: Unified
  engines:
  - name: llama-405b
    members:
    - role: Leader
      nodeSelector:
        devices:
        - name: gpu
          count: 8
          selectors:
          - cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("64Gi")) >= 0
      template:
        spec:
          containers:
          - name: engine
            image: vllm/vllm-openai:v0.11.0
            command:
            - /bin/sh
            - -c
            - >-
              ray start --head --port=6379;
              exec vllm serve
              --model=meta-llama/Llama-3.1-405B-Instruct
              --tensor-parallel-size=8
              --pipeline-parallel-size=2
              --port=8000
    - role: Worker
      worker:
        nodes: 1
      nodeSelector:
        devices:
        - name: gpu
          count: 8
          selectors:
          - cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("64Gi")) >= 0
      template:
        spec:
          containers:
          - name: engine
            image: vllm/vllm-openai:v0.11.0
            command:
            - /bin/sh
            - -c
            - exec ray start --address=${MODELPLANE_LEADER_ADDRESS}:6379 --block
```



#### Multi-node data + expert parallel

The same MoE model, data-parallel across two nodes. One engine, two pods,
eight GPUs per pod. The commands differ from the tensor+pipeline case: vLLM's
multi-node data-parallel launch uses a coordinator rather than a Ray head, and
the follower runs `--headless`. But the *shape* is the same engine. Modelplane
never learns the difference; it lays out a leader and a follower and runs the
commands the user wrote. This is the payoff of keeping coordination asymmetry in
the members' commands: a launch convention Modelplane has never heard of still
works.

**Schedules as:** 2 nodes, 8 GPUs each.

```yaml
spec:
  serving:
    mode: Unified
  engines:
  - name: deepseek-v3
    members:
    - role: Leader
      nodeSelector:
        devices:
        - name: gpu
          count: 8
          selectors:
          - cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("48Gi")) >= 0
      template:
        spec:
          containers:
          - name: engine
            image: vllm/vllm-openai:v0.11.0
            command:
            - /bin/sh
            - -c
            - >-
              exec vllm serve
              --model=deepseek-ai/DeepSeek-V3
              --tensor-parallel-size=1
              --enable-expert-parallel
              --data-parallel-size=16
              --data-parallel-size-local=8
              --data-parallel-address=${MODELPLANE_LEADER_ADDRESS}
              --data-parallel-rpc-port=13345
              --port=8000
    - role: Worker
      worker:
        nodes: 1
      nodeSelector:
        devices:
        - name: gpu
          count: 8
          selectors:
          - cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("48Gi")) >= 0
      template:
        spec:
          containers:
          - name: engine
            image: vllm/vllm-openai:v0.11.0
            command:
            - /bin/sh
            - -c
            - >-
              exec vllm serve
              --model=deepseek-ai/DeepSeek-V3
              --tensor-parallel-size=1
              --enable-expert-parallel
              --data-parallel-size=16
              --data-parallel-size-local=8
              --data-parallel-start-rank=8
              --data-parallel-address=${MODELPLANE_LEADER_ADDRESS}
              --data-parallel-rpc-port=13345
              --headless
```

#### Data parallel with an API-server-only leader

The previous example's leader does double duty: it runs the API server and the
DP coordinator *and* hosts eight engine ranks. At large DP sizes the API server
process becomes a bottleneck, and vLLM's [data parallel
deployment](https://docs.vllm.ai/en/latest/serving/data_parallel_deployment.html)
docs support running it on a node of its own: the head runs `vllm serve` with
`--data-parallel-size-local=0` (only the API server and coordinator, no engines)
while every rank runs on `--headless` workers.

That leader needs no GPUs, so it carries no `nodeSelector`. A member without
one claims no devices and costs no nodes: its pod rides along on the gang's
pool, packed onto the workers' nodes by the cluster's scheduler. The members of
this gang are heterogeneous, and the shape expresses that without any new
mechanism.

**Schedules as:** 1 node, 8 GPUs, plus a GPU-less leader pod on the same pool.

```yaml
spec:
  serving:
    mode: Unified
  engines:
  - name: deepseek-v3
    members:
    - role: Leader
      template:
        spec:
          containers:
          - name: engine
            image: vllm/vllm-openai:v0.11.0
            command:
            - /bin/sh
            - -c
            - >-
              exec vllm serve
              --model=deepseek-ai/DeepSeek-V3
              --enable-expert-parallel
              --data-parallel-size=8
              --data-parallel-size-local=0
              --data-parallel-address=${MODELPLANE_LEADER_ADDRESS}
              --data-parallel-rpc-port=13345
              --port=8000
    - role: Worker
      worker:
        nodes: 1
      nodeSelector:
        devices:
        - name: gpu
          count: 8
          selectors:
          - cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("48Gi")) >= 0
      template:
        spec:
          containers:
          - name: engine
            image: vllm/vllm-openai:v0.11.0
            command:
            - /bin/sh
            - -c
            - >-
              exec vllm serve
              --model=deepseek-ai/DeepSeek-V3
              --enable-expert-parallel
              --data-parallel-size=8
              --data-parallel-size-local=8
              --data-parallel-address=${MODELPLANE_LEADER_ADDRESS}
              --data-parallel-rpc-port=13345
              --headless
```

#### Disaggregated, single-node phases

Prefill and decode split into separate engines on their own hardware, serving the
same model: three single-GPU prefill copies and two two-GPU decode copies,
sized by each engine's `copies`. Decode gets more GPU per copy for KV-cache
capacity. The KV producer/consumer roles are engine flags. Everything that
differs between the phases, hardware and KV role and copy count, is carried by
the two engines.

**Schedules as:** 7 nodes co-located in one network domain on one cluster: 3×1
GPU for prefill and 2×2 GPU for decode, in potentially different pools.

```yaml
spec:
  serving:
    mode: PrefillDecode
  engines:
  - name: prefill
    phase: Prefill
    copies: 3
    members:
    - role: Standalone
      nodeSelector:
        devices:
        - name: gpu
          count: 1
          selectors:
          - cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("24Gi")) >= 0
      template:
        spec:
          containers:
          - name: engine
            image: vllm/vllm-openai:v0.11.0
            args:
            - --model=meta-llama/Llama-3.1-8B-Instruct
            - --kv-transfer-config={"kv_connector":"NixlConnector","kv_role":"kv_producer"}
  - name: decode
    phase: Decode
    copies: 2
    members:
    - role: Standalone
      nodeSelector:
        devices:
        - name: gpu
          count: 2
          selectors:
          - cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("40Gi")) >= 0
      template:
        spec:
          containers:
          - name: engine
            image: vllm/vllm-openai:v0.11.0
            args:
            - --model=meta-llama/Llama-3.1-8B-Instruct
            - --tensor-parallel-size=2
            - --kv-transfer-config={"kv_connector":"NixlConnector","kv_role":"kv_consumer"}
```

#### Disaggregated with a multi-node phase

The phases need not share a shape. Here prefill is single-node (one `Standalone`
member) while decode is a two-node gang (a `Leader` and a `Worker`), because
decode is the larger, latency-sensitive phase. An engine's gang structure is
orthogonal to the prefill/decode split, so a `Standalone` and a `Leader`/`Worker`
engine disaggregate together exactly as two single-pod engines would.

**Schedules as:** 3 nodes co-located in one network domain on one cluster: 1×8
GPU for prefill and 2×8 GPU for the decode gang.

```yaml
spec:
  serving:
    mode: PrefillDecode
  engines:
  - name: prefill
    phase: Prefill
    members:
    - role: Standalone
      nodeSelector:
        devices:
        - name: gpu
          count: 8
          selectors:
          - cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("141Gi")) >= 0
      template:
        spec:
          containers:
          - name: engine
            image: vllm/vllm-openai:v0.11.0
            args:
            - --model=meta-llama/Llama-3.1-405B-Instruct
            - --tensor-parallel-size=8
            - --kv-transfer-config={"kv_connector":"NixlConnector","kv_role":"kv_producer"}
  - name: decode
    phase: Decode
    members:
    - role: Leader
      nodeSelector:
        devices:
        - name: gpu
          count: 8
          selectors:
          - cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("64Gi")) >= 0
      template:
        spec:
          containers:
          - name: engine
            image: vllm/vllm-openai:v0.11.0
            command:
            - /bin/sh
            - -c
            - >-
              ray start --head --port=6379;
              exec vllm serve
              --model=meta-llama/Llama-3.1-405B-Instruct
              --tensor-parallel-size=8
              --pipeline-parallel-size=2
              --kv-transfer-config={"kv_connector":"NixlConnector","kv_role":"kv_consumer"}
    - role: Worker
      worker:
        nodes: 1
      nodeSelector:
        devices:
        - name: gpu
          count: 8
          selectors:
          - cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("64Gi")) >= 0
      template:
        spec:
          containers:
          - name: engine
            image: vllm/vllm-openai:v0.11.0
            command:
            - /bin/sh
            - -c
            - exec ray start --address=${MODELPLANE_LEADER_ADDRESS}:6379 --block
```

## Alternatives considered

### An engine-level nodeSelector

An earlier revision of this design put `nodeSelector` on the engine rather than
the member, reasoning that a gang's leader and followers are homogeneous.

This is usually true, but not always. A vLLM data-parallel head running
`--data-parallel-size-local=0` serves only the API server and coordinator. Under
an engine-level selector that leader claims a full GPU node it never uses. The
member-level selector costs some repetition in the homogeneous case and buys the
heterogeneous case: a claimless member, or members on different pools when their
requirements genuinely diverge.

### A flat array of pods

Rather than `spec.engines` being a list of engines that each hold a `members`
array, the engines could be one flat array of pods, each tagging the gang it
belongs to with a key.

The trouble is `copies`. Running N copies of a gang is one number, and in the
nested form it has an obvious home: `copies` on the engine, mapping straight to
the Deployment's or LeaderWorkerSet's replica count. A flat array has no object
to hang it on. Put it on the leader and "copies" on a single pod reads as
copies of that pod, not the gang; put it on every member and the values have
to agree; keep it in a side table keyed by gang name and the grouping is split
across two places. The engine object is the thing that's copied, so it's
where the copy count belongs.

### Letting the user configure the endpoint picker

A disaggregated deployment is fronted by an endpoint picker (EPP) with its own
image and `EndpointPickerConfig`. This design has Modelplane choose and
configure the picker, deriving its config from the `disaggregation` block. The
alternative is to expose the picker as a field the user fills in, the way they
write the engine container — an explicit picker template on `spec.serving`.

We chose to keep it out of the API. The picker is cluster-edge serving
infrastructure, closer to the gateway than to the model: which picker to run,
and the scoring config a prefill/decode split needs, follow from the shape
Modelplane already knows, so making every disaggregated deployment carry a
picker template is detail the user shouldn't have to author. It also keeps the
picker swappable — an implementation detail, like the choice between a Service
and an InferencePool. If a real need to tune the picker per deployment emerges,
`spec.serving` is where that knob would live; until then, Modelplane owns it.

### Referencing phase engines by name from serving

Instead of each engine marking its `phase`, `spec.serving` could carry a
`disaggregation` block naming the engines that play each part:

```yaml
spec:
  serving:
    mode: PrefillDecode
    disaggregation:
      prefillEngineName: prefill
      decodeEngineName: decode
```

An earlier draft of this design did exactly that, reasoning that an engine's
prefill/decode part is a routing concern, so it belongs with serving; and that
marking engines directly would make disaggregation something Modelplane infers
from whether engines happen to carry markers.

The inference objection dissolves once `mode` exists: `mode: PrefillDecode` is
the single explicit statement of intent either way, and `phase` is just the
marking that says which engine is which. What the name references add is
indirection and a failure class the markers can't have. A `prefillEngineName`
that names no engine, or a typo'd one, is a dangling reference that validation
has to join across two lists to catch. A `phase` on the engine can't dangle, and
the validation is simpler: under `PrefillDecode`, exactly one `Prefill` and one
`Decode`.

## Future improvements

### NVIDIA Dynamo

Dynamo's ([#111](https://github.com/modelplaneai/modelplane/issues/111))
deployment unit, the `DynamoGraphDeployment` (DGD), is strikingly close to the
ModelReplica this design proposes: an array of named components, each a pod
template with a replica count and a node count, composing to Deployments,
PodCliqueSets or LeaderWorkerSets, and routing.

Modelplane could compose a DGD, but it would be wrapping one near-equivalent in
another. A DGD does roughly the same thing as a ModelReplica.

Any value is more likely in Dynamo's lower-level components, consumed à la
carte. For example Modelplane could choose to compose a
[Grove](https://github.com/ai-dynamo/grove) PodCliqueSet instead of a
LeaderWorkerSet, or could use
[ModelExpress](https://github.com/ai-dynamo/modelexpress) to implement a
ModelCache.
