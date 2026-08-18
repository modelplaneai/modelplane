---
title: Kimi-K2
weight: 30
description: A 1T MoE served prefill/decode disaggregated across two H200 nodes.
model: RedHatAI/Kimi-K2-Instruct-quantized.w4a16
vendors: [Moonshot AI]
clouds: [EKS]
accelerators: [H200]
engines: [vLLM]
arch: MoE
precisions: ["INT4"]
size: 1T A32B
ctx: "131,072"
servingModes: [PrefillDecode]
engineImages: [vllm/vllm-openai:v0.23.0]
gpuNote: 8× per node
---
<!-- vale write-good.Passive = NO -->
A 1T MoE (1 trillion parameters) served prefill/decode disaggregated across two
H200 nodes: two engines, one per phase, with Modelplane composing the llm-d
routing layer between them. This recipe serves an INT4 quantization of the
model; the native FP8 weights need four such nodes.

This recipe was run end to end; the `InferenceClass` and `ModelDeployment` are
the exact manifests from that run. Apply the platform side first, then the ML
side. The `InferenceCluster` carries an EC2 capacity reservation placeholder to
edit before applying.

## Validated deployments

{{< validated-deployments >}}

## Platform

{{< manifests "recipes/kimi-k2/inference-class.yaml" >}}

{{< manifests path="recipes/kimi-k2/inference-cluster.yaml" apply="false" >}}

{{< editCode >}}
```bash
curl -fsSL {{< manifest-url "recipes/kimi-k2/inference-cluster.yaml" >}} \
  | sed 's/cr-0123456789abcdef0/$@<your-reservation-id>$@/' \
  | kubectl apply -f -
```
{{< /editCode >}}

## Deployment

{{< manifests "recipes/kimi-k2/model-cache.yaml" >}}

{{< manifests "recipes/kimi-k2/model-deployment.yaml" >}}

{{< manifests "recipes/kimi-k2/model-service.yaml" >}}
<!-- vale write-good.Passive = YES -->
