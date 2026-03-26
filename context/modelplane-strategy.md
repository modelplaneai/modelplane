# Modelplane — Strategy

**Author:** Bassam Tabbara
**Date:** March 19, 2026
**Status:** Internal working document

---

## The opportunity in one paragraph

Every serious organization building on AI will eventually run into the same problem: they need to operate AI inference on infrastructure they own and control, and there is no good platform to do it.

Four structural forces make this inevitable, not optional:

**Cost at scale.** A company running 10B tokens/day on managed APIs spends $15–20M/year. Moving the same workload to owned GPU infrastructure costs 60–70% less — not through engineering heroics, but through removing vendor margins on compute. At volume, this is the largest infrastructure cost most organizations will face.

**Regulatory inevitability.** The EU AI Act, US executive orders on AI in critical infrastructure, and sector-specific data residency requirements (HIPAA, FedRAMP, GDPR) make external inference structurally impossible for entire industries. This is not a preference — a hospital or bank whose prompts touch patient or financial data has no legal path to managed inference at scale.

**Latency and performance ownership.** Managed inference SLAs are shared-infrastructure SLAs. Organizations with latency-sensitive products — real-time voice, trading systems, embedded AI — cannot accept tail latency governed by someone else's fleet state.

**Customization.** Fine-tuned and proprietary models cannot run on managed inference platforms. When an organization's competitive differentiation is a domain-specific model, there is no path forward except owning the inference layer.

The managed inference services — Baseten, Fireworks, Together.ai — are excellent but they own the infrastructure and the control layer, which makes them structurally unusable for regulated industries, organizations with data sovereignty requirements, and anyone who needs to own their cost structure at scale. Their economics are also deteriorating — margins are thin and getting thinner as GPU costs compress, making the token pricing model increasingly unsustainable for customers running at volume. The open source tools — KServe, KubeAI, vLLM — are powerful engines but they are not platforms. They solve the serving problem on a single cluster. Nobody has solved the operations, governance, and federation problem across a fleet. The position is vacant. Modelplane claims it.

---

## What Modelplane is

Modelplane is the open source federation layer for AI inference. It is the central control plane that manages a fleet of inference environments — across cloud, neo cloud, and on-premise — and exposes the whole thing as a self-service platform for developers and ML engineers.

The closest analogy is Baseten's central layer. Baseten's architecture is a federated control plane (hosted by Baseten) managing distributed data planes (their cloud or your VPC). Everything valuable about Baseten — fleet management, intelligent routing, cost attribution, policy enforcement, self-service developer experience — lives in that central layer. The data planes are relatively dumb; they just run models.

Modelplane is that central layer, running entirely within infrastructure you own, open source, with no vendor in the middle.

Modelplane is open core. The core capability — the federation control plane, the model catalog, the self-service deployment experience, multi-environment management — is open source and genuinely unlimited. Upbound AI is the commercial product built on top, adding the capabilities enterprises need at fleet scale. More on that below.

This is categorically different from what every competitor offers:

Managed inference services (Baseten, Fireworks, Together.ai) run your inference on their infrastructure under their control plane. Their BYOC options put the data plane in your VPC but the control plane remains theirs — their governance, their routing decisions, their visibility into your workloads.

Hyperscaler AI services (AWS Bedrock, Google Vertex AI, Azure AI Foundry) are single-cloud managed inference. They cannot unify workloads across clouds, neo clouds, and on-premise, and their token-based pricing prevents customers from using existing GPU investments and reservations.

Frontier model providers (Anthropic, OpenAI) are API-only inference for their own models. They are not self-hosted platforms and are not designed to run on infrastructure you own. They are potential gateway partners — workloads that need frontier model quality can be routed through Modelplane's intelligent gateway to these providers, making them part of the inference surface rather than alternatives to it.

Inference engines and operators (KServe, KubeAI, OME, vLLM, SGLang, Dynamo) run models on individual clusters. They are Modelplane's backends — the serving layer Modelplane manages — not its competitors.

