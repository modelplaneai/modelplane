# Crossplane v2.2 technical deep-dive

**Crossplane v2.2, released early February 2026, consolidates the v2 architecture around namespaced XRs, the function pipeline as the sole composition engine, required schemas (new), the alpha pipeline inspector, and Operations.** This report covers the composition layer internals, the Python composition ecosystem, the packaging system including MRDs/MRAPs, and end-to-end design patterns for building custom platform APIs. All code examples and API shapes reflect v2.2 HEAD.

---

## 1. The composition function pipeline at the proto level

Every composition and operation in v2 runs through the same gRPC pipeline. Crossplane calls each function sequentially via `FunctionRunnerService.RunFunction`, threading desired state and context through the chain.

### Proto service and messages

```protobuf
service FunctionRunnerService {
  rpc RunFunction(RunFunctionRequest) returns (RunFunctionResponse) {}
}

message RunFunctionRequest {
  RequestMeta meta = 1;
  State observed = 2;
  State desired = 3;
  optional google.protobuf.Struct input = 4;
  optional google.protobuf.Struct context = 5;
  map<string, Resources> extra_resources = 6 [deprecated = true];
  map<string, Credentials> credentials = 7;
  map<string, Resources> required_resources = 8;
  map<string, Schema> required_schemas = 9;
}

message RunFunctionResponse {
  ResponseMeta meta = 1;
  State desired = 2;
  repeated Result results = 3;
  optional google.protobuf.Struct context = 4;
  Requirements requirements = 5;
  repeated Condition conditions = 6;
  optional google.protobuf.Struct output = 7;
}
```

The `State` message carries both composite and composed resources:

```protobuf
message State {
  Resource composite = 1;
  map<string, Resource> resources = 2;
}

message Resource {
  google.protobuf.Struct resource = 1;
  map<string, bytes> connection_details = 2;
  Ready ready = 3;
}
```

**Observed state is frozen** — Crossplane snapshots the XR and all composed resources once before the pipeline starts. Every step receives the identical `observed`. **Desired state accumulates** — each function receives the previous function's `desired` output, modifies it, and passes it forward. The critical contract: a function **must** copy all desired state it does not modify. The SDK helpers (`response.To(req)` in Go, `response.to(req)` in Python) handle this automatically.

### v2.2 capability advertisement

The `RequestMeta.capabilities` field, new in v2.2, tells functions which features the runtime supports:

```protobuf
enum Capability {
  CAPABILITY_UNSPECIFIED = 0;
  CAPABILITY_CAPABILITIES = 1;
  CAPABILITY_REQUIRED_RESOURCES = 2;
  CAPABILITY_CREDENTIALS = 3;
  CAPABILITY_CONDITIONS = 4;
  CAPABILITY_REQUIRED_SCHEMAS = 5;    // new in v2.2
}
```

Functions can check for `CAPABILITY_REQUIRED_SCHEMAS` before requesting OpenAPI schemas, enabling graceful degradation on older runtimes.

### Function chaining data flow

```
  Crossplane observes XR + composed → frozen "observed"
       │
  Step 0:  Request{observed, desired: ∅, input: step[0].input, context: {}}
       │   → Response{desired: D₀, context: C₀}
       │
  Step 1:  Request{observed, desired: D₀, input: step[1].input, context: C₀}
       │   → Response{desired: D₁, context: C₁}
       │
  Step N:  Request{observed, desired: Dₙ₋₁, ...}
       │   → Response{desired: Dₙ}
       │
  Crossplane applies Dₙ (create/update/delete)
```

Pipeline rules enforced by the runtime: functions **must** echo `meta.tag` unchanged, **must not** set `metadata.name` on desired composed resources (Crossplane generates names), **should** set `crossplane.io/external-name` to influence external names, and **should** specify `ResponseMeta.ttl` to enable result caching. A `SEVERITY_FATAL` result terminates the entire pipeline. `Resources` mode was removed in v2 — **only `Pipeline` mode** is supported.

---

## 2. Required resources and required schemas

### Required resources (v2 rename of extra resources)

Two delivery mechanisms exist. **Bootstrap requirements** are declared in the Composition YAML and pre-populated before the first `RunFunction` call, avoiding an extra gRPC round-trip:

