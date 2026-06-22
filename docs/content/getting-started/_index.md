---
title: Get started
weight: 10
navLanding: "Start here"
description: A guided tour of Modelplane, from an empty control plane to a model served across regions.
---

Modelplane is an open source control plane for AI inference. It separates two
concerns: a platform team managing GPU capacity, and ML teams deploying models
against it. Without it, every change on one side creates work for the other.
When the platform team updates infrastructure, ML teams have to react. When
model requirements change, the platform team gets a request.

With Modelplane, the platform team publishes hardware without knowing what
models will run on it. The ML team declares what a model needs without knowing
what clusters exist. The scheduler resolves it and keeps it current as
both sides change.

In this tour, you'll switch between provisioning infrastructure and declaring a
model to see how they interact. By the end you'll have a three cluster fleet
across two regions and one OpenAI-compatible endpoint routing to a model served
across both.

This is not a production setup and takes around 45 minutes to run.

## Before you begin

You'll need [kind](https://kind.sigs.k8s.io/),
[kubectl](https://kubernetes.io/docs/tasks/tools/), and
[Helm](https://helm.sh/docs/intro/install/) installed, plus an AWS or GCP account
with permission to create clusters. Each step covers what it needs as you reach
it.

## The tour

1. [Installation]({{< ref "getting-started/installation.md" >}}): stand up the Modelplane control plane.
2. [Build the platform]({{< ref "getting-started/build-the-platform.md" >}}): provision your first GPU cluster.
3. [Deploying a model]({{< ref "getting-started/deploying-a-model.md" >}}): serve a model and send it a request.
4. [Scale the platform]({{< ref "getting-started/scale-the-platform.md" >}}): grow to a multi-region fleet.
5. [Scale the model]({{< ref "getting-started/scale-the-model.md" >}}): serve the model from two regions behind one endpoint.

First, follow the [Installation]({{< ref "getting-started/installation.md"
>}}) guide.
