---
title: Overview
weight: 5
description: What Modelplane is, why it exists, and how it works.
navLanding: "Introduction"
---
<!-- vale write-good.Passive = NO -->
Modelplane is the open source control plane for AI inference. It's software you
install and run in your own environment, and it orchestrates both layers that
serving a model takes: the infrastructure that runs inference across cloud,
neocloud, and on-premise, and the model deployments on top of it. Modelplane
manages scheduling of model replicas, autoscaling, caching of weights, routing,
and the lifecycle of the serving stack. It's an active control plane, always
reconciling the fleet toward the state you declare.

You install Modelplane on a Kubernetes cluster, which becomes the control cluster
for your inference fleet. It's built on [Crossplane](https://crossplane.io) and
requires it: platform teams and developers describe what they want as Kubernetes
resources, and Modelplane composes the infrastructure and deployments to match.

## Deploy a model

Once a platform team has registered a cluster, an ML team deploys a model with one
manifest:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: qwen-demo
  namespace: ml-team
spec:
  replicas: 1
  engines:
  - name: qwen
    members:
    - role: Standalone
      nodeSelector:
        devices:
        - name: gpu
          count: 1
          selectors:
          - cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("20Gi")) >= 0
      template:
        spec:
          containers:
          - name: engine
            image: vllm/vllm-openai:v0.7.3
            args: ["--model=Qwen/Qwen2.5-0.5B-Instruct"]
```

Modelplane schedules the replica onto a cluster with a free GPU and exposes one
OpenAI-compatible endpoint for it:
`http://<gateway>/ml-team/qwen-demo/v1/chat/completions`.

{{< cardgroup >}}
{{< card title="Get started" href="/getting-started/" cta="Deploy on a real fleet" >}}
Go from nothing to a live OpenAI-compatible endpoint in about 45 minutes.
{{< /card >}}
{{< /cardgroup >}}

## Built for the fleet

Modelplane's capabilities are all fleet-centric. A serving engine runs a model,
and a cluster orchestrator coordinates models within a single cluster. Modelplane
operates across the whole fleet:

- **Scheduling** model replicas across clusters, clouds, and regions.
- **Autoscaling** replicas across the fleet.
- **Routing** traffic to replicas wherever they run.
- **Caching** model weights so pods don't re-download them.
- **Provisioning** the clusters themselves.

## A universal orchestrator for inference

Modelplane is designed to be a universal orchestrator for inference. It runs
inference clusters on any cloud, neocloud, or on-premise environment, or any
combination of them. Modelplane can provision the clusters for you, or you can
bring your own.

It supports any serving engine that runs as a container, and frontier serving
topologies including tensor parallel, pipeline parallel, data and expert
parallel, and prefill/decode disaggregation. Modelplane works across different
accelerators and networking fabrics, and schedules each model's replicas by
matching the model's hardware requirements to the hardware available across your
clusters.

## One endpoint for the whole fleet

Modelplane sets up fleet-wide routing and exposes a single OpenAI-compatible
endpoint that routes to model replicas across the fleet. It can also route to
managed inference services running outside Modelplane's control. This enables
multi-cluster, multi-region serving, with fallback to managed inference
providers, all behind one endpoint.

## A rich API for two teams

Modelplane draws a clear boundary between the people who run the infrastructure
and the people who ship models on it, and gives each a role-shaped API.

{{< cardgroup cols="2" >}}
{{< card title="I run the platform" href="/platform/" accent="platform" cta="Platform docs" >}}
Provision clusters across clouds, define hardware classes, and set the capacity and policy the fleet runs within.
{{< /card >}}
{{< card title="I deploy models" href="/models/" accent="developer" cta="Model docs" >}}
Declare a model, expose it as one OpenAI-compatible service, and route traffic across replicas and endpoints.
{{< /card >}}
{{< /cardgroup >}}

**Platform teams** manage the inference infrastructure: hardware definitions,
capacity, cost, governance, and policy. **Developers** and ML engineers deploy
models onto that infrastructure and get back one OpenAI-compatible endpoint per
service. Neither team has to know the details of the other's job.

## What Modelplane is not

Modelplane is the fleet-level control plane *above* the serving engine. It
doesn't compete with vLLM or Dynamo; it composes them across clusters. It isn't a
managed inference service either. Modelplane is open source and runs in your own
clusters, so the models, the data, and the infrastructure stay yours.

## Start here

This Overview is the place to start. Read it top to bottom and you'll understand
what Modelplane is and how it works, with the rest of the docs a click away.

{{< cardgroup cols="2" >}}
{{< card title="Why Modelplane" href="/overview/why/" >}}
The problem it solves, and how it compares to building inference yourself or buying it as a service.
{{< /card >}}
{{< card title="How Modelplane works" href="/overview/how-it-works/" >}}
The architecture, the resources, and what happens when you deploy a model.
{{< /card >}}
{{< card title="FAQ" href="/overview/faq/" >}}
Short answers to the questions practitioners ask first.
{{< /card >}}
{{< /cardgroup >}}

When you're ready to deploy, [Getting started]({{< ref "/getting-started" >}}) takes
you from nothing to a live OpenAI-compatible endpoint in about 45 minutes.

Modelplane is at v0.1: early and moving fast. The picture above is the design the
control plane is built around. For exactly what ships today versus what's on the
roadmap, see [Why Modelplane]({{< ref "/overview/why" >}}) and the
[platform docs]({{< ref "/platform" >}}).
<!-- vale write-good.Passive = YES -->
