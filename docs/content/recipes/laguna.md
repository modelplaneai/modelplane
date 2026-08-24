---
title: Laguna-S-2.1
weight: 35
description: A 118B code MoE served FP8 on a single 8x H100 node on Nebius.
model: poolside/Laguna-S-2.1-FP8
vendors: [Poolside]
clouds: [Nebius]
accelerators: [H100]
engines: [vLLM, SGLang]
arch: MoE
precisions: ["FP8"]
size: 118B A8B
ctx: "262,144"
servingModes: [Standalone]
engineImages: [vllm/vllm-openai:v0.25.1, lmsysorg/sglang:v0.5.12.post1-cu129]
gpuNote: 8× per node
---
<!-- vale write-good.Passive = NO -->
Poolside's Laguna-S-2.1 (118B total, 8B active MoE) served FP8 as a single
`Standalone` vLLM engine on one 8x H100 node on Nebius. The FP8 weights (~121 GiB)
fit one node with headroom for KV cache, so the engine is tensor-parallel across
the 8 GPUs over NVLink, with no gang and no prefill/decode disaggregation. Weights
stage once to a `ModelCache` on a Nebius shared filesystem and mount at `/mnt/models`.

This recipe was run end to end on Nebius (`eu-north`): serving and tool calling
validated on a single 8x H100 node. `poolside/Laguna-S-2.1-FP8` is a public
repository, so no Hugging Face token or Secret is needed. Apply the platform
side first, then the ML side.

## Validated deployments

{{< validated-deployments >}}

## Platform

{{< manifests "recipes/laguna/inference-class-nebius.yaml" >}}

{{< manifests "recipes/laguna/inference-cluster-nebius.yaml" >}}

## Deployment

{{< manifests "recipes/laguna/model-cache.yaml" >}}

{{< manifests "recipes/laguna/model-deployment.yaml" >}}

{{< manifests "recipes/laguna/model-service.yaml" >}}

## Serving with SGLang

`model-deployment-sglang.yaml` is an alternative to the vLLM deployment above. It
serves the same model with SGLang, which has native Laguna support and the
`poolside_v1` parsers. Apply it instead of `model-deployment.yaml`.

{{< manifests "recipes/laguna/model-deployment-sglang.yaml" >}}
<!-- vale write-good.Passive = YES -->
