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
deployment and labels it. Two of those labels carry routing intent:

- `modelplane.ai/deployment`: the deployment the replica belongs to.
- `modelplane.ai/cluster`: the cluster the replica runs on.

Modelplane creates an endpoint only once its replica is Ready, serving and
reachable, and withdraws it if the replica later goes unhealthy. A service only
ever routes to replicas that can actually answer, so a deployment that's still
starting or scaling up has fewer endpoints behind its URL until those replicas
come up. You don't create endpoints yourself. You point a service at them.

`spec.endpoints` is a list, and the entries combine: the service routes to every
endpoint that any entry matches. The patterns below build on that.

## Route to a whole deployment

The common case: one selector matching a deployment's name reaches every replica,
wherever in the fleet they run.

```yaml {nocopy=true}
spec:
  endpoints:
  - selector:
      matchLabels:
        modelplane.ai/deployment: qwen3-8b   # every replica of this deployment
```

## Route to part of a deployment

Add a second label to narrow within a deployment. A selector matches an endpoint
only when all its labels match, so pairing the deployment with a cluster routes to
just that cluster's replicas. This is how you take a cluster out of service
without redeploying: point the service at the clusters you want and leave one out,
and traffic drains to the rest.

```yaml {nocopy=true}
spec:
  endpoints:
  # Only the replicas on prod-us-east, e.g. while draining another cluster.
  - selector:
      matchLabels:
        modelplane.ai/deployment: qwen3-8b
        modelplane.ai/cluster: prod-us-east
```

## Route across several deployments

Give more than one entry to front several deployments behind the same URL. Each
entry contributes its matched endpoints, and traffic spreads evenly across every
one.

```yaml {nocopy=true}
spec:
  endpoints:
  - selector:
      matchLabels:
        modelplane.ai/deployment: qwen3-8b
  - selector:
      matchLabels:
        modelplane.ai/deployment: qwen3-8b-v2
```

This is the shape an A/B test or a canary rollout would take, but note traffic is
split **evenly** across the matched endpoints today. Weighting one entry over
another, to send, say, 5% of traffic to a canary, is tracked in
[#90](https://github.com/modelplaneai/modelplane/issues/90). Until then the split
follows endpoint counts, not a ratio you set.

The entries don't have to be deployments. One can select a manually created
[ModelEndpoint]({{< ref "model-endpoint.md" >}}) that points at an external
provider, so a service can send overflow or break-glass traffic to a SaaS
endpoint alongside your own replicas:

```yaml {nocopy=true}
spec:
  endpoints:
  - selector:
      matchLabels:
        modelplane.ai/deployment: kimi-k2
  - selector:
      matchLabels:
        modelplane.ai/external-provider: together
```

Endpoints with different path layouts coexist behind the one URL.

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

## Sending a request

The service's public address is on `status.address`, in the form
`http://<gateway>/<namespace>/<service-name>`:

```bash
ADDRESS=$(kubectl get ms qwen -n ml-team -o jsonpath='{.status.address}')
```

It's an OpenAI-compatible endpoint, so append the OpenAI path and send a request.
The `model` field is the name the engine serves (its `--served-model-name`, or the
model's Hugging Face id if you didn't set one):

```bash
curl "$ADDRESS/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

## Example

{{< manifests "concepts/model-service.yaml" >}}
<!-- vale write-good.Passive = YES -->