```yaml
pipeline:
- step: process
  functionRef:
    name: my-function
  requirements:
    requiredResources:
    - requirementName: app-config
      apiVersion: v1
      kind: ConfigMap
      name: app-configuration
      namespace: default
    requiredSchemas:
    - requirementName: bucket-schema
      apiVersion: s3.aws.m.upbound.io/v1beta1
      kind: Bucket
```

**Dynamic requirements** are returned in `RunFunctionResponse.requirements.resources`:

```protobuf
message Requirements {
  map<string, ResourceSelector> extra_resources = 1 [deprecated = true];
  map<string, ResourceSelector> resources = 2;
  map<string, SchemaSelector> schemas = 3;
}

message ResourceSelector {
  string api_version = 1;
  string kind = 2;
  oneof match { string match_name = 3; MatchLabels match_labels = 4; }
  optional string namespace = 5;
}
```

Crossplane re-calls the function with requested resources in `required_resources`. The loop runs up to **5 iterations** and stabilizes when requirements stop changing. Non-existent resources produce an empty `Resources` message (empty `items` list).

### Required schemas (new in v2.2)

Functions request OpenAPI v3 schemas via `requirements.schemas`. On re-call, `required_schemas["key"]` contains:

```protobuf
message SchemaSelector { string api_version = 1; string kind = 2; }
message Schema { optional google.protobuf.Struct openapi_v3 = 1; }
```

The `openapi_v3` field carries the CRD's `spec.versions[].schema.openAPIV3Schema` as unstructured JSON. This enables functions to perform schema-aware validation, default injection, or documentation generation without bundling CRD schemas at build time.

---

## 3. Available composition functions and SDKs

| Function | Package | Input Kind | Key capabilities |
|----------|---------|-----------|-----------------|
| **function-patch-and-transform** | `xpkg.crossplane.io/crossplane-contrib/function-patch-and-transform:v0.8.2` | `Resources` (`pt.fn.crossplane.io/v1beta1`) | YAML-native patching, transforms, readiness checks, environment field paths |
| **function-go-templating** | `xpkg.crossplane.io/crossplane-contrib/function-go-templating` | `GoTemplate` (`gotemplating.fn.crossplane.io/v1beta1`) | Helm-like Go templates with Sprig, inline or ConfigMap source, extra-resources context key |
| **function-kcl** | `xpkg.crossplane.io/crossplane-contrib/function-kcl:v0.11.2` | `KCLInput` (`krm.kcl.dev/v1alpha1`) | KCL DSL (CNCF hosted), inline/OCI/Git source, `option("params").oxr` accessors, sandboxed |
| **function-python** | `xpkg.crossplane.io/crossplane-contrib/function-python:v0.1.0` | `Script` (`python.fn.crossplane.io/v1beta1`) | Inline Python in YAML, `compose(req, rsp)` / `operate(req, rsp)`, full SDK + stdlib access |
| **function-pythonic** | `xpkg.crossplane.io/crossplane-contrib/function-pythonic:v0.3.0` | Higher-level Python | Terse syntax hiding proto-level APIs |

Custom functions are built with **function-sdk-go** (`github.com/crossplane/function-sdk-go`) or **function-sdk-python** (`crossplane-function-sdk-python` on PyPI, currently **v0.11.0**). Both implement the same `FunctionRunnerService` gRPC interface.

---

## 4. The Python composition ecosystem in depth

### function-python: inline scripts in Composition YAML

The `Script` input type embeds Python directly. The function runtime calls `compose(req, rsp)` for compositions or `operate(req, rsp)` for operations. The response is pre-seeded with previous pipeline state via `response.to(req)`.

```yaml
apiVersion: apiextensions.crossplane.io/v1
kind: Composition
metadata:
  name: app-python
spec:
  compositeTypeRef:
    apiVersion: example.crossplane.io/v1
    kind: App
  mode: Pipeline
  pipeline:
  - step: compose-resources
    functionRef:
      name: function-python
    input:
      apiVersion: python.fn.crossplane.io/v1beta1
      kind: Script
      script: |
        from crossplane.function.proto.v1 import run_function_pb2 as fnv1

        def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
            xr = req.observed.composite.resource
            name = xr["metadata"]["name"]
            image = xr["spec"]["image"]

            rsp.desired.resources["deployment"].resource.update({
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "replicas": 2,
                    "selector": {"matchLabels": {"app": name}},
                    "template": {
                        "metadata": {"labels": {"app": name}},
                        "spec": {"containers": [{"name": "app", "image": image,
                                                  "ports": [{"containerPort": 80}]}]}
                    }
                }
            })
            rsp.desired.resources["deployment"].ready = True
```

