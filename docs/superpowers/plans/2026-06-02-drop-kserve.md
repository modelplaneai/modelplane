# Drop KServe — dispatch to native + llm-d Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove Modelplane's KServe dependency by turning `compose-model-replica` into a backend dispatcher (native Kubernetes for single-pod, llm-d for multi-pod) and replacing the `KServeBackend` cluster-install XR with a backend-neutral `ServingStack`.

**Architecture:** One `compose-model-replica` reconcile loop owns shared concerns (cluster resolution, engine extraction, conditions, `ModelEndpoint.url`) and delegates the emitted workload to `backends/{native,llmd,dynamo}.py` behind a shared interface that returns provider-kubernetes `Object`s and/or provider-helm `Release`s. `ServingStack` installs the union serving substrate (LeaderWorkerSet, Gateway API + inference extension, cert-manager, Prometheus, llm-d).

**Tech Stack:** Crossplane v2 composition functions (Python, function-sdk-python), Pydantic models generated from XRDs (`schemas/python/`), provider-kubernetes (`Object`), provider-helm (`Release`), Nix (`nix flake check`, `nix run .#generate`, `nix run .#fix`).

**Spec:** `docs/superpowers/specs/2026-06-02-drop-kserve-design.md`

---

## Conventions for every task

- **Test command (canonical):** `nix flake check` (lints + runs all function unit tests).
- **Fast iteration on one function:** `nix develop -c python -m unittest discover -s functions/<name>/tests -v` (run from repo root inside the dev shell).
- **Auto-fix lint/format before committing:** `nix run .#fix`.
- **Schema regeneration (after any XRD change):** `nix run .#generate`, then commit the regenerated files under `schemas/python/`.
- **Commits are signed off:** every commit uses `git commit -s`.
- Tests are `unittest.IsolatedAsyncioTestCase` golden-response comparisons: build a typed `RunFunctionRequest`, call `FunctionRunner.RunFunction()`, compare the `RunFunctionResponse` via `json_format.MessageToDict`. Match the existing style in each `tests/test_fn.py`.

## File structure

**`functions/compose-model-replica/function/`** — becomes the dispatcher.
- `fn.py` (modify) — shared concerns + `select_backend()` + delegation. KServe emission, the `hf://` model-URI hack, and `--model=` stripping are deleted.
- `backends/__init__.py` (create) — package marker.
- `backends/base.py` (create) — `Backend` protocol, `ComposedResource` alias, `select_backend()`, `needs_cross_pod_coordination()`, shared `_k8s_object()` / `_helm_release()` builders, `engine_container()`.
- `backends/native.py` (create) — single-pod: `Deployment` + `Service` + `HTTPRoute`.
- `backends/llmd.py` (create) — multi-pod: `llm-d-modelservice` Helm `Release` + GAIE `InferencePool` + `InferenceObjective` + EPP.
- `backends/dynamo.py` (create) — v0.1 stub; never selected; raises if reached.

**`apis/servingstacks/`** (rename from `apis/kservebackends/`)
- `definition.yaml` (modify) — `kind: ServingStack`, drop `kserve`/`keda` version pins, add `llmD` pins.
- `composition.yaml` (modify) — composite type + function ref renamed.

**`functions/compose-serving-stack/`** (rename from `functions/compose-kserve-backend/`)
- `function/fn.py` (modify) — drop KServe + KEDA + storage patch; add llm-d; bump LWS; rename gateway naming.
- `pyproject.toml`, `function/main.py`, `tests/test_fn.py` (modify) — package rename + content tests.

**`functions/compose-inference-cluster/function/fn.py`** (modify) — references to the renamed XR/model/keys.

**`crossplane-project.yaml`** (modify) — function tarball entry rename.

**Generated:** `schemas/python/models/ai/modelplane/infrastructure/servingstack/` (replaces `.../kservebackend/`), regenerated via `nix run .#generate`.

**Docs/examples:** `docs/concepts.md`, `docs/getting-started.md`, `examples/qwen-demo/00-prerequisites.yaml`, `examples/qwen-demo/03-cluster.yaml`, `examples/deployment/model-deployment-multinode.yaml`.

**Spike artifact:** `docs/superpowers/notes/llm-d-v0.7-surface.md` (created in Task 0).

---

## Phase 0 — Verify the llm-d surface

### Task 0: llm-d v0.7 / GAIE verification spike

The exact `llm-d-modelservice` chart coordinates, its Helm values schema, and the GAIE routing resource group/versions are external API we must pin before writing `backends/llmd.py` (Task 4) and the `ServingStack` llm-d install (Task 8). This task produces a concrete reference artifact; it writes no code.

**Files:**
- Create: `docs/superpowers/notes/llm-d-v0.7-surface.md`

- [ ] **Step 1: Capture the chart coordinates**

From <https://github.com/llm-d-incubation/llm-d-modelservice> and its Helm repo
`https://llm-d-incubation.github.io/llm-d-modelservice/`, record into the notes file:
- chart name, Helm repo URL, and the exact chart version compatible with llm-d v0.7.0;
- the `llm-d-infra` prerequisite chart name/repo/version (installed once per cluster).

- [ ] **Step 2: Capture the modelservice values keys actually used**

Record the exact Helm values keys the backend will set, with example values:
`modelArtifacts.uri` (forms: `hf://org/model`, `pvc://<claim>/<path>`),
parallelism keys (tensor / pipeline), `multinode` (bool) and its effect (LWS vs Deployment),
prefill/decode replica keys, the engine image/args/env keys, and how pod labels are
configured so GAIE selectors match. Quote the upstream `values.yaml` field paths verbatim.

- [ ] **Step 3: Capture the GAIE routing resources**

From <https://gateway-api-inference-extension.sigs.k8s.io/api-types/inferencepool/> record:
- `InferencePool` apiVersion (`inference.networking.k8s.io/v1`) and the v1 `spec` shape
  (`selector`, `targetPortNumber`/`targetPorts`, `extensionRef`);
