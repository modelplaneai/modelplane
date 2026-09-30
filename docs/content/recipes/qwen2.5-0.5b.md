---
title: Qwen2.5-0.5B
weight: 13
description: A 0.5B dense chat model on a single AMD Instinct MI325X on Vultr bare metal.
model: Qwen/Qwen2.5-0.5B-Instruct
vendors: [Qwen]
clouds: [VultrBaremetal]
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
A 0.5B dense chat model on a single AMD Instinct MI325X on Vultr bare metal:
one `Standalone` engine, no cache, weights pulled straight from Hugging Face.
The model is deliberately small - the point of this run is the AMD serving
path, not the model. The `vbm-256c-3072gb-8-mi325x-gpu` plan carries 8 GPUs
with just under 256 GiB of HBM3e each; the engine claims one of them through
DRA (`gpu.amd.com`) against the amdgpu driver preinstalled on Vultr's image.
The engine is AMD's ROCm vLLM build - the upstream `vllm/vllm-openai` image
supports only CUDA - and its entrypoint is not the OpenAI server, so the
command invokes `vllm serve` explicitly.

This recipe was run end to end on Vultr bare metal (`ord`); the
`InferenceClass`, `InferenceCluster`, and `ModelDeployment` are the exact
manifests from that run. The VultrBaremetal source needs the Vultr API key and
an SSH key pair Secret applied first - see `examples/vultr-baremetal` in the
repository. Bare metal GPU plans are expensive (the MI325X plan bills
thousands of dollars per month), region-gated, and often out of stock, so
check availability before applying, and expect provisioning to take tens of
minutes.

## Validated deployments

{{< validated-deployments >}}

## Platform

{{< manifests "recipes/qwen2.5-0.5b/inference-class.yaml" >}}

{{< manifests "recipes/qwen2.5-0.5b/inference-cluster.yaml" >}}

## Deployment

{{< manifests "recipes/qwen2.5-0.5b/model-deployment.yaml" >}}

{{< manifests "recipes/qwen2.5-0.5b/model-service.yaml" >}}
<!-- vale write-good.Passive = YES -->
