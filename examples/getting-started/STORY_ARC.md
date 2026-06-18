# Getting-started story arc

The narrative for the getting-started guide (and the demo video). Each stage is
motivated by outgrowing the previous one and adds exactly one capability. Two
through-lines run the whole way: **platform vs. ML-team separation** ("platform
offers hardware as classes/clusters; the ML team declares what its model needs"),
and **"declare intent, Modelplane composes the rest."**

**Spine, in one line:** *serve a model (one cluster) → run it on the right
hardware everywhere (capability scheduling across the fleet) → serve it in
production (route traffic and roll out new versions safely).*

> The runnable demo (`stage0`/`stage1`/`stage2` manifests + `record.sh`) uses
> **A100-40GB** as the "big" GPU because the demo project has no A100-80/H100
> quota; the narrative below keeps the higher-end framing. The capability story is
> identical either way — only the GPU and the `memory >=` threshold differ.

---

## Stage 0 — Get started: serve one model (single cluster)

**Scenario:** "I have a model and a GPU cluster. Get it serving behind an OpenAI
endpoint."

**Setup:** one `InferenceCluster` (a modest "starter" GPU — L4), one
`InferenceClass`, one `ModelDeployment`, one `ModelService`.

**Teaches:** the core object graph + the platform/ML split; the basic CEL selector
framed simply — *"ask for the GPU you need"* (`memory >= 20Gi`). Here the CEL
looks modest on purpose; it's a single homogeneous cluster.

**Payoff:** `curl` the endpoint, get a completion. Done in minutes on one cheap
GPU.

---

## Stage 1 — Scale to the fleet: schedule by capability (multi-cluster)

**Scenario:** "This model outgrew the cheap L4 box — take it to our real workload
clusters." The platform has since added bigger GPU clusters (two, in different
regions) next to the L4.

**The turn:** instead of hand-picking with a region label, the ML team states the
hardware its model needs, and the DRA scheduler finds it fleet-wide:

```
- cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("70Gi")) >= 0
```

→ lands on the two "expensive" clusters across regions, skips the L4 — no
`clusterSelector`. New big-GPU capacity in a third region tomorrow is eligible
automatically.

**Teaches:** heterogeneous fleet (multiple classes/clusters), capability-based
selection as the real value of CEL, and that `region`/labels are an *orthogonal*
concern (data residency), not the hardware discriminator. Natural place to also
introduce multi-node gangs + `ModelCache` (a model too big for one node).

**Also note (one line):** because one `ModelService` already fronts the replicas
across both regions, this is your **HA** posture too — one endpoint, two regions,
lose one and keep serving.

**Payoff:** *"the ML team asks for the hardware its model needs and Modelplane
finds it"* — the DRA scheduler story.

---

## Stage 2 — Serve it in production: roll out new versions safely

**Scenario:** "It's running on the right hardware — now serve it well: ship a new
model version without downtime or risk."

**Routing / blue-green upgrade with `ModelService`** — one front door over
multiple deployments (the `endpoints[]` list); traffic follows replica capacity.
Ship a new model version (`v2`) behind the **same endpoint**, shift traffic by
scaling `v2` up and `v1` down, then retire `v1` — or roll back instantly by
deleting it. Clients never change a line; no traffic weights, no new gateway, the
address never moves.

**Payoff:** production-grade serving — and a template for adopting *any* advanced
technique as a deployment-level change.

### Out of scope (for this arc)

**Prefill/decode disaggregation** — selecting clusters by interconnect/topology
and composing the disaggregated serving path — is a more advanced technique built
on the same primitives, but it is **not part of this getting-started arc**. It's
tracked separately (see the disaggregation PR/demo) and is post-v0.1.

---

The runnable backing for Stages 0–2 lives next to this file: see `README.md` for
the manifests, `record.sh` (the self-playing screencast), and the pre-flight.
