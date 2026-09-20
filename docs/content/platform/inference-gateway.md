---
title: Set Up the Gateway
weight: 33
description: The front door callers reach your models through, speaking the OpenAI and Anthropic APIs.
---
**API:** [`modelplane.ai/v1alpha1` · InferenceGateway]({{< ref "/reference/inferencegateways" >}})
<!-- vale write-good.Passive = NO -->
The `InferenceGateway` is the front door for inference requests: the address a
caller sees. It speaks the OpenAI and Anthropic APIs and routes each request on
to a cluster serving the model it asked for.

It runs on an `InferenceCluster`, named by `spec.clusterName`. The cluster it
names can serve models too, or run the gateway alone.

Create as many as you need, one per cluster. A second gateway naming a cluster
that already has one reports `ClusterAlreadyHasGateway` and doesn't become
ready. A gateway is where a request enters your fleet, so run one per place
requests should enter from. `spec.serviceSelector` decides
which `ModelService`s each one serves. Left unset, a gateway serves every
service. Scoping a gateway to a region is how you express residency: label a
service for the EU and it reaches only EU gateways, and from there only the
endpoints it selects.

Set `spec.tls.certificateRefs` to serve over TLS, with certificates for the names
callers will use. The names are yours: point your DNS at the address the gateway
publishes.

```bash
kubectl get ig eu -o jsonpath='{.status.address}'
```

Callers then reach the OpenAI API at `https://<name>/v1`, and Anthropic's
Messages API at `https://<name>/anthropic/v1`. A gateway without TLS publishes
these URLs, built from its address, as `status.endpoints`.

Callers reach a model by naming it: the model in a request body is
`<namespace>/<service>`, and the gateway rewrites it to the name each backend
knows the model by, so one address serves every model. `GET /v1/models` lists
what this gateway routes.

To authenticate callers by API key, set `spec.auth.method` to `APIKey` and
`spec.auth.apiKey.secretSelector` to select Secrets holding the keys. Each key
in a selected Secret is one caller: the entry's name is the identity and its
value is the key. A caller sends its key as `Authorization: Bearer <key>`, as
OpenAI clients do, or in `x-api-key`, as Anthropic clients do. The gateway
stamps the identity onto every request and usage record, and never forwards
the caller's key to a model.

The gateway is cluster-scoped, so it selects these Secrets from `modelplane-system`
on the control plane. Label each to match the `secretSelector`, with one entry
per caller:

```yaml {nocopy=true}
apiVersion: v1
kind: Secret
metadata:
  name: inference-keys
  namespace: modelplane-system
  labels:
    modelplane.ai/inference-keys: "true"
stringData:
  alice: sk-alice-...
  bob: sk-bob-...
```

## Run behind another gateway

Without `spec.auth` the gateway authenticates nobody. That's the shape for
running behind a gateway that already does: the upstream sets the
`x-modelplane-caller` header to name the caller it authenticated, and the
gateway trusts it. You have to ensure traffic reaches this gateway only through
that front, so nothing else can set the header.

## Example

{{< manifests "concepts/inference-gateway.yaml" >}}
<!-- vale write-good.Passive = YES -->
