---
title: Expose a Model
weight: 20
description: Expose model endpoints via a unified OpenAI-compatible URL.
---
**API:** [`modelplane.ai/v1alpha1` · ModelService]({{< ref "/reference/modelservices" >}})
<!-- vale write-good.Passive = NO -->
A [`ModelDeployment`]({{< ref "model-deployment.md" >}}) serves a model, but its
replicas are scattered across the fleet with no single address. A `ModelService`
gives them one: a stable, unified, OpenAI-compatible URL that load-balances
across every replica, wherever it runs.

A service selects what to route to by label. Behind the scenes, Modelplane
creates one `ModelEndpoint`, a single reachable backend, for each replica of a
deployment and labels it with the deployment's name. It creates an endpoint only
once the replica is Ready, serving and reachable, and withdraws it if the replica
later goes unhealthy. A service only ever routes to replicas that can actually
answer, so a deployment that's still starting or scaling up has fewer endpoints
behind its URL until those replicas come up. You don't create these; you point a
service at them. In the common case that's one selector matching one deployment:

```yaml {nocopy=true}
spec:
  endpoints:
  - selector:
      matchLabels:
        modelplane.ai/deployment: qwen3-8b   # every replica of this deployment
```

`spec.endpoints` is a list, and the entries combine: the service routes to every
endpoint any entry matches. That's how one service can front several deployments
at once, or mix a deployment's replicas with a manually created
[ModelEndpoint]({{< ref "model-endpoint.md" >}}) pointing at an external provider.
Endpoints with different path layouts coexist behind the one URL.

Traffic is split evenly across the matched endpoints. Weighting one entry over
another, for canary or A/B rollouts, is tracked in
[#90](https://github.com/modelplaneai/modelplane/issues/90).

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
