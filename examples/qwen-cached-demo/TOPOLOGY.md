# qwen-cached-demo — XR / MR topology

What the demo composes, from user-facing XRs (top) down to live workload-cluster pods (bottom). **ModelCache work highlighted in yellow.** **External substrate we may replace with Modelplane-internal primitives highlighted in orange (KServe, LWS, LLMInferenceService).**

```mermaid
flowchart TB
  classDef cache    fill:#fde047,stroke:#a16207,stroke-width:3px,color:#000
  classDef swap     fill:#fdba74,stroke:#c2410c,stroke-width:3px,color:#000
  classDef xr       fill:#dbeafe,stroke:#1e40af,color:#000
  classDef intxr    fill:#e0e7ff,stroke:#4338ca,color:#000
  classDef mr       fill:#f3f4f6,stroke:#374151,color:#000
  classDef rt       fill:#dcfce7,stroke:#15803d,color:#000

  %% ─── User-facing XRs (modelplane.ai/v1alpha1) ────────────────────────
  subgraph USER["User-facing XRs (modelplane.ai/v1alpha1)"]
    direction TB
    IG["InferenceGateway"]:::xr
    ICL["InferenceClass<br/>gke-t4-1x-n1"]:::xr
    IC["InferenceCluster<br/>qwen-cached-demo"]:::xr
    MC(["ModelCache<br/>qwen-2-5-0-5b"]):::cache
    MD["ModelDeployment<br/>qwen-cached-demo<br/>TensorPipeline 1×2"]:::xr
    ME["ModelEndpoint<br/>(composed per replica)"]:::xr
    MS["ModelService<br/>qwen-cached-demo"]:::xr
  end

  %% ─── Internal Modelplane XRs ────────────────────────────────────────
  subgraph INTERNAL["Internal Modelplane XRs"]
    direction TB
    GKE["GKECluster<br/>(GCP-specific)"]:::intxr
    KSC["KServeCluster<br/>(installs KServe + LWS)"]:::intxr
    MR_REPLICA["ModelReplica<br/>(per-cluster replica)"]:::intxr
  end

  %% ─── GCP MRs ────────────────────────────────────────────────────────
  subgraph GCPMR["GCP MRs (provider-gcp)"]
    direction LR
    NET[Network]:::mr
    SUB[Subnetwork]:::mr
    CL["Cluster<br/>+ Filestore CSI addon"]:::mr
    NP["NodePool ×2<br/>nvidia-tesla-t4"]:::mr
  end

  %% ─── Helm / k8s install MRs ─────────────────────────────────────────
  subgraph PLATMR["Workload-cluster install MRs"]
    direction LR
    KSREL(["Release: KServe"]):::swap
    LWSREL(["Release: LWS"]):::swap
    KGW["Object → Gateway, GatewayClass<br/>CRDs"]:::mr
  end

  %% ─── ModelCache MRs (highlighted yellow) ─────────────────────────────
  subgraph CACHEMR["ModelCache MRs"]
    direction LR
    PVCMR(["Object → PVC<br/>RWX, Filestore"]):::cache
    JOBMR(["Object → Hydration Job<br/>HF → PVC"]):::cache
  end

  %% ─── Serving MRs ────────────────────────────────────────────────────
  subgraph WLMR["Serving MRs"]
    direction LR
    LIS(["Object → LLMInferenceService<br/>(KServe v1alpha1)<br/>model.uri = pvc://…"]):::swap
    BE["Backend<br/>(envoy gateway)"]:::mr
    HTR["HTTPRoute<br/>(envoy gateway)"]:::mr
  end

  %% ─── Workload-cluster runtime ───────────────────────────────────────
  subgraph RUNTIME["Workload-cluster runtime"]
    direction LR
    GANG{{"LWS gang<br/>leader + worker pod"}}:::swap
    PVCBOUND[("Bound PVC<br/>/mnt/models")]:::cache
  end

  %% ─── Edges ──────────────────────────────────────────────────────────
  IC --> GKE
  IC --> KSC
  ICL -. referenced .-> IC

  GKE --> NET
  GKE --> SUB
  GKE --> CL
  GKE --> NP

  KSC --> KSREL
  KSC --> LWSREL
  KSC --> KGW

  MC ==> PVCMR
  MC ==> JOBMR
  PVCMR ==> PVCBOUND
  JOBMR == writes ==> PVCBOUND

  MC -. "spec.caches[]" .-> MD
  MD --> MR_REPLICA
  MD --> ME
  MR_REPLICA --> LIS
  MR_REPLICA --> BE
  ME -. "reads backendName" .-> MR_REPLICA
  LIS --> GANG
  GANG == "mounts RWX" ==> PVCBOUND

  MS -. "selects by label" .-> ME
  MS --> HTR
  HTR -. "backendRefs[] → " .-> BE
  HTR -. "exposed on" .-> IG
```

## Legend

| Color | Layer |
|---|---|
| 🟡 yellow | **ModelCache work** — XR, PVC MR, Job MR, mounted PVC (new in PR #78) |
| 🟠 orange | **External substrate we may replace** — KServe `LLMInferenceService`, LWS gang, KServe/LWS Helm releases. Internalising these would let us own the engine-pod + gang lifecycle directly instead of riding on top of two upstream operators. |
| 🔵 blue | User-facing Modelplane XR |
| 🟣 indigo | Internal Modelplane XR (composition-only, not user-applied) |
| ⚪ grey | Managed Resource (cloud or k8s primitive) |
| 🟢 green | Workload-cluster runtime |

## Key paths

**Cluster provisioning** (cold infra, runs once per cluster):
`InferenceCluster` → `GKECluster` → `Network` / `Subnetwork` / `Cluster (+Filestore CSI addon)` / `NodePool ×2`
`InferenceCluster` → `KServeCluster` → Helm `Release` for KServe + LWS, plus Gateway CRDs.

**Cache hydration** (yellow path, runs once per cluster per ModelCache):
`ModelCache` → `Object → PVC` (RWX, Filestore-backed) + `Object → Hydration Job` (HF → PVC).
The Job writes weights into the PVC and exits. Cache reports `ArtifactReady`.

**Serving** (one ModelDeployment, one ModelService, all the orange stuff is currently KServe/LWS):
`ModelDeployment` → `ModelReplica` (one per matching cluster) → `Object → LLMInferenceService` + `Backend`.
The LLMInferenceService spec has `model.uri = pvc://modelcache-<name>` so KServe + LWS spin up a **gang of 2 pods** that both mount the cached PVC at `/mnt/models` — the yellow `mounts RWX` edge from the gang back to the cached PVC.
`ModelDeployment` → `ModelEndpoint` (one per replica) — reads `backendName` from the `ModelReplica`'s composed `Backend`.
`ModelService` selects `ModelEndpoint`s by label and emits an `HTTPRoute` whose `backendRefs[]` point at those backends; the route attaches to `InferenceGateway`.

## Why the orange highlight matters

The orange items are *substrate we don't own*. KServe's `LLMInferenceService` shape and LWS gang semantics are upstream contracts; today we compose them as MRs because they exist and they work. If/when Modelplane introduces an internal serving primitive (one that owns engine-pod + gang lifecycle without two operators in the middle), the swap point is exactly the orange band — the user-facing API (`ModelDeployment` / `ModelService` / `ModelEndpoint`) is unaffected.

The minimum needed to unblock multi-node LWS today is the **yellow path**. Everything orange is here because the existing OSS substrate makes it free to plug in for v0.1.
