# qwen-cached-demo — XR / MR topology

What the demo composes, top-down from user-facing XRs to live workload pods serving HTTP. Verified end-to-end: real chat completion returns HTTP 200 over the path drawn below.

- 🟡 **ModelCache work** (new in PR #78 — the v0.1 primitive)
- 🟠 **External substrate we may swap** (KServe LLMInferenceService, LWS gang, KServe/LWS installs)

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
    ICL[InferenceClass · gke-t4-1x-n1]:::xr
    IC[InferenceCluster · qwen-cached-demo]:::xr
    MC(["ModelCache · qwen-2-5-0-5b<br/>backend: PVC"]):::cache
    MD["ModelDeployment · qwen-cached-demo<br/>TensorPipeline tensor=1 pipeline=2"]:::xr
    ME[ModelEndpoint]:::xr
    MS[ModelService]:::xr
  end

  %% ─── Internal Modelplane XRs ────────────────────────────────────
  subgraph INT["Internal XRs"]
    direction LR
    GKE["GKECluster · GCP-specific<br/>status.network.name"]:::intxr
    KSC["KServeCluster · KServe + LWS install"]:::intxr
    MRL[ModelReplica · per-cluster]:::intxr
  end

  %% ─── GCP infra MRs ──────────────────────────────────────────────
  subgraph GCP["GCP MRs · provider-gcp"]
    direction LR
    NET[Network]:::mr
    SUB[Subnetwork]:::mr
    CL["Cluster + Filestore CSI addon"]:::mr
    NP["NodePool ×2 · T4 / n1-standard-4"]:::mr
    PS["ProjectService · file.googleapis.com"]:::mr
  end

  %% ─── Workload-cluster installs ──────────────────────────────────
  subgraph INST["Workload-cluster installs"]
    direction LR
    KSREL([Release · KServe v0.18]):::swap
    LWSREL([Release · LWS]):::swap
    SCMR(["Object → StorageClass · modelplane-rwx<br/>parameters.network = our VPC"]):::cache
  end

  %% ─── ModelCache MRs ─────────────────────────────────────────────
  subgraph CACHEMR["ModelCache MRs"]
    direction LR
    PVCMR(["Object → PVC · RWX, Filestore"]):::cache
    JOBMR(["Object → Hydration Job<br/>hf download → /mnt/model"]):::cache
  end

  %% ─── Serving MRs ────────────────────────────────────────────────
  subgraph SERVE["Serving MRs"]
    direction LR
    LIS(["Object → LLMInferenceService<br/>model.uri = pvc://modelcache-...<br/>worker = flat PodSpec, ray bootstrap cmd"]):::swap
    BE[Backend · envoy gateway]:::mr
    HTR[HTTPRoute · envoy gateway]:::mr
  end

  %% ─── Workload-cluster runtime ───────────────────────────────────
  subgraph RT["Workload-cluster runtime"]
    direction LR
    LEAD{{"LWS leader pod<br/>ray start --head + vllm serve"}}:::swap
    WORK{{"LWS worker pod<br/>ray start --address=... --block"}}:::swap
    PVCBOUND[("Bound PVC · /mnt/models<br/>Qwen 2.5 0.5B-Instruct")]:::cache
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
  GKE -. status.network.name .-> SCMR

  %% ─── ModelCache path (yellow) ──────────────────────────────────
  MC ==> PVCMR
  MC ==> JOBMR
  PVCMR ==> PVCBOUND
  JOBMR == writes Qwen ==> PVCBOUND
  SCMR -. used by .-> PVCMR

  %% ─── Serving path ──────────────────────────────────────────────
  MC -. "spec.caches[]" .-> MD
  MD --> MRL
  MD --> ME
  MRL --> LIS
  MRL --> BE
  ME -. backendName .-> MRL
  LIS --> LEAD
  LIS --> WORK
  LEAD <-. ray cluster .-> WORK
  LEAD == mounts ==> PVCBOUND
  WORK == mounts ==> PVCBOUND

  MS -. selects by label .-> ME
  MS --> HTR
  HTR -. backendRefs[] .-> BE
  HTR -. exposed on .-> IG
```

## What this proves

**The yellow path is the v0.1 primitive.** `ModelCache` composes a per-cluster RWX PVC + a one-shot hydration Job; the Job pulls Qwen 2.5 0.5B-Instruct from HuggingFace into the PVC. `InferenceCluster.spec.storage.csiDrivers: [SharedFilesystem]` is the cloud-agnostic capability declaration; the GKE composition branch reads `GKECluster.status.network.name` and composes the `modelplane-rwx` StorageClass on the workload cluster with `parameters.network` set so Filestore lands on the cluster's VPC (otherwise it defaults to the GCP `default` VPC and is unreachable from our nodes). `compose-gke-cluster` also auto-enables `file.googleapis.com` via a `ProjectService` MR.

**`ModelDeployment.spec.caches[]` is the wire.** The composition function reads it and sets `LLMInferenceService.spec.model.uri = pvc://modelcache-qwen-2-5-0-5b`. KServe mounts that PVC at `/mnt/models` on every gang pod. The function also appends `--model=/mnt/models` to the engine args (KServe v0.17+ stopped injecting that automatically) and a Ray-bootstrap shell wrapper as the container `command` when `topology.strategy == TensorPipeline`.

**The LWS gang of 2 pods both read from the same cached PVC.** Leader runs `ray start --head` then execs `vllm serve`; worker runs `ray start --address=$LWS_LEADER_ADDRESS:6379 --block` so vLLM's placement group sees both GPUs and pipeline-parallel-size=2 can land. End-to-end: real chat completion over HTTP 200.

## Why the orange band is interesting

The orange items (KServe `Release`, LWS `Release`, `LLMInferenceService` MR, the LWS gang itself) are upstream-operator territory. v0.1 composes them today because they exist and work and ship `model.uri = pvc://…` mounting semantics out of the box. If/when Modelplane introduces an internal serving primitive that owns engine-pod + gang lifecycle directly (composing `LeaderWorkerSet` ourselves with the same bootstrap-as-command pattern, without KServe in the middle), the swap point is exactly this band. The yellow path and the user-facing API are unaffected.