- `InferenceObjective` apiVersion and `spec` shape (model-name → pool mapping, criticality);
- the EPP (endpoint picker) deployment/service the pool's `extensionRef` points at, and
  whether llm-d's v0.7 well-lit path expects a shared or per-pool EPP.

- [ ] **Step 4: Confirm the in-cluster gateway**

Confirm whether the v0.7 recipe uses Envoy Gateway (keep) or Istio/kgateway, and pin a
GAIE-conformant version. Record the decision and version in the notes file.

- [ ] **Step 5: Commit the notes**

```bash
git add docs/superpowers/notes/llm-d-v0.7-surface.md
git commit -s -m "docs: pin llm-d v0.7 / GAIE surface for KServe removal"
```

> Tasks 4 and 8 reference this file by name. Where a step below says "the chart
> version from the spike" or "the GAIE apiVersion from the spike," substitute the
> value recorded here.

---

## Phase 1 — Dispatcher + backends in compose-model-replica

### Task 1: Backend interface and dispatch predicate

**Files:**
- Create: `functions/compose-model-replica/function/backends/__init__.py`
- Create: `functions/compose-model-replica/function/backends/base.py`
- Create: `functions/compose-model-replica/tests/test_backends.py`

- [ ] **Step 1: Write the failing predicate/dispatch test**

Create `functions/compose-model-replica/tests/test_backends.py`:

```python
"""Tests for backend selection and the dispatch predicate."""

import unittest

from function.backends import base
from models.ai.modelplane.modelreplica import v1alpha1


def _replica(*, tensor=1, pipeline=1):
    return v1alpha1.ModelReplica(
        spec=v1alpha1.SpecModel(
            clusterName="c",
            workers=v1alpha1.Workers(
                topology=v1alpha1.Topology(tensor=tensor, pipeline=pipeline),
                template=v1alpha1.Template(
                    spec=v1alpha1.Spec(
                        containers=[v1alpha1.Container(name="engine", image="img")],
                    ),
                ),
            ),
        ),
    )


class TestDispatch(unittest.TestCase):
    def test_single_pod_is_native(self):
        self.assertEqual(base.select_backend(_replica(tensor=8, pipeline=1)), base.NATIVE)

    def test_multi_node_is_llmd(self):
        self.assertEqual(base.select_backend(_replica(tensor=8, pipeline=2)), base.LLMD)

    def test_needs_coordination_only_when_multi_node(self):
        self.assertFalse(base.needs_cross_pod_coordination(_replica(tensor=4, pipeline=1)))
        self.assertTrue(base.needs_cross_pod_coordination(_replica(tensor=4, pipeline=3)))
```

- [ ] **Step 2: Run it to verify it fails**

Run: `nix develop -c python -m unittest discover -s functions/compose-model-replica/tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'function.backends'`.

- [ ] **Step 3: Create the package marker**

Create `functions/compose-model-replica/function/backends/__init__.py` (empty file).

- [ ] **Step 4: Implement the interface and predicate**

Create `functions/compose-model-replica/function/backends/base.py`:

```python
"""Backend dispatch for compose-model-replica.

A backend turns a ModelReplica + its InferenceCluster into the cluster-level
serving resources. Backends return provider-kubernetes Objects and/or
provider-helm Releases; the dispatcher (fn.py) applies them to the response.
"""

from typing import Protocol

from crossplane.function import resource
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

# A composed resource is either a provider-kubernetes Object or a
# provider-helm Release. fn.py writes these into the response by key.
ComposedResource = k8sobjv1alpha1.Object | helmv1beta1.Release

# Backend identifiers.
NATIVE = "native"
LLMD = "llmd"
DYNAMO = "dynamo"


def engine_container(replica: v1alpha1.ModelReplica):
    """Return the container named 'engine'. The XRD's CEL validation
    guarantees exactly one exists, so this always succeeds."""
    return next(c for c in replica.spec.workers.template.spec.containers if c.name == "engine")


def nodes_per_worker(replica: v1alpha1.ModelReplica) -> int:
    """Nodes spanned by one worker.

    v0.1 topology implements only tensor + pipeline, so this is `pipeline`.
    When data/dataLocal land, this becomes pipeline * (data / dataLocal).
    """
    return int(replica.spec.workers.topology.pipeline or 1)


def needs_cross_pod_coordination(replica: v1alpha1.ModelReplica) -> bool:
    """True when the replica is more than one self-contained pod.

    v0.1: true iff nodes_per_worker > 1. Extension points (no-ops until the
    fields exist): a `prefill` block (disaggregated P/D) or multi-node data
    parallelism (data > dataLocal) also make this true.
    """
    return nodes_per_worker(replica) > 1


def select_backend(replica: v1alpha1.ModelReplica) -> str:
    """Pick the lightest serving path. No user-facing backend field.

    Dynamo is dormant in v0.1: no Dynamo-only capability is wired, so a
    multi-pod replica always selects llm-d.
    """
    if not needs_cross_pod_coordination(replica):
        return NATIVE
    return LLMD


class Backend(Protocol):
    """Builds the cluster-level serving resources for one ModelReplica."""

    def build(
        self,
        replica: v1alpha1.ModelReplica,
        cluster: icv1alpha1.InferenceCluster,
        deployment_name: str,
    ) -> dict[str, ComposedResource]:
        """Return a mapping of response resource-key -> composed resource."""
        ...
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `nix develop -c python -m unittest discover -s functions/compose-model-replica/tests -v`
Expected: PASS (3 tests in `TestDispatch`).

- [ ] **Step 6: Commit**

```bash
nix run .#fix
git add functions/compose-model-replica/function/backends/__init__.py \
        functions/compose-model-replica/function/backends/base.py \
        functions/compose-model-replica/tests/test_backends.py
