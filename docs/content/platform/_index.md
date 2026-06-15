---
title: Build the Inference Stack Platform
weight: 20
description: Resources for platform engineers provisioning GPU clusters and hardware classes for ML teams.
---
<!-- vale write-good.Passive = NO -->
Platform teams provision infrastructure and define hardware classes.

## Resources

- [InferenceGateway]({{< ref "inference-gateway.md" >}}) - unified OpenAI-compatible endpoint on the control plane
- [InferenceClass]({{< ref "inference-class.md" >}}) - hardware recipe defining GPU type, count, and provisioning
- [InferenceCluster]({{< ref "inference-cluster.md" >}}) - a Kubernetes cluster registered for model serving
- [ServingStack]({{< ref "serving-stack.md" >}}) - the serving substrate Modelplane installs on every managed cluster
<!-- vale write-good.Passive = YES -->
