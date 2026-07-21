# Inference-aware fleet routing

**Status:** Draft
**Date:** July 2026
**Author:** Dennis Ramdass

This document proposes cache-locality affinity and criticality on the control
plane's fleet gateway, composed against the Gateway API standard so the design
stays portable across gateways. It builds on [design.md](./design.md) and
addresses [#71](https://github.com/modelplaneai/modelplane/issues/71) (routing
affinity for cache locality) and
[#8](https://github.com/modelplaneai/modelplane/issues/8) (inference-aware routing
on the control plane gateway). It relates to
[#89](https://github.com/modelplaneai/modelplane/pull/89) (why the fleet gateway
is Traefik) and [#179](https://github.com/modelplaneai/modelplane/issues/179)
(per-cluster EPP config).

## Summary

Modelplane routes in two layers. The control plane's fleet gateway sends a request
to a cluster, and that cluster's own gateway and endpoint picker (EPP) choose a
replica. The per-cluster layer is already inference-aware: the EPP scores replicas
by prefix-cache hit and queue depth. The fleet layer is not. It routes by path and
static weight only, so it is cache-blind. A multi-turn chat can hit a different
cluster each turn and pay full prefill every time (~800ms TTFT against a cold
cache, ~150ms against a warm one), and the fleet can't express request priority.

The fix is to route the same session to the same cluster (cache locality, #71)
and to express criticality and weighting (#8). The design composes
these against the Gateway API standard and its inference extension, rather than
against any one gateway's own resources, so a conformant gateway works without
Modelplane carrying a codepath per vendor.

## Current routing

- **Fleet gateway (Traefik).** `compose-model-service` composes a Gateway-API
  HTTPRoute: path match `/<ns>/<name>/`, static per-endpoint weights, and a
  `URLRewrite` on each backendRef. That last piece is beyond the Gateway API spec,
  which permits `URLRewrite` only at the rule level, not per backendRef (#85).
  Spec-compliant gateways reject it, and Envoy returned 500s
  (envoyproxy/gateway#7099), so the control plane moved to Traefik (#89), which
  allows it as an extension. It's what lets one weighted split rewrite each
  backend's path differently, a self-hosted model at `/v1/` next to a provider at
  `/openai/v1/`.
- **Per-cluster gateway (Envoy Gateway + EPP).** `compose-model-replica` fronts
  the engines with an InferencePool and an endpoint picker that scores replicas by
  prefix cache and queue depth, and for disaggregated serving decides prefill
  versus decode by uncached-suffix length.

## What the fleet gateway should do

### Cache-locality affinity (#71)

An `affinity` block on `ModelService`, routing to a cluster and delegating the
replica pick to that cluster's EPP:

```yaml
spec:
  endpoints:
  - selector:
      matchLabels:
        modelplane.ai/deployment: kimi-k2
  affinity:
    type: Session             # Session | ClientIP | None
    sessionHeader: X-Session-ID
```

- **Session.** Route by a session header, so a client that carries a conversation
  ID reaches the same cluster each turn. This covers the multi-turn case #71 is
  about.
- **ClientIP.** Route by client source IP. No header needed, but coarse: clients
  behind one egress IP share a cluster, and affinity is lost when an IP changes.

Both key on the client's identity and pin a conversation to a cluster, which is
what multi-turn locality needs. Routing by prompt content instead, to co-locate
different sessions that share a prefix, is a possible later mode (see open
questions).

### Criticality and weighting (#8)

Request priority (production preempts experimentation under load) and weighted
splitting, expressed through the inference extension rather than the static
HTTPRoute weights we compose today. Path-based multi-tenancy stays: model-name
routing can't tell apart two teams that deploy the same model, so the
`/<ns>/<deployment>/` prefix remains the tenant boundary.

### Route to a cluster, not a replica

The fleet gateway forwards to a cluster. The cluster's EPP (or Dynamo, per #65)
makes the warm-cache replica pick with real cache-state visibility. The fleet
doesn't tokenize, track KV state across clusters, or move KV over the WAN. It
moves the request and lets the cluster serve it. A cold cluster warms up as
matching prompts reinforce it.

```mermaid
flowchart LR
    C["client\n(multi-turn chat)"]
    subgraph fleet["fleet gateway"]
        SP["route by session key\n(SessionPersistence)"]
    end
    subgraph ca["cluster A"]
        EA["EPP\nprefix + queue"]
        RA["warm replica"]
    end
    subgraph cb["cluster B"]
        EB["EPP"]
    end
    C --> SP
    SP -->|"same session to same cluster"| EA --> RA
    SP -.->|other sessions| EB
    classDef new fill:#ffb74d,stroke:#e65100,stroke-width:3px,color:#000;
    class SP new
```

## Compose to the standard, not to a vendor

One rule keeps this from fragmenting. Modelplane composes standard resources
and requires the gateway to conform, rather than writing a routing codepath per
gateway. Most of what the fleet gateway should do is already standard.

- **Session and ClientIP affinity** are Gateway API `SessionPersistence`
  (GEP-1619). The standard covers both header-based and cookie-based persistence,
  so the Session mode above is a standard resource, not a vendor extension.
- **Criticality and inference-aware weighting** are the Gateway API Inference
  Extension (GAIE): `InferencePool` and `InferenceModel`. GAIE is the emerging
  multi-vendor standard for inference routing, implemented by GKE Inference
  Gateway, Istio, kgateway, Envoy Gateway, and Kong.

So the composition target is Gateway API core plus `SessionPersistence` plus GAIE.
Any gateway conformant to that profile works with one codepath, and the
`InferenceGateway` `backend` discriminator selects among conformant gateways
rather than among bespoke codepaths.

The standard is a baseline, not a ceiling. The `backend` discriminator, the
per-cluster EPP's configurable policy (#179), and the gateway's ext_proc chain are
seams where extra routing logic can attach without forking the composition.
The baseline stays standard and portable, and richer behavior layers on top
through those seams rather than living in core.

One thing falls outside the standard today: **path aggregation.** Each
`ModelEndpoint` carries a per-replica rewrite path, so the per-backendRef
`URLRewrite` that pins us to Traefik (#89) fires for any multi-endpoint
ModelService, not only external-provider mixes. For a self-hosted fleet, which is
the inference-aware target, two changes remove the need for it:

1. Serve a deployment's replicas at one deployment-scoped path, so their endpoints
   share a single route-level rewrite.
2. Weight canaries through GAIE `InferenceModel` rather than per-backendRef
   weights.

The remaining case is folding an external provider into the same weighted split,
where the provider's `/openai/v1/` differs from the self-hosted `/v1/`.
Normalizing that off the gateway would take a **per-provider adapter**: a small
proxy that presents the canonical path and injects the provider's API key. That is
more than a path rewrite, because Modelplane doesn't inject provider auth at all
today (an endpoint just points at the FQDN), so the key has to come from a Secret
rather than sit in a CR. **This document does not propose building it.** The
adapter belongs with a provider-auth design, not with fleet routing. Until it
exists, heterogeneous-path provider aggregation stays a Traefik-backend concern,
and the standard, inference-aware backend serves self-hosted fleets.

No single gateway meets the whole profile today. Envoy Gateway is closest (GAIE
and `SessionPersistence`) once the self-hosted normalization above is in place.
Traefik keeps provider aggregation, but not the profile. As the standard matures,
more gateways qualify with no new Modelplane code. That is the point of composing
to the standard: the supported set grows without the design diverging.

## Validation

Every gateway behavior this design relies on has to be proven on the real gateway
before we commit to it. Where each stands:

- **Per-backendRef `URLRewrite` on Traefik.** Proven. It is in use today (#89).
- **`SessionPersistence` (Session affinity).** Traefik's Gateway-API provider
  doesn't support it (traefik#11243), so Session affinity does not work on the
  backend we run. It needs a gateway that supports GEP-1619 (the Envoy Gateway
  family), where it's still in the experimental channel. Unvalidated.
- **GAIE (criticality, weighting).** Traefik doesn't support it. Needs a GAIE
  gateway, tested against our composed `InferencePool` and `InferenceModel`.
  Unvalidated.
- **Client-IP affinity on Traefik.** Its HRW strategy is set through the
  `TraefikService` CRD, not standard HTTPRoute, so even the interim ClientIP
  option is Traefik-specific. Unvalidated.
- **Deployment-scoped path normalization.** Serving a deployment's replicas at one
  path changes the two-gateway path contract, so it needs an end-to-end test:
  fleet route, per-cluster match, pod mount.

The consequence is blunt: the inference-aware features do not run on the Traefik
backend we run. They require the standard, GAIE-capable backend (Envoy Gateway)
with path aggregation normalized first, and that backend has to be stood up and
tested before this design is committed.

## Alternatives considered

### A codepath per gateway backend

The tempting shape is a Traefik backend and an Envoy backend, each composing that
gateway's own resources. It fragments fast: every routing feature has to be built
and tested against each backend, and the backends drift. Composing to the standard
keeps one codepath and pushes the variation into a conformance question.

### Require only base Gateway-API conformance

Too weak to promise. Base conformance guarantees neither `SessionPersistence`
(experimental channel) nor GAIE, so a conformant gateway may still be unable to run
this routing. The profile has to name `SessionPersistence` and GAIE.

### Lean on Traefik's own features

Staying on Traefik is correct for provider aggregation, and its HRW strategy
(client IP) and cookie stickiness (`TraefikService` CRD) even give coarse
affinity. But those are coarse and partly outside Gateway API, and leaning on them
forecloses the inference-aware routing this document is about. Only a single
feature pins us to Traefik, so the cost of staying exceeds the cost of composing
to the standard. Client-IP affinity is worth keeping as an interim on the Traefik
backend.

### Replica-level routing from the fleet

The fleet would guess without per-replica cache-state visibility and duplicate what
the EPP and Dynamo already do inside a cluster. This is #71's own boundary.

## Open questions

- **Content-based affinity.** Routing by prompt hash (#71's PrefixHash) would
  co-locate different sessions that share a prefix. It attaches at the ext_proc
  seam rather than core, and overlaps the per-cluster EPP, which already routes by
  prefix inside a cluster. Worth adding only if a shared-prefix workload shows the
  cross-cluster consistency beats letting each cluster warm its own copy.
- **Backend selection.** Chosen per `InferenceGateway` only, or can a deployment
  request inference-aware routing and let the platform resolve the backend?
- **Hot key.** A heavily shared session pins to one cluster while peers idle. Some
  gateways offer a hybrid consistent-hash-then-least-loaded policy. Expose it or
  default it.

## Interaction with related issues

- **#89 (Traefik rationale):** the per-backendRef rewrite that pins the current
  backend, and the feature this design proposes relocating.
- **#179 (EPP config):** the per-cluster picker whose replica decision the fleet
  delegates to. Making its config user-overridable turns it into an extension seam
  for custom routing policy, complementary to this design.
- **#68 (scheduler) and #70 (capacity signal):** companions from #71, same
  delegation pattern.
- **#225 (canary docs):** weighted splitting documented there moves to GAIE.
</content>
