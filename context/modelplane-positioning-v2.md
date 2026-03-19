# Modelplane — Positioning & Messaging

**Version:** 2.1  
**Date:** March 2026  
**Status:** Internal working document

---

## The one-liner

**Modelplane — open source control plane for AI models.**

---

## Tagline options by audience

- **For developers:** "The simplest way to run open source models on infrastructure you own."
- **For platform engineers:** "Self-host any model, on any GPU cluster, managed like infrastructure."
- **For NVIDIA / partners:** "The inference control plane for your AI factory."
- **For analysts / investors:** "What Crossplane did for cloud infrastructure, Modelplane does for AI models."

The reason for multiple options: buyers don't search for "AI factory" or "inference control plane." They search for "self-hosted LLM deployment" or "run models on Kubernetes." Discovery language matches their problem. Positioning language does the differentiation work once you have their attention. See the two-level messaging section below.

---

## Positioning statement

Modelplane is the open source control plane for AI models. It brings models into your control plane as declarative resources — versioned, governed, composable — and operates them continuously across any GPU infrastructure, without humans in the operational loop.

When a platform team deploys Modelplane, they are building the inference layer of their AI factory. The control plane provisions GPU environments, places models, reconciles state, and enforces policy — continuously, automatically, at the pace AI demands.

When an ML team uses Modelplane, they deploy a model with two lines of YAML and get back a working OpenAI-compatible endpoint. The infrastructure complexity is invisible.

---

## What Modelplane is not

Getting this wrong collapses Modelplane into existing categories it doesn't belong in.

**Not a managed inference service.** Baseten, Fireworks, Together.ai are managed services running on their infrastructure. Modelplane is software you run on infrastructure you own. Baseten is for when you want someone else to run your inference. Modelplane is for when you need to run it yourself — and want it to run itself.

**Not an inference engine.** vLLM, SGLang, Dynamo, KServe are engines and serving frameworks. Modelplane runs on top of them. It is the operational and governance layer, not the serving layer.

**Not an MLOps platform.** Red Hat OpenShift AI is an MLOps platform covering the full model lifecycle from data to training to serving to monitoring. Modelplane is purpose-built for inference operations. Narrow by design, which is why it's simple.

**Not a Kubernetes operator.** KubeAI, Kaito, and raw KServe are operators — they manage pods. Modelplane is built on Crossplane, a control plane framework: declarative desired state, continuous reconciliation, composable resources, full governance model. Operators are components Modelplane manages.

**Not a job scheduler.** SkyPilot is a job orchestration tool — you launch tasks, they run and complete. Modelplane manages persistent infrastructure — inference environments and model deployments that exist continuously, reconcile automatically, and enforce policy.

---

## The category

**Open source AI inference control plane.**

This is a new category. Nobody owns it yet. The conditions that create it:

1. Enterprises are bringing AI workloads in-house — driven by cost, compliance, customization, and sovereignty requirements that SaaS inference platforms structurally cannot address.
2. AI factories require inference infrastructure that operates at machine speed — continuous reconciliation, not human-initiated operations.
3. Models need to be managed as first-class infrastructure resources, not called as API endpoints — with versioning, governance, policy enforcement, and composability.
4. No existing open source project provides all three simultaneously. The field is fragmented: engines solve performance, operators solve deployment, gateways solve routing. Nobody has assembled the control plane layer that governs all of them.

Modelplane's claim: the control plane is the right architectural answer to all four conditions simultaneously.

---

## Target personas

### Primary — Platform engineering teams

**Who they are:** The team that operates Kubernetes, manages infrastructure across clouds and on-prem, runs the internal developer platform, and enforces security and compliance. At companies like Apple, JPMC, Nike, Elastic — they already run Crossplane.

**Their problem:** ML teams are asking them to provide GPU infrastructure and inference capacity. They don't have a good answer. The existing options are: build it themselves (12-18 months), buy Red Hat OpenShift AI (requires OpenShift everywhere), tell ML teams to use Baseten (unacceptable for data sovereignty), or assemble vLLM, KServe, KEDA, and custom YAML by hand.

