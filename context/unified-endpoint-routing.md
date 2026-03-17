# Unified Endpoint Routing: Options and Recommendation

**Author:** Nic Cope  
**Date:** March 2026

---

The ModelDeployment unified endpoint — `status.endpoint.url` — needs to
physically exist as infrastructure. It's an OpenAI-compatible HTTP endpoint that
routes requests across all healthy ModelPlacements. Each placement lives on a
different InferenceEnvironment (a different Kubernetes cluster), and each has its
own per-environment endpoint behind that cluster's Envoy Gateway. The unified
endpoint is a cross-cluster HTTP reverse proxy problem.

Open question #4 in the PRD calls this out as unresolved. This document surveys
the options for what software could implement it and where it would run.

---

## Constraints

A few things narrow the field:

- **Cross-cluster.** ModelPlacements are on different Kubernetes clusters,
  potentially in different clouds. Standard Kubernetes Service and HTTPRoute
  don't work across cluster boundaries.
- **OpenAI-compatible.** The endpoint passes through `/v1/chat/completions` and
  related paths. It needs to handle SSE streaming for token-by-token responses.
- **Composable.** Whatever we deploy, Crossplane composition functions need to
  manage it. CRD-based configuration is strongly preferred over config files or
  imperative APIs.
- **Round-robin for v0.1.** Simple equal-weight load balancing is sufficient.
  But the choice should leave a clear path to intelligent routing (closest,
  cheapest, fastest) in v0.2+.
- **Portable.** Must work on both vanilla Crossplane and Upbound Spaces without
  Spaces-specific code paths.

It's also worth noting the single-placement case. When a ModelDeployment targets
one InferenceEnvironment, the unified endpoint could just be a CNAME or
passthrough to the placement's endpoint — no routing proxy needed. The routing
infrastructure is only required when there are 2+ placements.

---

## Where it would run

### On the control plane cluster

Deploy the routing proxy alongside Crossplane. `function-modelplane-deploy`
composes it as Kubernetes resources in the control plane cluster.

This is the simplest option. The deploy function already aggregates placement
status and manages the unified endpoint URL — composing the routing
infrastructure is a natural extension of what it already does.

The main concern is network connectivity. The control plane must be able to reach
each data plane cluster's gateway address. For self-hosted Crossplane or
self-hosted Spaces, this is a reasonable assumption — if `provider-kubernetes`
can reach the cluster's API server, the control plane can probably reach its
gateway. For Upbound Spaces cloud deployments, the data plane gateways would
need to be reachable from the control plane, which means either public-facing
gateway addresses or PrivateLink/VPC peering. That's likely already the case —
the `status.gateway.address` on InferenceEnvironment isn't useful if nothing can
reach it.

A secondary concern is that inference traffic flows through the control plane
cluster, which isn't designed for high-throughput data plane work. For a v0.1
proof of concept this is fine. For production at scale, users would want to move
the routing proxy elsewhere.

### On a dedicated gateway cluster

A lightweight cluster (no GPUs needed) running only the routing proxy, placed in
a network-optimal location with connectivity to all GPU clusters.

This separates inference traffic from the control plane, which is better
operationally. But it introduces a question: who provisions this cluster? It's
outside the InferenceEnvironment model. You'd need either a new resource type or
an InferenceEnvironment with `backend: None` that just runs a gateway. It's
awkward for v0.1.

### Cloud load balancer

Use `provider-aws` or `provider-gcp` to compose a cloud-native load balancer
(NLB, ALB, GCP HTTP LB) with backends pointing at each placement's gateway
address.

This is highly available, fully managed, and plays to Crossplane's strengths —
composing cloud resources is what Crossplane does. But it requires cloud-specific
composition logic (different for AWS, GCP, Azure), doesn't work on-prem, and is
harder to get working for a portable v0.1 demo. It could be a documented
alternative for production deployments.

### DNS-based routing

Weighted DNS records (via K8GB or ExternalDNS) distribute traffic across
placements. K8GB adds health checking and automatic failover.

DNS resolves once per TTL (typically 30–300s), so "round-robin" is
coarse-grained — each client talks to one backend for minutes. There's no
per-request intelligence. This is completely inadequate for the intelligent
routing strategies in v0.2+ (cheapest, fastest, closest all require per-request
decisions). Only viable for coarse failover, not load balancing.

### My take

The control plane cluster is the right default for v0.1. It's the simplest thing
that works, it's fully composable, and the networking constraint is reasonable
for a proof of concept. The cloud load balancer approach is worth documenting as
a production alternative for users who want managed infrastructure and are
running on a single cloud.

---

## What software

### Envoy Gateway with the Backend API

