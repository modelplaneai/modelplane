---
title: Manage the Serving Stack
weight: 40
description: The serving substrate Modelplane installs on every managed cluster.
---
**API:** [`infrastructure.modelplane.ai/v1alpha1` · ServingStack]({{< ref "reference.md" >}}#crd-servingstack)
<!-- vale write-good.Passive = NO -->
A `ServingStack` installs the serving substrate on a Kubernetes cluster:
LeaderWorkerSet, Gateway API, cert-manager, and Prometheus. Modelplane composes
one automatically on every `InferenceCluster` it manages — don't create these
directly.


Platform teams interact with the serving stack indirectly: Modelplane installs
and upgrades it as part of cluster lifecycle. If a cluster's stack is
unhealthy, check the `ServingStack` status on the workload cluster:

```bash
kubectl get servingstack -n modelplane-system
```
<!-- vale write-good.Passive = YES -->


## Example

{{< manifests "platform/serving-stack.yaml" >}}