**What they want:** An inference platform that fits their existing operational model — declarative, Kubernetes-native, GitOps-compatible, policy-enforced — that they don't have to build themselves and that ML teams can self-serve against.

**Why Modelplane:** It's a Crossplane Configuration package. It installs in minutes on infrastructure they already operate. Models become resources managed exactly like their cloud infrastructure. No new tools, no new operational model, no new vendor to trust.

**What they're actually searching for:**
- "Self-hosted LLM deployment Kubernetes"
- "Run open source models on our own infrastructure"
- "Model serving on-premise enterprise"
- "vLLM production deployment platform"

They are not searching for "AI factory" or "inference control plane" yet. Lead with their search terms, arrive at the positioning once you have their attention.

### Secondary — AI-native engineering teams

**Who they are:** Engineering teams at AI-native companies that have hit the scale wall with managed inference. Spending too much on API tokens, latency is unpredictable, or building on fine-tuned models that can't run on shared infrastructure.

**Their problem:** Baseten and frontier APIs worked at prototype scale. Now the bill is too high, latency is unpredictable, or their custom model needs a home. They've outgrown the managed service but don't want to become GPU infrastructure experts.

**What they want:** The Baseten developer experience — simple deployment, reliable endpoint — but running on infrastructure they control and priced on infrastructure they own, not tokens they consume.

**Why Modelplane:** Two lines of YAML to deploy a model. OpenAI-compatible endpoint out of the box. Runs on Coreweave, AWS, GCP, or their own hardware. No per-token pricing.

---

## The forcing functions that drive adoption

Platform engineers and AI-native teams don't choose self-hosted inference because it's inherently better. They choose it when one or more forcing functions arrives. Modelplane's job is to be the obvious answer at that moment.

**Cost.** At scale, API token economics become the largest infrastructure line item. Companies spending $15-20M/year on inference can reduce costs 60-70% through hybrid architectures. Primary forcing function for AI-native companies.

**Compliance.** Financial services, healthcare, government, defense — prompts cannot leave internal infrastructure. Hard requirement, not a preference. Every large bank, hospital system, and government agency building AI self-hosts by necessity. This forcing function makes the enterprise market structurally guaranteed regardless of how good managed services become.

**Customization.** Fine-tuned and custom models don't run on managed platforms. When a company's competitive advantage is a domain-specific model, they need to run their own inference.

**Control.** Unpredictable latency, rate limits, model deprecations, and vendor concentration risk. Enterprises have learned from lock-in with VMware, Oracle, and AWS.

**Consistency.** Platform teams want one operational model — not separate systems for cloud infrastructure, application workloads, and AI inference. Modelplane makes inference infrastructure operate like everything else they manage. For Crossplane shops this forcing function is immediate.

---

## Why Modelplane wins

### The architectural argument — models as resources

Most inference platforms treat models as endpoints — something you call. Modelplane treats models as resources — something you declare, version, govern, and compose.

This unlocks the entire Kubernetes operational model:

**Desired state.** Declare what you want to exist. The control plane makes it so and keeps it that way. Model drift, failed placements, config changes — corrected automatically.

**Continuous reconciliation.** The control plane continuously compares desired state to actual state. This is not monitoring with auto-remediation bolted on. It's the foundational operational loop that makes infrastructure self-managing.

**GitOps.** Model deployments live in Git like any other infrastructure. Reviewed, approved, audited. Every change has a commit.

**RBAC and policy.** Who can deploy which models to which environments is enforced by the control plane, not by convention or documentation.

**Composability.** Models, environments, and policies compose into higher-order abstractions — exactly as Crossplane XRDs compose cloud resources into platform abstractions.

This is not an inference engine optimization play. It's an operational architecture play. How do you operate AI inference at the scale and governance level enterprises require? The same way you operate everything else — with a control plane.

### The infrastructure argument — runs where your factory runs

Modelplane runs on any infrastructure where your AI factory runs:

