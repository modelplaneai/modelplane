# ModelCache sources

**Status:** Draft
**Date:** October 2026
**Author:** Dennis Ramdass
**Issues:** [#512](https://github.com/modelplaneai/modelplane/issues/512)

## Summary

A `ModelCache` reads only from HuggingFace, and it stages what it reads onto a
ReadWriteMany claim on each cluster. On some clouds that claim is slow, costly
at small sizes, or fails to bind.

I propose that `spec.source` get two new values. `OCI` reads the model from a
registry with no claim. `PersistentVolumeClaim` mounts a claim the user already
filled. Each source writes the `mount` that
[the mount contract](./modelcache-mount-contract.md) adds to each cluster
entry, and replicas copy it as they do for `HuggingFace`.

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelCache
metadata:
  name: qwen3-8b
  namespace: ml-team
spec:
  source: OCI
  oci:
    ref: us-docker.pkg.dev/ml-team/models/qwen3-8b@sha256:<digest>
```

## Background

### One source

`spec.source` has one value, `HuggingFace`. This forces two kinds of user to
work around Modelplane:

- A user whose models are in a registry, like Artifact Registry or ECR, must
  copy them to HuggingFace first.
- A user whose weights are already on a claim can't say so. A fleet with no
  internet connection has no other option.

A registry can hold weights in two forms:

- A **container image**. The kubelet can mount it as an image volume.
- A **model artifact**, in the [ModelPack](https://modelpack.org/) format. The
  kubelet can't mount it, so the
  [model CSI driver](https://github.com/modelpack/model-csi-driver) mounts it.

### The ReadWriteMany claim

Every pod of a multi-node gang reads the same weights, so the claim must be
ReadWriteMany. On GKE that's Filestore, and on EKS it's EFS. These reports show
the cost:

| Issue | What it shows |
|---|---|
| [#204](https://github.com/modelplaneai/modelplane/issues/204) | A slow load from an EFS-backed cache. |
| [#414](https://github.com/modelplaneai/modelplane/issues/414) | `sizeGiB` has no effect below 1 TiB on GKE, because the storage class is Filestore Enterprise. |
| [#383](https://github.com/modelplaneai/modelplane/issues/383) | No measurement says what a ReadWriteMany backend must deliver. |
| [#494](https://github.com/modelplaneai/modelplane/issues/494) | A claim was Pending after the cloud replaced its node. The cache said `Hydrating` for 40 minutes. |
| [#221](https://github.com/modelplaneai/modelplane/issues/221) | A claim never bound, because the cluster didn't enable the Filestore CSI driver. |

An `OCI` source doesn't use a ReadWriteMany claim. The node pulls the model to
its own disk.

## Goals

**Read the model where it is.** A user can serve a model from an OCI registry or
from a claim they filled.

**Don't stage what a node can pull.** An `OCI` source doesn't use a Modelplane
claim.

**Mount every source the same way.** Each source writes the cluster entry's
`mount`, so replicas don't need code for a new source.

This design doesn't cover:

- Finding two caches with the same model.
- Copying a model from one cluster to another.

## Proposal

### Sources

```yaml
spec:
  source: OCI          # HuggingFace, OCI, or PersistentVolumeClaim
  huggingFace: {}      # unchanged
  oci:
    ref: ""            # a tag or a digest
    subPath: ""        # the directory that holds the weights
    imagePullSecrets: []  # secrets on each cluster, for a private registry
  persistentVolumeClaim:
    claimName: ""      # a claim in the replica's namespace on each cluster
    subPath: ""
```

| Source | The user gives | What mounts it | What the cluster needs |
|---|---|---|---|
| `HuggingFace` | A repository ID | A claim that Modelplane stages | A ReadWriteMany storage class |
| `OCI` | A container image that holds the weights | An `image` volume | Kubernetes 1.35 or later, and containerd 2.1 or later or CRI-O |
| `OCI` | A model artifact | A `csi` volume from the model CSI driver | The driver, which Modelplane installs on every cluster |
| `PersistentVolumeClaim` | A claim the user filled | A `persistentVolumeClaim` volume | The claim, bound on the cluster. A multi-node gang needs ReadWriteMany or ReadOnlyMany. |

For an `OCI` source:

- On each cluster, a resolver Job reads the reference's manifest. The Job writes
  the digest and the manifest's media type to a ConfigMap, and Modelplane
  observes the ConfigMap, as it observes the HuggingFace staging Job.
- The media type tells Modelplane which kind of artifact it is. The user doesn't
  say. Any other kind of artifact fails the cluster entry, and the message gives
  the media type.
- Modelplane resolves a tag once and stores the digest in the cluster entry. It
  writes the digest into `mount`, and resolves again only when the spec changes.
  Each cluster resolves a moving tag on its own, possibly to a different digest.
  A digest in `ref` pins every cluster to one model.
- A private registry needs `oci.imagePullSecrets`. The resolver Job reads the
  manifest with them, and `mount` lists them for the engine pod. A public
  registry needs none.
- The kubelet pulls the reference when the pod starts, and shares the pulled
  bytes with later pods on the node.

For a `PersistentVolumeClaim` source, the claim must be in the namespace where
the replica's pods run. A claim that isn't there yet leaves the cluster entry
`Pending`, with a message.

Only `HuggingFace` stages onto a Modelplane claim, so only `HuggingFace` uses
`sizeGiB` and a cluster's `status.cache.storageClassName`. `compose-model-cache`
and `compose-model-deployment` don't require a storage class for `OCI` and
`PersistentVolumeClaim`.

Modelplane installs the model CSI driver on every cluster.

### Mounts

Each source writes the cluster entry's `mount`, as
[the mount contract](./modelcache-mount-contract.md) describes for
`HuggingFace`. `OCI` and `PersistentVolumeClaim` put the model at
`/mnt/models`, and don't set environment variables. They also extend the
contract:

- `mount` gets `imagePullSecrets`, a list of `corev1.LocalObjectReference`.
  `compose-model-replica` adds them to the pod, for an `OCI` source in a
  private registry.
- If a volume mount sets `readOnly`, `compose-model-replica` adds an `emptyDir` with a
  size limit and points `HOME` at it. Engines write compile and download caches
  under `HOME`, and the size limit stops a large one from getting the pod
  evicted.

### Cluster status

`InferenceCluster.status.cache` gets an `imageVolumes` boolean, next to
`storageClassName`. Modelplane sets it from the cluster's observed server
version and its nodes' container runtime. It says whether the cluster can mount
an image volume.

It only matters for container images. A model artifact uses the CSI driver on
any version. A container image on a cluster that can't mount it fails the
cluster entry, and the message gives the reason.

ModelExpress applies only to `HuggingFace` caches. An `OCI` pull is already
local to each node.

### Examples

#### OCI

The `qwen3-8b` cache from the summary gives this cluster entry on a cluster
that can mount image volumes. An image volume is always read-only:

```yaml
status:
  clusters:
  - name: gke-us-central1
    phase: Ready
    mount:
      volumes:
      - name: model-cache
        image:
          reference: us-docker.pkg.dev/ml-team/models/qwen3-8b@sha256:<digest>
          pullPolicy: IfNotPresent
      volumeMounts:
      - name: model-cache
        mountPath: /mnt/models
        readOnly: true
```

#### PersistentVolumeClaim

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: ModelCache
metadata:
  name: kimi-k2
  namespace: ml-team
spec:
  source: PersistentVolumeClaim
  persistentVolumeClaim:
    claimName: kimi-k2-weights
    subPath: Kimi-K2-Instruct
```

```yaml
status:
  clusters:
  - name: nebius-eu-north1
    phase: Ready
    mount:
      volumes:
      - name: model-cache
        persistentVolumeClaim:
          claimName: kimi-k2-weights
      volumeMounts:
      - name: model-cache
        mountPath: /mnt/models
        subPath: Kimi-K2-Instruct
  - name: gke-us-central1
    phase: Pending
    message: persistentvolumeclaim "kimi-k2-weights" not found
```

### Limits

- **A Ready cache can still hold the wrong model.** Resolving the reference
  finds a missing repository, a bad pull secret, or an unknown artifact kind. It
  can't tell that the artifact is a different model, or that the claim behind a
  `PersistentVolumeClaim` source is incomplete. Those fail when the engine
  starts.
- **An `OCI` source uses the node's disk.** The CSI driver keeps artifacts under
  `/var/lib/model-csi`. Image volumes go to the image store, under the kubelet's
  `imagefs` eviction limits. `sizeGiB` controls neither. A pool's disk must fit
  its largest model.
- **A private registry needs a pull secret.** The resolver Job can't use the
  node's identity, so a private ECR, Artifact Registry, or ACR repository needs
  `oci.imagePullSecrets` too.
- **The CSI driver is another component.** It's a DaemonSet to install and
  upgrade on every cluster, and its registry credentials are static. The driver
  must also stop after its pods, or a pod stays `Terminating`, as in #499.
  Container images avoid these costs.

## Future improvements

**Copy to the node.** An init container could copy the model to the node's disk
through a `hostPath` volume. That avoids the ReadWriteMany read for
`HuggingFace`, but it puts each model on each node, so it needs size limits and
eviction first.

**Several caches per deployment.**
[#281](https://github.com/modelplaneai/modelplane/issues/281) asks for a draft
model or tokenizer from a second cache. Each cache would then need its own
volume name and its own path, where `OCI` and `PersistentVolumeClaim` use
`/mnt/models` here. The mount's shape already allows that, because replicas
copy whatever the cache publishes.

**Cache metrics.** Pull time and each cluster entry's phase would have shown the
stuck cache in #494. Cache state is on the control plane, so it waits on #476.

**Pod faults in the cluster entry.** #498 asks the control plane to show why a
model doesn't serve. The contract covers the model not being readable. A
container's exit code needs another data source.

## Alternatives considered

### One enum value for each kind of artifact

`OCIImage` and `OCIArtifact` would make the user say which kind the publisher
made. Users often don't know. Modelplane can read the manifest and find out,
and a wrong guess would only fail the cluster entry.

### Install the CSI driver only where it's needed

Installing the driver only on clusters that serve a model artifact would save an
idle DaemonSet. But a composition function sees one cluster, not the fleet's
caches, and a missing driver means a pod that never starts.

### CRI-O's built-in artifact mounts

CRI-O can mount OCI artifacts as image volumes, with no driver. Modelplane
provisions no CRI-O clusters, and a second mount path adds another branch to
test. The driver stops being needed once containerd adds the same feature.