The script has full access to the `crossplane.function` module (resource helpers, response helpers, logging) and the entire Python standard library. **function-python is the only function supporting Operations at launch.** Use it for quick compositions and operational scripts; switch to a standalone function when inline YAML becomes unwieldy.

### function-sdk-python: building standalone functions

**Version**: 0.11.0 on PyPI (`crossplane-function-sdk-python`). Requires Python ≥3.11, <3.14. Status: beta (no stable API guarantee until v1.0.0).

The core pattern implements `FunctionRunnerService` as an async gRPC servicer:

```python
import grpc
from crossplane.function import logging, response, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1

class FunctionRunner(grpcv1.FunctionRunnerService):
    def __init__(self):
        self.log = logging.get_logger()

    async def RunFunction(
        self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext
    ) -> fnv1.RunFunctionResponse:
        log = self.log.bind(tag=req.meta.tag)
        rsp = response.to(req)

        region = req.observed.composite.resource["spec"]["region"]
        names = list(req.observed.composite.resource["spec"]["names"])

        for name in names:
            rsp.desired.resources[f"bucket-{name}"].resource.update({
                "apiVersion": "s3.aws.m.upbound.io/v1beta1",
                "kind": "Bucket",
                "metadata": {"annotations": {"crossplane.io/external-name": name}},
                "spec": {"forProvider": {"region": region}},
            })

        response.normal(rsp, f"Composed {len(names)} buckets in {region}")
        return rsp
```

**The protobuf map field quirk** is critical to understand. You **cannot** assign directly to protobuf map keys — `rsp.desired.resources["x"] = fnv1.Resource(...)` does not work. Instead, accessing a nonexistent key auto-creates an empty message (like `defaultdict`), and you mutate it in place:

```python
# ✅ Correct: access-and-mutate
rsp.desired.resources["bucket"].resource.update({...})
rsp.desired.resources["bucket"].ready = fnv1.READY_TRUE

# ❌ Wrong: direct assignment silently fails
rsp.desired.resources["bucket"] = fnv1.Resource(resource=some_struct)
```

**Helper modules**:

- **`crossplane.function.response`**: `to(req)` initializes response copying desired/context/tag; `normal(rsp, msg)`, `warning(rsp, msg)`, `fatal(rsp, msg)` append results
- **`crossplane.function.resource`**: `update(r, source)` updates a `Resource` from dict/Struct/Pydantic model; `dict_to_struct(d)` / `struct_to_dict(s)` convert between Python dicts and protobuf Structs; `get_condition(resource, typ)` extracts status conditions
- **`crossplane.function.logging`**: `configure(level)` sets up structlog (Level.DEBUG for console, Level.INFO for JSON lines); `get_logger()` returns a `BoundLogger`

### function-template-python: project scaffold

```
function-xbuckets/
├── Dockerfile
├── example/              # xr.yaml, composition.yaml, functions.yaml
├── function/
│   ├── __version__.py    # Hatch reads version from here
│   ├── fn.py             # FunctionRunner class
│   └── main.py           # gRPC server entry point (no edits needed)
├── package/
│   ├── crossplane.yaml   # meta.pkg.crossplane.io/v1 Function
│   └── input/            # Optional OpenAPI schema for function input
├── pyproject.toml
├── renovate.json
└── tests/
    └── test_fn.py        # unittest.IsolatedAsyncioTestCase
```

The `pyproject.toml` pins `crossplane-function-sdk-python==0.11.0`, requires Python ≥3.11,<3.14, and uses **Hatch** as the build system with **Ruff** for linting. Key commands: `hatch run development` starts the gRPC server on `localhost:9443` with `--insecure --debug`; `hatch fmt` lints; `hatch test` runs unit tests.

Unit tests follow a dataclass-based pattern using `IsolatedAsyncioTestCase`:

