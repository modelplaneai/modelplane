---
name: crossplane-python-functions
description: Write and review Modelplane's Python composition functions under functions/. Use when writing, debugging, refactoring or reviewing a composition function, or when dealing with required resources, readiness, resource gating, deletion ordering or Usages.
---

# Writing Crossplane Python Composition Functions

## How Modelplane Builds Functions

The Working on composition functions section of CONTRIBUTING.md is the
reference for how Modelplane lays out, builds and tests a function. In short:

- Each function is a standalone Python package under `functions/<name>/`. Its
  logic is in `function/fn.py`, as a `Composer` that a gRPC `FunctionRunner`
  calls. `compose-inference-gateway/function/fn.py` is the model to follow.
- `FunctionRunner.RunFunction` builds the response with `response.to(req)`,
  runs the `Composer`, and returns the response. That's the SDK's standard
  runner, and it's correct here.
- Functions import generated Pydantic models from the `models` package,
  generated into `schemas/python/` from the XRDs and dependency CRDs. Managed
  resources use the `.m.` (namespaced) variants:

```python
from models.ai.modelplane.inferencegateway import v1alpha1
from models.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
```

- `nix run .#build` regenerates the models after an XRD or dependency
  changes. Never edit them by hand.
- `nix run .#test -- <function>` runs one function's tests, and
  `nix flake check` runs everything. `nix run .#run` deploys the project to a
  local control plane.

The rest of this skill covers how Crossplane behaves, which is what most bugs
in a function get wrong.

## Composing Resources

Use Pydantic models for everything with a generated model. They catch field
name mistakes at write time:

```python
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
```

Raw dicts are the fallback for CRDs without generated models (Gateway API,
MetalLB, Usages, etc.):

```python
resource.update(rsp.desired.resources["gateway-class"], {
    "apiVersion": "gateway.networking.k8s.io/v1",
    "kind": "GatewayClass",
    "metadata": {"name": "envoy"},
    "spec": {"controllerName": "gateway.envoyproxy.io/gatewayclass-controller"},
})
```

### Protobuf Number Coercion

All numbers from XRD fields arrive as Python `float`, not `int`. Cast
explicitly:

```python
desired_envs = int(xr.spec.environments)  # protobuf delivers as float
```

## Readiness

Set XR readiness via `rsp.desired.composite.ready`:

```python
rsp.desired.composite.ready = fnv1.READY_TRUE
```

Do NOT set the `Ready` condition via `fnv1.Condition`, because it's reserved.
Do NOT use `TARGET_COMPOSITE_AND_CLAIM`, because Crossplane v2 has no claims.

For composed resources, check the appropriate condition (not always `Ready`):

```python
from crossplane.function import resource

def _has_condition(req, name, cond):
    """Check if an observed composed resource has a condition set to True."""
    observed = req.observed.resources.get(name)
    if observed is None:
        return False
    return resource.get_condition(observed.resource, cond).status == "True"

# Crossplane MRs use Ready
if _has_condition(req, "my-cluster", "Ready"):
    rsp.desired.resources["my-cluster"].ready = fnv1.READY_TRUE

# Gateway API uses Accepted
if _has_condition(req, "my-gateway", "Accepted"):
    rsp.desired.resources["my-gateway"].ready = fnv1.READY_TRUE
```

Mark resources without conditions (ProviderConfigs, ConfigMaps) as
always-ready:

```python
rsp.desired.resources["my-providerconfig"].ready = fnv1.READY_TRUE
```

## Results as Kubernetes Events

`response.normal()` and `response.warning()` emit Kubernetes events visible
in `kubectl describe`. Use them deliberately:

- **Normal (every reconcile, time-bounded):** "Waiting for: gke-cluster".
  Expected, self-resolving. Stops when the resource becomes ready.
- **Normal (transition, once):** "GKE cluster ready, composing KServeStack".
  A milestone. Detect transitions by comparing current state to observed state.
  <!-- vale off -->
- **Warning (every reconcile, persistent):** "spec.kserve.cluster.gke is
  required".
  <!-- vale on -->
  The user can fix it, and it won't self-resolve.

Detect transitions:
```python
was_ready = resource.get_condition(
    req.observed.composite.resource, "Ready"
).status == "True"

if not not_ready:
    rsp.desired.composite.ready = fnv1.READY_TRUE
    if not was_ready:
        response.normal(rsp, "Ready, gateway: 34.55.233.135")
else:
    response.normal(rsp, f"Waiting for: {', '.join(not_ready)}")
```

Never emit Normal events on every reconcile in the steady state. They
accumulate as Kubernetes Event objects.

## Gating Resources on Dependencies

