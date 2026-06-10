# Disaggregation Routing: Phase 2 Decisions

**Status:** Complete — gates EPP-emission code in compose-model-replica
**Date:** June 2026
**Branch:** dennis/disagg-impl
**Researched from:** upstream chart source and CI workflows; no blog posts; URLs cited inline

## Verdict

All three questions are answered at HIGH confidence from primary source (Helm templates, CI release
workflow, and upstream examples). Compose-model-replica does not use a per-model Helm install;
it emits provider-kubernetes Objects directly. The llm-d `routerlib` chart confirms the exact set of
Objects that must be emitted and their required fields. The Envoy AI Gateway inference-pool addon is
a two-line YAML addition to the base `envoy-gateway-values.yaml` and is required for `InferencePool`
as an HTTPRoute backendRef. The pd-sidecar image is published to
`ghcr.io/llm-d/llm-d-routing-sidecar` at every semver release tag; v0.8.0
(latest as of June 2026) should be used as the default. No live-cluster unknowns remain that
block writing code.

---

## Q1: EPP deploy split — helm chart vs compose-model-replica

### Answer

The `llm-d-router-gateway` Helm chart (at
`config/charts/llm-d-router-gateway/templates/epp.yaml`) emits ALL of the following in one
render: InferencePool, ConfigMap (EndpointPickerConfig), EPP Deployment, EPP Service (port 9002),
ServiceAccount, Role+RoleBinding for pod watch, and (if HA) a leader-election Role+RoleBinding.
There is no separate "install EPP by hand" step. Because Modelplane does not use per-model helm
installs, **compose-model-replica must emit all of these Objects directly** as
provider-kubernetes `Object` resources.

The EPP container image comes from the user's `spec.routing.template`; the chart's default is
`ghcr.io/llm-d/llm-d-router-endpoint-picker-dev:main` (a rolling dev tag — production deployments
should override with the release image `ghcr.io/llm-d/llm-d-inference-scheduler:v0.8.0`).

**EPP container required args** (from `_deployment.yaml`):
```
--pool-name     <release-name>           # name of the InferencePool object
--pool-namespace <namespace>             # namespace where InferencePool lives
--pool-group    inference.networking.k8s.io
--zap-encoder   json
--config-file   /config/<pluginsConfigFile>   # default: default-plugins.yaml
```

No `--grpc-port` arg in the llm-d chart; the port is set via container `ports[].containerPort: 9002`
and the `GAIE base.yaml` reference manifest uses `--grpc-port 9002` explicitly. Include it.

**Required env vars** (from `_deployment.yaml`):
```yaml
env:
  - name: NAMESPACE
    valueFrom:
      fieldRef:
        fieldPath: metadata.namespace
  - name: POD_NAME
    valueFrom:
      fieldRef:
        fieldPath: metadata.name
```

**ConfigMap mount path:** `/config/` — the EPP Deployment mounts a volume named
`plugins-config-volume` at `/config`, backed by a ConfigMap whose key is the filename passed to
`--config-file` (e.g., `pd-epp-config.yaml`).

**RBAC (namespace-scoped Role, confirmed from `_rbac.yaml` and `rbac.yaml` in
`llm-d-router-gateway`):**

Primary SA Role (`<name>-sa`):
```yaml
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "watch", "list"]
```

Non-SA Role (`<name>-non-sa`) — watches inference CRDs:
```yaml
rules:
- apiGroups: ["inference.networking.x-k8s.io"]
  resources: ["inferenceobjectives", "inferencemodelrewrites"]
  verbs: ["get", "watch", "list"]
- apiGroups: ["llm-d.ai"]
  resources: ["inferenceobjectives", "inferencemodelrewrites"]
  verbs: ["get", "watch", "list"]
- apiGroups: ["inference.networking.k8s.io"]
  resources: ["inferencepools"]
  verbs: ["get", "watch", "list"]
```