git commit -s -m "feat(replica): add backend dispatch interface and predicate"
```

### Task 2: Native backend (single-pod)

**Files:**
- Create: `functions/compose-model-replica/function/backends/native.py`
- Modify: `functions/compose-model-replica/tests/test_backends.py`

- [ ] **Step 1: Write the failing native-backend test**

Append to `functions/compose-model-replica/tests/test_backends.py`:

```python
from function.backends import native


class TestNativeBackend(unittest.TestCase):
    def setUp(self):
        self.replica = v1alpha1.ModelReplica(
            spec=v1alpha1.SpecModel(
                clusterName="cluster-a",
                workers=v1alpha1.Workers(
                    topology=v1alpha1.Topology(tensor=2, pipeline=1),
                    template=v1alpha1.Template(
                        spec=v1alpha1.Spec(
                            containers=[
                                v1alpha1.Container(
                                    name="engine",
                                    image="vllm/vllm-openai:latest",
                                    args=["--model=Qwen/Qwen3-0.6B"],
                                ),
                            ],
                        ),
                    ),
                ),
            ),
        )
        self.cluster = icv1alpha1.InferenceCluster(
            metadata=metav1.ObjectMeta(name="cluster-a"),
        )

    def test_emits_deployment_service_route(self):
        out = native.NativeBackend().build(self.replica, self.cluster, "my-deployment")
        kinds = sorted(
            o.spec.forProvider.manifest["kind"] for o in out.values()
        )
        self.assertEqual(kinds, ["Deployment", "HTTPRoute", "Service"])

    def test_engine_args_passed_through_unmodified(self):
        out = native.NativeBackend().build(self.replica, self.cluster, "my-deployment")
        dep = next(o for o in out.values() if o.spec.forProvider.manifest["kind"] == "Deployment")
        container = dep.spec.forProvider.manifest["spec"]["template"]["spec"]["containers"][0]
        # No hf:// rewrite, no --model stripping: the engine fetches directly.
        self.assertIn("--model=Qwen/Qwen3-0.6B", container["args"])
        self.assertEqual(container["resources"]["limits"]["nvidia.com/gpu"], "2")
```

Add these imports at the top of the test file if not already present:

```python
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
```

- [ ] **Step 2: Run it to verify it fails**

Run: `nix develop -c python -m unittest discover -s functions/compose-model-replica/tests -v`
Expected: FAIL — `ImportError: cannot import name 'native'`.

- [ ] **Step 3: Implement the native backend**

Create `functions/compose-model-replica/function/backends/native.py`:

```python
"""Native single-pod backend: plain Kubernetes Deployment + Service + HTTPRoute.

For a single self-contained pod no orchestrator is needed. Weights load
directly: the engine's --model arg is passed through unmodified, so vLLM/SGLang
fetches from its source at startup using credentials from engine.env.
"""

from crossplane.function import resource
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

from function.backends import base

# Namespace for serving workloads on remote clusters.
_REMOTE_NAMESPACE = "default"

# Port the engine serves the OpenAI-compatible API on.
_ENGINE_PORT = 8000


def _object(provider_config: str, manifest: dict) -> k8sobjv1alpha1.Object:
    return k8sobjv1alpha1.Object(
        spec=k8sobjv1alpha1.Spec(
            providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                kind="ClusterProviderConfig",
                name=provider_config,
            ),
            readiness=k8sobjv1alpha1.Readiness(policy="DeriveFromObject"),
            forProvider=k8sobjv1alpha1.ForProvider(manifest=manifest),
        ),
    )


class NativeBackend:
    def build(
        self,
        replica: v1alpha1.ModelReplica,
        cluster: icv1alpha1.InferenceCluster,
        deployment_name: str,
    ) -> dict[str, k8sobjv1alpha1.Object]:
        engine = base.engine_container(replica)
        pc = cluster.status.providerConfigRef.name
        name = resource.child_name(deployment_name)
        labels = {"modelplane.ai/serving": name}

        container = {
            "name": "engine",
            "image": engine.image,
            "args": list(engine.args or []),
            "ports": [{"containerPort": _ENGINE_PORT}],
            "resources": {"limits": {"nvidia.com/gpu": str(replica.spec.workers.topology.tensor)}},
            # vLLM tensor parallelism needs a large /dev/shm.
            "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
            "readinessProbe": {
                "httpGet": {"path": "/health", "port": _ENGINE_PORT},
                "initialDelaySeconds": 30,
                "periodSeconds": 10,
            },
        }
        if engine.env:
            container["env"] = [e.model_dump(exclude_none=True) for e in engine.env]

        pod_spec = {
            "containers": [container],
            "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
        }
        tmpl = replica.spec.workers.template
        if tmpl.spec.imagePullSecrets:
            pod_spec["imagePullSecrets"] = [s.model_dump(exclude_none=True) for s in tmpl.spec.imagePullSecrets]

        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": _REMOTE_NAMESPACE},
            "spec": {
                "replicas": int(replica.spec.workers.count or 1),
                "selector": {"matchLabels": labels},
                "template": {"metadata": {"labels": labels}, "spec": pod_spec},
            },
        }

        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "namespace": _REMOTE_NAMESPACE},
            "spec": {"selector": labels, "ports": [{"port": 80, "targetPort": _ENGINE_PORT}]},
        }

        http_route = {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "HTTPRoute",
            "metadata": {"name": name, "namespace": _REMOTE_NAMESPACE},
            "spec": {
                "parentRefs": [{"name": "inference-gateway", "namespace": "modelplane-system"}],
                "rules": [
                    {
                        "matches": [{"path": {"type": "PathPrefix", "value": f"/{replica.metadata.namespace}/{deployment_name}/"}}],
                        "backendRefs": [{"name": name, "port": 80}],
                    }
                ],
            },
        }

        return {
            "model-serving": _object(pc, deployment),
            "model-service": _object(pc, service),
            "model-route": _object(pc, http_route),
        }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `nix develop -c python -m unittest discover -s functions/compose-model-replica/tests -v`
