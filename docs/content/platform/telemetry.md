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
server engine, inference gateway and Envoy proxy, router, and the GPU exporter
your cloud provides. It renames each component's series to a single
`modelplane_*` vocabulary and exports them to wherever you say - any backend the
collector has an exporter for, not only OTLP.

Modelplane allows you to write one destination for your metrics. You don't need
to manage per-deployment configurations or update your configuration when a
deployment changes. The collector finds pods itself, so a leader/worker split or a
prefill/decode pair is collected the same as a single pod.

## Telemetry workflow

Every series carries `cluster`, `job`, and `instance` labels of the target
resource. A series about a deployment also carries `deployment`, `replica`,
`namespace`, `engine`, and `role` labels.

Each replica publishes its own series, so combine them in your query. Which
combination is right follows from what the metric measures:

 - `sum by (deployment)`, for anything counted, such as requests, tokens, or queue depth.
 - `avg by (deployment)`, for a ratio.
 - `max by (deployment)`, for a saturation figure an alert fires on.

To combine:

```promql
sum by (deployment) (rate(modelplane_frontend_request_duration_seconds_count[5m]))
```

The replica is an index rather than a pod, so it's bounded by the replica count
and survives a restart and a rolling update. Group by `replica`, not by `instance`:

```promql
# One line per replica, stable across rolling updates
max by (deployment, replica) (modelplane_kv_cache_utilization_ratio)
```
`instance` is the pod's address, which keeps two pods of the same replica apart. It
turns over on every rolling update, so group by `replica` rather than by `instance`.

Some examples of the available metrics:

| Metric | Means |
| --- | --- |
| `modelplane_frontend_ttft_seconds` | Time to the first token, measured at the gateway |
| `modelplane_frontend_tpot_seconds` | Time per output token, measured at the gateway |
| `modelplane_frontend_request_duration_seconds` | What the caller waited, end to end |
| `modelplane_request_queue_seconds` | How long a request waited before the engine started |
| `modelplane_requests_waiting` | Queue depth per engine |
| `modelplane_kv_cache_utilization_ratio` | KV-cache occupancy, averaged over replicas |
| `modelplane_request_input_tokens` | Prompt size, as a histogram |
| `modelplane_request_output_tokens` | Generated length, as a histogram |
| `modelplane_gpu_memory_used_bytes` | Framebuffer memory in use, per GPU |
| `modelplane_energy_joules_total` | Energy drawn since the driver last reloaded |

Latency appears twice on purpose. The `frontend_` series are what your caller experienced,
measured at the gateway. The engine's own series are what the engine spent. For
example, if the frontend metric is slow and the engine isn't, you can 
troubleshoot routing, queueing, or networking issues instead of the model.


Saturation gauges are per replica, so how you combine them decides what you see. Average
across a deployment to plan capacity, and take the maximum to alert: three replicas at 0.3
and one at 0.99 average to something comfortable while the fourth evicts and recomputes. A
high maximum beside `modelplane_requests_preempted_total` climbing is one replica thrashing. To alert on it:

```promql
max by (deployment) (modelplane_kv_cache_utilization_ratio) > 0.95
```

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
the key that holds the token:

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
token needs no restart.

If you run Prometheus, export to your Prometheus endpoint instead and query the fleet there:

```yaml
spec:
  sinks:
  - name: prometheus
    type: prometheus_remote_write
    endpoint: https://prom.example.internal/api/v1/write
```

If you create more than one sink, all get the entire stream. Each sink carries its
own credential, so a vendor and your own Prometheus don't have to share a Secret.

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
backend to report. Where a sink names a `secretRef`, you create that Secret once on the
control plane and Modelplane copies it to every cluster running a collector, so the
credential is held on each of them.

## Computing rates, quantiles, and ratios

A collector transforms each measurement as it passes it on. It holds no history, so it
produces no rates and no quantiles. Your backend does that. A fleet-wide p99:

```promql
histogram_quantile(0.99, sum by (le) (
  rate(modelplane_frontend_ttft_seconds_bucket{deployment="qwen3-8b"}[5m])))
```

Modelplane has no dashboards of its own. What it exports is counters and histogram buckets, and
your backend derives the rates and quantiles at query time. To precompute them instead,
export to Prometheus and write recording rules there.

## Engines

Modelplane already knows vLLM's and SGLang's metric names and renames them for you, so
neither needs anything from you here. SGLang needs two flags: `--enable-metrics` to publish `/metrics` at all, and
`--collect-tokens-histogram` for the prompt and generation histograms behind
`modelplane_request_input_tokens` and `modelplane_request_output_tokens`. Without the
second it publishes those as plain counters and both series stay empty. vLLM needs
nothing.

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

SGLang publishes no queue time per request and no preemption counters, so
`modelplane_request_queue_seconds` and `modelplane_requests_preempted_total` carry vLLM
only.

Any other OpenAI-compatible engine reports its frontend numbers with no configuration.
The gateway measures those, not the engine, so `modelplane_frontend_*` works for an
engine Modelplane has never seen.

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
backend holds every replica's series and combines them at query time.

Say `fromUnit` whenever the engine measures in something other than the unit the name
claims, and Modelplane converts to the base one. Skipping this is the expensive mistake here:
a series named `_seconds` that holds milliseconds reads a thousand times fast, and nothing
downstream can tell.

Rename only where the measurements agree. Two engines' histograms under one name are worth
less than nothing if their buckets disagree, because a quantile over them is wrong rather
than approximate.

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

## Why engine latency and gateway latency differ

`modelplane_request_ttft_seconds` comes from the engine, and engines bucket their
histograms differently. vLLM resolves down to a millisecond. SGLang resolves to a hundred
of them. A quantile across both is wrong, not approximate. Use the engine series to compare
one engine against itself, and the `frontend_` series for anything fleet-wide.

Some measurements don't translate at all. SGLang's inter-token latency isn't vLLM's time per
output token, so neither is renamed onto a shared name. The gateway measures time per output
token for both.
