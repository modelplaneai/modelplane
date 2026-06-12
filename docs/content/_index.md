---
title: Take off with Modelplane
weight: -1
description: Introduction to Modelplane
---

**The open source control plane for AI models.**

Modelplane extends [Crossplane] to manage AI model inference across a fleet of
GPU clusters. Platform teams provision clusters and define hardware classes. ML
teams deploy models and get back a unified, OpenAI-compatible endpoint.
Modelplane handles fleet scheduling, multi-cluster routing, and infrastructure
composition.

## Deploy a model

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: qwen-demo
  namespace: ml-team
spec:
  replicas: 2
  engines:
  - name: qwen
    members:
    - role: Standalone
      nodeSelector:
        devices:
        - name: gpu
          count: 1
          selectors:
          - cel: |
              device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("20Gi")) >= 0
      template:
        spec:
          containers:
          - name: engine
            image: vllm/vllm-openai:v0.7.3
            args:
            - "--model=Qwen/Qwen2.5-0.5B-Instruct"
```

This deploys two replicas of Qwen 2.5 0.5B and produces a unified,
OpenAI-compatible endpoint. The scheduler picks which clusters the replicas run
on based on GPU capacity and each member's device requests.

## How it works

Modelplane draws a clear boundary between two teams.

**Platform teams** create `InferenceClusters` describing their GPU fleet and
`InferenceClasses` defining hardware recipes (GPU type, count). They set
organizational metadata via labels on clusters: tier, region, provider.

**ML teams** create a `ModelDeployment` carrying everything needed to serve a
model: the inference engines (their templates and device requests here) and
replica count. Modelplane schedules each replica to a ready cluster with
matching capacity, composes a `ModelReplica` per cluster, and creates
`ModelEndpoints` for routing. A
`ModelService` routes traffic across endpoints through a unified [Envoy Gateway]
endpoint on the control plane.

Modelplane is the fleet-level control plane above the inference engine. It
doesn't compete with vLLM or Dynamo. It manages them across clusters.

## Current status

Modelplane is at v0.1. It's early and evolving fast.

| | What works today |
|---|---|
| Cluster sources | GKE (provisioned), Existing (bring your own kubeconfig) |
| Serving engines | vLLM |
| Scaling | Scale ModelDeployment using `spec.replicas` |
| Routing | Unified OpenAI-compatible endpoint via ModelService |

See [issues labeled `enhancement`][enhancements] for what's planned.

## Getting started

Follow the [getting started guide](docs/getting-started.md) to deploy Modelplane
on a local kind cluster and serve a model on GKE. The [concepts
page](docs/concepts.md) explains the key resources and how they relate.

The [`examples/`](examples/) directory has annotated manifests covering the full
workflow: gateway setup, cluster provisioning, and model deployments.

## Development

Modelplane uses [Nix] for builds and the development environment. You don't need
Nix installed locally. See [CONTRIBUTING.md] for how to get set up, run checks,
and submit changes.

## Get involved

Contributions, bug reports, and feature requests are welcome.

- **Issues:** [GitHub Issues][issues]
- **Discussions:** [GitHub Discussions][discussions]
- **Slack:** [#modelplane][slack] in the Crossplane workspace

## License

Modelplane is under the [Apache 2.0 license](LICENSE).

<!-- Named links -->
[Crossplane]: https://crossplane.io
[Configuration]: https://docs.crossplane.io/latest/concepts/packages/#configuration-packages
[Envoy Gateway]: https://gateway.envoyproxy.io
[KServe]: https://kserve.github.io/website/
[CONTRIBUTING.md]: CONTRIBUTING.md
[Nix]: https://nixos.org
[issues]: https://github.com/modelplaneai/modelplane/issues
[enhancements]: https://github.com/modelplaneai/modelplane/issues?q=is%3Aissue+is%3Aopen+label%3Aenhancement
[discussions]: https://github.com/modelplaneai/modelplane/discussions
[slack]: https://crossplane.slack.com
