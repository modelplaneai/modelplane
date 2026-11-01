# Worked Examples

Real before and after pairs from composition function code, taken from the
sessions where these rules got made. The "before" in each case is what an agent
wrote and Nic corrected, so treat the left column as the thing to avoid.

Identifiers are as they appeared, minus anything environment-specific.

## Reading through a typed model

The rule applies to reads, not just writes. A chain of `.get()` calls has the
same defect as building a dict: nothing checks the keys.

```python
# Before.
def _observed_efs_filesystem_id(self):
    observed = self.req.observed.resources.get("efs-filesystem")
    if not observed:
        return None
    manifest = resource.struct_to_dict(observed.resource)
    return manifest.get("metadata", {}).get("annotations", {}).get("crossplane.io/external-name")

# After.
def _observed_efs_filesystem_id(self):
    observed = self.req.observed.resources.get("efs-filesystem")
    if not observed:
        return None
    fs = fsv1beta1.FileSystem.model_validate(resource.struct_to_dict(observed.resource))
    if not fs.metadata or not fs.metadata.annotations:
        return None
    return fs.metadata.annotations.get(_ANNOTATION_EXTERNAL_NAME)
```

The read now goes through the generated model, and the magic string became a
constant. The `if not observed` guard stayed, because a resource
really can be absent on an early reconcile. That's the distinction: guard a
state that happens, drop the guards for states the schema rules out.

## Dropping guards the schema already makes impossible

Same file, later in the same session. Every `x if x and x.y else` chain here was
defending against a field the XRD marks required.

```python
# Before.
env_name = env.metadata.name if env.metadata else ""
capacity = env.status.capacity if env.status and env.status.capacity else None
backend = capacity.backend if capacity else ""

if engine not in _COMPAT.get(backend, []):
    continue

gpu_pools = capacity.gpuPools if capacity and capacity.gpuPools else []

# After.
env_name = env.metadata.name if env.metadata else ""
backend = env.status.capacity.backend or ""

if engine not in _COMPAT.get(backend, []):
    continue

gpu_pools = env.status.capacity.gpuPools
```

`capacity` existed only to hold the defensive chain. Once the guards go, so
does the variable.

## Locals that only shorten an expression

The same loop, one pass later.

```python
# Before.
for p in all_placements:
    p_labels = p.metadata.labels if p.metadata and p.metadata.labels else {}
    p_deployment = p_labels.get("modelplane.ai/deployment", "")
    if p_deployment == xr_name:
        continue
    p_ie = p.spec.inferenceEnvironmentRef.name if p.spec and p.spec.inferenceEnvironmentRef else ""
    if p_ie == env_name:
        p_gpu_count = 0
        if p.status and p.status.resources and p.status.resources.gpu:
            p_gpu_count = p.status.resources.gpu.count or 0
        used_gpus += p_gpu_count

# After.
for p in all_placements:
    p_labels = p.metadata.labels or {}
    p_deployment = p_labels.get("modelplane.ai/deployment", "")
    if p_deployment == xr_name:
        continue
    p_ie = p.spec.inferenceEnvironmentRef.name if p.spec and p.spec.inferenceEnvironmentRef else ""
    if p_ie == env_name:
        used_gpus += p.status.resources.gpu.count or 0
```

`p_gpu_count` names nothing the expression doesn't already say, so four lines
become one. `p_labels` and `p_deployment` survive, because they hold a magic
string and a comparison that reads better named.

## Inlining a value used once

Final pass over the same block.

```python
# Before.
backend = env.status.capacity.backend or ""

if engine not in _COMPAT.get(backend, []):
    continue

gpu_pools = env.status.capacity.gpuPools
best_gpus_needed = None
for pool in gpu_pools:

# After.
if engine not in _COMPAT.get(env.status.capacity.backend or "", []):
    continue

best_gpus_needed = None
for pool in env.status.capacity.gpuPools:
```

Each of the three passes removed a name rather than adding structure, and the
code got shorter every time.
