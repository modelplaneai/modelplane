# Advanced

> Five common scenarios end-users will hit, expressed as **deltas from the [Quickstart](./quickstart.md)**. Each is the smallest YAML change that unlocks the capability — multi-region routing, BYOC with KAI, P/D disaggregation, custom hardware, SaaS spillover.
>
> See [scheduling.md](./scheduling.md) for what the scheduler does for each scenario; [design.md](./design.md) for the architectural decisions behind these shapes.
>
> YAML is **abridged** here — the full field-by-field schemas are in [#64](https://github.com/modelplaneai/modelplane/pull/64).

## A. Multi-region weighted routing

Two clusters, two `ModelDeployment`s with shared labels, one `ModelEndpoint` weighted across them. Selector-based routes — bumping an MD revision keeps the URL stable (the environment-promotion pattern).

```yaml
# Two MDs share labels — only `region` differs
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: kimi-k2-us
  namespace: app-team
  labels:
    modelplane.ai/model: kimi-k2
    modelplane.ai/region: us-east-1
    modelplane.ai/environment: production
spec:
  replicas: 2
  model: { name: moonshotai/Kimi-K2-Instruct }
  source: HuggingFace
  huggingFace: { repo: moonshotai/Kimi-K2-Instruct }
  clusterSelector:
    matchAttributes:
      cloud.region: us-east-1
      network.bandwidthGbps: ">=400"
  deviceSelector:
    requests:
      - name: gpus
        count: 16
        perNode: 8
        matchAttributes: { vramGiB: ">=141", capabilities: [fp8] }
  parallelism: { tensor: 8, pipeline: 2 }
  engine: { name: vLLM, image: vllm/vllm-openai:v0.8.0 }
---
# kimi-k2-eu: same shape, region: eu-west-1
---
apiVersion: modelplane.ai/v1alpha1
kind: ModelEndpoint
metadata: { name: kimi-k2-global, namespace: app-team }
spec:
  routes:
    - type: Deployment
      weight: 50
      deployment:
        selector:
          matchLabels:
            modelplane.ai/model: kimi-k2
            modelplane.ai/region: us-east-1
            modelplane.ai/environment: production
    - type: Deployment
      weight: 50
      deployment:
        selector:
          matchLabels: { modelplane.ai/model: kimi-k2, modelplane.ai/region: eu-west-1 }
```

**What's interesting:** routing by *label selector*, not `ref`. Push a new revision of the MD with the same labels; the ME keeps routing without edits. Region affinity for end users (EU traffic to EU MD) is a gateway concern, not the matcher's. Cross-cluster replica spread on each MD is automatic — see [scheduling.md > Multi-replica autoscaling](./scheduling.md#e-multi-replica-autoscaling--keda--composer--matcher-loop).

## B. BYOC with KAI scheduler

Operator points Modelplane at an existing CoreWeave H200 cluster running KAI. **No install** — Modelplane detects KAI's `Project` CRD and uses it.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceCluster
metadata: { name: cw-kai-h200 }
spec:
  cluster:
    source: Existing
    existing:
      secretRef: { namespace: platform-system, name: cw-kubeconfig, key: kubeconfig }
  scheduler: { type: auto }            # auto detects Project CRD → kai
  backend:   { type: kserve, version: v0.18.0 }   # detected, BYO
  provisioning: { mode: dra }
  attributes:
    cloud.provider: coreweave
    cloud.region: us-east-1
    network.fabric: ib
  nodePools:
    - { name: kai-pool-h200, class: h200-nvl-8x }
```

Inspect detection:

```bash
$ kubectl describe inferencecluster cw-kai-h200 | grep -A4 Detected
Status:
  Detected:
    Scheduler:     kai
    Backend:       kserve@v0.18.0
    DRA:           true
    Capacity:      Queue.status (KAI puller, every 5s)
  Conditions:    Ready=True
```

Same `ModelDeployment` from scenario A would land here unchanged — the matcher doesn't care whether the scheduler was installed or detected. Full BYOC behavior in [scheduling.md > BYOC](./scheduling.md#byoc-how-scheduling-works-on-a-customer-owned-cluster).

## C. Disaggregated prefill / decode (xPyD)

Add `roles.prefill` and `roles.decode` to a `ModelDeployment`. Backend adapter renders separate sub-pod-sets that all land on the **same** cluster (KV cache transfer too expensive over WAN).

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata: { name: llama-405b, namespace: app-team }
spec:
  replicas: 1
  model: { name: meta/Llama-3.1-405B }
  source: HuggingFace
  huggingFace: { repo: meta-llama/Meta-Llama-3.1-405B }
  deviceSelector:
    requests:
      - { name: gpus, count: 8, perNode: 8, matchAttributes: { vramGiB: ">=141" } }
  parallelism: { tensor: 8 }
  roles:
    prefill: { replicas: 5 }            # 5 prefill pods, inherits root selector
    decode:  { replicas: 3 }            # 3 decode pods
  engine:
    name: vLLM
    image: vllm/vllm-openai:v0.8.0
    optimizations: { kvCacheRouting: true }
```

The MD doesn't say anything about NIXL / KV transfer / gang admission — backend adapter handles the wiring. `kubectl get modelreplicas` shows one MR; the cluster shows 8 pods (5 prefill + 3 decode) co-located via the in-cluster scheduler. Walkthrough: [scheduling.md > Disaggregated prefill / decode](./scheduling.md#d-disaggregated-prefill--decode-pd--llama-405b-with-xpyd).

## D. Custom hardware via `InferenceClass`

Bespoke AMD MI325X partition not in the default catalog. Cluster-scoped class declared once; clusters reference it by name.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceClass
metadata: { name: acme-mi325-2x }
spec:
  expands:
    vendor: amd
    product: MI325
    architecture: cdna3
    formFactor: oam
    vramGiB: 256
    capabilities: [fp8, fp4]
    gpuCount: 2
    interconnect.type: infinity-fabric
  aliases: [acme:internal-mi325-2x]
---
apiVersion: modelplane.ai/v1alpha1
kind: InferenceCluster
metadata: { name: acme-mi325 }
spec:
  cluster: { source: Existing, existing: { secretRef: ... } }
  scheduler: { type: auto }
  backend:   { type: managed-kserve, version: v0.18.0 }
  nodePools:
    - { name: mi325-pool, class: acme-mi325-2x }   # references the class
```

A workload requesting `vramGiB >= 200 && capabilities contains fp8` matches without any MD-level changes. Adding new SKUs is one CR, not a code change.

## E. Spillover to a SaaS provider

Local cluster saturates → burst to Together AI. Register an `InferenceProvider` for the SaaS endpoint, add a weighted route on the ME.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceProvider
metadata:
  name: together-prod
  namespace: app-team
  labels: { modelplane.ai/role: spillover }
spec:
  endpoint:
    url: https://api.together.xyz/v1
    auth: { secretRef: { namespace: platform-system, name: together-key, key: api-key } }
---
apiVersion: modelplane.ai/v1alpha1
kind: ModelEndpoint
metadata: { name: kimi-k2-with-burst, namespace: app-team }
spec:
  routes:
    - type: Deployment
      weight: 95
      deployment: { selector: { matchLabels: { modelplane.ai/model: kimi-k2 } } }
    - type: InferenceProvider
      weight: 5
      inferenceProvider: { selector: { matchLabels: { modelplane.ai/role: spillover } } }
```

When the matcher reports the local fleet at capacity (`InferenceCluster.status.capacity` saturated), the gateway shifts traffic to the spillover route. No `ModelDeployment` change. **`InferenceProvider` is never a placement target** — the matcher considers only `InferenceCluster`. SaaS routes flow only through the ME.

## What this tells us about complexity

Reading the YAML for both quickstart + advanced together:

- **3 CRs the user always writes** (`InferenceCluster`, `ModelDeployment`, `ModelEndpoint`). 1 more for SaaS routes (`InferenceProvider`); 1 more for custom hardware (`InferenceClass`).
- **The MD is the only chunky resource** — ~30-50 lines for a typical workload. Most of that is engine config + selectors that are inherent to inference, not Modelplane-specific.
- **Advanced scenarios are *deltas***, not redesigns. Multi-region is "add a label, copy the MD, write the ME". Disagg is "add `roles`". BYOC with KAI is "set `source: Existing`". Spillover is one extra route entry.
- **No user touches**: `ModelReplica`, capacity status, `KServeBackend`, `ScaledObject`, `LLMInferenceService`, `LeaderWorkerSet`, `ResourceClaim`, `ClusterQueue`, `PodGroup`, scheduler / capacity adapters, the matcher. All internal mechanics — see [design.md > IRs](./design.md#what-we-treat-as-ir-and-why-this-matters-for-byo-) and [design.md > Crossplane lifecycle layers](./design.md#crossplane-lifecycle-layers--what-gets-reconciled-where).

If MD spec sprawl is the complexity risk, the mitigation is org-specific Compositions on top — `ApprovedModel`-style abstractions that compress 50 lines of MD into a 5-line claim. Stock Crossplane; lives alongside Modelplane, not inside it.

The places where complexity *can* leak (and what we plan to do about each):

1. **`matchTrace` debugging.** When no IC matches, the user reads `MR.status.matchTrace`. Currently designed as structured per-cluster missing-features + suggestions; we'll iterate based on real misses. See [scheduling.md > Federation-layer scheduling](./scheduling.md#federation-layer-scheduling-what-modelplane-builds).
2. **Cold-start ambiguity.** A `Ready=False` MD could mean "pulling image", "pool scaling from 0", "gang scheduling pending", "engine loading weights". We commit to granular conditions on the MR (`MR.status.conditions[Pulling]`, `[LWSGangPending]`, `[EngineLoading]`) so users see *which* cold-start stage they're in.
3. **`engine.advanced[]` typos.** `acme.com/turbo-mode` vs `acme.com/trubo-mode`. Fuzzy-match suggestions in `matchTrace.suggestions` cover this; users see the typo flagged.

These are the places to invest in UX once the foundation lands.
