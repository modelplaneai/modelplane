---
title: Why Modelplane
weight: 10
description: The problem Modelplane solves and how it compares to the alternatives.
---
<!-- vale write-good.Passive = NO -->
Open-weight models are becoming the default for serious inference. Cost control,
governance, and data sovereignty all push organizations away from hosted
proprietary APIs and toward running open-weight models on infrastructure they
own. Kubernetes is where that runs, and platform teams are now asked to provide
GPU inference to their ML teams the same way they already provide cloud
infrastructure.

## Serving one model is solved. The fleet isn't.

Inside a single cluster, the open ecosystem is strong. vLLM, SGLang, and others 
serve models. LeaderWorkerSet runs multi-node topologies. Dynamic Resource Allocation
(DRA) binds GPUs to pods. llm-d adds model-aware routing and prefill/decode
coordination. NVIDIA Dynamo brings KV-cache management and GPU-to-GPU weight
transfer. Running a model on one Kubernetes cluster is, increasingly, a solved
problem.

The hard part is the fleet. GPU capacity is scarce and scattered: some in a
hyperscaler, some on a neocloud, some on hardware you already own, across more
than one region. Serving models on it means scheduling each model to the right
hardware, routing traffic across clusters to a stable endpoint, accounting for
capacity fleet-wide, and giving ML teams self-service without giving up
governance. These problems sit *above* any single cluster. Every team that serves
models at scale ends up building it themselves, in private.

## What Modelplane does instead

Modelplane is the fleet control plane you'd otherwise build, as open source that
runs in your own clusters. You describe your GPU fleet and your models as
Kubernetes resources, and Modelplane reconciles the rest. It's the same move
platform teams already made with [Crossplane](https://crossplane.io) for cloud
infrastructure, applied one layer down to inference.

- **One fleet, many clouds.** Modelplane treats every cluster, cloud, and region
  as one pool. It provisions clusters or runs on existing clusters on cloud, 
  neocloud, or on-premise.
- **One endpoint per service.** Every model is exposed through a single
  OpenAI-compatible endpoint, with weighted routing for canary and A/B rollouts
  across replicas, and fallback to external providers when you want it.
- **A clean team boundary.** Platform teams set capacity and policy once;
  developers deploy against it without filing tickets for infrastructure.
- **Yours, end to end.** The models, the data, and the clusters stay under your
  control. Modelplane is [Apache 2.0](https://github.com/modelplaneai/modelplane/blob/main/LICENSE)
  and neutral across models, engines, accelerators, and clouds, so there's no
  proprietary control plane to lock into.

## Next steps

{{< cardgroup cols="2" >}}
{{< card title="How Modelplane works" href="/overview/how-it-works/" >}}
The architecture, the two-team boundary, and what happens when you deploy a model.
{{< /card >}}
{{< card title="FAQ" href="/overview/faq/" >}}
How Modelplane compares to cluster orchestrators and managed providers, and what it requires.
{{< /card >}}
{{< /cardgroup >}}
<!-- vale write-good.Passive = YES -->
