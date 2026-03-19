# Modelplane — Implementation Reference

This document captures implementation details for Modelplane v0.1 — directory
layout, composition function behavior, RBAC requirements, provider dependencies,
and developer workflow. It supplements `design.md` (the design document) with
the specifics an implementer — human or LLM agent — needs to build from.

For design rationale, API schemas, and scope decisions, see `design.md`. For
background on KServe, the inference landscape, Crossplane v2 patterns, and
unified endpoint routing, see the companion documents in `context/`.

---

## Project layout

```
modelplane/
├── apis/
│   ├── clustermodel/
│   │   ├── definition.yaml           # XRD (scope: Cluster)
│   │   └── composition.yaml          # Composition referencing function
│   ├── model/
│   │   ├── definition.yaml           # XRD (scope: Namespaced)
│   │   └── composition.yaml          # Same function, different XRD
│   ├── inferenceenvironment/
│   │   ├── definition.yaml           # XRD (scope: Cluster)
│   │   └── composition.yaml
│   ├── modeldeployment/
│   │   ├── definition.yaml           # XRD (scope: Namespaced)
│   │   └── composition.yaml
│   └── modelplacement/
│       ├── definition.yaml           # XRD (scope: Namespaced)
│       └── composition.yaml
├── functions/
│   ├── function-modelplane-model/    # Shared by ClusterModel and Model
│   ├── function-modelplane-env/      # InferenceEnvironment composition logic
│   ├── function-modelplane-deploy/   # ModelDeployment → ModelPlacement fan-out
│   └── function-modelplane-placement/ # ModelPlacement → backend-specific resources
├── package/
│   └── crossplane.yaml              # Configuration package metadata
└── examples/
    ├── clustermodel-llama-8b.yaml
    ├── clustermodel-llama-70b.yaml
    ├── environment-gke-kserve.yaml
    ├── deployment-single-env.yaml
    └── deployment-multi-env.yaml
```

---

## Composition function details

All functions are standalone Python packages built with `function-sdk-python`
v0.11.0, packaged as multi-arch OCI images with embedded runtime.

### `function-modelplane-env`

The env function is a dispatch layer that reads the discriminated unions in the
InferenceEnvironment spec and composes the appropriate lower-level XRs. There is
a single Composition for InferenceEnvironment — the function handles all backend
and cloud provider combinations.

For `backend: KServe` with `cluster.provider: GKE`, the function composes:

1. **A `GKECluster` XR** that provisions the GKE cluster, GPU node pools, and
   outputs a ProviderConfig (provider-kubernetes + provider-helm) for targeting
   the cluster.

2. **A `KServeStack` XR** that consumes the GKECluster's ProviderConfig and
   installs the full KServe dependency chain on the cluster:

   1. **cert-manager** — TLS certificate management
   2. **Gateway API CRDs** — Kubernetes routing primitives
   3. **Envoy Gateway** — Gateway API implementation
   4. **Envoy AI Gateway** — AI-specific traffic management (token rate
      limiting, model routing)
   5. **LeaderWorkerSet** — Multi-node pod coordination
   6. **KServe LLMInferenceService CRDs and controller** — The inference
      control plane

   KServe provides a single install script
   (`llmisvc-full-install-with-manifests.sh`) and Helm charts
   (`kserve-llmisvc-crd`, `kserve-llmisvc-resources`) for deployment.
   Modelplane bootstraps these via `provider-helm`.

3. **Environment-level Kubernetes objects** on the provisioned cluster —
   `LLMInferenceServiceConfig` (platform-wide base engine config),
   `LocalModelNodeGroup` (if `modelCache.enabled`), and RBAC grants.

The `GKECluster` and `KServeStack` XRs are internal implementation details —
they have their own XRDs, Compositions, and composition functions (in the
`modelplane-infra` repo), but they're not part of Modelplane's public API.

The env function also:
- Projects `spec.backend` as a well-known label on the InferenceEnvironment XR
  (e.g., `modelplane.ai/backend: KServe`)
