---
title: Expose a Model
weight: 20
description: Expose a deployment's replicas as one model a caller can name.
---
**API:** [`modelplane.ai/v1alpha1` · ModelService]({{< ref "/reference/modelservices" >}})
<!-- vale write-good.Passive = NO -->
A [`ModelDeployment`]({{< ref "model-deployment.md" >}}) serves a model, but its
replicas are scattered across the fleet with no single name. A `ModelService`
gives them one: a stable name that load-balances across every replica, wherever
it runs. A caller names it as the model in an ordinary OpenAI or Anthropic
request to a gateway that serves it.

A service selects what to route to by label. Behind the scenes, Modelplane
creates one `ModelEndpoint`, a single reachable backend, for each replica of a
deployment and labels it. Two of those labels carry routing intent:

- `modelplane.ai/deployment`: the deployment the replica belongs to.
- `modelplane.ai/cluster`: the cluster the replica runs on.

An `InferenceCluster` adds its own labels too. Whatever you put under its
`spec.placement.metadata.labels` lands on every endpoint and replica scheduled
there, so a service can select on a property of the cluster, like its region.

Modelplane creates an endpoint only once its replica is Ready, serving and
reachable, and withdraws it if the replica later goes unhealthy. A service only
ever routes to replicas that can actually answer, so a deployment that's still
starting or scaling up has fewer endpoints behind it until those replicas
come up. You don't create endpoints yourself. You point a service at them.

`spec.endpoints` is a list, and the entries combine: the service routes to every
endpoint that any entry matches. The patterns below build on that.

## Route to a whole deployment

The common case: one selector matching a deployment's name reaches every replica,
wherever in the fleet they run.

```yaml {nocopy=true}
spec:
  endpoints:
  - name: qwen3-8b
    selector:
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
  - name: qwen3-8b-us-east
    selector:
      matchLabels:
        modelplane.ai/deployment: qwen3-8b
        modelplane.ai/cluster: prod-us-east
```

## Route across several deployments

Give more than one entry to front several deployments under the same model name. Each
entry contributes its matched endpoints. By default every entry carries equal
weight, so traffic splits evenly between entries and then spreads as evenly as
possible across the endpoints each one matches.

```yaml {nocopy=true}
spec:
  endpoints:
  - name: qwen3-8b
    selector:
      matchLabels:
        modelplane.ai/deployment: qwen3-8b
  - name: qwen3-8b-v2
    selector:
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
  - name: stable
    weight: 95
    selector:
      matchLabels:
        modelplane.ai/deployment: qwen3-8b
  - name: canary
    weight: 5
    selector:
      matchLabels:
        modelplane.ai/deployment: qwen3-8b-v2
```

The entries don't have to be deployments. One can select a manually created
[ModelEndpoint]({{< ref "model-endpoint.md" >}}) that points at an external
provider, so one model name covers both your own replicas and a SaaS endpoint.
At equal priority the two share traffic by weight. To send the provider only the
traffic your replicas can't serve, see [failover tiers](#failover-tiers) below.

```yaml {nocopy=true}
spec:
  endpoints:
  - name: kimi-k2
    selector:
      matchLabels:
        modelplane.ai/deployment: kimi-k2
  - name: together
    selector:
      matchLabels:
        modelplane.ai/external-provider: together
```

## Failover tiers

`priority` orders entries into tiers. Lower is preferred. A tier takes a growing
share of traffic as the tiers above it lose healthy endpoints, and takes over
entirely once they have none. Put your own replicas at priority 0 and a
third-party provider at priority 1, and the provider becomes a backup for the
traffic your replicas can't serve. Entries that share a priority split traffic
by weight, as above.

```yaml {nocopy=true}
spec:
  endpoints:
  - name: self
    priority: 0
    selector:
      matchLabels:
        modelplane.ai/deployment: kimi-k2
  - name: together
    priority: 1
    selector:
      matchLabels:
        modelplane.ai/external-provider: together
```

## Timeouts

`timeouts` sets how long a gateway waits on the service's endpoints. `request`
bounds a whole request, retries included. `idle` is how long an endpoint may
send nothing. Before the first byte, the gateway gives up on the endpoint, which
counts against its health, and retries the request, on another endpoint if
there is one. Each retry starts the response again, so an `idle` shorter than a
response that isn't streamed makes the backend generate it up to four times
before the caller gets a 504. After the first byte, the stream is cut short.
They default to `300s` and `60s`.

Whether a response sends anything early depends on whether the caller streams. A
streamed response starts after prefill, so `idle` bounds time to first token and
every gap between chunks after it. A response that isn't streamed sends nothing
until it's complete. If any of a
service's callers don't stream, set `idle` at least as long as `request`, or to
`0s` to disable it.

```yaml {nocopy=true}
spec:
  timeouts:
    request: 600s
    idle: 0s
```

Tune both from what the gateway measures. AI Gateway's
`gen_ai.server.time_to_first_token` and `gen_ai.server.request.duration` metrics
give each model's latencies, and Envoy's
`envoy_cluster_upstream_rq_per_try_idle_timeout` counts idle timeouts. If that
count rises while the endpoints are healthy, `idle` is too short. A restarting
gateway lets requests already in flight run for five minutes, so a restart can
cut off a response allowed longer than that.

## How a service reaches its gateways

An `InferenceGateway` names the services it serves, through a `serviceSelector`
that matches a service's labels. A gateway with no selector serves every service.
Label a service for a region and give that region's gateways a matching selector,
and only they serve it.

For every gateway that serves it, Modelplane composes a `ModelRoute` that renders
the routing onto that gateway's cluster. You don't write `ModelRoute`s.
`status.routes` counts them, and `kubectl get modelroutes -l
modelplane.ai/service=<name>` shows each one, its gateway, and whether the route
is ready there. Look there when a service is Ready but a gateway isn't serving
it.

## Sending a request

A caller names the model as `<namespace>/<service>`. A gateway without TLS
publishes a base URL per API it speaks:

```bash
ADDRESS=$(kubectl get ig public -o jsonpath='{.status.endpoints.openAI}')
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
an Anthropic request for an endpoint that speaks OpenAI, as the engines
Modelplane runs do, so a caller of your own replicas can use either: the gateway
serves the Messages API under `/anthropic/v1`, and a client that speaks it,
including Claude Code via `ANTHROPIC_BASE_URL`, needs nothing else. See
[the Messages API guide]({{< ref "/guides/anthropic-messages-api" >}}).

It doesn't translate the other way. An endpoint whose `api.schema` is
`Anthropic` serves only Anthropic callers, and an OpenAI request routed to it
fails, so don't mix one into a service that OpenAI callers use.

Scrape an engine's own operational paths like `/metrics` and `/health` from the
replica directly. See
[Collecting engine metrics]({{< ref "/guides/collecting-engine-metrics" >}}).

## Example

{{< manifests "concepts/model-service.yaml" >}}
<!-- vale write-good.Passive = YES -->
