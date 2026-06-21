---
title: FAQ
weight: 35
description: Short answers to the questions practitioners ask about Modelplane first.
---
<!-- vale write-good.Passive = NO -->
Short answers to the questions that come up first, with links to the full
treatment. If you're new here, read the [Introduction]({{< ref "/overview" >}})
and [How Modelplane works]({{< ref "/overview/how-it-works" >}}) first.

## What Modelplane is

{{< qa "Is Modelplane a serving engine like vLLM?" >}}
No, Modelplane is the control plane *above* the engine. It composes serving
engines like vLLM, SGLang, and NVIDIA TensorRT-LLM, and operates them across a
fleet of clusters; it doesn't serve tokens itself. You bring the engine; Modelplane schedules
it, routes to it, scales it, and caches its weights across your inference fleet.
{{< /qa >}}

{{< qa "Does Modelplane replace vLLM or SGLang?" >}}
No, they run the model; Modelplane runs the fleet. A `ModelDeployment` carries
your engine container and its flags, and Modelplane composes it onto the right
cluster. Switching or upgrading engines is a change to your deployment, not to
Modelplane.
{{< /qa >}}

{{< qa "How is Modelplane different from KServe or NVIDIA Dynamo?" >}}
Scope. KServe and Dynamo are cluster orchestrators: they schedule, scale, route,
and cache within a single Kubernetes cluster. Modelplane runs its operations across a
fleet of clusters, clouds, and regions. Modelplane uses llm-d for multi-node serving, 
and KV-cache management, as do KServe and Dynamo. Modelplane is planning deeper integrations
with NVIDIA Dynamo in future releases.
{{< /qa >}}

{{< qa "How is Modelplane different from a managed provider like Baseten or Fireworks?" >}}
Managed providers run fleet-scale serving inside their own closed platform.
Modelplane is the open equivalent that runs in infrastructure you own. The
difference is open, in your own infrastructure, community-driven, and neutral
across the stack, not scope. You can still route to a managed provider from Modelplane.
{{< /qa >}}

## What it supports

{{< qa "What models does Modelplane support?" >}}
Modelplane supports any model, including open weights, custom models, and just about
anything that can be downloaded from Hugging Face, NVIDIA NGC, and other registries.
{{< /qa >}}

{{< qa "Which engines and accelerators are supported?" >}}
The API is engine-agnostic: any engine that runs as a container works, and its
flags are yours to write. Multiple accelerators are supported as long as they
can be bound through DRA, and the device model (DRA plus CEL selectors) is built to
extend to other accelerators and fabrics.
{{< /qa >}}

{{< qa "Which clouds or neoclouds does Modelplane support?" >}}
Today Modelplane provisions clusters on a few hyperscalers and neoclouds, and supports
bringing your own Kubernetes cluster anywhere. More provisioners are on the roadmap; the
bring-your-own path means you can run on any Kubernetes now.
{{< /qa >}}

{{< qa "Can I bring my own cluster, or run on a neocloud or on-premise?" >}}
Yes, an `InferenceCluster` with `source: Existing` registers a cluster you already
run, through its kubeconfig. Modelplane installs the serving stack it needs but
doesn't provision the infrastructure. This is how you run on neoclouds and
on-premise today.
{{< /qa >}}

## What it requires

{{< qa "Where does Modelplane run?" >}}
Modelplane runs as a control plane on a control cluster: an ordinary Kubernetes
cluster with Crossplane installed, with no GPUs of its own. The inference clusters
it manages do the serving, and each needs Dynamic Resource Allocation (DRA,
Kubernetes v1.35+) to bind GPUs to pods. Modelplane assumes exclusive ownership of
every inference cluster, so dedicate each one to Modelplane rather than sharing it
with other workloads.
{{< /qa >}}