Expected: PASS (`TestNativeBackend` green).

- [ ] **Step 5: Commit**

```bash
nix run .#fix
git add functions/compose-model-replica/function/backends/native.py \
        functions/compose-model-replica/tests/test_backends.py
git commit -s -m "feat(replica): add native single-pod backend"
```

### Task 3: Refactor fn.py to dispatch, delete KServe emission

**Files:**
- Modify: `functions/compose-model-replica/function/fn.py`
- Modify: `functions/compose-model-replica/tests/test_fn.py`

- [ ] **Step 1: Update the existing golden test to expect the native Deployment**

In `functions/compose-model-replica/tests/test_fn.py`, replace the `want1.desired.resources`
block (lines ~100-149, the `model-serving` `LLMInferenceService`) so the response now expects
the three native `Object`s the dispatcher produces for the single-pod `tensor=1` case. The
`model-serving` Object's manifest is now:

```python
{
    "apiVersion": "apps/v1",
    "kind": "Deployment",
    "metadata": {"name": "my-deployment-1154c", "namespace": "default"},
    "spec": {
        "replicas": 1,
        "selector": {"matchLabels": {"modelplane.ai/serving": "my-deployment-1154c"}},
        "template": {
            "metadata": {"labels": {"modelplane.ai/serving": "my-deployment-1154c"}},
            "spec": {
                "containers": [
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"containerPort": 8000}],
                        "resources": {"limits": {"nvidia.com/gpu": "1"}},
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                        },
                    }
                ],
                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            },
        },
    },
}
```

Add the `model-service` (`Service`) and `model-route` (`HTTPRoute`) entries to
`want1.desired.resources` mirroring `native.py`'s output, each wrapped in the same
`Object` envelope (providerConfigRef kind `ClusterProviderConfig`, name `cluster-a-pc`,
`readiness.policy` `DeriveFromObject`). Update the `name`/`subTest` label from
"composes LLMInferenceService" to "composes native Deployment".

- [ ] **Step 2: Run it to verify it fails**

Run: `nix develop -c python -m unittest discover -s functions/compose-model-replica/tests -v`
Expected: FAIL — `test_compose` mismatch (got KServe `LLMInferenceService`, want `Deployment`).

- [ ] **Step 3: Rewrite `compose_model_serving` as a dispatcher**

In `functions/compose-model-replica/function/fn.py`:

1. Add imports near the top:

```python
from function.backends import base, dynamo, llmd, native
```

2. Delete `_NAMESPACE_REMOTE`, the `_build_container`, `_build_pod_spec`, and
   `_engine_container` methods, and the entire `model.uri` / `hf://` / `--model=`
   stripping block inside `compose_model_serving`.

3. Replace `compose_model_serving` with:

```python
_BACKENDS = {
    base.NATIVE: native.NativeBackend,
    base.LLMD: llmd.LLMDBackend,
    base.DYNAMO: dynamo.DynamoBackend,
}


def compose_model_serving(self):
    """Dispatch to the backend that matches the replica's topology."""
    self.engine = base.engine_container(self.xr)
    backend_id = base.select_backend(self.xr)
    backend = _BACKENDS[backend_id]()
    deployment_name = self._deployment_name()
    for key, composed in backend.build(self.xr, self.ic, deployment_name).items():
        resource.update(self.rsp.desired.resources[key], composed)
```

4. Rename `llmis_name` to `_deployment_name` (it already reads the
   `modelplane.ai/deployment` label and returns the raw deployment name; the
   `resource.child_name` call moves into the backends). Its body becomes:

```python
def _deployment_name(self):
    labels = self.xr.metadata.labels or {}
    return labels.get(_LABEL_DEPLOYMENT, self.xr.metadata.name)
```

5. In `derive_conditions`, the readiness gate at the end keys off `MODEL_RESOURCE_KEY`
   (`"model-serving"`), which the native and llm-d backends still produce — leave that
   logic intact.

- [ ] **Step 4: Run the test to verify it passes**

Run: `nix develop -c python -m unittest discover -s functions/compose-model-replica/tests -v`
Expected: PASS (`test_compose` and all `test_backends` cases).

- [ ] **Step 5: Commit**

```bash
nix run .#fix
git add functions/compose-model-replica/function/fn.py \
        functions/compose-model-replica/tests/test_fn.py
git commit -s -m "refactor(replica): dispatch to backends, drop KServe LLMInferenceService"
```

### Task 4: llm-d backend (multi-pod)

Uses the coordinates and shapes recorded in `docs/superpowers/notes/llm-d-v0.7-surface.md` (Task 0).

**Files:**
- Create: `functions/compose-model-replica/function/backends/llmd.py`
- Modify: `functions/compose-model-replica/tests/test_backends.py`

- [ ] **Step 1: Write the failing llm-d test**

Append to `functions/compose-model-replica/tests/test_backends.py`:

```python
from function.backends import llmd
from models.io.crossplane.m.helm.release import v1beta1 as helmv1beta1


class TestLLMDBackend(unittest.TestCase):
    def setUp(self):
        self.replica = v1alpha1.ModelReplica(
            metadata=metav1.ObjectMeta(name="r", namespace="ml-team"),
            spec=v1alpha1.SpecModel(
                clusterName="cluster-a",
                workers=v1alpha1.Workers(
                    count=1,
                    topology=v1alpha1.Topology(tensor=8, pipeline=2),
                    template=v1alpha1.Template(
                        spec=v1alpha1.Spec(
                            containers=[
                                v1alpha1.Container(
                                    name="engine",
                                    image="vllm/vllm-openai:latest",
                                    args=["--model=meta-llama/Llama-3.1-405B"],
                                ),
                            ],
                        ),
                    ),
                ),
            ),
        )
        self.cluster = icv1alpha1.InferenceCluster(metadata=metav1.ObjectMeta(name="cluster-a"))

    def test_emits_modelservice_release_and_routing(self):
        out = llmd.LLMDBackend().build(self.replica, self.cluster, "llama-405b")
        release = out["model-serving"]
        self.assertIsInstance(release, helmv1beta1.Release)
        # multi-node: the chart's multinode flag is set.
        self.assertTrue(release.spec.forProvider.values["multinode"])
        # GAIE routing is composed alongside the workload.
        kinds = {
            o.spec.forProvider.manifest["kind"]
            for k, o in out.items()
            if k != "model-serving"
        }
        self.assertEqual(kinds, {"InferencePool", "InferenceObjective"})

    def test_no_cache_uses_hf_uri(self):
        out = llmd.LLMDBackend().build(self.replica, self.cluster, "llama-405b")
        values = out["model-serving"].spec.forProvider.values
        self.assertEqual(values["modelArtifacts"]["uri"], "hf://meta-llama/Llama-3.1-405B")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `nix develop -c python -m unittest discover -s functions/compose-model-replica/tests -v`
Expected: FAIL — `ImportError: cannot import name 'llmd'`.

- [ ] **Step 3: Implement the llm-d backend**

Create `functions/compose-model-replica/function/backends/llmd.py`. Use the chart
name/repo/version and the GAIE apiVersions recorded in the Task 0 notes file — substitute
them for the `_CHART_*` and `_INFERENCEPOOL_API` / `_INFERENCEOBJECTIVE_API` constants below.

```python
"""llm-d multi-pod backend.

Composes the llm-d-modelservice Helm chart (which renders the
Deployment/LeaderWorkerSet, Services, DRA ResourceClaims, and model-download
init-containers) plus the per-model GAIE routing: an InferencePool selecting the
model's pods, an InferenceObjective mapping the public model name into the pool,
and the EPP the pool references. The per-cluster GAIE CRDs/controller and the
Gateway are installed once by ServingStack.
"""

from crossplane.function import resource
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

from function.backends import base

_REMOTE_NAMESPACE = "default"
_ENGINE_PORT = 8000

# From docs/superpowers/notes/llm-d-v0.7-surface.md (Task 0):
_CHART_NAME = "llm-d-modelservice"          # confirm
_CHART_REPO = "https://llm-d-incubation.github.io/llm-d-modelservice/"  # confirm
_CHART_VERSION = "<from-spike>"              # confirm
_INFERENCEPOOL_API = "inference.networking.k8s.io/v1"        # confirm
_INFERENCEOBJECTIVE_API = "inference.networking.k8s.io/v1"   # confirm


def _model_uri(engine, replica) -> tuple[str, list[str]]:
    """Return (modelArtifacts.uri, engine_args_without_model).

    No ModelCache: hf:// direct fetch. With modelCacheRef: pvc:// mount.
    """
    model_name = ""
    args = []
    for arg in list(engine.args or []):
        if arg.startswith("--model="):
            model_name = arg.split("=", 1)[1]
        else:
            args.append(arg)
    if replica.spec.modelCacheRef:
        return f"pvc://{replica.spec.modelCacheRef.name}/", args
    return f"hf://{model_name}", args


def _object(provider_config: str, manifest: dict) -> k8sobjv1alpha1.Object:
    return k8sobjv1alpha1.Object(
        spec=k8sobjv1alpha1.Spec(
            providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                kind="ClusterProviderConfig", name=provider_config,
            ),
            readiness=k8sobjv1alpha1.Readiness(policy="DeriveFromObject"),
            forProvider=k8sobjv1alpha1.ForProvider(manifest=manifest),
        ),
    )


class LLMDBackend:
    def build(
        self,
        replica: v1alpha1.ModelReplica,
        cluster: icv1alpha1.InferenceCluster,
        deployment_name: str,
    ) -> dict[str, base.ComposedResource]:
        engine = base.engine_container(replica)
        pc = cluster.status.providerConfigRef.name
        name = resource.child_name(deployment_name)
        topology = replica.spec.workers.topology
        uri, args = _model_uri(engine, replica)
        labels = {"modelplane.ai/serving": name}

        # Helm values — keys per the Task 0 spike notes.
        values = {
            "modelArtifacts": {"uri": uri},
            "multinode": base.nodes_per_worker(replica) > 1,
            "parallelism": {"tensor": topology.tensor, "pipeline": int(topology.pipeline or 1)},
            "decode": {"replicas": int(replica.spec.workers.count or 1)},
            "containers": [{"name": "engine", "image": engine.image, "args": args}],
            "podLabels": labels,
        }
        if engine.env:
            values["containers"][0]["env"] = [e.model_dump(exclude_none=True) for e in engine.env]

        release = helmv1beta1.Release(
            spec=helmv1beta1.Spec(
                providerConfigRef=helmv1beta1.ProviderConfigRef(kind="ProviderConfig", name=pc),
                forProvider=helmv1beta1.ForProvider(
                    chart=helmv1beta1.Chart(name=_CHART_NAME, repository=_CHART_REPO, version=_CHART_VERSION),
                    namespace=_REMOTE_NAMESPACE,
                    values=values,
                ),
            ),
        )

        inference_pool = _object(pc, {
            "apiVersion": _INFERENCEPOOL_API,
            "kind": "InferencePool",
            "metadata": {"name": name, "namespace": _REMOTE_NAMESPACE},
            "spec": {
                "selector": labels,
                "targetPortNumber": _ENGINE_PORT,
                "extensionRef": {"name": f"{name}-epp"},
            },
        })

        inference_objective = _object(pc, {
            "apiVersion": _INFERENCEOBJECTIVE_API,
            "kind": "InferenceObjective",
            "metadata": {"name": name, "namespace": _REMOTE_NAMESPACE},
            "spec": {"poolRef": {"name": name}, "modelName": deployment_name},
        })

        return {
            "model-serving": release,
            "model-inferencepool": inference_pool,
            "model-inferenceobjective": inference_objective,
        }