Job orchestrators (SkyPilot) launch tasks across clouds for researchers. They manage ephemeral jobs, not persistent organizational inference infrastructure.

MLOps platforms (Red Hat OpenShift AI) are full-lifecycle platforms requiring their own distribution everywhere. They are not inference-specific and not infrastructure-agnostic.

The position Modelplane occupies — open source federation control plane for inference, running in your infrastructure — is empty.

---

## The problem it solves

### For enterprises

Large organizations are being asked by their boards and business units to deploy AI across their operations. They have existing GPU infrastructure — DGX clusters, cloud GPU reservations, Coreweave contracts. They have compliance requirements: prompts cannot leave their data centers, models must be approved before deployment, costs must be attributed per team, access must be audited. They have platform teams running Kubernetes, and in many cases Crossplane.

Their options today are all bad:

- Build it themselves — 12-18 months, high ongoing cost, nobody wants to own it long-term
- Buy Red Hat OpenShift AI — requires OpenShift everywhere, a full MLOps platform they don't need, and still provides no federation layer across their heterogeneous estate
- Use Baseten BYOC — their control plane still manages your data and your policy; unacceptable for regulated industries
- Assemble KServe, KEDA, vLLM, and custom YAML by hand — enormous operational complexity, no governance, no self-service for ML teams, no federation across clusters

What they actually need: a control plane that federates across their existing infrastructure, enforces their policies, gives their ML teams a self-service experience, and attributes cost — without sending data to a vendor's SaaS platform or ceding control of the governance layer.

### For AI-native companies

AI-native companies hit the managed inference wall when scale makes API economics painful. A company spending $15-20M/year on inference can reduce costs 60-70% by moving to owned GPU infrastructure. The problem is they don't want to become GPU infrastructure experts — they want the Baseten experience (deploy a model, get an endpoint, don't think about the rest) on infrastructure they own.

Their options today are equally bad: stay on managed inference and accept the cost and lock-in, or build their own serving layer and become an infrastructure company. Neither is right.

What they actually need is the Baseten experience on their own Coreweave, AWS, or NVIDIA infrastructure, with the flexibility to route intelligently across their fleet as it grows.

### For AI service providers and AI factory operators

A third segment is emerging rapidly. Organizations — in the US, in sovereign nations, and across the world — are building AI factories: purpose-built infrastructure for token production at scale. Jensen Huang described $500B in orders in 2026 and at least $1T projected for 2027. These operators need a platform to run inference across their GPU fleets, serve multiple internal or external customers, enforce policy, attribute cost, and expose a self-service experience. They are building, in effect, a Baseten for their own infrastructure. Modelplane is the platform they need to build on.

---

## The strategic position

Modelplane claims the federation layer. This is the right layer to own for three reasons.

**It is where all the value lives.** Inference engines are commoditizing — vLLM, SGLang, Dynamo are excellent and getting better. The serving layer is not the differentiation. The federation layer — how you manage a fleet, route across it, govern it, attribute cost, and give developers a clean interface — is where every organization will eventually need to invest. Owning that layer, the way Crossplane owns infrastructure management, is the durable position.

**It is architecturally natural for Upbound.** Crossplane is a federation layer for infrastructure. Modelplane is the same architectural pattern applied to AI inference. The same reconciliation loop, the same declarative desired state, the same composability model. Upbound has spent eight years building this architecture and stewarding the open source ecosystem around it — Crossplane and Rook are both CNCF Graduated projects, among the very few infrastructure control plane projects to reach that status. The competitive moat is a decade of production experience building control planes at scale for Apple, JPMC, Nike, and hundreds of enterprises worldwide, combined with the ecosystem relationships and CNCF credibility to make an open source project land.

**It is structurally unaddressable by the incumbents.** Baseten cannot give you their central layer without their central layer being theirs. Managed inference players cannot offer the federation layer without giving up the business model that funds them. The only way to get the federation layer you own is to build it yourself — or use Modelplane. This is also explicitly an ecosystem play. Modelplane needs the same community governance, partnership network, and neutral positioning that Crossplane built over eight years. Upbound has done this twice before. The ecosystem flywheel is a real competitive advantage.

