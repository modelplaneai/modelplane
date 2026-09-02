---
title: Set Up the Gateway
weight: 10
description: The OpenAI-compatible front door callers reach your models through.
---
**API:** [`modelplane.ai/v1alpha1` · InferenceGateway]({{< ref "/reference/inferencegateways" >}})
<!-- vale write-good.Passive = NO -->
The `InferenceGateway` is the front door for inference requests: the
OpenAI-compatible address a caller sees, which routes each request on to a
cluster serving the model it asked for.

It runs on an `InferenceCluster`, named by `spec.clusterName`, because that
cluster already runs the gateway software. It installs nothing on your control
plane. The cluster it runs on needs no GPU pools: one with none is a gateway and
nothing else.

Create as many as you need. A gateway is where a request enters your fleet, so
you want one per place requests should enter from, and `spec.serviceSelector`
decides which `ModelService`s each one serves. Scoping a gateway to a region is
how residency is expressed: a service labelled for the EU reaches only EU
gateways, and from there only the endpoints it selects. Left unset, a gateway
serves every service.

A gateway doesn't fail over. Availability comes from running more of them,
because failing over would change the address callers use and could move traffic
out of the jurisdiction the gateway exists to hold.

Set `spec.hostname` and `spec.tls.certificateRefs` to answer on a name over
TLS, which is the shape you want in production. Point that name at the address
the gateway publishes:

```bash
kubectl get ig eu -o jsonpath='{.status.address}'
```

Callers reach a model by naming it, not by path: the model in an OpenAI request
body is `<namespace>/<service>`, and the gateway rewrites it to whatever the
engine was started as, so one address serves every model. And
`GET /v1/models` lists what this gateway will route.

Use `spec.auth.secretSelector` to authenticate callers. Each key in a selected
Secret is one caller: the entry's name is the identity and its value is the key,
so adding a caller means writing a Secret rather than editing the gateway. The
gateway stamps the identity onto every request and usage record, and never
forwards the caller's key to a model. Without `auth` the gateway authenticates
nobody, which is deliberate: it's the shape for running behind something that
already has.
## Example

{{< manifests "concepts/inference-gateway.yaml" >}}
<!-- vale write-good.Passive = YES -->
