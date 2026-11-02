---
title: Claude Code through an InferenceGateway
weight: 20
description: Run an InferenceGateway in front of Anthropic's own models, with the organization's key and per-person usage records.
---
<!-- vale write-good.Passive = NO -->
The [Anthropic Messages API guide]({{< ref "anthropic-messages-api.md" >}})
points Claude Code at a model Modelplane serves. This one covers the other
common case: Claude's own models, served by Anthropic, through the
`InferenceGateway`, so the platform team holds the Anthropic key and each
person's usage lands in the gateway's records under their own name.

## Platform

The `InferenceGateway` authenticates each person with their own Modelplane
key. Each key in a selected Secret is one caller, and the gateway stamps the
identity onto every usage record without forwarding the key anywhere:

{{< manifests "guides/claude-code-gateway/inference-gateway.yaml" >}}

Label the callers' Secrets to match and give every person an entry, their
name and their key:

```yaml {nocopy=true}
apiVersion: v1
kind: Secret
metadata:
  name: claude-callers
  namespace: modelplane-system
  labels:
    modelplane.ai/inference-keys: "true"
stringData:
  dennis: sk-modelplane-dennis-...
  priya: sk-modelplane-priya-...
```

## Anthropic endpoint

A `ModelEndpoint` for `api.anthropic.com` is where the request goes. The
`ModelEndpoint` has one `model` field, and the gateway rewrites each
request's model to it on the way out, so each Claude model gets its own
endpoint. Each endpoint carries a label of its own so its service selects it
alone. The key the endpoint presents sits in the endpoint's namespace:

{{< manifests "guides/claude-code-gateway/anthropic-api-key.yaml" >}}

{{< manifests "guides/claude-code-gateway/model-endpoint.yaml" >}}

The Anthropic schema is what keeps this clean: the gateway translates an
Anthropic request for an OpenAI backend, but not the reverse, so it serves
only Anthropic clients on these endpoints. A service that mixes an
Anthropic endpoint with an OpenAI one serves both client shapes on one
name, and any feature that needs an `anthropic-*` header, prompt caching or
thinking, works only on the requests that backend speaks natively. The
gateway forwards Claude Code's request headers, `anthropic-beta` and
`anthropic-version` among them, to the backend unchanged, so those features
work on requests the endpoint answers.

## One service per model

Name each `ModelService` after the Claude ID: `anthropic/claude-opus-5-5`,
`anthropic/claude-sonnet-5-5`, `anthropic/claude-haiku-5-5`. Claude Code
reads the model ID out of every response to decide its context window,
thinking and request fields; a name it does not recognize prints
`[claude-code:unrecognized_model]` and makes it fall back to generic
defaults. Setting the request timeout to 1800s here, over the 300s default,
because a long Opus turn with thinking can run past it:

{{< manifests "guides/claude-code-gateway/model-service.yaml" >}}

## Client settings

Claude Code appends `/v1/messages` to `ANTHROPIC_BASE_URL`, so the base URL
is the gateway's Anthropic address minus its final `/v1`:

```bash
ADDRESS=$(kubectl get ig public -o jsonpath='{.status.endpoints.anthropic}')
export ANTHROPIC_BASE_URL="${ADDRESS%/v1}"
```

`status.endpoints` is published only for a gateway serving plain HTTP. A
gateway with TLS, like the one above, answers on the DNS names you point at
`status.address`, so set the base URL to `https://<name>/anthropic` instead.

The person's Modelplane key goes in `ANTHROPIC_AUTH_TOKEN`, which Claude
Code sends as `Authorization: Bearer <key>`, the form the gateway's
`spec.auth` checks first. Put it in `ANTHROPIC_API_KEY` instead and the
request still passes, but Claude Code then treats it as a key from the
Anthropic Console: it asks the person to approve it and changes its default
background model.

Map each model tier onto the matching service:

```bash
export ANTHROPIC_AUTH_TOKEN=sk-modelplane-dennis-...
export ANTHROPIC_DEFAULT_OPUS_MODEL=anthropic/claude-opus-5-5
export ANTHROPIC_DEFAULT_SONNET_MODEL=anthropic/claude-sonnet-5-5
export ANTHROPIC_DEFAULT_HAIKU_MODEL=anthropic/claude-haiku-5-5
```

Both credential variables replace the person's claude.ai login, so the
organization's key pays for the usage either way. Distribute the block
through [Claude Code's managed
settings](https://code.claude.com/docs/en/managed-settings), as organizations
roll out gateway configuration, or set it in each person's shell.

The gateway must be reachable from wherever `claude` runs. If its address is
in-cluster only, the Messages API guide's port-forward works the same way.

## Timeouts and retries

`spec.timeouts.request` bounds a request end to end, including the gateway's
own retries, and it defaults to 300s. A long turn with a large output runs
longer than that, so set it from the longest response you expect; the
services above use 1800s. `spec.timeouts.idle` defaults to 60s. Claude Code
streams, so a streamed response keeps the stream alive and the default
holds; it only matters if any caller doesn't stream, in which case set it at
least as long as `request`.

The gateway retries a 429 or 503 up to three times, on another endpoint if
one is left at the same priority. With one Anthropic endpoint, a
rate-limited request goes back to the same place, and Claude Code retries on
its own as well, using the `retry-after` header. That is usually what you
want from a shared key: the gateway's retries bound the damage of a
transient failure, and Claude Code's honor the quota Anthropic sends back.

## Usage records

Each request logs one usage record on the gateway's proxy, with the caller
the gateway resolved from the key, the service the request named, the
endpoint that served it, and the token counts:

```json
{
  "caller": "dennis",
  "service": "anthropic/claude-opus-5-5",
  "endpoint": "anthropic/modelroute-...-anthropic-claude-opus-5-5",
  "served_model": "claude-opus-5-5",
  "input_tokens": "12345",
  "output_tokens": "678",
  "total_tokens": "13023",
  "status": "200",
  "duration_ms": "42113",
  "start_time": "2026-11-03T10:11:12Z"
}
```

The token counts come from the model's usage in its response: the gateway's
ext-proc reads them, including from a streamed response's final usage frame,
and `input_tokens` includes the tokens a cached prompt read back. The records
are in the proxy's stdout, so collect them the way you collect any other pod
logs. Claude Code sends an `x-claude-code-session-id` header on every request,
which would join a person's turns into one session, but Modelplane doesn't
record it today ([#535](https://github.com/modelplaneai/modelplane/issues/535)).

## People's own subscriptions

Anthropic also documents running Claude Code through a gateway on each
person's own claude.ai subscription, with the person's login forwarded on.
That shape doesn't work through an `InferenceGateway` today: the gateway's
caller auth reads `Authorization` before anything else, so it can't pass
the login through while also identifying the person, and the model IDs
Claude Code sends don't match a route name. See
[#596](https://github.com/modelplaneai/modelplane/issues/596). This page
will cover it once that lands.
<!-- vale write-good.Passive = YES -->
