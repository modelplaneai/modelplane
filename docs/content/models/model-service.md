---
title: Expose a Model
weight: 20
description: Expose model endpoints via a unified OpenAI-compatible URL.
---
**API:** [`modelplane.ai/v1alpha1` · ModelService]({{< ref "reference#crd-modelservice" >}})
<!-- vale write-good.Passive = NO -->
A `ModelService` exposes one or more `ModelEndpoints` via a unified,
OpenAI-compatible endpoint. It selects endpoints by label and composes a Gateway
API `HTTPRoute` that load-balances across them.


Each backendRef in the HTTPRoute carries its own `URLRewrite` filter derived from
the endpoint's `spec.rewritePath`, so endpoints from different deployments or
external providers with different path layouts coexist correctly.

Read the service's public address from `status.address`:

```bash
kubectl get ms qwen -n ml-team -o jsonpath='{.status.address}'
```

## Example

{{< manifests "concepts/model-service.yaml" >}}
<!-- vale write-good.Passive = YES -->
