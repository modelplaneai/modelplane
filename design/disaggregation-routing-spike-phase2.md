# Disaggregation Routing Spike: Phase 2

**Status:** Complete
**Date:** June 2026
**Branch:** docs-preview

## Verdict

All three questions are now confirmed at high confidence from primary sources (llm-d
upstream Go source, Envoy AI Gateway docs/source, GAIE manifests). The EPP header
is `x-prefiller-host-port` (confirmed in `pkg/common/routing/common.go`). The EPP
discovers both prefill and decode pods through a **single** `InferencePool` that
selects all pods sharing a common label; within the EPP, `prefill-filter` and
`decode-filter` plugins partition the set using the `llm-d.ai/role` label. Envoy AI
Gateway v0.7.0 (released June 4, 2026, targeting v1.0 GA by June 30, 2026) supports
`InferencePool` as an HTTPRoute backendRef and runs **on top of** core Envoy Gateway
(`gateway-helm`), adding two additional charts (`ai-gateway-crds-helm`,
`ai-gateway-helm`). Phase 2 requires replacing the ServingStack's current standalone
`gateway-helm` install with a three-chart stack.

---

## Question 1: Prefill header and handoff component

**Confirmed header name: `x-prefiller-host-port`**

The constant is defined in
`pkg/common/routing/common.go` in
`github.com/llm-d/llm-d-inference-scheduler`:

```go
// PrefillEndpointHeader is the header name used to indicate Prefill worker <ip:port>
PrefillEndpointHeader = "x-prefiller-host-port"
```

The value is in `host:port` format (e.g., `10.0.0.5:8000`).

The EPP's `disagg-profile-handler` sets this header via the `PreRequest` hook
in `disagg_headers_handler.go`: it picks the prefill pod from the prefill
scheduling profile's result and writes `net.JoinHostPort(addr, port)` into
`request.Headers[routing.PrefillEndpointHeader]`.

The component that reads the header and performs the handoff is the
**pd-sidecar**, a reverse proxy that runs as a sidecar container on every
**decode** pod. The sidecar was previously in the standalone repo
`github.com/llm-d/llm-d-routing-sidecar`, which is now deprecated and marked
for archival. The code lives in the main inference-scheduler repo under
`cmd/pd-sidecar/` and `pkg/sidecar/`. On receiving a request whose
`x-prefiller-host-port` header is set, the sidecar forwards it to the named
prefill pod to perform remote prefill, receives the KV block IDs back, then
sends the decode request to the local vLLM instance with the KV transfer
parameters. If the header is absent, the decode pod runs both prefill and
decode locally.

The sidecar is **not** an init container and is not in-engine. It runs as a
sidecar container on the decode pod, listening on a port in front of vLLM
(default 8000), with vLLM listening on an inner port (default 8001). The sidecar
binary is built from `Dockerfile.sidecar` in the inference-scheduler repo.

**Confidence: HIGH**. Header name confirmed from source; sidecar migration
confirmed from repo README deprecation notice and directory structure.

Sources:
- `pkg/common/routing/common.go` — https://github.com/llm-d/llm-d-inference-scheduler/blob/main/pkg/common/routing/common.go
- `pkg/epp/framework/plugins/scheduling/profilehandler/disagg/disagg_headers_handler.go` — https://github.com/llm-d/llm-d-inference-scheduler/blob/main/pkg/epp/framework/plugins/scheduling/profilehandler/disagg/disagg_headers_handler.go
- `cmd/pd-sidecar/` — https://github.com/llm-d/llm-d-inference-scheduler/tree/main/cmd/pd-sidecar
- Archived sidecar repo — https://github.com/llm-d/llm-d-routing-sidecar

---

## Question 2: Prefill pod discovery and label convention

**Discovery mechanism: single InferencePool watches all pods; label filters
partition within the EPP.**

The EPP is started with `--pool-name` and `--pool-namespace` pointing at a
single `InferencePool`. By default, the EPP reconciler watches the
`InferencePool`'s `spec.selector.matchLabels` to enumerate all matching pods and
builds an endpoint datastore from them. For P/D disaggregation, the
`InferencePool` selector must be broad enough to match **both** prefill and decode
pods (e.g., `app: my-model`). The EPP then uses `prefill-filter` and
`decode-filter` scheduling plugins to split the full pod set into the prefill
and decode pools at scheduling time.