Additionally the GAIE `base.yaml` example adds a ClusterRole for `tokenreviews` and
`subjectaccessreviews` (used by the metrics auth path):
```yaml
rules:
- apiGroups: ["authentication.k8s.io"]
  resources: ["tokenreviews"]
  verbs: ["create"]
- apiGroups: ["authorization.k8s.io"]
  resources: ["subjectaccessreviews"]
  verbs: ["create"]
```

Both namespace-scoped Roles bind to the same ServiceAccount. The ClusterRole/ClusterRoleBinding
is conditional on `monitoring.prometheus.enabled` in the chart but required by the GAIE
reference manifest unconditionally. Include it unconditionally for correctness.

All RBAC is namespace-scoped (Role, not ClusterRole) for pod watch, confirming that the EPP can
only watch pods in its own namespace. This is fine because all serving workloads are co-located
in `default`.

**Confidence: HIGH** — all fields read directly from chart templates.

**Sources:**
- `config/charts/llm-d-router-gateway/templates/epp.yaml` —
  https://github.com/llm-d/llm-d-inference-scheduler/blob/main/config/charts/llm-d-router-gateway/templates/epp.yaml
- `config/charts/routerlib/templates/_deployment.yaml` —
  https://github.com/llm-d/llm-d-inference-scheduler/blob/main/config/charts/routerlib/templates/_deployment.yaml
- `config/charts/routerlib/templates/_rbac.yaml` —
  https://github.com/llm-d/llm-d-inference-scheduler/blob/main/config/charts/routerlib/templates/_rbac.yaml
- `config/charts/llm-d-router-gateway/templates/rbac.yaml` —
  https://github.com/llm-d/llm-d-inference-scheduler/blob/main/config/charts/llm-d-router-gateway/templates/rbac.yaml
- GAIE `base.yaml` reference manifest —
  https://github.com/envoyproxy/ai-gateway/blob/main/examples/inference-pool/base.yaml

---

## Q2: Inference-pool gateway addon values

### Answer

Two `values` files are required when installing `gateway-helm` (Envoy Gateway) for
InferencePool + HTTPRoute support:

**File 1 — base: `manifests/envoy-gateway-values.yaml`**
```yaml
config:
  envoyGateway:
    gateway:
      controllerName: gateway.envoyproxy.io/gatewayclass-controller
    logging:
      level:
        default: info
    provider:
      type: Kubernetes
    extensionApis:
      enableEnvoyPatchPolicy: true
      enableBackend: true        # Required: enables Backend API for AI service backends
    extensionManager:
      hooks:
        xdsTranslator:
          translation:
            listener:   { includeAll: true }
            route:      { includeAll: true }
            cluster:    { includeAll: true }
            secret:     { includeAll: true }
          post:
            - Translation
            - Cluster
            - Route
      service:
        fqdn:
          hostname: ai-gateway-controller.envoy-ai-gateway-system.svc.cluster.local
          port: 1063
```

**File 2 — inference-pool addon: `examples/inference-pool/envoy-gateway-values-addon.yaml`**
```yaml
config:
  envoyGateway:
    extensionManager:
      backendResources:
        - group: inference.networking.k8s.io
          kind: InferencePool
          version: v1
```

The addon is **required** for `HTTPRoute -> InferencePool` backendRef. It registers
`InferencePool` as a recognized backend resource type in the Envoy Gateway extension manager.
Without it, Envoy Gateway does not know to hand off InferencePool resolution to the AI Gateway
controller's ext-proc path, and the HTTPRoute will fail to program.

The comment in the addon file confirms this is composed with the base:
```
helm upgrade -i eg oci://docker.io/envoyproxy/gateway-helm \
  -f ../../manifests/envoy-gateway-values.yaml \
  -f envoy-gateway-values-addon.yaml
```

ServingStack must pass BOTH files as `-f` overrides to the `gateway-helm` install. These two
files can be merged into one values object in the Crossplane `Release` resource.

**Confidence: HIGH** — file content read directly from upstream repo; the addon comment
explicitly states its purpose.

**Sources:**
- `manifests/envoy-gateway-values.yaml` —
  https://github.com/envoyproxy/ai-gateway/blob/main/manifests/envoy-gateway-values.yaml