---

## What the federation layer actually does

Claiming the federation layer is only meaningful if it is built out. This is what the federation layer delivers — the capabilities that make Modelplane the central control plane for an organization's inference fleet, and that distinguish it from every single-cluster operator or managed service.

**What this looks like in practice.** An organization runs model deployments across a Coreweave lease and two on-premise DGX clusters. A cost spike hits Coreweave during peak hours — the federation layer detects current fleet economics in real time, reroutes 60% of traffic to the on-premise clusters automatically, and restores distribution when costs normalize. In parallel: every EU-origin request is pinned by policy to the EU cluster, enforced architecturally — not by an administrator remembering a configuration. A team in finance pushes a model update; it clears the deployment approval workflow and lands in staging, but the production budget threshold is exhausted for the month, so the control plane blocks the production rollout without human intervention. None of this is glued together from individual tools. It requires a control plane that holds real-time fleet state, enforces policy continuously, and makes routing decisions that account for all of it simultaneously.

**Fleet management.** A single view of every inference environment across every cloud, neo cloud, and on-premise cluster in the organization — health, capacity, active deployments, model versions, utilization. Not a dashboard bolted on top; a control plane that continuously reconciles actual state against desired state and surfaces the delta. Organizations running inference across AWS, Coreweave, and on-premise DGX simultaneously have one place to understand and operate all of it.

**Intelligent cross-environment routing.** Route inference requests across environments based on latency, cost, capacity, and availability. This is deceptively hard. Effective routing requires real-time fleet state across every environment — active models, GPU utilization, queue depth, latency distributions. It requires cost modeling that accounts for spot pricing, reservation amortization, and token throughput per dollar across heterogeneous hardware. It requires latency prediction that accounts for cold-start penalties, model cache state, and network topology. It requires failure handling that detects degradation before it cascades and reroutes without disrupting in-flight requests. Baseten spends enormous engineering investment at the fleet level building this — it is the core of their operational moat. Modelplane's reconciliation-based architecture is the right foundation: routing policy is declared once, enforced continuously by the control plane, and updated as fleet state changes — not configured manually and forgotten. This is where the control plane architecture matters most.

**Cost attribution and optimization.** Which team consumed what GPU capacity across which environments, at what cost per token. Showback and chargeback across the fleet. Budget controls that block deployments when quotas are exceeded. This is what makes the platform financially governable at enterprise scale — the difference between GPU infrastructure as a shared resource nobody manages and GPU infrastructure as an allocated, accountable asset.

**Policy engine.** Declarative policies that enforce data sovereignty, deployment approvals, model governance, and compliance rules across the fleet. Not Kubernetes RBAC — purpose-built inference policies that the control plane enforces continuously. A policy that says "no model may be deployed to production without security review" or "EU tenant data may only be processed in EU environments" is expressed once and enforced everywhere, automatically.

**Data sovereignty controls.** The federation layer knows which requests are allowed to go where and enforces it architecturally. EU data never reaches US environments. Regulated data never leaves air-gapped clusters. This is not a configurable rule that an administrator remembers to set — it is structural enforcement built into the control plane. For regulated industries, this is the capability that makes the platform procurable. For sovereign AI deployments, it is the entire point.

**Developer experience layer.** CLI, UI, and API that give the ML team a clean interface to the federation layer. `modelplane deploy llama-70b --env production` and it just works, regardless of whether production runs on AWS, Coreweave, or an on-premise DGX cluster. The fleet complexity is entirely invisible to the developer. This is what Baseten invests in most visibly and what drives their developer loyalty. Modelplane needs to match it — not eventually, but as a first-class investment alongside the control plane infrastructure.

