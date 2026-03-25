# MVP Build Log

## Phase 1: XRDs, compositions, and examples
**Status:** Complete

- Created all 5 XRDs, 5 compositions, and 4 examples.
- Directories: `apis/{inferenceenvironments,clustermodels,models,modelplacements,modeldeployments}/`
- Examples: `examples/{inferenceenvironment,clustermodel,modelplacement,modeldeployment}/`

### Design decision: removed `replicas` from ModelDeployment and ModelPlacement

The MVP spec included a `replicas` field on both ModelDeployment (passed through
to placements) and ModelPlacement (passed to LLMInferenceService). This field
doesn't appear in the design doc.

Replicas control throughput scaling in KServe — each replica is a separate vLLM
pod with the full model loaded, and the inference gateway load-balances across
them. It's a production scaling concern, not relevant to the MVP demo (single
small model, no real traffic).

Decision: removed `replicas` from both XRDs. The compose-model-placement
function will hardcode `replicas: 1` on the LLMInferenceService. This can be
added to the API later when scaling becomes a real concern.

- `up project build` succeeded. Pydantic models generated at
  `.up/python/models/ai/modelplane/{clustermodel,inferenceenvironment,model,modeldeployment,modelplacement}/`.

## Phase 2: compose-model function + KServeStack gateway address change
**Status:** Complete

- Created `functions/compose-model/main.py` — trivial function that validates
  the spec and sets Ready=True. Used by both ClusterModel and Model compositions.
- Updated `functions/compose-kserve-stack/main.py` — added gateway address
  extraction from observed Gateway Object's `status.atProvider.manifest.status.addresses[0].value`.
  Also fixed pre-existing ruff issues (unused imports, ambiguous variable `l`).
- Created `tests/test-cluster-model/main.py`.
- Ruff clean on all new code.

### Root cause: function name vs repository prefix mismatch

`up test run` and `up composition render` both failed with:
```
unknown Function "upbound-modelplane-infracompose-gke-cluster"
```

Root cause: the function name in compositions must match the DNS label derived
from the image repository path. The `up` CLI derives function names via
`xpkg.ToDNSLabel(tag.Context().RepositoryStr())` (see
`internal/render/functions.go` and `internal/xpkg/name.go` in the `up` repo).

`ToDNSLabel` converts `/` to `-` and silently drops `_` (underscores aren't in
its allowed character set). So for an image tagged
`xpkg.upbound.io/negzupboundio/modelplane-infra_compose-gke-cluster:arm64`,
the repository string is `negzupboundio/modelplane-infra_compose-gke-cluster`,
and `ToDNSLabel` produces `negzupboundio-modelplane-infracompose-gke-cluster`.

The compositions were using `upbound-modelplane-infracompose-gke-cluster` —
the `upbound-` prefix came from the spec's naming convention documentation,
but the actual prefix must match the repository owner in `upbound.yaml`
(`spec.repository: xpkg.upbound.io/negzupboundio/modelplane-infra`).

Fix: updated all composition `functionRef.name` values from
`upbound-modelplane-infra...` to `negzupboundio-modelplane-infra...`.
The existing GKECluster and KServeStack compositions also had the wrong prefix
— they must have been working against an older repository path or an older `up`
CLI that resolved names differently.

## Phase 3: compose-inference-env function + test
**Status:** Complete

- Created `functions/compose-inference-env/main.py`.
  - Composes Namespace, GKECluster, and KServeStack in `ie-{name}` namespace.
  - Gates KServeStack on GKECluster secrets availability.
  - Computes GPU capacity from node pool config using static VRAM lookup table.
  - Reads KServeStack gateway address from observed state.
  - Writes `status.providerConfigRef.name`, `status.gateway.address`,
    `status.namespace`, `status.capacity.{backend,gpuPools}`.
- Created `tests/test-inference-env/main.py`.
- Ruff clean.

## Phase 4: compose-model-placement function + test
**Status:** Complete

- Created `functions/compose-model-placement/main.py`.
  - Uses required resources to read ClusterModel/Model and InferenceEnvironment.
  - Computes GPU count from model VRAM / environment pool VRAM.
  - Composes a provider-kubernetes Object wrapping LLMInferenceService.
  - Hardcodes replicas=1 (see Phase 1 design decision).
  - Sets `securityContext.runAsUser: 0` for vLLM compatibility.
  - Includes `router: {gateway: {}, route: {}}` for KServe managed routing.
  - Writes `status.endpoint.url`, `status.model.name`, `status.resources.gpu.count`.