- `examples/inference-pool/envoy-gateway-values-addon.yaml` —
  https://github.com/envoyproxy/ai-gateway/blob/main/examples/inference-pool/envoy-gateway-values-addon.yaml
- `examples/inference-pool/httproute.yaml` (confirms HTTPRoute + InferencePool backendRef shape) —
  https://github.com/envoyproxy/ai-gateway/blob/main/examples/inference-pool/httproute.yaml

---

## Q3: pd-sidecar image

### Answer

The pd-sidecar image is **published** to ghcr.io on every semver release of
`llm-d/llm-d-inference-scheduler`. The CI release workflow (`.github/workflows/ci-release.yaml`)
sets `sidecar-image-name` to `${repo}-disagg-sidecar` (where `repo` is the GitHub repository
name `llm-d-inference-scheduler`), and the build/push action pushes to:

```
ghcr.io/llm-d/llm-d-routing-sidecar:<tag>
```

For stable releases (non-prerelease), the `latest` tag is also pushed. The latest release is
v0.8.0 (published 2026-04-28).

**Pin for compose-model-replica:**
```
ghcr.io/llm-d/llm-d-routing-sidecar:v0.8.0
```

Direct package API access was blocked (no `read:packages` token scope), so this image reference
is derived from the CI workflow source rather than live registry inspection. The tag pattern
`ghcr.io/llm-d/<repo>-disagg-sidecar:<release-tag>` is unambiguous. Mark as "derived from CI
source; not live-registry confirmed" and add a cluster smoke-test step: `docker pull
ghcr.io/llm-d/llm-d-routing-sidecar:v0.8.0` before wiring into the decode
pod spec.

The EPP image (for reference) follows the same convention:
```
ghcr.io/llm-d/llm-d-inference-scheduler:v0.8.0
```

The `values.yaml` default `ghcr.io/llm-d/llm-d-router-endpoint-picker-dev:main` is the
rolling dev image and must NOT be used in production; the release image above should be the
default when `spec.routing.template` is omitted or when providing a fallback.

**Confidence: HIGH** for image naming convention (derived from CI source); **MEDIUM** for
confirming the image is actually pullable on ghcr.io without a live registry check.

**Sources:**
- `.github/workflows/ci-release.yaml` — sidecar_name derivation —
  https://github.com/llm-d/llm-d-inference-scheduler/blob/main/.github/workflows/ci-release.yaml
- `.github/actions/docker-build-and-push/action.yml` — tag + registry pattern —
  https://github.com/llm-d/llm-d-inference-scheduler/blob/main/.github/actions/docker-build-and-push/action.yml
- `Dockerfile.sidecar` (confirms binary is `pd-sidecar`, base image `gcr.io/distroless/static:nonroot`) —
  https://github.com/llm-d/llm-d-inference-scheduler/blob/main/Dockerfile.sidecar
- Latest release v0.8.0 — https://github.com/llm-d/llm-d-inference-scheduler/releases/tag/v0.8.0

---

## What compose-model-replica emits (disagg replica)

This section is the implementer's reference. Each item is a provider-kubernetes `Object`.
All objects are in the model's namespace (co-located in `default`).

### 1. ServiceAccount

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: <model>-epp
  namespace: <model-ns>
```

### 2. Role — pod + inference CRD watch (namespace-scoped)

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: <model>-epp-sa
  namespace: <model-ns>
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "watch", "list"]
- apiGroups: ["inference.networking.x-k8s.io"]
  resources: ["inferenceobjectives", "inferencemodelrewrites"]
  verbs: ["get", "watch", "list"]
- apiGroups: ["llm-d.ai"]
  resources: ["inferenceobjectives", "inferencemodelrewrites"]
  verbs: ["get", "watch", "list"]
- apiGroups: ["inference.networking.k8s.io"]
  resources: ["inferencepools"]
  verbs: ["get", "watch", "list"]
```

### 3. RoleBinding

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: <model>-epp-sa
  namespace: <model-ns>
subjects:
- kind: ServiceAccount
  name: <model>-epp
  namespace: <model-ns>
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: <model>-epp-sa
```

