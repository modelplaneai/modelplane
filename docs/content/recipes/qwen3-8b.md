---
title: Qwen3-8B
weight: 10
description: An 8.2B dense chat model on a single NVIDIA L4.
model: Qwen/Qwen3-8B
vendors: [Qwen]
clouds: [EKS]
accelerators: [L4]
engines: [vLLM]
arch: Dense
precisions: ["BF16"]
size: 8B
ctx: "16,384"
servingModes: [Standalone]
engineImages: [vllm/vllm-openai:v0.23.0]
gpuNote: 1× per node
features:
  - name: Speculative decoding
    href: "#speculative-decoding"
    note: for low latency and small batch sizes
---
<!-- vale write-good.Passive = NO -->
An 8.2B dense chat model on a single NVIDIA L4. The smallest recipe: one
`Standalone` engine, no cache, weights pulled straight from Hugging Face.

This recipe was run end to end; the `InferenceClass` and `ModelDeployment` are
the exact manifests from that run. Apply the platform side first, then the ML
side.

## Validated deployments

{{< validated-deployments >}}

## Platform

{{< manifests "recipes/qwen3-8b/inference-class.yaml" >}}

{{< manifests "recipes/qwen3-8b/inference-cluster.yaml" >}}

## Deployment

{{< manifests "recipes/qwen3-8b/model-deployment.yaml" >}}

{{< manifests "recipes/qwen3-8b/model-service.yaml" >}}

## Speculative decoding

The same model and platform also serve with n-gram (prompt-lookup) speculative
decoding, which proposes tokens by matching the prompt and so needs no draft
model or second set of weights. On copy-heavy output, editing a pasted code
block where most output tokens are copied from the prompt, it roughly doubles
decode throughput and halves the time per output token:

| Metric | Without speculation | With n-gram speculation |
|---|---|---|
| Output token throughput (tok/s) | 16.10 | 39.01 |
| Mean TPOT (ms/token) | 60.20 | 24.21 |

Measured on a single L4 (`vllm/vllm-openai:v0.23.0`, Qwen3-8B, 30 copy-heavy
prompts at concurrency 1) against the same model without `--speculative-config`;
the speculative run accepted 65% of drafted tokens, a mean acceptance length of
4.27 of 5. Speculation proposes several tokens per decode step and verifies them in
one forward pass, so when the output repeats the prompt most proposed tokens are
accepted at once, without changing what the model would have generated.

This variant was run end to end on GKE on the same single-L4 platform shape;
the `ModelDeployment` below is the exact manifest from that run, and the
numbers above are from the same run. Apply it instead of (or alongside) the
deployment above:

{{< manifests "recipes/qwen3-8b-speculative-decoding/model-deployment.yaml" >}}

{{< manifests "recipes/qwen3-8b-speculative-decoding/model-service.yaml" >}}

Speculation is active when the engine logs its `SpeculativeConfig` at startup
(`method='ngram'`). The call below pastes a code block and asks for a small edit,
the copy-heavy case n-gram accelerates, so most output tokens are matched straight
from the prompt:

```bash
ADDR=$(kubectl get ms qwen3-8b-spec -n ml-team -o jsonpath='{.status.address}')
curl -s "$ADDR/v1/chat/completions" -H 'Content-Type: application/json' -d '{
  "model": "qwen3-8b-spec",
  "messages": [{"role":"user","content":"Return this Python function unchanged except rename the variable `total` to `subtotal`. Output only the code.\n\ndef cart(items):\n    total = 0\n    for item in items:\n        total += item.price\n    return total"}],
  "max_tokens": 200, "temperature": 0 }'
```

With the engine running, its logs report how many proposed tokens it accepts:

```bash
kubectl logs -n ml-team -l modelplane.ai/deployment=qwen3-8b-spec \
  | grep "SpecDecoding metrics"
```
<!-- vale write-good.Passive = YES -->
