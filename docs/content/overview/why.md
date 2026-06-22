---
title: Why Modelplane
weight: 10
description: The problem Modelplane solves and how it compares to the alternatives.
---
<!-- vale write-good.TooWordy = NO -->
<!-- vale write-good.Passive = NO -->
Open-weight models are becoming the choice for organizations: they can be
post-trained, including with reinforcement learning, to compete with frontier
models, and they put cost, governance, and data sovereignty back under the
organization's control. As they do, platform teams are 
increasingly asked to provide GPU inference to their ML and development teams the 
same way they already provide cloud infrastructure.

## Kubernetes is becoming the default orchestrator

Kubernetes is rapidly becoming the default orchestrator for inference. The broader 
cloud-native community is investing heavily to make it a first-class platform for
AI workloads, adding device-aware scheduling, multi-node inference, distributed
serving, and accelerator management. The major open source inference projects are
converging on it; among them are vLLM, SGLang, NVIDIA Dynamo, llm-d, Ray, Slurm,
KubeAI, and Kueue. Neoclouds like Baseten and CoreWeave have standardized on
Kubernetes for their own operations. Inside a single cluster, the open source
stack is now strong.

## Inference is a fleet problem

Inference, however, almost always runs across more than one cluster. Accelerator
availability scatters capacity across hardware types, providers, and regions.
Sovereignty and compliance pin workloads to specific locations. Operators run
across multiple clouds and on-premise environments. Large clusters
concentrate failure and risk, so fleets of smaller clusters are often preferable,
and inference workloads don't bin-pack the way other workloads do.

Inference grows into a fleet, and a new set of problems appears above
any single cluster:

- Deciding where each model runs across available capacity.
- Optimizing placement across heterogeneous accelerators.
- Failing over across clouds and regions.
- Routing by cost, latency, and sovereignty requirements.
- Provisioning new capacity as demand grows.
- Caching and distributing model weights across the fleet.
- Managing the lifecycle of models, clusters, and infrastructure as one system.

Open source addresses pieces of this but none brings all the pieces together in a
fleet-wide system of record that manages placement, caching, capacity, policy, and
routing across an entire fleet. The labs, hyperscalers, and managed providers have
all solved these problems in a proprietary way, but the open equivalent does not
yet exist.

## Modelplane extends Kubernetes to manage the fleet

Modelplane does for the fleet what Kubernetes does for the cluster. It's the open
source control plane above your inference clusters across cloud, neocloud, and
on-premise: it places model deployments, autoscales replicas, provisions and
manages the infrastructure underneath, caches and distributes model weights, and
routes inference through one unified gateway with fallback to managed providers.
It turns "I need this model served" into a stable endpoint for any ML team.

Modelplane composes these projects rather than replacing them, and stays neutral
across models, accelerators, clouds, and serving stacks. It's built on
[Crossplane](https://crossplane.io) and extends Kubernetes to manage inference
at the fleet level. Modelplane is open source, Apache 2 licensed, and we plan to
donate it to a neutral open source foundation later this year.

{{< cardgroup cols="2" >}}
{{< card title="How Modelplane works" href="/overview/how-it-works/" >}}
The architecture, the resources, and what happens when you deploy a model.
{{< /card >}}
{{< card title="FAQ" href="/overview/faq/" >}}
How Modelplane compares to cluster orchestrators and managed providers, and what it requires.
{{< /card >}}
{{< /cardgroup >}}
<!-- vale write-good.TooWordy = YES -->
<!-- vale write-good.Passive = YES -->
