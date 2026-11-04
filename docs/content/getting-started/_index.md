---
title: Get started
weight: 10
navLanding: "Tour of Modelplane"
description: A guided tour of Modelplane, from an empty control plane to a model served across regions.
---

{{< hint "tip" >}}
No cloud account or GPU? [Try Modelplane locally]({{< ref
"getting-started/local-demo.md" >}}) on two kind clusters instead.
{{< /hint >}}

Modelplane is an open source control plane for AI inference. It separates two
concerns: a platform team managing GPU capacity, and ML teams deploying models
against it. Without it, every change on one side creates work for the other.
When the platform team updates infrastructure, ML teams have to react. When
model requirements change, the platform team gets a request.

With Modelplane, the platform team publishes its hardware without knowing what
models will run on it. The ML team declares what a model needs without knowing
what clusters exist. The control plane places the model on matching hardware
and moves it only when a change on either side breaks the match.

In this tour, you'll switch between provisioning infrastructure and declaring a
model to see how they interact. By the end you'll have a GPU fleet across three
regions and one OpenAI-compatible endpoint routing to a model served across two
of them.

This is not a production setup and takes around 45 minutes to run.

## What you'll build

The platform team provisions a first, small-GPU cluster and grows it to two
larger-GPU regions; the ML team serves a model on the L4, then scales it onto a
bigger GPU, all behind one endpoint.

{{< asciinema src="what-youll-build.cast" poster="npt:2:13" >}}

## Before you begin

You'll need an AWS or GCP account with permission to create clusters. Each step
covers what it needs as you reach it.

## The tour

0. [Try Modelplane locally]({{< ref "getting-started/local-demo.md" >}}): spin up local clusters and use synthetic GPUs to see the Modelplane workflow.
1. [Install the control plane]({{< ref "/install" >}}): install Modelplane on a Kubernetes cluster.
2. [Build the platform]({{< ref "getting-started/build-the-platform.md" >}}): provision your first GPU cluster.
3. [Deploying a model]({{< ref "getting-started/deploying-a-model.md" >}}): serve a model and send it a request.
4. [Scale the platform]({{< ref "getting-started/scale-the-platform.md" >}}): grow to a multi-region fleet.
5. [Scale the model]({{< ref "getting-started/scale-the-model.md" >}}): serve the model from two regions behind one endpoint.

First, [install the control plane]({{< ref "/install" >}}).
