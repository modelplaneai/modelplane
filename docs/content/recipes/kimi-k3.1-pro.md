---
title: Kimi K3.1 Pro
weight: 30
description: A 2.8T hybrid-attention MoE served by SGLang on one 8x B300 node.
model: moonshotai/Kimi-K3.1-Pro
vendors: [Moonshot AI]
clouds: [EKS]
accelerators: [B300]
engines: [SGLang]
arch: MoE
precisions: ["MXFP4"]
size: 2.8T A104B
ctx: "524,288"
servingModes: [Standalone]
engineImages: [lmsysorg/sglang:dev-kimi-k31-pro]
gpuNote: 8× per node
---
<!-- vale write-good.Passive = NO -->
Moonshot AI's Kimi K3.1 Pro (2.8 trillion parameters, 104 billion active) served
by SGLang as one `Standalone` engine on one 8x B300 node on EKS. The weights (~1.42 TiB,
MXFP4 routed experts and BF16 elsewhere) fit the node's 8 × 288 GB with room
for a large KV cache, so the engine is tensor-parallel across the 8 GPUs and
doesn't need a gang or prefill/decode disaggregation. Decode context parallelism
shards the KV cache across the ranks, which gives a pool of about 8.6M tokens.

The model mixes linear attention with sparse attention layers, which changes
how prefix caching behaves. SGLang reuses a cached prefix only up to the last
linear-attention state it kept. The deployment keeps several states per prefix
path. The comments in `model-deployment.yaml` explain the flags beyond
SGLang's basics.

This recipe ran on one `p6-b300.48xlarge` on EKS behind an `InferenceGateway`,
serving recorded coding-agent traffic. That run read the weights from a copy
staged on the node's local NVMe; this recipe reads them from the shared
`ModelCache`. `moonshotai/Kimi-K3.1-Pro` is a public repository under Moonshot
AI's own license, so no Hugging Face token or Secret is needed. Apply the
platform side first, then the ML side.

## Validated deployments

{{< validated-deployments >}}

## Platform

{{< manifests "recipes/kimi-k3.1-pro/inference-class.yaml" >}}

{{< manifests "recipes/kimi-k3.1-pro/inference-cluster.yaml" >}}

## Deployment

{{< manifests "recipes/kimi-k3.1-pro/model-cache.yaml" >}}

{{< manifests "recipes/kimi-k3.1-pro/model-deployment.yaml" >}}

{{< manifests "recipes/kimi-k3.1-pro/model-service.yaml" >}}
<!-- vale write-good.Passive = YES -->
