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


Create one `InferenceGateway` per control plane. It must be named `default`. When
running the control plane in kind, set `loadBalancer: MetalLB` to get a
LoadBalancer IP inside the Docker network.

Once ready, read the gateway's external address from the resource's status:

```bash
kubectl get ig default
```
## Example

{{< manifests "concepts/inference-gateway.yaml" >}}
<!-- vale write-good.Passive = YES -->
