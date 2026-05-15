# qwen-cached-demo — XR / MR topology

Composition tree for the demo, top-down from user-facing XRs to live workload pods.

- 🟡 **ModelCache work** (new in PR #78)
- 🟠 **External substrate we may swap** (KServe LLMInferenceService, LWS gang, KServe/LWS installs — owned by upstream operators today)

```mermaid
flowchart TB
  classDef cache  fill:#fde047,stroke:#a16207,stroke-width:3px,color:#000
  classDef swap   fill:#fdba74,stroke:#c2410c,stroke-width:3px,color:#000
  classDef xr     fill:#dbeafe,stroke:#1e40af,color:#000
  classDef intxr  fill:#e0e7ff,stroke:#4338ca,color:#000
  classDef mr     fill:#f3f4f6,stroke:#374151,color:#000
  classDef rt     fill:#dcfce7,stroke:#15803d,color:#000

  %% ─── User-facing XRs ─────────────────────────────────────────────
  subgraph USER["User-facing XRs · modelplane.ai/v1alpha1"]
    direction LR
    IG[InferenceGateway]:::xr
    ICL[InferenceClass]:::xr
    IC[InferenceCluster]:::xr
    MC([ModelCache]):::cache
    MD[ModelDeployment]:::xr
    ME[ModelEndpoint]:::xr
    MS[ModelService]:::xr
  end

  %% ─── Internal Modelplane XRs ────────────────────────────────────
  subgraph INT["Internal XRs"]
    direction LR
    GKE[GKECluster]:::intxr
    KSC[KServeCluster]:::intxr
    MRL[ModelReplica]:::intxr
  end

  %% ─── GCP infra MRs ──────────────────────────────────────────────
  subgraph GCP["GCP MRs · provider-gcp"]
    direction LR
    NET[Network]:::mr
    SUB[Subnetwork]:::mr
    CL["Cluster + Filestore CSI addon"]:::mr
    NP["NodePool ×2 · T4"]:::mr
    PS["ProjectService · file.googleapis.com"]:::mr
  end

  %% ─── Workload-cluster install MRs ──────────────────────────────
  subgraph INST["Workload-cluster installs"]
    direction LR
    KSREL([Release · KServe]):::swap
    LWSREL([Release · LWS]):::swap
    SCMR([Object → StorageClass · modelplane-rwx]):::cache
  end

  %% ─── ModelCache MRs ─────────────────────────────────────────────
  subgraph CACHEMR["ModelCache MRs"]
    direction LR
    PVCMR([Object → PVC · RWX, Filestore]):::cache
    JOBMR([Object → Hydration Job · HF → PVC]):::cache
  end

  %% ─── Serving MRs ────────────────────────────────────────────────
  subgraph SERVE["Serving MRs"]
    direction LR
    LIS([Object → LLMInferenceService]):::swap
    BE[Backend · envoy gateway]:::mr
    HTR[HTTPRoute · envoy gateway]:::mr
  end

  %% ─── Workload-cluster runtime ───────────────────────────────────
  subgraph RT["Workload-cluster runtime"]
    direction LR
    GANG{{LWS gang · leader + worker}}:::swap
    PVCBOUND[("Bound PVC · /mnt/models")]:::cache
  end

  %% ─── Cluster provisioning edges ────────────────────────────────
  IC --> GKE
  IC --> KSC
  ICL -. ref .-> IC
  GKE --> NET
  GKE --> SUB
  GKE --> CL
  GKE --> NP
  GKE --> PS
  KSC --> KSREL
  KSC --> LWSREL
  IC --> SCMR

  %% ─── ModelCache path (yellow) ──────────────────────────────────
  MC ==> PVCMR
  MC ==> JOBMR
  PVCMR ==> PVCBOUND
  JOBMR == writes ==> PVCBOUND
  SCMR -. used by .-> PVCMR

  %% ─── Serving path ──────────────────────────────────────────────
  MC -. "spec.caches[]" .-> MD
  MD --> MRL
  MD --> ME
  MRL --> LIS
  MRL --> BE
  ME -. reads backendName .-> MRL
  LIS --> GANG
  GANG == mounts RWX ==> PVCBOUND

  MS -. selects by label .-> ME
  MS --> HTR
  HTR -. backendRefs[] .-> BE
  HTR -. exposed on .-> IG
```

## What changed in PR #78 / on this branch

**ModelCache primitive (yellow).** New user-facing XR + composition function that takes an artifact source (HuggingFace, S3, OCI, …) and a backend (PVC for v0.1) and emits a per-cluster RWX `PVC` + a one-shot hydration `Job`. `ModelDeployment.spec.caches[]` references the cache, and the deployment's composition sets `model.uri = pvc://modelcache-<name>` on the `LLMInferenceService` so every pod in the LWS gang mounts the same pre-populated PVC.

**Cloud-agnostic storage capability.** `InferenceCluster.spec.storage.csiDrivers: [SharedFilesystem]` is the *semantic* capability the user requests. The GKE branch of `compose-inference-cluster` reads the underlying VPC name from `GKECluster.status.network.name` and composes a workload-cluster `StorageClass` (`modelplane-rwx`) with `parameters.network=<our VPC>` so Filestore PVCs land in the reachable network. EKS / AKS branches will follow the same pattern with their respective knobs.

**GCP API auto-enable.** `compose-gke-cluster` now also composes a `ProjectService` MR for `file.googleapis.com` whenever the user opts into the Filestore CSI addon — without it, PVCs sit Pending forever with `SERVICE_DISABLED` in their workload-cluster events.

## Why the orange band is interesting

The orange items (`KServe Release`, `LWS Release`, `Object → LLMInferenceService`, and the LWS gang itself) are upstream-operator territory. We compose them today because they exist, work, and ship `model.uri = pvc://…` semantics out of the box. If/when Modelplane introduces an internal serving primitive that owns engine-pod + gang lifecycle directly, the swap point is exactly this band — `ModelDeployment` / `ModelService` / `ModelEndpoint` and the yellow ModelCache path are untouched.

## Why the yellow band is the minimum

Multi-node LWS serving structurally requires the same weight bytes on every gang pod. The minimum primitive that delivers that — and works without KServe's storage-initializer init-container OOMing on big models — is a per-cluster RWX PVC, hydrated once by a side Job, mounted read-only by every gang pod. v0.1 ships exactly that. Single-node scale-up benefits as a side effect (no per-replica HF pull). The harder content-addressed wins stay in v0.2 behind the same user-facing API.
