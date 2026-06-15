---
title: Understand Replicas
weight: 50
description: One instance of a ModelDeployment placed on a specific cluster. Created automatically by Modelplane.
---
**API:** [`modelplane.ai/v1alpha1` · ModelReplica]({{< ref "reference.md" >}}#crd-modelreplica)
<!-- vale write-good.Passive = NO -->
The `ModelDeployment` composition function creates `ModelReplicas`. Don't create
them directly.


Each replica represents a model deployed to a specific cluster. It reads the
replica's engines and composes a workload per engine from its member roles: a
native Kubernetes Deployment for a Standalone engine, or an llm-d
LeaderWorkerSet for a Leader/Worker gang. One Service and HTTPRoute, spanning
every engine's serving pods, front the replica as a unified OpenAI-compatible
endpoint.

## Example

{{< manifests "deployment/model-replica.yaml" >}}
<!-- vale write-good.Passive = YES -->
