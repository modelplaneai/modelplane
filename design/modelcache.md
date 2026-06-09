# ModelCache

**Status:** Draft
**Date:** May 2026
**Author:** Dennis Ramdass

This document proposes `ModelCache`, a resource for staging model weights on
workload-cluster storage. It builds on the base design in [design.md](./design.md).

## Summary

A ModelCache stages a model artifact on workload-cluster storage as a
first-class resource. Modelplane composes a ReadWriteMany PVC on each matched
cluster and hydrates it from the configured source. ModelDeployments reference
a cache via `spec.modelCacheRef.name`; the cache's PVC is mounted into every
worker pod automatically.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelCache
metadata:
  name: kimi-k2
  namespace: ml-team
spec:
  source: HuggingFace
  huggingFace:
    repo: moonshotai/Kimi-K2-Instruct
    authSecret:
      name: hf-token
    sizeGiB: 1500
```

The cache is mounted at `/mnt/models` on every consuming pod; the engine
container's model arg should reference this path. The path is fixed rather
than configurable to keep Modelplane out of engine-specific arg rewriting —
different engines take different flags (e.g. `--model=` for vLLM,
`--model-path=` for SGLang) and users pass whatever their engine expects.

`spec.source` is a required discriminator: an enum naming the source kind, with
the matching source object set alongside it (e.g. `source: HuggingFace` selects
`spec.huggingFace`). A CEL rule requires that object, so the API is validated at
admission rather than inferring the source from which fields are set, and the
variant a user wants is always explicit. `HuggingFace` is the only value today;
each source declares its own fields, including capacity when the source uses
central storage. Future sources add an enum value and a sibling object:
`Dragonfly` would distribute the artifact to per-node local caches via P2P with
no shared PVC (no `sizeGiB`, since there's no central storage); `S3` would
mirror the `huggingFace` shape with a different fetch protocol; `OCI` would pull
a bundled OCI artifact into the PVC, which is where NVIDIA NIM-style prepackaged
engine+weights bundles fit.

## Multi-node

Multi-node deployments require a `modelCacheRef`. Every pod in the
LeaderWorkerSet gang must load the same model weights, and concurrent fetches
don't coordinate safely. The failure modes are mostly invisible: simultaneous
writes to overlapping paths tear files; pods that land on different upstream
revisions if the source updates mid-pull serve mismatched weights to a single
TP/PP topology; partial downloads can leave a pod serving incorrect weights
with no surface error; concurrent egress hits HuggingFace rate limits
non-deterministically. Modelplane rejects multi-node deployments without a
cache reference. Single-node deployments may reference a cache as a
cold-start optimization; without one the engine fetches the model ephemerally
at startup.

## Storage backends

The cache's PVC backend is picked per cluster. For Modelplane-provisioned
clusters (`cluster.source: GKE` today), Modelplane provisions a Filestore
Enterprise instance with multishare on first ModelCache creation, composes a
StorageClass named `modelplane-rwx` against it, and marks that StorageClass
as the cluster's default — RWX is the right default for an inference cluster.
Subsequent caches share the same Filestore instance. For existing clusters
(`cluster.source: Existing`), the admin creates an RWX StorageClass on the
workload cluster; Modelplane uses the convention name `modelplane-rwx` unless
overridden, and doesn't touch the cluster's default class annotation.

Platform teams can override the backend on either source type by setting
`cluster.<source>.cache.storageClassName` to a StorageClass already created
on the workload cluster. Any backend supporting RWX dynamic provisioning
works (WekaIO, NetApp Trident, FSx for NetApp, etc.). The ML team's
ModelCache and ModelDeployment specs are unchanged regardless of backend.

## Alternatives considered

### CacheClass (cache storage recipe)

I considered a `CacheClass` resource parallel to `InferenceClass` — a
platform-team-defined named recipe for storage tier, capacity, backup policy,
and so on. ModelDeployments or ModelCaches would reference one by name.

Storage decisions today reduce to one knob: "does the platform team want a
non-default backend on this cluster?" One optional
`cluster.<source>.cache.storageClassName` field covers that. A recipe layer
would split storage configuration across two resources and introduce
cross-resource references ML teams have to reason about. If real demand for
multiple tiers per cluster emerges, a recipe abstraction can be added
additively without breaking the current shape.

### Storage backend enum on InferenceCluster

I considered a `cacheBackend: Filestore | GCSFuse | StorageClass` enum on
`cluster.gke` (and analogs per provider). It mixed categories — `Filestore`
is a product name, `StorageClass` is a Kubernetes primitive — and didn't
generalize cross-provider. Adding EKS would require `EFS | FSx`; AKS would
require `AzureFiles | NetAppFiles`. The enum exploded per provider and leaked
cloud terminology into the API.

The chosen shape — `cluster.<source>.cache.storageClassName` as a single
optional string per provider sub-block — captures the same intent ("use my
backend, not yours") without enumerating cloud products.

### Cache size envelope on InferenceCluster

I considered a `cacheCapacityGiB` (or similar) field at the cluster level
for platform teams to declare total cache budget. Storage choice on
Filestore-style backends correlates with size — bigger tier, more capacity
envelope, more cost. Putting capacity on the cluster meant the cluster had to
commit to a size envelope before any ModelCache existed.

Capacity is per-cache, owned by the ML team, and declared inside each source
that uses central storage (e.g. `ModelCache.spec.source.huggingFace.sizeGiB`).
Cluster-level cache infrastructure scales to absorb caches as they're
created (multishare on GKE; admin's allocation elsewhere). Platform teams
don't have to predict ML team capacity needs upfront.

### Configurable mount path

I considered an optional `ModelCache.spec.mountPath` field defaulted to
`/mnt/models`, letting users override for unusual cases (multi-cache pods,
engine-specific paths, org conventions). None of those are v0.1 needs, and
hardcoding the path mirrors the `engine` container name convention — strong
opinionation pays predictability dividends. If a real use case emerges,
adding the field back is additive.
