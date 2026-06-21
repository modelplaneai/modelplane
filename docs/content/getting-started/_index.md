---
title: Get started
weight: 10
navLanding: "Start here"
description: A guided tour of Modelplane, from an empty control plane to a model served across regions.
---
Modelplane is an open source control plane for AI inference. It separates two
concerns: building a GPU cluster fleet with published hardware capabilities, and
deploying models against those capabilities.

This is a guided tour, not a production setup. You'll stand up a throwaway
environment, watch both sides of Modelplane work end to end, and tear it all down
at the finish.

## Before you begin

You'll need [kind](https://kind.sigs.k8s.io/),
[kubectl](https://kubernetes.io/docs/tasks/tools/), and
[Helm](https://helm.sh/docs/intro/install/) installed, plus an AWS or GCP account
with permission to create clusters. Each step covers what it needs as you reach
it.

## The tour

The tour walks both sides of Modelplane, one short step at a time:

1. [Installation]({{< ref "getting-started/installation.md" >}}): stand up the Modelplane control plane.
2. [Build the platform]({{< ref "getting-started/build-the-platform.md" >}}): provision your first GPU cluster.
3. [Deploying a model]({{< ref "getting-started/deploying-a-model.md" >}}): serve a model and send it a request.
4. [Scale the platform]({{< ref "getting-started/scale-the-platform.md" >}}): grow to a multi-region fleet.
5. [Scale the model]({{< ref "getting-started/scale-the-model.md" >}}): serve the model from two regions behind one endpoint.

Start with [Installation]({{< ref "getting-started/installation.md" >}}). When
you're finished, [Clean up]({{< ref "getting-started/clean-up.md" >}}) removes
everything you created.
