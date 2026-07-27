# Metrics collection

**Status:** Draft
**Date:** July 2026
**Author:** Dennis Ramdass

This document proposes managed metrics collection inside each `InferenceCluster`, with
a discoverable Prometheus URL on the cluster's status so the platform team can consume
it. Modelplane composes a `PodMonitor` for each source it owns on every cluster, always
on, and the cluster's own Prometheus scrapes them. Cross-cluster aggregation stays with
the platform team. It builds on [design.md](./design.md) and addresses
[#269](https://github.com/modelplaneai/modelplane/issues/269).

## What to monitor

There are four things worth watching in a Modelplane deployment.

1. **Inference signal (data plane).** vLLM's `/metrics`, the EPP's `llm_d_epp_*`, and
   Envoy. TTFT, tokens per second, queue depth, KV-cache occupancy, and request and
   error rates per model. It answers "is my model serving well, and is it saturated?"
2. **Substrate health.** The stack Modelplane installs on each workload cluster: is the
   gateway up, are cert-manager, the LeaderWorkerSet controller, and the NVIDIA DRA
   driver healthy, are GPUs allocatable. "Is the machinery on this cluster working?"
3. **Control-plane health.** Modelplane itself. Crossplane reconcile rates and errors,
   function latency and panics, the fleet scheduler placing replicas, and XR
   `Ready`/`Synced`. "Is the thing I operate working?"
4. **Fleet roll-up.** Across every cluster and deployment: total capacity, GPU usage,
   how many deployments are degraded, and cost.

This proposal covers (1) and (2) inside each cluster and makes the data discoverable.
An MD author cares about (1), and the platform team owns the rest. A per-deployment
opt-in would cover only (1), and even then the platform team has to plumb the metrics
somewhere the author can read them. So collection is always on inside each cluster,
with no toggle for an MD author to manage. Aggregating across clusters and monitoring
the control plane stay with the platform team, and the end of this document returns to
them.

## Collection: always-on PodMonitors

On each cluster, Modelplane composes a `PodMonitor` for every source it owns, with no
opt-in or opt-out. Three existing pieces make this cheap:

- **The serving label spans every shape.** `modelplane.ai/serving` is on standalone
  pods, LeaderWorkerSet leaders, and both prefill/decode engines (it's the label the
  InferencePool selects on). One selector on it follows the shape, so the leader/worker
  and prefill/decode branching needs no special casing.
- **Prometheus discovers openly.** `compose-serving-stack` runs kube-prometheus-stack
  with `podMonitorSelectorNilUsesHelmValues: false` and an empty namespace selector, so
  a composed `PodMonitor` is scraped with no operator action. It already scrapes the
  gateway's Envoy proxies this way.
- **Modelplane owns the picker.** The EPP is Modelplane's own Deployment, so its
  metrics port and flags are ours to set.

The sources are the engine (per deployment), the endpoint picker (wherever one exists),
and the serving-stack components (per cluster).

### Engine metrics

`compose-model-replica` composes a `PodMonitor` on the workload cluster (the same place
it composes the Service and InferencePool), selecting the replica's serving pods by
`modelplane.ai/serving`:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: <replica>-metrics
  namespace: default
spec:
  selector:
    matchLabels:
      modelplane.ai/serving: <replica-name>
  podMetricsEndpoints:
  - port: http
    path: /metrics
    interval: 30s
```

It has no status of its own, so it uses default readiness (Ready once synced), like the
Service. No `mrap.yaml` change is needed: it's a provider-kubernetes `Object`, already
activated, and the CRD is installed by the Prometheus stack.

### Naming the port

The engine's serving port carries `/metrics`, so the backends give it a name (for
example `http`) in `native.py`, `llmd.py`, and the decode engine in `routing.py`. The
pd-sidecar's port stays unnamed. The `PodMonitor` then scrapes that port by name, which
matters for prefill/decode: the decode engine serves on 8001 because the pd-sidecar
takes 8000, so a `PodMonitor` matching port 8000 by number scrapes the sidecar on decode
pods, not the engine. Referencing the port by name scrapes the engine on every pod
regardless of its number. Naming is additive; the Service and probes that reference the
port by number are unaffected.

### Endpoint picker metrics

Multi-pod Unified and prefill/decode serving front the engines with an endpoint picker
(EPP) that Modelplane composes, today `llm-d-router-endpoint-picker:v0.9.0`. The EPP
makes the routing decisions the engine metrics don't show: endpoint selection,
prefix-cache-aware scoring, and the prefill/decode split. It exposes a rich
`llm_d_epp_*` metric set (request and error totals, TTFT and per-output-token latency,
in-flight requests, pool KV-cache usage, and scheduler and disaggregation timings), the
signal for whether that routing is working.

Because Modelplane owns the EPP Deployment and its args, collecting these is
straightforward. The EPP serves `/metrics` on a port set by `--metrics-port` (default
9090), and its auth is a flag, `--metrics-endpoint-auth`. Modelplane sets
`--metrics-endpoint-auth=false` (the metrics are non-sensitive routing stats reachable
only in-cluster), declares the 9090 port on the Deployment, and composes a plain
`PodMonitor` for it, the same shape as the engine's. It's composed alongside the EPP
objects in `routing.py`, so it exists wherever an EPP does. No ClusterRole, token, or
TLS to manage.

### Serving-stack components

`compose-serving-stack` composes a `PodMonitor` for the substrate it installs: the
gateway's Envoy proxies (already scraped), cert-manager, the LeaderWorkerSet controller,
and the NVIDIA DRA driver. These are per-cluster and outlive any one deployment, so they
belong to the serving stack rather than to a replica.

### What gets scraped

The `PodMonitor` ingests everything a source exposes on `/metrics`. What the engine
emits is the user's, set through engine flags like everything else (vLLM's
`--disable-log-stats` and friends), so Modelplane doesn't decide which metrics exist. It
scrapes what's there.

This scrape feeds Prometheus for dashboards and alerting, so the 30s interval is an
observability default, not a routing input. The EPP scrapes engine `/metrics` on its own
fast internal loop for routing decisions, so routing latency doesn't depend on this
interval. An engine pod exposes on the order of dozens of series (request and latency
histograms, KV-cache, throughput); retention and storage sizing are the cluster
Prometheus's configuration, not the composition of individual monitors.

## Making the per-cluster Prometheus discoverable

Each `InferenceCluster` already runs a Prometheus, installed by `compose-serving-stack`
(kube-prometheus-stack). With the `PodMonitor`s above composed, that Prometheus holds
all of the cluster's Modelplane sources, from the engines and EPPs to the serving-stack
components. Modelplane exposes its in-cluster URL on the `InferenceCluster` status:

```yaml
status:
  metrics:
    prometheusURL: http://prometheus-operated.modelplane-system.svc:9090
```

The platform team reads that URL to scrape or federate the cluster, without reaching
into Modelplane internals to find where the Prometheus runs or what it collects. This is
the line the proposal reaches: collection wired up and discoverable, so monitoring a
cluster's Modelplane workloads needs no knowledge of how they are composed.

## Aggregation, and where this stops

This proposal stops at per-cluster collection and the discoverable URL. Beyond that, the
platform team wires up cross-cluster aggregation and control-plane monitoring itself.
Whether Modelplane should later package a managed central Prometheus for the whole
deployment is out of scope here, and the main open question below.

## Architecture

```mermaid
flowchart LR
    subgraph icA["InferenceCluster A"]
        PMA["PodMonitors\n(engines, EPPs, serving stack)"]
        PRA["cluster Prometheus"]
        STA["status.metrics.prometheusURL"]
    end
    subgraph icB["InferenceCluster B"]
        PRB["cluster Prometheus"]
    end
    PT["platform team\nscrape / aggregate / alert"]
    PMA --> PRA
    PRA --> STA
    STA -.discovered by.-> PT
    PRB -.discovered by.-> PT
    classDef new fill:#ffb74d,stroke:#e65100,stroke-width:3px,color:#000;
    class PMA,PRA,STA new
```

## Alternatives considered

### A per-deployment opt-out field

An earlier shape of this proposal put an `enabled` toggle on the deployment and composed
the engine and picker `PodMonitor`s unless it was false. It covers only the data plane,
and it asks an MD author to opt in or out of collection that the platform team plumbs and
consumes. Always-on collection at every layer fits the ownership better, so the toggle is
dropped.

### A managed central Prometheus in this proposal

Composing one central Prometheus for the whole deployment, federating every cluster and
scraping the control plane, would give an operator a single endpoint. It is more to own
than wiring up what each cluster already runs, so this proposal leaves aggregation to the
platform team and stops at the discoverable URL.

### One cluster-wide PodMonitor from the serving stack

`compose-serving-stack` could install a single `PodMonitor` selecting all serving pods
(`modelplane.ai/serving` Exists). It would have no per-deployment lifecycle: it wouldn't
come and go with a deployment. Composing the engine monitor per replica ties that
collection to the thing it observes, while the serving-stack monitors, which are
per-cluster, are composed once with the stack.

### ServiceMonitor instead of PodMonitor

A `ServiceMonitor` scrapes through a Service, so it needs a Service per scrape target.
The engine and picker pods are the targets, and a `PodMonitor` scrapes them directly,
matching the manual path.

### Authenticate the EPP metrics endpoint

The EPP can serve `/metrics` behind controller-runtime auth (a `ClusterRole` with
`nonResourceURLs: /metrics` plus a bearer token). Since Modelplane owns the EPP args and
the endpoint carries non-sensitive routing stats reachable only in-cluster,
`--metrics-endpoint-auth=false` collects them with a plain `PodMonitor` and nothing to
manage. Auth would add a `ClusterRole` and a bearer token for no gain here.

## Open questions

- **Central aggregation.** Whether Modelplane should package a managed central Prometheus
  for the whole deployment, or leave cross-cluster aggregation and control-plane
  monitoring to the platform team. This proposal leaves it out, and that is the main call
  to make.
- **Discoverable field shape.** The `status.metrics.prometheusURL` name, and whether it
  should also carry a service reference or scrape credentials the platform team needs.
- **Port name.** `http` (the serving port that also serves `/metrics`) versus `metrics`.
  Leaning `http`, since it's the one serving port.

## Interaction with #264

The [#264](https://github.com/modelplaneai/modelplane/issues/264) example documents the
manual path: a hand-written `PodMonitor` plus the operator wiring to consume it. Once
collection is composed and the cluster's Prometheus URL is exposed, that example drops
the hand-written `podmonitor.yaml`.

On upgrade, an existing hand-written `PodMonitor` has to be deleted, or it double-scrapes
the same pods alongside the composed one. This warrants a release note.