**Observability across the fleet.** Aggregated metrics, latency distributions, error rates, and token throughput at the organization level — not per-cluster. The ability to understand your inference fleet the way Baseten understands it from their central position: which models are performing, which environments are saturated, where cost is concentrated, where latency is degrading. This visibility is what enables every other capability on this list to be exercised intelligently.

These capabilities define the product. v0.1 ships the foundation. The roadmap earns the rest.

---

## The three customer segments

### Enterprises

Enterprises arrive at Modelplane through compliance and sovereignty forcing functions. A bank's prompts cannot leave their data centers. A hospital's patient data cannot touch external infrastructure. A government agency requires air-gapped deployment. These are hard requirements, not preferences. No managed inference platform — regardless of quality — can serve them.

The enterprise buyer is the **platform engineering team**. These teams operate Kubernetes across multiple clouds and on-premise. They speak declarative infrastructure. Most large organizations running Kubernetes are receptive to Crossplane; many already run it. Modelplane extends the same control plane they already operate — or would readily adopt — into AI inference without requiring new tooling or new operational models.

Their purchase criteria: does it fit our operational model, does it satisfy our compliance requirements, can our ML teams self-serve without coming to us for every deployment, can we attribute cost and enforce policy across our entire fleet?

Enterprise adoption is driven by compliance necessity and accelerated by operational fit.

### AI-native companies

AI-native companies arrive at Modelplane through cost and control forcing functions. Managed inference works at prototype scale. At production scale — 10B tokens/day and above — the cost on managed APIs is multiples of what owned GPU infrastructure costs at the same throughput.

The buyer is a **senior engineer or head of infrastructure** who is tired of paying managed inference margins and wants the same developer experience on infrastructure they own. They have Coreweave contracts, AWS GPU reservations, or on-premise clusters sitting underutilized. They are not looking for a new infrastructure project — they are looking for the operational layer that makes their existing GPU investment useful.

Their purchase criteria: same developer experience as Baseten, runs on our infrastructure, intelligent routing across our GPU fleet to optimize cost and latency.

AI-native adoption is driven by cost pressure and accelerated by developer experience quality.

### AI service providers and AI factory operators

Organizations building AI factories — sovereign AI infrastructure for national governments, GPU cloud operators serving enterprise customers, large enterprises building internal AI platforms at scale — need a platform to operate their fleet. They are providers of inference to others, not end consumers. Their requirements combine elements of both segments above: self-service for their customers, governance and attribution across their fleet, data sovereignty enforcement, and the ability to run at a scale that token-based SaaS pricing makes economically impossible.

The buyer is a **CTO or VP of Infrastructure** building the operational layer for their AI factory. Modelplane gives them the open source platform to do it. Upbound AI gives them the commercial product with the certifications and managed operations their customers require.

---

## Why they choose Modelplane over alternatives

**Over DIY:** Modelplane is what they would build if they had 18 months and a dedicated team. It is available today, open source, and self-operable forever. The open core model means they get a complete foundation without the build cost, and they retain full ownership of the platform.

**Over managed inference (Baseten, Fireworks, Together.ai):** Their data stays in their infrastructure. Their control plane is theirs. At scale, their economics are dramatically better — they use their own GPU capacity rather than paying vendor margins on compute. Modelplane gives them the same self-service developer experience on infrastructure they own.

**Over hyperscaler AI services (Bedrock, Vertex AI, Azure AI Foundry):** Not locked to a single cloud. Runs across cloud, neo cloud, and on-premise simultaneously. Can use existing GPU investments and reservations. No token-based pricing that penalizes scale. A single platform across a heterogeneous estate.

**Over frontier model APIs (Anthropic, OpenAI):** Not alternatives for self-hosted open-weight model deployment. Modelplane's intelligent gateway routes appropriate workloads to frontier APIs when self-hosted models are not the right tool — making Modelplane a complete inference platform rather than a closed system.

**Over KServe, KubeAI, OME raw:** Modelplane is a platform, not an operator. These are Modelplane's backends. Modelplane adds the federation layer — fleet management, multi-environment routing, model catalog, self-service for ML teams, governance — that single-cluster operators do not provide. Users running raw KServe who want the platform abstraction are the natural Modelplane adopters.

