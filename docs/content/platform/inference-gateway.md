---
title: Set Up the Gateway
weight: 10
description: The front door callers reach your models through, speaking the OpenAI and Anthropic APIs.
---
**API:** [`modelplane.ai/v1alpha1` · InferenceGateway]({{< ref "/reference/inferencegateways" >}})
<!-- vale write-good.Passive = NO -->
The `InferenceGateway` is the front door for inference requests: the address a
caller sees. It speaks the OpenAI and Anthropic APIs and routes each request on
to a cluster serving the model it asked for.

It runs on an `InferenceCluster`, named by `spec.clusterName`. A gateway has to
run somewhere, and Modelplane reuses `InferenceCluster` rather than a separate
kind for it. The cluster it names can serve models too, or run the gateway
alone: a cluster with no GPU pools hosts a gateway and nothing else.

Create as many as you need. A gateway is where a request enters your fleet, so
run one per place requests should enter from. `spec.serviceSelector` decides
which `ModelService`s each one serves. Left unset, a gateway serves every
service. Scoping a gateway to a region is how you express residency: label a
service for the EU and it reaches only EU gateways, and from there only the
endpoints it selects.

Set `spec.hostname` and `spec.tls.certificateRefs` to answer on a name over TLS.
The name is yours: point your DNS at the address the gateway publishes.

```bash
kubectl get ig eu -o jsonpath='{.status.address}'
```

Callers reach a model by naming it: the model in a request body is
`<namespace>/<service>`, and the gateway rewrites it to whatever the engine was
started as, so one address serves every model. `GET /v1/models` lists what this
gateway routes.

Use `spec.auth.secretSelector` to authenticate callers. Each key in a selected
Secret is one caller: the entry's name is the identity and its value is the key.
The gateway stamps the identity onto every request and usage record, and never
forwards the caller's key to a model.

## Run behind another gateway

Without `spec.auth` the gateway authenticates nobody. That's the shape for
running behind a gateway that already does: the upstream sets the
`x-modelplane-caller` header to name the caller it authenticated, and the
gateway trusts it. You have to ensure traffic reaches this gateway only through
that front, so nothing else can set the header.

## Example

{{< manifests "concepts/inference-gateway.yaml" >}}
<!-- vale write-good.Passive = YES -->
