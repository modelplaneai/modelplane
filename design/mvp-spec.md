# Modelplane MVP — Build Spec

Nic Cope, March 2026

---

## What this is

This spec describes the minimum work to demo Modelplane end-to-end: a platform
team creates an InferenceEnvironment and registers a model, an ML team creates a
ModelDeployment, and a working OpenAI-compatible endpoint comes back. One cloud
(GKE), one backend (KServe), one small model (Qwen 2.5 0.5B on an L4), fixed
replicas.

The full design is in `design/design.md`. This spec is a subset scoped to what
an LLM agent can build in a focused session, with concrete inputs and outputs
for every resource.

---

## What already exists

Two internal XRs with working composition functions and tests:

- **GKECluster** (`apis/gkeclusters/`) — provisions a GKE cluster with VPC,
  subnet, node pools, service account, IAM binding, and ProviderConfigs. Output:
  `status.secrets` array containing kubeconfig and SA key secret references.
- **KServeStack** (`apis/kservestacks/`) — installs cert-manager, Envoy Gateway,
  LeaderWorkerSet, Inference Extension CRDs, KServe CRDs, KServe controller,
  GatewayClass, and Gateway on a remote cluster. Input: `spec.secrets` array.

Both are namespaced, use `infrastructure.modelplane.ai/v1alpha1`, and have been
validated end-to-end (see `context/kserve-gke-validation.md`). The composition
functions live in `functions/compose-gke-cluster/main.py` and
`functions/compose-kserve-stack/main.py`. They use the `compose(req, rsp)`
convention with `function-python` inline execution, not standalone gRPC servers.

---

## How to write functions, tests, and XRDs

This section captures the patterns established by the existing GKECluster and
KServeStack code. Follow these patterns exactly — they reflect how Upbound
projects work in practice, which differs from the standalone Crossplane SDK
documentation in several important ways.

### Project structure and the `up` CLI

This is an **Upbound Project** (`upbound.yaml`, kind `Project`). The `up` CLI
manages the build lifecycle:

- **`up project build`** builds everything — functions, XRDs, compositions — into
  a single `.uppkg` OCI artifact in `_output/`. It also regenerates Pydantic
  models in `.up/python/models/` from XRD schemas and provider CRDs.
- **`up test run tests/*`** runs CompositionTests locally without a cluster.
- **`up dependency add`** adds provider/function dependencies to `upbound.yaml`
  and updates the model cache.

After adding a new XRD, run `up project build` before writing the function or
test — this generates the Pydantic models you'll import.

### Function convention: `compose(req, rsp)`

Functions are **embedded Python functions** executed by the `function-python`
runtime. They are NOT standalone gRPC servers. Each function lives in
`functions/{name}/main.py` and exports a single function:

```python
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    # Mutate rsp in place. Do not return anything.
    # Do not call response.to(req) — the runtime does this for you.
    # rsp.desired is pre-populated with the previous pipeline step's output.
    pass
```

**Do not** use the class-based `FunctionRunner` pattern from the standalone SDK
docs. **Do not** call `response.to(req)`. **Do not** return `rsp`. The runtime
handles all of this.

Each function directory contains only `main.py` — the function code.

The `up` CLI packages these automatically. No `Dockerfile`, `pyproject.toml`,
`requirements.txt`, or `__init__.py` needed. The `function-python` runtime has
the SDK, pydantic, and the Python standard library available. Third-party
libraries beyond these are not supported.

### Import paths for Pydantic models

The `up` CLI generates Pydantic models under `.up/python/models/`. Functions and
tests import them with relative imports from `.model.*`. The path structure
mirrors the reversed DNS of the Kubernetes API group.

**Functions** import managed resource models from the `.m.` (monolithic) variant:

```python
# GCP managed resources — note the .m. segment
from .model.io.upbound.m.gcp.compute.network import v1beta1 as networkv1beta1
from .model.io.upbound.m.gcp.container.cluster import v1beta1 as clusterv1beta1

# Crossplane provider resources — also .m.
from .model.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from .model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from .model.io.crossplane.m.kubernetes.providerconfig import v1alpha1 as k8spcv1alpha1
from .model.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1

# XR models — no .m. segment (these are our own XRDs)
# The import path mirrors the reversed DNS of the API group.
#
# infrastructure.modelplane.ai → .model.ai.modelplane.infrastructure.{kind}
from .model.ai.modelplane.infrastructure.gkecluster import v1alpha1
from .model.ai.modelplane.infrastructure.kservestack import v1alpha1
#
# modelplane.ai → .model.ai.modelplane.{kind}
# (The new public APIs use a shorter API group, so the import path is shorter.)
from .model.ai.modelplane.inferenceenvironment import v1alpha1
from .model.ai.modelplane.clustermodel import v1alpha1
from .model.ai.modelplane.modelplacement import v1alpha1
from .model.ai.modelplane.modeldeployment import v1alpha1

# Kubernetes meta types
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
```

**Tests** import from the non-`.m.` variant for assertions:

```python
# Test models — no .m. segment
from .model.io.upbound.gcp.compute.network import v1beta1 as networkv1beta1
from .model.io.upbound.gcp.container.cluster import v1beta2 as clusterv1beta2
from .model.io.crossplane.helm.release import v1beta1 as helmv1beta1

# CompositionTest model
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest

# Kubernetes meta types (same in both contexts)
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as k8s
```

The `.m.` stands for "managed" — it's the namespace-scoped variant of each MR,
introduced in Crossplane v2 to distinguish from the legacy cluster-scoped MRs.
Functions compose namespaced MRs (the `.m.` path); tests assert against the
legacy cluster-scoped models (the non-`.m.` path). The API version suffixes may
also differ between the two variants (e.g., functions use `v1beta1` while the
legacy variant might have `v1beta2` — use whichever the generated models
provide). When in doubt, check what exists under `.up/python/models/`.

### Build before writing functions

`up project build` generates Pydantic models for everything — XRs, managed
resources, and test fixtures. When you create a new XRD (e.g.,
`apis/inferenceenvironments/definition.yaml`), the Pydantic model at
`.model.ai.modelplane.inferenceenvironment.v1alpha1` does not exist yet. Run
`up project build` to generate it before writing the function.

**Always use Pydantic models instead of raw dicts.** The generated models give
you field names, types, required vs optional, enums, and defaults — the schema
catches mistakes at write time instead of at deploy time. The existing functions
use Pydantic models for both the XR and all composed resources. Follow that
pattern:

```python
from .model.ai.modelplane.inferenceenvironment import v1alpha1
xr = v1alpha1.InferenceEnvironment(**resource.struct_to_dict(req.observed.composite.resource))
project = xr.spec.cluster.gke.project
```

Raw dicts are a last resort for resources that don't have generated models (e.g.,
third-party CRDs not in the project's dependencies).

### Composing resources with `resource.update()`

Use `resource.update()` to write desired resources. Always use Pydantic models
— they validate field names and types at write time:

```python
from crossplane.function import resource

# Pydantic model — use this for everything with a generated model
resource.update(
    rsp.desired.resources["network"],
    networkv1beta1.Network(
        spec=networkv1beta1.Spec(
            forProvider=networkv1beta1.ForProvider(
                project="my-project",
                autoCreateSubnetworks=False,
            ),
        ),
    ),
)

# Raw dict — last resort, only for CRDs without generated models
# (e.g., third-party CRDs not in the project's dependencies)
resource.update(
    rsp.desired.resources["gateway-class"],
    {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "GatewayClass",
        "metadata": {"name": "envoy"},
        "spec": {
            "controllerName": "gateway.envoyproxy.io/gatewayclass-controller",
        },
    },
)
```

**Protobuf map quirk:** You cannot assign to `rsp.desired.resources["name"]`.
Accessing a nonexistent key auto-creates an empty `Resource` message.
`resource.update()` mutates it in place. This is correct protobuf behavior, not
a bug.

### Naming composed resources

By default, Crossplane generates a random name for each composed resource,
derived from the parent XR's name. You generally want this — it's safe and
avoids conflicts.

You _can_ set `metadata.name` on a composed resource, but if you do, you must
ensure the name won't conflict when someone creates a second instance of the XR.
The existing functions set names on ProviderConfigs (e.g.,
`{xr_name}-kubeconfig`) because they need deterministic names that other
resources can reference. This is safe because the XR name is unique, so the
derived ProviderConfig name is too.

The same applies to provider-kubernetes Objects whose wrapped resource needs a
specific name on the remote cluster (e.g., a Gateway named
`kserve-ingress-gateway`).

For resources where the name doesn't matter (Networks, NodePools, Helm
Releases), let Crossplane generate the name.

### Composing resources on the control plane vs remote clusters

Crossplane v2 can compose **any Kubernetes resource** directly — Deployments,
ConfigMaps, Gateway API resources, Envoy Gateway resources, etc. This eliminates
the need for provider-kubernetes Object wrappers when the resource lives on the
**control plane cluster** (where Crossplane runs).

Resources on **remote clusters** still need provider-kubernetes Objects, because
Crossplane needs a ProviderConfig to reach the remote API server.

This distinction matters for the MVP:

- **compose-model-deployment** composes HTTPRoute and Backend resources on the
  **control plane** → compose them directly (no Object wrapper). Requires RBAC
  ClusterRoles granting Crossplane access to these CRDs (see Prerequisites).
  These resources use raw dicts because Gateway API and Envoy Gateway CRDs
  aren't in the project's dependencies — this is the one exception to the
  "always use Pydantic models" rule.
- **compose-model-placement** composes LLMInferenceService on a **remote
  cluster** → must use a provider-kubernetes Object with a ProviderConfig.
- **compose-kserve-stack** composes Helm Releases and Objects on a **remote
  cluster** → uses provider-helm and provider-kubernetes with ProviderConfigs.

