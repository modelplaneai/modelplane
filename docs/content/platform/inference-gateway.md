---
title: Set Up the Gateway
weight: 10
description: Unified OpenAI-compatible endpoint on the control plane cluster.
---
**API:** [`modelplane.ai/v1alpha1` · InferenceGateway]({{< ref "/reference/inferencegateways" >}})
<!-- vale write-good.Passive = NO -->
The `InferenceGateway` creates a unified, OpenAI-compatible endpoint on the
control plane cluster. It installs [Traefik Proxy](https://traefik.io) and
creates a Gateway that routes requests to model endpoints on remote inference
clusters.


Create one `InferenceGateway` named `default` per control plane. When
running the control plane in kind, set `loadBalancer: MetalLB` to get a
LoadBalancer IP inside the Docker network.

## Two layers of routing

Modelplane routes inference requests through two gateways, and the
`InferenceGateway` is the first of them:

1. **The control-plane gateway** is what the `InferenceGateway` configures. It's
   the front door: one OpenAI-compatible address that a `ModelService` exposes,
   routing each request to the right inference cluster's edge. Modelplane runs
   Traefik here because it supports `URLRewrite` filters on each `backendRef`,
   which the routing to each replica's path depends on.
2. **A per-cluster gateway** runs on each inference cluster, routing from the
   cluster edge to the model engines (vLLM and the like). Modelplane installs and
   configures this as part of the cluster's serving stack; it isn't something the
   platform team sets up directly.

The `backend` discriminator selects which gateway runs on the control plane.
`Traefik` is the only value today.

Once ready, read the gateway's external address from the resource's status:

```bash
kubectl get ig default
```
## Example

{{< manifests "concepts/inference-gateway.yaml" >}}
<!-- vale write-good.Passive = YES -->