- Created `tests/test-model-placement/main.py`.
- Ruff clean.

## Phase 5: compose-model-deployment function + test
**Status:** Complete

- Created `functions/compose-model-deployment/main.py`.
  - Uses required resources: all InferenceEnvironments, ClusterModel, all
    ModelPlacements (for capacity tracking), and bootstrap Gateway.
  - Scheduler: filters by engine/backend compat, VRAM capacity, label selector,
    readiness. Sorts by name for determinism.
  - Composes ModelPlacement per matched environment.
  - Composes Backend + HTTPRoute on control plane for unified endpoint routing.
  - Backend name resolution: reads Crossplane-generated names from observed
    Backends on second reconcile to populate HTTPRoute backendRefs.
  - Writes `status.endpoint.url`, `status.placements.{total,ready}`,
    `status.model.name`.
- Created `tests/test-model-deployment/main.py`.
- Ruff clean.

## Build verification
**Status:** Complete

- `up project build` succeeds with all 6 functions (2 existing + 4 new).
- Package contains all function images for amd64 and arm64.
- All new code passes ruff checks.
- `up composition render` works for GKECluster after fixing function name prefix.
- All 4 MVP tests pass: cluster-model, inference-env, model-placement,
  model-deployment.

### Missing model symlinks in function directories

Functions need a `model` symlink (`model -> ../../.up/python/models`) to
resolve `.model.*` imports at runtime. The `up function generate` command
creates this automatically, but hand-created function directories don't have
it. Without the symlink the function container crashes on startup with
`ModuleNotFoundError: No module named 'function.model'`.

### XRD defaults and pass-through

The compose-inference-env function initially set explicit defaults for optional
GKECluster fields (diskSizeGb, nodeCount, etc.) using `or` fallbacks. This
caused the function to emit those fields even when the user didn't set them in
the InferenceEnvironment spec. Since the GKECluster XRD has its own defaults,
the function should only pass through fields the user explicitly set and let
the downstream XRD handle defaults. Fixed by checking `is not None` instead of
using `or`.

## E2E deployment
**Status:** In progress

### Fixes applied during e2e

- **URL rewriting in compose-model-deployment**: The HTTPRoute was rewriting
  `/{ns}/{deployment}/` to `/`, but the remote KServe gateway expects
  `/{remote-ns}/{llmis-name}/`. Fixed by:
  1. Setting deterministic ModelPlacement names: `{deployment}-{ie}` (truncated
     to 63 chars). This is safe because the deployment name is unique.
  2. The LLMInferenceService name = ModelPlacement XR name (compose-model-placement
     already does `llmis_name = xr_name`).
  3. HTTPRoute rewrite prefix = `/{remote-ns}/{placement-name}/`.
  For multi-env, all backends behind one rule need the same rewrite path.
  This works for MVP (environments=1). Multi-env would need separate rules
  per backend, or the same LLMIS name across all clusters.

- **extraArgs pass-through**: compose-model-placement now passes
  `spec.vllm.extraArgs` through to the LLMInferenceService container args.

- **Protobuf empty match_labels**: `response.require_resources` with
  `match_labels={}` causes `unsupported required resource selector type <nil>`
  at the Crossplane level. An empty labels map is a valid protobuf
  `MatchLabels`, but the protobuf serialization optimizes the empty map away,
  leaving the `oneof match` field unset. The Crossplane Go code then hits the
  default switch case and returns an error.
  Workaround: require InferenceEnvironments to carry a `modelplane.ai/managed-by: modelplane`
  label, and match on that instead of using an empty labels map. This is a
  Crossplane v2 bug — empty `match_labels` should mean "match all".

- **Repository prefix**: Changed `upbound.yaml` `spec.repository` from
  `xpkg.upbound.io/negzupboundio/modelplane-infra` to
  `xpkg.upbound.io/upbound/modelplane-infra` so that function names match
  the `upbound-` prefix used by `up project run`.

### Issue: `up project run` not updating function images

`up project run` consistently fails to load the compose-model-deployment
function image into the kind cluster's internal TLS registry. All other
functions load fine. The issue persists across:
- Complete kind cluster recreation
- Deleting all configurations, functions, and revisions
- Clearing `~/.up/build-cache/`
- Using `--no-build-cache`

The `up project build --no-build-cache` produces new Docker images locally with
new digests, but `up project run` keeps loading the old digests into the
cluster. The "Loading packages into control plane" step reports success even
though the model-deployment function is missing from the cluster's registry.

