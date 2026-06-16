---
title: Route to External Providers
weight: 40
description: A reachable inference endpoint, composed per replica or created manually for external providers.
---
**API:** [`modelplane.ai/v1alpha1` · ModelEndpoint]({{< ref "reference#crd-modelendpoint" >}})
<!-- vale write-good.Passive = NO -->
A `ModelEndpoint` is a reachable inference endpoint. Modelplane composes one per
`ModelReplica`, but ML teams can also create them manually for external SaaS
providers (Together, BaseTen).


Each endpoint composes an Envoy Gateway `Backend` on the control plane.
`ModelEndpoint` surfaces the Backend's name in `status.routing.backendName` so
`ModelService` can reference it in its HTTPRoute.
<!-- vale write-good.Passive = YES -->

## Example

{{< manifests "deployment/model-endpoint.yaml" >}}
