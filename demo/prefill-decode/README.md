# Deploying advanced serving techniques

*Latency-sensitive LLM workloads live and die by two numbers — TTFT and ITL — and
the techniques that protect them are powerful but operationally heavy to deploy,
evaluate, and roll out. This is how Modelplane turns adopting one into a
deployment-level workflow, walked end to end with prefill/decode disaggregation
and a runnable Qwen example.*

> Grounded in a live run on GKE L4 GPUs. We use **Qwen2.5** as a small, concrete
> stand-in you can actually run, but every step is identical for the frontier
> model you serve in production. Benchmark numbers are marked where they're still
> placeholders pending the final hardware run.

---

## The problem: latency under load

If you serve an LLM to users, two latencies define the experience:

- **Time-to-first-token (TTFT)** — how long before the response *starts*. It's the
  prompt-processing cost, and it dominates perceived responsiveness — worst for
  long prompts (RAG, long context, agents).
- **Inter-token latency (ITL)** — the gap between streamed tokens once it's going.
  It's the *smoothness*; a strict ITL target is what keeps a stream from
  stuttering.

Both are easy to hit at low load and hard to hold under it. As concurrency climbs,
requests contend for the same GPU and the **tail** — p95/p99 TTFT and ITL — blows
out long before average throughput looks bad. For latency-sensitive workloads that
tail *is* the SLO, and it's where naive scaling stops helping: adding replicas just
adds more servers each still doing two conflicting jobs at once.

## Advanced serving techniques — powerful, but a pain to ship

So teams reach for techniques that attack the tail directly: **prefill/decode
disaggregation**, speculative decoding, chunked prefill, prefix-cache-aware
routing. They work — but each usually means new components (routers, sidecars,
KV-transfer fabrics), careful wiring, and a risky production rollout. That
operational cost is why a lot of teams never get past "we should try that."

Modelplane's bet: adopting one of these should be a *deployment-level* change, not
a quarter-long project — declare it, evaluate it on your own traffic, and promote
it behind a stable endpoint, reversibly. The rest of this guide walks exactly that,
end to end, with the most impactful of the bunch: **prefill/decode disaggregation**.

## The technique: prefill/decode disaggregation

Every LLM request runs in two phases, and they could hardly be more different.

**Prefill** ingests the prompt. It runs the whole prompt through the model in a
single parallel pass to build the KV cache and emit the first token. It's
**compute-bound** — it saturates the GPU's math units. It's also bursty: a 4,000-
token prompt is one big expensive event. Prefill is what your **time-to-first-
token (TTFT)** is made of.

**Decode** generates the response, one token at a time, each step attending over
the KV cache built so far. It's **memory-bandwidth-bound** — light on math, heavy
on moving the KV cache through the GPU. It's steady: hundreds of small steps.
Decode is what your **inter-token latency (ITL)** — the smoothness of the stream —
is made of.

Now put both on the same GPU, which is what a normal ("unified") server does.
They fight. A big prefill grabs the compute units and **stalls every in-flight
decode** sharing that GPU — so one user's long prompt makes everyone else's tokens
stutter. Under load this shows up exactly where it hurts: **p99 inter-token
latency spikes**, even though average throughput looks fine.

**Disaggregation** ends the fight. Prefill runs on its own workers, decode on its
own, and the KV cache is shipped from prefill to decode over a fast transport
(here, **NIXL**, typically over RDMA). The payoffs:

- **No more interference.** A prefill burst can't stall decode — they're not on
  the same GPU. Tail ITL holds under load.
- **Independent scaling.** Prompt-heavy traffic? Add prefill workers. Long
  generations? Add decode. You stop sizing one pool for two opposite jobs.
- **Phase-appropriate tuning.** Each side can use different parallelism, batching,
  even different hardware.

The catch — and the reason this is a *decision*, not a default — is that KV-cache
hop. Moving the cache between workers costs latency and bandwidth. For a short
prompt at low load, that hop is pure overhead and a unified server wins. The
technique pays off as **prompts get longer, concurrency rises, and your ITL SLO
gets stricter**. So the only honest way to know is to measure it on *your*
traffic. That's what this guide does.

