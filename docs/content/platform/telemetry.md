---
title: Monitor the Fleet
weight: 37
aliases:
- /guides/collecting-engine-metrics/
- /guides/telemetry/
description: Collect normalized metrics across the fleet and send them to any backend the collector can export to.
---
<!-- vale write-good.Passive = NO -->

Modelplane runs an OpenTelemetry collector on every inference cluster. It
collects from every component Modelplane installs. This includes the inference
server engine, inference gateway and Envoy proxy, router, and the cluster's
GPU exporter - on a provisioned cluster Modelplane or the cloud installs one,
and on an existing cluster you provide it, as
[Requirements for an existing cluster]({{< ref "/platform/inference-cluster.md#requirements-for-an-existing-cluster" >}})
describes. It renames each component's series to a common
`modelplane_*` vocabulary and exports them to wherever you say - any backend the
collector has an exporter for, not only OTLP.

Modelplane allows you to write one destination for your metrics. You don't need
to manage per-deployment configurations or update your configuration when a
deployment changes. The collector finds pods itself, so a leader/worker split or
a prefill/decode pair doesn't need configuration of its own.

## Telemetry workflow

Every series has `cluster`, `job`, and `instance` labels of the target resource.
A series about a deployment also has `deployment`, `replica`, `namespace`,
`engine`, and `role` labels. On a prefill/decode deployment, an engine's series also
has a `phase` label, `Prefill` or `Decode`.

The gateway's `modelplane_frontend_*` series have no `deployment` label. They
carry the model instead: `gen_ai_original_model` is the model the caller sent,
`<namespace>/<service>`, and `gen_ai_request_model` is the deployment the gateway
sent the request to, `<namespace>/<deployment>`.
`modelplane_frontend_requests_active` is counted per `ModelService` instead. It
has a `service` label, and the same `namespace` label as an engine's series:
the team's namespace as it's mirrored on the inference cluster, such as
`mp-ml-team-51733`. The namespace in `gen_ai_original_model` is the team's own,
such as `ml-team`, so the two don't join on it.

Each replica publishes its own series, so combine them in your query. Which
combination is right follows from what the metric measures:

 - `sum by (deployment)`, for anything counted, such as requests, tokens, or queue depth.
 - `avg by (deployment)`, for a ratio.
 - `max by (deployment)`, for a saturation figure you alert on.
 - `sum by (le)` inside `histogram_quantile`, for a histogram.

To combine:

```promql
sum by (deployment) (modelplane_requests_running)
```

The replica is an index rather than a pod, so it's bounded by the replica count
and doesn't change across a restart or a rolling update. Group by `replica`, not
by `instance`:

```promql
# One line per replica, stable across rolling updates
max by (deployment, replica) (modelplane_kv_cache_utilization_ratio)
```
`instance` is the pod's address, which keeps two pods of the same replica apart. It
turns over on every rolling update, so group by `replica` rather than by `instance`.

A prefill/decode deployment runs some requests on both engines, and both count them.
The decode engines see every request with its whole prompt, so count requests,
tokens and how requests ended there:

```promql
sum by (deployment) (rate(modelplane_engine_requests_total{phase!="Prefill"}[5m]))
sum by (deployment) (rate(modelplane_engine_tokens_total{direction="input", phase!="Prefill"}[5m]))
sum by (deployment, reason) (rate(modelplane_responses_total{phase!="Prefill"}[5m]))
```

Some examples of the available metrics:

| Metric | Means |
| --- | --- |
| `modelplane_frontend_ttft_seconds` | Time to the first token, measured at the gateway. Streaming requests only. |
| `modelplane_frontend_tpot_seconds` | Time per output token, measured at the gateway. Streaming requests only. |
| `modelplane_frontend_request_duration_seconds` | What the caller waited, end to end |
| `modelplane_frontend_requests_active` | Requests open at the gateway, per `ModelService` |
| `modelplane_request_queue_seconds` | How long a request waited before the engine started |
| `modelplane_requests_waiting` | Queue depth per engine |
| `modelplane_kv_cache_utilization_ratio` | KV-cache occupancy, averaged over replicas |
| `modelplane_engine_tokens_total` | Prompt and generated tokens of the requests the engine finished, by `direction`: `input` or `output` |
| `modelplane_engine_requests_total` | Requests the engine finished |
| `modelplane_gpu_memory_used_bytes` | Framebuffer memory in use, per GPU |
| `modelplane_energy_joules_total` | Energy drawn since the driver last reloaded |

