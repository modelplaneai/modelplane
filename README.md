![CI](https://github.com/modelplaneai/modelplane/workflows/CI/badge.svg) [![GitHub release](https://img.shields.io/github/release/modelplaneai/modelplane/all.svg)](https://github.com/modelplaneai/modelplane/releases) [![Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

<img src="docs/images/modelplane-horizontal-color.png" alt="Modelplane" width="300" />

**The open source control plane for AI models.**

Modelplane extends [Crossplane] to manage AI model inference as declarative infrastructure. Platform teams define inference environments and approve models. ML teams deploy with two lines of YAML and get back a working endpoint. The control plane handles placement, scaling, reconciliation, and policy enforcement — continuously, without humans in the operational loop.

Modelplane is built on [Crossplane] — [CNCF Graduated] — using the same declarative, reconciliation-based architecture that platform teams at Apple, JPMC, and Nike already trust for cloud infrastructure.

## How it works

Platform teams use Modelplane to provision GPU clusters, install inference stacks, and register approved models in a catalog — specifying the image, engine, and policy for each. ML teams then deploy from that catalog by declaring what they want; the control plane handles placement, scaling, and reconciliation continuously. The result is an OpenAI-compatible endpoint, with no coordination required between the two teams.


## Infrastructure

Modelplane runs on any infrastructure where your models need to run:

| Type | Supported |
|---|---|
| Major clouds | AWS EKS, GCP GKE, Azure AKS, Oracle OKE |
| GPU clouds | CoreWeave, Lambda, Crusoe |
| On-premise | NVIDIA DGX via Base Command Manager, air-gapped |

## Inference backends

| Backend | Status |
|---|---|
| KServe `LLMInferenceService` | v0.1 — current |
| NVIDIA Dynamo | Planned |
| KubeAI | Planned |

Modelplane is the control plane layer above the inference engine. It does not compete with vLLM, SGLang, or KServe — it manages them.

## Releases

| Release | Status |
|:---:|:---:|
| v0.1 | Current — GKECluster, KServeBackend, KServe backend |
| v0.2 | Planned — KubeAI backend, scale-to-zero |

## Get involved

Modelplane is a community project. Contributions, bug reports, and feature requests are welcome.

- **Issues:** [Open an issue][issues] for bugs, questions, or feature requests
- **Discussions:** [GitHub Discussions][discussions] for design and community conversation
- **Slack:** [#modelplane][slack] in the Crossplane Slack workspace

To contribute, see [CONTRIBUTING.md].

## License

Modelplane is under the [Apache 2.0 license](LICENSE).

Apache 2, no cluster limits, no token caps, no usage restrictions of any kind. Run it at any scale, forever, free.

---

<!-- Named links -->
[Crossplane]: https://crossplane.io
[CNCF Graduated]: https://www.cncf.io/projects/crossplane/
[CONTRIBUTING.md]: CONTRIBUTING.md
[issues]: https://github.com/modelplaneai/modelplane/issues
[discussions]: https://github.com/modelplaneai/modelplane/discussions
[slack]: https://crossplane.slack.com
