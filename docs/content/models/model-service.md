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
entry contributes its matched endpoints. By default every entry carries equal
weight, so traffic splits evenly between entries and then spreads as evenly as
possible across the endpoints each one matches.

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

## Split traffic by weight

Set a `weight` on an entry to give it a fixed share of traffic instead of an
equal one. Weights are relative: an entry weighted 80 next to one weighted 20
takes 80% of requests. The weight applies to the entry as a whole and spreads
as evenly as possible across the endpoints it matches, so scaling a deployment
up or down doesn't change its share. An entry without a `weight` defaults to 1.

This is the shape of a canary rollout: send most traffic to the stable deployment
and a sliver to the new one, then shift the ratio as confidence grows.

```yaml {nocopy=true}
spec:
  endpoints:
  - weight: 95
    selector:
      matchLabels:
        modelplane.ai/deployment: qwen3-8b
  - weight: 5
    selector:
      matchLabels:
        modelplane.ai/deployment: qwen3-8b-v2
```

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

Endpoints served by different providers, on different paths, coexist behind the
one model name.

## Sending a request

A caller names the model rather than a path. The name is
`<namespace>/<service>`, and `status.gateways` lists the gateways serving it;
each publishes a base URL per API it speaks:

```bash
ADDRESS=$(kubectl get ig local -o jsonpath='{.status.endpoints.openAI}')
```

Send a request naming the service. The gateway rewrites the name to whatever
each endpoint's engine or provider expects, so one name reaches replicas and
third-party providers alike:

```bash
curl "$ADDRESS/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "ml-team/qwen",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

`GET $ADDRESS/models` lists every model that gateway will route, which is how a
caller discovers the name.

## Alternate APIs

The gateway speaks the OpenAI API and Anthropic's Messages API, and translates
between them and whatever an endpoint speaks, so a caller can use either
regardless of the engine behind it: `status.endpoints.anthropic` is the base URL
for the Messages API, and a client that speaks it, including Claude Code via
`ANTHROPIC_BASE_URL`, needs nothing else. See
[the Messages API guide]({{< ref "/guides/anthropic-messages-api" >}}).

Because the gateway resolves a model name rather than forwarding a path, an
engine's own operational paths are not exposed through it. Scrape `/metrics` and
`/health` from the replica, not through the gateway. See
[Collecting engine metrics]({{< ref "/guides/collecting-engine-metrics" >}}).

There's one exception to the translation, and it's set by the deployment rather
than the service.
[Disaggregated serving]({{< ref "model-deployment.md#disaggregated-serving" >}})
reads OpenAI-format request bodies to pick a prefill and decode worker, so a
request that arrives in another API shape still reaches the engine but skips
that cache-aware routing. Unified serving forwards every API shape the same way.

## Example

{{< manifests "concepts/model-service.yaml" >}}
<!-- vale write-good.Passive = YES -->
