---
title: Why Modelplane
weight: 10
description: The problem Modelplane solves and how it compares to the alternatives.
---
<!-- vale write-good.Passive = NO -->
Serving your own models in production is a fleet problem long before it's a model
problem. GPU capacity is scarce and scattered: some in a hyperscaler, some on a
neocloud, some on hardware you already own. Getting a model in front of users
means provisioning clusters, scheduling replicas onto the right GPUs, scaling
them with demand, routing traffic to a stable endpoint, and staging weights so
pods don't re-download them on every restart. Every team that serves models ends
up building the same control plane to do it.

Most teams take one of two paths, and each costs something.

- **Build it yourself on Kubernetes.** You keep full control, but you own all the
  glue: cluster provisioning, GPU scheduling, autoscaling, gateways, and caching,
  across every cloud you use. That's a platform to build and maintain, not a
  feature.
- **Buy managed inference as a service.** You skip the glue, but you give up
  control: your models and traffic run on someone else's infrastructure, you take
  on lock-in, and you serve where the vendor has capacity rather than where you do.

## What Modelplane does instead

Modelplane is the control plane you'd otherwise build, as open source that runs in
your own clusters. You describe your GPU fleet and your models as resources, and
Modelplane reconciles the rest.

- **One fleet, many clouds.** Modelplane treats every cluster, cloud, and region
  as one pool. It provisions GKE and EKS clusters, and brings in any other
  Kubernetes cluster, on a neocloud or on-prem, that you point it at.
- **One endpoint per service.** Every model is exposed through a single
  OpenAI-compatible endpoint, with weighted routing for canary and A/B rollouts
  across replicas and endpoints.
- **A clean team boundary.** Platform teams set capacity and policy once;
  developers deploy against it without filing tickets for infrastructure.
- **Yours, end to end.** The models, the data, and the clusters stay under your
  control. Modelplane is [Apache 2.0](https://github.com/modelplaneai/modelplane/blob/main/LICENSE)
  and builds on open [Crossplane](https://crossplane.io), so there's
  no proprietary control plane to lock into.

## Where it stands today

Modelplane is at v0.1: early, focused, and moving fast. What ships today provisions
GKE and EKS (and runs on any cluster you bring), schedules and scales replicas,
routes through one OpenAI-compatible endpoint, and caches weights from Hugging
Face. See [How Modelplane works]({{< ref "/overview/how-it-works" >}}) for the
mechanics, and the [platform docs]({{< ref "/platform" >}}) for provisioning
clusters across clouds, accelerators, and engines, including what's on the roadmap.
<!-- vale write-good.Passive = YES -->
