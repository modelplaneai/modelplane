[![CI](https://github.com/modelplaneai/modelplane/actions/workflows/ci.yml/badge.svg)](https://github.com/modelplaneai/modelplane/actions/workflows/ci.yml)
[![GitHub release](https://img.shields.io/github/release/modelplaneai/modelplane/all.svg)](https://github.com/modelplaneai/modelplane/releases)
[![Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

<img src="docs/images/modelplane-horizontal-color.png" alt="Modelplane" width="300" />

**The open source control plane for AI models.**

Modelplane extends [Crossplane] to manage AI model inference as declarative
infrastructure. Platform teams provision GPU clusters, install inference
backends, and curate a model catalog. ML teams deploy from that catalog and get
back a working endpoint. The control plane handles placement, scaling, and
reconciliation continuously.

## Deploy a model

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: qwen
  namespace: ml-team
spec:
  modelRef:
    name: qwen-2-5-0-5b
  environments: 2
```

This deploys Qwen 2.5 0.5B to two inference environments and produces a unified,
OpenAI-compatible endpoint. The platform decides where to place the model based
on GPU capacity and backend compatibility

## How it works

Modelplane draws a clear boundary between two teams.

**Platform teams** create `InferenceEnvironments`, which are Kubernetes clusters
with an inference backend installed. They also register approved models as
`Models` in a catalog. Each model specifies its source, VRAM
requirements, and one or more serving profiles that configure engines like vLLM.

**ML teams** create a `ModelDeployment` referencing a catalog model and specify
how many environments to deploy across. Modelplane matches serving profiles to
available environments, checks GPU capacity, and creates a `ModelPlacement` per
environment. Traffic routes through a unified [Envoy Gateway] endpoint on the
control plane.

Modelplane is the control plane layer above the inference engine. It doesn't
compete with vLLM, SGLang, or KServe. It manages them.

## Current status

Modelplane is at v0.1. It's early and evolving fast.

| | What works today |
|---|---|
| Cluster sources | GKE (provisioned), Existing (bring your own kubeconfig) |
| Inference backends | [KServe] LLMInferenceService, [NVIDIA Dynamo] |
| Serving engines | vLLM, SGLang |
| Scaling | Fixed replicas, concurrency-based autoscaling via KEDA |
| Routing | Unified OpenAI-compatible endpoint via Envoy Gateway |

See [issues labeled `enhancement`][enhancements] for what's planned.

## Getting started

Follow the [getting started guide](docs/getting-started.md) to deploy Modelplane
on a local kind cluster and serve a model on GKE. The
[concepts page](docs/concepts.md) explains the key resources and how they relate.

The [`examples/`](examples/) directory has annotated manifests covering the full
workflow: gateway setup, environment provisioning, model catalog entries, and
deployments.

Modelplane also has a web UI for browsing the catalog, deploying models,
monitoring placements, and chatting with deployed models. See
[`ui/deploy/`](ui/deploy/) for installation manifests.

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
[NVIDIA Dynamo]: https://github.com/ai-dynamo/dynamo
[CONTRIBUTING.md]: CONTRIBUTING.md
[Nix]: https://nixos.org
[issues]: https://github.com/modelplaneai/modelplane/issues
[enhancements]: https://github.com/modelplaneai/modelplane/issues?q=is%3Aissue+is%3Aopen+label%3Aenhancement
[discussions]: https://github.com/modelplaneai/modelplane/discussions
[slack]: https://crossplane.slack.com
