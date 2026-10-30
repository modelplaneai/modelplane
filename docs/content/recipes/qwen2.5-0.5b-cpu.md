---
title: Qwen2.5-0.5B on CPU
weight: 14
description: A 0.5B dense chat model served on CPUs alone, on a Civo CPU pool.
model: Qwen/Qwen2.5-0.5B-Instruct
vendors: [Qwen]
clouds: [Civo]
accelerators: [CPU]
engines: [vLLM]
arch: Dense
precisions: ["BF16"]
size: 0.5B
ctx: "4,096"
servingModes: [Standalone]
engineImages: [vllm/vllm-openai-cpu:v0.32.0-x86_64]
---
<!-- vale write-good.Passive = NO -->
A 0.5B dense chat model served on CPUs alone, on a Civo `g4p.kube.medium`
pool: one `Standalone` engine, no cache, weights pulled straight from Hugging
Face. A CPU pool is the cheapest way to exercise a Modelplane deployment end
to end - no GPU to find, no driver to wait for - and a 0.5B model is small
enough that CPU inference answers in sensible time.

The cluster pins Kubernetes 1.36: the claim consumes the device's cores as
consumable capacity, which Kubernetes enables by default from 1.36, on an
older version the engine pod fails when the node prepares its claim.

The class's provisioning block omits `accelerator`, which is what makes the
pool a CPU pool: the cluster composition builds it with a standard node image
and the `modelplane.ai/cpu` label and taint, and the serving stack installs
`dra-driver-cpu` - and no GPU machinery - because the class claims `dra.cpu`.
The claim pins the engine to its NUMA node's cpuset, and the engine's CPU
request is the one number that sizes everything: Kubernetes accounts the pod
by it, and Modelplane stamps it into the claim as the share of the device to
take. Five of the six claimable cores, because the driver refuses a claim
that would take them all - the node's shared containers keep the last one.
The engine is vLLM's CPU build, whose image serves the same OpenAI API as the
CUDA one. The same class without its provisioning block describes a
bring-your-own CPU pool, on any conformant cluster: label its nodes
`modelplane.ai/pool=cpu-pool` and `modelplane.ai/cpu=true` instead.

## Validated deployments

{{< validated-deployments >}}

## Platform

{{< manifests "recipes/qwen2.5-0.5b-cpu/inference-class.yaml" >}}

{{< manifests "recipes/qwen2.5-0.5b-cpu/inference-cluster.yaml" >}}

## Deployment

{{< manifests "recipes/qwen2.5-0.5b-cpu/model-deployment.yaml" >}}

{{< manifests "recipes/qwen2.5-0.5b-cpu/model-service.yaml" >}}
<!-- vale write-good.Passive = YES -->