```

> If the spike found that llm-d v0.7's well-lit path expects the EPP as its own
> Deployment+Service (rather than bundled by the chart), add `_object(...)` entries
> for the EPP here and a corresponding assertion in Step 1.

- [ ] **Step 4: Run the test to verify it passes**

Run: `nix develop -c python -m unittest discover -s functions/compose-model-replica/tests -v`
Expected: PASS (`TestLLMDBackend` green).

- [ ] **Step 5: Commit**

```bash
nix run .#fix
git add functions/compose-model-replica/function/backends/llmd.py \
        functions/compose-model-replica/tests/test_backends.py
git commit -s -m "feat(replica): add llm-d multi-pod backend"
```

### Task 5: Dynamo stub backend

**Files:**
- Create: `functions/compose-model-replica/function/backends/dynamo.py`
- Modify: `functions/compose-model-replica/tests/test_backends.py`

- [ ] **Step 1: Write the failing stub test**

Append to `functions/compose-model-replica/tests/test_backends.py`:

```python
from function.backends import dynamo


class TestDynamoStub(unittest.TestCase):
    def test_not_selected_in_v01(self):
        # No Dynamo-only capability is wired, so dispatch never returns DYNAMO.
        self.assertNotEqual(base.select_backend(_replica(tensor=8, pipeline=2)), base.DYNAMO)

    def test_build_raises(self):
        with self.assertRaises(NotImplementedError):
            dynamo.DynamoBackend().build(_replica(tensor=8, pipeline=2), icv1alpha1.InferenceCluster(), "d")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `nix develop -c python -m unittest discover -s functions/compose-model-replica/tests -v`
Expected: FAIL — `ImportError: cannot import name 'dynamo'`.

- [ ] **Step 3: Implement the stub**

Create `functions/compose-model-replica/function/backends/dynamo.py`:

```python
"""NVIDIA Dynamo backend — designed-for, not built in v0.1.

The dispatcher never selects this in v0.1 (no Dynamo-only capability is wired).
When built, build() will emit a DynamoGraphDeployment (nvidia.com/v1alpha1)
Object reconciled by the Dynamo operator installed by ServingStack.
"""

from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1

from function.backends import base


class DynamoBackend:
    def build(
        self,
        replica: v1alpha1.ModelReplica,
        cluster: icv1alpha1.InferenceCluster,
        deployment_name: str,
    ) -> dict[str, base.ComposedResource]:
        raise NotImplementedError("the Dynamo backend is not implemented in v0.1")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `nix develop -c python -m unittest discover -s functions/compose-model-replica/tests -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
nix run .#fix
git add functions/compose-model-replica/function/backends/dynamo.py \
        functions/compose-model-replica/tests/test_backends.py
git commit -s -m "feat(replica): add Dynamo backend stub (dormant in v0.1)"
```

---

## Phase 2 — Rename KServeBackend → ServingStack

### Task 6: Rename the XRD and regenerate schemas

**Files:**
- Rename: `apis/kservebackends/` → `apis/servingstacks/`
- Modify: `apis/servingstacks/definition.yaml`, `apis/servingstacks/composition.yaml`
- Regenerate: `schemas/python/models/ai/modelplane/infrastructure/servingstack/`

- [ ] **Step 1: Move the directory**

```bash
git mv apis/kservebackends apis/servingstacks
```

- [ ] **Step 2: Edit `apis/servingstacks/definition.yaml`**

- `metadata.name`: `servingstacks.infrastructure.modelplane.ai`
- `spec.names`: `kind: ServingStack`, `plural: servingstacks`, `shortNames: [ss]`
- Update the `description` to "installs the serving substrate (LeaderWorkerSet, Gateway
  API + inference extension, cert-manager, Prometheus, and llm-d) on a Kubernetes cluster."
- In `spec.versions[0].schema...properties.spec.properties.versions.properties`:
  remove `kserve` and `keda`; add:

```yaml
                  llmD:
                    type: string
                    default: "v0.7.0"
                    description: llm-d-modelservice chart version.
                    minLength: 1
                    maxLength: 32
                  gatewayApiInferenceExtension:
                    type: string
                    default: "v1.0.1"
                    description: Gateway API Inference Extension version.
                    minLength: 1
                    maxLength: 32
```

  Bump `leaderWorkerSet` default `"v0.7.0"` → `"v0.8.0"`. Keep `certManager`,
  `envoyGateway`, `prometheus`.
- Update the `KSERVE` printer column (`jsonPath: .spec.versions.kserve`) to
  `name: LLMD`, `jsonPath: .spec.versions.llmD`.

- [ ] **Step 3: Edit `apis/servingstacks/composition.yaml`**

- `metadata.name`: `servingstacks.infrastructure.modelplane.ai`
- `spec.compositeTypeRef.kind`: `ServingStack`
- `spec.pipeline[0].functionRef.name`: `modelplane-modelplanecompose-serving-stack`
- `spec.pipeline[0].step`: `compose-serving-stack`

- [ ] **Step 4: Regenerate schemas**

Run: `nix run .#generate`
Expected: a new `schemas/python/models/ai/modelplane/infrastructure/servingstack/` package
appears and `.../kservebackend/` disappears.

- [ ] **Step 5: Verify the build sees the new model**

Run: `nix develop -c python -c "from models.ai.modelplane.infrastructure.servingstack import v1alpha1; print(v1alpha1.ServingStack)"`
Expected: prints the class without error.

- [ ] **Step 6: Commit**

```bash
git add apis/servingstacks schemas/python
git commit -s -m "feat(api): rename KServeBackend XRD to ServingStack"
```

### Task 7: Rename the function package (behavior preserved)

This step renames the function and updates the model import only — it does **not** change
what gets installed yet (Task 8 does that), so its tests stay green throughout.

**Files:**
- Rename: `functions/compose-kserve-backend/` → `functions/compose-serving-stack/`
- Modify: that function's `pyproject.toml`, `function/fn.py` (imports + class), `tests/test_fn.py`
- Modify: `crossplane-project.yaml`