- **Major clouds:** AWS EKS, GCP GKE, Azure AKS, Oracle OKE
- **GPU clouds (neo cloud):** Coreweave, Lambda, Crusoe, Vultr
- **On-premise / AI factory:** NVIDIA DGX via BCM, air-gapped environments, sovereign deployments

Each environment gets full infrastructure-specific capabilities — GPU topology awareness, model caching, prefill/decode disaggregation where supported. The control plane handles environment-specific complexity; the user-facing API stays consistent across all of them. This is not a lowest-common-denominator abstraction — it's continuous reconciliation applied uniformly across fundamentally different infrastructure types.

### The open source argument — genuinely open

Apache 2, no usage limits, no cluster caps, no token restrictions. Run at any scale, forever, free. Same commitment as Crossplane. This is the foundation of enterprise trust.

Red Hat OpenShift AI requires OpenShift and is not portable. Baseten is closed-source SaaS. KubeAI and KServe are open but raw — no governance, no abstraction, no enterprise operational model. Modelplane is the only option that is genuinely open, genuinely enterprise-ready in its architecture, and genuinely portable across infrastructure.

### The Crossplane ecosystem argument — distribution advantage

Enterprises already running Crossplane — Apple, JPMC, Nike, Elastic, MongoDB, Grafana — get Modelplane as a native extension of their existing control plane. No new operational model. No new toolchain. The same Compositions, Functions, and XRDs they already understand, applied to AI inference.

This is a distribution advantage no new entrant can replicate. When a platform team at a Crossplane customer needs to provide inference infrastructure, Modelplane is the obvious first choice.

---

## Competitive positioning

### vs. SkyPilot

The most commonly cited open source comparison. The distinction is fundamental, not superficial.

SkyPilot's mental model is **imperative job execution** — write a task YAML describing resource requirements and commands, run `sky launch`, SkyPilot finds available GPUs and runs the job. Optimized for researcher productivity and cost arbitrage across clouds.

Modelplane's mental model is **declarative desired state** — declare what you want to exist, the control plane makes it so and keeps it that way continuously.

| | SkyPilot | Modelplane |
|---|---|---|
| Mental model | Imperative job execution | Declarative desired state |
| Primary user | ML researchers | Platform engineering teams |
| Operational mode | You launch and manage | Control plane reconciles |
| Multi-tenancy | None | Built-in via Kubernetes RBAC |
| Governance | None | First-class via Crossplane |
| Model catalog | No | ClusterModel resource |
| Persistence | Jobs run and terminate | Resources exist until deleted |
| Crossplane | None | Native — built on Crossplane |

These are not competing products at different maturity levels. Different problems, different buyers. SkyPilot is a potential integration target — a platform team might use SkyPilot for training and batch jobs on the same infrastructure Modelplane manages for inference.

### vs. KubeAI, Kaito, llm-d, KServe

These are Modelplane's backends and ecosystem partners, not competitors.

- **KServe** — v0.1 inference backend. CNCF Incubating.
- **KubeAI** — planned v0.2 backend. Adds scale-to-zero.
- **llm-d** — Red Hat/Google/IBM distributed inference framework. Backend candidate.
- **vLLM, SGLang, Dynamo** — Inference engines. Modelplane runs on top of them.

These projects solve the serving and performance layer. Modelplane is the control plane layer above them — governance, multi-tenancy, model catalog, lifecycle management, policy enforcement, unified API. Users running raw KServe or vLLM are the natural Modelplane adopters. Never compete with vLLM. Never compete with KServe. Run on top of them and make them enterprise-operable.

### vs. Red Hat OpenShift AI

Most serious enterprise competitor. The differentiation is structural.

Red Hat requires OpenShift everywhere — significant prerequisite, specific expertise, and a commitment to their distribution across your entire Kubernetes estate. Enterprises running heterogeneous environments cannot use OpenShift AI without OpenShift underneath all of it.