**Over Red Hat OpenShift AI:** Infrastructure-agnostic. Runs on any Kubernetes distribution, any cloud, any GPU cloud, any on-premise estate. Does not require OpenShift. Narrowly focused on inference, not a full MLOps platform requiring commitment to Red Hat's full stack. Red Hat cannot offer neutral infrastructure positioning without compromising their core business.

**Over SkyPilot:** Persistent organizational platform, not job orchestration. Governance model designed for organizations, not individual researchers. Federation across a persistent fleet, not ad-hoc task launching.

---

## How Upbound makes money

Modelplane is true open source — Apache 2, no usage limits, no cluster caps, no token restrictions. This is not negotiable. The moment usage limits appear, community trust, partnership opportunities, and the CNCF trajectory evaporate. The Crossplane precedent holds exactly: genuine open source drives adoption; commercial captures value at the enterprise layer.

**The principled boundary.** Modelplane is a single-fleet control plane — everything an organization needs to operate AI inference within one logical fleet, completely and without restriction. Upbound AI is the multi-fleet layer and enterprise system-of-record: unified management across multiple Modelplane deployments, the intelligent gateway that extends routing decisions across self-hosted and frontier APIs, and the enterprise governance capabilities that large organizations require for compliance procurement. The line is not arbitrary — it maps directly to organizational complexity. A single team running inference on one cluster or one cloud needs Modelplane. An enterprise operating inference across business units, regions, or customer environments — and needing to treat all of it as one coherent system — needs Upbound AI.

**Upbound AI** is the commercial product built on Modelplane. It adds the capabilities enterprises and AI factory operators need at federation scale that open source Modelplane genuinely does not provide:

**Global management plane.** Modelplane runs one inference fleet per deployment. Organizations operating multiple fleets — across regions, business units, or customer environments — need a unified management layer. Upbound AI provides the global console, API, and control fabric across multiple Modelplane deployments with cross-cluster observability and fleet-level analytics.

**Intelligent gateway.** Cost, quality, and latency-aware routing across self-hosted models and frontier APIs. Hybrid routing between owned GPU infrastructure and Anthropic, OpenAI, or other providers when appropriate. This is the capability that makes Modelplane a complete inference platform rather than a self-hosted-only solution.

**Enterprise governance.** Policy engine beyond Kubernetes RBAC — deployment approval workflows, compliance-ready audit trails, cost attribution and chargeback across teams and environments, budget controls that enforce spending limits at the fleet level.

**Enterprise integrations.** Integrations with enterprise identity providers, SIEM systems, cost management platforms, and observability stacks at the depth enterprise procurement requires.

**Managed operations.** Upbound runs the control plane as a managed service for enterprises that want the developer experience without the operational burden — the same model as Upbound Platform (managed Spaces) applied to Modelplane.

**Support and professional services.** Enterprise support contracts with SLA guarantees. Professional services for implementation, migration, and optimization. Table stakes for regulated industry procurement.

**Pricing model:** Upbound AI is priced on GPU infrastructure under management — not token volume. Charging per token recreates the cost structure that drove buyers to self-hosting in the first place. The value Upbound AI delivers is autonomous management of AI infrastructure at enterprise scale. Price proportional to the scope of what is being continuously managed — GPU count or cluster count under the global management plane.

**The flywheel:** Modelplane deployments grow through open source adoption and ecosystem partnerships. Organizations that hit multi-cluster complexity, compliance requirements, or need enterprise support convert to Upbound AI. Every Upbound AI customer also runs Upbound Platform underneath — the stack compounds. GPU infrastructure count grows with customer AI ambitions. Revenue grows with infrastructure scope, not token volume. The same flywheel Crossplane and Upbound Platform have been building for eight years, now applied to the inference layer.

---

## How we launch

### v0.1 — Control plane primitives

