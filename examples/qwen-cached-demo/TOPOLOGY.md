# qwen-cached-demo — XR / MR topology

What the demo composes, from user-facing XRs (top) down to live workload-cluster pods (bottom). **ModelCache work highlighted in yellow.**

```mermaid
flowchart TB
  classDef cache  fill:#fde047,stroke:#a16207,stroke-width:3px,color:#000
  classDef xr     fill:#dbeafe,stroke:#1e40af,color:#000
  classDef intxr  fill:#e0e7ff,stroke:#4338ca,color:#000
  classDef mr     fill:#f3f4f6,stroke:#374151,color:#000
  classDef rt     fill:#dcfce7,stroke:#15803d,color:#000

  %% ─── User-facing XRs (modelplane.ai/v1alpha1) ────────────────────────
  subgraph USER["User-facing XRs"]
    direction LR
    IC["InferenceCluster<br/>qwen-cached-demo"]:::xr
    ICL["InferenceClass<br/>gke-t4-1x-n1"]:::xr
    MC(["ModelCache<br/>qwen-2-5-0-5b"]):::cache
    MD["ModelDeployment<br/>qwen-cached-demo<br/>TensorPipeline 1×2"]:::xr
    MS["ModelService<br/>qwen-cached-demo"]:::xr
  end

  %% ─── Internal XRs ────────────────────────────────────────────────────
  subgraph INTERNAL["Internal XRs"]
    direction LR
    GKE["GKECluster<br/>(GCP-specific)"]:::intxr
    KSC["KServeCluster<br/>(KServe + LWS install)"]:::intxr
  end

  %% ─── GCP MRs ────────────────────────────────────────────────────────
  subgraph GCPMR["GCP MRs (provider-gcp)"]
    direction LR
    NET[Network]:::mr
    SUB[Subnetwork]:::mr
    CL["Cluster<br/>+ Filestore CSI addon"]:::mr
    NP["NodePool ×2<br/>nvidia-tesla-t4"]:::mr
  end

  %% ─── Helm / k8s MRs ─────────────────────────────────────────────────
  subgraph PLATMR["Platform MRs (provider-helm / provider-kubernetes)"]
    direction LR
    KSREL["Release<br/>KServe"]:::mr
    LWSREL["Release<br/>LWS"]:::mr
  end

  %% ─── ModelCache MRs (highlighted) ────────────────────────────────────
  subgraph CACHEMR["ModelCache MRs"]
    direction LR
    PVCMR(["Object → PVC<br/>RWX, Filestore"]):::cache
    JOBMR(["Object → Hydration Job<br/>HF → PVC"]):::cache
  end

  %% ─── Deployment / Service MRs ───────────────────────────────────────
  subgraph WLMR["Workload MRs"]
    direction LR
    LIS["Object → LLMInferenceService<br/>model.uri = pvc://…"]:::mr
    HTR["Object → HTTPRoute"]:::mr
  end

  %% ─── Workload-cluster runtime ───────────────────────────────────────
  subgraph RUNTIME["Workload-cluster runtime"]
    direction LR
    GANG{{"LWS gang<br/>leader + worker pod"}}:::rt
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

  MC ==> PVCMR
  MC ==> JOBMR
  PVCMR ==> PVCBOUND
  JOBMR == writes ==> PVCBOUND

  MC -. "spec.caches[]" .-> MD
  MD --> LIS
  LIS --> GANG
  GANG == "mounts RWX" ==> PVCBOUND

  MS --> HTR
  HTR -. routes to .-> GANG
```

## Legend

| Color | Layer |
|---|---|
| 🟡 yellow | **ModelCache** — XR, MRs, hydrated PVC (the new primitive in PR #78) |
| 🔵 blue | User-facing XR |
| 🟣 indigo | Internal XR (one layer down) |
| ⚪ grey | Managed Resource (a real cloud / k8s object) |
| 🟢 green | Runtime objects on the workload cluster |

## What the highlighted path shows

1. `ModelCache` XR composes two MRs: a `PVC` (RWX, backed by Filestore via the cluster's `csiDrivers: [SharedFilesystem]` capability) and a one-shot hydration `Job` that pulls weights from HuggingFace and writes them to the PVC.
2. `ModelDeployment.spec.caches: [{name: qwen-2-5-0-5b}]` makes the composition function set `LLMInferenceService.spec.model.uri = pvc://modelcache-qwen-2-5-0-5b`.
3. KServe + LWS expand that into a **gang of 2 pods (leader + worker)** that both mount the same PVC at `/mnt/models` — no per-pod HF download, no init-container OOM.

The minimum needed to unblock multi-node LWS is exactly the yellow path. Everything else (cluster, KServe, gateway) is shared infrastructure that exists whether ModelCache exists or not.