```python
class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        logging.configure(level=logging.Level.DISABLED)

    async def test_compose_buckets(self):
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct({
                        "apiVersion": "example.crossplane.io/v1",
                        "kind": "XBuckets",
                        "metadata": {"name": "test"},
                        "spec": {"region": "us-east-2", "names": ["a", "b"]},
                    })
                )
            )
        )
        runner = fn.FunctionRunner()
        got = await runner.RunFunction(req, None)
        self.assertIn("bucket-a", dict(got.desired.resources))
        self.assertIn("bucket-b", dict(got.desired.resources))
```

### CLI developer experience

**`crossplane xpkg init`** scaffolds from the template:

```bash
crossplane xpkg init function-xbuckets function-template-python -d function-xbuckets
```

**`crossplane render`** runs the composition pipeline locally, requiring Docker by default:

```bash
crossplane render example/xr.yaml example/composition.yaml example/functions.yaml \
  -r              # include function results
  -x              # include full XR
  -e observed.yaml  # mock observed composed resources
```

The **Development runtime annotation** bypasses Docker and connects to a locally running function:

```yaml
apiVersion: pkg.crossplane.io/v1
kind: Function
metadata:
  name: function-xbuckets
  annotations:
    render.crossplane.io/runtime: Development
    # render.crossplane.io/runtime-development-target: localhost:9443
spec:
  package: xpkg.crossplane.io/example/function-xbuckets:v0.1.0
```

Run `hatch run development` in one terminal, `crossplane render` in another. The function serves insecure gRPC on `localhost:9443`.

### Upbound `up` CLI tooling

The `up` CLI (v0.44.x) provides a higher-level project workflow:

- **`up project init my-project --language=python`** — scaffolds a full project with `upbound.yaml`, `apis/`, `functions/`, `examples/`, `tests/` directories
- **`up xrd generate examples/my-app.yaml`** — auto-generates XRD OpenAPI schema from an example resource
- **`up function generate --language=python compose-resources apis/composition.yaml`** — generates embedded function boilerplate
- **`up composition render`** — local pipeline simulation (Docker-based)
- **`up project run`** — deploys to a live Upbound development control plane (ephemeral, 24h TTL)
- **VSCode integration** — `up dependency add` generates JSON schemas in `.up/` for real-time validation of Python composition code

Embedded functions live in `functions/` and are packaged automatically — no separate Docker push required.

---

## 5. How v2 changed the XR model

### Namespaced XRs and the scope field

v2 XRDs (`apiextensions.crossplane.io/v2`) default to **`scope: Namespaced`**. The `spec.scope` field accepts three values:

| Scope | Behavior | Claims |
|-------|----------|--------|
| **`Namespaced`** (v2 default) | XR is namespace-scoped, composes resources in its namespace | Not supported |
| **`Cluster`** | XR is cluster-scoped, composes any resource in any namespace | Not supported |
| **`LegacyCluster`** (v1 default) | v1 backward-compat: cluster-scoped, `spec.compositionRef` at top level | Supported |

```yaml
apiVersion: apiextensions.crossplane.io/v2
kind: CompositeResourceDefinition
metadata:
  name: apps.example.crossplane.io
spec:
  scope: Namespaced
  group: example.crossplane.io
  names:
    kind: App
    plural: apps
  versions:
  - name: v1
    served: true
    referenceable: true
    schema:
      openAPIV3Schema:
        type: object
        properties:
          spec:
            type: object
            properties:
              image: { type: string }
            required: [image]
```

**Claims are deprecated** in the v2 API — `spec.claimNames` is explicitly unsupported. With namespaced XRs, the Claim/XR duality is unnecessary; users create XRs directly in their namespace. Connection secret keys (`spec.connectionSecretKeys`) were also removed; connection secrets must now be manually composed as Kubernetes Secrets via functions.

### The `spec.crossplane` sub-object

v2 XRs cleanly separate user fields from Crossplane machinery:

```yaml
apiVersion: example.crossplane.io/v1
kind: App
metadata:
  namespace: default
  name: my-app
spec:
  image: nginx                              # user field
  crossplane:                               # Crossplane internals
    compositionRef:
      name: app-python
    compositionRevisionRef:
      name: app-python-41b6efe
    compositionUpdatePolicy: Automatic      # or Manual
    compositionRevisionSelector:
      matchLabels:
        channel: staging
    resourceRefs:
    - apiVersion: apps/v1
      kind: Deployment
      name: my-app-9bj8j
    - apiVersion: v1
      kind: Service
      name: my-app-bflc4
```