### Bootstrap vs dynamic required resources

Required resources can be declared two ways:

**Bootstrap requirements** go in the Composition YAML and are pre-populated
before the function runs — no extra reconcile needed:

```yaml
pipeline:
- functionRef:
    name: upbound-modelplane-infracompose-model-deployment
  step: compose-model-deployment
  requirements:
    requiredResources:
    - requirementName: gateway
      apiVersion: gateway.networking.k8s.io/v1
      kind: Gateway
      name: modelplane
      namespace: modelplane-system
```

Use bootstrap for resources with known, fixed names (like the control plane
Gateway).

**Dynamic requirements** are returned in the function response and resolved on
the next reconcile. Use these when the resource name comes from the XR spec
(e.g., the ClusterModel name from `spec.modelRef.name`).

compose-model-deployment should use bootstrap for the Gateway and dynamic for
ClusterModel and InferenceEnvironments.

### Reading observed state

To read the observed composite (the XR):

```python
xr = resource.struct_to_dict(req.observed.composite.resource)
```

To read an observed composed resource's status:

```python
observed = req.observed.resources.get("my-resource")
if observed:
    d = resource.struct_to_dict(observed.resource)
    some_value = d.get("status", {}).get("atProvider", {}).get("someField")
```

For provider-kubernetes Objects, the remote resource's status is nested:

```python
# The Object's atProvider.manifest contains the actual remote resource
remote_status = (
    d.get("status", {})
    .get("atProvider", {})
    .get("manifest", {})
    .get("status", {})
)
```

### Required resources (for cross-XR reads)

The new functions (compose-model-placement, compose-model-deployment) need to
read resources they don't own — ClusterModels and InferenceEnvironments. Use the
SDK helpers:

```python
from crossplane.function import request, response

def compose(req, rsp):
    xr = resource.struct_to_dict(req.observed.composite.resource)

    # Always declare what you need (every reconcile, not just the first)
    response.require_resources(
        rsp,
        name="model",
        api_version="modelplane.ai/v1alpha1",
        kind="ClusterModel",
        match_name=xr["spec"]["modelRef"]["name"],
    )
    response.require_resources(
        rsp,
        name="environments",
        api_version="modelplane.ai/v1alpha1",
        kind="InferenceEnvironment",
        match_labels={},  # empty = match all
    )

    # Read what Crossplane resolved (empty on the first call)
    model = request.get_required_resource(req, "model")
    if model is None:
        return  # Crossplane will re-call with the resource

    envs = request.get_required_resources(req, "environments")
```

The loop runs up to 5 iterations and stabilizes when requirements stop changing.
On the first reconcile, requirements are declared but not yet resolved — the
function returns early and Crossplane re-calls it with the resolved resources.

### Readiness tracking

The existing functions manually track readiness. Follow this pattern:

```python
def _is_ready(req: fnv1.RunFunctionRequest, name: str) -> bool:
    """Check if an observed composed resource has Ready=True."""
    observed = req.observed.resources.get(name)
    if observed is None:
        return False
    c = resource.get_condition(observed.resource, "Ready")
    return c.status == "True"

def compose(req, rsp):
    # ... compose resources ...

    # Check readiness of composed resources
    all_resources = ["cert-manager", "kserve-crds", "kserve-controller"]
    all_ready = True
    not_ready = []
    for r in all_resources:
        if _is_ready(req, r):
            rsp.desired.resources[r].ready = fnv1.READY_TRUE
        else:
            all_ready = False
            not_ready.append(r)

    # Set the XR's Ready condition
    if all_ready:
        rsp.conditions.append(fnv1.Condition(
            type="Ready",
            status=fnv1.STATUS_CONDITION_TRUE,
            reason="Available",
            target=fnv1.TARGET_COMPOSITE_AND_CLAIM,
        ))
    else:
        rsp.conditions.append(fnv1.Condition(
            type="Ready",
            status=fnv1.STATUS_CONDITION_FALSE,
            reason="Creating",
            message=f"Waiting for: {', '.join(not_ready)}",
            target=fnv1.TARGET_COMPOSITE_AND_CLAIM,
        ))
```

Resources that have no Ready condition (ConfigMaps, ProviderConfigs) should be
marked always-ready:

```python
rsp.desired.resources["my-configmap"].ready = fnv1.READY_TRUE
```

### Gating resources on dependencies

The KServeStack function demonstrates gating: it only composes KServe CRDs and
the controller after cert-manager is Ready, and it only composes resources
targeting the remote cluster after the ProviderConfigs have been observed. Follow
this pattern when a resource depends on another being available:

```python
# Don't compose resources that target the remote cluster until we've
# seen the ProviderConfig in observed state (meaning Crossplane has
# persisted it from a previous reconcile).
pc_observed = "provider-config-helm" in req.observed.resources

if pc_observed:
    resource.update(rsp.desired.resources["cert-manager"], ...)

# Don't compose KServe until cert-manager is ready (KServe's Helm chart
# creates Certificate resources that need cert-manager).
cert_manager_ready = _is_ready(req, "cert-manager")

if pc_observed and cert_manager_ready:
    resource.update(rsp.desired.resources["kserve-crds"], ...)
```

This means some resources won't appear in the desired state on early reconciles.
That's fine — Crossplane re-reconciles and the function gradually adds resources
as dependencies become ready.

**Critical: once you compose a resource, always keep emitting it.** If a
function includes a resource in desired state on one reconcile and then omits it
on the next, Crossplane interprets that as "the function no longer wants this
resource" and **deletes it**. So gating only applies to the *first time* a
resource appears. Once a resource has been composed (i.e., it exists in
`req.observed.resources`), the function must keep including it in desired state
on every subsequent reconcile, regardless of whether its dependencies are still
met.

In practice this means: always compose the resource unconditionally if it
already exists in observed state, and only gate on the initial creation:

```python
# Always emit the ProviderConfig (it has no dependencies)
resource.update(rsp.desired.resources["provider-config"], ...)

# Gate cert-manager on the ProviderConfig being observed, but always
# emit it once it exists.
cert_manager_exists = "cert-manager" in req.observed.resources
if pc_observed or cert_manager_exists:
    resource.update(rsp.desired.resources["cert-manager"], ...)
```

### XRD conventions

XRDs use `apiextensions.crossplane.io/v2`. Compositions use
`apiextensions.crossplane.io/v1` — this is correct, the Composition API version
didn't change.

```yaml
# definition.yaml — v2
apiVersion: apiextensions.crossplane.io/v2
kind: CompositeResourceDefinition

# composition.yaml — v1
apiVersion: apiextensions.crossplane.io/v1
kind: Composition
```

Function references in compositions use the auto-generated name format
`upbound-{project}{function-dir}` (no hyphens between project and function):

```yaml
spec:
  compositeTypeRef:
    apiVersion: modelplane.ai/v1alpha1
    kind: InferenceEnvironment
  mode: Pipeline
  pipeline:
  - functionRef:
      name: upbound-modelplane-infracompose-inference-env
    step: compose-inference-env
```

The project name comes from `upbound.yaml` `metadata.name` (`modelplane-infra`).
The function name comes from the directory name under `functions/`. Concatenated
with no separator: `upbound-` + `modelplane-infra` + `compose-inference-env`.

### CompositionTest conventions

Tests live in `tests/{name}/main.py`. Each test declares a `test` variable:

```python
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as k8s

test = compositiontest.CompositionTest(
    metadata=k8s.ObjectMeta(name="my-test"),
    spec=compositiontest.Spec(
        compositionPath="apis/myresource/composition.yaml",
        xrPath="examples/myresource/example.yaml",
        xrdPath="apis/myresource/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        assertResources=[
            # List of expected resources as dicts (via .model_dump(exclude_unset=True))
        ],
    ),
)
```

The `assertResources` list uses Pydantic models dumped with
`model_dump(exclude_unset=True)` — this means only fields you explicitly set are
asserted. Missing fields are ignored, so you can assert on a subset of the
resource's fields.

To assert on which composition resource name a resource maps to, use the
`crossplane.io/composition-resource-name` annotation:

```python
networkv1beta1.Network(
    apiVersion="compute.gcp.upbound.io/v1beta1",
    kind="Network",
    metadata=k8s.ObjectMeta(
        annotations={
            "crossplane.io/composition-resource-name": "network",
        },
    ),
    spec=networkv1beta1.Spec(
        forProvider=networkv1beta1.ForProvider(
            project="my-project",
            autoCreateSubnetworks=False,
        ),
    ),
).model_dump(exclude_unset=True)
```

To assert on the XR itself (including status), include it as the first entry
with the XR's name and namespace:

```python
gkeclusterv1alpha1.GKECluster(
    apiVersion="infrastructure.modelplane.ai/v1alpha1",
    kind="GKECluster",
    metadata=k8s.ObjectMeta(
        name="gpu-us-central1",
        namespace="gpu-us-central1",
    ),
    spec=gkeclusterv1alpha1.Spec(...),
    status=gkeclusterv1alpha1.Status(...),
).model_dump(exclude_unset=True)
```

### Build, test, iterate cycle

```bash
# 1. Write the XRD
#    apis/myresource/definition.yaml

# 2. Write the composition
#    apis/myresource/composition.yaml

# 3. Build to generate Pydantic models
up project build

# 4. Write the function
#    functions/compose-myresource/main.py

# 5. Write the example
#    examples/myresource/example.yaml

# 6. Write the test
#    tests/test-myresource/main.py

# 7. Build again (to pick up the new function)
up project build

# 8. Run tests
up test run tests/test-myresource
```

### Linting and type checking

Run **ruff** for fast linting and **pyright** for type checking after each
phase. Neither is installed — use Nix to run them:

