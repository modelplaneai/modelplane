---
title: Route to External Providers
weight: 40
description: A reachable inference endpoint, composed per replica or created manually for external providers.
---
**API:** [`modelplane.ai/v1alpha1` · ModelEndpoint]({{< ref "/reference/modelendpoints" >}})
<!-- vale write-good.Passive = NO -->
A `ModelEndpoint` is a single reachable inference endpoint that a
[`ModelService`]({{< ref "model-service.md" >}}) can route to. Modelplane creates
one for each of your replicas automatically, but you can also create one by hand
to point at an inference endpoint Modelplane doesn't run, most often a SaaS
provider like Together or Baseten. A service treats both the same, so you can
front your own replicas and an external provider as one model, splitting traffic
between them or keeping the provider as a backup.

## Routing to an external provider

Create a `ModelEndpoint` with the five things the manifest numbers:

{{< manifests "concepts/model-endpoint.yaml" >}}

Then point a [`ModelService`]({{< ref "model-service.md" >}}) at it. Selecting
`modelplane.ai/external-provider: together` routes to the provider; adding a
second entry for a deployment fronts both as one model, splitting its traffic
between them by weight:

{{< manifests "concepts/model-service-external.yaml" >}}

To keep the provider as a backup instead, see
[failover tiers]({{< ref "model-service.md#failover-tiers" >}}).

Anything speaking the OpenAI or Anthropic API works. `origin` is the scheme and
host to reach it at, with no path; `api.prefix` is the path the provider serves
those APIs under, and `api.schema` which of the two it speaks. Only those change
between providers.
<!-- vale write-good.Passive = YES -->