Latency appears twice on purpose. The `frontend_` series are what your caller experienced,
measured at the gateway. The engine's own series are what the engine spent. For
example, if the frontend metric is slow and the engine isn't, you can 
troubleshoot routing, queueing, or networking issues instead of the model.

{{< hint "note" >}}
The gateway records time to first token (TTFT) and time per output token (TPOT)
only for streaming responses. A deployment that doesn't serve streaming requests
has no series for either, so use `modelplane_frontend_request_duration_seconds`
for it.
{{</ hint >}}

Saturation gauges are per replica, so how you combine them decides what you see. Average
across a deployment to plan capacity, and take the maximum to alert: three replicas at 0.3
and one at 0.99 average to something comfortable while the fourth evicts and recomputes. A
high maximum beside `modelplane_requests_preempted_total` climbing is one replica thrashing. To alert on it:

```promql
max by (deployment) (modelplane_kv_cache_utilization_ratio) > 0.95
```

An engine works on a request until it finishes or is told to stop. When a caller
disconnects and the engine isn't told, the engine's running and waiting counts don't
drop while the gateway's count of open requests does, and new requests queue behind
output nobody reads. To compare the two for a deployment `qwen3-8b` that a
`ModelService` of the same name fronts, and that serves each request on one engine:

```promql
sum by (namespace) (modelplane_requests_running{deployment="qwen3-8b"})
  + sum by (namespace) (modelplane_requests_waiting{deployment="qwen3-8b"})
  - sum by (namespace) (modelplane_frontend_requests_active{service="qwen3-8b"})
```

Each series is summed on its own, so an engine missing one of them for a scrape,
such as just after it restarts, doesn't drop out of the result. The gateway and
the engines are scraped at different moments, so expect small differences that
come and go. A difference above 5 for 5 minutes is a reasonable place to start an
alert: the engine is doing work no caller is waiting for.

## Send telemetry to a destination

Create a `TelemetryDestination` for your OpenTelemetry-compatible endpoint:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: TelemetryDestination
metadata:
  name: default
spec:
  sinks:
  - name: primary
    type: otlphttp
    endpoint: https://otel.example.internal
```

`type` names a collector exporter, by the name OpenTelemetry gives it.

To authenticate with a bearer token, store the token in a Secret in
`modelplane-system` on your control plane. Create it once. Modelplane copies it to
every cluster running a collector, so you don't put the credential on each GPU
cluster yourself.

```shell
kubectl create secret generic telemetry-credentials \
  --namespace modelplane-system \
  --from-literal=token=<your-token>
```

Reference the Secret from the sink with `secretRef`, and set `auth.bearerTokenKey` to
the token's key in the Secret:

```yaml
spec:
  sinks:
  - name: primary
    type: otlphttp
    endpoint: https://otel.example.internal
    secretRef:
      name: telemetry-credentials
    auth:
      bearerTokenKey: token
```

Modelplane configures the collector to send the token with every export. Rotating the
token doesn't require a restart.

If you run Prometheus, export to your Prometheus endpoint instead and query the fleet there:

```yaml
spec:
  sinks:
  - name: prometheus
    type: prometheus_remote_write
    endpoint: https://prom.example.internal/api/v1/write
```

If you create more than one sink, all get the entire stream. Credentials are per
sink, so a vendor and your own Prometheus don't have to share a Secret.

```yaml
spec:
  sinks:
  - name: vendor
    type: otlphttp
    endpoint: https://otel.vendor.example
    secretRef:
      name: vendor-token
    auth:
      bearerTokenKey: token
  - name: prometheus
    type: prometheus_remote_write
    endpoint: https://prom.example.internal/api/v1/write
```

That is two copies of the fleet's metrics, so a vendor charging per sample charges for
both.

Sinks can also come from more than one `TelemetryDestination`. Modelplane concatenates
them, so a team adding an export creates its own object rather than editing one somebody
else owns. Sink names are what the collector calls its exporters, so they have to be
unique across destinations; where two collide, the destination whose name sorts first
keeps it and Modelplane says so on the `ServingStack`.

Anything else the exporter takes goes under `config`, passed through as you wrote it:

```yaml
  - name: vendor
    type: otlphttp
    endpoint: https://otel.vendor.example
    config:
      compression: gzip
      sending_queue:
        queue_size: 10000
      tls:
        ca_file: /etc/ssl/certs/internal.pem
