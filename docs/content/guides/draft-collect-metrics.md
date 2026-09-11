---
title: Collect metrics
weight: 25
draft: true
description: Collect engine, router, and cluster metrics across the fleet and send them to one destination.
---
<!-- vale write-good.Passive = NO -->
{{< hint warning >}}
**Draft.** This page documents [the metrics design][design], which isn't built yet.
It's here to check the API reads well before it's implemented, and it's excluded from
the site by `draft: true`. Remove this page before merging the design.

The API line below is plain text rather than a `ref`, because the reference page is
generated from a CRD that doesn't exist yet and Hugo fails a `ref` it can't resolve.

[design]: https://github.com/modelplaneai/modelplane/pull/363
{{< /hint >}}

**API:** `modelplane.ai/v1alpha1` · MetricMapping

Modelplane collects metrics from everything it runs and sends them to a single
destination for the whole fleet. Each engine's names are rewritten to one Modelplane
vocabulary on the way, so a dashboard doesn't care which engine produced a number.
There's no `PodMonitor` to write and no per-cluster Prometheus to reach into.

Two things to set up: where the metrics go, and what engine each deployment runs.

## Choosing a destination

Modelplane collects nothing until a destination exists, since a collector nothing reads
costs GPU-cluster resources for no return. Point it at any endpoint that speaks OTLP:

```yaml {nocopy=true}
spec:
  otlp:
    endpoint: otel.example.internal:4317
    authSecret:
      name: otlp-token         # a Secret in this namespace
      key: token
```

Create the Secret once on the control plane. Modelplane propagates it to every cluster
that needs it, the same way a `ModelCache` credential travels:

```bash
kubectl create secret generic otlp-token \
  --namespace modelplane-system \
  --from-literal=token=<token>
```

A cluster that can't reach your destination needs `metrics.transport: Pull` on its
`InferenceCluster`. Modelplane then collects over the connection the control plane
already has to that cluster's API server, and forwards to the destination from the
center. Nothing on the cluster is exposed either way. Status reports which mode applied:

```console
$ kubectl get inferencecluster
NAME          READY   METRICS   AGE
eks-us-east   True    Push      6d
gke-eu-west   True    Push      6d
onprem-dc1    True    Pull      2d
```

## Naming your engine

Metrics arrive under whatever name the engine gave them.
`vllm:num_requests_waiting` and SGLang's queue depth counter are the same number, so
Modelplane renames both to `modelplane_requests_waiting` once it knows which engine
produced them. Set `type` to say:

```yaml {nocopy=true}
spec:
  template:
    spec:
      engines:
      - name: qwen3-8b
        type: vllm             # selects the rename rules
```

Modelplane provides rules for `vllm`, `sglang`, and `trtllm`. Leave `type` off and the
engine's metrics still arrive, under their own names, and Modelplane reports that no
mapping matched rather than guessing one.

## Adding an engine Modelplane doesn't cover

A forked or new engine needs a `MetricMapping`. Set `type` to any value and write the
mapping that selects it:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: MetricMapping
metadata:
  name: my-vllm-fork
spec:
  selector:
    matchLabels:
      modelplane.ai/engine: my-vllm-fork
  rename:
    vllm:time_to_first_token_seconds: modelplane_time_to_first_token
    vllm:num_requests_waiting: modelplane_requests_waiting
```

Applying it is the whole change. No fork of Modelplane, and no waiting on a release.

## What you get

Every series carries `engine`, `cluster`, `model`, `deployment`, and `namespace`, so one
query spans the fleet:

| Metric | Means |
| --- | --- |
| `modelplane_time_to_first_token` | Latency to the first token, as a histogram |
| `modelplane_inter_token_latency` | The gap between output tokens, as a histogram |
| `modelplane_requests_waiting` | Queue depth per engine |
| `modelplane_kv_cache_usage` | KV-cache occupancy per engine |

<!-- vale Google.Acronyms = NO -->
Labels naming an individual pod are dropped before the metrics leave the cluster. A
rolling update would otherwise leave a dead series behind for every pod it replaced.
<!-- vale Google.Acronyms = YES -->

Alongside the engines, Modelplane collects its routers, the stack it installs on each
cluster, and its own control plane, all under `modelplane_*`. Across the fleet it also
reports totals for capacity, GPU usage, and degraded deployments.

## Migrating from a hand-written `PodMonitor`

Earlier versions had you write a `PodMonitor` and reach into the in-cluster Prometheus.
Both are gone. Delete the `PodMonitor`: with the Prometheus operator no longer installed
it stops working, and it stops working quietly. Queries you used to run against that
Prometheus move to whatever consumes your destination.
<!-- vale write-good.Passive = YES -->
