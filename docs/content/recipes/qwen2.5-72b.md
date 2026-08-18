---
title: Qwen2.5-72B
weight: 37
description: A 72B dense chat model (AWQ INT4) on a single 80 GB GPU, on AKS and Nebius.
model: Qwen/Qwen2.5-72B-Instruct-AWQ
vendors: [Qwen]
clouds: [AKS, Nebius]
accelerators: [A100, H100]
engines: [vLLM]
arch: Dense
precisions: ["AWQ INT4"]
size: 72B
ctx: "8,192"
servingModes: [Standalone]
engineImages: [vllm/vllm-openai:v0.23.0]
gpuNote: 1× per node
---
<!-- vale write-good.Passive = NO -->
A 72B dense chat model served from an AWQ INT4 quantization on a single 80 GB
GPU per replica: one `Standalone` engine fed by a `ModelCache`. The platform
side comes in two shapes - an A100 on AKS and an H100 on Nebius - and the ML
side is the same manifest for both. The deployment carries no
`clusterSelector` and two replicas, and each pool holds exactly one GPU, so
with both platforms applied one replica runs on each. The service then splits
traffic between the two GPUs by weight, which the last section uses to compare
them. To serve on just one platform, apply one tab and drop `replicas` to 1.

These manifests mirror the repository's AKS and Nebius demos. Apply the
platform side first, then the ML side.

## Validated deployments

{{< validated-deployments >}}

## Platform

{{< tabs >}}
{{< tab "AKS" >}}
{{< manifests "recipes/qwen2.5-72b/inference-class-aks.yaml" >}}

{{< manifests "recipes/qwen2.5-72b/inference-cluster-aks.yaml" >}}
{{< /tab >}}
{{< tab "Nebius" >}}
{{< manifests "recipes/qwen2.5-72b/inference-class-nebius.yaml" >}}

{{< manifests "recipes/qwen2.5-72b/inference-cluster-nebius.yaml" >}}
{{< /tab >}}
{{< /tabs >}}

## Deployment

{{< manifests "recipes/qwen2.5-72b/model-cache.yaml" >}}

{{< manifests "recipes/qwen2.5-72b/model-deployment.yaml" >}}

{{< manifests "recipes/qwen2.5-72b/model-service.yaml" >}}

## Compare the A100 and the H100

Replicas are fleet-wide, not per-cluster: the deployment's `replicas: 2` means
two complete serving instances, and because each pool holds a single 80 GB GPU
they land one on the A100 and one on the H100. Modelplane labels each
replica's endpoint with the cluster it runs on, so the service can split
traffic between the platforms by weight. This service pairs the deployment
label with each cluster label and gives each GPU half of the live traffic
behind the same URL:

{{< manifests "recipes/qwen2.5-72b/model-service-split.yaml" >}}

Both GPUs now serve the same workload, so their engine metrics give a direct
performance comparison: scrape each replica's latency and throughput as in
[Collecting engine metrics]({{< ref "/guides/collecting-engine-metrics.md" >}}) and
read the two side by side. Weights are relative, so once one platform wins,
shift the 50/50 toward it - 80/20, and as far as 100/0 - without touching the
deployment.
<!-- vale write-good.Passive = YES -->
