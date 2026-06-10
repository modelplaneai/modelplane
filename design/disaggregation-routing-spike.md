# Disaggregation Routing Feasibility Spike

**Status:** Complete  
**Date:** June 2026  
**Author:** Dennis Ramdass  
**Branch:** dennis/disagg-impl  

## Verdict

The working assumption — "emit one InferencePool selecting both role pods, EPP as picker, HTTPRoute→InferencePool" — needs modification on two independent axes. First, core Envoy Gateway (which Modelplane's ServingStack installs) does **not** support `InferencePool` as an HTTPRoute backendRef; that capability lives in a separate project, Envoy AI Gateway, or in Istio, kgateway, and GKE Gateway. Second, the llm-d inference-scheduler's disaggregation architecture uses a **single** InferencePool that contains only the decode pods; the EPP picks the decode target, then injects an `x-prefiller-host-port` header into the forwarded request so the decode-side routing sidecar can pull the KV cache from the chosen prefill pod. The assumption that the InferencePool selects "both role pods" and the EPP pair-picks is incorrect — prefill pods are outside the InferencePool entirely, reachable only as sidecar-forwarded targets. These two findings together mean that the Phase 2 backend must either (a) switch the workload gateway from core Envoy Gateway to a GAIE-conformant implementation or (b) retain the current `HTTPRoute → Service` pattern and build disaggregation coordination at a lower level.

---

## Gateway Support (Q1)

### InferencePool API version and status

The Gateway API Inference Extension (GAIE) shipped `InferencePool` v1 GA under the API group `inference.networking.k8s.io` (the `x-k8s.io` pre-GA prefix was dropped at the v1.0.0 release). As of v1.0.1 (late 2025), the resource is considered stable. The companion `InferenceModel` type was renamed `InferenceObjective` at v1. All current documentation, examples, and conformance tests use `inference.networking.k8s.io/v1`.

Sources:
- [InferencePool API type](https://gateway-api-inference-extension.sigs.k8s.io/api-types/inferencepool/) — GA since v1.0.0, `inference.networking.k8s.io/v1`
- [v1 API Reference](https://gateway-api-inference-extension.sigs.k8s.io/reference/spec/)
- [Introducing Gateway API Inference Extension (Kubernetes blog, June 2025)](https://kubernetes.io/blog/2025/06/05/introducing-gateway-api-inference-extension/)

### Core Envoy Gateway

**Core Envoy Gateway does not support InferencePool.** Modelplane's ServingStack installs the core Envoy Gateway chart (`oci://docker.io/envoyproxy/gateway-helm`, version v1.3.0 as of the current design). The v1.3.0 release notes contain no mention of InferencePool, GAIE, or inference extension support. The project's extension-types API lists only `Endpoints` and `DynamicResolver` as backend types; InferencePool is absent. The GAIE implementations list on `gateway-api-inference-extension.sigs.k8s.io` does not include core Envoy Gateway.

This is already noted in the codebase: `functions/compose-model-replica/function/backends/llmd.py` explicitly documents that "Envoy Gateway's `InferencePool` v1 support is unconfirmed; alternatively switch the workload gateway to Istio/agentgateway."

Sources:
- [Envoy Gateway v1.3.0 release notes](https://gateway.envoyproxy.io/news/releases/notes/v1.3.0/) — no inference extension mention
- [Envoy Gateway extension types](https://gateway.envoyproxy.io/latest/api/extension_types/) — no InferencePool

### Envoy AI Gateway (separate project)

**Envoy AI Gateway** is a distinct project (`envoyproxy/ai-gateway`, `aigateway.envoyproxy.io`) that wraps core Envoy Gateway with AI-specific features. It is **not** what Modelplane's ServingStack installs. Envoy AI Gateway v0.3.0 (August 2025) introduced InferencePool support via integration with GAIE v0.5.1. HTTPRoute backendRefs with `group: inference.networking.k8s.io`, `kind: InferencePool` are supported. Only one InferencePool per HTTPRoute rule is permitted. Envoy AI Gateway labels this as a non-alpha capability, but the project as a whole is still pre-v1.0.

Sources:
- [Envoy AI Gateway InferencePool support (v0.3 docs)](https://aigateway.envoyproxy.io/docs/0.3/capabilities/inference/inferencepool-support/)
- [HTTPRoute + InferencePool guide](https://aigateway.envoyproxy.io/docs/capabilities/inference/httproute-inferencepool/)
- [EPP blog post (July 2025)](https://aigateway.envoyproxy.io/blog/endpoint-picker-for-inference-routing/)
- [Envoy AI Gateway v0.3.x release notes](https://aigateway.envoyproxy.io/release-notes/v0.3/)

### Alternatives that do support InferencePool

All three alternatives below are stable or near-stable with GAIE v1:

| Gateway | InferencePool support | Notes |
|---|---|---|
| **Istio** | v1.28+ (full v1 support); v1.29 promotes to beta | Full service-mesh feature set; significant operational overhead vs. standalone Envoy Gateway |
| **kgateway** | v2.0.x (stable) | Envoy-based; built specifically for AI workload routing; lighter than Istio |
| **GKE Gateway** | Listed as supported | GCP-managed; only relevant if workload clusters are GKE |
| **Envoy AI Gateway** | v0.3.0 (pre-v1.0 project) | Superset of core Envoy Gateway; replacing the Helm chart is the lowest-friction path |

Switching Modelplane's workload gateway would involve:
1. Replacing the Envoy Gateway Helm chart in `compose-serving-stack` with the chosen alternative's chart.
2. Updating the GatewayClass controller name.
3. Verifying that existing `HTTPRoute → Service` resources (current native and llm-d paths) continue to work unchanged (all three alternatives are Gateway API conformant).
4. For Istio: adding the Istio control-plane operator; the operational footprint is substantially larger.
5. For Envoy AI Gateway: the switch is minimal — it ships its own operator on top of the core Envoy Gateway data plane, so existing HTTPRoutes continue to work and InferencePool becomes available as an additional backendRef kind.

Sources:
- [Istio GAIE support blog](https://istio.io/latest/blog/2025/inference-extension-support/)
- [Istio 1.28 GA announcement](https://istio.io/latest/news/releases/1.28.x/announcing-1.28/)
- [kgateway inference extension docs](https://kgateway.dev/docs/envoy/2.0.x/integrations/inference-extension/)
- [GAIE implementations page](https://gateway-api-inference-extension.sigs.k8s.io/implementations/gateways/)

---

## Disaggregation Request Mechanism (Q2)

### Architecture overview

The llm-d inference-scheduler architecture document explicitly states: **"Single `InferencePool` and single `EPP` due to Envoy limitations."** The InferencePool contains **only the decode pods** — it selects on `llm-d.ai/role: decode` (plus an app label). Prefill pods are separate Kubernetes workloads that are not members of the InferencePool.

Source: `github.com/llm-d/llm-d-inference-scheduler/blob/main/docs/architecture.md` (confirmed via fetch; single-pool constraint explicitly stated)

### Request flow

1. **Client → Gateway**: The client sends an OpenAI-compatible request to the workload cluster's inference gateway (HTTPRoute → InferencePool).
2. **EPP selects decode pod**: The EPP (running as a GAIE ext-proc sidecar) receives the request via Envoy's External Processing filter. It runs the scheduling pipeline against the decode pods in the InferencePool:
   - **Filter pass**: The decode-filter (`NewDecodeRole`) retains only pods with `llm-d.ai/role` values of `decode`, `prefill-decode`, or `encode-prefill-decode`.
   - **Score pass**: Scorers evaluate KV cache locality, queue depth, prefix hit probability, and session affinity.
   - **Select**: The highest-scored decode pod is chosen.
3. **EPP also selects a prefill pod**: For a disaggregated request (prompt length above the disaggregation threshold), the scheduler runs a second scheduling pass — a prefill filter pass using `NewPrefillRole`, which retains pods labelled `prefill`, `prefill-decode`, or `encode-prefill-decode`. A prefill pod is selected for KV-cache locality.
4. **Header injection**: The EPP injects the chosen prefill target as an `x-prefiller-host-port` header (in `host:port` format) into the request before forwarding it to the selected decode pod.
5. **Decode pod → Prefill pod (sidecar)**: A routing sidecar co-located with the decode pod intercepts the request, reads `x-prefiller-host-port`, and proxies the prompt to the designated prefill pod's vLLM (`kv_producer`) engine. The prefill engine processes the prompt and transfers the resulting KV cache to the decode pod via NixlConnector over the fast interconnect (NVLink/RDMA).
6. **Decode pod generates tokens**: The decode pod's vLLM (`kv_consumer`) engine consumes the transferred KV cache and generates tokens. The streaming response returns through the sidecar → EPP → gateway → client.

**Note**: The routing sidecar project (`llm-d/llm-d-routing-sidecar`) was archived on 3 February 2026; its code has been folded into `llm-d/llm-d-inference-scheduler`. The disaggregation sidecar is now described as a component of the scheduler repo, deployed alongside decode workers.

### Required pod labels

| Label | Prefill pods | Decode pods | "Both" pods |
|---|---|---|---|
| `llm-d.ai/role` | `prefill` | `decode` | `prefill-decode` |
| `app` (example) | `<model>-prefill` | `<model>-decode` | `<model>-worker` |

The InferencePool selector uses `llm-d.ai/role: decode` (plus an app label). Prefill pods are not selected by the InferencePool but must be reachable by cluster-internal DNS for the sidecar's `x-prefiller-host-port` forwarding.

Sources:
- [llm-d inference-scheduler architecture](https://github.com/llm-d/llm-d-inference-scheduler/blob/main/docs/architecture.md) — single InferencePool constraint, dual filter pass for P/D
- [filter package (pkg.go.dev)](https://pkg.go.dev/github.com/llm-d/llm-d-inference-scheduler/pkg/plugins/filter) — `NewDecodeRole`, `NewPrefillRole`, `llm-d.ai/role` label constants
- [llm-d routing sidecar (archived)](https://github.com/llm-d/llm-d-routing-sidecar) — `x-prefiller-host-port` header, archived Feb 2026, code moved to scheduler repo
- [Solo.io deep dive](https://www.solo.io/blog/deep-dive-into-llm-d-and-distributed-inference) — `x-prefiller-url` header and decode→prefill forwarding flow
- [Spheron deployment guide](https://www.spheron.network/blog/llm-d-kubernetes-disaggregated-inference-guide/) — InferencePool selector targets decode pods only; prefill pods are outside the pool

---

## Implications for the Phase 2 backend

### Working assumption: "emit one InferencePool selecting both role pods, EPP as picker, HTTPRoute→InferencePool"

This assumption must be revised on two counts:

**1. The InferencePool selects decode pods only, not both roles.**  
The assumption that the InferencePool selects "both role pods" is incorrect. The InferencePool is a decode-only pool. Prefill pods live outside it. The EPP runs two internal scheduling passes (one per role) and coordinates via the `x-prefiller-host-port` header, but only decode pods are registered in the pool. The correct mental model is: one InferencePool → decode pods only; the EPP has out-of-band knowledge of prefill pod addresses (e.g. from a Kubernetes-watch of pods with `llm-d.ai/role: prefill` in the same namespace). **This part of the working assumption is wrong but does not require an architectural rethink — the Phase 2 backend should emit one InferencePool scoped to decode pods plus a separate (unlabelled-by-pool) set of prefill pods with a headless Service so the sidecar can address them.**

**2. Core Envoy Gateway does not support HTTPRoute→InferencePool.**  
The gateway half of the working assumption ("HTTPRoute→InferencePool") cannot be implemented with the current workload gateway. The Phase 2 backend must make one of three choices before emitting InferencePool resources:

| Option | Change required | Complexity |
|---|---|---|
| **A: Switch to Envoy AI Gateway** | Replace the `gateway-helm` chart in `compose-serving-stack` with the Envoy AI Gateway chart; update GatewayClass controller name. Existing `HTTPRoute → Service` routes continue to work. | Low — same data plane, same resource model |
| **B: Switch to kgateway** | Replace chart and GatewayClass. Full GAIE v1 support in 2.0.x. Existing HTTPRoutes continue to work. | Low-medium |
| **C: Switch to Istio** | Add Istio control plane operator; GAIE v1 support in Istio 1.28+. Much larger operational footprint. | High |
| **D: Retain HTTPRoute→Service (no InferencePool)** | Keep the current pattern; implement disaggregation coordination entirely in the routing sidecar injected alongside decode pods, without a GAIE EPP. No gateway change needed. | Low — defers GAIE entirely |

Option A is the lowest-friction path: Envoy AI Gateway is built on core Envoy Gateway, its Helm chart replaces the existing one, and no existing Gateway API resources need to change.

> **DECISION (Dennis, 2026-06-10): Option A — Envoy AI Gateway.** ServingStack swaps its
> `envoyproxy/gateway-helm` release for the Envoy AI Gateway chart (same EG data plane) and
> installs the GAIE `InferencePool` CRDs. That gateway swap is well-understood and can land
> independently. Before writing the InferencePool/EPP/sidecar emission, confirm the two
> still-open items against `llm-d/llm-d-inference-scheduler` source: (1) the exact prefiller
> header name (`x-prefiller-host-port` vs `x-prefiller-url`) and how the EPP discovers prefill
> pod addresses outside the pool; (2) Envoy AI Gateway's current InferencePool maturity
> (confirmed at v0.3.x; verify the mid-2026 release/stability). Don't emit routing resources
> on the unconfirmed mechanism.

Option D is acceptable as a v0.1 disaggregation target if GAIE routing is deferred. The current `llmd.py` backend already uses this pattern (HTTPRoute→Service), and disaggregation can be layered on top via sidecar injection without involving the gateway at all. The gap is that prefix-cache-aware endpoint selection (the primary value of the EPP) is unavailable without a GAIE-conformant gateway.

### Confidence and evidence gaps

- The single-InferencePool architecture for disaggregation is well-evidenced from the scheduler's own architecture doc and the filter package source, corroborated by secondary sources. **High confidence.**
- The `x-prefiller-host-port` header and sidecar-forwarding flow are confirmed by the (now-archived) routing sidecar repo and the Solo.io deep dive. The routing sidecar code has moved into the scheduler repo; the header may have been renamed (the archived repo uses `x-prefiller-host-port`; the Solo.io post uses `x-prefiller-url`). The exact current header name should be confirmed against `llm-d/llm-d-inference-scheduler` source before implementing. **Medium confidence on exact header name.**
- Core Envoy Gateway's lack of InferencePool support is confirmed by absence of any mention in release notes and extension types documentation, and is consistent with the existing in-repo comment in `llmd.py`. **High confidence.**
- Envoy AI Gateway's InferencePool support at v0.3.0 is confirmed. Whether the project has reached v1.0 or stable status by mid-2026 is unconfirmed — latest confirmed release is v0.3.x (August 2025 series). **Medium confidence on current release.**
- The exact llm-d mechanism for the EPP to discover prefill pod addresses outside the InferencePool (Kubernetes watch, sidecar config, etc.) is not fully documented in publicly available sources. **Low confidence — check llm-d source directly before implementing C2 (pd-role label / decode Service excludes prefill).**
