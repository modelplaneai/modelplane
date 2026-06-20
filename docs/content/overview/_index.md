---
title: Overview
weight: 5
description: What Modelplane is, why it exists, and how it works.
navLanding: "Introduction"
---
<!-- vale write-good.Passive = NO -->
Modelplane is the open source control plane for AI inference. It sits above your
inference clusters across cloud, neocloud, and on-premise, and turns a fleet of
GPU clusters into a single place to serve models. Platform teams set policy and
capacity; developers declare a model and get a serving endpoint. Modelplane
continuously reconciles the whole fleet: provisioning, scheduling, autoscaling,
routing, and caching. It all runs under your control.

This Overview is the place to start. It answers three questions:

- [Why Modelplane]({{< ref "/overview/why" >}}): the problem it solves and how it
  compares to building inference yourself or buying it as a service.
- [How Modelplane works]({{< ref "/overview/how-it-works" >}}): the architecture, the
  two-team boundary, and what happens when you deploy a model.

When you're ready to deploy, [Getting started]({{< ref "/getting-started" >}}) takes
you from nothing to a live OpenAI-compatible endpoint in about 45 minutes.

## Two teams, one control plane

Modelplane draws a clear boundary between the people who run the infrastructure
and the people who ship models on it.

{{< personas >}}

**Platform teams** describe their GPU fleet as resources: which clusters exist,
what hardware each offers, and the policy and capacity the fleet runs within.
**Developers** declare a model and replica count, and get back one
OpenAI-compatible endpoint per service. Neither team has to know the details of
the other's job.

## What Modelplane is not

Modelplane is the fleet-level control plane *above* the inference engine. It
doesn't compete with vLLM or Dynamo; it manages them across clusters. It isn't a
managed inference service either. Modelplane is open source and runs in your own
clusters, so the models, the data, and the infrastructure stay yours.

It builds on [Crossplane](https://crossplane.io): platform teams describe their
GPU fleet as resources, developers describe a deployment, and Modelplane composes
the clusters, schedules replicas, and exposes one OpenAI-compatible endpoint per
service. The [Concepts]({{< ref "/overview/concepts" >}}) page covers every resource and
how they relate.
<!-- vale write-good.Passive = YES -->