The experience is also fundamentally different. Red Hat is UI and dashboard-driven — notebooks, forms, wizard-based deployment. Modelplane is two lines of declarative YAML. Red Hat has NCP AI Cloud Ready status from NVIDIA — Modelplane should pursue the same certification via the existing NCP, DGX Cloud, and BCM relationships.

The line: "Red Hat is the AI factory platform for organizations committed to OpenShift everywhere. Modelplane is infrastructure-agnostic — it runs on any Kubernetes distribution, any cloud, any GPU cloud, any on-prem estate. That's the right answer for enterprises that are heterogeneous by design."

### vs. Baseten, Fireworks, Together.ai, Modal

Structurally different products for structurally different buyers.

Baseten cannot close the gap. Their business model is managing GPU infrastructure for customers. Their self-hosted option runs in customer VPCs on hyperscalers — not on Coreweave bare metal, not on on-premise DGX, not in air-gapped environments. Their product direction moves toward more lock-in, not less.

The AI factory framing belongs to Modelplane. Managed inference players are the AI factory as a service — they own the factory, you rent access. Modelplane helps you own and operate your own factory. SaaS inference players structurally cannot use AI factory language without telling their customers to stop using them.

### vs. DIY internal platforms

12-24 months to build something production-ready. High ongoing maintenance. Every organization solving the same problems independently.

"Modelplane is what you would build if you had 18 months and a dedicated team. It's available today, it's open source, and you can self-operate it forever."

---

## Two-level messaging — discovery vs. positioning

