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
front your own replicas and an external provider behind one URL: send overflow to
the provider when your fleet is busy, or fail over to it as a break-glass option.

## Routing to an external provider

Create a `ModelEndpoint` with three things:

{{< manifests "concepts/model-endpoint.yaml" >}}

Then point a [`ModelService`]({{< ref "model-service.md" >}}) at it. Selecting
`modelplane.ai/external-provider: together` routes to the provider; adding a
second entry for a deployment fronts both behind one URL, so traffic can spill
over to the provider alongside your own replicas:

{{< manifests "concepts/model-service-external.yaml" >}}

Anything speaking the OpenAI or Anthropic API works. `origin` is the scheme and
host to reach it at, with no path; `api.prefix` is the path the provider serves
those APIs under, and `api.schema` which of the two it speaks. Only those change
between providers.
<!-- vale write-good.Passive = YES -->
