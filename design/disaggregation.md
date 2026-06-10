# Prefill/Decode Disaggregation

**Status:** Draft
**Date:** June 2026
**Author:** Dennis Ramdass

This document proposes prefill/decode disaggregation for Modelplane. It builds
on the base design in [design.md](./design.md) and the routing it relies on.

## Summary

LLM inference has two phases with opposite hardware profiles. Prefill processes
the whole prompt at once and is compute-bound; it sets time-to-first-token.
Decode generates one token at a time and is memory-bandwidth-bound; it sets
inter-token latency. Run on the same pods, a prefill burst stalls in-flight
decodes and neither phase can be tuned independently.

Disaggregation runs the two phases as separate pod sets. A prefill instance
processes the prompt and transfers its KV cache to a decode instance, which
generates the output. Modelplane expresses this with a `prefill` block on the
deployment: the top-level `workers` is the decode (or unified) role, and adding
a `prefill` block makes the deployment disaggregated.

It pays off for large models under load with strict TTFT/ITL targets, long
context, and a fast interconnect, where the two phases' loads are large and
skewed enough to tune separately; for small models, short context, or low
traffic the KV-transfer overhead outweighs the benefit and aggregated serving
(optionally with chunked prefill) is simpler. The choice is the operator's:
Modelplane serves unified by default and disaggregates only when a `prefill`
block is set.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: llama-405b
  namespace: ml-team
spec:
  replicas: 1
  modelCacheRef:
    name: llama-405b
  # Top-level workers: the decode role (memory-bandwidth-bound).
  workers:
    count: 3
    topology:
      tensor: 8
    template:
      spec:
        containers:
        - name: engine
          image: vllm/vllm-openai:v0.9.1
          args:
          - "--model=/mnt/models"
          - '--kv-transfer-config={"kv_connector":"NixlConnector","kv_role":"kv_consumer"}'
  # Decode's hardware. nodeSelector is required (a list of DRA device requests);
  # here a high-VRAM GPU plus the InfiniBand fabric the KV transfer needs.
  nodeSelector:
    devices:
    - name: gpu
      count: 8
      selectors:
      - cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("141Gi")) >= 0
    - name: nic
      count: 8
      selectors:
      - cel: device.attributes["nic.nvidia.com"].linkType == "infiniband"
  # Prefill role. Self-contained, with its own (compute-bound, single-GPU) hardware.
  prefill:
    workers:
      count: 5
      topology:
        tensor: 1
      template:
        spec:
          containers:
          - name: engine
            image: vllm/vllm-openai:v0.9.1
            args:
            - "--model=/mnt/models"
            - '--kv-transfer-config={"kv_connector":"NixlConnector","kv_role":"kv_producer"}'
    nodeSelector:
      devices:
      - name: gpu
        count: 1
        selectors:
        - cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0
      - name: nic
        count: 1
        selectors:
        - cel: device.attributes["nic.nvidia.com"].linkType == "infiniband"