```bash
# Lint all functions and tests
nix run nixpkgs#ruff -- check functions/ tests/

# Type-check a specific function (needs the venv from up project build)
nix run nixpkgs#pyright -- functions/compose-gke-cluster/main.py
```

Ruff catches unused imports, style issues, and common bugs without needing
import resolution. Pyright resolves the `.model.*` imports through the symlinks
that `up project build` creates and catches real type errors.

Fix ruff errors before moving on. Pyright errors are worth reviewing but some
may be false positives from the generated models (e.g., `Optional` fields that
are always populated in practice) — use judgment.

### Local cluster for integration testing

A kind cluster named `modelplane` is available for manual integration testing.
Always run composition tests (`up test run`) first — they're fast, local, and
don't require a cluster. Use the kind cluster to verify things that composition
tests can't cover (e.g., that functions actually run correctly against a live
Crossplane).

To deploy to the kind cluster, build and push the project:

```bash
up project build
up project push --tag v0.1.0-dev.1
```

Then install the Configuration on the cluster. When you make changes and want
the cluster to pick them up, you **must bump the tag** — Crossplane caches
package images by tag and won't re-pull the same tag:

```bash
# After making changes:
up project build
up project push --tag v0.1.0-dev.2    # Bump the number
```

This is a common gotcha. If the cluster isn't picking up your changes, you
almost certainly forgot to bump the tag.

### Keep a build log

Maintain a running log at `design/mvp-build-log.md`. Append to it after each
phase — what was done, what worked, what broke, and how you fixed it. This
serves three purposes:

1. **Handoff** — if your context window fills up and a new session starts, the
   log is the first thing to read. It tells you where you left off, what's
   already been tried, and what to avoid.
2. **Debugging** — when something fails, check the log for whether the same
   thing failed before and what the fix was.
3. **Visibility** — the person reviewing your work can skim the log instead of
   reading every file diff.

Format:

```markdown
# MVP Build Log

## Phase 1: XRDs, compositions, and examples
**Status:** Complete

- Created all 4 XRDs and compositions.
- `up project build` succeeded.
- Pydantic models generated at `.up/python/models/ai/modelplane/`.
- Note: had to use `scope: Cluster` explicitly on InferenceEnvironment
  and ClusterModel — v2 defaults to Namespaced.

## Phase 2: ClusterModel function + KServeStack change
**Status:** In progress

- ClusterModel function done, test passes.
- KServeStack gateway address change: the observed Gateway Object's status
  path is `status.atProvider.manifest.status.addresses[0].value` — confirmed
  by reading the existing test fixtures.
- Ruff found an unused import in compose-model — fixed.
```

---

## What we're building

Four new XRDs and their composition functions:

| # | XRD | Scope | Function | What it does |
|---|-----|-------|----------|--------------|
| 1 | InferenceEnvironment | Cluster | compose-inference-env | Composes GKECluster + KServeStack, threads secrets between them |
| 2 | ClusterModel | Cluster | compose-model | Validation-only. Sets Ready. |
| 3 | Model | Namespaced | compose-model (shared) | Same as ClusterModel, for ML team's private models. |
| 4 | ModelPlacement | Namespaced | compose-model-placement | Deploys one model on one environment. Composes LLMInferenceService on the remote cluster. |
| 5 | ModelDeployment | Namespaced | compose-model-deployment | Fan-out to ModelPlacements. Composes routing resources on the control plane. Surfaces unified endpoint. |

Plus:

- **Control plane Envoy Gateway** — a prerequisite. Envoy Gateway installed on
  the control plane cluster with `enableBackend: true` and a Gateway resource.
  This is infrastructure setup, not something the functions compose.
- **Demo ClusterModel** — a YAML file for Qwen 2.5 0.5B Instruct on vLLM.
- **Demo scenario** — the YAML files a user applies to see it work end-to-end.

---

## Implementation plan

Build in this order. Each phase has a clear "done" signal: `up project build`
succeeds and `up test run` passes for everything written so far. Don't move to
the next phase until the current one is green.

### Phase 1: All XRDs, compositions, and examples (YAML only)

Write all five XRDs (`definition.yaml`), all five compositions
(`composition.yaml`), and all example YAMLs. No Python yet — this is the
foundation that `up project build` needs to generate Pydantic models.

Files to create:
- `apis/inferenceenvironments/definition.yaml`
- `apis/inferenceenvironments/composition.yaml`
- `apis/clustermodels/definition.yaml`
- `apis/clustermodels/composition.yaml`
- `apis/models/definition.yaml`
- `apis/models/composition.yaml`
- `apis/modelplacements/definition.yaml`
- `apis/modelplacements/composition.yaml`
- `apis/modeldeployments/definition.yaml`
- `apis/modeldeployments/composition.yaml`
- `examples/inferenceenvironment/demo.yaml`
- `examples/clustermodel/qwen-0.5b.yaml`
- `examples/modelplacement/example.yaml` (needed as `xrPath` for tests)
- `examples/modeldeployment/qwen-demo.yaml`

Then run `up project build`. This generates Pydantic models for all four new
XRs under `.up/python/models/ai/modelplane/`. Verify the models exist before
moving on.

**Done when:** `up project build` succeeds and the Pydantic models exist.

### Phase 2: Model function + KServeStack change

The two simplest pieces of Python. Start here to validate the build/test cycle
before tackling harder functions.

1. **Model function** — `functions/compose-model/main.py`.
   Trivial: read the XR, set Ready=True. No composed resources. Used by both
   ClusterModel and Model compositions.
2. **ClusterModel test** — `tests/test-cluster-model/main.py`.
3. **KServeStack gateway address change** — edit
   `functions/compose-kserve-stack/main.py` to read the observed Gateway
   Object's status and populate `status.gateway.address`.
4. Run `up project build`, then lint and test:
   ```bash
   up project build
   nix run nixpkgs#ruff -- check functions/ tests/
   up test run tests/test-cluster-model tests/test-kservestack
   ```

**Done when:** Ruff is clean and tests pass.

### Phase 3: InferenceEnvironment function

Composes a GKECluster and KServeStack, threads secrets between them. This is the
first function that gates resources on dependencies (KServeStack waits for
GKECluster secrets).

1. **InferenceEnvironment function** —
   `functions/compose-inference-env/main.py`.
2. **InferenceEnvironment test** — `tests/test-inference-env/main.py`.
3. Build, lint, test:
   ```bash
   up project build
   nix run nixpkgs#ruff -- check functions/compose-inference-env/
   up test run tests/test-inference-env
   ```

**Done when:** Ruff is clean and test passes.

### Phase 4: ModelPlacement function

The first function that uses required resources to read across XR boundaries.
Composes a provider-kubernetes Object wrapping an LLMInferenceService on the
remote cluster.

1. **ModelPlacement function** —
   `functions/compose-model-placement/main.py`.
2. **ModelPlacement test** — `tests/test-model-placement/main.py`.
3. Build, lint, test:
   ```bash
   up project build
   nix run nixpkgs#ruff -- check functions/compose-model-placement/
   up test run tests/test-model-placement
   ```

**Done when:** Ruff is clean and test passes.

### Phase 5: ModelDeployment function

The most complex function. Fan-out to ModelPlacements, Envoy Gateway routing on
the control plane, status aggregation, bootstrap + dynamic required resources,
and Backend name resolution across reconcile cycles.

1. **ModelDeployment function** —
   `functions/compose-model-deployment/main.py`.
2. **ModelDeployment test** — `tests/test-model-deployment/main.py`.
3. Build, lint, test everything:
   ```bash
   up project build
   nix run nixpkgs#ruff -- check functions/ tests/
   up test run tests/*
   ```

**Done when:** Ruff is clean and all tests pass.

---

## Prerequisites

Before any Modelplane resources are created, the control plane cluster needs:

1. **Crossplane v2.2+** with `function-python` installed.
2. **Providers**: `provider-gcp-container`, `provider-gcp-compute`,
   `provider-gcp-cloudplatform`, `provider-helm`, `provider-kubernetes`.

3. **RBAC ClusterRole** granting Crossplane permission to compose Namespaces,
   Gateway API, and Envoy Gateway resources. This cannot be self-composed
   (Crossplane needs the permission before it can grant itself the permission):
   ```yaml
   apiVersion: rbac.authorization.k8s.io/v1
   kind: ClusterRole
   metadata:
     name: crossplane-compose-modelplane
     labels:
       rbac.crossplane.io/aggregate-to-crossplane: "true"
   rules:
   - apiGroups: ["rbac.authorization.k8s.io"]
     resources: ["clusterroles"]
     verbs: ["*"]
   - apiGroups: [""]
     resources: ["namespaces"]
     verbs: ["*"]
   - apiGroups: ["gateway.networking.k8s.io"]
     resources: ["gateways", "gatewayclasses", "httproutes"]
     verbs: ["*"]
   - apiGroups: ["gateway.envoyproxy.io"]
     resources: ["backends"]
     verbs: ["*"]
   ```

Items 3-5 from the original spec (Envoy Gateway, GatewayClass, Gateway, and
namespace) are now composed by the **InferenceGateway** XR. The platform team
creates one InferenceGateway instead of manually installing these components.

---

## Status contracts

Every XR-to-XR dependency flows through status fields. This table is the single
source of truth for what each XR writes and who reads it.