The Configuration then reports:
```
cannot resolve package dependencies: missing dependencies:
  "xpkg.upbound.io/upbound/modelplane-infra_compose-model-deployment" (sha256:...)
```

This appears to be a bug in the `up` CLI's image caching/loading logic during
`up project run`. Switching to explicit `up project push` with semver tags
to a remote registry as a workaround.

### Issue: `up project run` repository prefix mismatch

`up project run` pushes images with the `upbound/` org prefix regardless of
what `spec.repository` is set to in `upbound.yaml`. On the cluster, function
names are derived from the image repository path via `ToDNSLabel`. So
compositions must use `upbound-modelplane-infra...` when deployed via
`up project run`, but `negzupboundio-modelplane-infra...` when pushed to the
real registry via `up project push`. You can't use the same composition YAML
for both workflows without changing the repository.

### Issue: private repositories on xpkg.upbound.io

`up project push` creates new function repositories as **private** by default.
Crossplane's `packagePullSecrets` mechanism uses `k8schain` which implements
the Docker registry token exchange flow. But xpkg.upbound.io's token endpoint
(`/service/token`) returns tokens that still fail auth — the exchange flow
doesn't work, at least not with Upbound session JWT tokens as the password.

Workaround: use `up repo update <name> --private=false --publish=false --force`
to make all function repositories public. This needs to be done for every new
function repo, which is easy to forget.

Additionally, `up repo update` has an aggressive rate limit that persists for
10+ minutes after a batch of updates. During the initial setup we updated 5
repos in quick succession, and subsequent updates for new repos hit
`Repository Update Rate Limit Exceeded` for extended periods. The rate limit
also can't be bypassed via the REST API directly (`PUT /v1/repositories/...`
returns 200 but doesn't actually update the repo). This makes iterative
development painful — every new function means waiting out a rate limit before
Crossplane can pull it.

### Correction: `up project build` does NOT use Docker buildx

Earlier entries in this log blamed Docker buildx caching for stale function
code. This was wrong. Investigation of the `up` CLI source code revealed
that Python function builds don't use Docker or buildx at all. The build:

1. Pulls the `function-interpreter-python` base image from the registry
2. Tars the function directory (following symlinks) using `go-containerregistry`
3. Appends the tar as a layer to the base image in memory
4. Writes the result to the `.uppkg` file

No Docker daemon is involved. The build always reads source files directly
from the filesystem — there is no build cache to invalidate.

The "stale code" I was seeing was from `docker run` on OLD images left in
the Docker daemon by previous `up test run` or `up project run` sessions.
These are NOT the images that `up project build` produces. The `.uppkg` was
always correct. The confusion was caused by verifying the wrong image.

The actual issue when code changes didn't take effect on a cluster was
Crossplane's `IfNotPresent` Function caching (see below), not the build.

### Issue: Crossplane v2 IfNotPresent caching for function images

When updating a Configuration's package tag (e.g., `v0.1.0-dev.1` →
`v0.1.0-dev.2`), Crossplane resolves the new function digests from the
Configuration's dependencies. But existing Function objects with the same name
are not repulled if they already exist — `IfNotPresent` means Crossplane sees
the Function exists and skips the pull, even though the digest changed.

Workaround: delete the specific Function object to force Crossplane to repull
with the new digest:
```bash
kubectl delete function <name>
```

### Issue: Crossplane v2 namespaced MR apiVersions

In Crossplane v2, namespaced managed resources use the `.m.` API group
variant (e.g., `kubernetes.m.crossplane.io/v1alpha1` instead of
`kubernetes.crossplane.io/v1alpha2`). The API version also differs — the
namespaced Object CRD is `v1alpha1`, not `v1alpha2`. Functions composing
Objects for namespaced XRs must use the `.m.` variant or Crossplane rejects
the resource with "cannot apply cluster scoped composed resource for a
namespaced composite resource."

### Issue: XRD integer fields arrive as floats in protobuf

XRD fields with `type: integer` arrive in the function as Python floats (e.g.,
`environments: 1.0` instead of `1`). This is because protobuf Struct values
use `double` for all numbers. Using the value directly as a list slice index
causes `TypeError: slice indices must be integers`. Fix: cast with `int()`.

### Issue: RBAC for bootstrap required resources

The spec's prerequisites RBAC ClusterRole granted access to `httproutes` and
`backends` but not `gateways`. The composition's bootstrap requirement needs
to read the control plane Gateway, which requires `get`/`watch`/`list`
permissions. Without this, Crossplane logs:
```
cannot fetch bootstrap required resources: Timeout: failed waiting for Informer to sync
```
The error doesn't mention RBAC, making it hard to diagnose without debug
logging enabled on Crossplane (`--debug` flag on the deployment).

