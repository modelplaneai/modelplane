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

```yaml {nocopy=true}
apiVersion: modelplane.ai/v1alpha1
kind: ModelEndpoint
metadata:
  name: kimi-k2-together
  namespace: ml-team
  labels:
    # 1. A label of your own for a ModelService to select on. Any label
    #    works; modelplane.ai/external-provider is a readable convention.
    modelplane.ai/external-provider: together
spec:
  # 2. The provider's base URL.
  url: https://api.together.xyz/
  # 3. The path to rewrite requests to. A ModelService receives requests at
  #    /<namespace>/<service>/v1/... and strips only the /<namespace>/<service>/
  #    prefix, so an OpenAI-compatible provider that already serves /v1/...
  #    takes just /.
  rewritePath: /
```

Then point a [`ModelService`]({{< ref "model-service.md" >}}) at it. Selecting
`modelplane.ai/external-provider: together` routes to the provider; adding a
second entry for a deployment fronts both behind one URL, so traffic can spill
over to the provider alongside your own replicas:

```yaml {nocopy=true}
apiVersion: modelplane.ai/v1alpha1
kind: ModelService
metadata:
  name: kimi-k2
  namespace: ml-team
spec:
  endpoints:
  - selector:
      matchLabels:
        modelplane.ai/deployment: kimi-k2          # your own replicas
  - selector:
      matchLabels:
        modelplane.ai/external-provider: together  # the endpoint above
```

The provider must speak the OpenAI API, since that's the contract a
`ModelService` exposes. Anything OpenAI-compatible works; `url` and `rewritePath`
are all that change between providers.
<!-- vale write-good.Passive = YES -->

## Example

{{< manifests "concepts/model-endpoint.yaml" >}}