The native llm-d label convention is:

| Label key        | Values                                              |
|------------------|-----------------------------------------------------|
| `llm-d.ai/role`  | `prefill`, `decode`, `encode`, `prefill-decode`, `encode-prefill`, `encode-prefill-decode` |

The `prefill-filter` plugin accepts pods whose `llm-d.ai/role` is `prefill`,
`encode-prefill`, `prefill-decode`, or `encode-prefill-decode`. The `decode-filter`
accepts `decode`, `prefill-decode`, `encode-prefill-decode`, and the deprecated
`both`. Both are implemented in
`pkg/epp/framework/plugins/scheduling/filter/bylabel/roles.go`.

The reference deployment manifest (`deploy/components/vllm-prefill/deployment.yaml`)
labels prefill pods with both `llm-d.ai/component: prefill` and
`llm-d.ai/role: prefill`. The InferencePool selector uses a shared label like
`llm-d.ai/inference-serving: "true"` or `app: <pool-name>` that covers all pods.

**Implications for Modelplane's `modelplane.ai/pd-role` label:** the llm-d EPP
does not use `modelplane.ai/pd-role`; it only reads `llm-d.ai/role`. If
Modelplane uses the built-in `prefill-filter`/`decode-filter` plugins (which is
the simplest path), pods must carry `llm-d.ai/role: prefill` or
`llm-d.ai/role: decode`. Alternatively, the `label-selector-filter` plugin
(generic Kubernetes selector syntax) can be configured to read any label key,
including `modelplane.ai/pd-role`. The disagreement docs explicitly describe
this as the supported path for external workloads with different labeling
conventions. Using `label-selector-filter` with `modelplane.ai/pd-role` avoids
touching Pod labels but requires a custom `EndpointPickerConfig`.

**Remaining unknown:** it is not confirmed in source whether the EPP RBAC
(pod watch) must be in the same namespace as the InferencePool. The e2e
manifest uses a namespace-scoped Role rather than ClusterRole, implying both
pools must be co-located. Cross-namespace prefill discovery is unconfirmed.

**Confidence: HIGH** for single-pool + label-filter mechanism; **MEDIUM** for
cross-namespace assumptions.

Sources:
- `pkg/epp/framework/plugins/scheduling/filter/bylabel/roles.go` — https://github.com/llm-d/llm-d-inference-scheduler/blob/main/pkg/epp/framework/plugins/scheduling/filter/bylabel/roles.go
- `deploy/components/vllm-prefill/deployment.yaml` — https://github.com/llm-d/llm-d-inference-scheduler/blob/main/deploy/components/vllm-prefill/deployment.yaml
- `test/sidecar/config/nixl/inferencepool.yaml` — https://github.com/llm-d/llm-d-inference-scheduler/blob/main/test/sidecar/config/nixl/inferencepool.yaml
- `docs/disaggregation.md` — https://github.com/llm-d/llm-d-inference-scheduler/blob/main/docs/disaggregation.md
- `deploy/config/sim-pd-epp-config.yaml` — https://github.com/llm-d/llm-d-inference-scheduler/blob/main/deploy/config/sim-pd-epp-config.yaml

---

## Question 3: Envoy AI Gateway InferencePool maturity (mid-2026)

**Latest release: v0.7.0, released June 4, 2026.**

