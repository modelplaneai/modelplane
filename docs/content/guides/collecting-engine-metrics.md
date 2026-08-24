---
title: Collecting engine metrics
weight: 20
description: Scrape a vLLM engine's Prometheus metrics through the in-cluster Prometheus.
---
<!-- vale write-good.Passive = NO -->
Scraping an inference engine's Prometheus metrics, shown on the smallest serving
shape: a 0.5B Qwen chat model on one NVIDIA L4. vLLM publishes metrics at
`/metrics` on its serving port with no extra flag, and Modelplane runs a
Prometheus on every workload cluster with `PodMonitor` discovery open across
namespaces, so scraping the engine is a `PodMonitor` plus a `port-forward`. The
model is only the subject; the same wiring fits any engine, with the SGLang,
leader/worker, and prefill/decode differences noted at the end.

This was run end to end on GKE. The `InferenceClass` and `ModelDeployment` are the
exact manifests from that run, and the `PodMonitor` below scraped this deployment.
Apply the platform side first, then the ML side.

## Platform

{{< manifests "guides/collecting-engine-metrics/inference-class.yaml" >}}

{{< manifests "guides/collecting-engine-metrics/inference-cluster.yaml" >}}

## Deployment

{{< manifests "guides/collecting-engine-metrics/model-deployment.yaml" >}}

{{< manifests "guides/collecting-engine-metrics/model-service.yaml" >}}

## Scraping the metrics

The `PodMonitor` selects engine pods by the `modelplane.ai/serving` label
Modelplane stamps on them, and the `monitoring` namespace Prometheus discovers any
`PodMonitor`, so this is the whole config. The engine container port is unnamed,
so reference it by number with `targetPort`:

{{< manifests "guides/collecting-engine-metrics/podmonitor.yaml" >}}

The engine pods and the `PodMonitor` CRD live on the workload cluster, not the
control plane, so apply it there. Then read the metrics from the in-cluster
Prometheus over a `port-forward`:

```bash
kubectl -n monitoring port-forward svc/prometheus-prometheus 9090:9090   # workload cluster
# open http://localhost:9090, Status > Targets to confirm the scrape, then query
# e.g. vllm:num_requests_running or vllm:gpu_cache_usage_perc
```

### Other engine shapes

The `PodMonitor` above fits a single-pod vLLM engine. The selector and port shift
by shape:

- **SGLang**: exposes `/metrics` only when the engine runs with
  `--enable-metrics`; otherwise it's identical (same selector, `targetPort: 8000`).
- **Leader/worker**: only the leader serves the API and carries
  `modelplane.ai/serving`, so the selector above already scrapes the leader alone;
  the workers expose nothing.
- **prefill/decode**: two engines, labelled `llm-d.ai/role: prefill` and
  `llm-d.ai/role: decode`. The prefill engine serves on `8000`; the decode engine
  sits behind the routing sidecar that takes `8000` and listens on `8001`, so
  scrape decode with `targetPort: 8001`. Select each by its role label to keep
  them apart.
<!-- vale write-good.Passive = YES -->
