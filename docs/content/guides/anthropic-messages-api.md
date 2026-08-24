---
title: Anthropic Messages API
weight: 10
description: Serve a model on the Anthropic Messages API and drive it from Claude Code.
---
<!-- vale write-good.Passive = NO -->
A vLLM server registers the Anthropic Messages API at `/v1/messages` alongside
its OpenAI routes, with no extra flag. Modelplane's route matches the
`/<namespace>/<service>/` prefix and preserves the path below it, so the same
service URL answers both `/v1/chat/completions` and `/v1/messages`. A client that
speaks the Messages API, including Claude Code via `ANTHROPIC_BASE_URL`, talks to
the deployment directly. See
[Alternate APIs]({{< ref "/models/model-service.md" >}}) for the routing detail.

This recipe serves Qwen3-8B on a single NVIDIA H100 on Nebius, with tool calling
on: `--enable-auto-tool-choice` and `--tool-call-parser=hermes` are what let
Claude Code's tool use work. An 8B model needs a fraction of an H100, so the GPU
has ample headroom. Apply the platform side first, then the ML side.

## Platform

{{< manifests "guides/anthropic-messages-api/inference-class.yaml" >}}

{{< manifests "guides/anthropic-messages-api/inference-cluster.yaml" >}}

## Deployment

{{< manifests "guides/anthropic-messages-api/model-deployment.yaml" >}}

{{< manifests "guides/anthropic-messages-api/model-service.yaml" >}}

## Send a request

Read the endpoint's public address from the `ModelService` status:

```bash
ADDRESS=$(kubectl get ms qwen3-8b -n ml-team -o jsonpath='{.status.address}')
```

Post to `/v1/messages` in the Messages API shape. The `model` field is the
engine's `--served-model-name` (`qwen`); `max_tokens` is required:

```bash
curl "$ADDRESS/v1/messages" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "qwen",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

`status.address` is the in-cluster gateway address, so run the request from
inside the cluster if your shell can't reach it directly:

```bash
kubectl run -i --rm curl-test \
  --image=curlimages/curl \
  --restart=Never \
  --env="ADDRESS=$ADDRESS" \
  -- sh -c 'curl -s "$ADDRESS/v1/messages" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d "{\"model\":\"qwen\",\"max_tokens\":1024,\"messages\":[{\"role\":\"user\",\"content\":\"Hello!\"}]}"'
```

## Point Claude Code at it

Claude Code appends `/v1/messages` to `ANTHROPIC_BASE_URL`, so point it at the
service address and map its model tiers onto the served name. vLLM doesn't check
the auth token, so any non-empty value works. Claude Code reserves 32000 output
tokens by default, which alone leaves little context room on a small model; cap
it with `CLAUDE_CODE_MAX_OUTPUT_TOKENS` so the input and output fit under the
engine's `--max-model-len` (40960 here):

```bash
export ANTHROPIC_BASE_URL="$ADDRESS"
export ANTHROPIC_AUTH_TOKEN=dummy
export ANTHROPIC_DEFAULT_OPUS_MODEL=qwen
export ANTHROPIC_DEFAULT_SONNET_MODEL=qwen
export ANTHROPIC_DEFAULT_HAIKU_MODEL=qwen
export CLAUDE_CODE_MAX_OUTPUT_TOKENS=8192
claude
```

The gateway must be reachable from wherever `claude` runs. If `$ADDRESS` is
in-cluster only, forward the Traefik gateway service to a local port:

```bash
kubectl -n traefik-system port-forward svc/traefik 8080:80
```

Then point `ANTHROPIC_BASE_URL` at the local port, keeping the service's
`/<namespace>/<service>` path prefix so Claude Code's `/v1/messages` still routes:

```bash
export ANTHROPIC_BASE_URL="http://localhost:8080/ml-team/qwen3-8b"
```
<!-- vale write-good.Passive = YES -->
