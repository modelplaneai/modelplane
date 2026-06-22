---
title: How Modelplane works
weight: 20
description: The architecture, the resources, and what happens when you deploy a model.
aliases:
  - /concepts/
  - /overview/concepts/
---
<!-- vale write-good.Passive = NO -->
Modelplane runs as a control plane on its own cluster, the **control cluster**,
above the **inference clusters** that actually serve models. It's built on
[Crossplane](https://crossplane.io): platform teams and developers describe what
they want as Kubernetes resources, and Modelplane continuously reconciles the
fleet to match, composing the clusters, scheduling replicas, and exposing
endpoints. This page is the full tour. It covers the architecture and resources, then walks through what happens when you deploy a model.

## Modelplane API

Modelplane's API is two sets of resources, one per team, with everything in
between filled in for you. Platform teams describe the fleet, ML teams describe a
model, and Modelplane composes the rest.

<div class="mp-lanes">
  <div class="mp-lane mp-lane--platform">
    <div class="mp-lane-title">Platform team creates</div>
    <a class="mp-chip" href="{{< ref "/platform/inference-gateway" >}}">
      <span class="mp-chip-name">InferenceGateway</span>
      <span class="mp-chip-desc">The unified, OpenAI-compatible entry point on the control cluster.</span>
    </a>
    <a class="mp-chip" href="{{< ref "/platform/inference-class" >}}">
      <span class="mp-chip-name">InferenceClass</span>
      <span class="mp-chip-desc">A tested hardware recipe for a node pool: the devices it offers and how to provision it.</span>
    </a>
    <a class="mp-chip" href="{{< ref "/platform/inference-cluster" >}}">
      <span class="mp-chip-name">InferenceCluster</span>
      <span class="mp-chip-desc">A Kubernetes cluster in the fleet, provisioned by Modelplane or brought as-is.</span>
    </a>
  </div>
  <div class="mp-lane mp-lane--ml">
    <div class="mp-lane-title">ML team creates</div>
    <a class="mp-chip" href="{{< ref "/models/model-deployment" >}}">
      <span class="mp-chip-name">ModelDeployment</span>
      <span class="mp-chip-desc">A model to serve, with engines, replica count, and an optional cache.</span>
    </a>
    <a class="mp-chip" href="{{< ref "/models/model-service" >}}">
      <span class="mp-chip-name">ModelService</span>
      <span class="mp-chip-desc">One OpenAI-compatible endpoint, weighted across the endpoints it selects.</span>
    </a>
    <a class="mp-chip" href="{{< ref "/models/model-cache" >}}">
      <span class="mp-chip-name">ModelCache</span>
      <span class="mp-chip-desc">Model weights staged once per cluster on shared storage.</span>
    </a>
  </div>
  <div class="mp-lane mp-lane--composed">
    <div class="mp-lane-title">Modelplane composes</div>
    <a class="mp-chip" href="{{< ref "/models/model-deployment" >}}">
      <span class="mp-chip-name">ModelReplica</span>
      <span class="mp-chip-desc">One complete serving instance on a specific cluster.</span>
    </a>
    <a class="mp-chip" href="{{< ref "/models/model-endpoint" >}}">
      <span class="mp-chip-name">ModelEndpoint</span>
      <span class="mp-chip-desc">A reachable endpoint, one per replica or set manually for an external provider.</span>
    </a>
  </div>
</div>

The hierarchy mirrors Kubernetes core one scope up: `ModelDeployment` →
`ModelReplica` → `ModelService` → `ModelEndpoint` parallels `Deployment` → `Pod` → `Service` →
`Endpoint`, across a fleet instead of within a single cluster.

## What the control plane reconciles

Once the resources exist, Modelplane keeps the fleet matching them. Five concerns
run continuously:

1. **Provisioning.** From an `InferenceCluster`, Modelplane creates a full cluster 
   and its GPU node pools, or brings in a cluster you already run on
   any Kubernetes, and installs the serving stack on each.
2. **Scheduling.** A two-level scheduler places work: it pins each `ModelReplica`
   to a cluster and pool whose hardware meets the model's requirements, then the
   cluster's own scheduler binds the GPUs to the serving pods through DRA.
3. **Autoscaling.** Replicas are the scaling axis. Scaling a `ModelDeployment`'s
   `spec.replicas` adds or removes whole serving instances through the standard
   Kubernetes scale subresource, so `kubectl scale` or a KEDA `ScaledObject` work
   out of the box.
4. **Routing.** A `ModelService` exposes one OpenAI-compatible endpoint through
   the gateway and load-balances across the deployment's `ModelEndpoints`, with
   weights for canary and A/B rollouts. `ModelEndpoints` can also fall back to
   external inference services.
5. **Caching.** A `ModelCache` stages model weights on cluster storage once, so
   serving pods read them locally instead of re-downloading on every start.

## Universal compatibility

Modelplane is deliberately unopinionated about the engine. A `ModelDeployment`
describes the *shape* of a deployment, how many pods, on how many nodes, with
which devices, and nothing about how the engine runs internally. The engine flags
you write carry parallelism (tensor, pipeline, data, expert), quantization, and KV
transfer; Modelplane never injects them.

This is what lets one API serve any container-based engine and any topology
without special cases. Modelplane composes the engine onto the right cluster
resource and injects almost nothing, just the address a multi-node leader is
reachable at, so a worker can join it. New engines and new parallelism strategies
work without a change to Modelplane. The community publishes recipes (worked, copyable
manifests) to bridge the gap that flexibility leaves, rather than hard-coding
choices into the API.

## Fleet scheduler

For each replica, the scheduler picks a `(cluster, pool)` in two steps:

1. **Filter clusters** by `clusterSelector.matchLabels` against the standard
   Kubernetes labels on each `InferenceCluster`, the organizational metadata:
   tier, region, provider, compliance posture.
2. **Filter pools** by matching each device request in the deployment's
   `nodeSelector.devices` against the pool's `InferenceClass`. A request is based
   on DRA: a `count` and CEL selectors over a device's attributes and capacity, like
   "a GPU with at least 141Gi of memory." A pool fits when it has the devices the
   model asks for and enough free nodes to hold a replica.

Capacity is accounted at the node level across the fleet, so Modelplane never
overcommits a pool. Replicas are pinned to their cluster once placed and stay
there across reconciles; if a cluster is deleted, the scheduler re-places its
replicas elsewhere.

## Deploying a model

Creating a `ModelDeployment` kicks off the loop end to end. The scheduler
discovers the ready clusters (filtered by your label selector if you set one),
matches each engine's device requests against their pools, and pins each replica
to a cluster that fits. Modelplane composes a `ModelReplica` on each chosen
cluster, turns it into the right serving workload there, creates a `ModelEndpoint`
per replica, and your `ModelService` routes traffic across them through one stable
endpoint on the gateway. Scale the deployment up or down and the same loop
re-converges.

## Serving topologies

A single-node deployment composes to a Kubernetes Deployment fronted by a
service. When a model is too large for one node, an engine becomes a gang: a
`Leader` member and one or more `Worker` members that Modelplane composes into a
LeaderWorkerSet, serving the model together across nodes. Gang deployments stage
their weights through a `ModelCache`, which is required once more than one pod
loads the same model.

Disaggregated serving splits prefill and decode into separate engines
(`serving.mode: PrefillDecode`) that run on the same cluster and hand off the KV
cache between them. Modelplane wires up the cluster-edge routing that pairs each
request's prefill and decode; the engines carry the KV-transfer flags. Both are
described in full in the [model deployment docs]({{< ref "/models/model-deployment" >}}).

## Next steps

{{< cardgroup cols="2" >}}
{{< card title="FAQ" href="/overview/faq/" >}}
Quick answers on how Modelplane compares and what it requires.
{{< /card >}}
{{< card title="Get started" href="/getting-started/" >}}
Put it together: deploy Modelplane and serve a model.
{{< /card >}}
{{< /cardgroup >}}
<!-- vale write-good.Passive = YES -->
