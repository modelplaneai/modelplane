---
title: Qwen3-8B
weight: 10
description: An 8.2B dense chat model on a single NVIDIA L4.
---
<!-- vale write-good.Passive = NO -->
An 8.2B dense chat model on a single NVIDIA L4. The smallest recipe: one
`Standalone` engine, no cache, weights pulled straight from Hugging Face.

This recipe was run end to end; the `InferenceClass` and `ModelDeployment` are
the exact manifests from that run. Apply the platform side first, then the ML
side.

## Platform

{{< manifests "examples/qwen3-8b/inference-class.yaml" >}}

{{< manifests "examples/qwen3-8b/inference-cluster.yaml" >}}

## Deployment

{{< manifests "examples/qwen3-8b/model-deployment.yaml" >}}

{{< manifests "examples/qwen3-8b/model-service.yaml" >}}
<!-- vale write-good.Passive = YES -->
