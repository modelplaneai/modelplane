# ModelCache sources and the mount contract

**Status:** Draft
**Date:** October 2026
**Author:** Dennis Ramdass
**Issues:** [#512](https://github.com/modelplaneai/modelplane/issues/512), [#495](https://github.com/modelplaneai/modelplane/issues/495), [#210](https://github.com/modelplaneai/modelplane/issues/210)

## Summary

A `ModelCache` reads only from HuggingFace, and it stages what it reads onto a
ReadWriteMany claim on each cluster. On some clouds that claim is slow, costly
at small sizes, or fails to bind. `compose-model-replica`, the function that
mounts the cache into engine pods, works out the claim's name itself, and it
doesn't wait for the cache. An engine can start before the weights are there,
and download them a second time.

I propose two changes:

- `spec.source` gets two new values. `OCI` reads the model from a registry with
  no claim. `PersistentVolumeClaim` mounts a claim the user already filled.
- Each cluster entry in `status.clusters[]` gets a `mount` field. It says how to
  read the model on that cluster. `compose-model-replica` copies it into the
  pod, and waits until it's there.

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

### Mounting the cache

A cluster entry has a `phase` and a `message`, but not the claim's name.
`compose-model-replica` computes the name in `cache_pvc_name`, and
`compose-model-cache` keeps a second copy of the same code. The name is only
right for a `HuggingFace` source.

No function checks whether the model is readable.
`compose-model-deployment` checks that a cluster has a storage class, not that
the cache staged onto it. This causes these faults:

- [#495](https://github.com/modelplaneai/modelplane/issues/495): an engine
  started while its 464 GB cache was staging. It downloaded the model again, in
  parallel.
- [#498](https://github.com/modelplaneai/modelplane/issues/498): a deployment's
  status doesn't say why a model doesn't serve. The faults above each needed a
  person with access to the workload cluster.

## Goals

**Read the model where it is.** A user can serve a model from an OCI registry or
from a claim they filled.

**Don't stage what a node can pull.** An `OCI` source doesn't use a Modelplane
claim.

**Give each cluster one answer.** A cluster entry says how to read the model
there, and `compose-model-replica` copies that answer instead of computing it.

**Wait for the model.** Modelplane composes no engine pod on a cluster until
that cluster's entry has a `mount`.

**Say why a cluster can't read the model.** The cluster entry gives the reason.

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

### The mount contract

Each cluster entry gets a `mount` field. Each item uses some of the fields of
the Kubernetes type in its comment:

```yaml
mount:
  volumes: []           # corev1.Volume: persistentVolumeClaim, image, or csi
  volumeMounts: []      # corev1.VolumeMount: name, mountPath, subPath, readOnly
  env: []               # corev1.EnvVar: name, value
  imagePullSecrets: []  # corev1.LocalObjectReference
```

`OCI` and `PersistentVolumeClaim` put the model at `/mnt/models`, and `env` is
empty. A `HuggingFace` entry puts the hub cache root at `/mnt/models` and sets
`HF_HUB_CACHE`. The engine then finds the staged snapshot by repository ID.

`compose-model-cache` knows the source, so it writes the contract. Each cluster
entry comes from that cluster's own observed resources, so a failed cluster
doesn't change another cluster's entry.

`compose-model-replica` reads the `ModelCache` as a required resource, and
builds the pod without knowing the source. It follows these rules:

- It finds the entry for its own cluster.
- It adds `imagePullSecrets` to the pod.
- It adds `env` before the user's environment. A user's variable with the same
  name then takes effect. It adds `volumes` and `volumeMounts` as they are.
- If a mount is read-only, it adds an `emptyDir` with a size limit and points
  `HOME` at it. Engines write compile and download caches under `HOME`, and the
  size limit stops a large one from getting the pod evicted.
- If the cluster has no entry, or the entry has no `mount`, it sets a condition
  and composes no workload. This fixes #495.

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

**Cache metrics.** Pull time and each cluster entry's phase would have shown the
stuck cache in #494. Cache state is on the control plane, so it waits on #476.

**Pod faults in the cluster entry.** #498 asks the control plane to show why a
model doesn't serve. The contract covers the model not being readable. A
container's exit code needs another data source.

## Alternatives considered

### A child resource for each cluster

#210 asks for a `ModelCacheHydration` child for each cluster. The contract
needs one place for each cluster, and `status.clusters[]` already gives it.

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