| XR | Status field | Type | Written by | Read by |
|----|-------------|------|------------|---------|
| GKECluster | `status.secrets` | `[]Secret` | compose-gke-cluster | compose-inference-env |
| KServeStack | `status.gateway.address` | `string` | compose-kserve-stack | compose-inference-env |
| InferenceEnvironment | `status.providerConfigRef.name` | `string` | compose-inference-env | compose-model-placement |
| InferenceEnvironment | `status.gateway.address` | `string` | compose-inference-env | compose-model-deployment |
| InferenceEnvironment | `status.capacity.backend` | `string` | compose-inference-env | compose-model-deployment |
| InferenceEnvironment | `status.capacity.gpuPools` | `[]Pool` | compose-inference-env | compose-model-deployment |
| ClusterModel | (none — spec only) | — | — | compose-model-placement, compose-model-deployment |
| Model | (none — spec only) | — | — | compose-model-placement, compose-model-deployment |
| ModelPlacement | `status.endpoint.url` | `string` | compose-model-placement | compose-model-deployment |
| ModelPlacement | `status.resources.gpu.count` | `integer` | compose-model-placement | compose-model-deployment |
| ModelPlacement | `status.ready` | (condition) | compose-model-placement | compose-model-deployment |
| ModelDeployment | `status.endpoint.url` | `string` | compose-model-deployment | user |

### Secret type

The `Secret` type used in GKECluster's `status.secrets` and KServeStack's
`spec.secrets`:

```yaml
type: object
required: [type, name, key]
properties:
  type:
    type: string
    enum: [Kubeconfig, GCPServiceAccountKey]
  name:
    type: string
  key:
    type: string
```

---

## 1. InferenceEnvironment

**API group:** `modelplane.ai/v1alpha1`  
**Scope:** Cluster  
**Function:** `compose-inference-env` (in `functions/compose-inference-env/main.py`)

### What it does

Composes a GKECluster and a KServeStack in a generated namespace, threads the
GKECluster's output secrets into the KServeStack's input, and surfaces the
ProviderConfig name and gateway address in its own status.

### XRD

```yaml
apiVersion: apiextensions.crossplane.io/v2
kind: CompositeResourceDefinition
metadata:
  name: inferenceenvironments.modelplane.ai
spec:
  group: modelplane.ai
  names:
    categories: [crossplane, modelplane]
    kind: InferenceEnvironment
    plural: inferenceenvironments
    shortNames: [ie]
  scope: Cluster
  versions:
  - name: v1alpha1
    served: true
    referenceable: true
    additionalPrinterColumns:
    - name: READY
      type: string
      jsonPath: .status.conditions[?(@.type=='Ready')].status
    - name: REGION
      type: string
      jsonPath: .spec.kserve.cluster.gke.region
    - name: GATEWAY
      type: string
      jsonPath: .status.gateway.address
    - name: AGE
      type: date
      jsonPath: .metadata.creationTimestamp
    schema:
      openAPIV3Schema:
        type: object
        required: [spec]
        properties:
          spec:
            type: object
            required: [backend]
            properties:
              backend:
                type: string
                description: >-
                  Inference backend to deploy. MVP supports KServe only.
                enum: [KServe]
              kserve:
                type: object
                description: >-
                  KServe backend configuration. Required when backend is KServe.
                required: [cluster]
                properties:
                  version:
                    type: string
                    default: "v0.16.0"
                  cluster:
                    type: object
                    required: [source]
                    properties:
                      source:
                        type: string
                        description: >-
                          Cluster provisioning method. MVP supports GKE only.
                        enum: [GKE]
                      gke:
                        type: object
                        description: >-
                          GKE cluster configuration. Required when source is GKE.
                        required: [project, region, nodePools]
                        properties:
                          project:
                            type: string
                            minLength: 6
                            maxLength: 30
                          region:
                            type: string
                            minLength: 1
                            maxLength: 32
                          kubernetesVersion:
                            type: string
                            default: "1.35"
                          nodePools:
                            type: array
                            minItems: 1
                            maxItems: 8
                            x-kubernetes-list-type: map
                            x-kubernetes-list-map-keys: [name]
                            items:
                              type: object
                              required: [name, role, machineType]
                              properties:
                                name:
                                  type: string
                                  maxLength: 40
                                role:
                                  type: string
                                  enum: [System, GPU]
                                machineType:
                                  type: string
                                diskSizeGb:
                                  type: integer
                                  default: 100
                                nodeCount:
                                  type: integer
                                  default: 1
                                minNodeCount:
                                  type: integer
                                  default: 0
                                maxNodeCount:
                                  type: integer
                                  default: 8
                                gpu:
                                  type: object
                                  properties:
                                    acceleratorType:
                                      type: string
                                    acceleratorCount:
                                      type: integer
                                      default: 1
                                zones:
                                  type: array
                                  items:
                                    type: string
          status:
            type: object
            properties:
              providerConfigRef:
                type: object
                properties:
                  name:
                    type: string
                    description: >-
                      Name of the ProviderConfig targeting the remote cluster.
                      Used by ModelPlacement to create resources on the cluster.
              gateway:
                type: object
                properties:
                  address:
                    type: string
                    description: >-
                      External IP of the KServe gateway on the remote cluster.
                      Used by ModelDeployment for unified endpoint routing.
              capacity:
                type: object
                description: >-
                  Declared capacity computed from the node pool config.
                properties:
                  backend:
                    type: string
                    description: >-
                      The backend type (copied from spec.backend).
                      Used by the deploy function for engine compatibility.
                  gpuPools:
                    type: array
                    description: >-
                      GPU pools with per-device VRAM. The env function
                      resolves VRAM from a static lookup table keyed by
                      acceleratorType.
                    items:
                      type: object
                      properties:
                        acceleratorType:
                          type: string
                        memory:
                          type: string
                          description: Per-GPU VRAM (e.g. "24Gi").
                        count:
                          type: integer
                          description: Total GPUs in this pool.
              namespace:
                type: string
                description: >-
                  Namespace where the GKECluster and KServeStack were created.
```

### Composition YAML

```yaml
apiVersion: apiextensions.crossplane.io/v1
kind: Composition
metadata:
  name: inferenceenvironments.modelplane.ai
spec:
  compositeTypeRef:
    apiVersion: modelplane.ai/v1alpha1
    kind: InferenceEnvironment
  mode: Pipeline
  pipeline:
  - functionRef:
      name: upbound-modelplane-infracompose-inference-env
    step: compose-inference-env
```

### Function behavior

```
compose-inference-env(req, rsp):

  1. Read InferenceEnvironment spec from observed composite.

  2. Derive a namespace name: "ie-{xr.metadata.name}".
     Compose a Namespace resource with that name.

  3. Always compose a GKECluster XR in that namespace:
     - Map spec.kserve.cluster.gke fields directly to GKECluster spec
     - Name: xr.metadata.name

  4. Read the observed GKECluster's status.secrets.
     Gate KServeStack on secrets being available — but keep emitting
     GKECluster unconditionally (see "Gating resources on dependencies").

  5. If secrets are available (or KServeStack already exists in observed
     state), compose a KServeStack XR in the same namespace:
     - Name: "{xr.metadata.name}-kserve"
     - spec.secrets: copied from GKECluster status.secrets
     - spec.versions.kserve: from spec.kserve.version (or default "v0.16.0")

  6. Read the observed KServeStack's status.gateway.address.
     (May not be available yet — that's fine, just don't populate it.)

  7. Compute capacity from node pool config using a static VRAM lookup table:

     GPU_VRAM = {
       "nvidia-l4": "24Gi",
       "nvidia-t4": "16Gi",
       "nvidia-a100-40gb": "40Gi",
       "nvidia-a100-80gb": "80Gi",
       "nvidia-h100-80gb": "80Gi",
       "nvidia-h100-mega-80gb": "80Gi",
       "nvidia-v100": "16Gi",
     }

     gpu_pools = []
     for pool in spec.kserve.cluster.gke.nodePools:
       if pool.role == "GPU" and pool.gpu:
         acc_type = pool.gpu.acceleratorType
         gpu_pools.append({
           "acceleratorType": acc_type,
           "memory": GPU_VRAM.get(acc_type, "0Gi"),
           "count": pool.gpu.acceleratorCount * pool.nodeCount,
         })

  8. Write to XR status:
     - status.providerConfigRef.name = "{xr.metadata.name}-kubeconfig"
       (This is the ProviderConfig name that compose-gke-cluster creates.
        It's deterministic from the GKECluster name.)
     - status.gateway.address = KServeStack's gateway address (if available)
     - status.namespace = the generated namespace name
     - status.capacity.backend = spec.backend (e.g., "KServe")
     - status.capacity.gpuPools = gpu_pools

  9. Set Ready condition based on GKECluster and KServeStack readiness.
```

### Example input

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceEnvironment
metadata:
  name: demo-us-central
  labels:
    modelplane.ai/region: us-central
spec:
  backend: KServe
  kserve:
    version: v0.16.0
    cluster:
      source: GKE
      gke:
        project: my-gcp-project
        region: us-central1
        nodePools:
        - name: system
          role: System
          machineType: e2-standard-4
          nodeCount: 1
          minNodeCount: 1
          maxNodeCount: 2
        - name: gpu-l4
          role: GPU
          machineType: g2-standard-8
          gpu:
            acceleratorType: nvidia-l4
            acceleratorCount: 1
          nodeCount: 1
          minNodeCount: 0
          maxNodeCount: 2
          zones:
          - us-central1-a
          - us-central1-c
```

### Expected composed resources

```
InferenceEnvironment: demo-us-central
├── Namespace: ie-demo-us-central
├── GKECluster: demo-us-central (in ie-demo-us-central)
│   ├── Network, Subnet, Cluster, NodePools, SA, SAKey, ProviderConfigs
│   └── status.secrets → [Kubeconfig, GCPServiceAccountKey]
└── KServeStack: demo-us-central-kserve (in ie-demo-us-central)
    ├── ProviderConfigs, cert-manager, Envoy Gateway, LWS, KServe, Gateway
    └── status.gateway.address → "34.x.x.x"