```
Modelplane doesn't define a schema for an exporter's settings so anything under
the `config` is passed to the collector exactly as written. TLS, retries,
queueing, compression and headers all work and any new settings in the collector
are respected and the sink keeps working. 

To use an authentication scheme Modelplane doesn't compose, define the extension
yourself under `spec.extensions`. Then reference it by its key from the sink's
`config.auth.authenticator`. The `auth` block does the same wiring for you when
you use a bearer token.

```yaml
spec:
  sinks:
  - name: vendor
    type: otlphttp
    endpoint: https://otel.vendor.example
    secretRef:
      name: vendor-oauth
    config:
      auth:
        authenticator: oauth2client/vendor
  extensions:
    oauth2client/vendor:
      client_id: modelplane
      client_secret: ${env:CLIENT_SECRET}
      token_url: https://issuer.example/oauth2/token
```

Modelplane doesn't run any collectors until you create a
`TelemetryDestination`.

Creating a destination turns collection on everywhere at once, and there's no per-deployment
opt-out.

Each cluster's collector exports to your backend itself. A cluster needs a route to that
backend to report. Where a sink sets a `secretRef`, you create that Secret once on the
control plane and Modelplane copies it to every cluster running a collector, so the
credential is held on each of them.

## Computing rates, quantiles, and ratios

A collector transforms each measurement as it passes it on. It doesn't store past
measurements, so it can't compute rates or quantiles. Your backend does that. A
fleet-wide p99 of time to first token, which requires streaming traffic:

```promql
histogram_quantile(0.99, sum by (le) (
  rate(modelplane_frontend_ttft_seconds_bucket{gen_ai_request_model="ml-team/qwen3-8b"}[5m])))
```

Modelplane has no dashboards of its own. What it exports is counters and histogram buckets, and
your backend derives the rates and quantiles at query time. To precompute them instead,
export to Prometheus and write recording rules there.

The gateway's histograms have fixed upper buckets (AI Gateway v1.1.0):

- Request duration: 81.92 seconds
- Time to first token: 60 seconds
- Time per output token: 2.5 seconds

A quantile above the top bucket reads as that bound, no matter how long the request
took, so a p99 of `modelplane_frontend_request_duration_seconds` on long agentic
requests reads 81.92 seconds. For long requests, query the engine's
`modelplane_request_duration_seconds` instead. vLLM's buckets reach 7680
seconds.

## Engines

Modelplane already knows vLLM's and SGLang's metric names and renames them for you.
vLLM needs v0.9.2 or later, whose engine publishes the names Modelplane reads. SGLang
needs `--enable-metrics` to publish `/metrics` at all. Before v0.5.12 it also needs
`--collect-tokens-histogram` for the token histograms behind
`modelplane_engine_tokens_total` and `modelplane_engine_requests_total`. Without it
those two series are empty.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: my-sglang-model
  namespace: ml-team
spec:
  template:
    spec:
      engines:
      - name: engine
        members:
        - role: Standalone
          template:
            spec:
              containers:
              - name: engine
                image: lmsysorg/sglang:v0.5.10.post1-runtime
                command:
                - /bin/sh
                - -c
                - >-
                  exec python3 -m sglang.launch_server
                  --model-path <model>
                  --host 0.0.0.0
                  --port 8000
                  --enable-metrics
                  --collect-tokens-histogram
```

SGLang's prefix cache and retraction counters populate
`modelplane_prefix_cache_hits_total`, `modelplane_prefix_cache_lookups_total` and
`modelplane_requests_preempted_total`, so a prefix cache hit rate compares across both
engines. SGLang counts a request when it finishes, where vLLM counts it when it's
scheduled, so an aborted request is missing from SGLang's counts. SGLang's queue time
histogram uses different buckets from vLLM's, so only vLLM populates
`modelplane_request_queue_seconds`.

vLLM's `vllm:request_success_total` becomes `modelplane_responses_total`, the
requests the engine finished, with vLLM's `finished_reason` renamed to `reason`:
`stop`, `length` and `abort` at v0.9.2, and `error` and `repetition` as well from
v0.23.0. A client that disconnects should end its request as `abort`. If `abort`
stays at 0 while clients give up, the disconnect isn't reaching the engine, and the
engine keeps generating output nobody reads. On a prefill/decode deployment, read
`reason` on `phase!="Prefill"`: the prefill engine generates one token for each
request it runs, so it reports `length` for every one. SGLang doesn't publish a
counter of how its requests ended.

SGLang does count the HTTP responses it sends, by status code. Its counter,
`sglang:http_responses_total`, becomes `modelplane_engine_responses_total`, with the
status code in a `status` label. The codes an OpenAI-compatible server returns become
`ok`, `client_error` and `server_error`, the classes the gateway uses, and a rarer
code keeps its number. The series keeps SGLang's `endpoint` label, because SGLang
counts its `/health` and `/metrics` responses too, so filter on it. The requests
SGLang has open, `sglang:http_requests_active`, become `modelplane_engine_requests_active`,
with the same `endpoint` label. The vLLM mapping populates neither.