### Issue: buildx builder deleted during cache purge

Running `docker buildx prune -af` can destroy the buildx builder container
(`xpkg-builder`) that `up project build` uses. After this, `up project build`
succeeds but silently doesn't produce local Docker images. The build output
(`.uppkg`) is correct but `docker images` shows nothing new. Need to let `up`
recreate the builder on next build, or manually recreate it.

### Current status

Switched from `up project run` to manual `up project push --tag <semver>` +
vanilla Crossplane v2.2.0 on kind. Push to `xpkg.upbound.io/negzupboundio/`
with bumped semver for each iteration. All repos made public.

### End-to-end demo working

All resources Ready:
```
ClusterModel:           qwen-0.5b-vllm          Ready
InferenceEnvironment:   demo-us-central          Ready   gateway=34.55.233.135
ModelDeployment:        qwen-demo                Ready   placements=1/1
ModelPlacement:         qwen-demo-demo-us-central Ready  endpoint=http://34.55.233.135/default/qwen-demo-demo-us-central/v1
```

Verified with curl:
```
curl http://34.55.233.135/default/qwen-demo-demo-us-central/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "Qwen/Qwen2.5-0.5B-Instruct", "messages": [{"role": "user", "content": "Hello!"}]}'

→ 200 OK, model responds: "I am Qwen, developed by Alibaba Cloud team"
```

Deployed on: Crossplane v2.2.0 (vanilla, not UXP) on kind, with GKE cluster
`crossplane-playground` in `us-central1`, KServe v0.16.0, vLLM v0.7.3,
Qwen 2.5 0.5B Instruct on a single L4 GPU.

### Additional fixes applied during e2e (continued)

- **Backend IP vs FQDN**: Envoy Gateway Backend `fqdn` field rejects IP
  addresses with "hostname X is an IP address". Changed to `ip.address` field.

- **Backend/HTTPRoute readiness**: These resources don't have a `Ready`
  condition. Backend has `Accepted` and `Invalid` conditions. HTTPRoute has
  `Accepted` (nested under `status.parents[].conditions`). Updated the
  readiness check to look for `Accepted` on HTTPRoute and check `Invalid` is
  not True on Backend.

- **ClusterProviderConfig identity block**: The kubeconfig secret from GKE
  doesn't contain auth credentials (user section is empty). The existing
  ProviderConfig uses a GCP `identity` block with `GoogleApplicationCredentials`
  referencing the SA key secret. The ClusterProviderConfig composed by
  compose-inference-env was missing this identity block.

- **buildx cache invalidation**: Recreating the `xpkg-builder` Docker buildx
  builder (`docker buildx rm xpkg-builder && docker buildx create --name
  xpkg-builder --driver docker-container --use`) was the only reliable way to
  ensure code changes were picked up. Both `--no-build-cache` and deleting
  `~/.up/build-cache/` were insufficient.

Total iterations pushed to registry: v0.1.0-dev.1 through v0.1.0-dev.23.

## InferenceGateway
**Status:** Complete

Added InferenceGateway XR to compose the control plane routing infrastructure.
This eliminates 5 of 6 manual prerequisites (Envoy Gateway Helm install,
GatewayClass, Gateway, namespace, and the envoy-gateway-system namespace).
Only the RBAC ClusterRole remains as a manual prerequisite (chicken-and-egg —
Crossplane needs the permission before it can grant itself the permission).

### Issues encountered

- **Cluster-scoped XR composing namespaced Helm Release**: the function must
  set `metadata.namespace` on the Release explicitly (via Pydantic
  `metadata=metav1.ObjectMeta(namespace=...)`). Crossplane preserves the
  function-set namespace for cluster-scoped XRs. Without this, the
  resourceRef has no namespace and Crossplane can't GET the resource.

- **Provider-helm InjectedIdentity lacks namespace creation permission**:
  provider-helm's SA can't create the `envoy-gateway-system` namespace. Fixed
  by composing the namespace directly and setting `skipCreateNamespace=True`
  on the Helm release.

- **GatewayClass and Gateway readiness**: these resources have `Accepted`
  conditions, not `Ready`. The function checks `Accepted` on both and sets
  the composed resource ready flag accordingly. On kind clusters, the Gateway
  is `Accepted` but never `Programmed` (no LoadBalancer) — this is fine.