v0.1 ships the control plane primitives — the declarative resource model, the reconciliation loop, the multi-environment targeting layer — that everything else is built on. It is not the federation layer yet. It is the foundation from which the federation layer gets built, in public, with design partners pulling the roadmap into shape.

It ships:

- Five declarative resources: ClusterModel, Model, InferenceEnvironment, ModelDeployment, ModelPlacement
- KServe LLMInferenceService as the inference backend
- NVIDIA Dynamo as a second backend, with NIM support and NVIDIA-native integrations
- AWS, GCP, Azure, Oracle, and Coreweave as infrastructure targets, plus BYOC via kubeconfig for enterprises with existing clusters
- 4-5 popular models (Llama 3.1 8B, 70B, Qwen 2.5, Mistral) with pre-tested configurations
- OpenAI-compatible endpoint from every deployment
- Two-line ML team self-service deployment experience

The honest framing: v0.1 proves that models can be managed as declarative infrastructure resources across multiple environments. v0.3 is where the federation value becomes visible. The gap between them is the roadmap — transparent, dated, and design-partner-driven.

v0.1 proves the architecture works, establishes the category claim, secures strategic partners, and creates the design partner relationships that pull the federation features into existence. The risk of waiting for a complete product is that the position gets filled.

### Launch motion

**Claim the position publicly.** The launch narrative: we are building the open source federation layer for AI inference — the central control plane that manages your fleet of inference environments. Here is v0.1, the foundation. Here is what follows. Transparent about what it is today and what it becomes.

**NVIDIA partnership first.** The most important launch action is formalizing the NVIDIA relationship. Existing relationships across the NCP, DGX Cloud, and BCM teams make this achievable. The ask: NIM integration certification, Dynamo as a supported backend, joint reference architecture for DGX/BCM deployments, NCP AI Cloud Ready status. This credential makes every enterprise conversation easier and positions Modelplane as the operational layer for AI factory deployments. NVIDIA sells AI factories — Modelplane operates them.

**Neo cloud and cloud co-launch.** Coreweave and Oracle are the natural partners — enterprise-focused, NVIDIA-aligned, need the operational story to compete with hyperscalers. Joint reference architectures and co-marketing make Modelplane the default inference platform for their customers. Lambda Labs extends the neo cloud story.

**Crossplane community activation.** The existing Crossplane community — hundreds of enterprises, active contributors, KubeCon presence — is the most valuable distribution channel at launch. Modelplane is announced as the natural extension of the control plane they already operate. No new adoption barrier for existing Crossplane users.

**CNCF Sandbox application.** Immediately after launch, in coordination with launch partners, Modelplane applies for CNCF Sandbox status. Crossplane's CNCF Graduated status is the trust signal that makes enterprise procurement possible. Modelplane on a clear path to the same governance creates the same signal with the same audience.

**5-10 design partners before launch.** Identify organizations — mix of enterprises, AI-native companies, and AI factory operators — who will use v0.1 in production and provide structured feedback. Their requirements drive the v0.2/v0.3 roadmap. Their case studies validate the category claim and the commercial story.

### What follows v0.1

The federation layer gets built in order of what design partners reveal matters most. The expected sequence:

**v0.2 — Breadth and developer experience**

- KubeAI as a second backend — adds scale-to-zero, broader engine support
- `modelplane` CLI — developer experience beyond kubectl
- Custom model packaging — support for custom and fine-tuned models beyond HuggingFace open-weight models
- SGLang engine support
- EKS cluster provisioning as a second managed cloud provider

**v0.3 — Federation value**

- Fleet management — unified view across all inference environments: health, capacity, model versions, active deployments
- Cost attribution — per team, per model, per environment; showback and chargeback across the fleet
- Intelligent routing — latency-aware and cost-aware request routing across environments
- Policy CRDs — PlacementPolicy, ResourcePolicy, RoutingPolicy for governance at the fleet level

**v0.4 — Enterprise depth and Upbound AI**