> **Mental model:** unified serving optimizes average throughput on cheap
> hardware; disaggregation buys you a **predictable tail** under load, at the cost
> of a KV-transfer hop. You're trading a little efficiency for a lot of
> consistency — worth it exactly when consistency is the thing you're short on.

---

## The scenario

You serve a model in production on Modelplane — a `ModelDeployment` became a few
vLLM replicas behind one endpoint, each handling whole requests. It works, but
tail latency is creeping up as traffic grows, and you want to know:

> *Is disaggregation worth turning on for our workload — and how do we roll it out
> without risking the production endpoint?*

We'll stand up a disaggregated variant on spare capacity, prove it's genuinely
disaggregating, benchmark it against a replay of real traffic, and promote it to
production by shifting capacity — reversible at every step. Nothing here leaves
the ML team's lane: the platform team owns clusters, GPU pools, and the gateway,
and does **nothing** disaggregation-specific. Disaggregation is a field on your
deployment.

Everything runs from the repo root:

```bash
demo/prefill-decode/run.sh deploy     # Step 1: baseline + P/D canary
demo/prefill-decode/run.sh prove      # Step 2: show KV actually moving
demo/prefill-decode/run.sh bench      # Step 3: replay benchmark, side by side
demo/prefill-decode/run.sh promote    # Step 4: shift capacity to P/D
demo/prefill-decode/run.sh rollback   # undo, any time
```

---

## Step 0 — the baseline you already have

Your production deployment, unified, plus the `ModelService` your apps call. One
detail to notice now, because it's the whole rollout mechanism later: **a
`ModelService` load-balances evenly across every healthy replica it matches, and
there's one endpoint per replica — so traffic follows replica count.**

<details>
<summary><code>qwen-unified.yaml</code> + <code>qwen-service.yaml</code> — what's running today</summary>

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: qwen-unified
  namespace: ml-team
  labels:
    model: qwen          # shared label; the ModelService selects on this
spec:
  replicas: 2
  modelCacheRef:
    name: qwen           # weights staged once on RWX storage; pods start fast
  serving:
    mode: Unified
  engines:
  - name: server
    members:
    - role: Standalone
      nodeSelector:
        devices:
        - name: gpu
          count: 1
          selectors:
          - cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("20Gi")) >= 0
      template:
        spec:
          containers:
          - name: engine
            image: docker.io/vllm/vllm-openai:v0.19.1-x86_64-cu130
            args: [/mnt/models, --served-model-name=qwen, --max-model-len=10240]
---
apiVersion: modelplane.ai/v1alpha1
kind: ModelService
metadata:
  name: qwen
  namespace: ml-team
spec:
  endpoints:
  - selector:
      matchLabels:
        model: qwen      # matches every deployment labelled model: qwen
```

</details>

---

## Step 1 — stand up a disaggregated variant

You don't touch production. You apply a **second**, independent deployment,
`qwen-pd`, on the cluster's spare GPU capacity, and turn on disaggregation. It
carries the same `model: qwen` label, so the moment it's healthy it sits behind
the shared endpoint as a **small (~1/3) canary** — production's two replicas keep
serving the rest. (Deployments require at least one replica, so "off" later means
*removing it from rotation*, not scaling to zero — see Step 4.) The diff from
unified is tiny: a serving mode, and the single engine becomes a prefill/decode
pair.

<details>
<summary><code>qwen-pd.yaml</code> — the disaggregated variant</summary>

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: qwen-pd
  namespace: ml-team
  labels:
    model: qwen          # same shared label — behind the same front door
spec:
  replicas: 1            # a small canary slice; production keeps serving the rest
  modelCacheRef:
    name: qwen           # same staged weights as unified
  serving:
    mode: PrefillDecode  # the one real change
  engines:
  - name: prefill
    phase: Prefill
    copies: 1
    members:
    - role: Standalone
      nodeSelector:
        devices:
        - name: gpu
          count: 1
          selectors:
          - cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("20Gi")) >= 0
      template:
        spec:
          containers:
          - name: engine
            image: docker.io/vllm/vllm-openai:v0.19.1-x86_64-cu130
            args:
            - /mnt/models
            - --served-model-name=qwen
            - --max-model-len=10240
            - --block-size=128
            - '--kv-transfer-config={"kv_connector":"NixlConnector","kv_role":"kv_producer"}'
  - name: decode
    phase: Decode
    copies: 1
    members:
    - role: Standalone
      nodeSelector:
        devices:
        - name: gpu
          count: 1
          selectors:
          - cel: device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("20Gi")) >= 0
      template:
        spec:
          containers:
          - name: engine
            image: docker.io/vllm/vllm-openai:v0.19.1-x86_64-cu130
            args:
            - /mnt/models
            - --served-model-name=qwen
            - --port=8001        # decode listens here; the sidecar fronts :8000
            - --max-model-len=10240
            - --block-size=128
            - '--kv-transfer-config={"kv_connector":"NixlConnector","kv_role":"kv_consumer"}'
```