- **Functions can't set metadata.labels on the XR**: `resource.update` on
  `rsp.desired.composite` with `metadata.labels` is accepted by the function
  but Crossplane doesn't apply label changes from functions to the XR. User
  metadata is user-managed. Workaround: require platform teams to add a
  `modelplane.ai/environment: "true"` label to InferenceEnvironments. This is
  needed because `match_labels={}` doesn't work (protobuf bug) and there's no
  `match_all` option.

- **Docker buildkit state volumes persist across builder recreation**: the
  volume name is `buildx_buildkit_xpkg-builder0_state`. Removing the builder
  (`docker buildx rm`) does NOT remove the volume. The volume must be removed
  explicitly with `docker volume rm`. Without this, the new builder reuses
  cached layers from the old one. The full cache purge sequence is:
  ```bash
  docker buildx rm xpkg-builder
  docker volume ls -q | grep buildkit | xargs -r docker volume rm
  docker builder prune -af
  docker buildx create --name xpkg-builder --driver docker-container --use
  up project build --no-build-cache
  ```

## MetalLB for kind clusters
**Status:** Complete

Added `loadBalancer: MetalLB` discriminator to InferenceGateway's envoyGateway
config. When set, the function composes MetalLB as a Helm release and configures
an IPAddressPool + L2Advertisement. This gives the Gateway a real IP on kind.

### Unified endpoint verified end-to-end

Full path tested from inside the kind cluster:
```
curl http://172.18.255.200/ml-team/qwen-demo/v1/chat/completions
→ control plane Envoy Gateway (MetalLB IP)
→ HTTPRoute rewrites /ml-team/qwen-demo/ → /default/qwen-demo-demo-us-central/
→ Backend forwards to 34.55.233.135:80 (remote KServe gateway on GKE)
→ Remote HTTPRoute rewrites prefix → /
→ vLLM pod serves Qwen 2.5 0.5B Instruct
→ 200 OK with chat completion response
```

The 500 errors from earlier were because the GKE gateway's external IP
(`34.55.233.135`) isn't reachable from the host machine — only from inside the
Docker/kind network. From inside kind, the full control plane → remote cluster
path works.

### Issues

- **MetalLB conflicts with manual install**: if MetalLB was previously installed
  via `kubectl apply`, the Helm release fails with ownership metadata errors.
  The manual install must be fully deleted first.

- **RBAC for MetalLB CRDs**: the prerequisites ClusterRole needs `metallb.io`
  resources (ipaddresspools, l2advertisements) for the function to compose them.

Total iterations pushed to registry: v0.1.0-dev.1 through v0.1.0-dev.24.

---

## Feedback for the `up` CLI team

### The iteration loop is painful

The inner loop for developing a Crossplane v2 project with embedded Python
functions is: edit code → build → push → patch Configuration tag → delete
Function → wait for repull → wait for function pod → wait for reconcile →
check. This takes 2-3 minutes per iteration, and most of that time is spent
working around caching issues.

`up project run` should be the fast path but it has several issues that make
it unreliable (detailed above). The fallback — `up project push` to a remote
registry — works but is slow and requires manual Function deletion to force
Crossplane to pick up the new digest.

Ideal: `up project run` should detect code changes and hot-reload the
function container without requiring a full package rebuild and push cycle.

### `--no-build-cache` doesn't do what it says

The flag clears `~/.up/build-cache/` but not Docker's buildx cache. The
buildx cache is the one that matters — it caches the Python function code
layer. After editing `main.py`, `up project build --no-build-cache` produces
an image with the old code. The only fix is `docker buildx rm xpkg-builder`
followed by `docker buildx create --name xpkg-builder ...`.

This is the single most confusing issue in the entire build. The build reports
success, `up project push` pushes new digests (because the metadata changes
even if the code layer is stale), but the deployed function runs old code.

### `up project build` should fail loudly when the builder is missing

If the `xpkg-builder` buildx builder doesn't exist or is inactive,
`up project build` succeeds but produces no local Docker images. The
`.uppkg` is built but `docker images` shows nothing. There's no error or
warning. The next `up project push` pushes whatever was built, which may be
stale.

### `up function generate` should be documented as the only way to create functions

Hand-creating a function directory with just `main.py` appears to work (the
build succeeds) but the function crashes at runtime because the `model`
symlink is missing. The spec and `up function generate --help` don't mention
that the symlink is required. The error (`ModuleNotFoundError: No module
named 'function.model'`) doesn't suggest the fix.

### `up project run` overrides the repository prefix