- [ ] **Step 1: Move the directory**

```bash
git mv functions/compose-kserve-backend functions/compose-serving-stack
```

- [ ] **Step 2: Update package metadata and project file**

- In `functions/compose-serving-stack/pyproject.toml`, rename the project/package name
  `compose-kserve-backend` → `compose-serving-stack` (and any script entry).
- In `crossplane-project.yaml`, change the function tarball block:
  `name: compose-kserve-backend` → `name: compose-serving-stack`,
  `pathPrefix: _output/functions/compose-kserve-backend` →
  `_output/functions/compose-serving-stack`.

- [ ] **Step 3: Update the model import and XR class in `fn.py`**

In `functions/compose-serving-stack/function/fn.py`:
- `from models.ai.modelplane.infrastructure.kservebackend import v1alpha1`
  → `from models.ai.modelplane.infrastructure.servingstack import v1alpha1`
- `self.xr = v1alpha1.KServeBackend(...)` → `self.xr = v1alpha1.ServingStack(...)`

- [ ] **Step 4: Update the test's typed XR**

In `functions/compose-serving-stack/tests/test_fn.py`, replace `KServeBackend` model
construction with `ServingStack` and update the import path. Leave the asserted installed
resources unchanged for now.

- [ ] **Step 5: Run tests**

Run: `nix flake check`
Expected: PASS (function renamed, behavior identical).

- [ ] **Step 6: Commit**

```bash
nix run .#fix
git add functions/compose-serving-stack crossplane-project.yaml
git commit -s -m "feat(fn): rename compose-kserve-backend to compose-serving-stack"
```

### Task 8: Swap ServingStack contents — drop KServe/KEDA, add llm-d

Uses the chart coordinates from `docs/superpowers/notes/llm-d-v0.7-surface.md` (Task 0).

**Files:**
- Modify: `functions/compose-serving-stack/function/fn.py`
- Modify: `functions/compose-serving-stack/tests/test_fn.py`

- [ ] **Step 1: Update the test to expect llm-d, not KServe/KEDA**

In `functions/compose-serving-stack/tests/test_fn.py`, edit the expected response so that:
- the `kserve-crds`, `kserve-controller`, `kserve-storage-patch`, and `keda` resources are
  **absent**;
- an `llm-d-infra` and `llm-d-modelservice` Helm `Release` (chart coordinates from the spike)
  are **present**;
- the `leader-worker-set` Release version is `v0.8.0`;
- `cert-manager`, `envoy-gateway`, `prometheus`, the inference-extension CRDs, `gateway-class`,
  and `gateway` remain present;
- the `gateway` manifest's name is `inference-gateway` and namespace `modelplane-system`
  (renamed from `kserve-ingress-gateway` / `kserve`).

- [ ] **Step 2: Run it to verify it fails**

Run: `nix develop -c python -m unittest discover -s functions/compose-serving-stack/tests -v`
Expected: FAIL — KServe/KEDA resources still composed; llm-d absent.

- [ ] **Step 3: Edit `fn.py`**

In `functions/compose-serving-stack/function/fn.py`:
1. Delete `compose_kserve`, `compose_keda`, `compose_storage_patch`, and the
   `_STORAGE_INITIALIZER_CONFIG` / `_KUSTOMIZE_STORAGE_PATCH` / `_KEDA_*` constants and
   `_keda_release`.
2. Remove `compose_keda()`, `compose_kserve()`, `compose_storage_patch()` from
   `compose()`; remove `kserve-storage-patch` from `mark_readiness().always_ready` and
   `kserve-crds` / `kserve-controller` / `keda` from `condition_ready`.
3. In `compose_gateway()`, rename the Gateway manifest `metadata.name` to
   `"inference-gateway"` and `metadata.namespace` to `"modelplane-system"`.