Envoy Gateway is already in the KServe dependency chain — it's a known quantity
in the Modelplane stack. Its [Backend API] (`gateway.envoyproxy.io/v1alpha1
Backend`) lets you define external endpoints by FQDN or IP, then reference them
as `backendRefs` in standard Gateway API `HTTPRoute` resources.
`BackendTrafficPolicy` provides round-robin, least-request, random, and
consistent-hash load balancing.

Here's how it would work. `function-modelplane-deploy` composes:

- One `Backend` resource per healthy ModelPlacement, with the FQDN set to that
  placement's gateway address (from `InferenceEnvironment.status.gateway.address`)
- One `HTTPRoute` with `backendRefs` pointing at all Backends with equal weights
- One `BackendTrafficPolicy` with `type: RoundRobin`

All of these are Kubernetes CRDs — exactly what Crossplane compositions are
designed to manage.

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: Backend
metadata:
  name: llama-70b-global-us-east
spec:
  endpoints:
    - fqdn:
        hostname: gpu-us-east.gateway.example.com
        port: 443
---
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: Backend
metadata:
  name: llama-70b-global-us-west
spec:
  endpoints:
    - fqdn:
        hostname: gpu-us-west.gateway.example.com
        port: 443
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: llama-70b-global
spec:
  parentRefs:
    - name: modelplane-gateway
  hostnames:
    - llama-70b-global.inference.example.com
  rules:
    - backendRefs:
        - group: gateway.envoyproxy.io
          kind: Backend
          name: llama-70b-global-us-east
          weight: 50
        - group: gateway.envoyproxy.io
          kind: Backend
          name: llama-70b-global-us-west
          weight: 50