- Populates `status.capacity` by reading node and GPU info from the provisioned
  cluster
- Writes `status.resolved` — the spec with defaults filled in (e.g., default
  node pool config when `nodePools` is omitted)

Adding a new cloud provider (EKS) means adding an `EKSCluster` XR with its own
composition function and a new branch in the env function. Adding a new backend
(KubeAI) means adding a `KubeAIStack` XR and a new branch. The
InferenceEnvironment Composition never changes.

### `function-modelplane-model`

Serves both `ClusterModel` and `Model` — the logic is identical since they share
a schema. When either is created, it validates the model source configuration
and updates XR status. Model caching is handled at the InferenceEnvironment
level (via `LocalModelNodeGroup`), not by this function.

### `function-modelplane-deploy`

Handles fan-out from ModelDeployment to ModelPlacements and status aggregation.
Backend-agnostic — stamps placements from ModelDeployment's `modelRef` plus a
resolved environment reference.

When a `ModelDeployment` XR is created, it:

1. **Resolves target InferenceEnvironments.** If `spec.environmentSelector` is
   specified, evaluates it against InferenceEnvironment labels. If omitted,
   matches model requirements (GPU count, VRAM from the referenced Model's
   `spec.resources`) and engine/backend compatibility against all available
   environments. Reads matched environments via required resources.

2. **Composes a `ModelPlacement` XR** for each compatible target environment,
   with `modelRef` and `inferenceEnvironmentRef`. Applies the
   `modelplane.ai/deployment` label for discoverability.

3. **Aggregates status** across ModelPlacements and surfaces the unified
   endpoint URL on the ModelDeployment's status.

### `function-modelplane-placement`

Owns the complete resolution-to-backend pipeline for a single environment. The
only function that knows about specific backends.

When a `ModelPlacement` XR is created, it:

1. **Reads the referenced Model** (ClusterModel or Model, based on
   `spec.modelRef.kind`) and the **target InferenceEnvironment** via required
   resources.

2. **Checks engine/backend compatibility.** The Model declares an engine (e.g.,
   vLLM); the InferenceEnvironment declares a backend (e.g., KServe). If the
   backend doesn't support the engine, sets `Compatible: False` and composes
   nothing.

3. **Composes backend-specific resources** on the target cluster using the
   Model's engine config. For KServe, this means an `LLMInferenceService`:

   ```yaml
   apiVersion: serving.kserve.io/v1alpha1
   kind: LLMInferenceService
   metadata:
     name: llama-70b-global
     namespace: ml-team-a
   spec:
     baseRefs:
       - name: modelplane-base-config
     model:
       uri: hf://meta-llama/Llama-3.1-70B-Instruct
       name: meta-llama/Llama-3.1-70B-Instruct
     replicas: 3
     parallelism:
       tensor: 4
     template:
       containers:
         - name: main
           image: vllm/vllm-openai:v0.16.0
           args:
             - --max-model-len=32768
             - --enable-prefix-caching
             - --gpu-memory-utilization=0.9
           resources:
             limits:
               nvidia.com/gpu: "4"
               cpu: "16"
               memory: 128Gi
     router:
       gateway: {}
       route: {}
       scheduler: {}
   ```

   vLLM-specific engine config fields from the Model are translated to vLLM CLI
   args: `maxModelLen: 32768` → `--max-model-len=32768`, `prefixCaching: true`
   → `--enable-prefix-caching`, `gpuMemoryUtilization: 0.9` →
   `--gpu-memory-utilization=0.9`. This translation is engine-specific and
   contained within this function.

4. **Optionally composes KEDA ScaledObject** and Prometheus ServiceMonitor for
   autoscaling.

5. **Reports resolved config in `status.resolvedEngine`.**

6. **Maps backend resource status** onto ModelPlacement's uniform status
   conditions. The Ready, ModelCached, and EndpointAvailable conditions are
   derived from the LLMInferenceService's status.

---

## Cross-resource references and required resources

The composition functions rely on Crossplane v2's **required resources**
mechanism to read across XR boundaries:

