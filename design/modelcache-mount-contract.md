# ModelCache mount contract

**Status:** Draft
**Date:** October 2026
**Author:** Dennis Ramdass

This document proposes that each `ModelCache` say how to read its model on each
cluster, and that a `ModelReplica` wait until it does. It builds on
[modelcache.md](./modelcache.md).

## Background

A `ModelCache` stages a HuggingFace model onto a ReadWriteMany claim on each
cluster it matches, with a hydration Job. A `ModelDeployment` that references
the cache mounts that claim into its engine pods at `/mnt/models`.

`compose-model-replica` works out the claim's name itself, with a copy of the
code `compose-model-cache` uses to name it. It mounts the claim as soon as the
scheduler places the replica, and it can't tell whether the model is there yet.

This caused these faults:

- An engine started while its 464 GB cache was still staging. It found no
  model on the claim, and downloaded the whole model again in parallel with the
  Job ([#495](https://github.com/modelplaneai/modelplane/issues/495)).
- A claim waited on a node the cloud had replaced. The cache said `Pending`
  for 40 minutes, and nothing on the control plane said why
  ([#494](https://github.com/modelplaneai/modelplane/issues/494)).

## Goals

- Give each cluster one answer about how to read the model there, which a
  replica copies instead of computing.
- Don't start an engine on a cluster until the model is readable there.
- Say why a cluster can't read the model yet.
- Keep a serving replica's engines running when its cache changes.

It's not a goal to add sources other than HuggingFace. That's
[modelcache-sources.md](https://github.com/modelplaneai/modelplane/pull/505).

## Proposal

### The mount

Each entry in `status.clusters[]` gets a `mount` field, set while the entry's
phase is `Ready`. Each item uses some of the fields of the Kubernetes type in
its comment:

```yaml
status:
  clusters:
  - name: cluster-a
    phase: Ready
    mount:
      volumes:        # corev1.Volume: name, persistentVolumeClaim
      - name: model-cache
        persistentVolumeClaim:
          claimName: modelcache-ml-team-qwen-17db2
      volumeMounts:   # corev1.VolumeMount: name, mountPath, readOnly
      - name: model-cache
        mountPath: /mnt/models
      env:            # corev1.EnvVar: name, value
      - name: HF_HUB_CACHE
        value: /mnt/models
```

`compose-model-cache` knows the source and the claim, so it writes the mount.
For a `HuggingFace` source it's the claim at `/mnt/models`, and `HF_HUB_CACHE`
pointing there. An engine then finds the staged snapshot by its repo ID. These
match the volume, mount and variable that `compose-model-replica` composes
today, so running engines don't restart when you upgrade Modelplane.

### Waiting for the mount

`compose-model-replica` requires the `ModelCache` and reads the entry for its
own cluster. It adds the mount's volumes to each engine pod, and its volume
mounts and variables to each engine container. A variable the engine sets
itself comes later in the list, so it takes effect.

Until the entry has a mount, a new replica composes nothing. Its
`ModelAccepted` condition is `False`, with reason `WaitingForCache` and a
message that says what it waits for. The message comes from the cache's entry,
or says the cache doesn't stage to the replica's cluster, or that the cache
doesn't exist.

A replica whose engines already run doesn't wait. Until its entry has a mount,
it keeps the cache volume, volume mount and `HF_HUB_CACHE` its workload has
now, or no cache if it has none, and warns. It reads them from the workload it
composed last time, and its engines don't restart when the claim disappears,
when someone deletes the cache, or when someone adds or switches to a cache
that isn't ready. Stopping a serving model because its cache changed would
turn a storage problem into an outage, and moving it onto a cache that's still
downloading would start it without its model.

### Messages

`compose-model-cache` writes a `message` for each entry that isn't `Ready`:

| Phase | Message gives |
|---|---|
| `Pending` | The claim it waits on, and the node the scheduler chose for it, from the claim's `volume.kubernetes.io/selected-node` annotation. |
| Any phase but `Ready`, with no token | The Secret and key the hydration Job needs. |
| `Hydrating` | The repository and the claim it downloads into. |
| `Failed` | The Job, how many attempts failed, and the Job's own message. |

## Future improvements

**Other sources.** A source that reads the model from a registry or from a
claim the user filled writes its own mount. Replicas copy whatever the cache
publishes, so they don't need to change for it.

**Several caches per deployment.**
[#281](https://github.com/modelplaneai/modelplane/issues/281) asks for a draft
model or tokenizer from a second cache. Each cache would then need its own
volume name and its own path. The mount's shape already allows that, because
replicas copy whatever the cache publishes.

## Alternatives considered

### Compute the mount in the replica

The replica could keep naming the claim and check the cache's phase. That keeps
two copies of the naming code in step, and every new source would add a branch
to both functions.

### A child resource for each cluster

[#210](https://github.com/modelplaneai/modelplane/issues/210) asks for a
`ModelCacheHydration` child for each cluster. The mount needs one place for each
cluster, and `status.clusters[]` already gives it.

### Hold the replica's last mount in its own status

A running replica could keep the mount it last used in its own status, and
reuse it when the cache stops publishing one. The workload the replica composed
already holds that mount, and a second copy in status would have to match it.

### Derive the mount from the replica's reference

A running replica could derive the mount a `HuggingFace` cache would publish
from its own `modelCacheRef`. That restarts the engines onto a cache that isn't
ready when someone adds a cache or switches to another one.