```

## The prefill block

The `prefill` block is self-contained: its own `workers.count`, `topology`,
`template`, and `nodeSelector`. It repeats settings rather than inheriting from
the root, because explicit repetition is easier to reason about than an implicit
merge. This matches the shape design.md already sketches for disaggregation.

The prefill:decode ratio is the two `workers.count` values. It is a topology
parameter fixed per deployment, not a scaling knob, and both counts are
explicit. There is no default ratio, consistent with design.md avoiding
cross-resource defaulting.

Because the block carries its own `nodeSelector` and `topology`, an operator can
place prefill and decode on different GPU classes through the normal
capability-matching mechanism. Prefill is compute-bound and suits high-FLOPS
GPUs; decode is memory-bandwidth-bound and suits high-bandwidth GPUs. Modelplane
does not choose that hardware. It exposes the knob, and the in-cluster scheduler
places the pods, the same as the unified path. Prefill and decode of a replica
stay on one InferenceCluster, since KV transfer needs co-location, so distinct
hardware means different pools within that cluster rather than different
clusters.

A deployment without a `prefill` block is unified serving and is unaffected.

## KV cache transfer

The prefill engine produces the KV cache and the decode engine consumes it,
configured through the engine's `--kv-transfer-config` (`NixlConnector`, with
`kv_role` `kv_producer` on prefill and `kv_consumer` on decode). NIXL moves the
cache between the two over the fastest available interconnect.

KV cache size grows roughly linearly with input length, on the order of 0.1 GB
per 1K input tokens for an 8B model, so under load the transfer can reach tens
of GB/s. That is comfortable over NVLink within a node and over RDMA/InfiniBand
across nodes, but it saturates PCIe or plain ethernet. Where the engine supports
it, the transfer is hidden behind compute (layer by layer, asynchronous,
chunked), keeping the disaggregation overhead small.

## Routing

Disaggregation needs to route a request to a prefill instance, transfer the KV
cache, then have a decode instance generate from it. Modelplane does this with a
Gateway API Inference Extension (GAIE) `InferencePool` fronted by a swappable
endpoint-picker (EPP), defaulting to the llm-d inference-scheduler — no bespoke
proxy.

**One `InferencePool` fronts both roles.** Its selector matches a deployment's
prefill and decode pods alike; the EPP partitions them internally by a role
label (`llm-d.ai/role: prefill|decode`). Per request the EPP picks a decode pod,
then a prefill pod, and passes the decode pod the chosen prefill's address (an
`x-prefiller-host-port` header). A small routing sidecar on the decode pod
forwards the prompt to that prefill, which runs prefill and transfers its KV
cache over NIXL; the decode engine then generates. The EPP's prefix-cache scorer
still applies, so cache-aware placement carries over. (An earlier sketch assumed
a decode-only pool with the EPP pair-picking across two pools; the llm-d
mechanism is the single-pool, role-partitioned form above.)

The EPP itself is configured through `routing.template` — a curated PodSpec
subset, defaulting to the llm-d EPP and overridable by image and args, the same
shape and owner as the engine.

A deployment with a `prefill` block selects the multi-pod (llm-d) backend even
at `pipeline: 1`, because disaggregation needs cross-pod coordination regardless
of the per-role topology.

**Gateway.** `InferencePool` as an `HTTPRoute` backend needs a GAIE-conformant
gateway; core Envoy Gateway — which ServingStack installs today for plain
`HTTPRoute → Service` routing — does not serve it. Modelplane therefore runs
**Envoy AI Gateway** on the serving clusters: it layers on the same Envoy
Gateway data plane, so existing plain routes are unaffected. Unified serving
keeps its plain `Service` route for now; the `InferencePool`/EPP path is used by
disaggregated serving (and can later carry KV-/load-aware routing for unified
serving too).

## Constraints

These are documented now and enforced as the matching and validation surfaces
mature.

- **Co-location.** A replica's prefill and decode must be schedulable on one
  InferenceCluster. The fleet scheduler rejects the deployment if no matched
  cluster can host both roles.
- **Interconnect.** KV transfer needs NVLink within a node or RDMA/InfiniBand
  across nodes; over PCIe or ethernet it bottlenecks. The fabric is modeled as a
  `Synthetic` device on the InferenceClass (e.g. a `nic` device with
  `claim: Synthetic`, since no DRA driver claims it) and matched by a
  `nodeSelector` device request, the same way as a claimable GPU — see the
  example above.
- **Connector and model compatibility.** Both roles run a compatible KV
  connector (`NixlConnector`, paired `kv_role`) on the same model and dtype,
  with compatible parallelism so the KV layout matches.
- **Both roles explicit.** A disaggregated deployment sets both `workers.count`
  and `prefill.workers.count`.

## Alternatives considered

### Two ModelDeployments (one prefill, one decode)

Disaggregation could be expressed as two separate ModelDeployments — one for
prefill, one for decode — reusing existing primitives with no new `prefill`
block. It is close to what's proposed and appealingly minimal, but the single-MD
form is better on two counts:

- **Co-location.** With one MD the scheduler has everything it needs to place
  prefill and decode workers on the same cluster sensibly. With two MDs the
  author must either get crafty with `nodeSelector`s to force co-location, or we
  add cross-MD co-scheduling hints (and the scheduler would then have to reason
  about all MDs together).
- **It's still a model deployment.** A disaggregated MD is conceptually one
  thing — "a model deployment." A prefill-only MD isn't one; it's an
  implementation half that only makes sense paired with a decode MD.

### KServe prefill section

The original sketch (issue #34) expressed disaggregation through KServe's
`LLMInferenceService.prefill` section. Modelplane dropped KServe for a backend
dispatcher (native and llm-d), so disaggregation now lives in the `prefill`
block on the deployment and is emitted by the llm-d backend. The concept carries
over; the resource does not.

### A bespoke prefill/decode proxy

vLLM and Ray ship a small proxy that sequences prefill and decode. Running our
own proxy would work, but the GAIE `InferencePool` plus a swappable EPP is the
standard seam for prefix- and KV-aware routing. Reusing it means one routing
component we can extend to unified serving later, rather than a
disaggregation-only proxy.

### A routing discriminator instead of a template

The EPP could be selected by a `picker` enum:

```yaml
spec:
  routing:
    picker: llm-d        # enum: llm-d | ...; each value hard-codes an EPP
```

Instead it is a curated PodSpec subset, the same shape and owner as the engine —
defaulting to the llm-d EPP, overridable by image and args:

```yaml
spec:
  routing:
    template:
      spec:
        containers:
        - name: epp
          image: ghcr.io/llm-d/llm-d-inference-scheduler:v0.8.0   # default
          args: ["--config-file=/config/epp.yaml"]                # override to tune scorers
```

A discriminator would force Modelplane to enumerate and version every supported
picker; the template treats the EPP as what it is — a container — and lets a
user swap or tune it (different image, extra scorer args) without an API change,
matching the engine convention and design.md's preference against gratuitous
discriminators.

### Modelplane choosing per-role hardware

Modelplane could read the compute-bound and bandwidth-bound profiles and place
each role on a chosen GPU class. It does not. Placement stays a user-declared
`nodeSelector` resolved by the in-cluster scheduler, the same as every other
workload. Modelplane exposes the knob and guards correctness; it does not make
in-cluster scheduling decisions.

### An implicit prefill:decode ratio

A default ratio would let a deployment request disaggregation without prefill
counts. Both counts are required instead, so the topology is explicit and
nothing depends on cross-resource defaulting.
