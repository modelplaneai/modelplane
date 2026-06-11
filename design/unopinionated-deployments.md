# Unopinionated ModelDeployments

**Status:** Draft
**Date:** June 2026
**Author:** Nic Cope

This document proposes a revision to how a `ModelDeployment` describes its
workers and serving, so the API stays unopinionated about the inference engine
and the parallelism topology. The API describes a deployment's shape, not how
the model is run. It iterates on and supersedes parts of the base design in
[design.md](./design.md).

## Summary

I propose a ModelDeployment describe two things: the *shape* of its inference
engines (`spec.workers`) and how those engines are *served* at the cluster edge
(`spec.serving`).

`spec.workers` specifies one or more worker groups. A group may have either one
`Standalone` member, or one `Leader` and one or more `Worker` members. Each
group may have `replicas` - the _group_ can be replicated N times.

`spec.serving` specifies how the `InferenceCluster` exposes the worker groups as
an OpenAI compatible inference URL, suitable for the Modelplane control plane to
use as a `ModelEndpoint`.

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
  workers:
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
  serving:
    mode: Unified
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

A ModelDeployment describes two things: the *shape* of its inference workers
(`spec.workers`) and how they are *served* at an InferenceCluster's edge
(`spec.serving`). Modelplane composes a ModelReplica per `spec.replicas` and the
fleet scheduler places each on a cluster.

Under this design inference engine configuration is opaque to Modelplane, and
engines are deployed as flexibly shaped groups.

As a result of these choices:

- Modelplane can broadly and automatically support new inference engines.
- Modelplane can broadly and automatically support new inference topologies.
- Modelplane is loosely coupled to any particular serving stack (LWS,
  InferencePool, llm-d, etc).

The tradeoff is verbosity. Instead of specifying "topology: tensor-parallel",
the ModelDeployment author must describe the shape of the topology and the flags
for each inference worker.

This section covers the worker shape, the serving config, how a ModelReplica is
scheduled, and a worked example for every topology we expect to support.

### Workers

`spec.workers` describes a ModelReplica's topology as an array of worker groups.
A group is one serving unit: a standalone pod, or a gang of pods coordinating
across nodes. Each group may be replicated (but not autoscaled) within a
ModelReplica.

- `name`: identifies the group.
- `replicas`: how many copies of this group to run per `ModelReplica`.
- `members`: the group's pods. A group is either a single `Standalone` member or
  a `Leader` and a `Worker`; no other combination is valid.

Each member has:

- `role`: `Standalone` (default), `Leader`, or `Worker`.
- `count`: follower pods, for a `Worker` only. Each follower is one pod on one
  node, so `count` is also the number of follower nodes. Defaults to 1.
- `nodeSelector`: the per-node device request — what devices the member's engine
  pod needs. Its GPU `count` is the GPUs per node.
- `template`: a curated PodTemplateSpec carrying the engine container, its image,
  and its command and args.

A `Standalone` group composes to a Deployment. A `Leader`/`Worker` group composes
to a LeaderWorkerSet whose gang size is one leader plus the followers. Which
workload kind backs each is an implementation detail.

Modelplane expects the user to provide all the engine commands and flags needed
to form a topology. Some of those commands need to find other pods in the group:
in a multi-node tensor+pipeline gang, for instance, the Ray followers need the
Ray leader's address. Modelplane injects a small set of `MODELPLANE_` env vars
into the engine containers for this (today just `MODELPLANE_LEADER_ADDRESS`). For
the LWS backend it aliases the variable to `LWS_LEADER_ADDRESS`.

Note the relationship between `ModelDeployment` replicas, worker group replicas,
and worker count:

- `ModelDeployment` replicas specifies how many replicas of the entire model
  topology and serving apparatus should be stamped out. Replicas often run on
  different InferenceClusters.
- Group replicas specifies how many identical copies of a group each
  `ModelReplica` stamps out. Always within the same InferenceCluster, but
  potentially on different pools.
- Worker count specifies how big each replica of a worker gang is - how many
  workers per leader.

I think the need for `ModelDeployment` replicas is obvious. It's how Modelplane
autoscales a model, and how it spreads replicas across multiple clusters. So is
worker count: it's how you size a model that can't fit on a single node. Group
replicas is more subtle. I toyed with not exposing it at all, but I think we
need it for two things.

The first reason we need group replicas is disaggregated serving. With a prefill
group and a decode group, group replicas are how you control the
prefill-to-decode worker ratio.

The second reason is sizing the ModelReplica itself. Group replicas maps to the
underlying Deployment or LeaderWorkerSet's replicas. Without it, running ten
copies of a single-node model means ten ModelReplicas of one pod each.
Modelplane would then schedule many pod-sized ModelReplicas across the fleet,
placing individual pods on clusters: the Kubernetes scheduler's job, done with
less information than the Kubernetes scheduler has. Group replicas lets one
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
configuration from the shape of the `workers` block.

By default Modelplane assumes:

- Every Standalone or Leader member exposes an OpenAI endpoint on port 8000.
- Every Standalone or Leader member should be part of one Kubernetes Service.
- The Kubernetes Service should be exposed by a Gateway API HTTPRoute.

This is `Unified` serving in the example above (i.e. not disaggregated).

The only other valid configuration is:

```yaml
spec:
  serving:
    mode: Disaggregated
    disaggregation:
      prefillGroupName: prefill
      decodeGroupName: decode
```

This tells Modelplane to configure inference-aware routing optimized for
disaggregated serving. With `mode: Disaggregated` modelplane assumes:

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
under this design the scheduler is really co-scheduling a set of worker groups
to a single cluster. Each worker group may have different (even disjoint)
nodeSelectors, and therefore may need to be scheduled to different node pools.

