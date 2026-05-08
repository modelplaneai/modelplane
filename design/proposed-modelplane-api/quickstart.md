# Quickstart

> Minimum path from "fresh control plane" to "I can curl an LLM" in **4 CRs and one cluster**. Reuses an existing K8s cluster (the managed-install path is the same; one CR field swap).
>
> See also: [advanced.md](./advanced.md) for common scenarios beyond hello-world; [scheduling.md](./scheduling.md) for what's happening under the hood; [design.md](./design.md) for *why* it's shaped this way.

## 0. Install Modelplane

```bash
$ up xpkg install xpkg.upbound.io/modelplaneai/modelplane:v0.1
# (provider-google + KEDA prereq are dependencies; up resolves them)
```

## 1. Register your first cluster

`Existing` source — Modelplane uses an existing kubeconfig. (For a Modelplane-provisioned managed cluster, swap `source: Existing` for `source: GKE` / `EKS` / `AKS` and provide the cloud project / region / cluster name; the rest is identical.)

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceCluster
metadata:
  name: dev
spec:
  cluster:
    source: Existing
    existing:
      secretRef:
        namespace: platform-system
        name: dev-cluster-kubeconfig
        key: kubeconfig
  scheduler: { type: auto }              # detect; greenfield → managed-kueue
  backend:   { type: managed-kserve, version: v0.18.0 }
  attributes:
    cloud.region: us-east-1
    modelplane.ai/tier: dev
  nodePools:
    - { name: l40s, class: l40s-4x }
```

`scheduler.type: auto` lets Modelplane decide based on what's installed in the cluster — see [scheduling.md > In-cluster scheduling](./scheduling.md#in-cluster-scheduling-kai-and-kueue-both-first-class).

## 2. Deploy a model

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: gpt-oss-20b
  namespace: app-team
spec:
  replicas: 1
  model:  { name: openai/gpt-oss-20b }
  source: HuggingFace
  huggingFace: { repo: openai/gpt-oss-20b }
  deviceSelector:
    requests:
      - { name: gpu, count: 1, matchLabels: { nvidia.com/gpu.family: ada } }
  engine: { name: vLLM, image: vllm/vllm-openai:v0.8.0 }
  scaling:
    signal: Concurrency
    concurrency: { minReplicas: 1, maxReplicas: 4, target: 32 }
```

This is the only chunky CR you write — most of it is engine config (`engine`) and selectors (`deviceSelector`) inherent to inference, not Modelplane-specific. The full schema is in [#64](https://github.com/modelplaneai/modelplane/pull/64).

## 3. Route traffic

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelEndpoint
metadata:
  name: gpt-oss
  namespace: app-team
spec:
  routes:
    - type: Deployment
      weight: 100
      deployment: { ref: { name: gpt-oss-20b } }
```

## 4. Curl it

```bash
$ kubectl get modelendpoint gpt-oss -n app-team -o jsonpath='{.status.url}'
https://gpt-oss.app-team.modelplane.example/v1

$ curl https://gpt-oss.app-team.modelplane.example/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"gpt-oss-20b","messages":[{"role":"user","content":"hello"}]}'
```

## Inspect what happened

```bash
$ kubectl get modelreplicas -n app-team
NAME                READY   TARGET   KIND               AGE
gpt-oss-20b-0       True    dev      InferenceCluster   45s

$ kubectl describe modeldeployment gpt-oss-20b -n app-team | grep -A4 Status
Status:
  Conditions:        Ready=True
  Model Replicas:    total=1 ready=1
  Match Trace:       1 cluster considered, 1 eligible
```

A `ModelReplica` is the placement decision: which `(cluster, pool)` your replica landed on. You don't write these — the matcher creates them when you set `spec.replicas` on the `ModelDeployment`. See [scheduling.md > Federation-layer scheduling](./scheduling.md#federation-layer-scheduling-what-modelplane-builds) for what the matcher does.

## What you wrote vs what's running

You authored **4 CRs (~60 lines of YAML)**. Behind them are running:

- a `KEDA ScaledObject` watching gateway concurrency
- a `KServe LLMInferenceService` with a single-pod Deployment under it
- the cluster's scheduler (Kueue or KAI) admitting the pod
- the capacity adapter polling the scheduler's status to feed `IC.status.capacity` back to the matcher
- a drift-detection controller reconciling node labels against your declared `deviceAttributes`

None of those leak into your manifests. If you want to see the full lifecycle layering, [design.md > Crossplane lifecycle layers](./design.md#crossplane-lifecycle-layers--what-gets-reconciled-where) walks through which layer owns what.

## Where to next

- **More scenarios** — [advanced.md](./advanced.md) covers multi-region routing, BYOC with KAI, P/D disaggregation, custom hardware, and SaaS spillover.
- **Concept understanding** — [scheduling.md](./scheduling.md) is the operator's reference for how placement works (matcher behavior, in-cluster scheduling, multi-tenancy modes, BYOC edge cases).
- **Design review** — [design.md](./design.md) is the engineering reference: principles, IRs, plugin/adapter axes, risks, roadmap.