```

### Expected status when ready

```yaml
status:
  conditions:
  - type: Ready
    status: "True"
    reason: Available
  providerConfigRef:
    name: demo-us-central-kubeconfig
  gateway:
    address: "34.56.129.3"
  capacity:
    backend: KServe
    gpuPools:
    - acceleratorType: nvidia-l4
      memory: "24Gi"
      count: 1
  namespace: ie-demo-us-central
```

---

## 2. ClusterModel and Model

**API group:** `modelplane.ai/v1alpha1`  
**Scope:** ClusterModel is Cluster, Model is Namespaced  
**Function:** `compose-model` (in `functions/compose-model/main.py`) — shared by both

### What it does

Nothing. Both are data records — model catalog entries. `ClusterModel` is
cluster-scoped (platform team's curated catalog). `Model` is namespace-scoped
(ML team's private models). They share the same schema and the same composition
function. The function validates the spec and sets Ready. No resources are
composed.

### XRD

```yaml
apiVersion: apiextensions.crossplane.io/v2
kind: CompositeResourceDefinition
metadata:
  name: clustermodels.modelplane.ai
spec:
  group: modelplane.ai
  names:
    categories: [crossplane, modelplane]
    kind: ClusterModel
    plural: clustermodels
    shortNames: [cm]
  scope: Cluster
  versions:
  - name: v1alpha1
    served: true
    referenceable: true
    additionalPrinterColumns:
    - name: READY
      type: string
      jsonPath: .status.conditions[?(@.type=='Ready')].status
    - name: MODEL
      type: string
      jsonPath: .spec.model.name
    - name: ENGINE
      type: string
      jsonPath: .spec.engine
    - name: VRAM
      type: string
      jsonPath: .spec.resources.vram
    - name: AGE
      type: date
      jsonPath: .metadata.creationTimestamp
    schema:
      openAPIV3Schema:
        type: object
        required: [spec]
        properties:
          spec:
            type: object
            required: [model, source, engine, resources]
            properties:
              model:
                type: object
                required: [name]
                description: Model identity passed to the serving engine.
                properties:
                  name:
                    type: string
                    description: >-
                      Model name as the engine knows it
                      (e.g. "Qwen/Qwen2.5-0.5B-Instruct").
              source:
                type: string
                description: Where to download model weights from.
                enum: [HuggingFace]
              huggingFace:
                type: object
                description: >-
                  HuggingFace model source. Required when source is HuggingFace.
                required: [repo]
                properties:
                  repo:
                    type: string
                    description: >-
                      HuggingFace repo ID (e.g. "Qwen/Qwen2.5-0.5B-Instruct").
                  revision:
                    type: string
                    description: >-
                      Git revision (branch, tag, or commit). Defaults to main.
                    default: main
                  secretRef:
                    type: object
                    description: >-
                      Secret containing HuggingFace token for gated models.
                    properties:
                      name:
                        type: string
                      namespace:
                        type: string
                      key:
                        type: string
              engine:
                type: string
                description: Inference engine.
                enum: [vLLM]
              vllm:
                type: object
                description: >-
                  vLLM-specific configuration. Only applies when engine is vLLM.
                properties:
                  image:
                    type: string
                    default: "vllm/vllm-openai:v0.7.3"
                  extraArgs:
                    type: array
                    description: >-
                      Additional CLI arguments passed to vLLM
                      (e.g. ["--max-model-len=4096"]).
                    items:
                      type: string
              resources:
                type: object
                required: [vram]
                description: >-
                  Hardware requirements. The model specifies total VRAM
                  needed. The scheduler computes GPU count per-environment
                  based on available per-GPU VRAM.
                properties:
                  vram:
                    type: string
                    description: >-
                      Total VRAM the model needs (e.g. "24Gi", "140Gi").
                      The scheduler divides this by the per-GPU VRAM of
                      each candidate pool to determine how many GPUs are
                      required.
                  cpu:
                    type: string
                    default: "4"
                  memory:
                    type: string
                    default: "16Gi"
          status:
            type: object
            description: >-
              ClusterModel has no composed resources. Status is conditions only.
```

### XRD (Model — namespace-scoped)

Identical schema to ClusterModel above, with these differences:

```yaml
apiVersion: apiextensions.crossplane.io/v2
kind: CompositeResourceDefinition
metadata:
  name: models.modelplane.ai
spec:
  group: modelplane.ai
  names:
    categories: [crossplane, modelplane]
    kind: Model
    plural: models
  scope: Namespaced
  # ... same versions/schema as ClusterModel
```

### Composition YAML

Two compositions, both referencing the same function:

```yaml
apiVersion: apiextensions.crossplane.io/v1
kind: Composition
metadata:
  name: clustermodels.modelplane.ai
spec:
  compositeTypeRef:
    apiVersion: modelplane.ai/v1alpha1
    kind: ClusterModel
  mode: Pipeline
  pipeline:
  - functionRef:
      name: upbound-modelplane-infracompose-model
    step: compose-model
---
apiVersion: apiextensions.crossplane.io/v1
kind: Composition
metadata:
  name: models.modelplane.ai
spec:
  compositeTypeRef:
    apiVersion: modelplane.ai/v1alpha1
    kind: Model
  mode: Pipeline
  pipeline:
  - functionRef:
      name: upbound-modelplane-infracompose-model
    step: compose-model
```

### Function behavior

```
compose-model(req, rsp):

  1. Read ClusterModel spec from observed composite.
  2. Validate: if engine is "vLLM", spec.vllm should exist (warning if not).
  3. Set Ready=True, reason=Available.
  4. That's it. No resources to compose.
```

The function exists because every XRD needs a Composition and every Composition
needs at least one pipeline step. The function is trivially simple — it's just
the readiness gate.

### Demo ClusterModel

This is the model we use for the end-to-end demo. Qwen 2.5 0.5B Instruct is
small enough to run on a single L4 GPU with minimal memory.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ClusterModel
metadata:
  name: qwen-0.5b-vllm
  labels:
    modelplane.ai/family: qwen
    modelplane.ai/size: 0.5b
spec:
  model:
    name: Qwen/Qwen2.5-0.5B-Instruct
  source: HuggingFace
  huggingFace:
    repo: Qwen/Qwen2.5-0.5B-Instruct
  engine: vLLM
  vllm:
    image: vllm/vllm-openai:v0.7.3
  resources:
    vram: "2Gi"    # Qwen 0.5B is tiny — fits easily on any GPU
    cpu: "3"
    memory: "10Gi"
```

---

## 3. ModelPlacement

**API group:** `modelplane.ai/v1alpha1`  
**Scope:** Namespaced  
**Function:** `compose-model-placement` (in `functions/compose-model-placement/main.py`)

### What it does

Deploys one model on one InferenceEnvironment. Reads the referenced ClusterModel
and InferenceEnvironment via required resources, then composes a KServe
`LLMInferenceService` on the remote cluster using a provider-kubernetes Object.

### XRD

```yaml
apiVersion: apiextensions.crossplane.io/v2
kind: CompositeResourceDefinition
metadata:
  name: modelplacements.modelplane.ai
spec:
  group: modelplane.ai
  names:
    categories: [crossplane, modelplane]
    kind: ModelPlacement
    plural: modelplacements
    shortNames: [mp]
  scope: Namespaced
  versions:
  - name: v1alpha1
    served: true
    referenceable: true
    additionalPrinterColumns:
    - name: READY
      type: string
      jsonPath: .status.conditions[?(@.type=='Ready')].status
    - name: MODEL
      type: string
      jsonPath: .spec.modelRef.name
    - name: ENVIRONMENT
      type: string
      jsonPath: .spec.inferenceEnvironmentRef.name
    - name: ENDPOINT
      type: string
      jsonPath: .status.endpoint.url
    - name: AGE
      type: date
      jsonPath: .metadata.creationTimestamp
    schema:
      openAPIV3Schema:
        type: object
        required: [spec]
        properties:
          spec:
            type: object
            required: [modelRef, inferenceEnvironmentRef]
            properties:
              modelRef:
                type: object
                required: [name]
                description: Reference to a ClusterModel or Model.
                properties:
                  kind:
                    type: string
                    description: >-
                      Kind of model resource. Defaults to ClusterModel.
                    enum: [ClusterModel, Model]
                    default: ClusterModel
                  name:
                    type: string
              inferenceEnvironmentRef:
                type: object
                required: [name]
                description: Reference to an InferenceEnvironment.
                properties:
                  name:
                    type: string
              replicas:
                type: integer
                default: 1
                minimum: 1
                maximum: 8
                description: >-
                  Number of model server replicas. Fixed — no autoscaling
                  in the MVP.
          status:
            type: object
            properties:
              endpoint:
                type: object
                properties:
                  url:
                    type: string
                    description: >-
                      Per-placement endpoint URL on the remote cluster's
                      gateway. Format:
                      http://<gateway-ip>/<namespace>/<llmis-name>/v1
              model:
                type: object
                properties:
                  name:
                    type: string
                    description: Resolved model name from ClusterModel.
              resources:
                type: object
                description: >-
                  Resources consumed by this placement. Copied from the
                  model spec at placement time. Used by the deploy function
                  to compute available capacity per environment.
                properties:
                  gpu:
                    type: object
                    properties:
                      count:
                        type: integer
```

### Required resources

The function needs to read two resources it doesn't own:

1. **ClusterModel** — referenced by `spec.modelRef.name`. Cluster-scoped.
2. **InferenceEnvironment** — referenced by `spec.inferenceEnvironmentRef.name`. Cluster-scoped.

These are requested via the Composition YAML's bootstrap requirements OR
dynamically via `rsp.requirements.resources`. The dynamic approach is needed
because the names come from the XR spec (not known at Composition authoring
time).

### Composition YAML

