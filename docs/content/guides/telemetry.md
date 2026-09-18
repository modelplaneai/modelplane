---
title: Telemetry
weight: 20
draft: true
aliases:
- /guides/collecting-engine-metrics/
description: Collect normalized metrics across the fleet and send them anywhere that speaks OTLP.
---
<!-- vale write-good.Passive = NO -->
{{< hint warning >}}
**Draft.** This page documents [the metrics design][design], which isn't built yet. It's
here to check the experience reads well before it's implemented, and it's excluded from the
site by `draft: true`. It replaces [Collecting engine metrics]({{< ref
"guides/collecting-engine-metrics.md" >}}) when the per-cluster Prometheus stack is
removed, and takes that page's URL with it.

[design]: https://github.com/modelplaneai/modelplane/pull/363
{{< /hint >}}

Modelplane runs an OpenTelemetry collector on every inference cluster. It collects from
every component Modelplane installs, which is more than your engines. It renames each
component's series to a single `modelplane_*` vocabulary and pushes to a collector on your
control plane. That collector is your fleet's
single egress point, and it sends to any backend that speaks OTLP.

Modelplane has no API for this: nothing to write, and nothing to keep in sync as your
deployments change.

## What you get

Every series carries `cluster`. A series about a deployment also carries `deployment`,
`namespace`, `model`, and `engine`. Some of what you can read:

| Metric | Means |
| --- | --- |
| `modelplane_frontend_ttft_seconds` | Time to the first token, measured at the gateway |
| `modelplane_frontend_tpot_seconds` | Time per output token, measured at the gateway |
| `modelplane_frontend_request_duration_seconds` | What the caller waited, end to end |
| `modelplane_request_queue_seconds` | How long a request waited before the engine started |
| `modelplane_requests_waiting` | Queue depth per engine |
| `modelplane_kv_cache_utilization_ratio` | KV-cache occupancy, 0 to 1 |
| `modelplane_tokens_total` | Tokens in and out, by `direction` |
| `modelplane_replica_gpus` | GPUs a replica holds |
| `modelplane_gpu_seconds_total` | GPU-time bound to serving |

Latency appears twice on purpose. The `frontend_` series are what your caller experienced,
measured at the gateway. The engine's own series are what the engine spent. When the
frontend number is slow and the engine number isn't, the problem is routing, queueing, or
the network rather than the model.

<!-- vale Google.Acronyms = NO -->
No series names a pod. Replicas are interchangeable, so they're summed before the metrics
leave the cluster; a rolling update would otherwise leave a dead series behind for every pod
it replaced.
<!-- vale Google.Acronyms = YES -->

## Sending it somewhere

Create a `TelemetryDestination` naming whatever you already run:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: TelemetryDestination
metadata:
  name: default
spec:
  exporters:
    otlphttp:
      endpoint: https://otel.example.internal
```

`spec.exporters` is the OpenTelemetry collector's own exporters block, so any exporter the
collector provides works here, with its usual TLS and retry settings.

Put credentials in a Secret and name it with `secretRef`. Modelplane mounts its keys into
the collector as environment variables, so your config refers to `${env:OTLP_TOKEN}` and the
token never appears in `kubectl get -o yaml`:

```yaml
spec:
  secretRef:
    name: telemetry-credentials
  extensions:
    bearertokenauth:
      token: ${env:OTLP_TOKEN}
  exporters:
    otlphttp:
      endpoint: https://otel.example.internal
      auth:
        authenticator: bearertokenauth
```

If you run Prometheus, export to that instead and query the fleet there:

```yaml
spec:
  exporters:
    prometheusremotewrite:
      endpoint: https://prom.example.internal/api/v1/write
```

Until you create one, Modelplane composes no collectors: nothing here stores anything, so
collecting with nowhere to send it would spend GPU-cluster memory on samples nobody reads.
Creating a destination turns collection on everywhere at once, and there's no per-deployment
opt-out.

Your clusters reach the control plane, and only the control plane reaches your backend. A
cluster with no route to your observability stack still reports, and the backend's
credential lives in one place instead of on every GPU cluster.

## Computing rates, quantiles, and ratios

A collector transforms each measurement as it passes it on. It holds no history, so it
produces no rates and no quantiles. Your backend does that. A fleet-wide p99:

```promql
histogram_quantile(0.99, sum by (le) (
  rate(modelplane_frontend_ttft_seconds_bucket{model="Qwen/Qwen3-8B"}[5m])))
```

Modelplane provides these as queries and Grafana dashboards rather than as precomputed
series. To precompute them, export to Prometheus and write recording rules there.

## Engines

vLLM and SGLang need no configuration.

Any other OpenAI-compatible engine reports its top-line numbers with no configuration
either. The gateway measures those, not the engine, so `modelplane_frontend_*` and the token
counters work for an engine Modelplane has never seen.

To normalize that engine's own metrics as well, create a `MetricMapping`:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: MetricMapping
metadata:
  name: my-engine
spec:
  statements:
  - set(name, "modelplane_requests_waiting")
      where name == "my_engine_queued_requests"
```

`spec.statements` are OTTL, the collector's own transform language. Modelplane renders them
into every cluster's collector, so you write them once. An engine with no statements is
still collected, under its own names.

One engine needs a flag. SGLang publishes `/metrics` only when it runs with
`--enable-metrics`, so add it to the engine args. vLLM needs nothing.

## Why engine latency and gateway latency differ

`modelplane_request_ttft_seconds` comes from the engine, and engines bucket their
histograms differently. vLLM resolves down to a millisecond. SGLang resolves to a hundred
of them. A quantile across both is wrong, not approximate. Use the engine series to compare
one engine against itself, and the `frontend_` series for anything fleet-wide.

Some measurements don't translate at all. SGLang's inter-token latency isn't vLLM's time per
output token, so neither is renamed onto a shared name. The gateway measures time per output
token for both.

## Migrating from a hand-written `PodMonitor`

[Collecting engine metrics]({{< ref "guides/collecting-engine-metrics.md" >}}) had you write
a `PodMonitor` and reach the in-cluster Prometheus over a `port-forward`. Both are gone, and
this page replaces that one. Delete the `PodMonitor`: once the Prometheus operator is no
longer installed it stops working, and it stops working quietly. Queries you ran against
that Prometheus move to your backend.
<!-- vale write-good.Passive = YES -->
