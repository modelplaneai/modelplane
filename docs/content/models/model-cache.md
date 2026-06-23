---
title: Cache Model Weights
weight: 30
description: Stage model weights on cluster storage before serving.
---
<!-- vale write-good.Passive = NO -->
**API:** [`modelplane.ai/v1alpha1` · ModelCache]({{< ref "/reference/modelcaches" >}})

A `ModelCache` stages a model's weights on shared workload-cluster storage,
fetched once from the configured source rather than downloaded again on every pod
start. `ModelDeployments` reference a cache via `spec.modelCacheRef.name`, and
Modelplane mounts it at `/mnt/models` in every serving pod, shared across the
pods of a multi-node engine. The engine reads weights locally from the mount.

`ModelCache` is recommended for multi-node deployments and optional for
single-node cold-start optimization.

## What to cache

The required `source` enum names the kind, with the matching source object set
alongside it. Setting `source: HuggingFace` selects `spec.huggingFace`, which
carries the `repo` to fetch, an optional `revision` (branch, tag, or commit), and
`sizeGiB`, how much storage the weights get on each cluster. Size it to the
model, since a value below the model's size leaves no room to stage the weights.
`HuggingFace` is the only source today.

The cache mounts at `/mnt/models` on every consuming pod, so the engine's args
reference that path (`--model=/mnt/models` for vLLM) rather than the source.

## Authenticating

A gated or private model needs a credential to fetch. When a cache stages the
weights, the credential lives on the cache: set `authSecret` to name a Secret in
the cache's namespace, and Modelplane propagates it to every cluster the cache
stages to, for the hydration to read.

Create the Secret once on the control plane, then reference it:

```bash
kubectl create secret generic hf-token \
  --namespace ml-team \
  --from-literal=HF_TOKEN=hf_xxxxxxxx
```

```yaml {nocopy=true}
spec:
  source: HuggingFace
  huggingFace:
    repo: Qwen/Qwen3-Coder-480B-A35B-Instruct
    authSecret:
      name: hf-token         # a Secret in this ModelCache's namespace
      key: HF_TOKEN          # defaults to HF_TOKEN
    sizeGiB: 1100
```

Without a cache, the engine fetches the model itself at startup, so the
credential goes on the `ModelDeployment` instead, as `HF_TOKEN` in the engine
container's `env`.

## Where to cache

An optional `clusterSelector` scopes where the cache is staged. Omitting it
stages the cache on every cluster in the fleet; setting `matchLabels` restricts
it to clusters carrying those labels. A `ModelDeployment` that references the cache
places *new* replicas only onto clusters within this footprint, so narrowing the
selector also narrows where replicas can land: a replica never schedules to a
cluster the cache didn't stage to. Replicas already running are left where they
are.

## Loading from cache

A cache only pays off if the engine reads from it quickly. With its default
loader an engine can read a large model from shared storage slowly enough that
the cache makes cold starts *worse* than fetching the model directly, since you
pay to hydrate the cache and then wait on a slow read. Choose a fast loader with
your engine flags.

For vLLM on EKS, `--load-format=runai_streamer` reads from the EFS-backed cache
dramatically faster than the default loader (minutes rather than tens of
minutes for a large model), tuned further with `--model-loader-extra-config`:

```yaml {nocopy=true}
args:
- --model=/mnt/models
- --load-format=runai_streamer
- --model-loader-extra-config={"concurrency":16,"distributed":true}
```

The right loader and settings depend on the engine and the storage backend, so
treat these as a starting point and measure your own cold-start time. The
[Kimi-K2 example]({{< ref "/examples/kimi-k2" >}}) uses this configuration end to
end.

## Storage prerequisites

<!-- vale Google.Acronyms = NO -->
The cache PVC needs a `ReadWriteMany` (RWX) StorageClass on the workload cluster.
What the platform admin must set up depends on the cloud:
<!-- vale Google.Acronyms = YES -->

- **GKE** and **EKS:** auto-provisioned. Nothing for the admin to do.
- **Existing:** the admin sets up a `ReadWriteMany` StorageClass on the cluster.

Either way, your `ModelCache` and `ModelDeployment` specs are the same. How
storage is provided on each cluster source, and how to bring your own backend, is
covered in [Register a Cluster]({{< ref "/platform/inference-cluster.md#cache-storage" >}}).

## Example

{{< manifests "concepts/model-cache.yaml" >}}
<!-- vale write-good.Passive = YES -->
