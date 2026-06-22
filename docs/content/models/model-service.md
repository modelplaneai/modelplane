---
title: Expose a Model
weight: 20
description: Expose model endpoints via a unified OpenAI-compatible URL.
---
**API:** [`modelplane.ai/v1alpha1` · ModelService]({{< ref "/reference/modelservices" >}})
<!-- vale write-good.Passive = NO -->
A `ModelService` exposes one or more `ModelEndpoints` via a unified,
OpenAI-compatible endpoint. It selects endpoints by label and composes a Gateway
API `HTTPRoute` that load-balances across them.


Each backendRef in the HTTPRoute carries its own `URLRewrite` filter derived from
the endpoint's `spec.rewritePath`, so endpoints from different deployments or
external providers with different path layouts coexist correctly.

The route matches the `/<namespace>/<service>/` prefix and forwards everything
below it to the engine, so the endpoint speaks whatever API the engine serves.
OpenAI compatibility comes from the engines, not the route. An engine that also exposes
another protocol is reachable on the same URL: a vLLM replica that serves the
Anthropic Messages API answers on `/v1/messages`, so a client that speaks it
(including Claude Code, via `ANTHROPIC_BASE_URL`) talks to it directly. The
engine's operational paths come through the same way: `/health` and the
Prometheus `/metrics` are reachable on the service URL. The prefill/decode and
caching routers parse OpenAI-format request bodies, so an endpoint that serves
another shape uses a plain `ModelService` with even weighting rather than those
routers.

Read the service's public address from `status.address`:

```bash
kubectl get ms qwen -n ml-team -o jsonpath='{.status.address}'
```

## Example

{{< manifests "concepts/model-service.yaml" >}}
<!-- vale write-good.Passive = YES -->
