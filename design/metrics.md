# Metrics collection

**Status:** Draft
**Date:** August 2026
**Author:** Dennis Ramdass

This document proposes collecting metrics on every cluster, normalizing them to a
`modelplane_*` namespace, and aggregating them up to one Modelplane view at the control
plane. It builds on [design.md](./design.md) and addresses
[#269](https://github.com/modelplaneai/modelplane/issues/269).

## Summary

I propose four things.

**Collect on every cluster, always on.** Modelplane collects from every source it owns,
the engines, the endpoint pickers, and the substrate, with no per-deployment toggle.

**Normalize to `modelplane_*`.** Each engine names its metrics its own way (`vllm:*`,
`sglang:*`). The collector renames them to one Modelplane vocabulary, picked by an
engine-type label, so a dashboard reads Modelplane's names and not each engine's.

**Aggregate to one view.** Every cluster's series roll up to a single store at the
control plane, so one query covers the whole deployment instead of a per-cluster island.

**Collect with OpenTelemetry.** The collector is an OpenTelemetry collector, not a
per-cluster Prometheus. The section below gives the reasons.

Approving this means agreeing that normalization and aggregation are Modelplane's job
rather than the platform team's, that collection is always on, and that the collector is
OpenTelemetry.

## What to monitor

Four things are worth watching in a Modelplane deployment.

**Inference signal (data plane).** The engine's `/metrics`, the EPP's `llm_d_epp_*`, and
Envoy. TTFT, inter-token latency, tokens per second, queue depth, KV-cache occupancy, and
request and error rates per model. It answers "is my model serving well, and is it
saturated?"

**Substrate health.** The stack Modelplane installs on each workload cluster. Is the
gateway up, are cert-manager, the LeaderWorkerSet controller, the NVIDIA DRA driver, and
the pod scheduler healthy, are GPUs allocatable and gangs forming. "Is the machinery on
this cluster working?"

**Control-plane health.** Modelplane itself. Crossplane reconcile rates and errors,
function latency and panics, the fleet scheduler placing replicas, and XR `Ready`/`Synced`.
"Is the thing I operate working?"

**Fleet roll-up.** Across every cluster and deployment: total capacity, GPU usage,
degraded deployments, and cost.

All four are in the central view. The data plane and substrate are collected on each
cluster and aggregated up, the control plane is scraped at the center, and the fleet
roll-up is recording rules over the aggregate.

## Collect on every cluster

On each cluster Modelplane collects from every source it owns, with no opt-in or opt-out.
Three existing pieces make it cheap.

- **The serving label spans every shape.** `modelplane.ai/serving` is on standalone pods,
  LeaderWorkerSet leaders, and both prefill and decode engines, since it's the label the
  InferencePool selects on. One selector on it follows the shape, so leader/worker and
  prefill/decode need no special casing.
- **The stack already scrapes.** `compose-serving-stack` runs a metrics stack on every
  workload cluster and already scrapes the gateway's Envoy proxies, so adding a target is
  composition, not new infrastructure.
- **Modelplane owns the picker.** The EPP is Modelplane's own Deployment, so its metrics
  port and flags are ours to set.

A cluster-wide selector on `modelplane.ai/serving` covers every engine of every
deployment, so collection is a cluster property rather than something composed per
replica. A second selector covers the endpoint pickers, and a third the substrate
`compose-serving-stack` installs.

Scrape the engine port by name, not by number. The engine serves `/metrics` on its
serving port, which the backends name `http` in `native.py`, `llmd.py`, and `routing.py`.
It is `http` and not `metrics` because it is the one serving port, not a dedicated metrics
one. Under prefill/decode the decode engine serves on 8001 because the pd-sidecar takes
8000, so matching 8000 by number would scrape the sidecar. By name, the scrape follows the
engine on every pod.

## Capture from an opaque engine

Modelplane doesn't know which engine a deployment runs. The ML team supplies an image and
args, and serving stays opaque to the engine inside. Normalization is the opposite.
`vllm:time_to_first_token_seconds` and `sglang:time_to_first_token_seconds` fold into one
`modelplane_*` series only if something knows which engine produced them. So we need just
enough engine identity to pick a mapping, and no more.

The pattern is the one the [GAIE model-server-protocol](https://github.com/kubernetes-sigs/gateway-api-inference-extension/blob/main/docs/proposals/003-model-server-protocol/README.md)
already uses, and that Modelplane's routing depends on: read a label, don't detect the
engine. The GAIE endpoint picker carries metric mappings for vLLM and SGLang and selects
one from an engine-type label on the pod. If Modelplane runs that picker for
KV-cache-aware routing, the label already exists, and normalization reuses it. One label,
one mapping registry, two consumers.

- **A capture contract.** An engine exposes Prometheus `/metrics`. The required set
  follows the GAIE protocol and the OpenTelemetry GenAI conventions: TTFT, time per output
  token, queue depth, KV-cache occupancy. It's the metrics analogue of the OpenAI API
  contract Modelplane already assumes for serving.
- **Selection by label.** An engine-type label (`modelplane.ai/engine: vllm`) picks the
  `MetricMapping`. The ML team already chose the engine in the image. Naming its kind for
  metrics is one token and touches nothing about serving.
- **A registry of first-class resources.** Each mapping is a `MetricMapping`, a Modelplane
  kind, not a ConfigMap or an EnvironmentConfig. Modelplane installs the built-in ones
  (vLLM, SGLang, Triton/TensorRT-LLM). A platform team applies one more for a new or forked
  engine. Being typed, it validates on apply and appears under `kubectl get metricmappings`,
  and adding one is no fork and no Modelplane release.
- **Graceful degradation.** An unlabelled or unmapped engine still gets scraped and
  aggregated under its native names. The rename is skipped and Modelplane surfaces it
  ("no mapping for `X`") rather than guessing a mapping and reporting the wrong thing.

A `MetricMapping` is small: a selector for the pods it applies to, the source names, the
`modelplane_*` name each becomes, and the labels to keep or add. The vLLM one:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: MetricMapping
metadata:
  name: vllm
spec:
  selector:
    matchLabels:
      modelplane.ai/engine: vllm
  rename:
    vllm:time_to_first_token_seconds: modelplane_time_to_first_token
    vllm:inter_token_latency_seconds: modelplane_inter_token_latency
    vllm:num_requests_waiting: modelplane_requests_waiting
  labels:
    add: { engine: vllm }
```

`compose-serving-stack` reads every `MetricMapping` as a required resource, the same way
`compose-model-deployment` reads `InferenceCluster` and `ModelCache`. It renders them into
the collector's config, the ConfigMap the OTel collector loads on each cluster. The
`rename` map becomes transform-processor rules. A new engine is a new `MetricMapping`, not a
package change.

As engines emit the OpenTelemetry conventions directly (vLLM already emits OTLP traces,
and native OTLP metrics are in progress), each mapping shrinks toward identity and the
label becomes optional.

## Normalize to `modelplane_*`

The collector renames each engine's series to a `modelplane_*` surface with a consistent
label set (`engine`, `cluster`, `deployment`, `model`), so a dashboard reads one
vocabulary. Latency matters most. Measure it on P50/P90/P99 rather than the mean. The
distribution is right-skewed, so the mean hides the tail. Keep inference-only separate
from end-to-end.

| `modelplane_*` | vLLM | SGLang | TRT-LLM / Triton |
| --- | --- | --- | --- |
| `time_to_first_token` | `vllm:time_to_first_token_seconds` | `sglang:time_to_first_token_seconds` | derived |
| `inter_token_latency` | `vllm:inter_token_latency_seconds` | `sglang:inter_token_latency_seconds` | derived |
| `time_per_output_token` | `vllm:time_per_output_token_seconds` | `sglang:time_per_output_token_seconds` | derived |
| `request_prefill_time` | `vllm:request_prefill_time_seconds` | `sglang:per_stage_req_latency_seconds` | `nv_trt_llm_*` |
| `request_decode_time` | `vllm:request_decode_time_seconds` | per-stage | `nv_trt_llm_*` |
| `e2e_request_latency` | `vllm:e2e_request_latency_seconds` | `sglang:e2e_request_latency_seconds` | `nv_inference_request_duration_us` |
| `requests_waiting` | `vllm:num_requests_waiting` | scheduler waiting | `nv_trt_llm_request_metrics` |
| `kv_cache_usage` | `vllm:kv_cache_usage_perc` | token usage | TRT-LLM KV metrics |
| `prefix_cache_hits` | `vllm:prefix_cache_hits` | cache hit | n/a |
| `input_sequence_tokens` | `vllm:request_prompt_tokens` | prompt tokens | `nv_trt_llm_*` |
| `output_sequence_tokens` | `vllm:request_generation_tokens` | generation tokens | `nv_trt_llm_*` |
| `requests_total{outcome}` | `vllm:request_success_total` | request counters | Triton success/fail |

vLLM and SGLang map cleanly. Their names already nearly match, and both align to the
OpenTelemetry set. Triton and TensorRT-LLM expose batch-manager stats rather than native
TTFT and ITL histograms, so those rows are derived or wait on newer TensorRT-LLM metrics.
That gap is stated, not hidden.

Inter-token latency and time per output token stay separate. ITL is the per-token gap a
streaming user feels. TPOT is the amortized decode rate. Only TPOT is in the OpenTelemetry
set, so we carry both.

Under disaggregation the two roles show different health. A prefill worker is watched on
`modelplane_time_to_first_token` and prefill-queue depth. A decode worker is watched on
`modelplane_inter_token_latency` and `modelplane_kv_cache_usage`. A `role={prefill,decode}`
label carries the split, set from the same serving labels. The finer signals are the two
disaggregation bottlenecks, queued prefill tokens and in-flight decode KV tokens, reported
by the engine's scheduler loop.

These series feed more than dashboards. An autoscaler or an SLA planner reads the same
normalized latency, sequence-length, and queue series to size prefill against decode and
hold TTFT and ITL under target. NVIDIA's Dynamo Planner is the reference for such a
consumer. It samples on the order of seconds, faster than a dashboard needs, so the scrape
interval is a knob rather than a fixed value.

## Cluster scheduler metrics

The engine is not the only pluggable component on a workload cluster. The pod scheduler
that places the engine pods is one too. By default it is kube-scheduler. On a managed
cluster that scheduler sits in the provider's control plane and is often not scrapable. A
fleet running multi-node gangs or GPU fairness installs a gang scheduler instead, NVIDIA
KAI or Volcano. Those run as in-cluster pods the collector reaches. Modelplane treats such
a scheduler like an engine. A per-scheduler mapping, keyed by the one installed, normalizes
to a `modelplane_cluster_scheduler_*` surface. The name says cluster because a
future Modelplane fleet scheduler, placing replicas across clusters rather than pods across
nodes, would get its own `modelplane_fleet_scheduler_*` surface.

Five signals matter, and they answer whether a replica's pods reach GPUs and whether the
cluster's capacity is shared fairly across teams.

- **Pending or unschedulable work.** kube-scheduler's `scheduler_pending_pods{queue}`,
  Volcano's `volcano_unschedule_job_counts`, a KAI queue's waiting podgroups.
- **Scheduling latency.** `scheduler_scheduling_attempt_duration_seconds`,
  `volcano_e2e_job_scheduling_latency_milliseconds`.
- **Gang readiness.** Whether a podgroup's pods can all start at once,
  `volcano_queue_pod_group_pending_count` against `_running_count`. A gang that never forms
  is a stuck multi-node deployment.
- **Per-queue GPU allocation against quota.** `kai_queue_allocated_gpus`, Volcano's
  `volcano_queue_allocated_scalar_resources` against `_deserved_` and `_capacity_`, with
  `volcano_queue_overused` for fairness.
- **Preemptions and evictions.** `scheduler_preemption_victims`,
  `volcano_pod_preemption_victims`.

A scheduler's mapping is a `MetricMapping` like an engine's, and the degradation rule
carries over. An unmapped scheduler still gets scraped under its native names, and
Modelplane surfaces that rather than guessing.

## Aggregate to one view

Per-cluster collection is half the ask. Each cluster's series roll up to a single
Modelplane store at the control plane, which also scrapes the control plane's own metrics
(Crossplane, the functions, the fleet scheduler). One query then covers the whole deployment
rather than a per-cluster island an operator stitches together by hand. The fleet roll-up
(capacity, GPU usage, degraded deployments, and SLO attainment such as the fraction of
requests under a TTFT target) is recording rules over the aggregate.

The store is a single Prometheus-compatible instance with a short retention window. Scaling
it horizontally is out of scope here.

## Collector: OpenTelemetry

The collector is an OpenTelemetry collector. Three reasons settle it over a per-cluster
Prometheus.

- **The normalization target is a standard.** The OpenTelemetry GenAI conventions already
  define `time_to_first_token` and `time_per_output_token` as histograms with LLM-shaped
  buckets. `modelplane_*` adopts those names rather than inventing them.
- **The rename happens in the pipeline.** The collector scrapes each engine's `/metrics`
  with the Prometheus receiver. The transform processor renames the series to
  `modelplane_*`, keyed by the engine label, before forwarding up. A Prometheus stack
  pushes that rename into recording rules on every cluster and still needs its own
  federation.
- **One pipeline carries three signals.** Metrics, the #77 traces, and logs travel
  together, where a Prometheus stack is metrics only.

Each cluster reaches the center by pushing (remote-write) or the center pulls. On the pull
path Modelplane publishes the cluster's endpoint on the `InferenceCluster` status. On the
push path nothing is exposed outside the cluster.

## Architecture

```mermaid
flowchart LR
    subgraph icA["InferenceCluster A"]
        SA["engines / EPPs / substrate"]
        CA["OTel collector\n(scrape + rename to modelplane_*)"]
    end
    subgraph icB["InferenceCluster B"]
        CB["OTel collector"]
    end
    subgraph cp["control plane"]
        XP["Crossplane\n(functions, fleet scheduler, XRs)"]
        CENT["Modelplane store\n+ fleet roll-up rules"]
    end
    OP["operator\ndashboards + alerting"]
    SA --> CA
    CA -->|remote-write| CENT
    CB -->|remote-write| CENT
    XP -->|scraped by| CENT
    CENT --> OP
    classDef new fill:#ffb74d,stroke:#e65100,stroke-width:3px,color:#000;
    class CENT,CA,CB new
```

## Alternatives considered

### A Prometheus stack

Each cluster runs the kube-prometheus-stack `compose-serving-stack` already installs, with
composed `PodMonitor`s, and remote-writes to a central Prometheus-compatible store. It's
the incumbent and PromQL is standard. The collector wins for the reasons
above: it renames in the pipeline instead of through per-cluster recording rules, carries
traces and logs on the same path, and runs no full Prometheus per cluster. The central
store can still be Prometheus-compatible.

### Stop at per-cluster collection

An earlier shape collected on each cluster and left aggregation to the platform team,
publishing a Prometheus URL on the `InferenceCluster` status. Aggregating up to a
Modelplane view is the actual ask, so leaving it out means everyone rebuilds the same
fleet view by hand. Per-cluster collection stays, but as the bottom half of the pipeline,
not the whole of it.

### Raw engine metric names, no normalization

Aggregating the engines' native names (`vllm:*`, `llm_d_epp_*`) as-is is less work, but it
hands an operator a different vocabulary per engine and per component. The `modelplane_*`
surface is the point of aggregating in the first place: one set of names and labels for the
whole deployment.

### A PodMonitor per replica

`compose-model-replica` could compose a `PodMonitor` per replica, so collection comes and
goes with the deployment. With no opt-out and a cluster-wide store, that per-deployment
lifecycle buys nothing over one cluster-wide selector, and it composes N monitors where one
does the same job.

### A per-deployment opt-out field

An earlier shape put an `enabled` toggle on the deployment. It covers only the data plane
and asks an MD author to opt in or out of collection the platform team consumes. Always-on
collection fits the ownership better, so the toggle is dropped.

### Authenticate the EPP metrics endpoint

The EPP can serve `/metrics` behind controller-runtime auth (a `ClusterRole` with
`nonResourceURLs: /metrics` plus a bearer token). Since Modelplane owns the EPP args and
the endpoint carries non-sensitive routing stats reachable only in-cluster,
`--metrics-endpoint-auth=false` collects them with nothing to manage. Auth would add a
`ClusterRole` and a bearer token for no gain here.

## Interaction with #264

The [#264](https://github.com/modelplaneai/modelplane/issues/264) example documents the
manual path: a hand-written `PodMonitor` plus the operator wiring to consume it. Once
collection is composed and aggregated, that example drops the hand-written
`podmonitor.yaml`.

On upgrade, an existing hand-written `PodMonitor` has to be deleted, or it double-scrapes
the same pods alongside the composed one. This warrants a release note.