The built-in mappings cover what every deployment needs, from the engine versions
the recipes pin, which is what they're tested against: vLLM v0.9.2, v0.23.0, v0.25.1
and v0.27.1, and SGLang v0.5.10 and v0.5.12. Metrics for a feature you turn on, such
as speculative decoding or a KV offload tier, aren't among them. Write a
`MetricMapping` for those alongside the deployment that turns the feature on. The
speculative decoding variant of the [Qwen3-8B recipe]({{< ref "/recipes/qwen3-8b.md#speculative-decoding" >}})
includes one. SGLang's `spec_accept_rate` and `spec_accept_length` aren't mapped,
because what `spec_accept_rate` measures changed between v0.5.10 and v0.5.12. For a
KV offload tier, which vLLM runs from v0.27.1 with
`--kv-offloading-backend`:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: MetricMapping
metadata:
  name: vllm-kv-offload
spec:
  metrics:
  - from: vllm:kv_offload_store_bytes_total
    to: modelplane_kv_offload_store_bytes_total
  - from: vllm:kv_offload_load_bytes_total
    to: modelplane_kv_offload_load_bytes_total
  - from: vllm:kv_offload_store_time_total
    to: modelplane_kv_offload_store_seconds_total
  - from: vllm:kv_offload_load_time_total
    to: modelplane_kv_offload_load_seconds_total
```

SGLang publishes `modelplane_requests_running` and `modelplane_requests_waiting` more
than once per engine under `--enable-priority-scheduling`, once in total and once per
priority, and once per tensor-parallel rank under
`--enable-metrics-for-all-schedulers`. With either flag, a sum over them counts each
request more than once. Keeping only one of those series is tracked in
[#549](https://github.com/modelplaneai/modelplane/issues/549).

Because the gateway, not the engine, measures the `modelplane_frontend_*` series, they
work without configuration for an engine Modelplane has never seen.

To normalize that engine's own metrics as well, create a `MetricMapping`:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: MetricMapping
metadata:
  name: my-engine
spec:
  metrics:
  - from: my_engine_queued_requests
    to: modelplane_requests_waiting
  - from: my_engine_kv_transfer_ms
    fromUnit: Milliseconds
    to: modelplane_request_kv_transfer_seconds
```

Modelplane renders every mapping into every cluster's collector, so you write one once.
`from` is the name your engine emits and `to` is what Modelplane calls it.

Modelplane leaves the combining to your backend. A scrape of one replica is one batch, so
a collector that added them up would be summing readings taken at different moments, and
two readings of one cumulative counter come to twice the traffic that happened. Your
backend stores every replica's series and combines them at query time.

Say `fromUnit` whenever the engine measures in something other than the unit the name
claims, and Modelplane converts to the base one. Skipping this is the expensive mistake here:
a series named `_seconds` that holds milliseconds reads a thousand times fast, and nothing
downstream can tell.

Rename only where the measurements agree. If two engines' histograms share a name and
their buckets disagree, a quantile over them is wrong rather than approximate.

### Examples

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: MetricMapping
metadata:
  name: my-engine
spec:
  metrics:
  # A plain rename.
  - from: my_engine_queued_requests
    to: modelplane_requests_waiting

  # A unit conversion. The engine reports milliseconds; the name says seconds.
  - from: my_engine_kv_transfer_ms
    to: modelplane_request_kv_transfer_seconds
    fromUnit: Milliseconds

  # A request count taken out of a duration histogram. The histogram
  # keeps its own name; this adds a counter beside it.
  - from: my_engine_request_duration_seconds
    part: Count
    to: modelplane_requests_total

  # Two counters folded into one name, told apart by a fixed label.
  - from: my_engine_prompt_tokens_total
    to: modelplane_tokens_total
    labels:
    - name: direction
      value: input
  - from: my_engine_generated_tokens_total
    to: modelplane_tokens_total
    labels:
    - name: direction
      value: output

  # A label the engine already emits, renamed and its values translated.
  - from: my_engine_finished_requests_total
    to: modelplane_responses_total
    labels:
    - name: reason
      from: finish_reason
      values:
        eos: stop
        max_tokens: length