- `function-modelplane-deploy` requests InferenceEnvironment resources (by label
  selector or all, for compatibility matching) and the referenced Model (for
  resource requirements).
- `function-modelplane-placement` requests the referenced ClusterModel or Model
  (based on `spec.modelRef.kind`) and the target InferenceEnvironment.
- `function-modelplane-model` does not require cross-resource reads.
- `function-modelplane-env` reads observed state of composed GKECluster and
  KServeStack XRs.

This uses Crossplane v2.2's bootstrap requirements in the Composition YAML:

```yaml
pipeline:
  - step: compose-placements
    functionRef:
      name: function-modelplane-deploy
    requirements:
      requiredResources:
        # InferenceEnvironments resolved dynamically by the function
        # from spec.environmentSelector
```

For dynamically-named resources, the function returns requirements in the
`RunFunctionResponse` and Crossplane re-invokes with the resolved resources (up
to 5 iterations). The placement function's two required resource reads (Model
and InferenceEnvironment) have known names from its own spec, so they resolve in
a single iteration.

---

## RBAC ClusterRoles

Crossplane v2 can compose any Kubernetes resource, but needs RBAC grants for
non-Crossplane types. Modelplane ships ClusterRoles with the
`rbac.crossplane.io/aggregate-to-crossplane: "true"` label to grant Crossplane
access to:

- KServe CRDs (`serving.kserve.io/*`)
- Gateway API CRDs (`gateway.networking.k8s.io/*`)
- Gateway API Inference Extension CRDs (`inference.networking.k8s.io/*`,
  `inference.networking.x-k8s.io/*`)
- LeaderWorkerSet CRDs (`leaderworkerset.x-k8s.io/*`)
- KEDA CRDs (`keda.sh/*`) — for autoscaling
- Core resources composed directly (ConfigMaps, Secrets, Services)

---

## Provider dependencies

Modelplane depends on existing Crossplane providers:

- **`provider-gcp`** — for provisioning GKE clusters and GPU node pools (used by
  the `GKECluster` XR's composition function)
- **`provider-kubernetes`** (v1.2.1+) — for creating KServe resources
  (LLMInferenceService, LocalModelCache, etc.) on provisioned clusters via
  ProviderConfig + kubeconfig
- **`provider-helm`** (v1.2.0+) — for installing KServe and its dependencies as
  Helm releases on provisioned clusters

These are declared as dependencies in the Configuration package's
`crossplane.yaml`, not bundled.

---

## Developer workflow

```bash
# Install Modelplane on your Crossplane control plane
crossplane xpkg install configuration xpkg.crossplane.io/modelplane/modelplane:v0.1.0

# Platform team: define an environment (cluster-scoped)
kubectl apply -f environment-gpu-cluster.yaml

# Platform team: register a model in the catalog (cluster-scoped)
kubectl apply -f clustermodel-llama-70b.yaml

# ML team: deploy the model (namespaced)
kubectl apply -f - <<EOF
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: llama-70b-production
  namespace: ml-team-a
spec:
  modelRef:
    kind: ClusterModel
    name: llama-3.1-70b-instruct-vllm
  environmentSelector:
    matchLabels:
      modelplane.ai/environment: gpu-cluster-us-east
EOF

# ML team: check deployment status
kubectl get modeldeployment llama-70b-production -n ml-team-a
# → READY  PLACEMENTS  ENDPOINT
# → True   1/1         https://llama-70b-production.inference.example.com

# ML team: inspect per-environment status
kubectl get modelplacements -n ml-team-a
# → NAME                          ENVIRONMENT          READY
# → llama-70b-production-us-east  gpu-cluster-us-east  True

# ML team: use the unified endpoint
curl https://llama-70b-production.inference.example.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "meta-llama/Llama-3.1-70B-Instruct", "messages": [{"role": "user", "content": "Hello"}]}'
```

The `crossplane render` command provides local composition testing without a
cluster. `up composition render` provides the same capability in the Upbound
toolchain.
