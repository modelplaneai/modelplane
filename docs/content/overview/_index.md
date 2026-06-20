---
title: Overview
weight: 5
description: What Modelplane is, why it exists, and how it works.
navLanding: "Introduction"
---
<!-- vale write-good.Passive = NO -->
Modelplane is the open source control plane for AI inference. It's software you
install and run in your own environment, and it orchestrates the models, serving
stack, and infrastructure across cloud, neocloud, and on-premise. Modelplane
supports running any model and any engine on any infrastructure.

Modelplane operates across the whole fleet: provisioning inference clusters,
scheduling model deployments on compatible clusters, autoscaling model replicas
across clusters, caching model weights across clusters, and routing across
clusters.

It's an active system that is always reconciling the fleet toward the state you
declare. You install Modelplane on a Kubernetes cluster, which becomes the
control cluster for your inference fleet. It's built on
[Crossplane](https://crossplane.io) and fully integrates with your existing
platform systems.

{{< hint warning >}}
Modelplane is under active development. We have opted to build the project in the
open, collaborating with the broad AI inference community on integrations and
capabilities.
{{< /hint >}}

## Deploy a model

Modelplane's API is declarative, designed for platform teams responsible for the
inference infrastructure and developers deploying models on that infrastructure.

Once a platform team has provisioned inference clusters and declared the available
GPUs and networking fabric, an ML development team deploys a model with a
declarative manifest:

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

Modelplane schedules a model replica onto an inference cluster with free,
compatible GPUs and memory, and deploys the serving engine. Exposing an
OpenAI-compatible endpoint can be done by declaring a model service:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelService
metadata:
  name: qwen
  namespace: ml-team
spec:
  endpoints:
  - selector:
      matchLabels:
        modelplane.ai/deployment: qwen-demo
```
## A universal control plane for AI inference

Modelplane is designed to be a universal control plane for inference. It runs
inference clusters on any cloud, neocloud, or on-premise environment, or any
combination of them. Modelplane can provision the clusters for you, or you can
bring your own.

It supports any serving engine that runs as a container, and can serve
frontier-quality models using advanced topologies including tensor parallel,
pipeline parallel, data and expert parallel, and prefill/decode disaggregation.
Modelplane works across different accelerators and networking fabrics, and
schedules each model's replicas by matching the model's hardware requirements to
the hardware available across your clusters.

## What Modelplane is not

Modelplane is not a serving engine like vLLM, SGLang, or TensorRT-LLM. Modelplane
composes serving engines and orchestrates them fleet-wide across cloud, neocloud,
and on-premise. Modelplane is not a managed inference service like Baseten,
Together, or Fireworks. These offer cloud services, while Modelplane is
self-hosted software.

## Next steps

{{< cardgroup cols="2" >}}
{{< card title="Get started" href="/getting-started/" cta="Deploy on a real fleet" >}}
Go from nothing to a live OpenAI-compatible endpoint in about 45 minutes.
{{< /card >}}
{{< card title="Why Modelplane" href="/overview/why/" cta="Learn more" >}}
Learn more about Modelplane's capabilities and how it works.
{{< /card >}}
{{< /cardgroup >}}

<!-- vale write-good.Passive = YES -->