**Level 1 — Discovery messaging** (speaks buyer's language, problem-first)

For search, word-of-mouth, community channels — how Modelplane gets found:

- "The simplest way to run open source models on your own infrastructure"
- "Self-host any model, on any GPU cluster, in minutes"
- "Stop paying API bills. Run models where your data lives."
- "Kubernetes-native model serving without the operational complexity"
- "The on-premise Baseten that actually runs on your own hardware"

**Level 2 — Positioning messaging** (speaks the market's language, category-first)

For conversations, documentation intros, partner pitches, press — once the buyer is paying attention:

- "The open source control plane for AI models"
- "What Crossplane did for cloud infrastructure, Modelplane does for AI inference"
- "Models as first-class resources — not endpoints to call, but infrastructure to govern"
- "The inference control plane for your AI factory"

The transition: "You're dealing with this problem because every organization is effectively building an AI factory right now — and the inference layer is the hardest operational problem in it. Modelplane is the control plane that solves it."

Do not lead with Level 2 in developer-facing contexts. Do not use Level 1 with NVIDIA, analysts, or boards.

---

## Message hierarchy by audience

### For platform engineers

**Problem:** Your ML teams need GPU infrastructure and inference capacity. You don't have a good way to give it to them that fits your existing operational model without turning your team into a GPU ops team.

**Solution:** Modelplane extends your Crossplane control plane to manage AI inference — the same declarative, reconciliation-based model you already use for cloud infrastructure, applied to models.

**Proof:** Install as a standard Crossplane Configuration package. Platform team defines inference environments and approves models. ML teams deploy with two lines of YAML. The control plane handles placement, scaling, reconciliation, and policy enforcement.

### For AI-native engineering teams

**Problem:** You're spending too much on API tokens, your latency is unpredictable, or your custom model needs a home on infrastructure you control.

**Solution:** Modelplane gives you the Baseten developer experience on infrastructure you control. Any model, any GPU infrastructure, OpenAI-compatible endpoint, no per-token pricing.

**Proof:** Two lines of YAML to deploy a model. Working endpoint in minutes. Change one line of code to switch from OpenAI to your self-hosted model.

### For NVIDIA and infrastructure partners

**Problem:** Enterprises are buying AI factory hardware — DGX, Coreweave, on-prem GPU clusters — with no clear answer for how to operate the inference layer. There is no Baseten for infrastructure you own.

**Solution:** Modelplane is the open source inference control plane for AI factories — Dynamo as a backend, NIM as the model packaging standard, DGX/BCM as native infrastructure targets.

**Proof:** Built on Crossplane — trusted by Apple, JPMC, Nike. Existing technical relationships across NCP, DGX Cloud, and BCM teams. NVIDIA builds AI factories. Modelplane operates them.

### For investors and analysts

**Problem:** The AI factory era creates a new infrastructure category. Enterprises need to operate inference at machine speed, across heterogeneous infrastructure spanning cloud, neo cloud, and on-premise. The field is fragmented — no control plane layer exists.

**Solution:** Modelplane is the open source control plane for AI models. The same architectural pattern that made Crossplane the de facto infrastructure control plane standard, applied to AI model inference at the right moment.

**Proof:** Launching with AWS, GCP, Azure, Oracle, Coreweave, NVIDIA DGX. KServe and Dynamo backends. Built by the team behind Crossplane — CNCF Graduated, trusted by Apple, JPMC, Nike, and hundreds of enterprises worldwide.

---

## The Crossplane parallel

| | Crossplane | Modelplane |
|---|---|---|
| What it manages | Cloud infrastructure | AI model inference |
| Architecture | Kubernetes control plane | Kubernetes control plane |
| Key abstraction | Infrastructure as resources | Models as resources |
| Open source | CNCF Graduated | Apache 2 / CNCF trajectory |
| Commercial product | Upbound Platform | Upbound AI |
| Enterprise customers | Apple, JPMC, Nike | Same base + AI-native |

The Crossplane story: we built the open source control plane standard for cloud infrastructure. Eight years later, Apple, JPMC, and Nike independently converged on it under their own pressure to scale — without coordination, without us selling them on it. Modelplane applies the same architecture to AI model inference. The difference is the window is months, not years.

---

## The AI factory framing — when to use it

**Use with:** NVIDIA, neo cloud partners, analysts, investors, boards, enterprise CIOs. These audiences are using this language after Jensen's GTC keynote and it signals you're in the right conversation.

**Don't lead with for:** Platform engineers searching for solutions, ML engineers evaluating tools, developer communities. Speak their problem first.

**The structural advantage:** The AI factory framing is ownable by Upbound and Modelplane in a way SaaS inference players cannot use it. When Baseten says "build your AI factory," they're telling customers to stop using them.

**The transition phrase:** "You're dealing with this problem because every organization is effectively building an AI factory right now — and the inference layer is the hardest operational problem in it. Modelplane is the control plane that solves it."

---

## The Upbound AI relationship

Modelplane is the open source project. Upbound AI is the commercial open core product built on Modelplane, layered on Upbound Platform.

**The product family:**
- Crossplane — open source control plane for infrastructure (CNCF Graduated)
- Upbound Platform — commercial product built on Crossplane
- Modelplane — open source control plane for AI models (Apache 2, CNCF trajectory)
- Upbound AI — commercial product built on Modelplane, layered on Upbound Platform

Every Upbound AI customer is also an Upbound Platform customer. The stack compounds: Crossplane manages GPU clusters, Modelplane manages models on them, Upbound AI adds the gateway intelligence and enterprise management layer, Upbound Platform provides the control plane fabric underneath everything.

**The open source boundary:** Full inference control plane, all five resources, all backends, all infrastructure targets, community support — no usage limits of any kind. Genuine open source.

**What Upbound AI adds:**
- Global management plane across multiple Modelplane deployments
- Intelligent routing gateway — cost, quality, and latency optimization across self-hosted and frontier APIs
- Multi-cluster governance and cost attribution
- SOC 2 / ISO 27001 certifications
- Managed operations — Upbound runs the control plane for you
- SLA-backed uptime guarantees
- 24/7 enterprise support
- Upbound Spaces integration

**Pricing:** Scoped to GPU infrastructure under management — not token volume. The value is autonomous management of AI infrastructure at enterprise scale. Price proportional to the scope of what the platform is continuously managing.

**The line:** "Modelplane is free for any team running inference on their own infrastructure. Upbound AI is what you buy when you're operating inference at enterprise scale and need the governance, intelligence, and managed operations that scale requires."

---

## Partner map

### Launch partners

**NVIDIA** — anchor partner. Existing relationships across NCP, DGX Cloud, BCM. Ask: NIM integration certification, Dynamo backend, joint reference architecture, NCP AI Cloud Ready status. This relationship unlocks most others.

**Coreweave** — neo cloud anchor. Most enterprise-focused GPU cloud, most NVIDIA-aligned. Crossplane provider plus reference architecture. Co-marketing: Coreweave brings GPU capacity, Modelplane brings operational intelligence.

**KServe community** — v0.1 backend project. Contribute upstream, co-present at KubeCon, get listed as production adopter.

**Hugging Face** — model distribution. "Deploy on Modelplane" button on model pages is a significant distribution multiplier.

**Lambda Labs** — second neo cloud. Reference architecture, developer community reach.

**vLLM community** — primary inference engine, primary buyer audience. Visibility and co-documentation.

### Shortly after launch

**KubeAI** — v0.2 backend. Start contributor relationship now, ahead of the integration.

**CNCF** — Sandbox application. Natural trajectory from Crossplane precedent.

**Oracle Cloud** — Sovereign and enterprise. On launch infrastructure list, worth formalizing.

**Crusoe** — Growing neo cloud with enterprise traction.

**llm-d** — Backend integration discussion. Ecosystem alignment over competition.

### Medium term

**Dell** — On-premise enterprise hardware channel for DGX deployments.

**Datadog / New Relic** — Native observability integrations. Enterprise table stakes.

**Weights & Biases / MLflow** — ML team adoption, model tracking integration.

**Accenture / Deloitte / Thoughtworks** — SI channel for Fortune 500 Upbound AI deals.

---

## Launch narrative

Platform teams at companies like Apple, JPMC, and Nike already use Crossplane to manage their infrastructure declaratively. Those same teams are now being asked to provide AI inference infrastructure — on AWS, GCP, Azure, Coreweave, and on-premise DGX clusters. Modelplane gives them a native Crossplane extension to do so: any model, on any engine, on any infrastructure, with the same declarative control plane model they already trust.

This is what it looks like to bring models into your control plane.

---

## Key proof points

- Built on Crossplane — CNCF Graduated, 35+ enterprise production adopters
- Trusted by Apple, JPMC, Nike, Elastic, MongoDB, Grafana (via Crossplane)
- NVIDIA relationships: NCP program, DGX Cloud, BCM team
- KServe backend — CNCF Incubating, Bloomberg, Red Hat, IBM, NVIDIA adopters
- Launch infrastructure: AWS, GCP, Azure, Oracle, Coreweave, NVIDIA DGX
- Launch backends: KServe LLMInferenceService, NVIDIA Dynamo
- Launch models: Llama 3.1 8B, Llama 3.1 70B, Qwen 2.5, Mistral
- GitHub org established: github.com/modelplane-ai

---

## What not to say

**Don't say "inference platform"** — collapses into the Baseten category. Say "inference control plane."

**Don't lead with "AI factory" for developers** — they're not searching for it yet. Lead with their actual problem.

**Don't say "Kubernetes-native"** — table stakes, not differentiation.

**Don't lead with Crossplane for non-Crossplane audiences** — lead with the problem, arrive at Crossplane as why the solution is trustworthy.

**Don't compete with vLLM or KServe** — they are backends. Running on top of them is the story.

---

## What to measure

**Community health:** GitHub stars and fork rate, Configuration package installs, production deployments reported, contributors from organizations other than Upbound.

**Partnership signals:** NVIDIA AI Cloud Ready certification, neo cloud providers shipping Modelplane as reference architecture, joint reference architectures published.

**Commercial signals:** Modelplane deployments converting to Upbound AI, GPU count under Upbound AI management, time from install to Upbound AI trial.

**The north star:** When a platform engineer is asked "how do we run AI inference on our own infrastructure," Modelplane is the first answer they find and the first thing they try. That intent-to-install moment is the metric everything else flows from.
