# Serving stacks: Standard and Dynamo

**Status:** Accepted
**Date:** August 2026
**Author:** Nic Cope
**Issue:** [#111](https://github.com/modelplaneai/modelplane/issues/111)

## Summary

Modelplane composes and operates a serving layer across a fleet, but it isn't a
serving layer itself. Modelplane places, scales, caches, and fronts a model
across clusters and clouds. A serving stack owns what happens inside one
cluster, turning a stock engine into a routable serving instance. I propose we
add support for NVIDIA Dynamo as a serving stack.

The Dynamo stack composes one `DynamoGraphDeployment` per ModelReplica. Engines
keep their native images and launches, with a user-authored Dynamo native init
sidecar beside each serving member. The deployment selects `router: Dynamo`
and supplies its frontend template. The scheduler selects compatible Dynamo
clusters; Standard routers select Standard clusters.

This updates the original staged integration: explicit Leader/Worker templates
and native sidecars are available in the pinned 1.7 nightly. The sidecar path is
experimental; Modelplane does not substitute the in-process Dynamo engine runtime.

## Background

**Topology-aware placement and multi-role startup ordering.** Where a model's
shards land relative to the interconnect (NVLink domain → rack → zone) sets the
throughput it can reach. Modelplane has no model of that hierarchy today and
doesn't gang-schedule, so a large deployment can land split across domains or
stall half-placed. Grove and kai-scheduler offer topology-aware, all-or-nothing
placement.

**Faster cold starts (ModelExpress).** When Modelplane scales a deployment up,
each new replica loads its weights from storage. This is the slowest part of
coming online, and a source of contention when many replicas start at once.
ModelExpress lets a scaling-up replica skip that read entirely.

**Checkpoint/restore cold starts (Snapshot).** Bringing an engine online means
building the CUDA context, compiling kernels, and loading weights before it can
serve. Snapshot restores a checkpoint of a fully initialized worker instead,
cutting that startup to a restore.

**GPU-memory sharing and failover (GMS).** Modelplane has no fast-recovery story
today. When an engine crashes, its replacement reloads weights from disk. GMS
keeps a model's weights resident so a restarting or standby engine re-attaches
instead of reloading.

**Mid-stream request migration.** Today, if a worker dies mid-generation, the
best Modelplane can do is retry the whole request, and a long in-flight
generation is lost. Dynamo's frontend replays the delivered tokens to a new
worker and continues the stream.

Topology-aware placement and ModelExpress work against a stock, unmodified
engine, so Modelplane can adopt them without waiting for the DGD. Mid-stream
migration needs Dynamo's frontend-and-wrapped-engine architecture, which for us
means a composed DGD. GMS and Snapshot could work à la carte too, but we'd
rather get them from a DGD than rebuild their supporting machinery ourselves.

### Why now

Modelplane ran two serving stacks once.
[#15](https://github.com/modelplaneai/modelplane/pull/15) added Dynamo alongside
the KServe stack it already had, installing Dynamo's operator, etcd, and NATS and
composing a DynamoGraphDeployment.
[#44](https://github.com/modelplaneai/modelplane/pull/44) removed it a month
later, and [design.md](./design.md) records why under Multiple inference
orchestrators: a model's serving profile named the stack it wanted, so the API
could only hold what both supported, and that surface shrinks with each stack
added. [#99](https://github.com/modelplaneai/modelplane/pull/99) then dropped
KServe too, leaving Modelplane to compose the serving layer itself, the stack
this proposal calls Standard.

What's changed since is the shape of the API. [Unopinionated
ModelDeployments](./unopinionated-deployments.md) stopped describing a stack's
configuration and started describing an engine: `Standalone`, or a `Leader` and a
`Worker`, each with a command Modelplane passes through untouched. A
`ModelDeployment` names no stack, and Modelplane derives no engine flags from it.

That turns the question around. Instead of asking what every stack has in common,
we ask whether a given one can run what a `ModelDeployment` already describes:
two pod specs, two commands, and a way for a worker to find its leader. Grove and
kai-scheduler can, which is why the first phase needs no API change. A DGD can't
today, though the two additions below would be enough. KServe couldn't without
changing a good deal about KServe.

## Goals

**Give Modelplane users Dynamo capabilities.** Topology-aware gang scheduling
and faster cold starts now; failover, checkpoint restore, and mid-stream
migration as they follow. Adopting a serving stack that has them beats building
each one ourselves.

**Make the stack contract explicit.** A ModelDeployment is not independent of
its routing stack. `router: Dynamo` selects a Dynamo frontend and compatible
clusters; EndpointPicker and the default Unified picker select Standard.
Native engine launch and KV-transfer settings remain the user's responsibility.

**Keep the engine opaque.** The user writes one container named `engine` with
its `image`, `command`, and `args`, and what they write is what runs. We inject
no engine flags.

**Choose per cluster.** A fleet stands up Dynamo clusters and shifts deployments
onto them one at a time.

## Proposal

[Grove](https://github.com/ai-dynamo/grove) and
[kai-scheduler](https://github.com/NVIDIA/KAI-Scheduler) gang-schedule multi-node
engines, and [ModelExpress](https://github.com/ai-dynamo/modelexpress) streams
weights between them. All three work against a stock engine, so Modelplane can
adopt them under the model it already has.

### The cluster opt-in

A cluster runs one serving stack, set on its `InferenceCluster`: `Standard`
(the Modelplane-composed stack) or `Dynamo`, which installs the Dynamo operator,
Grove, KAI Scheduler and ModelExpress. Because the choice is
per-cluster, a fleet can take on Dynamo incrementally, standing up Dynamo
clusters and shifting deployments onto them cluster by cluster.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceCluster
metadata:
  name: eks-nvl72-us-east
spec:
  # The serving stack this cluster runs — the layer that plumbs a stock engine
  # into a routable serving instance. One per cluster:
  #
  #   Standard — Modelplane composes a Deployment or a LeaderWorkerSet and
  #     fronts it with Gateway API + an endpoint picker.
  #   Dynamo   — Modelplane composes DGDs with native engine sidecars and the
  #     user-authored frontend. The operator owns Grove workloads and runtime wiring.
  stack: Dynamo
  cluster:
    source: EKS
    eks:
      region: us-east-1
  nodePools:
  - name: gpu
    className: gb200-nvl72
```

### Grove and kai-scheduler

On a Dynamo cluster the operator lowers each DGD component into Grove workloads.
A multi-node engine declares distinct leader and worker role PodTemplates and
becomes a PodCliqueScalingGroup; single-node components use PodCliques. Minimum
available capacity joins the graph's base gang; capacity above each minimum
schedules independently. The DGD selects `Coherent` rolling updates, coordinating
prefill/decode availability across components without permanently coupling every copy.

Modelplane composes the DGD and device claim templates, not the underlying Grove
objects. It sets `nvidia.com/kai-scheduler-queue: modelplane` on the DGD so the
operator assigns generated cliques to Modelplane's queue.

`MODELPLANE_LEADER_ADDRESS` and `MODELPLANE_RANK` alias the operator's
`DYNAMO_LEADER_ADDRESS` and `DYNAMO_RANK`. Use them in command/args; dependent env
references are rejected because the operator sorts environment entries.

### ModelExpress

ModelExpress does two things: it runs a cache service that downloads and
deduplicates model weights, and it moves weights GPU-to-GPU between replicas over
NIXL. I propose we take only the second.

A ModelCache stays what it is on either stack, a per-cache PVC that Modelplane
hydrates itself. A Dynamo cluster additionally runs one ModelExpress server,
which brokers which replica holds a model in GPU memory and never handles the
weight bytes. The first replica loads from the PVC and publishes itself as a
source; later replicas pull from a peer's GPU. Modelplane injects the `MX_*` env
into every engine that references a cache.

Letting ModelExpress own the cache instead — one shared PVC it hydrates through
its own registry — would make a ModelCache mean something different depending on
which cluster it lands on, and put its weights out of reach of any engine not
using ModelExpress's loader. Keeping the PVC ours costs us the deduplicated pull
and buys a cache, and a deployment, that are portable across both stacks.

An engine opts in through its command: install the client, load with
`--load-format modelexpress`. Modelplane injects no `--load-format` of its own.
The command stays portable even so, because ModelExpress falls back to the
engine's native loader when it finds no server, no peer, or no fabric. On a
`Standard` cluster it just loads from the PVC.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: qwen3-8b
  namespace: ml-team
spec:
  replicas: 4
  template:
    spec:
      modelCacheRef:
        name: qwen3-8b          # a plain PVC on either stack; on Dynamo its replicas also seed each other
      engines:
      - name: qwen3-8b
        members:
        - role: Standalone
          nodeSelector:
            devices:
            - name: gpu
              count: 1
              selectors:
              - cel: device.driver == "gpu.nvidia.com"
            - name: efa                   # optional: fast fabric for P2P; without it, TCP or a storage read
              count: 1
              selectors:
              - cel: device.driver == "dra.net"
          template:
            spec:
              containers:
              - name: engine
                # The user writes the ModelExpress opt-in; Modelplane injects nothing into
                # command/args. It runs the server and supplies the MX_* env to reach it.
                image: vllm/vllm-openai:v0.23.0
                command: ["/bin/sh", "-c"]
                args:
                - >-
                  pip install --no-deps modelexpress &&
                  exec vllm serve Qwen/Qwen3-8B --load-format modelexpress
```

Behind the `modelCacheRef`, Modelplane provisions the RWX PVC and hydrates it as
it does on either stack, and binds an RDMA NIC if the engine requests one. The
cluster's one `modelexpress-server` is reachable through its own Service and
coordinates through ModelExpress's Kubernetes CRD backend; Modelplane injects
`MX_SERVER_ADDRESS`. A replica registers its VRAM and publishes its worker
endpoint as a `ModelMetadata` record for its peers to find.

The fast NIC is optional — a performance choice, not a requirement. On EKS it's
EFA, modeled as a claimable DRA device on the `InferenceClass` (driver `dra.net`,
via DraNet); an InfiniBand fabric would be a `Synthetic` placement-only device
instead. Either gives NIXL — ModelExpress's transfer layer — a fabric for the
fast path. Without one NIXL still moves weights peer-to-peer over TCP, and with
no peer at all a replica reads the PVC.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceClass
metadata:
  name: gb200-nvl72
spec:
  devices:
  - name: gpu
    claim: DRA
    driver: gpu.nvidia.com
    deviceClassName: gpu.nvidia.com
    count: 8
  # Optional fast fabric for ModelExpress's peer-to-peer transfer.
  - name: efa
    claim: DRA
    driver: dra.net
    deviceClassName: efa.networking.k8s.aws
    count: 16
```

## The composed DGD contract

The pinned operator accepts separate role PodTemplates and selects native engine
sidecar mode from `initContainers[name=runtime]` with `restartPolicy: Always` in
the serving member. Compute-only workers may omit it. The operator preserves
native engine image entrypoints, commands, arguments and probes and skips engine
backend launch hooks. Modelplane renames `engine` to DGD's `main` and adds its
placement, device claims, topology aliases and cache mounts. Other containers
and init containers pass through.

`serving.dynamo.frontend` owns replicas and a PodTemplate with a `main` container
and explicit command. Modelplane neither copies engine configuration to that
frontend nor chooses a router mode. `serving.dynamo.backendFramework` and
`runtimeVersionOverride` are optional, explicit values passed to DGD admission.
Modelplane does not infer either from images. Dated nightly runtime images need
the explicit version override; the author selects compatible runtime versions.

Backend and graph-topology edits replace the graph, releasing old GPUs rather
than requiring spare capacity before deletion. That can interrupt this replica.
Coherent rollout applies to mutable changes within one graph, not replacement.
GPU power-limit annotations conflict with Modelplane's DRA binding and are rejected.

## Alternatives considered

### A gang scheduler on the Standard stack

Gang scheduling and topology-aware placement are the one capability above with
credible alternatives. Volcano and Kueue can both gang-schedule a LeaderWorkerSet
against a hardware topology, and LWS integrates with each. JobSet can express
cross-role startup ordering.

So we could close that gap without Dynamo, by adding Volcano to the Standard
stack. A per-cluster stack means Standard can gain a gang scheduler without
touching a Dynamo cluster, and a fleet runs whichever fits each cluster.

That closes one of the gaps above and leaves the rest, and the ones it leaves are
the ones with no alternative at all. Nothing outside Dynamo keeps a model's
weights resident in GPU memory across an engine crash, and nothing short of a
frontend that owns the stream can resume a generation mid-flight. On a Dynamo
cluster gang scheduling arrives with those.

### A delegated engine

Before the plan above, I considered exposing the DGD directly in Modelplane's
API rather than composing it invisibly. In that design the ML team hands a whole
engine to Dynamo through a new member `role: Delegated` and a `stack: Dynamo`
selector, writing a Dynamo runtime image and letting Dynamo synthesize the
multi-node launch:

```yaml
engines:
- name: llama-405b
  copies: 1
  members:
  - role: Delegated          # NEW: hand the engine to a serving stack's orchestrator
    stack: Dynamo
    dynamo:
      nodes: 2               # total gang size; the stack owns the multi-node launch
    template:
      spec:
        containers:
        - name: engine
          image: nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.2.1
          command: ["python3", "-m", "dynamo.vllm"]
          args:
          - --model=meta-llama/Llama-3.1-405B-Instruct
          - --tensor-parallel-size=8
          - --pipeline-parallel-size=2
```

That alternative accepted the old DGD's single-template launch generation and
in-process runtime. It is not the chosen contract: native sidecars and explicit
role PodTemplates preserve user-authored engine launches without a new member
role. Routing still has an explicit stack choice through `router: Dynamo`.
