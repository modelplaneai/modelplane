---
title: Deploy a Model
weight: 10
description: Deploy a model to the fleet, from one pod to disaggregated prefill and decode.
---
**API:** [`modelplane.ai/v1alpha1` · ModelDeployment]({{< ref "/reference/modeldeployments" >}})
<!-- vale write-good.Passive = NO -->
A `ModelDeployment` is the ML team's primary interface. You describe the model
you want served, the hardware it needs, and how many copies to run; Modelplane
schedules it onto matching clusters and keeps it running. You never pick a
cluster.

Modelplane is unopinionated about the engine itself. You bring the container and
its flags, and Modelplane shapes a serving topology around it. The engine flags
you write carry parallelism, quantization, and KV transfer, never injected by
Modelplane. By default Modelplane expects the engine to serve on port 8000 and
answer `GET /health` once it's ready. For an engine that doesn't, see
[Engine port and readiness](#engine-port-and-readiness).

As with a Kubernetes Deployment, `spec.replicas` sets the replica count and
`spec.template` describes each replica. Labels for the composed ModelReplicas
and ModelEndpoints go on `spec.template.metadata.labels`, and everything else
goes in `spec.template.spec`. The `spec.template.spec.engines` array describes
the topology through two choices:

- **One pod or a gang**: whether an engine is one `Standalone` pod or a
  `Leader` with one or more `Worker` pods coordinating across nodes.
- **Unified or disaggregated**: whether `spec.template.spec.serving.mode` keeps
  prefill and decode together (`Unified`, the default) or splits them across two
  engines (`PrefillDecode`).

How many of each to run is a separate question, covered in
[Sizing a deployment](#sizing-a-deployment).

## Single-node

The default, and what the [getting started tour]({{< ref "/getting-started" >}})
deploys. One `Standalone` member is one pod on one node, claiming that node's
GPUs through its `nodeSelector`. It's usually the right choice when a model fits
on one node. Within a node, tensor parallelism is an engine flag
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

A gang should use a [`ModelCache`]({{< ref "model-cache.md" >}}) via
`spec.template.spec.modelCacheRef`. Every pod in the gang then mounts the same
weights instead of each pulling its own.

```yaml {nocopy=true}
modelCacheRef:
  name: qwen3-coder         # recommended for gangs
engines:
- name: qwen3-coder
  members:
  - role: Leader
  - role: Worker
    worker:
      nodes: 1              # one worker pod per node
```

Modelplane injects the gang's coordination env, so a worker's command can find
the leader and its own rank. `$(MODELPLANE_LEADER_ADDRESS)` is the leader's
address on either [serving stack]({{< ref "/platform/inference-cluster.md#serving-stack" >}}).
`$(MODELPLANE_RANK)` is the pod's node rank (0 on the leader, `1..N` on the
workers) on Standard, but isn't injected on Dynamo yet
([#418](https://github.com/modelplaneai/modelplane/issues/418)). A gang on Dynamo
derives its rank from Grove's `GROVE_PCLQ_POD_INDEX` instead,
`$$((GROVE_PCLQ_POD_INDEX + 1))` on the workers (`$$` escapes Kubernetes' own
expansion, leaving `$((...))` for the shell).

A member's `env` can read pod fields through `valueFrom.fieldRef`, like setting
vLLM's `VLLM_HOST_IP` from `status.podIP`, which multi-NIC RDMA nodes need so the
engine binds the right interface instead of guessing it.

## Disaggregated serving

The prefill and decode phases have opposite hardware profiles, and on one engine
a prefill burst stalls the decodes already running. Set
`spec.template.spec.serving.mode: PrefillDecode` to run them as two engines, one
marking `phase: Prefill` and the other `phase: Decode`. The routing sequences
prefill then decode, and the engines move the KV cache between them. Each phase
can sit on the GPU class that suits it.

```yaml {nocopy=true}
serving:
  mode: PrefillDecode       # the two engines below are one P/D pair
  router: EndpointPicker    # required under PrefillDecode
  endpointPicker:           # required with router
    template: {...}         # the picker container
    config: {...}           # the picker configuration
engines:
- name: prefill
  phase: Prefill
- name: decode
  phase: Decode
```

A `PrefillDecode` deployment writes its own routing. The endpoint picker's
configuration must suit your engines, and the llm-d sidecar in the decode pod
must match their KV connector. Set `serving.router: EndpointPicker` and
`serving.endpointPicker`, as [Configure Routing]({{< ref "routing.md" >}})
describes. The disaggregated example below is a complete vLLM pair.

Disaggregation pays off for large models under load with strict latency targets
and long context. For small models or low traffic, the KV-transfer overhead
outweighs the benefit, so unified serving is the default.

With a NIXL connector, the engine image must include the **NIXL** KV-transfer
runtime. vLLM's `NixlConnector` (and SGLang's NIXL transfer backend) import the
`nixl` package. On an image without it, the engines crash at startup with
`NIXL is not available`. Recent vanilla `vllm/vllm-openai` images include NIXL,
so pin a current tag rather than an old one.

## Engine port and readiness

A serving pod receives requests on port 8000, and Modelplane checks that the
engine is ready with `GET /health` on that port. For an engine that listens on
another port, set `serving.port`. For one that reports readiness elsewhere, set
a `readinessProbe` on the engine container. It replaces Modelplane's check
whole, and Kubernetes defaults apply to the fields it leaves out, so the timeout
is 1 second unless you set `timeoutSeconds`. This engine listens on 8080 and
answers `/v1/models` once it's ready:

```yaml {nocopy=true}
serving:
  port: 8080
engines:
- name: main
  members:
  - role: Standalone
    template:
      spec:
        containers:
        - name: engine
          image: registry.example.com/my-engine:1.0
          readinessProbe:
            httpGet:
              path: /v1/models
              port: http
```

`serving.port` is where the InferencePool sends requests for every engine of
the deployment. When a container in front of the engine, such as the llm-d
sidecar, declares that port, the engine serves elsewhere, as
[Ports]({{< ref "routing.md#ports" >}}) describes.

## Requesting GPUs

You don't specify a cluster or a GPU model. Instead each member's `nodeSelector`
lists the hardware its pods need, and Modelplane finds a node pool that has it.
The platform team publishes node pools as `InferenceClass` resources, each
describing the devices on its nodes. Your request is matched against them.

A request has a name (`gpu`), the number of devices each pod needs (`count`),
and one or more `selectors` those devices must match:

```yaml {nocopy=true}
nodeSelector:
  devices:
  - name: gpu
    count: 1                # one GPU per pod
    selectors:
    - cel: |
        device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("40Gi")) >= 0
```

Each selector is a [CEL](https://cel.dev/) expression that returns true or
false for one device. The part in brackets, `"gpu.nvidia.com"`, is the GPU
vendor's driver. The fields after it, like `memory` or `architecture`, are what
the platform team published for that device. This one says "match a GPU whose
memory is at least 40Gi." A device has to match every selector in the request.
Give two selectors to mean "Hopper, with at least 80Gi."

### Requesting more than one device

`devices` is a list, so a member can request distinct kinds of hardware at
once, each as its own entry with its own `count` and `selectors`. A node pool
matches the member only when it satisfies each entry. This is how you ask for
both a GPU and a fast NIC on the same node:

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

Use `quantity()` to parse Kubernetes quantities like `"40Gi"`, and `semver()` to
parse versions like `"9.0.0"`. Both return values that support `compareTo`
(which orders two values), `isGreaterThan`, and `isLessThan`. Combine selectors
with the usual CEL operators (`==`, `!=`, `>=`, `&&`, `||`).

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

Selectors use the CEL that Kubernetes DRA accepts for device selectors,
including `quantity()` and `semver()` but not Kubernetes' other extension
libraries, such as regular expressions and IP address helpers. Selectors in
practice are attribute and capacity comparisons like those above.

### Seeing what's available

To see what you can match against, list the classes the platform team has
published and look at the devices each one declares:

```bash
kubectl get inferenceclass
kubectl describe inferenceclass gke-l4-1x-g2
```

The `describe` output shows each device's driver, attributes (like
`architecture`), and capacity (like `memory`), which are exactly the keys your
selectors read. If no published class has a device that matches a selector, the
deployment won't schedule.

## Sizing a deployment

A deployment's pod count comes from separate settings that you size
independently:

- **`spec.replicas`** stamps out whole copies of the entire topology. Each
  replica is a complete serving instance, and replicas usually land on different
  clusters. This is the scaling axis (see [Scaling](#scaling)).
- **`engines[].copies`** runs several identical copies of one engine within a
  replica, on the same cluster. It's a fixed number, sized once, never
  autoscaled. Copies make a replica more resilient within its cluster. A node
  failure drops one copy instead of taking the whole replica out of service,
  and the replica keeps [serving](#reading-a-deployments-status) while at least
  one copy of each engine is ready. The replica reports `Ready` only when every
  copy is ready. In disaggregated serving, copies also set the
  prefill-to-decode ratio.
- **`worker.nodes`** sets how many nodes one gang spans: a `Leader` plus that
  many `Worker` pods. It's how big one multi-node engine is.

## Scaling

`spec.replicas` is the only scaling axis. Each replica is a complete,
fixed-shape serving instance, so scaling adds or removes whole instances across
the fleet. Because the deployment exposes the Kubernetes scale subresource,
`kubectl scale` and KEDA work without anything extra. There's no in-cluster pod
autoscaling.

## Reading a deployment's status

A deployment's `Ready` means full capacity, so `kubectl wait
--for=condition=Ready` returns once every replica is up. `ReplicasServing` says
whether the model answers requests.

| Condition | True when |
|---|---|
| `ReplicasScheduled` | Modelplane has placed every replica in `spec.replicas`. |
| `ReplicasServing` | At least one replica is serving and routed. |
| `ReplicasReady` | Every replica in `spec.replicas` is at full capacity. |
| `Ready` | `ReplicasReady` is True and the referenced `ModelCache` exists. |

A deployment with `ReplicasServing=True` and `Ready=False` is degraded. It
serves from the replicas and copies that are up. `kubectl get` shows
`ReplicasServing` in the `SERVING` column, and a replica's `ModelServing` in
its own.

Each `ModelReplica` reports `ModelAccepted`, `ModelServing` and `ModelReady`.
Each implies the one before it, so the first False one says what the replica is
waiting on:

- `ModelAccepted`: the cluster accepted every engine's workload.
- `ModelServing`: at least one copy of every engine is ready, and the endpoint
  picker and routes in front of them are in place. The deployment routes to a
  replica only while it's serving.
- `ModelReady`: every copy is ready. Its message counts the ready copies, and
  its reason is `Degraded` while the replica serves below full capacity. The
  replica's `Ready` mirrors it.

To see which replica a deployment is waiting on:

```bash
kubectl describe modelreplica -n ml-team -l modelplane.ai/deployment=<name>
```

## Choosing a topology

| Topology | Use when | How you set it |
|----------|----------|----------------|
| Single-node | The model fits on one node's GPUs | One `Standalone` member (the default) |
| Multi-node | The model is too large for one node | A `Leader` and one or more `Worker` members, ideally with a `modelCacheRef` |
| Disaggregated serving | Large model, heavy load, strict latency, long context | `serving.mode: PrefillDecode` with two phase engines |

## Examples

{{< tabs >}}
{{< tab "Single-node" >}}
{{< manifests "concepts/model-deployment.yaml" >}}
{{< /tab >}}
{{< tab "Multi-node" >}}
{{< manifests "concepts/model-deployment-multinode.yaml" >}}
{{< /tab >}}
{{< tab "Disaggregated" >}}
{{< manifests "concepts/model-deployment-disaggregated.yaml" >}}
{{< /tab >}}
{{< /tabs >}}
<!-- vale write-good.Passive = YES -->