```yaml
apiVersion: apiextensions.crossplane.io/v1
kind: Composition
metadata:
  name: modelplacements.modelplane.ai
spec:
  compositeTypeRef:
    apiVersion: modelplane.ai/v1alpha1
    kind: ModelPlacement
  mode: Pipeline
  pipeline:
  - functionRef:
      name: upbound-modelplane-infracompose-model-placement
    step: compose-model-placement
```

### Function behavior

```
compose-model-placement(req, rsp):

  1. Read ModelPlacement spec from observed composite.

  2. Request required resources (every reconcile, not just the first):
     model_kind = spec.get("modelRef", {}).get("kind", "ClusterModel")
     response.require_resources(rsp,
       name="model",
       api_version="modelplane.ai/v1alpha1",
       kind=model_kind,  # ClusterModel or Model
       match_name=spec["modelRef"]["name"],
     )
     response.require_resources(rsp,
       name="environment",
       api_version="modelplane.ai/v1alpha1",
       kind="InferenceEnvironment",
       match_name=spec["inferenceEnvironmentRef"]["name"],
     )

  3. Read required resources:
     model = request.get_required_resource(req, "model")
     ie = request.get_required_resource(req, "environment")
     If either is None → return early
     (Crossplane will re-call with the resolved resources).

  4. Extract from InferenceEnvironment:
     - pc_name = ie.status.providerConfigRef.name
     - gateway_address = ie.status.gateway.address
     - ie_namespace = ie.status.namespace
     If pc_name is not available → set Ready=False "Waiting for environment", return.

  5. Extract from ClusterModel (or Model):
     - model_name = cm.spec.model.name
     - model_repo = cm.spec.huggingFace.repo (when source is HuggingFace)
     - model_uri = "hf://" + model_repo  (the URI format vLLM/KServe expect)
     - image = cm.spec.vllm.image (or default "vllm/vllm-openai:v0.7.3")
     - model_vram = cm.spec.resources.vram (e.g., "2Gi")
     - cpu = cm.spec.resources.cpu (or "4")
     - memory = cm.spec.resources.memory (or "16Gi")
     - extra_args = cm.spec.vllm.extraArgs (or [])

  5a. Compute GPU count from model VRAM and environment capacity:
      - Find the first GPU pool in the IE with enough per-GPU VRAM:
        pool = first pool where parse_quantity(pool.memory) > 0
        (For the MVP, environments typically have one GPU pool.)
      - gpus_per_replica = ceil(parse_quantity(model_vram) / parse_quantity(pool.memory))
      - replicas = spec.replicas or 1
      - total_gpus = gpus_per_replica * replicas

      vLLM auto-detects the number of GPUs available to each pod and uses
      tensor parallelism across them. Setting nvidia.com/gpu on the
      container is sufficient — no explicit --tensor-parallel-size flag
      needed.

  6. Derive a stable LLMInferenceService name:
     llmis_name = xr.metadata.name
     llmis_namespace = "default"  # On the remote cluster

  7. Compose a provider-kubernetes Object wrapping an LLMInferenceService:

     resource.update(rsp.desired.resources["llm-inference-service"], {
       "apiVersion": "kubernetes.crossplane.io/v1alpha2",
       "kind": "Object",
       "spec": {
         "providerConfigRef": {"name": pc_name},
         "forProvider": {
           "manifest": {
             "apiVersion": "serving.kserve.io/v1alpha1",
             "kind": "LLMInferenceService",
             "metadata": {
               "name": llmis_name,
               "namespace": llmis_namespace,
             },
             "spec": {
               "model": {
                 "uri": model_uri,
                 "name": model_name,
               },
               "replicas": spec.replicas or 1,
               "template": {
                 "containers": [{
                   "name": "main",
                   "image": image,
                   "securityContext": {
                     "runAsUser": 0,
                     "runAsNonRoot": False,
                   },
                    "resources": {
                     "limits": {
                       "nvidia.com/gpu": str(gpus_per_replica),
                       "cpu": cpu,
                       "memory": memory,
                     },
                     "requests": {
                       "cpu": "1",
                       "memory": memory,
                     },
                   },
                 }],
               },
               "router": {
                 "gateway": {},
                 "route": {},
               },
             },
           },
         },
       },
     })

  8. Write to XR status:
     - status.model.name = model_name
     - status.resources.gpu.count = total_gpus  (gpus_per_replica × replicas)
     - status.endpoint.url = "http://{gateway_address}/{llmis_namespace}/{llmis_name}/v1"
       (Only if gateway_address is available. This is the path KServe's
        managed routing uses — see kserve-gke-validation.md Run 3.)

  9. Set Ready condition based on the Object's readiness.
```

### Important: vLLM runs as root

The `securityContext` override (`runAsUser: 0, runAsNonRoot: false`) is required
because `vllm/vllm-openai:v0.7.3` runs as root but KServe defaults to
`runAsNonRoot: true`. See `context/kserve-gke-validation.md` issue #5.

### Important: KServe managed routing

The `router: {gateway: {}, route: {}}` block tells KServe to create an
HTTPRoute on the remote cluster wiring the model to `kserve-ingress-gateway`.
The resulting path is `/{namespace}/{name}/v1/chat/completions`. This was
validated in test Run 3 — see `context/kserve-gke-validation.md`.

### Example input

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelPlacement
metadata:
  name: qwen-demo-us-central
  namespace: ml-team
spec:
  modelRef:
    name: qwen-0.5b-vllm
  inferenceEnvironmentRef:
    name: demo-us-central
  replicas: 1
```

### Expected composed resources

```
ModelPlacement: qwen-demo-us-central (in ml-team)
└── Object: llm-inference-service
    └── LLMInferenceService: qwen-demo-us-central (in default, on remote cluster)
        └── [KServe creates: Deployment, InferencePool, InferenceModel, HTTPRoute]
```

### Expected status when ready

```yaml
status:
  conditions:
  - type: Ready
    status: "True"
  endpoint:
    url: "http://34.56.129.3/default/qwen-demo-us-central/v1"
  model:
    name: Qwen/Qwen2.5-0.5B-Instruct
  resources:
    gpu:
      count: 1
```

---

## 4. ModelDeployment

**API group:** `modelplane.ai/v1alpha1`  
**Scope:** Namespaced  
**Function:** `compose-model-deployment` (in `functions/compose-model-deployment/main.py`)

### What it does

The consumer-facing API. Creates one ModelPlacement per matched
InferenceEnvironment, composes Envoy Gateway routing resources on the control
plane, and surfaces a unified endpoint URL in status.

### XRD

```yaml
apiVersion: apiextensions.crossplane.io/v2
kind: CompositeResourceDefinition
metadata:
  name: modeldeployments.modelplane.ai
spec:
  group: modelplane.ai
  names:
    categories: [crossplane, modelplane]
    kind: ModelDeployment
    plural: modeldeployments
    shortNames: [md]
  scope: Namespaced
  versions:
  - name: v1alpha1
    served: true
    referenceable: true
    additionalPrinterColumns:
    - name: READY
      type: string
      jsonPath: .status.conditions[?(@.type=='Ready')].status
    - name: MODEL
      type: string
      jsonPath: .spec.modelRef.name
    - name: ENVS
      type: string
      jsonPath: .status.placements.ready
    - name: ENDPOINT
      type: string
      jsonPath: .status.endpoint.url
    - name: AGE
      type: date
      jsonPath: .metadata.creationTimestamp
    schema:
      openAPIV3Schema:
        type: object
        required: [spec]
        properties:
          spec:
            type: object
            required: [modelRef, environments]
            properties:
              modelRef:
                type: object
                required: [name]
                description: Reference to a ClusterModel or Model.
                properties:
                  kind:
                    type: string
                    description: >-
                      Kind of model resource. Defaults to ClusterModel.
                    enum: [ClusterModel, Model]
                    default: ClusterModel
                  name:
                    type: string
              environments:
                type: integer
                minimum: 1
                maximum: 10
                description: >-
                  How many InferenceEnvironments to deploy to.
              environmentSelector:
                type: object
                description: >-
                  Optional label selector to filter environments.
                  If omitted, all ready environments are candidates.
                properties:
                  matchLabels:
                    type: object
                    x-kubernetes-preserve-unknown-fields: true
              replicas:
                type: integer
                default: 1
                minimum: 1
                maximum: 8
                description: >-
                  Replicas per placement. Passed through to each
                  ModelPlacement.
          status:
            type: object
            properties:
              endpoint:
                type: object
                properties:
                  url:
                    type: string
                    description: >-
                      Unified OpenAI-compatible endpoint URL. Routes
                      across all healthy placements.
              placements:
                type: object
                properties:
                  total:
                    type: integer
                  ready:
                    type: integer
              model:
                type: object
                properties:
                  name:
                    type: string
```

### Required resources

The function needs:

1. **All InferenceEnvironments** — to select which ones to target.
2. **The ClusterModel** — to read the model name for status.

### Composition YAML

```yaml
apiVersion: apiextensions.crossplane.io/v1
kind: Composition
metadata:
  name: modeldeployments.modelplane.ai
spec:
  compositeTypeRef:
    apiVersion: modelplane.ai/v1alpha1
    kind: ModelDeployment
  mode: Pipeline
  pipeline:
  - functionRef:
      name: upbound-modelplane-infracompose-model-deployment
    step: compose-model-deployment
    requirements:
      requiredResources:
      - requirementName: gateway
        apiVersion: gateway.networking.k8s.io/v1
        kind: Gateway
        name: modelplane
        namespace: modelplane-system