The roadmap tracking issue (envoyproxy/ai-gateway#2083) targets v1.0.0 GA by
June 30, 2026, with v1.0.0-rc1 on June 12, 2026. As of today (June 10, 2026),
v0.7.0 is the latest stable release; v1.0.0 has not shipped yet. The v0.6.0
CRDs were promoted to v1beta1.

**InferencePool support:** Envoy AI Gateway supports `InferencePool`
(`inference.networking.k8s.io/v1`) as a backendRef in both `HTTPRoute` and
`AIGatewayRoute`. Support was introduced at v0.3.0 (August 2025) and updated to
GAIE v1.0 at v0.4.0 (November 2025). The install requires the GAIE manifests
(CRDs + EPP controller) to be applied separately:

```
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/v1.0.1/manifests.yaml
```

**InferencePool as HTTPRoute backendRef is functioning in v0.7.0.** The
`examples/inference-pool/httproute.yaml` in the ai-gateway repo demonstrates
this pattern with `group: inference.networking.k8s.io`, `kind: InferencePool`
as a backendRef. However, the Envoy AI Gateway project does not yet designate
any capability as formally "GA/stable" at v0.7.0; v1.0 is the first stability
milestone. Treat InferencePool support as "production-quality beta" for the
Phase 2 pin.

**Install model: Envoy AI Gateway runs ON TOP of Envoy Gateway.** The install
sequence is four steps:

1. Gateway API CRDs (already in Modelplane's ServingStack)
2. `oci://docker.io/envoyproxy/gateway-helm` — core Envoy Gateway, with
   specific values from `manifests/envoy-gateway-values.yaml` in the
   ai-gateway repo that enable `extensionManager` hooks and the `Backend` API.
   This replaces the plain `gateway-helm` install in the current ServingStack.
3. `oci://docker.io/envoyproxy/ai-gateway-crds-helm` — AI Gateway CRDs
   (namespace: `envoy-ai-gateway-system`)
4. `oci://docker.io/envoyproxy/ai-gateway-helm` — AI Gateway controller
   (namespace: `envoy-ai-gateway-system`)

The `gateway-helm` install in step 2 requires overrides to activate the
`extensionManager` pointing to the AI Gateway controller service:

```yaml
config.envoyGateway.extensionManager.service.fqdn.hostname:
  ai-gateway-controller.envoy-ai-gateway-system.svc.cluster.local
config.envoyGateway.extensionApis.enableBackend: true
```

For InferencePool support specifically, an additional values addon
(`examples/inference-pool/envoy-gateway-values-addon.yaml`) may be required
to enable the ext-proc extension needed by the EPP.

**GatewayClass controllerName:** `gateway.envoyproxy.io/gatewayclass-controller`
(unchanged from core Envoy Gateway). Envoy AI Gateway does not introduce a
separate GatewayClass; it extends the existing one via the extensionManager hook.

**Confidence: HIGH** for release version, install model, GatewayClass, and
InferencePool backendRef functionality; **MEDIUM** for stability designation
(v1.0 has not shipped as of today).

Sources:
- Release list — https://aigateway.envoyproxy.io/release-notes/
- v1.0 GA roadmap — https://github.com/envoyproxy/ai-gateway/issues/2083
- HTTPRoute + InferencePool example — https://github.com/envoyproxy/ai-gateway/blob/main/examples/inference-pool/httproute.yaml
- Installation guide — https://github.com/envoyproxy/ai-gateway/blob/main/site/docs/getting-started/installation.md
- envoy-gateway-values.yaml — https://github.com/envoyproxy/ai-gateway/blob/main/manifests/envoy-gateway-values.yaml
- InferencePool example README — https://github.com/envoyproxy/ai-gateway/blob/main/examples/inference-pool/README.md
- GAIE v1.0.1 manifests — https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/v1.0.1/manifests.yaml

---

## Backend shape for Phase 2

### compose-serving-stack must install

Replace the existing bare `gateway-helm` install with a three-chart stack:

| Chart | Registry path | Namespace | Notes |
|---|---|---|---|
| `gateway-helm` | `oci://docker.io/envoyproxy/gateway-helm` | `envoy-gateway-system` | Must apply `envoy-gateway-values.yaml` overrides and the InferencePool addon values |
| `ai-gateway-crds-helm` | `oci://docker.io/envoyproxy/ai-gateway-crds-helm` | `envoy-ai-gateway-system` | Installs `AIGatewayRoute` and related CRDs |
| `ai-gateway-helm` | `oci://docker.io/envoyproxy/ai-gateway-helm` | `envoy-ai-gateway-system` | Runs the AI Gateway controller; depends on gateway-helm being ready |

Additionally install the GAIE CRDs and EPP controller:

```
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/v1.0.1/manifests.yaml
```

**Version to pin:** `v0.7.0` for all three ai-gateway charts. Do not use
`v0.0.0-latest`; it is overwritten on the registry and is not reproducible.

The GatewayClass `controllerName` remains `gateway.envoyproxy.io/gatewayclass-controller`.

### compose-model-replica llm-d backend must emit (disagg replica)

**InferencePool (decode pods only, or all pods with role-based filter):**

```yaml
apiVersion: inference.networking.k8s.io/v1
kind: InferencePool
metadata:
  name: <model>-decode
spec:
  selector:
    matchLabels:
      app: <model>              # covers both prefill and decode pods
      llm-d.ai/inference-serving: "true"
  targetPorts:
    - number: 8000
  endpointPickerRef:
    name: <model>-epp
    kind: Service
    port:
      number: 9002
```

The InferencePool selector must match **all** pods (prefill + decode). The EPP
partitions them internally using the `EndpointPickerConfig`.

**EPP EndpointPickerConfig (ConfigMap):**

```yaml
apiVersion: llm-d.ai/v1alpha1
kind: EndpointPickerConfig
plugins:
- type: prefill-filter        # selects pods with llm-d.ai/role: prefill
- type: decode-filter         # selects pods with llm-d.ai/role: decode
- type: prefix-cache-scorer
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
- name: decode
  plugins:
  - pluginRef: decode-filter
  - pluginRef: max-score-picker
  - pluginRef: prefix-cache-scorer
```

**Pod labels required on model replica pods:**

Each pod must carry the shared selector label AND the role label:

| Pod type | Required labels |
|---|---|
| Prefill pod | `app: <model>`, `llm-d.ai/inference-serving: "true"`, `llm-d.ai/role: prefill` |
| Decode pod  | `app: <model>`, `llm-d.ai/inference-serving: "true"`, `llm-d.ai/role: decode` |

**Relationship to `modelplane.ai/pd-role`:** Modelplane's existing
`modelplane.ai/pd-role` label is used internally by Crossplane compositions for
replica selection. The EPP does not read it. To use the built-in `prefill-filter`
and `decode-filter` plugins, replicas must additionally carry `llm-d.ai/role`.
Alternatively, replace the built-in filters with `label-selector-filter` plugins
configured to read `modelplane.ai/pd-role` — this avoids adding the llm-d label
but requires explicit EPP config for each model.

**Decode pod sidecar container:**

The pd-sidecar must be injected as a sidecar on every decode pod. The image is
built from `Dockerfile.sidecar` in the inference-scheduler repo. Key flags:

```
--kv-connector=nixlv2           # matches vLLM's --kv-transfer-config NixlConnector
--vllm-port=8001                # vLLM listens here; sidecar listens on 8000
--inference-pool=<ns>/<pool>    # for SSRF allowlisting (optional)
```

**HTTPRoute → InferencePool:**

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
spec:
  parentRefs:
  - kind: Gateway
    name: <serving-gateway>
  rules:
  - backendRefs:
    - group: inference.networking.k8s.io
      kind: InferencePool
      name: <model>-decode
      namespace: <model-ns>
```

---

## Remaining unknowns that block writing routing code

1. **EPP cross-namespace pod watch.** The test manifests use a namespace-scoped
   Role for pod watch. If Modelplane places prefill and decode pods in separate
   namespaces (one per replica), the EPP cannot see both. Confirm whether a single
   InferencePool + EPP can watch pods across namespaces, or whether prefill and
   decode must be co-located in the same namespace.

2. **GAIE EPP deploy model in llm-d helm.** The inference-scheduler's helm chart
   (`config/charts/routerlib`) generates the InferencePool template but it is
   unclear whether it also deploys the EPP Deployment and Service, or whether
   compose-model-replica must emit those directly. Verify what the routerlib chart
   installs vs what the compose function must emit.

3. **Envoy AI Gateway v1.0 stability date.** As of June 10, 2026, v1.0.0-rc1 is
   due June 12 and GA on June 30. The ServingStack version pin should be updated
   to v1.0.0 when it ships. The rc1 is safe to test against but should not be
   used in a production pin.

4. **InferencePool addon Envoy Gateway values.** The ai-gateway inference-pool
   example uses `examples/inference-pool/envoy-gateway-values-addon.yaml` in
   addition to the base `manifests/envoy-gateway-values.yaml`. The exact content
   of that addon and whether it is required for the HTTPRoute + InferencePool
   path (vs only AIGatewayRoute) needs to be verified before finalising the
   ServingStack helm values.
