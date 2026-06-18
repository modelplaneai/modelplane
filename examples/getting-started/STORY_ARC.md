# Getting-started story arc

The narrative for the getting-started guide and the demo video. Two parts, each
adding exactly one capability. Two through-lines run the whole way: **platform
vs. ML-team separation** ("platform offers hardware as classes/clusters; the ML
team declares what its model needs"), and **"declare intent, Modelplane composes
the rest."**

**Spine, in one line:** *serve a model (one cluster) → scale it across the fleet
on the right hardware everywhere (capability scheduling), without changing the
model or the endpoint.*

> The runnable demo uses one small model (`Qwen2.5-0.5B-Instruct`) the whole way
> and **edits the same deployment in place** between parts — the cleanest way to
> show that capability scheduling is a one-field change, not a rebuild. The model
> is deliberately tiny so the selector is about *mechanism*, not model size; size
> the threshold to your real model.
>
> Two matching tracks: **GKE** (single-GPU A100-40, `>= 35Gi`) and **EKS**
> (single-GPU L40S, `>= 40Gi`). Both avoid 8-GPU H100 boxes, which neither cloud
> offers as a single GPU and which would dwarf a getting-started budget.

---

## Part 1 — Get started: serve one model (single cluster)

**Scenario:** "I have a model and a GPU cluster. Get it serving behind an OpenAI
endpoint."

**Setup:** one `InferenceClass`, one `InferenceCluster` (a modest "starter" L4),
one `ModelDeployment`, one `ModelService`.

**Teaches:** the core object graph + the platform/ML split; the CEL selector
framed simply — *"ask for the GPU you need"* (`memory >= 20Gi`). The selector
looks modest on purpose; it's a single homogeneous cluster.

**Payoff:** `curl` the endpoint, get a completion. Done in minutes on one cheap
GPU.

---

## Part 2 — Scale to the fleet: schedule by capability (multi-cluster)

**Scenario:** "Traffic grew — take it to our real workload clusters." The
platform has added two bigger-GPU clusters in different regions next to the L4.

**The turn:** the ML team doesn't pick a cluster or swap the model. It edits the
**same** `qwen-demo` deployment in place — more replicas, and a selector that
asks for more GPU memory:

```
- cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("35Gi")) >= 0
```

→ the L4 (24 GB) no longer qualifies, so the replicas move to the two bigger
clusters across regions — no `clusterSelector`, no cluster names. New big-GPU
capacity in a third region tomorrow is eligible automatically.

**Teaches:** heterogeneous fleet (multiple classes/clusters), capability-based
selection as the real value of CEL, and that `region`/labels are an *orthogonal*
concern (data residency), not the hardware discriminator.

**Also note (one line):** because the same `ModelService` already fronts the
replicas across both regions, this is your **HA** posture too — one endpoint, two
regions, lose one and keep serving.

**Payoff:** *"the ML team asks for the hardware its model needs and Modelplane
finds it"* — and the endpoint URL never changed. The Part-1 `curl` still works,
now served from the bigger clusters.

### Out of scope (for this arc)

**Routing / A/B of serving configs** behind one `ModelService`, and
**prefill/decode disaggregation** (selecting clusters by interconnect and
composing the disaggregated path) are more advanced techniques built on the same
primitives. They are **not part of this getting-started arc** and are tracked
separately.

---

The runnable backing lives next to this file: `gke/` and `eks/` hold the
matching manifests, the GKE walkthrough docs, and `gke/record.sh` (the
self-playing screencast). See `README.md` for the recording flow and pre-flight.
