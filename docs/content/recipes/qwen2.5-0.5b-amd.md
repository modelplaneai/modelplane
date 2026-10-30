---
title: Qwen2.5-0.5B on AMD
weight: 13
description: A 0.5B dense chat model on one AMD Instinct MI325X on a bring-your-own cluster.
model: Qwen/Qwen2.5-0.5B-Instruct
vendors: [Qwen]
clouds: [Existing]
accelerators: [MI325X]
engines: [vLLM]
arch: Dense
precisions: ["BF16"]
size: 0.5B
ctx: "32,768"
servingModes: [Standalone]
engineImages: [rocm/vllm:rocm7.14.1_cdna_ubuntu24.04_py3.14_pytorch_2.11_vllm_0.23.0]
gpuNote: 1× of 8 per node
---
<!-- vale write-good.Passive = NO -->
A 0.5B dense chat model on one AMD Instinct MI325X on a bring-your-own
cluster: one `Standalone` engine, no cache, weights pulled straight from
Hugging Face. The model is deliberately small - the point of this run is the
AMD serving path, not the model. The class claims the GPU through DRA
(`gpu.amd.com`); the serving stack installs the AMD GPU operator in DRA mode
when a class claims that driver, against the amdgpu kernel driver the node
image already carries. The engine is AMD's ROCm vLLM.

`source: Existing` is the one cloud source that installs the AMD stack today.
Bring the cluster with the amdgpu driver on its GPU nodes, label the pool's
nodes `modelplane.ai/pool=gpu-mi325x`, and apply the platform side first, then
the ML side.

## Validated deployments

{{< validated-deployments >}}

## Platform

{{< manifests "recipes/qwen2.5-0.5b-amd/inference-class.yaml" >}}

{{< manifests "recipes/qwen2.5-0.5b-amd/inference-cluster.yaml" >}}

## Deployment

{{< manifests "recipes/qwen2.5-0.5b-amd/model-deployment.yaml" >}}

{{< manifests "recipes/qwen2.5-0.5b-amd/model-service.yaml" >}}
<!-- vale write-good.Passive = YES -->
