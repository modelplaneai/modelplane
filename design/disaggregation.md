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
  # Top-level workers: the decode role.
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
  # Prefill role. Self-contained.
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

Disaggregation needs a router that sends a request to a prefill instance, then
to a decode instance holding the transferred KV cache. Modelplane uses the same
routing layer as unified serving: a Gateway API Inference Extension
`InferencePool` fronted by a swappable endpoint-picker (EPP), defaulting to the
llm-d inference-scheduler. The EPP's prefill/decode scorer sequences the two
phases, and its prefix-cache scorer still applies. Disaggregation runs on the
multi-pod (llm-d) path, which already goes through this routing layer, so it
adds no separate proxy.

A deployment with a `prefill` block selects the multi-pod backend even at
`pipeline: 1`, because disaggregation needs cross-pod coordination regardless of
the per-role topology.

## Constraints

These are documented now and enforced as the matching and validation surfaces
mature.

- **Co-location.** A replica's prefill and decode must be schedulable on one
  InferenceCluster. The fleet scheduler rejects the deployment if no matched
  cluster can host both roles.
- **Interconnect.** KV transfer needs NVLink within a node or RDMA/InfiniBand
  across nodes; over PCIe or ethernet it bottlenecks. It is required as a
  cluster or pool capability (e.g. `networkInterNode`) and matched the same way
  as other hardware requirements.
- **Connector and model compatibility.** Both roles run a compatible KV
  connector (`NixlConnector`, paired `kv_role`) on the same model and dtype,
  with compatible parallelism so the KV layout matches.
- **Both roles explicit.** A disaggregated deployment sets both `workers.count`
  and `prefill.workers.count`.

## When to use

Disaggregation pays off for large models under load with strict TTFT and ITL
targets, long context, and a fast interconnect, where prefill and decode load
are large enough and skewed enough to tune separately. For small models, short
context, or low traffic, the KV-transfer overhead outweighs the benefit;
aggregated serving, optionally with chunked prefill, is simpler and usually
faster. The decision is the operator's. Modelplane serves unified by default and
disaggregates only when a `prefill` block is set.

## Alternatives considered

### KServe prefill section

The original sketch (issue #34) expressed disaggregation through KServe's
`LLMInferenceService.prefill` section. Modelplane dropped KServe for a backend
dispatcher (native and llm-d), so disaggregation now lives in the `prefill`
block on the deployment and is emitted by the llm-d backend. The concept carries
over; the resource does not.

### A bespoke prefill/decode proxy

vLLM and Ray ship a small proxy that sequences prefill and decode. Running our
own proxy would work, but the GAIE `InferencePool` plus a swappable EPP is the
standard seam and already gives prefix- and KV-aware routing for unified
serving. Reusing it means one routing component for both unified and
disaggregated serving rather than a disaggregation-only proxy.

### A routing discriminator instead of a template

The EPP could be selected by a `picker` enum. Instead it is a curated PodSpec
subset (`routing.template`), the same shape and owner as the engine, defaulting
to the llm-d EPP and overridable by image and args. This avoids a discriminator
for a component that is really just a container, matching the engine convention
and design.md's preference against gratuitous discriminators.

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