- Intelligent gateway with frontier API routing — self-hosted models plus Anthropic, OpenAI, and other providers in a unified inference surface
- Advanced policy engine — deployment approval workflows, compliance controls, sovereign data enforcement
- Upbound AI global management plane — cross-cluster observability, fleet-level analytics, enterprise console
- SOC 2 Type II certification audit begins

The transition from community product to enterprise platform happens between v0.3 and v0.4. v0.1 through v0.3 build the open source foundation and prove the category. v0.4 is where Upbound AI becomes commercially serious.

---

## The competitive dynamics

**Baseten** raised $585M at a $5B valuation solving inference operations for AI-native companies on managed infrastructure. Their moat is their central layer — routing intelligence, performance optimization, forward deployed engineers — built over six years. They cannot give customers that central layer without it being theirs. Structurally, they cannot address enterprises with data sovereignty requirements, organizations on NVIDIA on-premise hardware, AI factory operators, or anyone who needs the control plane itself to be in their infrastructure. The gap they structurally cannot fill is exactly the gap Modelplane fills.

**KServe, KubeAI, OME** are inference engines and operators. They run models on clusters. They are Modelplane's backends, not its competitors. The right relationship is integration and community partnership. Users running raw KServe who want the platform abstraction are the natural Modelplane adopters.

**Red Hat OpenShift AI** is the most mature enterprise competitor. NCP-certified, NVIDIA-partnered, full MLOps platform. The structural limit: requires OpenShift everywhere. Enterprises running heterogeneous Kubernetes estates cannot use OpenShift AI without OpenShift underneath all of it. Modelplane is infrastructure-agnostic by design. Red Hat cannot offer that neutrality without undermining their core business.

**Hyperscalers (AWS, Google, Azure)** offer managed AI inference deeply integrated with their own clouds. They cannot serve workloads across other clouds or on-premise, cannot unify heterogeneous estates, and their managed model makes data sovereignty requirements difficult or impossible to satisfy. They are the right answer for cloud-committed workloads. They are the wrong answer for everything else — which is where Modelplane lives.

**Frontier model providers (Anthropic, OpenAI)** are not competitors for self-hosted inference. They are gateway partners — Modelplane routes appropriate workloads to these providers through its intelligent gateway, making them part of a complete inference platform rather than alternatives to it.

**NVIDIA** is the most important partner and the most significant long-term wildcard. The threat is real and worth stating plainly: Run:ai provides GPU scheduling, NIM provides model packaging, Dynamo provides inference optimization, and NIM Microservices increasingly abstract the serving layer. The components of an integrated, NVIDIA-native inference platform exist inside NVIDIA today. The question is whether they assemble them into a full control plane.

The answer is probably not — and the reason is structural, not optimistic. NVIDIA's business is hardware distribution. Their software strategy is designed to make NVIDIA hardware easier to buy and operate — not to become a software company managing customers' control planes. A proprietary NVIDIA inference platform would require NVIDIA to take on operational accountability for customer fleets, compete with the partners (Baseten, Coreweave, managed services) who are buying enormous quantities of their hardware, and position their software as a reason to prefer NVIDIA over other GPU vendors. None of these serve their core business.

The partnership path is more valuable to both sides: NVIDIA sells AI factories, Modelplane operates them. An open source inference control plane that makes DGX hardware more operable, that certifies NIM and Dynamo as first-class backends, and that gives enterprise buyers a neutral operational layer is better for GPU hardware sales than a closed NVIDIA platform that customers are reluctant to adopt. The existing relationships across NCP, DGX Cloud, and BCM make this partnership achievable. The strategic alignment makes it durable.

---

## The full case in three sentences

Crossplane became the open source standard for infrastructure control planes because organizations need to own the layer that governs their infrastructure. The same logic applies to AI inference — and the same position is vacant. Modelplane is Crossplane for inference: the open source control plane that manages a fleet of inference environments the same way Crossplane manages cloud infrastructure, running entirely in your own infrastructure, with no vendor in the control layer. The managed inference platforms structurally cannot offer this. The position is Upbound's to take.