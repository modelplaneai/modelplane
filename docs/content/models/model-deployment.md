---
title: Deploy a Model
weight: 10
description: Deploy a model to the fleet with replica count and engine configuration.
---
**API:** [`modelplane.ai/v1alpha1` · ModelDeployment]({{< ref "/reference/modeldeployments" >}})
<!-- vale write-good.Passive = NO -->
A `ModelDeployment` is the ML team's primary interface. It carries everything
needed to deploy a model to the fleet: the inference engines, replica count, and
an optional [ModelCache]({{< ref "model-cache.md" >}}) reference for staged weights.


`spec.engines` is an array of inference engines. An engine is one serving unit:
a single `Standalone` member, or a gang of one `Leader` and one or more `Worker`
members (per `worker.nodes`) coordinating across nodes. Each member carries its
`nodeSelector` (the devices each of its pods needs from its node) and its
`template` (the engine container). A gang's members are usually homogeneous,
repeating the same `nodeSelector`, and the scheduler prefers to place them on
one node pool; a member may omit its `nodeSelector` entirely to claim no devices
(a coordinator-only leader), riding along on its gang's pool. An engine may set
`copies` to run several identical copies. Uses a fixed number, not a scaling knob.
Modelplane isn't opinionated about the engine itself: parallelism, quantization,
and KV transfer all live in the members' engine flags, written by you, never
injected by Modelplane.

When you create a `ModelDeployment`, the scheduler:

1. Discovers all ready `InferenceClusters` (filtered by `clusterSelector` labels
   if set). When the deployment references a [ModelCache]({{< ref "model-cache.md" >}})
   via `spec.modelCacheRef`, the candidate clusters for *new* replicas are
   further narrowed to the cache's own `clusterSelector` footprint, so a replica
   is never placed on a cluster the cache didn't stage to (where its PVC wouldn't
   exist). Existing replicas are always retained where they're pinned. If the
   referenced cache can't be read yet, the deployment reports
   `ModelCacheResolved=False` and holds off placing new replicas until it
   resolves, but keeps the replicas it already has - deleting a cache out from
   under a running deployment doesn't tear it down, since the cache is only read
   when weights are first staged.
2. Derives each member's node cost: a Standalone or Leader is one pod, Workers
   are `worker.nodes` worker pods, times the engine's `copies`. A member that
   claims no devices costs no nodes.
3. Matches each member's `nodeSelector` device requests against a candidate
   pool's `InferenceClass` devices, gated on the pool having enough available
   nodes, and co-schedules every member of a replica onto one cluster.
   Prefers one pool per engine and splits members across pools only when no
   pool satisfies them all.
4. Creates a `ModelReplica` for each selected cluster, carrying each member's
   pool and the resolved `claim: DRA` requests so the member's pods form DRA
   `ResourceClaims`.
5. Creates a `ModelEndpoint` for each replica, carrying the URL and rewrite path
   for routing.

A member's `template` is a curated subset of `PodTemplateSpec`. It carries a
single container named `engine`, the inference engine (like vLLM).

## Scaling

`ModelDeployment` replicas are the top scaling axis. Each `ModelReplica` is a
complete, fixed-shape serving instance. Scaling `spec.replicas` adds or removes
whole instances. There's no in-cluster pod autoscaling.

## Multi-node inference

When a model is too large to fit on one node's GPUs, make an engine a gang: give
it a `Leader` member and a `Worker` member, whose `worker.nodes` expands to that
many worker pods, one per node. Modelplane composes a LeaderWorkerSet of pods
that serve the model together. The leader runs the engine's coordination head and
serves; the workers join it, addressing the leader through the
`MODELPLANE_LEADER_ADDRESS` env var Modelplane injects. How the model is split
across the gang (tensor, pipeline, data, or expert parallelism) is up to the
engine flags you write on each member.

A member's `env` can read pod fields through `valueFrom.fieldRef`, like
setting vLLM's `VLLM_HOST_IP` from `status.podIP`, which multi-NIC RDMA nodes
need so the engine binds the right interface instead of guessing it.

Multi-node engines require a [ModelCache]({{< ref "model-cache.md" >}}) referenced
via `spec.modelCacheRef.name`, since every pod in the gang mounts it.

Disaggregation runs on the multi-node (llm-d) path. A request is routed to a
prefill instance and then to the decode instance holding its KV cache by the same
endpoint picker that fronts multi-node serving. Set `spec.serving.mode` to
`PrefillDecode` and mark each engine's `phase` as `Prefill` or `Decode` to enable
it. A deployment that omits `spec.serving` (or leaves `mode` as the default
`Unified`) is unified serving and is unaffected.

Disaggregation pays off for large models under load with strict latency targets
and long context. For small models or low traffic the KV-transfer overhead
outweighs the benefit, so aggregated serving (optionally with chunked prefill) is
the default.

Disaggregation requires the **engine image to provide the NIXL KV-transfer
runtime**. vLLM's `NixlConnector` (and SGLang's prefill/decode transfer) import
the `nixl` package, so disaggregated engines crash at startup with `NIXL is not
available` on an image that lacks it. Recent vanilla `vllm/vllm-openai` images
include NIXL, so pin a current tag rather than an old one. The engine image is
yours to choose, so this is a prerequisite Modelplane does not bundle for you.

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
