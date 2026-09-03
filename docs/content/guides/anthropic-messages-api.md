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

Read the Messages API base URL from the gateway serving the service. The gateway
publishes one per API it speaks:

```bash
ADDRESS=$(kubectl get ig local -o jsonpath='{.status.endpoints.anthropic}')
```

Post to `/messages` under it. The `model` field is the `ModelService`, as
`<namespace>/<service>`, and the gateway rewrites it to whatever the engine was
started as; `max_tokens` is required:

```bash
curl "$ADDRESS/messages" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "ml-team/qwen3-8b",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

A gateway with no `hostname` answers on its load balancer address, which may be
in-cluster only, so run the request from inside the cluster if your shell can't
reach it:

```bash
kubectl run -i --rm curl-test \
  --image=curlimages/curl \
  --restart=Never \
  --env="ADDRESS=$ADDRESS" \
  -- sh -c 'curl -s "$ADDRESS/messages" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d "{\"model\":\"ml-team/qwen3-8b\",\"max_tokens\":1024,\"messages\":[{\"role\":\"user\",\"content\":\"Hello!\"}]}"'
```

## Point Claude Code at it

Claude Code appends `/v1/messages` to `ANTHROPIC_BASE_URL`. The gateway's
Anthropic base URL already ends in `/anthropic/v1`, so strip that suffix and let
Claude Code add its own. Map every model tier onto the `ModelService`, since one
model serves them all here. This gateway authenticates nobody and vLLM ignores
the token, so any non-empty value works. Claude Code reserves 32000 output
tokens by default, which alone leaves little context room on a small model; cap
it with `CLAUDE_CODE_MAX_OUTPUT_TOKENS` so the input and output fit under the
engine's `--max-model-len` (40960 here):

```bash
export ANTHROPIC_BASE_URL="${ADDRESS%/anthropic/v1}/anthropic"
export ANTHROPIC_AUTH_TOKEN=dummy
export ANTHROPIC_DEFAULT_OPUS_MODEL=ml-team/qwen3-8b
export ANTHROPIC_DEFAULT_SONNET_MODEL=ml-team/qwen3-8b
export ANTHROPIC_DEFAULT_HAIKU_MODEL=ml-team/qwen3-8b
export CLAUDE_CODE_MAX_OUTPUT_TOKENS=8192
claude
```

The gateway must be reachable from wherever `claude` runs. If its address is
in-cluster only, forward the gateway's service to a local port. Envoy Gateway
names that service after the `Gateway` it belongs to and appends a hash, so
select it by label rather than by name, against the cluster the gateway runs on:

```bash
kubectl -n envoy-gateway-system port-forward 8080:80 \
  "$(kubectl -n envoy-gateway-system get svc -o name \
     -l gateway.envoyproxy.io/owning-gateway-name=fleet-gateway)"
```

Then point `ANTHROPIC_BASE_URL` at the local port:

```bash
export ANTHROPIC_BASE_URL="http://localhost:8080/anthropic"
```
<!-- vale write-good.Passive = YES -->