`LegacyCluster` XRs retain the v1 flat layout (`spec.compositionRef`, `spec.resourceRefs` at top level).

### Composition revisions

Each Composition edit creates an immutable `CompositionRevision`. With `compositionUpdatePolicy: Automatic` (default), the XR always uses the latest revision. With `Manual`, the XR stays pinned until `compositionRevisionRef.name` is explicitly updated. Label-based revision selection (`compositionRevisionSelector.matchLabels`) combined with `Automatic` policy enables channel-based rollouts (e.g., `channel: staging` → `channel: production`).

---

## 6. Operations: one-shot and scheduled function pipelines

Operations are an **alpha feature** (`ops.crossplane.io/v1alpha1`) enabled via `--enable-operations`. They reuse the same function pipeline as compositions but for operational tasks rather than continuous reconciliation.

### Three Operation kinds

**Operation** runs once to completion (like a Job):

```yaml
apiVersion: ops.crossplane.io/v1alpha1
kind: Operation
metadata:
  name: backup-database
spec:
  retryLimit: 10
  mode: Pipeline
  pipeline:
  - step: create-backup
    functionRef:
      name: function-python
    credentials:
    - name: backup-creds
      source: Secret
      secretRef:
        namespace: crossplane-system
        name: backup-credentials
    requirements:
      requiredResources:
      - requirementName: db
        apiVersion: rds.aws.m.upbound.io/v1beta1
        kind: Instance
        name: production-db
    input:
      apiVersion: python.fn.crossplane.io/v1beta1
      kind: Script
      script: |
        from crossplane.function.proto.v1 import run_function_pb2 as fnv1
        import datetime

        def operate(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
            rsp.output["timestamp"] = datetime.datetime.now().isoformat()
            rsp.output["status"] = "backup-initiated"
```

**CronOperation** runs on a cron schedule:

```yaml
apiVersion: ops.crossplane.io/v1alpha1
kind: CronOperation
metadata:
  name: nightly-backup
spec:
  schedule: "0 3 * * *"
  concurrencyPolicy: Forbid       # Allow | Forbid | Replace
  successfulHistoryLimit: 5
  failedHistoryLimit: 3
  operationTemplate:
    spec:
      mode: Pipeline
      pipeline:
      - step: backup
        functionRef:
          name: function-python
        input: { ... }
```

**WatchOperation** (v2.1+) runs when watched resources change. The changed resource is injected via the reserved requirement name `ops.crossplane.io/watched-resource`.

Functions declare operation support in `crossplane.yaml` via `spec.capabilities: [composition, operation]`. Operations force-apply resources **without owner references** (unlike compositions). Test locally with `crossplane alpha render op`.

---

## 7. Composing any Kubernetes resource

v2 compositions can compose **any** Kubernetes resource — native types (Deployments, Services, ConfigMaps), Crossplane MRs, or third-party CRDs (CloudNativePG, Cert-Manager, Cluster API). This eliminates the `provider-kubernetes` Object wrapper pattern.

The RBAC manager (enabled by default) automatically grants Crossplane access to MRs and XRs. For **any other resource type**, create a ClusterRole with the aggregation label:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: crossplane-compose-apps-core
  labels:
    rbac.crossplane.io/aggregate-to-crossplane: "true"
rules:
- apiGroups: ["apps"]
  resources: ["deployments"]
  verbs: ["*"]
