# Caching

Design proposal for managed model staging on workload clusters.

## What's now

Caching is invisible to ML teams. Platform teams declare RWX storage capability
on `InferenceCluster.spec.storage.rwxCache`:

```yaml
kind: InferenceCluster
spec:
  storage:
    rwxCache:
      storageClassName: standard-rwx
      defaultSizeGiB: 200
```

`ModelDeployment` has no caching surface. When the composition function
composes a `ModelReplica` for a multi-node deployment
(`workers.topology.pipeline > 1`), it reads the target cluster's `rwxCache`
config and auto-provisions a PVC + hydration Job on the workload cluster.
Single-node deployments fetch ephemerally in the engine container.

If the target cluster has no `rwxCache` declared, multi-node deployments
there fail-fast with a clear error.

The function's behavior:

| Topology | Behavior |
|---|---|
| Single-node | Ephemeral fetch in engine container. |
| Multi-node | Auto-provision PVC from cluster `rwxCache`. Fail-fast if not declared. |

ML teams have no caching surface today. Single-node cold-start optimization
and BYO storage are both deferred to the future `ModelCache` / `cacheRef`
shape.

### Scale and outlier models

The cluster's `defaultSizeGiB` covers the typical case — most LLMs fit in
~200 GiB. For very large models (e.g., 1 TB+ frontier-scale weights), the
platform team raises the cluster default to fit and picks a storage backend
that can hold it. The backend choice trades cold-start hydration time
against ongoing storage cost; both are per-cluster decisions paid by the
platform team. ML teams don't see this — they get whatever fits on the
cluster they target.

## What's future

A shared, referenceable cache primitive for ML teams who want cross-
deployment sharing or per-cache lifecycle:

- **ModelCache** (shared instance): a first-class resource that stages an
  artifact on workload-cluster storage independently of any deployment.
  Multiple ModelDeployments reference one cache by name; one hydration
  serves all of them.
- **`ModelDeployment.spec.modelCacheRef`** (ML opt-in): reference a
  ModelCache. The deployment mounts the cache's PVC into every worker pod
  automatically.

ModelCache also gives ML teams a per-cache size override, so a single
deployment for an outlier model doesn't require the platform team to raise
the cluster default. The cluster-level invisible behavior keeps working in
parallel; ModelCache is purely additive.

## Rationale: separation of concerns

`docs/concepts.md` already draws a boundary between platform teams (who
provision infrastructure) and ML teams (who deploy models). Storage is
infrastructure: it lives on cluster-scoped resources, CSI drivers are
installed per-cluster, RWX backends differ by provider. Exposing storage
primitives on a user-facing resource leaks infra concepts across the
boundary.

This design respects the boundary. ML teams write a ModelDeployment and
don't learn what "ReadWriteMany" means. Platform teams declare storage
once per cluster, on the resource they already own.

Storage choice doesn't affect inference behavior at steady state — model
weights load once into GPU memory and storage is silent during inference.
The choice only affects cold-start time and cost, both of which are
infrastructure-level optimizations that vary by cluster, not by deployment.
The bespoke ML-team needs that would justify per-deployment overrides —
custom CSI drivers, pre-staged PVCs, exotic backends — are rare and better
addressed by future opt-in (`ModelCache` / `cacheRef`) than by exposing
storage primitives now.

## Why hardcoding is OK for now

The composition function hardcodes a number of choices:

| Decision | Hardcoded value | Why it's OK |
|---|---|---|
| Cache trigger | `pipeline > 1` (multi-node only) | Multi-node is where concurrent fetches actually race; single-node ephemeral fetch works. |
| PVC size | Cluster `defaultSizeGiB` | One size per cluster is enough until a deployment needs an outlier. `ModelCache` is the per-cache override. |
| Cache lifecycle | Bound to ModelReplica | Cross-deployment sharing isn't required today. Independent lifecycle ships with `ModelCache`. |
| Hydration image / resources / tolerations | Modelplane internal | Platform concerns; ML teams shouldn't tune them. |

Each hardcoded choice has a future-PR escape valve via `ModelCache`. No
current choice closes off any of those paths — additions stay additive.
