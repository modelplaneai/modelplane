# Metrics collection

**Status:** Draft. The MetricMapping kind is proven and unmerged; the rest is unbuilt
**Date:** August 2026
**Author:** Dennis Ramdass

This document proposes collecting metrics on every cluster, normalizing them to a
`modelplane_*` namespace, and aggregating them up to one Modelplane view at the control
plane. It builds on [design.md](./design.md) and addresses
[#269](https://github.com/modelplaneai/modelplane/issues/269).

## Summary

**Collect on every cluster.** Modelplane collects from every source it owns, the engines,
the endpoint pickers, and the substrate, with no per-deployment toggle. A fleet with no
destination configured collects nothing, since a collector nothing reads is cost with no
reader.

**Normalize to `modelplane_*`.** Each engine names its metrics its own way (`vllm:*`,
`sglang:*`). The collector renames them to one Modelplane vocabulary, picked by an
engine-type label, so a dashboard reads Modelplane's names and not each engine's. That
label is Modelplane's to stamp, from a new `engines[].type` on the `ModelDeployment`.

**Aggregate to one view.** Every cluster's collector pushes to the control plane, where
the series roll up and leave as one stream, so what consumes them answers across the fleet
rather than per cluster. Modelplane runs no store, so the view is whatever that destination
is.

**Collect with OpenTelemetry.** The collector is an OpenTelemetry collector, and it
replaces the kube-prometheus-stack `compose-serving-stack` installs today. The section
below gives the reasons and what covers each thing that stack did.

Three API changes carry it. Two are settled: a `MetricMapping` kind holding one engine's
renames, and `engines[].type` on a `ModelDeployment`. The third needs deciding before this
is built: where a fleet's telemetry destination lives, though what to call it is settled
below. Naming the engine port is a fourth change, to `compose-model-replica` rather than to
an API.

Approving this means agreeing that normalization and aggregation are Modelplane's job
rather than the platform team's, that collection is on for every source once a destination
exists, and that the collector is OpenTelemetry in place of the Prometheus stack we install
today.

## What to monitor

**Inference signal (data plane).** The engine's `/metrics`, the EPP's `llm_d_epp_*`, and
Envoy. TTFT, inter-token latency, tokens per second, queue depth, KV-cache occupancy, and
request and error rates per model. It answers "is my model serving well, and is it
saturated?"

**Substrate health.** The stack Modelplane installs on each workload cluster. Is the
gateway up, are cert-manager, the NVIDIA DRA driver, and the multi-node controller
healthy, are GPUs allocatable and gangs forming. "Is the machinery on this cluster
working?" Which components those are now depends on `InferenceCluster.spec.stack`: a
`Standard` cluster runs the LeaderWorkerSet controller, a `Dynamo` one runs Grove, the KAI
Scheduler, and a ModelExpress server.

**Control-plane health.** Modelplane itself. Crossplane reconcile rates and errors,
function latency and panics, the fleet scheduler placing replicas, and XR `Ready`/`Synced`.
"Is the thing I operate working?"

**Fleet roll-up.** Across every cluster and deployment: total capacity, GPU usage,
degraded deployments, and cost.

Every one of these is in the central view. The data plane and substrate are collected on
each cluster and aggregated up, the control plane is scraped at the center, and the fleet
roll-up is the collector's aggregation over the collected series.

## Collect on every cluster

On each cluster Modelplane collects from every source it owns, with no per-deployment
opt-in or opt-out. The switch is one level up, and at the fleet: with no destination
configured anywhere, no cluster composes a collector, because a collector nothing reads is
cost with no reader. The gate is the fleet destination rather than anything per cluster,
since one destination is what makes the fleet's series one view.

Once a destination exists, collection is on for everything Modelplane owns, and a
`ModelDeployment` author doesn't get a toggle over telemetry the platform team consumes.

The pieces are already there.

- **The serving label spans every shape.** `modelplane.ai/serving` is on standalone pods,
  LeaderWorkerSet leaders, Grove leader cliques, and both prefill and decode engines,
  since it's the label the InferencePool selects on. One selector on it follows the shape,
  so leader/worker, prefill/decode, and a Dynamo cluster's PodCliqueSets need no special
  casing. The Dynamo work added a fourth workload kind without touching this, which is the
  test this approach had to pass.
- **Modelplane owns the picker.** The EPP is Modelplane's own Deployment, so its metrics
  port and flags are ours to set.

The scrape config carries over as it stands. `compose-serving-stack` scrapes the gateway's
Envoy proxies today through the Prometheus chart's `additionalScrapeConfigs`, which is a
`kubernetes_sd_configs` block. That is the same format the collector's `prometheus`
receiver takes, so the Envoy target moves across verbatim rather than being rewritten.

A cluster-wide selector on `modelplane.ai/serving` covers every engine of every
deployment, so collection is a cluster property rather than something composed per
replica. A second selector covers the endpoint pickers, and a third the substrate
`compose-serving-stack` installs, which is now stack-dependent: the LeaderWorkerSet
controller on a `Standard` cluster, or Grove, the KAI Scheduler and the ModelExpress
server on a `Dynamo` one. The substrate selector has to follow the stack, and the
ModelExpress server is a Modelplane-owned component we have not yet checked for a metrics
endpoint.

Scrape the engine port by name, not by number, which needs a change first: no backend
names it today. `native.py`, `llmd.py`, and `grove.py` all compose
`{"containerPort": 8000}` with no `name`, so the `__meta_kubernetes_pod_container_port_name`
relabel the scrape config below keeps on has nothing to match. Naming it is a prerequisite
of this design rather than something it can assume.

Name it `http` and not `metrics`, because it is the one serving port rather than a
dedicated metrics one. The reason to go by name at all is prefill/decode: the decode
engine serves on `_DECODE_ENGINE_PORT` (8001) because the pd-sidecar takes 8000, so
matching 8000 by number scrapes the sidecar. By name, the scrape follows the engine on
every pod, on every backend.

## Capture from an opaque engine

Modelplane doesn't know which engine a deployment runs. The ML team supplies an image and
args, and serving stays opaque to the engine inside. Normalization is the opposite.
`vllm:time_to_first_token_seconds` and `sglang:time_to_first_token_seconds` fold into one
`modelplane_*` series only if something knows which engine produced them. So we need just
enough engine identity to pick a mapping, and no more.

The pattern is the one the [GAIE model-server-protocol](https://github.com/kubernetes-sigs/gateway-api-inference-extension/blob/main/docs/proposals/003-model-server-protocol/README.md)
uses: read a label, don't detect the engine. The GAIE endpoint picker carries metric
mappings for vLLM and SGLang and selects one from an engine-type label on the pod.

No such label exists in Modelplane today, and an ML team can't add one. The
`ModelDeployment` XRD rejects any label key under the reserved `modelplane.ai/` prefix, on
both the deployment and the member pod template, so `modelplane.ai/engine: vllm` fails to
apply. That prefix is Modelplane's to stamp, which is how `modelplane.ai/serving`,
`modelplane.ai/workload` and `modelplane.ai/pool` already reach pods.

So the engine type is a field, and the label is derived from it. An optional `type` on the
engine, naming the engine's kind, which `compose-model-replica` stamps onto the pod as
`modelplane.ai/engine` alongside the labels it already applies. One field feeds two
consumers, the picker for routing and the collector for normalization, and the reserved
prefix keeps meaning what it means.

The picker routes any engine. Its KV- and queue-aware scoring reads the engine's standard
metrics through the same mapping, so an engine without them still routes, only less
informed.

- **A capture contract.** An engine exposes Prometheus `/metrics`. The required set
  follows the GAIE protocol and the OpenTelemetry GenAI conventions: TTFT, time per output
  token, queue depth, KV-cache occupancy. It's the metrics analogue of the OpenAI API
  contract Modelplane already assumes for serving.
- **Selection by a stamped label.** `ModelDeployment` gains `engines[].type`, and
  Modelplane stamps `modelplane.ai/engine` from it. That label picks the `MetricMapping`.
  The ML team already chose the engine in the image, so naming its kind touches nothing
  about serving. It is a free-form string validated as a label value, not an enum: the
  registry below is open to a mapping for a forked or unreleased engine, and an enum would
  close the selector against the values that mapping needs to match. A value with no
  mapping degrades to passthrough, which is the behaviour below rather than an error.
- **A registry of first-class resources.** Each mapping is a `MetricMapping`, a Modelplane
  kind, not a ConfigMap or an EnvironmentConfig. Modelplane installs the built-in ones
  (vLLM, SGLang, Triton/TensorRT-LLM). A platform team applies one more for a new or forked
  engine. Being typed, it validates on apply and appears under `kubectl get metricmappings`,
  and adding one is no fork and no Modelplane release.
- **Graceful degradation.** An unlabelled or unmapped engine still gets scraped and
  aggregated under its own names. The rename is skipped and Modelplane surfaces it
  ("no mapping for `X`") rather than guessing a mapping and reporting the wrong thing.
  One caveat, measured rather than assumed: the collector's Prometheus exporter
  sanitizes `:` to `_`, so an unmapped `vllm:gpu_cache_usage_perc` is published as
  `vllm_gpu_cache_usage_perc`. Passthrough keeps the name and not the punctuation.

Selecting by label rather than by metric name looks redundant at first, because engine
metric names are already namespaced (`vllm:`, `sglang:`) and a flat name-to-name map would
rename them unambiguously with no selector at all. It is not, and the reasons are worth
writing down so the field is not optimized away later. Degradation above is label-based by
construction: reporting "no mapping for `X`" means reading a pod's claimed engine and
finding no mapping for it. Name matching cannot tell that apart from a successful rename of
nothing. The consistent label set is per pod, not per series, so name matching cannot
attach `engine` and `cluster` to the series a mapping does not rename. A forked
engine emits the upstream names while needing its own mapping, and two mappings matching
one name cannot be told apart without the pod. And not every name is namespaced:
kube-scheduler's are plain `scheduler_*`, so the scheduler section needs the selector
most of all.

In collector terms that makes the rename an OTTL transform gated on a resource attribute,
rather than the simpler metrics-transform processor, which matches on metric name only.
The pod label reaches OTTL as a resource attribute through the k8sattributes processor.

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
      modelplane.ai/engine: vllm      # stamped by Modelplane from engines[].type
  rename:
    vllm:time_to_first_token_seconds: modelplane_time_to_first_token
    vllm:inter_token_latency_seconds: modelplane_inter_token_latency
    vllm:num_requests_waiting: modelplane_requests_waiting
    vllm:gpu_cache_usage_perc: modelplane_kv_cache_usage
  labels:
    add: { engine: vllm }
```

`compose-serving-stack` reads every `MetricMapping` as a required resource, the same way
`compose-model-deployment` reads `InferenceCluster` and `ModelCache`. It renders them into
the collector's config, the ConfigMap the OTel collector loads on each cluster. The
`rename` map becomes transform-processor rules, applied to metrics from the pods the
`selector` matches. A new engine is a new `MetricMapping`, not a package change.

What that renders, with the vLLM mapping above as the only one installed:

```yaml
receivers:
  prometheus:
    config:
      scrape_configs:
      - job_name: modelplane-engines
        kubernetes_sd_configs: [{ role: pod }]
        relabel_configs:
        # every engine of every deployment, whatever its workload kind
        - source_labels: [__meta_kubernetes_pod_label_modelplane_ai_serving]
          action: keep
          regex: .+
        # the engine's own port, not a sidecar's
        - source_labels: [__meta_kubernetes_pod_container_port_name]
          action: keep
          regex: http

processors:
  # lifts the stamped engine label onto the series as a resource attribute
  k8sattributes:
    extract:
      labels:
      - { tag_name: engine, key: modelplane.ai/engine, from: pod }

  # one block per MetricMapping, gated on the engine it selects
  transform/vllm:
    metric_statements:
    - context: metric
      conditions:
      - resource.attributes["engine"] == "vllm"
      statements:
      - set(name, "modelplane_time_to_first_token")
          where name == "vllm:time_to_first_token_seconds"
      - set(name, "modelplane_inter_token_latency")
          where name == "vllm:inter_token_latency_seconds"
      - set(name, "modelplane_requests_waiting")
          where name == "vllm:num_requests_waiting"
      - set(name, "modelplane_kv_cache_usage")
          where name == "vllm:gpu_cache_usage_perc"

exporters:
  otlp:
    endpoint: ${MODELPLANE_OTLP_ENDPOINT}
    auth: { authenticator: bearertokenauth }
```

An unmapped engine matches the scrape config and no `transform` block, so it arrives under
its own names. That is the degradation above, and the structure gives it rather than a
rule having to.

The kind and the collector that consumes it were built in
[#412](https://github.com/modelplaneai/modelplane/pull/412): `compose-serving-stack` reads
every `MetricMapping` and renders it into the collector's transform rules, each gated on
the engine the mapping selects. That PR is closed unmerged, waiting on this design, and
the branch `dennis/metrics-poc` stays.

That was validated on a real GKE cluster with vLLM 0.23.0, which publishes 359 metric
lines. The mapped ones came back renamed and labelled with their engine, the rename
happening in place rather than alongside the originals, and the remaining 308 passed
through. The EPP half of this document is still unimplemented: the endpoint picker
exposes no metrics port today.

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
| `kv_cache_usage` | `vllm:gpu_cache_usage_perc` | token usage | TRT-LLM KV metrics |
| `prefix_cache_hits` | `vllm:prefix_cache_hits` | cache hit | n/a |
| `input_sequence_tokens` | `vllm:request_prompt_tokens` | prompt tokens | `nv_trt_llm_*` |
| `output_sequence_tokens` | `vllm:request_generation_tokens` | generation tokens | `nv_trt_llm_*` |
| `requests_total{outcome}` | `vllm:request_success_total` | request counters | Triton success/fail |
| `tokens_total{kind}` | `vllm:prompt_tokens_total`, `vllm:generation_tokens_total` | token counters | Triton token counts |

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
that places the engine pods is one too. By default it is kube-scheduler, which on a managed
cluster sits in the provider's control plane and is often not scrapable.

A gang scheduler runs as in-cluster pods the collector reaches, and on a `Dynamo` cluster
Modelplane now installs one itself. `compose-serving-stack` composes the KAI Scheduler and
the queues its pods schedule against, so KAI's series are first-party rather than something
a platform team might have brought: the queue is `modelplane` under an unbounded
`modelplane-root`, and every Grove pod carries `kai.scheduler/queue: modelplane`. A fleet
that brought Volcano itself is the same problem one mapping further out.

Modelplane treats a scheduler like an engine. A per-scheduler mapping, keyed by the one
installed, normalizes to a `modelplane_cluster_scheduler_*` surface. The name says cluster
because a future Modelplane fleet scheduler, placing replicas across clusters rather than
pods across nodes, would get its own `modelplane_fleet_scheduler_*` surface.

These signals answer whether a replica's pods reach GPUs, and whether a cluster's capacity
is shared fairly across teams.

- **Pending or unschedulable work.** kube-scheduler's `scheduler_pending_pods{queue}`,
  Volcano's `volcano_unschedule_job_counts`, a KAI queue's waiting podgroups.
- **Scheduling latency.** `scheduler_scheduling_attempt_duration_seconds`,
  `volcano_e2e_job_scheduling_latency_milliseconds`.
- **Gang readiness.** Whether a podgroup's pods can all start at once,
  `volcano_queue_pod_group_pending_count` against `_running_count`. A gang that never forms
  is a stuck multi-node deployment. On a Dynamo cluster this has to come from KAI, not from
  Grove: `PodCliqueSet.status.podGangStatuses` exists on the type and nothing writes it, so
  `availableReplicas` is the only signal Grove publishes, and it can't distinguish a gang
  that never formed from one still forming.
- **Per-queue GPU allocation against quota.** `kai_queue_allocated_gpus`, Volcano's
  `volcano_queue_allocated_scalar_resources` against `_deserved_` and `_capacity_`, with
  `volcano_queue_overused` for fairness.
- **Preemptions and evictions.** `scheduler_preemption_victims`,
  `volcano_pod_preemption_victims`.

A scheduler's mapping is a `MetricMapping` like an engine's, and the degradation rule
carries over, punctuation caveat included. An unmapped scheduler still gets scraped
under its own names, and Modelplane surfaces that rather than guessing.

## Aggregate to one view

Per-cluster collection is half the ask. Each cluster's collector sends its series up to the
control plane, which also collects the control plane's own metrics (Crossplane, the
functions, the fleet scheduler). One query then covers the whole deployment rather than a
per-cluster island an operator stitches together by hand.

### Getting the series across

Modelplane has exactly one connection to a workload cluster it can count on, and it runs
the wrong way for this. The control plane reaches the cluster's API server with the
kubeconfig `provider-kubernetes` holds. Nothing guarantees a path back, least of all from
an on-premise or neocloud GPU cluster behind a firewall. So transport is a real question
rather than a detail of the exporter.

**Every cluster pushes.** Each cluster's collector OTLP-exports to one collector at the
control plane, which is OpenTelemetry's own multi-cluster pattern. The cluster needs egress
and nothing inbound, and nothing on it is exposed. The control plane exposes one OTLP
endpoint, a Gateway API listener with TLS, the same kind of surface the inference gateway
already serves.

Credentials are already solved. `ModelCache` propagates an `authSecret` from the control
plane to every matched cluster so hydration can read a HuggingFace token. A bearer token or
client certificate for the OTLP endpoint travels the same way, through the same mechanism,
so this adds a Secret to propagate rather than a way to propagate Secrets.

One transport, and no field selecting it. An earlier draft offered a second mode for a
cluster with no egress, pulling through the API server proxy at
`/api/v1/namespaces/<ns>/services/<collector>:<port>/proxy/metrics` with the credential
Modelplane already holds, and a field on the `InferenceCluster` to declare which mode a
cluster used. Both are out. The second mode isn't only a second transport: a pulled cluster
exposes a scrape endpoint where a pushing one exports and exposes nothing, so it costs a
second collector configuration, a second exporter shape, a `ClusterRole` granting
`services/proxy`, and a field with a status mirror and a printer column. That is a lot of
surface for a fallback whose own analysis was that every series crosses an API server not
built to carry them, which caps what a cluster in that mode could send.

Deferring it costs nothing, which is what makes it easy. The field would be optional and
default to push, so adding it when a cluster that can't egress actually turns up is
additive and breaks nobody. Until one does, Modelplane supports one transport and says so.

**Pull direct** stays ruled out either way: a LoadBalancer or Ingress per cluster needs
inbound exposure on every GPU cluster, which pushing avoids.

If the fallback is ever built, the platform team declares the mode rather than Modelplane
detecting it. A composition function does no network probing, so nothing at compose time
knows whether a cluster can reach the destination. Writing the user-facing page is what
surfaced that: the draft said "Modelplane notices it can't reach out", which is the
behaviour a reader would want and not one anything here can implement.

The roll-up is a set of `modelplane_*` series over the aggregate: capacity, GPU usage,
cost, degraded deployments, and SLO attainment such as the fraction of requests under a
TTFT target. The control-plane collector produces them in memory, because each is a spatial
aggregation it already does. It sums gauges and counters across clusters and merges
per-cluster histograms into a fleet histogram. SLO attainment is a ratio of buckets in
that merged histogram when a boundary sits at the target, which is ours to set. So
Modelplane runs no store and the control plane stays stateless, as running in a Space
requires.

Computing a percentile value or answering an ad-hoc query is read-time work for whatever
consumes the export, a dashboard or an operator's own Prometheus-compatible backend.

### Exporters and destination

The exporter contract is OTLP, `otlp` over gRPC or `otlphttp`, taken by the gateway
collector and by any OpenTelemetry-compatible backend. `prometheusremotewrite` covers an
operator who wants the series in a Prometheus-compatible store instead. Vendor-specific
exporters are out of scope: an operator who wants one puts it behind the gateway, where one
configuration serves the fleet rather than one per cluster.

A Modelplane user does not write collector YAML. The destination is fleet-level
configuration, one endpoint to match one view, propagated to each cluster's `ServingStack`
and rendered into the collector's config there. Its credential is a Secret reference
resolved per cluster, as above.

Where that configuration lives needs deciding before this is built, not during. A field on
a fleet-level resource and a kind of its own both work, and the choice is the same one
`MetricMapping` faced: a typed kind validates on apply and lists under `kubectl get`, at
the cost of another kind.

What to call it is settled either way, and it's telemetry rather than metrics. An OTLP
endpoint carries metrics, logs and traces on the same wire, so the destination is already
signal-agnostic and a name like `MetricsDestination` would describe it narrower than it is.
`TelemetryDestination` as a kind, or `spec.telemetry.destination` as a field. The rename is
free while nothing has been built and costs a deprecation window and a migration for every
user once something has.

It blocks more than the implementation. Configuring a destination is the first thing a user
does, since nothing is collected until one exists, so it is the first thing the docs
describe. Its scope and owner go with the decision: the platform team owns it and one
destination serves the fleet, which points cluster-scoped rather than namespaced.

### Cardinality

Every label multiplies series, and an inference fleet has labels that churn. Dropping them
in the collector before export is cheaper than paying for them downstream and then
aggregating them away.

Dropped: `pod`, `pod_uid`, and `container_id`. Each is new on every restart, so each turns
a rolling update into a fresh set of series that never gets written to again. Kept:
`engine`, `cluster`, `model`, `deployment`, and `namespace`, which are the dimensions the
roll-up and every dashboard query group by.

The obvious processor is the wrong one. The `attributes` processor's `delete_key` removes a
label but leaves the series that collided on it as separate, undefined points rather than
merging them. Merging within a dropped dimension is `metricstransform` with an aggregation
action, which sums the colliding series into one.
Getting this wrong looks like it worked and reports nonsense.

Histogram buckets are the other cardinality cost, and not one to trim. `le` is what makes
the fleet histogram and the SLO ratio above possible, so the buckets stay as the GenAI
conventions define them.

## Collector: OpenTelemetry

The collector is an OpenTelemetry collector, and it replaces the kube-prometheus-stack
`compose-serving-stack` installs. The reasons, over keeping Prometheus:

- **The normalization target is a standard.** The OpenTelemetry GenAI conventions already
  define `time_to_first_token` and `time_per_output_token` as histograms with LLM-shaped
  buckets. `modelplane_*` adopts those names rather than inventing them.
- **The rename happens in the pipeline.** The collector scrapes each engine's `/metrics`
  with the Prometheus receiver and renames the series before forwarding. A Prometheus stack
  pushes that rename into recording rules on every cluster and still needs its own
  federation.
- **One pipeline carries three signals.** Metrics, the #77 traces, and logs travel
  together, where a Prometheus stack is metrics only.

An earlier draft argued Prometheus had to stay because its operator defines the
`PodMonitor` CRD this design composes against. That has the dependency backwards.
`PodMonitor` is a consequence of having chosen Prometheus, not a requirement of collection.
The collector's `prometheus` receiver does Kubernetes service discovery itself, so
discovery is a scrape config in the collector's own ConfigMap and no CRD is involved.

### What replaces the stack

Each thing kube-prometheus-stack does today has a receiver that does it.

| Today | Replacement |
|---|---|
| `PodMonitor` discovery | `prometheus` receiver with `kubernetes_sd_configs` |
| The Envoy scrape config | the same block, moved into that receiver |
| kube-state-metrics | `k8s_cluster` receiver |
| cAdvisor and kubelet | `kubeletstats` receiver |
| node-exporter | `hostmetrics` receiver |

That splits the collector in two, which the current design doesn't describe. Node-scoped
receivers (`kubeletstats`, `hostmetrics`, and the `filelog` receiver when logs follow) need
a collector on every node, so they run as a DaemonSet. Cluster-scoped ones (`k8s_cluster`,
and the engine and EPP scrapes) run as one Deployment. The engine scrape could run in
either; putting it in the Deployment keeps one scrape config rather than N node-local ones.

Whether the DaemonSet tier is in the first cut is worth deciding separately. Engines, the
EPP and `k8s_cluster` answer the inference and substrate questions above. Node CPU, memory
and disk answer a question a platform team may already have another agent for.

### What we give up

Ad-hoc PromQL against a local store. Today an operator can port-forward a cluster's
Prometheus and query it. After this there is no per-cluster store, so ad-hoc querying moves
to whatever consumes the export. That is the same trade the roll-up section already makes
for the center, applied to each cluster.

Which inverts what a fresh install gives you, and the inversion is worth stating rather
than discovering. Today Modelplane installs a working per-cluster store with no
aggregation. After this it aggregates across the fleet and stores nothing, so an install
with no destination configured collects nothing at all. That is the right trade for a fleet
and the wrong one for a first afternoon with Modelplane, which argues for the getting
started path shipping a destination rather than leaving the field empty.

The [#264](https://github.com/modelplaneai/modelplane/issues/264) guide. Its whole workflow
is a hand-written `PodMonitor` plus a port-forward to the in-cluster Prometheus, and both
halves go. Rewriting it against the composed collector is part of this work, not a
follow-up.

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
        CENT["control-plane collector\n+ in-memory roll-up"]
    end
    OP["operator\ndashboards + alerting"]
    SA --> CA
    CA -->|"push (OTLP)"| CENT
    CB -->|"push (OTLP)"| CENT
    XP -->|scraped by| CENT
    CENT --> OP
    classDef new fill:#ffb74d,stroke:#e65100,stroke-width:3px,color:#000;
    class CENT,CA,CB new
```

## Alternatives considered

### A Prometheus stack

Each cluster keeps the kube-prometheus-stack `compose-serving-stack` installs today, with
composed `PodMonitor`s, and remote-writes to a central Prometheus-compatible store. It's
the incumbent, PromQL is standard, and it keeps the local store an operator can query. The
collector wins for the reasons above: the rename happens in the pipeline rather than in
recording rules on every cluster, and metrics, traces and logs travel one path. It also
runs no Prometheus per cluster, where this shape runs one everywhere and needs its own
federation on top. If an operator wants a store the export still reaches a
Prometheus-compatible one, at the center, once.

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
goes with the deployment. It buys nothing over one cluster-wide scrape config, and composes
N objects where one does the same job. It also assumes the CRD, which goes with the
Prometheus stack.

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
manual path, and it is the published `collecting-engine-metrics` guide. Both halves of that
workflow go: the hand-written `PodMonitor`, because discovery moves into the collector's
scrape config, and the port-forward to the in-cluster Prometheus, because there is no
longer one. Rewriting the guide against the composed collector is part of this work.

A hand-written `PodMonitor` left in place is inert once the
Prometheus Operator is gone, so it stops working rather than double-scraping, which is
quieter and worse; it should be called out. And an operator relying on that Prometheus for
anything of their own loses it, so the release note has to say the store is going and where
the series go instead.