{{< qa "Do I need Crossplane?" >}}
Yes, Modelplane is built on [Crossplane](https://crossplane.io) and requires it. If your 
platform team already runs Crossplane to manage cloud infrastructure, Modelplane is the 
same pattern applied to inference. Modelplane is built using Crossplane's composition 
function framework, and shares its infrastructure providers.
{{< /qa >}}


## What it can do

{{< qa "How does Modelplane decide where a model runs?" >}}
Two-level matching. First it filters clusters by their labels (tier, region,
provider) against your `clusterSelector`. Then it filters node pools by matching
your device requests, real DRA requests with CEL selectors over GPU memory,
architecture, and other attributes, against each pool's `InferenceClass`. It places each
replica on a cluster and pool that fits and has free capacity.
{{< /qa >}}

{{< qa "Can I serve across regions and clusters behind one endpoint?" >}}
Yes, that's the point. A `ModelService` exposes one OpenAI-compatible endpoint and
load-balances across every replica of a deployment, wherever they run, with
weights for canary and A/B rollouts.
{{< /qa >}}

{{< qa "Can I fall back to a managed provider?" >}}
Yes, a `ModelService` can send a slice of traffic to a manually created
`ModelEndpoint` that points at an external SaaS endpoint (for example Together or
Baseten), alongside your self-hosted replicas. Use it for overflow or break-glass
routing.
{{< /qa >}}

{{< qa "How do large or multi-node models work?" >}}
An engine can be a gang: a leader and one or more workers that Modelplane composes
into a LeaderWorkerSet across nodes. You write the coordination (for example Ray,
or vLLM's data-parallel coordinator) in the engine flags, and Modelplane injects
the leader's address so the workers can join it. Multi-node deployments stage
weights through a `ModelCache`.
{{< /qa >}}

{{< qa "What about disaggregated prefill/decode?" >}}
Set `serving.mode: PrefillDecode` and define separate prefill and decode engines.
They land on the same cluster, hand off the KV cache over a fast fabric, and
Modelplane configures the cluster-edge routing that pairs each request. The
KV-transfer flags live in your engine config.
{{< /qa >}}

{{< qa "How does scaling work?" >}}
Replicas are the only scaling axis. Each replica is a complete serving instance;
scaling `spec.replicas` adds or removes whole instances across the fleet. Because
a `ModelDeployment` exposes the Kubernetes scale subresource, `kubectl scale` and
KEDA work without anything extra. There's no per-pod autoscaling inside a cluster.
{{< /qa >}}

{{< qa "How are model weights handled?" >}}
A `ModelCache` stages weights once per cluster on shared (ReadWriteMany) storage,
and every pod reads them locally. Pods don't re-download on each start, and
concurrent starts don't race. It hydrates from Hugging Face today, is optional for
single-node deployments, and is required for multi-node ones.
{{< /qa >}}

## The project

{{< qa "Why did you pick Modelplane as a name for the project?" >}}
It's a fusion of AI Model and Control Plane. We also like that it implies that AI models
are their own layer (or plane) in the overall stack.
{{< /qa >}}

{{< qa "What does the logo signify?" >}}
Three popsicle sticks assembled to make a model plane. Balsa wood planes were the inspiration.
{{< /qa >}}

{{< qa "Is Modelplane production-ready?" >}}
Modelplane is in early development and moving fast. Treat it as early software. The
[platform docs]({{< ref "/platform" >}}) are specific about what ships today
versus what's planned. We are building it in the open.
{{< /qa >}}

{{< qa "What's the license and governance?" >}}
Modelplane is [Apache 2.0](https://github.com/modelplaneai/modelplane/blob/main/LICENSE),
with no usage caps or token metering, and is developed in the open. It's neutral
across models, engines, accelerators, and clouds, and is intended for donation to
a neutral open source foundation. It's a project from Upbound, the team behind Rook
and Crossplane, both CNCF Graduated and widely adopted projects.
{{< /qa >}}

{{< qa "How do I get involved?" >}}
Issues, discussions, and contributions are welcome on
[GitHub](https://github.com/modelplaneai/modelplane). See `CONTRIBUTING.md` for
development setup and the project's conventions.
{{< /qa >}}

## Next steps

{{< cardgroup cols="2" >}}
{{< card title="Get started" href="/getting-started/" >}}
Deploy Modelplane and serve your first model.
{{< /card >}}
{{< card title="How Modelplane works" href="/overview/how-it-works/" >}}
The architecture and the control loop, in one page.
{{< /card >}}
{{< /cardgroup >}}
<!-- vale write-good.Passive = YES -->
