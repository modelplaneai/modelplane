---
title: Architecture
weight: 40
navLabel: "Architecture"
navLanding: "How it's built"
description: How Modelplane is built, the Crossplane foundation, the composition-function model, and the choices behind them.
---
<!-- vale write-good.Passive = NO -->
Modelplane's central design choice is to build the control plane on
[Crossplane](https://crossplane.io) rather than as a bespoke set of Kubernetes
controllers. Everything else here follows from that. This section assumes you're
comfortable with Kubernetes; the rest of the Crossplane vocabulary you need is
below.

## Crossplane in brief

[Crossplane](https://crossplane.io) extends Kubernetes to manage things beyond
the cluster, cloud infrastructure, SaaS, and in Modelplane's case inference
fleets, through the same declarative, reconciled API model. Three of its concepts
matter here:

- **Composite Resources (XRs)** are custom resources whose controller, instead of
  talking to an external API directly, declares a set of other resources that
  should exist. Every Modelplane API, `InferenceCluster`, `ModelDeployment`,
  `ModelService`, is an XR.
- **Composition functions** are that controller logic. A function is a small gRPC
  service handed the observed XR and the resources it depends on, which returns
  the desired child resources. Crossplane runs the function every reconcile and
  reconciles whatever it returns.
- **Providers** are controllers that manage external systems through their own
  managed resources: `provider-gcp` and `provider-aws` for cloud APIs,
  `provider-helm` for Helm releases, `provider-kubernetes` for arbitrary objects
  on any cluster. A composition function composes these like any other resource.

Put together: a Modelplane API is an XR, its logic is a composition function, and
the function composes a mix of plain Kubernetes objects, other Modelplane XRs, and
provider resources. The API feels like Kubernetes core one scope up: a
`ModelDeployment` composes a `ModelReplica` per replica, a `ModelReplica`
composes the serving workload on its target cluster, and a `ModelService`
composes the routing resources.

## Why Crossplane?

Modelplane is, at its core, a system that turns declarative resources into
composed infrastructure spanning cloud accounts, many Kubernetes clusters, and
the workloads on them. That's the problem Crossplane solves, and it helps in two
ways: providers and functions.

**Providers** give us reach. Modelplane has to provision Kubernetes clusters and
all the infrastructure they need across different clouds, then install software
onto them. That's an enormous surface, and providers cover it without us rolling
our own controllers for each cloud API and Helm release. A platform team running
Crossplane already operates these providers, so Modelplane composes onto the same
stack rather than introducing a parallel one.

**Functions** are where Modelplane's own logic lives, and writing it as
composition functions buys several things:

- **Business logic, not controller plumbing.** A function computes desired state
  from observed state. Crossplane handles the watches, requeues, finalizers, and
  drift correction that a hand-written controller gets wrong in a dozen subtle
  ways.
- **Testability.** A function is a pure function of its inputs: feed it an XR and
  its dependencies, assert on the resources it returns. The whole test runs in
  process, with no API server to stand up. Modelplane's scheduler is tested this
  way, exhaustively, in isolation.
- **The right language for each job.** Functions can be written in any language.
  Modelplane's are Python, for fast iteration on the serving and scheduling logic
  and because Python is the common language of the ML world, which lowers the bar
  for contributors. The performance-sensitive distributed-systems core stays in
  Go, where Crossplane and its providers already are.

The bet underneath both is that inference infrastructure is the same shape of
problem as cloud infrastructure, which Crossplane manages well. Building on it
lets Modelplane spend its effort on the part that's actually inference-specific.

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