`up project run` pushes to its internal registry with the `upbound/` org
prefix, regardless of `spec.repository` in `upbound.yaml`. This means
composition `functionRef.name` values that work with `up project push` don't
work with `up project run`, and vice versa. There's no way to write a
composition that works with both workflows.

### New repos should be public by default (or at least configurable)

Every `up project push` that creates a new function repo creates it as
private. Since Crossplane can't authenticate to the Upbound registry via
`packagePullSecrets` (the token exchange doesn't work — see below), this
means every new function is silently inaccessible. The fix is manual:
`up repo update <name> --private=false --publish=false --force` for each
repo. There's no `up project push --public` flag.

### Error message for unresolvable function names is misleading

The error "unknown Function X — does it exist in your Functions file?"
suggests a missing file, but the real issue is a naming mismatch between the
composition's `functionRef.name` and the DNS label derived from the image
repository path. The error should show what function names are available.

## Feedback for the Crossplane team

### `match_labels={}` should mean "match all"

The Python SDK's `response.require_resources(match_labels={})` sets the
protobuf `MatchLabels` oneof variant with an empty labels map. But protobuf
optimizes this to nothing on the wire, leaving the `oneof match` field unset.
The Go code in `internal/xfn/required_resources.go` hits the default case
and returns `unsupported required resource selector type <nil>`.

This is a real footgun. "Match all resources of this kind" is a common
operation. The SDK accepts it, the protobuf definition supports it, but the
wire format loses it. Either the Go side should treat an unset selector as
"match all", or the SDK should reject empty labels with a clear error.

### Integer XRD fields arrive as floats

All numbers in protobuf Struct are `double`. A field declared as
`type: integer` in the XRD arrives as `1.0` in Python, not `1`. Using it
as a list index or range argument causes a TypeError. The SDK could handle
this — either by auto-casting integers when deserializing from Struct, or
by documenting the gotcha prominently.

### Informer sync timeout hides RBAC errors

When a bootstrap required resource references a CRD that Crossplane lacks
RBAC permissions for, the error is:
```
Timeout: failed waiting for *unstructured.Unstructured Informer to sync
```
This doesn't mention RBAC at all. Without `--debug` on the Crossplane
deployment, there's no hint that permissions are the issue. A check like
`kubectl auth can-i watch <resource>` before starting the informer would
produce a much clearer error.

### Namespaced XRs can't compose resources in other namespaces

A ModelPlacement (namespaced, in `ml-team`) needs to compose a
provider-kubernetes Object that targets a ProviderConfig in
`ie-demo-us-central`. In Crossplane v2, namespaced MRs can only reference
ProviderConfigs in their own namespace. The fix is to use a
ClusterProviderConfig instead, but this isn't obvious from the error message
("cannot apply cluster scoped composed resource for a namespaced composite
resource").

This is the correct behavior, but it's a significant design constraint that
affects cross-team resource composition. The InferenceEnvironment function
had to compose a ClusterProviderConfig in addition to the existing namespaced
ProviderConfig, specifically so that ModelPlacements in other namespaces
could reach the remote cluster.

### IfNotPresent caching prevents function updates

When a Configuration's tag changes (e.g., `v0.1.0-dev.1` → `v0.1.0-dev.2`),
Crossplane resolves new function digests from the Configuration's
dependencies. But if a Function object with the same name already exists,
the `IfNotPresent` pull policy means Crossplane doesn't repull it, even
though the digest changed. The workaround is deleting the Function to force
a fresh pull. This should happen automatically when the Configuration's
dependency resolution finds a new digest.

### "missing required capabilities: composition" during function startup

When a function pod is starting up (before the gRPC server is ready), the
FunctionRevision reports "missing required capabilities: composition". This
is misleading — the capabilities are declared in the package metadata, not
discovered from the running server. The real issue is that the function pod
hasn't started yet. The error message should say something like "function
pod is not yet ready" instead.

## Feedback for the function-sdk-python team

### Document the `model` symlink requirement

Function directories need `model -> ../../.up/python/models` for `.model.*`
imports to resolve. `up function generate` creates this, but there's no
documentation explaining that it's required or why. The runtime error is
`ModuleNotFoundError: No module named 'function.model'` which doesn't
suggest the fix.

### Document protobuf number coercion

All numbers from XRD fields arrive as Python `float`, not `int`. This
breaks list slicing, range(), and anywhere Python expects an integer. Add a
note to the SDK docs, or provide a helper like
`resource.get_int(xr, "spec.environments")`.

### `.m.` import paths are confusing

Functions use `.model.io.upbound.m.gcp...` (the `.m.` monolithic variant)
while tests use `.model.io.upbound.gcp...` (no `.m.`). The API versions
may also differ between variants. The only way to know which path to use is
to check what exists under `.up/python/models/`. A table in the docs mapping
"I want to compose a Network in a function" → "use
`.model.io.upbound.m.gcp.compute.network`" would help.

### `response.require_resources` should reject empty `match_labels`

Since empty `match_labels={}` causes a runtime error at the Crossplane level
(the protobuf oneof isn't set), the SDK should either reject it with a clear
error at call time, or ensure the oneof is properly set on the wire.

## Notes for an LLM repeating this work

### The development iteration workflow

```bash
# Edit function code, then:
docker buildx rm xpkg-builder
docker buildx create --name xpkg-builder --driver docker-container --use
up project build --no-build-cache
up project push --tag v0.1.0-dev.N    # Bump N every time

# On the cluster:
kubectl patch configuration modelplane-infra --type=merge \
  -p '{"spec":{"package":"xpkg.upbound.io/negzupboundio/modelplane-infra:v0.1.0-dev.N"}}'
kubectl delete function <changed-function-name>

# Wait ~60s for function pod to start, then check:
kubectl describe <resource> | grep ComposeResources | tail -3
kubectl logs -n crossplane-system <function-pod> --tail=5
```

You MUST bump the semver tag every push. You MUST delete the Function object
to force a repull. You MUST recreate the buildx builder to avoid stale
cached code. Skipping any of these steps means you're running old code
and debugging phantoms.

### How to verify your function code is actually deployed

```bash
# Check the local image (if available):
docker run --rm --entrypoint /venv/fn/bin/python \
  xpkg.upbound.io/negzupboundio/modelplane-infra_compose-<name>:arm64 \
  -c "open('/venv/fn/lib/python3.11/site-packages/function/main.py').read()"

# On the cluster, check function logs for Python exceptions:
kubectl logs -n crossplane-system <function-pod> --tail=20
```

If the function logs show no output at all, it hasn't been called yet.
Check Crossplane debug logs for the reason:
```bash
kubectl logs -n crossplane-system deployment/crossplane --tail=100 | grep <xr-name>
```

### Status flows between XRs

```
GKECluster.status.secrets → InferenceEnvironment reads them
KServeStack.status.gateway.address → InferenceEnvironment reads it
InferenceEnvironment.status.providerConfigRef.name → ModelPlacement reads it
InferenceEnvironment.status.gateway.address → ModelDeployment reads it
InferenceEnvironment.status.capacity.gpuPools → ModelDeployment reads it
ModelPlacement.status.endpoint.url → ModelDeployment reads it (not yet used)
```

Each of these flows requires the upstream XR to be Ready and have populated
its status before the downstream function can proceed. Functions return
early if required resources aren't resolved yet. Crossplane re-calls the
function on the next reconcile.

### Crossplane v2 specifics that differ from Crossplane v1 / standalone SDK docs

- Functions use `compose(req, rsp)` convention, not `FunctionRunner` class
- Don't call `response.to(req)` — the runtime does it
- Don't return `rsp` — mutate in place
- Namespaced MRs use `.m.` API groups (`kubernetes.m.crossplane.io/v1alpha1`)
- Namespaced XRs can only compose namespaced resources in the same namespace
- `resource.update()` mutates protobuf map entries in place (correct behavior)
- XRD `apiVersion` is `apiextensions.crossplane.io/v2`; Composition stays `v1`
- Use `scope: Cluster` explicitly on cluster-scoped XRDs — v2 defaults to
  Namespaced

### GKE provisioning timeline

A fresh GKE cluster takes ~15 minutes to provision. The KServeStack installs
cert-manager, Envoy Gateway, LWS, and KServe — another ~5 minutes after the
cluster is ready. The InferenceEnvironment takes ~20 minutes total from
creation to Ready. Plan for this when testing.

### Gateway API condition nesting

HTTPRoute conditions are nested under `status.parents[].conditions`, not
`status.conditions`. Backend conditions are at `status.conditions`. The
condition types are `Accepted`, `ResolvedRefs`, and `Invalid` — not `Ready`.
Check actual resources with `kubectl get <resource> -o yaml` before writing
readiness checks.

### Envoy Gateway Backend gotcha

The Backend CRD has separate `fqdn` and `ip` endpoint types. Using `fqdn`
with an IP address fails with "hostname X is an IP address". Use `ip.address`
for IP addresses. Since KServe gateways expose IPs (not DNS names), you'll
almost always need `ip`.

## Deletion reliability (learned during 3-cycle validation)

Getting InferenceEnvironment deletion to work reliably required solving
four distinct problems. Each was discovered by monitoring `kubectl describe`
on stuck resources — not by increasing timeouts.

### 1. Namespace termination blocks ProviderConfigUsage creation

When an IE composes a per-IE namespace and that namespace gets a
`deletionTimestamp`, it enters Terminating state. All managed resources in
that namespace can no longer create `ProviderConfigUsage` objects (the
namespace admission controller blocks new content). Without PCUs, providers
can't connect to GCP/Kubernetes to delete the external resources.

**Fix:** use a shared `modelplane-system` namespace for all IEs instead of
per-IE namespaces. The shared namespace is created by InferenceGateway and
never deleted during IE teardown.

Note: `protection.crossplane.io/Usage` does NOT fix this. Usages block
final deletion but don't prevent the namespace from entering Terminating
state (the `deletionTimestamp` is set immediately).

### 2. KServe webhooks prevent Helm uninstall

KServe's Helm chart installs validating webhooks for
`LLMInferenceServiceConfig`. When the chart is uninstalled, Helm tries to
delete these resources, but the webhook server pod is already gone (deleted
by an earlier Helm release in the same KServeStack). The webhook intercepts
the deletion request and fails with "no endpoints available for service."
This causes the Helm uninstall to retry indefinitely.

**Fix:** set `managementPolicies: ["Create", "Observe"]` on the KServe
controller and CRD Helm releases. Crossplane creates them but doesn't
attempt to uninstall them. Since the GKE cluster is being deleted anyway,
leaving KServe installed is harmless.

### 3. GatewayClass finalizer blocks Object deletion

The remote GatewayClass has a `gateway-exists-finalizer` that blocks
deletion while any Gateway references it. When compose-kserve-stack deletes
both the Gateway and GatewayClass Objects simultaneously, the GatewayClass
can't be deleted because the remote Gateway still exists (for a moment).
The Object retries `DeletedExternalResource` indefinitely.

**Fix:** set `managementPolicies: ["Create", "Observe"]` on the GatewayClass
Object, same rationale as KServe. The GatewayClass is cluster-level
infrastructure config — it's harmless to leave behind.

### 4. ProviderConfigUsage lingers after ModelDeployment deletion

When the teardown script deletes the ModelDeployment with `--wait=true`,
kubectl returns as soon as the MD API object disappears. But the
ModelPlacement's provider-kubernetes Object created a
`ProviderConfigUsage` for the ClusterProviderConfig. The PCU outlives the
Object because provider-kubernetes hasn't cleaned it up yet. This blocks
the ClusterProviderConfig from being deleted (it has an `in-use.crossplane.io`
finalizer), which blocks the IE from completing its foreground cascade.

**Fix:** use `--cascade=foreground` on the ModelDeployment deletion. This
blocks until ALL downstream resources (including PCUs) are cleaned up
before the MD deletion returns.

### The deletion chain (when everything works)

Observed timeline from monitoring `kubectl get managed` every 15 seconds:

```
T+0s    IE deleted. KServeStack + GKECluster get deletion timestamps.
T+15s   Helm releases uninstalling (cert-manager, envoy-gateway, LWS).
        KServe CRDs/controller skipped (managementPolicies: Create+Observe).
T+30s   KServeStack fully deleted. Usage allows GKECluster deletion.
T+45s   SA, SA key, IAM binding deleted.
T+60s   NodePools deleting. Network deletion starts (blocked by firewall rules).
T+120s  NodePools deleted.
T+300s  Network deletion retries succeed (firewall rules cleaned up by GKE).
T+330s  GKE Cluster deleting (GCP async operation).
T+420s  GKE Cluster deleted. Subnet + network deleted.
T+450s  GKECluster XR fully deleted. IE XR deleted.
```

Total: ~8-12 minutes per IE. Two IEs in parallel complete in ~12-15 minutes.

### Key debugging lesson

Every time deletion was "slow" (>15 minutes), something was stuck — not
just slow. The pattern was always: `kubectl describe` on the stuck resource
reveals a specific error (webhook failure, missing ProviderConfig, PCU
blocking finalizer, etc.). Increasing timeouts never fixed the underlying
problem. The right response to a slow deletion is always: describe the
resources, find the error, fix the root cause.

### Teardown script safety

If the foreground cascade times out, the teardown script must NOT proceed
to delete the Configuration or kind cluster. Killing the control plane
mid-deletion orphans GKE resources. The script now exits with an error
instructing the user to wait and re-run.
