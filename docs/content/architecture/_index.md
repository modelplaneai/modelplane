---
title: Architecture
weight: 40
navLabel: "Architecture"
navLanding: "How it's built"
description: How Modelplane is built, the Crossplane foundation, the composition-function model, and the choices behind them.
---
<!-- vale write-good.Passive = NO -->
This section is for people who want to know how Modelplane works under the hood:
contributors, and the curious who want to weigh up its design before they commit
to it. It assumes you've read [How Modelplane works]({{< ref "/overview/how-it-works.md" >}})
and are comfortable with Kubernetes and Crossplane. If you're trying to deploy a
model or run a fleet, the [Models]({{< ref "/models" >}}) and
[Platform]({{< ref "/platform" >}}) guides are the better starting point.

Modelplane's central design choice is to build the control plane on
[Crossplane](https://crossplane.io) rather than as a bespoke set of Kubernetes
controllers. Everything below follows from that.

## Resources are composite resources

Every Modelplane API, an `InferenceCluster`, a `ModelDeployment`, a
`ModelService`, is a Crossplane Composite Resource (XR), not a custom resource
served by a hand-written controller. Each XR has a composition function that acts
as its controller: it reads the XR's spec, reads other resources in the fleet
through Crossplane v2's required-resources mechanism, and returns the desired
child resources Modelplane should create.

The API feels like Kubernetes core one scope up. A `ModelDeployment`
composes a `ModelReplica` per replica; a `ModelReplica` composes the serving
workload on its target cluster; a `ModelService` composes the routing resources.
The composition functions are where Modelplane's logic lives, and they're the
unit a contributor works on. Each is a small gRPC service in `functions/` that
takes a request (the observed XR and its dependencies) and returns a response
(the desired children).

Building on Crossplane means Modelplane inherits reconciliation, dependency
tracking, and the providers a platform team already uses for cloud
infrastructure, rather than reimplementing them. It's the bet that
inference infrastructure is the same shape of problem as cloud infrastructure,
which Crossplane already manages well.

## Two clusters, two scopes

Modelplane runs on a **control cluster** and manages a fleet of **workload
clusters** (the `InferenceCluster`s). The split is deliberate: the control plane
holds no GPUs and serves no tokens. It schedules, composes, and routes; the
workload clusters do the serving.

Composing onto a remote workload cluster is a provider-kubernetes `ProviderConfig`
built from the cluster's kubeconfig. The control plane installs a serving stack on
every workload cluster it manages, provisioned or existing. The stack includes
LeaderWorkerSet for multi-node gangs, Envoy Gateway with the Gateway API Inference
Extension for inference-aware routing, the NVIDIA DRA driver for binding GPUs, and
supporting components. The contract is that Modelplane owns what runs on the
cluster.

## Two layers of routing

Inference requests pass through two gateways. The **control-plane gateway**
(Traefik) is the front door: one OpenAI-compatible address per `ModelService`,
routing each request to the right cluster's edge. Modelplane uses Traefik here
because it supports the `URLRewrite` filter on each `backendRef` that routing to a
replica's path depends on, which Envoy Gateway doesn't offer. A second gateway
(Envoy Gateway and the Inference Extension) runs on each cluster and routes from
the cluster edge to the model engines.

A `ModelEndpoint` composes a Kubernetes `Service` and an `EndpointSlice` on the
control plane, with an address type that follows the endpoint's URL (an IP for a
cluster gateway, an FQDN for an external provider). A `ModelService` builds one
`HTTPRoute` whose `backendRef`s point at those Services, each with a `URLRewrite`
filter derived from the endpoint's rewrite path.

## How the pieces connect

A multi-node engine composes to a LeaderWorkerSet. The leader runs the engine's
coordination head, and the workers join it through a `MODELPLANE_LEADER_ADDRESS`
env var Modelplane injects, aliased to the backend's own variable such as
`LWS_LEADER_ADDRESS`. Modelplane injects almost nothing else into engine
containers. The engine command and flags are yours.

GPUs bind through DRA on every workload cluster. Each `claim: DRA` device request
in a member's `nodeSelector` becomes a `DeviceRequest` in the `ResourceClaim` the
serving pods claim through, referencing the driver's `DeviceClass`. A
`claim: Synthetic` device is matched for scheduling but never claimed.

## In this section

- [Fleet Scheduling]({{< ref "scheduling.md" >}}): how the scheduler places
  replicas across the fleet, capacity accounting, pinning, and its deliberate
  limits.

More architecture topics, including a closer look at individual composition
functions, are coming as the section grows.
<!-- vale write-good.Passive = YES -->