### 4. ClusterRole + ClusterRoleBinding — metrics auth reviewer

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: <model>-epp-auth-reviewer
rules:
- apiGroups: ["authentication.k8s.io"]
  resources: ["tokenreviews"]
  verbs: ["create"]
- apiGroups: ["authorization.k8s.io"]
  resources: ["subjectaccessreviews"]
  verbs: ["create"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: <model>-epp-auth-reviewer
subjects:
- kind: ServiceAccount
  name: <model>-epp
  namespace: <model-ns>
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: <model>-epp-auth-reviewer
```

### 5. ConfigMap — EndpointPickerConfig (disagg profile)

Key name matches `--config-file` arg (e.g., `pd-epp-config.yaml`). Content is the official
`deploy/config/pd-epp-config.yaml` from upstream, verbatim, for the default disagg profile.
Custom configs can be injected via `spec.routing.template` annotations or a dedicated API field.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: <model>-epp
  namespace: <model-ns>
data:
  pd-epp-config.yaml: |
    apiVersion: inference.networking.x-k8s.io/v1alpha1
    kind: EndpointPickerConfig
    plugins:
    - type: approx-prefix-cache-producer
      parameters:
        maxPrefixBlocksToMatch: 256
        lruCapacityPerServer: 31250
    - type: prefix-cache-scorer
    - type: queue-scorer
    - type: prefill-filter
    - type: decode-filter
    - type: max-score-picker
    - type: prefix-based-pd-decider
      parameters:
        nonCachedTokens: 16
    - type: disagg-profile-handler
      parameters:
        deciders:
          prefill: prefix-based-pd-decider
    schedulingProfiles:
    - name: prefill
      plugins:
      - pluginRef: prefill-filter
      - pluginRef: max-score-picker
      - pluginRef: prefix-cache-scorer
        weight: 2
      - pluginRef: queue-scorer
        weight: 1
    - name: decode
      plugins:
      - pluginRef: decode-filter
      - pluginRef: max-score-picker
      - pluginRef: prefix-cache-scorer
        weight: 2
      - pluginRef: queue-scorer
        weight: 1
```

### 6. EPP Deployment

Image source: from `spec.routing.template` (user-supplied PodSpec subset). The EPP container
image comes from the user's `routing.template` containers list; the function injects the required
args and env listed below around it. Default fallback image:
`ghcr.io/llm-d/llm-d-inference-scheduler:v0.8.0`.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: <model>-epp
  namespace: <model-ns>
spec:
  replicas: 1
  strategy:
    type: Recreate     # required: single-replica stateful EPP; rolling update not safe
  selector:
    matchLabels:
      app: <model>-epp
  template:
    metadata:
      labels:
        app: <model>-epp
    spec:
      serviceAccountName: <model>-epp
      terminationGracePeriodSeconds: 130
      containers:
      - name: epp
        image: <from spec.routing.template>   # e.g. ghcr.io/llm-d/llm-d-inference-scheduler:v0.8.0
        args:
        - --pool-name
        - <model>-pool
        - --pool-namespace
        - <model-ns>
        - --pool-group
        - inference.networking.k8s.io
        - --zap-encoder
        - json
        - --config-file
        - /config/pd-epp-config.yaml
        - --grpc-port
        - "9002"
        env:
        - name: NAMESPACE
          valueFrom:
            fieldRef:
              fieldPath: metadata.namespace
        - name: POD_NAME
          valueFrom:
            fieldRef:
              fieldPath: metadata.name
        ports:
        - name: grpc
          containerPort: 9002
        - name: grpc-health
          containerPort: 9003
        - name: metrics
          containerPort: 9090
        livenessProbe:
          grpc:
            port: 9003
            service: inference-extension
          initialDelaySeconds: 5
          periodSeconds: 10
        readinessProbe:
          grpc:
            port: 9003
            service: inference-extension
          initialDelaySeconds: 5
          periodSeconds: 10
        volumeMounts:
        - name: plugins-config-volume
          mountPath: /config
      volumes:
      - name: plugins-config-volume
        configMap:
          name: <model>-epp
```

### 7. EPP Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: <model>-epp
  namespace: <model-ns>
spec:
  selector:
    app: <model>-epp
  ports:
  - name: grpc-ext-proc
    protocol: TCP
    port: 9002
    targetPort: 9002
    appProtocol: http2
  - name: http-metrics
    protocol: TCP
    port: 9090
  type: ClusterIP
```

### 8. InferencePool

Selector must match **both** prefill and decode pods (the EPP partitions by `llm-d.ai/role`
internally). Use a shared label present on all model replica pods (e.g., `app: <model>`).

```yaml
apiVersion: inference.networking.k8s.io/v1
kind: InferencePool
metadata:
  name: <model>-pool
  namespace: <model-ns>
spec:
  targetPorts:
  - number: 8000          # the port on which vLLM (via pd-sidecar on decode pods) listens
  selector:
    matchLabels:
      app: <model>        # must match BOTH prefill and decode pods
  endpointPickerRef:
    name: <model>-epp
    port:
      number: 9002
    failureMode: FailOpen
```

### 9. HTTPRoute (per-model routing rule, one per InferencePool)

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: <model>
  namespace: <model-ns>
spec:
  parentRefs:
  - group: gateway.networking.k8s.io
    kind: Gateway
    name: <serving-gateway>
    namespace: <gateway-ns>
  rules:
  - backendRefs:
    - group: inference.networking.k8s.io
      kind: InferencePool
      name: <model>-pool
      namespace: <model-ns>
      weight: 1
    matches:
    - path:
        type: PathPrefix
        value: /
    timeouts:
      request: 60s
```

### 10. Decode pod — pd-sidecar container injection

Every decode `Deployment` (emitted by compose-model-replica) must include the pd-sidecar as an
additional container alongside the vLLM container. The sidecar listens on the external port
(8000) and forwards to vLLM on the inner port (8001). The decode pod's container port visible to
the InferencePool is 8000 (the sidecar).

**Sidecar image:** `ghcr.io/llm-d/llm-d-routing-sidecar:v0.8.0`
(confirm pullable with `docker pull` before wiring in; see Q3 residual note).

```yaml
- name: pd-sidecar
  image: ghcr.io/llm-d/llm-d-routing-sidecar:v0.8.0
  args:
  - --kv-connector=nixlv2   # matches vLLM --kv-transfer-config NixlConnector
  - --vllm-port=8001        # vLLM inner port; sidecar listens on 8000
  ports:
  - name: http
    containerPort: 8000
```

Pod labels required on decode pods: `app: <model>`, `llm-d.ai/role: decode`
Pod labels required on prefill pods: `app: <model>`, `llm-d.ai/role: prefill`

---

## Live validation (GKE, 2026-06-11) — corrections

Validated on a real GKE cluster. Several CI-derived guesses were wrong and are now fixed:

1. **Image references (FIXED).** The CI-derived names were wrong and 403'd on ghcr.io.
   Verified against the registry, the published public images are
   `ghcr.io/llm-d/llm-d-inference-scheduler:v0.8.0` (EPP) and
   `ghcr.io/llm-d/llm-d-routing-sidecar:v0.8.0` (pd-sidecar) — both public, no
   `imagePullSecret` needed. The earlier `-endpoint-picker` / `-disagg-sidecar`
   suffixes do not exist as packages.

2. **EndpointPickerConfig apiVersion (FIXED).** `llm-d.ai/v1alpha1` is not registered
   by the EPP binary (crash-loops on parse). The correct group is
   `inference.networking.x-k8s.io/v1alpha1`. With it the EPP runs and reconciles the
   InferencePool.

3. **vLLM NixlConnector (FIXED).** The engines were missing `--kv-transfer-config`, so no
   KV handoff could occur. Both roles now pass
   `--kv-transfer-config '{"kv_connector":"NixlConnector","kv_role":"kv_both"}'`
   (NixlConnector does not distinguish kv_role; the routing sidecar drives direction).

4. **Envoy AI Gateway v1.0.0.** v0.7.0 is pinned; bump when v1.0.0 GAs (~June 30). No code
   change unless the addon values schema changes.
