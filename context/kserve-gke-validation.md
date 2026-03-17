# Modelplane Infra: End-to-End Test Plan

**Goal:** Get a working OpenAI-compatible inference endpoint powered by KServe
LLMInferenceService on our GKE cluster, proving the full Modelplane infra stack
works: GKECluster XR → KServeStack XR → live model serving inference traffic.

**Date:** March 2026

---

## End-to-end test result: SUCCESS (three times)

### Run 4: v0.8.2 — direct ConfigMap (2026-03-17)

Goal: verify v0.8.x refactoring that replaced ProviderConfig+Object wrapper with
a directly-composed ConfigMap for the storage initializer patch.

Upgraded compose-kserve-stack from v0.7.1 → v0.8.2 in-place (no XR recreate).
Crossplane garbage-collected the old local ProviderConfig and Object wrapper.
Direct ConfigMap composed in `crossplane-system`, `patchesFrom` still works,
inference verified end-to-end.

Key findings:
- Crossplane resolves function tags to digests via Docker Hub — loading a new
  image into kind with the same tag doesn't trigger a new revision. Must use a
  new tag (or push to registry).
- When changing a composed resource's GVK (Object→ConfigMap) with the same
  resource key, the resourceRef loses the namespace. Required a one-time manual
  patch to add `namespace: crossplane-system` to the resourceRef.
- ConfigMaps have no `Ready` condition, so the function must mark them
  always-ready (like LWS).

### Run 3: Fully automated KServeStack (2026-03-17)