Only compose a resource once Crossplane observes its dependencies. But once
composed, always keep emitting it. If a function omits a resource from desired
state, Crossplane deletes it.

```python
pc_observed = "provider-config" in req.observed.resources
release_exists = "cert-manager" in req.observed.resources

# Gate on dependency, but always emit once it exists
if pc_observed or release_exists:
    resource.update(rsp.desired.resources["cert-manager"], ...)
```

For readiness-based gating (e.g., KServe waits for cert-manager):

```python
cert_ready = _has_condition(req, "cert-manager", "Ready")
kserve_exists = "kserve" in req.observed.resources

if cert_ready or kserve_exists:
    resource.update(rsp.desired.resources["kserve"], ...)
```

## Required Resources (Cross-XR Reads)

Use required resources to read resources the function doesn't own:

```python
from crossplane.function import request, response

response.require_resources(
    rsp, name="cluster",
    api_version="platform.example.org/v1alpha1",
    kind="Cluster",
    match_name="my-cluster",
)

cluster = request.get_required_resource(req, "cluster")
if cluster is None:
    return  # Crossplane will re-call with the resource
```

Required resources are dicts, not Pydantic models. They're external resources
that Crossplane resolves.

The empty `match_labels={}` selector doesn't work. Protobuf serialization loses
the empty map, causing the `oneof match` to be unset. Use a real label to match
on, or use `match_name` for specific resources.

## Deletion Ordering with Usages

When you delete a composite, all composed resources get deletion timestamps
simultaneously. Resources with implicit dependencies (ProviderConfigs used by
Helm releases, clusters used by stacks installed on them) need explicit ordering.

Use `protection.crossplane.io/v1beta1` Usages:

```python
resource.update(rsp.desired.resources["usage-pc-by-release"], {
    "apiVersion": "protection.crossplane.io/v1beta1",
    "kind": "Usage",
    "metadata": {"namespace": "my-namespace"},
    "spec": {
        "of": {
            "apiVersion": "helm.m.crossplane.io/v1beta1",
            "kind": "ProviderConfig",
            "resourceRef": {"name": "my-provider-config"},
        },
        "by": {
            "apiVersion": "helm.m.crossplane.io/v1beta1",
            "kind": "Release",
            "resourceSelector": {
                "matchControllerRef": True,
                "matchLabels": {"my-label": "my-release"},
            },
        },
        "replayDeletion": True,
    },
})
```

Key rules:

- **One Usage per protected-by pair.** The selector resolves to the first
  matching resource and stops. A pair of releases needs a separate Usage for
  each, with distinct labels.
- **Use labels to distinguish resources** of the same kind in the selector.
- **`replayDeletion: True`** means that deleting the "by" resource also
  deletes the Usage, and Crossplane then retries deleting the "of" resource.
- **Usages don't prevent Terminating state.** A namespace with a
  `deletionTimestamp` blocks new object creation even if a Usage prevents its
  final deletion. Use shared long-lived namespaces instead of per-resource
  namespaces.

## managementPolicies for Undeletable Resources

Some resources can't be cleanly deleted (webhook-protected CRDs, resources with
controller-managed finalizers). Use `managementPolicies` to skip deletion:

```python
release.spec.managementPolicies = ["Create", "Observe", "Update"]
```

This is appropriate when the underlying infrastructure is going away anyway
(e.g., KServe on a GKE cluster that you're deprovisioning). Crossplane orphans
the resource, and it goes away with the cluster.

## Cluster-Scoped XRs Composing Namespaced Resources

Cluster-scoped XRs don't auto-populate `metadata.namespace` on composed
namespaced resources. Set it explicitly:

```python
helmv1beta1.Release(
    metadata=metav1.ObjectMeta(namespace="my-namespace"),
    spec=...,
)
```

Without this, the resource has no namespace in the XR's `resourceRefs` and
Crossplane can't GET it.

## Debugging

When something is slow or stuck during deletion, describe the resources:

```bash
kubectl describe <resource> -n <namespace>
```

Every "slow" deletion has a specific stuck resource with a specific error.
Common causes:

- **ProviderConfig not found:** deleted before the resources that use it. Fix
  with a Usage.
- **Namespace is terminating:** blocks ProviderConfigUsage creation. Fix by
  using a shared namespace.
- **Webhook has no endpoints:** something deleted the webhook server pod before
  the webhook-protected resources. Fix with `managementPolicies`.
- **Finalizer blocking deletion:** a remote controller hasn't processed the
  deletion yet (e.g., GatewayClass `gateway-exists-finalizer`). Fix with
  `managementPolicies` or a Usage to ensure Crossplane deletes the dependent
  first.

Never increase timeouts without first describing the stuck resources. The fix
is always in the deletion ordering, not the timeout.