```

The Backend API is disabled by default (for security — it can route traffic
outside the cluster). The env function would need to enable it in the Envoy
Gateway config with `extensionApis.enableBackend: true`.

There's no built-in health checking of Backend endpoints. The deploy function
would need to handle this by watching ModelPlacement status and removing Backends
for unhealthy placements from the HTTPRoute's `backendRefs`. This is fine —
the deploy function already aggregates placement health for status reporting.

The Backend API is `v1alpha1`, but so is everything else in Modelplane.

**v0.2+ upgrade path.** Envoy Gateway is the foundation for [Envoy AI Gateway],
which adds `AIGatewayRoute`, `AIServiceBackend`, and `BackendSecurityPolicy`
CRDs. These bring model-name virtualization (one unified model name maps to
different per-backend names), token-based rate limiting, provider fallback with
priority-based routing, and OpenTelemetry GenAI metrics. The upgrade from base
Envoy Gateway to Envoy AI Gateway is additive — same base, additional extension
CRDs. The model-name virtualization and token rate limiting map directly to
features enterprises will expect. The priority-based routing maps to the
`Closest`, `Cheapest`, `Fastest` strategies on the roadmap.

### Envoy AI Gateway

Everything above, plus the AI-specific extensions from day one. `AIGatewayRoute`
extracts model names from request bodies and routes based on them.
`AIServiceBackend` wraps backends with provider-specific auth and request
transformation. Token-based rate limiting tracks actual token consumption, not
just request counts.

I'd call this overkill for v0.1. For round-robin across identically-configured
placements serving the same model, the AI-specific features don't help. They add
an ExtProc deployment and CRDs beyond what base Envoy Gateway provides. But
they're the right answer for v0.2+ when routing needs to be model-aware and
cost-aware.

Starting with base Envoy Gateway and adding the AI Gateway extension later is a
clean upgrade — it's the same project, same maintainers, additive CRDs. I'd
treat this as "what the deploy function composes once intelligent routing lands."

### LiteLLM Proxy

LiteLLM is purpose-built for routing across multiple OpenAI-compatible backends.
It supports simple-shuffle (weighted random), least-busy, latency-based, and
cost-based routing strategies. It handles automatic fallbacks, retries, and
cooldowns. It's Redis-backed for multi-instance deployments, has a Helm chart,
and handles SSE streaming at 1,500+ RPS. It's been widely adopted — it's the
standard tool for multi-provider LLM routing.

The cost-based and latency-based routing strategies align well with the v0.2
roadmap. Virtual key management and per-team spend tracking are features
enterprises want. If Modelplane were a standalone application, LiteLLM would be
a strong choice.

The problem is composability. LiteLLM is configured via a YAML config file, not
Kubernetes CRDs. To manage it from a Crossplane composition, you'd compose a
ConfigMap containing the LiteLLM config and a Deployment running the proxy. When
placements come and go, you'd regenerate the ConfigMap and the proxy would need
to pick up the changes (LiteLLM watches its config file, but the timing is
imprecise). This is fragile compared to composing `Backend` and `HTTPRoute`
resources where Envoy Gateway's control plane handles the data plane update
atomically.

LiteLLM also needs Redis for shared state across instances, and optionally
PostgreSQL for spend tracking. That's more operational surface than "compose some
CRDs."

It also doesn't participate in the Gateway API ecosystem, so it can't benefit
from the Gateway API Inference Extension's KV-cache-aware routing work.

I wouldn't rule LiteLLM out entirely — there may be a future where Modelplane
uses LiteLLM as the routing engine behind an Envoy Gateway frontend, getting the
best of both worlds. But for v0.1, it doesn't fit the "everything is composed as
Kubernetes resources" model.

### Gateway API Inference Extension

InferencePool and the Endpoint Picker (EPP) provide KV-cache-aware,
prefix-aware, LoRA-affinity-aware routing. This is the most sophisticated
inference routing available in the Kubernetes ecosystem, and it's becoming the
shared substrate that KServe, AIBrix, and OME all integrate with.

But it operates within a single cluster. InferencePool selects pods by label in
the same namespace. The EPP scrapes Prometheus metrics from local model server
pods. There's no cross-cluster mode.

This is already being used at the per-ModelPlacement level — KServe creates an
InferencePool and EPP for each LLMInferenceService. The cross-cluster routing
problem is a layer above this. The Inference Extension doesn't compete with
Envoy Gateway's Backend API; they operate at different levels of the stack.

In the future, a cross-cluster aggregation of InferencePool metrics could feed
into the unified endpoint's routing decisions (imagine routing to the cluster
with the lowest aggregate KV cache utilization), but that doesn't exist today
and isn't something Modelplane should build in v0.1.

### Cloud load balancers (composed via Crossplane providers)

Use `provider-aws` to compose an NLB/ALB with target groups, or `provider-gcp`
for a GCP HTTP LB with backend services. Each ModelPlacement's gateway address
becomes a backend.

This is production-grade — highly available, managed, scales independently. And
Crossplane is literally designed to compose cloud resources, so the
composability story is strong. The deploy function would compose the load
balancer and its backends using the same patterns as any other Crossplane
composition.

The downside is portability. You need different composition logic for each cloud
provider. It doesn't work on-prem. And it's more complex to set up for a v0.1
demo than deploying Envoy Gateway on the control plane cluster.

I'd position this as a production alternative — documented in examples, not the
default path. "If you're running all your InferenceEnvironments on AWS, here's
how to compose an ALB as the unified endpoint instead of the default Envoy
Gateway."

### DNS-based routing (K8GB / ExternalDNS)

Weighted DNS records with health checking. K8GB is purpose-built for
multi-cluster global load balancing via DNS and supports round-robin, weighted
round-robin, failover, and GeoIP strategies.

DNS-based routing has a fundamental granularity problem. DNS resolves once per
TTL, so each client talks to one backend for 30–300 seconds. There's no
per-request load balancing. This makes it unsuitable for the v0.2+ intelligent
routing strategies, which all require per-request decisions. It's also a poor fit
for demonstrating that multi-environment routing works — a demo where requests go
to a single backend for minutes at a time doesn't look like load balancing.

K8GB could work as a coarse failover layer above the routing proxy (if the
control plane cluster's Envoy Gateway goes down, DNS fails over to a backup),
but it doesn't solve the primary routing problem.

---

## Summary

| Option | v0.1 fit | v0.2+ path | Composable | AI-aware |
|--------|----------|------------|------------|----------|
| **Envoy Gateway + Backend API** | Best | Add AI Gateway ext. | CRDs, native | No (v0.1) |
| Envoy AI Gateway | Overkill | Best | CRDs, native | Yes |
| LiteLLM | Workable | Good routing | ConfigMap (fragile) | Yes |
| GW API Inference Extension | Wrong layer | Intra-cluster only | N/A | Yes |
| Cloud LB (via providers) | Good (per-cloud) | Limited | Provider MRs | No |
| DNS (K8GB) | Too coarse | Inadequate | Limited | No |

I'd recommend **Envoy Gateway with the Backend API, deployed on the control
plane cluster.** It's already in the stack, it's CRD-native so Crossplane can
compose it, round-robin is a one-line weight config, and upgrading to Envoy AI
Gateway for v0.2 is additive. The deploy function composes `Backend` +
`HTTPRoute` + `BackendTrafficPolicy` per ModelDeployment, which is exactly the
kind of resource composition that Crossplane functions are designed for.

[Backend API]: https://gateway.envoyproxy.io/docs/tasks/traffic/backend/
[Envoy AI Gateway]: https://aigateway.envoyproxy.io/