- apiGroups: [""]
  resources: ["services", "configmaps"]
  verbs: ["*"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: crossplane-compose-cnpg
  labels:
    rbac.crossplane.io/aggregate-to-crossplane: "true"
rules:
- apiGroups: ["postgresql.cnpg.io"]
  resources: ["clusters"]
  verbs: ["*"]
```

The label **`rbac.crossplane.io/aggregate-to-crossplane: "true"`** causes Kubernetes ClusterRole aggregation to merge these rules into Crossplane's primary ClusterRole automatically.

---

## 8. The v2.2 pipeline inspector

The pipeline inspector is a new **alpha feature** (disabled by default) that forwards every `RunFunctionRequest`/`RunFunctionResponse` pair to a user-configured gRPC endpoint. When enabled, Crossplane streams the step name alongside each request/response, differentiating composition vs operation context via a `oneof`. Target use cases include real-time debugging dashboards, audit logging, and pipeline visualization.

---

## 9. The packaging layer

### Three package types

All use `meta.pkg.crossplane.io/v1` as the in-package metadata API and `pkg.crossplane.io/v1` as the cluster install API:

| Type | Meta Kind | Install Kind | Runtime |
|------|-----------|-------------|---------|
| **Provider** | `Provider` | `Provider` | Controller Deployment |
| **Function** | `Function` | `Function` | gRPC server Deployment |
| **Configuration** | `Configuration` | `Configuration` | None (YAML only) |

### The xpkg OCI specification

Packages are OCI images. The **`io.crossplane.xpkg`** annotation on layer descriptors identifies the package content layer:

- `io.crossplane.xpkg: base` — the primary layer containing `crossplane.yaml` and all YAML manifests. At most one layer may carry this annotation.
- Other values (e.g., `upbound`) are extension points for third-party consumers.

If no layer has the `base` annotation, Crossplane falls back to the full filesystem. For multi-arch packages, at least one manifest must be present; if multiple exist, Crossplane selects `linux/amd64` by default.

The `--embed-runtime-image` flag embeds a container runtime image (the controller or gRPC server binary) as a separate layer:

```bash
docker build . --platform=linux/amd64 --tag runtime-amd64
docker build . --platform=linux/arm64 --tag runtime-arm64

crossplane xpkg build --package-root=package \
  --embed-runtime-image=runtime-amd64 --package-file=function-amd64.xpkg
crossplane xpkg build --package-root=package \
  --embed-runtime-image=runtime-arm64 --package-file=function-arm64.xpkg

crossplane xpkg push --package-files=function-amd64.xpkg,function-arm64.xpkg \
  xpkg.crossplane.io/example/function-xbuckets:v0.1.0
```

### Default registry and dependency management

Since v1.20, the default registry is **`xpkg.crossplane.io`**. In v2.0+, the `--registry` flag was removed — all packages require fully qualified names.

Dependencies use `spec.dependsOn` with semver constraints (Masterminds/semver):

```yaml
apiVersion: meta.pkg.crossplane.io/v1
kind: Configuration
metadata:
  name: platform-networking
spec:
  crossplane:
    version: ">=v2.0.0"
  dependsOn:
  - provider: xpkg.crossplane.io/upbound/provider-aws-s3
    version: ">=v1.3.1"
  - function: xpkg.crossplane.io/crossplane-contrib/function-python
    version: ">=v0.1.0"
```

### ManagedResourceDefinitions and ManagedResourceActivationPolicies

**MRDs** (`apiextensions.crossplane.io/v1alpha1`) are lightweight abstractions over CRDs that enable **selective activation** of provider resources. Large providers install 100+ CRDs, each consuming ~3 MiB of API server memory. MRDs start `Inactive` (for providers declaring `capabilities: [safe-start]`) and transition one-way to `Active`, at which point Crossplane creates the underlying CRD.

```yaml
apiVersion: apiextensions.crossplane.io/v1alpha1
kind: ManagedResourceDefinition
metadata:
  name: buckets.s3.aws.m.crossplane.io
spec:
  group: s3.aws.m.crossplane.io
  names:
    kind: Bucket
    plural: buckets
  scope: Namespaced
  versions:
  - name: v1beta1
    served: true
    storage: true
    schema:
      openAPIV3Schema: { ... }
  connectionDetails:
  - name: bucket-name
    description: The name of the S3 bucket
  state: Inactive
```

**MRAPs** activate MRDs by name pattern (supporting `*` wildcards):

```yaml
apiVersion: apiextensions.crossplane.io/v1alpha1
kind: ManagedResourceActivationPolicy
metadata:
  name: aws-core
spec:
  activate:
  - buckets.s3.aws.m.crossplane.io
  - "*.ec2.aws.m.crossplane.io"
```

Multiple MRAPs are additive. By default, Crossplane v2 creates a default MRAP activating all MRDs (`*`); disable with `--set provider.defaultActivations={}` at install.

### DeploymentRuntimeConfig

Replaces the deprecated `ControllerConfig` (removed in v2). API: `pkg.crossplane.io/v1beta1` `DeploymentRuntimeConfig`.

```yaml
apiVersion: pkg.crossplane.io/v1beta1
kind: DeploymentRuntimeConfig
metadata:
  name: custom-runtime
spec:
  deploymentTemplate:
    spec:
      selector: {}
      template:
        spec:
          containers:
          - name: package-runtime          # Must use this name
            resources:
              limits:
                memory: 512Mi
            args:
            - --enable-external-secret-stores
  serviceAccountTemplate:
    metadata:
      annotations:
        eks.amazonaws.com/role-arn: arn:aws:iam::123456789:role/my-role
```

Referenced via `spec.runtimeConfigRef.name` on Provider or Function install objects. The container name **must** be `package-runtime` — any other name creates a sidecar. Migration tool: `crossplane beta convert deployment-runtime controller-config.yaml`.

---

## 10. End-to-end design patterns with Python

### Pattern A: inline function-python (rapid iteration)

1. Define a v2 XRD with `scope: Namespaced`
2. Write a Composition referencing `function-python` with an inline `Script`
3. Install `function-python` as a Function package
4. Test: `crossplane render xr.yaml composition.yaml functions.yaml -r`
5. Apply XRD, Composition, Function, and RBAC ClusterRoles to the cluster
6. Create an XR instance

### Pattern B: standalone function with function-sdk-python (production)

1. `crossplane xpkg init function-myapp function-template-python -d function-myapp`
2. Implement `function/fn.py` with `FunctionRunner.RunFunction`
3. Write unit tests in `tests/test_fn.py`
4. `hatch run development` + `crossplane render` with Development runtime for local iteration
5. `docker build . --platform=linux/amd64,linux/arm64` for multi-arch runtime images
6. `crossplane xpkg build --embed-runtime-image=... && crossplane xpkg push`
7. Install on cluster, reference in Composition

### Multi-step pipeline: Python + patch-and-transform

A powerful pattern chains a Python function that creates base resources with P&T that applies patches. The key: provide a `resources` entry in P&T with a matching `name` but **no `base`** — P&T patches the resource from the previous step:

```yaml
pipeline:
- step: create-base-resources
  functionRef:
    name: function-python
  input:
    apiVersion: python.fn.crossplane.io/v1beta1
    kind: Script
    script: |
      def compose(req, rsp):
          rsp.desired.resources["bucket"].resource.update({
              "apiVersion": "s3.aws.m.upbound.io/v1beta1",
              "kind": "Bucket",
              "spec": {"forProvider": {"region": "us-east-2"}}
          })

- step: apply-patches
  functionRef:
    name: function-patch-and-transform
  input:
    apiVersion: pt.fn.crossplane.io/v1beta1
    kind: Resources
    resources:
    - name: bucket                          # matches key from step 1, no "base"
      patches:
      - type: FromCompositeFieldPath
        fromFieldPath: spec.tags
        toFieldPath: spec.forProvider.tags

- step: auto-ready
  functionRef:
    name: function-auto-ready
```

### Reading cluster state with required resources

Bootstrap requirements pull ConfigMaps or other resources before the function runs:

```yaml
pipeline:
- step: configure-from-cluster
  functionRef:
    name: function-python
  requirements:
    requiredResources:
    - requirementName: cluster-config
      apiVersion: v1
      kind: ConfigMap
      name: platform-defaults
      namespace: crossplane-system
  input:
    apiVersion: python.fn.crossplane.io/v1beta1
    kind: Script
    script: |
      def compose(req, rsp):
          config = req.required_resources["cluster-config"].items[0]
          region = config.resource["data"]["defaultRegion"]
          # Use region to configure composed resources...
```

### Testing pyramid

- **Unit tests**: `hatch test` runs `unittest.IsolatedAsyncioTestCase` against `FunctionRunner.RunFunction` with mock `RunFunctionRequest` inputs — fast, no cluster needed
- **Local render**: `crossplane render` (or `up composition render`) simulates the full pipeline locally with Docker — validates function chaining, catches integration issues
- **Composition tests** (Up CLI): `up test run tests/*` using the `CompositionTest` API models a single controller loop with assertions on composed resources
- **E2E tests**: `up test run --e2e` or standalone `uptest e2e` provisions a real control plane, applies resources, waits for Ready conditions, and tears down