```

## GPUs

The GPU series come from a [DCGM
exporter](https://github.com/NVIDIA/dcgm-exporter), not from the engine: a GPU
belongs to a node, and its series have a `node` label rather than a
deployment. The collector discovers the exporter by its pod - an `app` or
`app.kubernetes.io/name` label containing `dcgm`, with a container port named
`metrics` - and scrapes it directly, not through Prometheus.

Where the exporter comes from depends on the cluster:

- **A cluster Modelplane provisions** runs one already: the serving stack
  installs an exporter wherever the cloud doesn't provide its own.
- **An existing cluster provides its own**, as [Requirements for an existing
  cluster]({{< ref "/platform/inference-cluster.md#requirements-for-an-existing-cluster" >}})
  describes. A GPU Operator install runs one by default; a cluster without
  one needs NVIDIA's dcgm-exporter chart, tolerating your GPU taint. The
  chart's pod already has the label and port the collector matches on.

Don't run two exporters: the collector scrapes every matching pod and would
collect each GPU twice.

The exporter's metrics ConfigMap decides which DCGM fields it publishes.
Modelplane maps eight to the `modelplane_*` surface and converts units where
DCGM reports in different ones:

| DCGM field | Becomes | Unit |
| --- | --- | --- |
| `DCGM_FI_DEV_FB_USED` | `modelplane_gpu_memory_used_bytes` | MiB, converted to bytes |
| `DCGM_FI_DEV_FB_FREE` | `modelplane_gpu_memory_free_bytes` | MiB, converted to bytes |
| `DCGM_FI_PROF_GR_ENGINE_ACTIVE` | `modelplane_gpu_compute_active_ratio` | ratio |
| `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` | `modelplane_gpu_tensor_active_ratio` | ratio |
| `DCGM_FI_PROF_DRAM_ACTIVE` | `modelplane_gpu_memory_bandwidth_ratio` | ratio |
| `DCGM_FI_DEV_GPU_TEMP` | `modelplane_gpu_temperature_celsius` | Celsius |
| `DCGM_FI_DEV_POWER_USAGE` | `modelplane_gpu_power_watts` | watts |
| `DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION` | `modelplane_energy_joules_total` | millijoules, converted to joules |

Recent default exporter configurations publish all eight, but older ones left the
`DCGM_FI_PROF_*` profiling fields out. Check what yours publishes - a
missing field means its `modelplane_*` series is silently empty:

```bash
kubectl port-forward -n <exporter-namespace> ds/<exporter> 9400:9400 &
curl -s localhost:9400/metrics | grep -c -E \
  'DCGM_FI_DEV_FB_(USED|FREE)|DCGM_FI_PROF_(GR_ENGINE_ACTIVE|PIPE_TENSOR_ACTIVE|DRAM_ACTIVE)|DCGM_FI_DEV_(GPU_TEMP|POWER_USAGE|TOTAL_ENERGY_CONSUMPTION)'
```

If fields are missing, extend the exporter's metrics ConfigMap - on a GPU
Operator install that's the `dcgmExporter.config` chart value. The
configuration Modelplane installs on the clouds it provisions publishes each
of these fields.

For a question about tensor parallelism over PCIe, map DCGM's PCIe and SM fields
too:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: MetricMapping
metadata:
  name: dcgm-interconnect
spec:
  metrics:
  - from: DCGM_FI_PROF_PCIE_TX_BYTES
    to: modelplane_gpu_pcie_transmit_bytes_per_second
  - from: DCGM_FI_PROF_PCIE_RX_BYTES
    to: modelplane_gpu_pcie_receive_bytes_per_second
  - from: DCGM_FI_PROF_SM_ACTIVE
    to: modelplane_gpu_sm_active_ratio
  - from: DCGM_FI_PROF_SM_OCCUPANCY
    to: modelplane_gpu_sm_occupancy_ratio
```

DCGM samples PCIe traffic as a rate, in bytes per second, so read the two PCIe series
as they are and don't take `rate()` of them.

## Why engine latency and gateway latency differ

`modelplane_request_ttft_seconds` comes from the engine, and engines bucket their
histograms differently. vLLM resolves down to a millisecond. SGLang resolves to a hundred
of them. A quantile across both is wrong, not approximate. Use the engine series to compare
one engine against itself, and the `frontend_` series for anything fleet-wide.

SGLang's time to first token is `modelplane_sglang_ttft_seconds`, a name of its own,
so it never mixes with vLLM's buckets. Its buckets run to 400 seconds where the
gateway's stop at 60, so it's the series to read when SGLang's first tokens take
longer than a minute.

Some measurements don't translate at all. SGLang's inter-token latency isn't vLLM's time per
output token, so neither is renamed onto a shared name. The gateway measures time per output
token for both.