</details>

```bash
demo/prefill-decode/run.sh deploy        # cluster, cache, both deployments, shared ModelService
```

**What you wrote vs. what Modelplane built.** You wrote model-and-serving
vocabulary: a serving mode, a prefill engine and a decode engine, the
`NixlConnector` KV roles, and a CEL selector for the GPU each needs. From that,
Modelplane composed the parts that are genuinely fiddly to get right by hand:

- the **endpoint picker** (llm-d inference scheduler) with the prefill/decode
  routing config armed correctly,
- the **InferencePool** and the **routing sidecar** on the decode pod,
- the **KV-transfer plumbing** the engine needs — the NIXL side-channel address
  (each engine advertises its own pod IP) and a shared-memory volume.

Get any one of those wrong by hand and you get silent decode-only serving or 500s
with nothing in the logs. That's the work Modelplane is absorbing.

> **Practical tip — the engine image.** Disaggregation needs the NIXL runtime in
> the image; recent vanilla `vllm/vllm-openai` tags ship it, so pin a current one.
> Keep `--block-size` identical on prefill and decode (the picker derives its
> prefix-cache block size from it), and give the decode engine `--port=8001` — the
> routing sidecar takes the public `:8000` and forwards to the engine.

---

## Step 2 — prove it's actually disaggregating

Before trusting a single latency number, confirm the mechanism: KV cache really
moving from prefill to decode. vLLM exposes this in its own metrics, so you don't
have to take anyone's word for it.

```bash
demo/prefill-decode/run.sh prove         # sends long prompts, reads the engines' NIXL counters
```

From a live run:

```
PREFILL  (kv_producer) — does the prompt work, generates ~nothing:
  vllm:prompt_tokens_total      6825
  vllm:generation_tokens_total  5

DECODE   (kv_consumer) — pulls KV over NIXL, then generates:
  vllm:nixl_xfer_time_seconds_count   5         # one transfer per request
  vllm:nixl_bytes_transferred_sum     ~403 MB   # KV cache moved across the wire
  vllm:generation_tokens_total        85
```

Prefill processes the prompt and hands off; decode pulls the KV and produces the
tokens. Disaggregation, measured — not asserted.

> **Practical tip — selectivity is a feature.** Short prompts skip the prefill hop
> entirely and serve decode-only; the picker only disaggregates when there's
> enough *uncached* prefill to be worth the transfer. So you don't pay the hop on
> trivial requests. You'll see this directly: tiny prompts leave the prefill
> counters flat.

---

## Step 3 — benchmark a replay of your traffic

A synthetic "1024-in / 1024-out" sweep describes a regime, not your users. So
benchmark a **replay of production traffic** — here a simulated trace with a
realistic spread of prompt and output lengths; in production you point the same
harness at your own request logs.

```bash
demo/prefill-decode/run.sh bench         # replays the trace against unified and P/D, rising concurrency
```

Read it like an engineer, not a marketer:

