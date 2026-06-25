---
title: Qwen3-8B speculative decoding
weight: 60
description: An 8.2B dense chat model on a single L4 with n-gram speculative decoding.
---
<!-- vale write-good.Passive = NO -->
This example sets up n-gram (prompt-lookup) speculative decoding for Qwen3-8B on a
single NVIDIA L4. On copy-heavy output, editing a pasted code block where most
output tokens are copied from the prompt, it roughly doubles decode throughput and
halves the time per output token:

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

This recipe was run end to end on GKE; the `ModelDeployment` below is the exact
manifest from that run, which served a valid completion, and the numbers above are
from the same run. Apply the platform side first, then the ML side.

## Setup

Qwen3-8B is an 8.2B dense chat model, served as one `Standalone` vLLM engine on a
single NVIDIA L4 with no cache and weights pulled straight from Hugging Face. The
deployment shape is incidental here. The speculative config is what matters.
Modelplane supports n-gram (prompt-lookup) speculative decoding, which proposes
tokens by matching the prompt and so needs no draft model or second set of weights.

## Platform

The platform side is the single-L4 shape shared with the
[Qwen3-8B example](https://docs.modelplane.ai/examples/qwen3-8b/): one
`InferenceClass` and a single-node `InferenceCluster` for one NVIDIA L4. Follow its
Platform section to create them, then apply the ML side below.

## Deployment

{{< manifests "examples/qwen3-8b-speculative-decoding/model-deployment.yaml" >}}

{{< manifests "examples/qwen3-8b-speculative-decoding/model-service.yaml" >}}

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