Goal: prove that creating a KServeStack XR "just works" with zero manual
intervention (except the pre-existing ClusterRoleBinding — issue #9, deferred).

Deleted KServeStack XR, recreated from scratch with compose-kserve-stack v0.7.1.
All KServe-specific issues now automated:
- Storage initializer 4Gi via `patchesFrom` Kustomize post-render patch
- Inference Extension CRDs installed as provider-kubernetes Objects
- LWS marked always-ready to avoid cosmetic readiness block
- cert-manager CRDs use `crds.keep: false` to avoid ownership conflict on recreate

New issue discovered: cert-manager CRD Helm ownership annotations persist across
delete/recreate (issue #11). Fixed with `crds.keep: false`.

```bash
curl http://34.56.129.3/default/test-qwen/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "Qwen/Qwen2.5-1.5B-Instruct", "messages": [{"role": "user", "content": "What is Crossplane?"}], "max_tokens": 100}'
```

Response:
```json
{"choices":[{"message":{"content":"Crossplane is an open-source platform for managing infrastructure access networks..."}}]}
```

### Run 2: Destroy-and-recreate (2026-03-17)

Full destroy/recreate cycle. Several new issues discovered and fixed (issues
8-10). Required manual CRD install and ConfigMap patch.

### Run 1: First successful inference (2026-03-17)

First end-to-end proof. Multiple manual steps needed.

### Working LLMInferenceService spec
```yaml
apiVersion: serving.kserve.io/v1alpha1
kind: LLMInferenceService
metadata:
  name: test-qwen
  namespace: default
spec:
  model:
    uri: hf://Qwen/Qwen2.5-1.5B-Instruct
    name: Qwen/Qwen2.5-1.5B-Instruct
  replicas: 1
  template:
    containers:
    - name: main
      image: vllm/vllm-openai:v0.7.3
      securityContext:
        runAsUser: 0
        runAsNonRoot: false
      resources:
        limits:
          nvidia.com/gpu: "1"
          cpu: "3"
          memory: 10Gi
        requests:
          cpu: "1"
          memory: 10Gi
  router:
    gateway: {}
    route: {}
```

---

## Issues discovered and fixed during testing

### 1. GKE auto-adds GPU taint — don't duplicate it
GKE automatically adds `nvidia.com/gpu=present:NoSchedule` to GPU nodepools.
The compose-gke-cluster function was also adding `nvidia.com/gpu=true:NoSchedule`,
causing a "duplicate taint" error from the GKE API. **Fixed** in v0.3.0 by
removing the explicit taint from the function.

### 2. KServe's managed gateway expects `kserve/kserve-ingress-gateway`
KServe v0.16 managed gateway mode (`router: {gateway: {}, route: {}}`) hardcodes
the gateway name to `kserve/kserve-ingress-gateway`. Our KServeStack composes a
Gateway named `modelplane` in `envoy-gateway-system`. **Fixed** in
compose-kserve-stack v0.5.0 — the composed Gateway Object is now named
`kserve-ingress-gateway` in the `kserve` namespace with
`allowedRoutes.namespaces.from: All`.

### 3. Gateway must allow cross-namespace routes
The `kserve-ingress-gateway` Gateway must have `allowedRoutes.namespaces.from: All`
to accept HTTPRoutes from the `default` namespace where LLMInferenceService lives.
**Fixed** in compose-kserve-stack v0.5.0.

### 4. Storage initializer OOM with default 1Gi memory
KServe's storage initializer init container defaults to 1Gi memory limit. Model
downloads (even 3GB Qwen2.5-1.5B) cause OOM. The `kserve-llmisvc-resources`
Helm chart (v0.16.0) has a **static ConfigMap template** with zero Helm values
paths to override `storageInitializer.memoryLimit`.
**Fixed** in compose-kserve-stack v0.7.0 using provider-helm's `patchesFrom`
field. The function composes a ConfigMap on the control plane containing a
Kustomize strategic merge patch. The Helm Release's `patchesFrom` applies this
patch post-render, so Helm's desired state includes 4Gi — no more fight between
controllers. Simplified in v0.8.2 from ProviderConfig+Object to a
directly-composed ConfigMap (Crossplane v2 supports composing any resource).

### 5. vLLM image runs as root
`vllm/vllm-openai:v0.7.3` runs as root, but KServe sets `runAsNonRoot: true`.
Must override with `securityContext: {runAsUser: 0, runAsNonRoot: false}`.

### 6. g2-standard-4 memory is tight for vLLM
g2-standard-4 has 16GB RAM, ~13Gi allocatable after GKE system reservation.
With DaemonSet overhead (~1.1Gi), only ~12Gi available. vLLM needs ~10Gi for
CPU memory (model weights in RAM + CUDA overhead). Memory limit must be ≤10Gi
to fit. Alternatively, use g2-standard-8 (32GB) for more headroom.

### 7. KServe doesn't install Inference Extension CRDs
KServe v0.16 expects Gateway API Inference Extension CRDs (`inferencepools`,
`inferencemodels`) but doesn't install them.
The CRD group is `inference.networking.x-k8s.io` and the API version is
`v1alpha2`. **Fixed** in compose-kserve-stack v0.6.1 — CRDs loaded from
`inference_extension_crds.json` and installed as provider-kubernetes Objects.

### 8. GPU nodepool zones — not all zones have every GPU type
**Discovered in Run 2.** GKE regional clusters auto-select zones (e.g.
us-central1-a, us-central1-c, us-central1-f). If a GPU type (e.g. nvidia-l4)
isn't available in one of those zones, nodepool creation fails with:
`Accelerator type "nvidia-l4" does not exist in zone us-central1-f`.
**Fixed** in compose-gke-cluster v0.4.0 by adding `zones` field to the XRD
nodepool spec and setting `nodeLocations` on the GKE NodePool MR. GPU pools
should specify zones that are a **subset of the cluster's zones** AND have
the desired GPU type. For us-central1 L4s: `["us-central1-a", "us-central1-c"]`.

### 9. GKE client certificate user needs ClusterRoleBinding
**Discovered in Run 2.** The GKE cluster connection secret uses a client
certificate with CN `client`. In GKE 1.35 with RBAC-only (no legacy ABAC),
this user has zero permissions. Provider-helm and provider-kubernetes fail with
`secrets is forbidden: User "client" cannot list resource "secrets"`.
**Current workaround:** manually create a ClusterRoleBinding via `gcloud` auth:
```bash
nix shell nixpkgs#google-cloud-sdk -c bash -c '
  TOKEN=$(gcloud auth print-access-token)
  cat > /tmp/gke-admin-kubeconfig.yaml <<EOF
  ...bearer token kubeconfig...
  EOF'
kubectl --kubeconfig /tmp/gke-admin-kubeconfig.yaml create clusterrolebinding \
  client-cluster-admin --clusterrole=cluster-admin --user=client
```
**TODO:** Add a ClusterRoleBinding Object to the compose-gke-cluster function
that grants cluster-admin to the `client` user. This is a chicken-and-egg
problem — the Object needs a working ProviderConfig to create the binding, but
the ProviderConfig uses the client cert that needs the binding. May need to use
GCP IAM-based auth instead of client certs, or use `masterAuth.username` (basic
auth) which is auto-granted cluster-admin in GKE.

### 10. Docker Hub rate limits for function images
**Discovered in Run 2.** Crossplane's package manager hits Docker Hub
unauthenticated pull rate limits when fetching function images. **Fix:** Create
a `docker-registry` Secret in `crossplane-system` and add it to the Function's
`spec.packagePullSecrets`. Or use a registry without rate limits (ghcr.io, etc).

### 11. cert-manager CRD Helm ownership on recreate
**Discovered in Run 3.** cert-manager CRDs default to `helm.sh/resource-policy:
keep`, so they survive Helm release deletion. On recreate, the new release has a
different name and Helm refuses to adopt the orphaned CRDs (`invalid ownership
metadata; annotation validation error: key "meta.helm.sh/release-name" must
equal ...`).
**Fixed** in compose-kserve-stack v0.7.1 by setting `crds.keep: false` in the
cert-manager Helm values, so CRDs are properly cleaned up on release deletion.

### 12. kserve-controller Release shows Unavailable after upgrade
**Discovered in Run 3.** After adding `patchesFrom` to the kserve-controller
Release, provider-helm reports Unavailable even though the deployment is 1/1
Ready. Clears up after a forced reconciliation. Same cosmetic pattern as LWS
(issue #6). Likely a provider-helm readiness check timing issue after Helm
upgrade.

### 13. LWS Release stuck Unavailable — chart version mismatch
**Discovered in Run 4.** The LWS Helm chart's metadata has `version: v0.7.0`
(with `v` prefix) but the KServeStack XRD defaulted to `0.7.0` (without prefix).
provider-helm's `isUpToDate` does an exact string comparison of
`spec.forProvider.chart.version` vs the installed chart's `metadata.version`.
The mismatch causes `isUpToDate` to return false every reconcile, triggering an
infinite upgrade loop (revision count climbed to 10+) and permanently reporting
the release as Unavailable.
**Fixed** in compose-kserve-stack v0.9.0 by changing the default to `v0.7.0`.
The always-ready workaround for LWS is no longer needed.

---

## Composition improvements — status

### compose-kserve-stack (all KServe issues fixed)

1. ✅ **Inference Extension CRDs** — v0.6.1. Loaded from JSON, installed as Objects.
2. ✅ **Storage initializer memory** — v0.8.2. Uses `patchesFrom` with a
   Kustomize post-render patch via directly-composed ConfigMap.
3. ✅ **LWS version mismatch** — v0.9.0. Chart metadata uses `v0.7.0` but
   XRD defaulted to `0.7.0`. Fixed default, removed always-ready workaround.
4. ✅ **Dead `gatewayApi` XRD field** — v0.9.0. Removed unused field.
5. ✅ **cert-manager CRD ownership** — v0.7.1. `crds.keep: false`.

### compose-gke-cluster

6. **ClusterRoleBinding for client cert** — Still manual (issue #9, deferred).
   May pivot to EKS or IAM-based auth.
7. ✅ **GPU nodepool zones** — v0.4.0.
8. ✅ **GPU taint removed** — v0.3.0.

---

## Current state

### What's working

- **Crossplane** is running in a local kind cluster (`kind-modelplane`).
- **GKECluster XR** (`test-us-central1`) is Ready. Composition function
  (`docker.io/negz/modelplane-compose-gke-cluster:v0.4.0`) provisions VPC,
  Subnet, GKE Cluster, NodePools (with zone support), and ProviderConfigs.
- **GKE cluster** is running in `crossplane-playground` / `us-central1` with:
  - `system` pool: `e2-standard-4`, 1 node, min 1 / max 2
  - `gpu-l4` pool: `g2-standard-4` with 1x `nvidia-l4`, 1 node, min 0 / max 2,
    zones: `[us-central1-a, us-central1-c]`
- **KServeStack XR** (`test-us-central1-kserve`) is **Ready=True**. All 5 Helm
  releases deployed, Inference Extension CRDs installed, storage initializer
  patched to 4Gi, LWS marked always-ready.
- **Inference is working** — LLMInferenceService `test-qwen` serving
  Qwen/Qwen2.5-1.5B-Instruct via vLLM on L4 GPU at `http://34.56.129.3`.

### What needs manual intervention on each recreate

1. **ClusterRoleBinding** for the `client` cert user (issue #9, deferred)

All other KServe-specific issues are now automated.

---

## Live resources on kind-modelplane

| Resource | Name | Status |
|---|---|---|
| GKECluster XR | `test-us-central1` | **Ready** |
| KServeStack XR | `test-us-central1-kserve` | **Ready** |
| GCP Network | various | Ready |
| GCP Subnetwork | various | Ready |
| GKE Cluster | various | Ready |
| NodePool (system) | various | Ready |
| NodePool (gpu-l4) | various | Ready |
| ProviderConfig (k8s) | `test-us-central1-kubeconfig` | exists |
| ProviderConfig (helm) | `test-us-central1-kubeconfig` | exists |
| Helm: cert-manager | various | Ready |
| Helm: envoy-gateway | various | Ready |
| Helm: kserve-crds | various | Ready |
| Helm: kserve-controller | various | Ready (patchesFrom) |
| Helm: lws | various | Ready |
| Object: GatewayClass | various | Ready |
| Object: Gateway | various | Ready |
| Object: Inference Ext CRDs (x2) | various | Ready |
| ConfigMap: storage patch | `test-us-central1-kserve-storage-patch` | Ready (always-ready) |

### On the GKE cluster

| Resource | Namespace | Name | Status |
|---|---|---|---|
| Gateway | `kserve` | `kserve-ingress-gateway` | Programmed, IP `34.56.129.3` |
| LLMInferenceService | `default` | `test-qwen` | Running (1/1 Ready) |
| HTTPRoute | `default` | `test-qwen-kserve-route` | Accepted |
| Inference Extension CRDs | cluster-wide | v0.3.0 | Installed (automated) |
| inferenceservice-config | `kserve` | ConfigMap | storageInitializer.memoryLimit=4Gi |
| ClusterRoleBinding | cluster-wide | `client-cluster-admin` | Active (manually created) |

### Function versions

| Function | Package | Version |
|---|---|---|
| compose-gke-cluster | `docker.io/negz/modelplane-compose-gke-cluster` | **v0.4.0** (zones support) |
| compose-kserve-stack | `docker.io/negz/modelplane-compose-kserve-stack` | **v0.9.0** (LWS fix, dead field removed) |

---

## Troubleshooting guide

### vLLM pod stuck in Pending
- GPU nodepool hasn't scaled up yet (check node count)
- Missing toleration for `nvidia.com/gpu` taint (KServe auto-adds it)
- Insufficient GPU resources (shouldn't happen with L4 + 1.5B model)

### vLLM pod in CrashLoopBackOff
- Image pull failure (check image tag exists)
- Model download failure (network issue, or model requires auth token)
- CUDA version mismatch between vLLM image and GPU driver
- OOM (model too large for GPU VRAM — not an issue for 1.5B on L4)

### Init container OOMKilled (storage initializer)
- `inferenceservice-config` configmap in `kserve` namespace needs
  `storageInitializer.memoryLimit: 4Gi` (default 1Gi is too low)
- After patching configmap, must restart kserve controller AND recreate
  the LLMInferenceService (existing Deployments don't update)

### HTTPRoute not created / no routing
- Inference Extension CRDs missing — install from
  `github.com/kubernetes-sigs/gateway-api-inference-extension/releases v0.3.0`
- KServe controller not running or not watching the namespace
- Gateway reference mismatch in the router config

### Helm releases show Synced=False
- ProviderConfig kubeconfig secret missing (cluster still creating)
- Client cert user lacks RBAC (issue #9) — create ClusterRoleBinding
- Docker Hub rate limit on function images (issue #10)

### GPU nodepool creation fails
- Zone doesn't have the GPU type — specify `zones` in the nodepool spec
- Zones must be a subset of the cluster's auto-selected zones
- Check which zones have the GPU: `gcloud compute accelerator-types list --filter="name=nvidia-l4" --format="value(zone)"`

### Gateway has no external IP
- GKE LoadBalancer provisioning is slow (wait a few minutes)
- Firewall rules blocking external access
- Use port-forward as fallback

### LWS release stays Unavailable
- This is cosmetic — the LWS controller pod is actually healthy
- Doesn't block inference, only blocks KServeStack XR Ready status

---

## File reference

```
modelplane-infra/
├── apis/
│   ├── gkeclusters/
│   │   ├── definition.yaml      # GKECluster XRD (v2, Cluster scope)
│   │   │                        #   includes zones field for nodepools
│   │   └── composition.yaml     # Pipeline → compose-gke-cluster function
│   └── kservestacks/
│       ├── definition.yaml      # KServeStack XRD (v2, Cluster scope)
│       └── composition.yaml     # Pipeline → compose-kserve-stack function
├── functions/
│   ├── compose-gke-cluster/
│   │   └── main.py              # Composes: Network, Subnet, Cluster,
│   │                            #   NodePools (loop, with nodeLocations),
│   │                            #   2x ProviderConfig
│   └── compose-kserve-stack/
│       ├── main.py              # Composes: 5 Helm releases, 4 Objects,
│       │                        #   1 direct ConfigMap. Handles storage
│       │                        #   initializer patch via patchesFrom.
│       ├── inference_extension_crds.json  # InferenceModel + InferencePool CRDs
│       └── inference_extension_crds.yaml  # Source YAML (reference only)
├── examples/
│   ├── gkecluster/example.yaml  # test GKECluster (system + gpu-l4 pools)
│   └── kservestack/example.yaml # test KServeStack (v0.16.0, envoy gateway)
├── tests/
│   ├── test-gkecluster/main.py  # CompositionTest for GKECluster
│   └── test-kservestack/main.py # CompositionTest for KServeStack
└── upbound.yaml                 # Project metadata, deps: provider-gcp-*,
                                 #   provider-helm, provider-kubernetes,
                                 #   function-auto-ready
```

## Context pointers

- **Modelplane PRD:** `~/control/upbound/scratch/modelplane-prd/README.md`
  (on the `modelplane-prd` branch). Defines the full API design, composition
  architecture, and KServe backend details.
- **GKE + KServe composition design:**
  `~/control/upbound/scratch/modelplane-prd/gke-kserve-composition.md`.
  Explains the two-XR split (GKECluster + KServeStack) and how they wire
  together via ProviderConfig.
- **Unified endpoint routing:**
  `~/control/upbound/scratch/modelplane-prd/unified-endpoint-routing.md`.
  Recommends Envoy Gateway + Backend API on the control plane cluster for
  cross-cluster routing.
- **K8s inference landscape:**
  `~/control/upbound/scratch/modelplane-prd/k8s-inference-landscape.md`.
  Surveys KServe, AIBrix, OME, llm-d, Gateway API Inference Extension.
- **Crossplane v2 deep-dive:**
  `~/control/upbound/scratch/modelplane-prd/crossplane-v2-deep-dive.md`.
  Covers function pipeline, required resources, Python SDK, packaging.
- **Composition functions use `function-sdk-python==0.9.0`** and Pydantic
  models generated by the `up` CLI into each function's `model/` directory.
  The functions use `compose(req, rsp)` convention (not a class-based
  `FunctionRunner`). No git repo is initialized in this directory.

## Key operational commands

### Get GKE kubeconfig
```bash
kubectl --context kind-modelplane get secret test-us-central1-kubeconfig \
  -n crossplane-system -o jsonpath='{.data.kubeconfig}' | base64 -d > /tmp/gke-kubeconfig.yaml
```

### Get admin kubeconfig (for creating ClusterRoleBinding)
```bash
nix shell nixpkgs#google-cloud-sdk -c bash -c '
TOKEN=$(gcloud auth print-access-token)
CA_DATA=$(kubectl --context kind-modelplane get secret test-us-central1-kubeconfig \
  -n crossplane-system -o jsonpath="{.data.kubeconfig}" | base64 -d | grep certificate-authority-data | awk "{print \$2}")
SERVER=$(kubectl --context kind-modelplane get secret test-us-central1-kubeconfig \
  -n crossplane-system -o jsonpath="{.data.kubeconfig}" | base64 -d | grep server | awk "{print \$2}")
cat > /tmp/gke-admin-kubeconfig.yaml <<EOF
apiVersion: v1
kind: Config
clusters:
- cluster:
    certificate-authority-data: ${CA_DATA}
    server: ${SERVER}
  name: gke
contexts:
- context:
    cluster: gke
    user: gke-admin
  name: gke
current-context: gke
users:
- name: gke-admin
  user:
    token: ${TOKEN}
EOF'
```

### Test inference
```bash
GATEWAY_IP=$(kubectl --kubeconfig /tmp/gke-kubeconfig.yaml get gateway kserve-ingress-gateway -n kserve -o jsonpath='{.status.addresses[0].value}')
curl http://${GATEWAY_IP}/default/test-qwen/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "Qwen/Qwen2.5-1.5B-Instruct", "messages": [{"role": "user", "content": "What is Crossplane?"}], "max_tokens": 100}'
```

### Build and push function
```bash
cd /home/negz/control/modelplane-infra
up project build
docker load < _output/modelplane-infra.uppkg
docker tag xpkg.upbound.io/upbound/modelplane-infra_compose-gke-cluster:arm64 docker.io/negz/modelplane-compose-gke-cluster:vX.Y.Z
docker push docker.io/negz/modelplane-compose-gke-cluster:vX.Y.Z
kubectl --context kind-modelplane patch function upbound-modelplane-infracompose-gke-cluster --type merge \
  -p '{"spec":{"package":"docker.io/negz/modelplane-compose-gke-cluster:vX.Y.Z"}}'
```

### Fix SSH in tmux
```bash
eval $(tmux showenv -s SSH_AUTH_SOCK)
```