- Watch **TTFT** (prefill) and **p99 ITL** (decode) as concurrency climbs, unified
  vs. disaggregated.
- Expect a **crossover**: at low load unified ties or wins (the hop is overhead);
  as concurrency and prompt length grow, disaggregation keeps the decode tail flat
  while unified's p99 ITL climbs.

> *‹FILL FROM THE REAL RUN: the crossover point — "above ~N concurrent, p99 ITL is
> X ms unified vs. Y ms disaggregated" — and throughput parity. If the demo
> hardware doesn't reach the crossover, say so and name the regime where it
> would.›*

> **Practical tip — reading the result.** The question isn't "which is faster on
> average" (often a wash); it's "where does the **tail** diverge, and is that
> region where my SLO lives." If your p99 ITL budget bites at the concurrency you
> actually run, disaggregation is buying you headroom. If you're nowhere near it,
> it isn't — and that's a perfectly good answer to have gotten cheaply.

---

## Step 4 — promote by moving capacity

You've decided it earns its place for your latency-sensitive traffic. Take it live
the way you'd promote any variant — **shift capacity and let traffic follow** —
not with a big-bang switch. Both deployments already share the `model: qwen`
label, so the existing `qwen` `ModelService` fronts both; the split is just the
replica ratio.

**Canary.** `qwen-pd` is at 1 replica from Step 1, unified at 2 — so the moment
P/D is healthy behind the service, it's already taking **~1/3** of traffic. Watch
it on real requests as long as you like.

**Promote.** Move capacity from old to new; traffic tracks it:

```bash
demo/prefill-decode/run.sh promote       # unified 2->1, qwen-pd 1->2  (now ~2/3 on P/D)
```

The cluster autoscales up for the second P/D replica as unified scales down.
Capacity is *moving* from the old deployment to the new one.

**Cut over** when you trust it — retire the old deployment so P/D carries 100%,
behind the same endpoint your apps have always called. They never noticed.
(`ModelDeployment` replicas can't go to 0, so "100% one variant" means taking the
other out of the shared `ModelService` — here, deleting it.)

```bash
kubectl -n ml-team delete modeldeployment qwen-unified   # 100% P/D
```

**Roll back at any point** — pull P/D out of rotation and traffic snaps back to
unified (re-`apply` `qwen-pd` to bring it back):

```bash
demo/prefill-decode/run.sh rollback      # delete qwen-pd, restore unified to 2
```

The production endpoint never moves through any of this — you only add, resize,
or remove deployments behind it.

---

## What actually made this easy

- **Separation of concerns.** The platform team did nothing P/D-specific — you
  added a deployment and scaled replicas. Neither side blocked the other.
- **An API shaped for the real scenario.** Trying an advanced technique was one
  field plus a phase split; Modelplane composed the picker, pool, sidecar, and KV
  wiring, and got the error-prone bits right.
- **A promotion model that's just capacity.** The shared `ModelService` balances
  evenly across healthy endpoints, so the canary split is the replica ratio, and
  promote / cut over / roll back are scaling a deployment or adding/removing it
  from the endpoint — no traffic weights to hand-tune, no platform ticket.

Cheap to experiment, safe to roll out — which is what lets a team actually *try*
something like disaggregation and ship it.

---

## Appendix

**Prerequisites.** A Modelplane control plane (Crossplane + GCP provider family,
healthy Configuration); one Modelplane GKE cluster with a GPU node pool sized for
the baseline plus the experiment (≈4–6 L4 lets you run both and promote); a
`ModelCache` staging the weights; local `kubectl`, `python3`, `uvx`.

**The runner.** `demo/prefill-decode/run.sh {deploy|prove|bench|promote|rollback}`
— each subcommand is a thin, readable wrapper over `kubectl` and GuideLLM; read it
to see exactly what it does.

**The recipe, in one place.** Recent vanilla `vllm/vllm-openai` image (ships
NIXL) · prefill `kv_producer`, decode `kv_consumer` + `--port=8001` · identical
`--block-size` on both · everything else (side-channel env, `/dev/shm`, picker
config) composed by Modelplane.
