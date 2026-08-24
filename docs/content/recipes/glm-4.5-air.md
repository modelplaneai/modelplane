---
title: GLM-4.5-Air
weight: 25
description: A 106B MoE served from a GGUF checkpoint via llama.cpp on a single A100.
model: unsloth/GLM-4.5-Air-GGUF:IQ4_XS
vendors: [Z.ai]
clouds: [GKE]
accelerators: [A100]
engines: [llama.cpp]
arch: MoE
precisions: ["GGUF IQ4_XS"]
size: 106B A12B
ctx: "8,192"
servingModes: [Standalone]
engineImages: [ghcr.io/ggml-org/llama.cpp:server-cuda]
gpuNote: 1× per node
---
<!-- vale write-good.Passive = NO -->
A 106B MoE served from an Unsloth GGUF checkpoint via llama.cpp instead of
vLLM, on a single A100 40 GB. Modelplane treats the engine as any
OpenAI-compatible container, so the only changes from a vLLM deployment are
the image and args: the container is still named `engine` and listens on
`:8000`. vLLM can't load this Unsloth quantization format. llama.cpp can, and
`-hf` pulls the checkpoint straight from Hugging Face at startup, so a
one-time deployment needs no `ModelCache`.

The model is bigger than one A100's VRAM, so `--n-cpu-moe` offloads the MoE
expert tensors to host RAM and the GPU runs the active path and KV cache.
That's how a 106B model fits one A100 instead of a multi-GPU node. Apply the
platform side first, then the ML side.

## Validated deployments

{{< validated-deployments >}}

## Platform

{{< manifests "recipes/glm-4.5-air/inference-class.yaml" >}}

{{< manifests "recipes/glm-4.5-air/inference-cluster.yaml" >}}

## Deployment

{{< manifests "recipes/glm-4.5-air/model-deployment.yaml" >}}

{{< manifests "recipes/glm-4.5-air/model-service.yaml" >}}
<!-- vale write-good.Passive = YES -->