```

### Function behavior

```
compose-model-deployment(req, rsp):

  1. Read ModelDeployment spec from observed composite.
     xr_name = xr.metadata.name
     xr_ns = xr.metadata.namespace

  2. Request required resources (every reconcile):
     model_kind = spec.get("modelRef", {}).get("kind", "ClusterModel")
     response.require_resources(rsp,
       name="environments",
       api_version="modelplane.ai/v1alpha1",
       kind="InferenceEnvironment",
       match_labels={},  # Empty = match all
     )
     response.require_resources(rsp,
       name="model",
       api_version="modelplane.ai/v1alpha1",
       kind=model_kind,  # ClusterModel or Model
       match_name=spec["modelRef"]["name"],
     )
     response.require_resources(rsp,
       name="all-placements",
       api_version="modelplane.ai/v1alpha1",
       kind="ModelPlacement",
       match_labels={},  # All placements across all namespaces
     )

  3. Read required resources:
     envs = request.get_required_resources(req, "environments")
     model = request.get_required_resource(req, "model")
     all_placements = request.get_required_resources(req, "all-placements")
     If envs is empty → set Ready=False "No environments found", return.
     If model is None → set Ready=False "Model not found", return.

  4. Schedule: filter and rank environments.

     a. Engine compatibility:
        - Build a compat map: {"KServe": ["vLLM"]}
        - For each environment, check that model.spec.engine is in
          compat_map[env.status.capacity.backend].
        - Skip incompatible environments.

     b. VRAM and capacity:
        - Parse model.spec.resources.vram as a quantity (e.g., "140Gi").
        - For each candidate environment, compute GPU requirements per-pool:
            For each pool in env.status.capacity.gpuPools:
              if parse_quantity(pool.memory) <= 0: skip
              gpus_needed = ceil(model_vram / pool.memory)
              → this pool could fit the model using gpus_needed GPUs
            eligible_gpus = sum(pool.count for eligible pools)
        - Compute used GPUs from existing placements:
            used_gpus = sum(
              p.status.resources.gpu.count
              for p in all_placements
              if p.spec.inferenceEnvironmentRef.name == env_name
            )
        - available = eligible_gpus - used_gpus
        - gpus_needed = min gpus_needed across eligible pools
        - Skip environments where available < gpus_needed.

        For quantity parsing, use a simple helper that converts Kubernetes
        resource quantities ("24Gi", "80Gi") to bytes for comparison. Only
        Gi and Mi suffixes are needed for VRAM.

     c. Label selector:
        - If spec.environmentSelector.matchLabels is set, skip environments
          whose labels don't match.

     d. Readiness:
        - Only include environments with Ready=True condition.

     e. Sort remaining environments by name (for determinism).
        Take the first N where N = spec.environments.
        If fewer than N match → set Ready=False with reason explaining
        why (e.g., "0 of 2 environments have enough available GPUs"),
        but still create placements for what we have.

  5. For each matched environment, compose a ModelPlacement XR:
     placement_name = "{xr_name}-{ie_name}"  (truncated to 63 chars)

      resource.update(rsp.desired.resources[f"placement-{ie_name}"], {
        "apiVersion": "modelplane.ai/v1alpha1",
        "kind": "ModelPlacement",
        "metadata": {
          "namespace": xr_ns,
          "labels": {
            "modelplane.ai/deployment": xr_name,
          },
        },
        "spec": {
          "modelRef": {"name": spec["modelRef"]["name"]},
          "inferenceEnvironmentRef": {"name": ie_name},
          "replicas": spec.get("replicas", 1),
        },
      })

  6. Read observed ModelPlacements to get their endpoint URLs and readiness.
     For each observed placement:
       - Check Ready condition
       - Read status.endpoint.url (if available)

   7. Read the gateway address from each targeted InferenceEnvironment's status.
      For each environment with a gateway address, compose routing resources
      on the control plane.

      These are composed DIRECTLY as Kubernetes resources — no Object wrapper
      needed because they live on the control plane cluster. (See "Composing
      resources on the control plane vs remote clusters" in the guidance section.)

      For each placement with a gateway address:

        resource.update(rsp.desired.resources[f"backend-{ie_name}"], {
          "apiVersion": "gateway.envoyproxy.io/v1alpha1",
          "kind": "Backend",
          "metadata": {
            "namespace": xr_ns,
          },
          "spec": {
            "endpoints": [{
              "fqdn": {
                "hostname": gateway_address,
                "port": 80,
              },
            }],
          },
        })

      Compose one HTTPRoute with backendRefs to all Backends:

      backend_refs = []
      for ie_name in matched_environments_with_gateway:
        backend_refs.append({
          "group": "gateway.envoyproxy.io",
          "kind": "Backend",
          "name": get_composed_resource_name("backend-{ie_name}"),
          "port": 80,
          "weight": 1,
        })

      resource.update(rsp.desired.resources["httproute"], {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {
          "namespace": xr_ns,
        },
        "spec": {
          "parentRefs": [{
            "name": "modelplane",
            "namespace": "modelplane-system",
          }],
          "rules": [{
            "matches": [{
              "path": {
                "type": "PathPrefix",
                "value": f"/{xr_ns}/{xr_name}/",
              },
            }],
            "filters": [{
              "type": "URLRewrite",
              "urlRewrite": {
                "path": {
                  "type": "ReplacePrefixMatch",
                  "replacePrefixMatch": "/",
                },
              },
            }],
            "backendRefs": backend_refs,
          }],
        },
      })

  8. Get the control plane Gateway's address for the endpoint URL.
      The Gateway is a bootstrap required resource (declared in the Composition
      YAML, not dynamically). It's available from the first reconcile:

      gateway = request.get_required_resource(req, "gateway")
      gateway_ip = gateway["status"]["addresses"][0]["value"]

  9. Write to XR status:
     - status.model.name = model name from ClusterModel
     - status.placements.total = len(matched_environments)
     - status.placements.ready = count of placements with Ready=True
     - status.endpoint.url = "http://{gateway_ip}/{xr_ns}/{xr_name}/v1/chat/completions"
       (Only if gateway_ip is available.)

  10. Set Ready condition:
      - True if at least one placement is Ready AND httproute is composed
      - False otherwise, with message listing what's pending
```

### A note on Backend resource names

Crossplane generates names for composed resources — the function doesn't set
`metadata.name` directly. The HTTPRoute's `backendRefs` need to reference
Backend resources by name. Here's how to handle this:

On the first reconcile, the function composes Backends and the HTTPRoute with
empty `backendRefs`. On the second reconcile, it reads the observed Backends'
generated names from their metadata and populates the HTTPRoute's `backendRefs`.
This adds a reconcile cycle but is straightforward.

```python
# Read the Crossplane-generated name from an observed Backend
backend_observed = req.observed.resources.get(f"backend-{ie_name}")
if backend_observed:
    d = resource.struct_to_dict(backend_observed.resource)
    backend_name = d["metadata"]["name"]
    backend_refs.append({
        "group": "gateway.envoyproxy.io",
        "kind": "Backend",
        "name": backend_name,
        "port": 80,
        "weight": 1,
    })
```

An extra reconcile is fine for a demo.

### Example input

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelDeployment
metadata:
  name: qwen-demo
  namespace: ml-team
spec:
  modelRef:
    name: qwen-0.5b-vllm
  environments: 1
  replicas: 1
```

### Expected composed resources

```
ModelDeployment: qwen-demo (in ml-team)
├── ModelPlacement: qwen-demo-demo-us-central (in ml-team)
│   └── Object: LLMInferenceService on remote cluster
├── Backend: (generated name, in ml-team)
│   └── endpoints: [{fqdn: {hostname: "34.56.129.3", port: 80}}]
└── HTTPRoute: (generated name, in ml-team)
    └── routes /{ml-team}/{qwen-demo}/* → Backend
```

### Expected status when ready

```yaml
status:
  conditions:
  - type: Ready
    status: "True"
    reason: PlacementsAvailable
  endpoint:
    url: "http://10.0.0.50/ml-team/qwen-demo/v1/chat/completions"
  placements:
    total: 1
    ready: 1
  model:
    name: Qwen/Qwen2.5-0.5B-Instruct
```

---

## Required change to KServeStack

The KServeStack function (`functions/compose-kserve-stack/main.py`) needs one
addition: **populate `status.gateway.address`** from the observed Gateway
Object's status.

The compose-kserve-stack function already composes a Gateway Object. It needs to
read the observed Gateway Object's status to extract the external IP and write it
to the XR's `status.gateway.address`.

```python
# After the existing gateway composition code, add:
gateway_observed = req.observed.resources.get("gateway")
if gateway_observed:
    gw = resource.struct_to_dict(gateway_observed.resource)
    addresses = (
        gw.get("status", {})
        .get("atProvider", {})
        .get("manifest", {})
        .get("status", {})
        .get("addresses", [])
    )
    if addresses:
        gateway_address = addresses[0].get("value")
        if gateway_address:
            resource.update(rsp.desired.composite, {
                "status": {
                    "gateway": {"address": gateway_address},
                },
            })
```

The path is `status.atProvider.manifest.status.addresses[0].value` because this
is a provider-kubernetes Object — the actual Gateway status is nested inside the
Object's `atProvider.manifest`.

---

## Demo scenario

End-to-end, this is what someone types to see Modelplane work:

### 1. Platform team: create the environment

```bash
kubectl apply -f examples/inferenceenvironment/demo.yaml
# Wait ~15 minutes for GKE cluster + KServe stack
kubectl get ie demo-us-central
# NAME               READY   REGION        GATEWAY        AGE
# demo-us-central    True    us-central1   34.56.129.3    15m
```

### 2. Platform team: register a model

```bash
kubectl apply -f examples/clustermodel/qwen-0.5b.yaml
kubectl get clustermodels
# NAME              READY   MODEL                          ENGINE   GPUS   AGE
# qwen-0.5b-vllm   True    Qwen/Qwen2.5-0.5B-Instruct    vLLM     1      5s
```

### 3. ML team: deploy the model

```bash
kubectl apply -f examples/modeldeployment/qwen-demo.yaml
# Wait ~5 minutes for model to download and start
kubectl get md -n ml-team
# NAME         READY   MODEL              ENVS   ENDPOINT                                                   AGE
# qwen-demo    True    qwen-0.5b-vllm     1      http://10.0.0.50/ml-team/qwen-demo/v1/chat/completions    5m
```

### 4. ML team: use the endpoint

```bash
ENDPOINT=$(kubectl get md qwen-demo -n ml-team -o jsonpath='{.status.endpoint.url}')
curl $ENDPOINT \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen2.5-0.5B-Instruct",
    "messages": [{"role": "user", "content": "What is Crossplane?"}],
    "max_tokens": 100
  }'
```

---

## Don't list

Things that are explicitly out of scope for this MVP:

- **Don't implement autoscaling.** Fixed replicas only. No KEDA, no HPA, no
  custom metrics.
- **Don't implement dynamic capacity discovery.** Capacity is computed from the
  node pool config, not discovered from the running cluster. If nodes autoscale,
  the declared capacity may not reflect reality.
- **Don't implement model source types beyond HuggingFace.** S3, GCS, and PVC
  sources are future work.
- **Don't implement `source: Existing` on InferenceEnvironment.** GKE
  provisioning only. Bring-your-own-cluster is a future path.
- **Don't implement BackendTrafficPolicy.** Envoy Gateway's default round-robin
  behavior on equal-weight backendRefs is sufficient.
- **Don't implement TLS.** Everything is HTTP. The gateway listeners are port 80.
- **Don't implement health checking of Backend endpoints.** If a placement goes
  unhealthy, it stays in the HTTPRoute's backendRefs. The deploy function
  removes it on the next reconcile when it sees the placement is no longer
  Ready, but there's no active health probing.
- **Don't implement model caching (LocalModelNodeGroup).** Models download from
  HuggingFace on every pod start. This is slow but acceptable for a demo with a
  small model.
- **Don't compose the control plane Envoy Gateway.** It's a prerequisite, not
  something the functions manage.
- **Don't implement immutable deployments.** Changes to ClusterModel propagate
  immediately.
- **Don't worry about InferenceEnvironment deletion.** If an IE is deleted while
  placements target it, those placements will fail. That's fine.

---

## Test cases

Each function should have a CompositionTest (in `tests/`) following the pattern
established by `tests/test-gkecluster/main.py` and
`tests/test-kservestack/main.py`.

### compose-inference-env

**Test: creates GKECluster and KServeStack when secrets are available**

Given:
- Observed XR: InferenceEnvironment with GKE config
- Observed resource "gke-cluster" with `status.secrets`:
  `[{type: Kubeconfig, name: demo-kubeconfig, key: kubeconfig}, {type: GCPServiceAccountKey, name: demo-sa-key, key: private_key}]`

Assert:
- Desired resources include "gke-cluster" (GKECluster) with correct project/region/nodePools
- Desired resources include "kserve-stack" (KServeStack) with `spec.secrets` matching the GKECluster's status.secrets
- Desired composite has `status.providerConfigRef.name` set

**Test: waits when GKECluster has no secrets yet**

Given:
- Observed XR: InferenceEnvironment with GKE config
- No observed "gke-cluster" resource

Assert:
- Desired resources include "gke-cluster"
- Desired resources do NOT include "kserve-stack"
- Ready condition is False with reason "Creating"

### compose-model

**Test: sets Ready for ClusterModel**

Given:
- Observed XR: ClusterModel with valid spec

Assert:
- Ready condition is True
- No composed resources

### compose-model-placement

**Test: composes LLMInferenceService when required resources are available**

Given:
- Observed XR: ModelPlacement with modelRef and inferenceEnvironmentRef
- Required resources include a ClusterModel with model name and vLLM config
- Required resources include an InferenceEnvironment with `status.providerConfigRef.name: "demo-kubeconfig"` and `status.gateway.address: "34.56.129.3"`

Assert:
- Desired resources include "llm-inference-service" (Object)
- The Object's manifest is an LLMInferenceService with correct model name, URI, image, GPU count
- The Object's providerConfigRef.name is "demo-kubeconfig"
- Desired composite has `status.endpoint.url` containing "34.56.129.3"
- Desired composite has `status.model.name` set
- Desired composite has `status.resources.gpu.count` set

**Test: requests required resources when not provided**

Given:
- Observed XR: ModelPlacement with modelRef.name="qwen" and inferenceEnvironmentRef.name="demo"
- No required resources provided

Assert:
- Response requirements include a resource selector for ClusterModel "qwen"
- Response requirements include a resource selector for InferenceEnvironment "demo"

### compose-model-deployment

**Test: creates placements and routing for one environment**

Given:
- Observed XR: ModelDeployment with environments=1
- Required resources include one ready InferenceEnvironment "demo-us-central"
  with gateway.address="34.56.129.3", capacity.backend="KServe",
  capacity.gpuPools=[{acceleratorType: nvidia-l4, memory: "24Gi", count: 1}]
- Required resources include ClusterModel "qwen-0.5b-vllm" with engine=vLLM,
  resources.vram="2Gi"
- Required resources include no existing ModelPlacements

Assert:
- Desired resources include "placement-demo-us-central" (ModelPlacement)
- Desired resources include "backend-demo-us-central" (Backend) with endpoint hostname "34.56.129.3"
- Desired resources include "httproute" (HTTPRoute) with path prefix matching

**Test: filters environments by selector**

Given:
- Observed XR: ModelDeployment with environmentSelector.matchLabels: {region: us-central}
- Required resources include two InferenceEnvironments:
  - "demo-us-central" with label region=us-central, Ready=True,
    capacity.gpuPools=[{memory: "24Gi", count: 1}]
  - "demo-eu-west" with label region=eu-west, Ready=True,
    capacity.gpuPools=[{memory: "24Gi", count: 1}]

Assert:
- Only "placement-demo-us-central" is created
- "placement-demo-eu-west" is NOT created

**Test: skips environments that aren't ready**

Given:
- Required resources include one InferenceEnvironment with Ready=False

Assert:
- No placements created
- Ready=False with message about no ready environments

**Test: schedules based on VRAM and available capacity**

Given:
- Observed XR: ModelDeployment with environments=1
- Required resources include ClusterModel with resources.vram="140Gi"
- Required resources include three InferenceEnvironments (all Ready=True):
  - "env-l4" with gpuPools=[{memory: "24Gi", count: 4}]
    → model needs ceil(140/24)=6 GPUs, pool has 4. Won't fit.
  - "env-h100" with gpuPools=[{memory: "80Gi", count: 8}]
    → model needs ceil(140/80)=2 GPUs, pool has 8. Fits.
  - "env-busy-h100" with gpuPools=[{memory: "80Gi", count: 8}]
    → model needs 2 GPUs, but 7 are in use. Available=1. Won't fit.
- Required resources include existing ModelPlacements targeting "env-busy-h100"
  with total status.resources.gpu.count=7

Assert:
- "env-l4" is skipped (needs 6 GPUs, only 4 available)
- "env-busy-h100" is skipped (needs 2 GPUs, only 1 available)
- "env-h100" is selected (needs 2, has 8 available)
- Only "placement-env-h100" is created

**Test: skips environments with incompatible backend**

Given:
- Required resources include ClusterModel with engine=vLLM
- Required resources include one InferenceEnvironment with
  capacity.backend="SomeUnsupportedBackend", Ready=True

Assert:
- No placements created
- Ready=False with message about no compatible environments

---

## File layout

When complete, the repo should look like:

```
modelplane/
├── apis/
│   ├── gkeclusters/              # (existing)
│   │   ├── definition.yaml
│   │   └── composition.yaml
│   ├── kservestacks/             # (existing)
│   │   ├── definition.yaml
│   │   └── composition.yaml
│   ├── inferenceenvironments/    # (new)
│   │   ├── definition.yaml
│   │   └── composition.yaml
│   ├── clustermodels/            # (new)
│   │   ├── definition.yaml
│   │   └── composition.yaml
│   ├── models/                   # (new)
│   │   ├── definition.yaml
│   │   └── composition.yaml
│   ├── modelplacements/          # (new)
│   │   ├── definition.yaml
│   │   └── composition.yaml
│   └── modeldeployments/         # (new)
│       ├── definition.yaml
│       └── composition.yaml
├── functions/
│   ├── compose-gke-cluster/      # (existing)
│   ├── compose-kserve-stack/     # (existing, needs gateway address change)
│   ├── compose-inference-env/    # (new)
│   │   └── main.py
│   ├── compose-model/            # (new, shared by ClusterModel + Model)
│   │   └── main.py
│   ├── compose-model-placement/  # (new)
│   │   └── main.py
│   └── compose-model-deployment/ # (new)
│       └── main.py
├── examples/
│   ├── inferenceenvironment/     # (new)
│   │   └── demo.yaml
│   ├── clustermodel/             # (new)
│   │   └── qwen-0.5b.yaml
│   ├── modeldeployment/          # (new)
│   │   └── qwen-demo.yaml
│   └── ...                       # (existing examples)
├── tests/
│   ├── test-gkecluster/          # (existing)
│   ├── test-kservestack/         # (existing)
│   ├── test-inference-env/       # (new)
│   │   └── main.py
│   ├── test-cluster-model/       # (new)
│   │   └── main.py
│   ├── test-model/               # (new, optional — same function as ClusterModel)
│   │   └── main.py
│   ├── test-model-placement/     # (new)
│   │   └── main.py
│   └── test-model-deployment/    # (new)
│       └── main.py
└── design/
    ├── design.md                 # (existing)
    └── mvp-spec.md               # (this file)
```
