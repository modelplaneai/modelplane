---
title: Qwen2.5-7B
weight: 12
description: A 7B dense chat model (AWQ INT4) on a single NVIDIA A16 on Vultr.
model: Qwen/Qwen2.5-7B-Instruct-AWQ
vendors: [Qwen]
clouds: [Vultr]
accelerators: [A16]
engines: [vLLM]
arch: Dense
precisions: ["AWQ INT4"]
size: 7B
ctx: "8,192"
servingModes: [Standalone]
engineImages: [vllm/vllm-openai:v0.9.2]
gpuNote: 1× per node
---
<!-- vale write-good.Passive = NO -->
A 7B dense chat model served from an AWQ INT4 quantization on a single NVIDIA
A16 on Vultr: one `Standalone` engine, no cache, weights pulled straight from
Hugging Face. The A16 slice on the `vcg-a16-6c-64g-16vram` plan carries 16 GiB
of VRAM, so the INT4 weights (~5 GiB) fit with headroom for KV cache;
`--gpu-memory-utilization=0.85` and `--enforce-eager` keep the engine inside
the small card.

This recipe was run end to end on Vultr (`ewr`); the `InferenceClass`,
`InferenceCluster`, and `ModelDeployment` are the exact manifests from that
run. GPU plans are region-gated on Vultr, so check the plan is offered in your
region before applying. Apply the platform side first, then the ML side.

## Validated deployments

{{< validated-deployments >}}

## Platform

{{< manifests "recipes/qwen2.5-7b/inference-class.yaml" >}}

{{< manifests "recipes/qwen2.5-7b/inference-cluster.yaml" >}}

## Deployment

{{< manifests "recipes/qwen2.5-7b/model-deployment.yaml" >}}

{{< manifests "recipes/qwen2.5-7b/model-service.yaml" >}}
<!-- vale write-good.Passive = YES -->