4. Add `compose_llm_d()` (gated on ProviderConfigs observed, like the others), composing two
   Helm Releases — `llm-d-infra` and `llm-d-modelservice` (the cluster-scoped install of the
   chart's CRDs/controller prerequisites) — using `_helm_release(...)` with the chart
   coordinates from the spike and `version=v.llmD`. Add `compose_llm_d()` to `compose()`
   after `compose_leader_worker_set()`, and add the two release keys to
   `mark_readiness().condition_ready`.
5. Update the `Versions()` access: `v.kserve` references are gone; new `v.llmD` and
   `v.gatewayApiInferenceExtension` are available from the regenerated model.

- [ ] **Step 4: Run the test to verify it passes**

Run: `nix develop -c python -m unittest discover -s functions/compose-serving-stack/tests -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
nix run .#fix
git add functions/compose-serving-stack
git commit -s -m "feat(serving-stack): install llm-d, drop KServe and KEDA"
```

### Task 9: Update compose-inference-cluster references

**Files:**
- Modify: `functions/compose-inference-cluster/function/fn.py`
- Modify: `functions/compose-inference-cluster/tests/test_fn.py`

- [ ] **Step 1: Update the test's expected composed XR**

In `functions/compose-inference-cluster/tests/test_fn.py`, change the expected backend XR
from `KServeBackend` to `ServingStack` (apiVersion unchanged: `infrastructure.modelplane.ai/v1alpha1`),
its resource key from `kserve-backend` to `serving-stack`, its name suffix from `-kserve` to
`-serving-stack`, and its `spec.versions` from `{kserve: "v0.16.0"}` to the new default
(omit version pins to use XRD defaults, or set `{llmD: "v0.7.0"}`).

- [ ] **Step 2: Run it to verify it fails**

Run: `nix develop -c python -m unittest discover -s functions/compose-inference-cluster/tests -v`
Expected: FAIL — still composes `KServeBackend`.

- [ ] **Step 3: Edit `fn.py`**

In `functions/compose-inference-cluster/function/fn.py`:
- Import: `from models.ai.modelplane.infrastructure.kservebackend import v1alpha1 as kssv1alpha1`
  → `from models.ai.modelplane.infrastructure.servingstack import v1alpha1 as ssv1alpha1`
  (update all `kssv1alpha1.` usages to `ssv1alpha1.`).
- `BACKEND_RESOURCE_KEY = "kserve-backend"` → `"serving-stack"`.
- Delete `KSERVE_VERSION = "v0.16.0"`.
- Rename method `compose_kserve_backend` → `compose_serving_stack`; update its two call sites.
- In that method: `kssv1alpha1.KServeBackend(...)` → `ssv1alpha1.ServingStack(...)`;
  `resource.child_name(self.xr.metadata.name, "kserve")` →
  `resource.child_name(self.xr.metadata.name, "serving-stack")`;
  `spec=ssv1alpha1.Spec(versions=ssv1alpha1.Versions(kserve=KSERVE_VERSION), secrets=...)`
  → drop the `versions=` arg (use XRD defaults): `spec=ssv1alpha1.Spec(secrets=backend_secrets)`.
- Update the module docstring and comments mentioning KServe.

- [ ] **Step 4: Run the test to verify it passes**

Run: `nix develop -c python -m unittest discover -s functions/compose-inference-cluster/tests -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
nix run .#fix
git add functions/compose-inference-cluster
git commit -s -m "refactor(inference-cluster): compose ServingStack instead of KServeBackend"
```

---

## Phase 3 — Docs, examples, and the no-KServe gate

### Task 10: Update examples and docs

**Files:**
- Modify: `examples/qwen-demo/00-prerequisites.yaml`, `examples/qwen-demo/03-cluster.yaml`,
  `examples/deployment/model-deployment-multinode.yaml`, `docs/concepts.md`,
  `docs/getting-started.md`

- [ ] **Step 1: Find every KServe / LLMInferenceService reference**

Run: `grep -rn -i "kserve\|llminferenceservice" examples/ docs/concepts.md docs/getting-started.md`
Expected: a list of lines to edit.

- [ ] **Step 2: Rewrite the references**

- Replace `KServeBackend` references with `ServingStack`.
- Replace any "KServe installs / LLMInferenceService" narrative in `docs/concepts.md` with the
  native + llm-d dispatch model (single-pod → Deployment+Service; multi-pod → llm-d).
- **Add the weight-loading contract note** (spec §"Weight loading"): without a `ModelCache`,
  the engine fetches weights at startup and the deployment must provide source credentials
  (e.g. `HF_TOKEN` via `engine.env`); the engine image must support the source.
- Remove KServe-specific prerequisites from the qwen-demo manifests.

- [ ] **Step 3: Commit**

```bash
git add examples docs/concepts.md docs/getting-started.md
git commit -s -m "docs: replace KServe with native + llm-d dispatch; document direct weight fetch"
```

### Task 11: Final verification and no-KServe gate

- [ ] **Step 1: Grep for any remaining KServe references**

Run: `grep -rn -i "kserve" --include='*.py' --include='*.yaml' --include='*.md' . | grep -v '.venv\|_output\|docs/superpowers/specs\|docs/superpowers/plans'`
Expected: **no matches** (the only acceptable remaining mentions are in the spec/plan docs).
If any appear, fix them and amend the relevant commit.

- [ ] **Step 2: Run the full check suite**

Run: `nix flake check`
Expected: PASS — all function unit tests, linters, and formatters green.

- [ ] **Step 3: Confirm the function inventory**

Run: `grep -n 'compose-' crossplane-project.yaml`
Expected: `compose-serving-stack` present; `compose-kserve-backend` absent.

- [ ] **Step 4: Commit any final fixes**

```bash
nix run .#fix
git add -A
git commit -s -m "chore: final cleanup after KServe removal" || echo "nothing to commit"
```

---

## Self-review (completed by plan author)

**Spec coverage:**
- Dispatch model / predicate → Task 1 (with the v0.1 `pipeline`-only reality from the spec note).
- One-function/strategy-module architecture → Tasks 1–5 (`backends/` package, no extra XR).
- Native backend → Task 2; llm-d backend → Task 4; Dynamo stub → Task 5.
- `ComposedResource = Object | Release` interface → Task 1.
- Delete `hf://` hack + `_VLLM_MULTI_NODE_BOOTSTRAP` → Task 3 (the Ray bootstrap is not on
  `main`, so only the `hf://` hack needs deletion; noted so an executor doesn't hunt for absent code).
- `KServeBackend` → `ServingStack` neutral substrate, version table (drop KServe/KEDA, bump
  LWS, add llm-d/GAIE, keep cert-manager/Prometheus/Envoy) → Tasks 6, 8.
- compose-inference-cluster rewiring → Task 9.
- Weight-loading contract + user docs → Tasks 2/4 (mechanism) and Task 10 (docs).
- GAIE routing per-replica, CRDs per-cluster → Task 4 (per-model `InferencePool`/`InferenceObjective`)
  and Task 8 (per-cluster CRDs/Gateway).
- llm-d spike (the spec's one open unknown) → Task 0.
- No-KServe gate + tests → Task 11.

**Placeholder scan:** The only deferred values are the llm-d chart version and GAIE
apiVersions, which Task 0 resolves into a committed notes file that Tasks 4 and 8 cite by
name. These are real external-API dependencies, not hand-waving.

**Type consistency:** `select_backend`/`needs_cross_pod_coordination`/`nodes_per_worker`/
`engine_container` (Task 1) are used unchanged in Tasks 2–5. `ComposedResource` and the
`build(replica, cluster, deployment_name) -> dict[str, ComposedResource]` signature are
identical across `native.py`, `llmd.py`, `dynamo.py`, and the `fn.py` dispatcher. Response
keys `model-serving` / `model-service` / `model-route` / `model-inferencepool` /
`model-inferenceobjective` are consistent between backends and the updated golden tests.