A worker group's cost is counted in nodes:

```
nodes = pods × replicas
pods = 1 (Standalone), or 1 (Leader) + Worker count
```

A group's shape determines what a pool must provide to host it: enough nodes,
each with enough GPUs. The following table works this through for each topology.
Every row is one group. A ModelReplica with multiple groups (disaggregation)
spans multiple rows, because the scheduler places each group on its own pool.
All of the ModelReplica's groups must be co-scheduled onto one cluster.


| ModelReplica | Group | Pods | Replicas | Required Nodes | Required GPUs/Node |
|---|---|---|---|---|---|
| Single GPU | main | 1 | 1 | 1 | 1 |
| Single-node TP=4 | main | 1 | 1 | 1 | 4 |
| Throughput | main | 1 | 4 | 4 | 1 |
| Multi-node TP+PP | main | 2 | 1 | 2 | 8 |
| Replicated gang | main | 2 | 3 | 6 | 8 |
| Disagg | prefill | 1 | 3 | 3 | 1 |
| Disagg | decode | 1 | 2 | 2 | 2 |


### Examples

A worked `spec` for every topology we expect to support. Each notes how it
schedules, so the shape, the serving surface, and the placement are all visible
in one place.

#### Single GPU

A small model on one GPU. One group, one member, one pod. Composes to a
Deployment fronted by a Service.

**Schedules as:** 1 node, 1 GPU.

```yaml
spec:
  serving:
    mode: Unified
  workers:
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

A model sharded across several GPUs on one node. Still one group, one member,
one pod; the pod just requests more GPUs. Tensor parallelism is an engine flag.
The `nodeSelector` device `count` and `--tensor-parallel-size` agree because the
user keeps them consistent.

**Schedules as:** 1 node, 4 GPUs.

```yaml
spec:
  serving:
    mode: Unified
  workers:
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
parallel across two. One group with a Leader and one Worker composes to a
LeaderWorkerSet of two pods. The leader runs the engine's coordination
head and serves; the follower joins it, addressing the leader through
`MODELPLANE_LEADER_ADDRESS`. The asymmetry between running the head and joining
it lives in the two members' commands, which the user writes.

**Schedules as:** 2 nodes, 8 GPUs each.

```yaml
spec:
  serving:
    mode: Unified
  workers:
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
      count: 1
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

The same MoE model, data-parallel across two nodes. One group, two pods,
eight GPUs per pod. The commands differ from the tensor+pipeline case: vLLM's
multi-node data-parallel launch uses a coordinator rather than a Ray head, and
the follower runs `--headless`. But the *shape* is the same group. Modelplane
never learns the difference; it lays out a leader and a follower and runs the
commands the user wrote. This is the payoff of keeping coordination asymmetry in
the members' commands: a launch convention Modelplane has never heard of still
works.

**Schedules as:** 2 nodes, 8 GPUs each.

```yaml
spec:
  serving:
    mode: Unified
  workers:
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
      count: 1
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

#### Disaggregated, single-node phases

Prefill and decode split into separate groups on their own hardware, serving the
same model: three single-GPU prefill replicas and two two-GPU decode replicas,
sized by each group's `replicas`. Decode gets more GPU per replica for KV-cache
capacity. The KV producer/consumer roles are engine flags. Everything that
differs between the phases, hardware and KV role and replica count, is carried by
the two groups.

**Schedules as:** 7 nodes co-located in one network domain on one cluster: 3×1
GPU for prefill and 2×2 GPU for decode, in potentially different pools.

```yaml
spec:
  serving:
    mode: Disaggregated
    disaggregation:
      prefillGroupName: prefill
      decodeGroupName: decode
  workers:
  - name: prefill
    replicas: 3
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
    replicas: 2
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
decode is the larger, latency-sensitive phase. A group's gang structure is
orthogonal to the prefill/decode split, so a `Standalone` and a `Leader`/`Worker`
group disaggregate together exactly as two single-pod groups would.

**Schedules as:** 3 nodes co-located in one network domain on one cluster: 1×8
GPU for prefill and 2×8 GPU for the decode gang.

```yaml
spec:
  serving:
    mode: Disaggregated
    disaggregation:
      prefillGroupName: prefill
      decodeGroupName: decode
  workers:
  - name: prefill
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
      count: 1
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

### A flat array of pods

Rather than `spec.workers` being a list of groups that each hold a `members`
array, the workers could be one flat array of pods, each tagging the gang it
belongs to with a key.

The trouble is `replicas`. Running N copies of a gang is one number, and in the
nested form it has an obvious home: `replicas` on the group, mapping straight to
the Deployment's or LeaderWorkerSet's replica count. A flat array has no object
to hang it on. Put it on the leader and "replicas" on a single pod reads as
replicas of that pod, not the gang; put it on every member and the values have
to agree; keep it in a side table keyed by gang name and the grouping is split
across two places. The group object is the thing that's replicated, so it's
where the replica count belongs.

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

### Marking prefill and decode on the group

A group's role in a disaggregated deployment — prefill or decode — could be a
field on the group itself, rather than something `spec.serving.disaggregation`
declares by referencing groups by name.

It belongs with serving, not the group. A group's prefill/decode role doesn't
change what the group is: the pods, the gang, the hardware are identical either
way. The role only matters for how requests are routed across the groups — which
is a serving concern, not the worker shape's. Putting it on the group would also
make disaggregation something Modelplane infers from whether groups happen to
carry a role, rather than a single explicit statement; and it would bolt a field
onto every group that means nothing for the common, unified case. Keeping the
prefill/decode mapping in `spec.serving.disaggregation` leaves `workers`
describing shape and `serving` describing how that shape is served.

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
