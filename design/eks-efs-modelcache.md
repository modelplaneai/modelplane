# Auto-provision EFS RWX storage for ModelCache on EKS

**Status:** Draft
**Date:** June 2026
**Author:** Dennis Ramdass
**Issue:** [#114](https://github.com/modelplaneai/modelplane/issues/114) (towards #66)

## Summary

On GKE, Modelplane provisions the `modelplane-rwx` Filestore StorageClass with no
admin setup, so a `ModelCache` PVC binds out of the box. On EKS, RWX storage is
bring-your-own today: the admin must install the EFS CSI driver, create an EFS
filesystem with mount targets and a security group, wire IAM, and create the
`modelplane-rwx-efs` StorageClass. This brings EKS to parity by auto-provisioning
that EFS substrate, so a `ModelCache` on EKS "just works."

## Goals

- A `ModelCache` on an EKS-backed `InferenceCluster` binds its RWX PVC with no
  admin setup, mirroring the GKE experience.
- The EFS substrate is provisioned per EKS cluster, unconditionally.

## Non-goals

- **FSx for Lustre** as an alternative high-throughput backend (the issue's future
  note) — not built here.
- **Configurable EFS throughput mode** — Elastic is hardcoded; not exposed on the
  API yet.

## Architecture

The work mirrors the GKE Filestore split across the same two composition
functions, plus one new provider dependency.

- **`compose-eks-cluster`** provisions the AWS-side EFS infrastructure and
  surfaces the resulting filesystem ID in `EKSCluster.status` — the same shape
  `compose-gke-cluster` uses to surface the VPC name for the Filestore
  StorageClass.
- **`compose-inference-cluster`** reads that filesystem ID and composes the
  `modelplane-rwx-efs` StorageClass on the workload cluster (a provider-kubernetes
  `Object`), gated on the ID being present — a sibling of its existing
  `compose_rwx_storage_class`.
- **`compose-model-cache`** is unchanged: EKS PVCs already default to
  `modelplane-rwx-efs` (`_DEFAULT_STORAGE_CLASS`), which now exists.

The EFS substrate is provisioned **unconditionally per EKS cluster**, like GKE
enabling the Filestore API. An empty EFS filesystem has negligible standing cost,
and gating provisioning on "a cache exists" would create a chicken-and-egg with
the StorageClass (the cache needs the class, which needs the filesystem).

## Component 1 — `compose-eks-cluster`: the EFS substrate

A new `compose_efs()` adds the following, all in the cluster's VPC:

- **EFS `FileSystem`** — `throughputMode: elastic` (auto-scales to demand, never
  throttles a cold-cache hydration or a burst of pods reading weights),
  `encrypted: true`.
- **`MountTarget` per node subnet** — one per AZ the nodes run in, each
  referencing the EFS security group below, so any node can mount the filesystem.
- **ec2 `SecurityGroup` + `SecurityGroupIngressRule`** — NFS (2049) ingress from
  the cluster's node security group.
- **IAM `Role` + `RolePolicyAttachment`** (`AmazonEFSCSIDriverPolicy`), with a
  trust policy for `pods.eks.amazonaws.com` (Pod Identity).
- **`eks-pod-identity-agent` Addon** (the Pod Identity prerequisite) and a
  **`PodIdentityAssociation`** mapping `kube-system/efs-csi-controller-sa` to that
  role.
- **`aws-efs-csi-driver` Addon**.

`status.efsFileSystemId` is surfaced from the `FileSystem` MR's external-name
annotation (set by the provider once the filesystem exists), the same way
`compose-gke-cluster` reads the composed network name.

### IAM: Pod Identity, not OIDC/IRSA

The EFS CSI driver gets its role through **EKS Pod Identity** — the
`eks-pod-identity-agent` addon plus a `PodIdentityAssociation` — rather than
classic IRSA. Pod Identity needs no OpenID Connect provider, thumbprint, or
web-identity trust policy, and `compose-eks-cluster` has no OIDC plumbing today,
so IRSA would be entirely net-new. Pod Identity is AWS's current recommended
mechanism, both models are already available, and it leaves a reusable Pod
Identity foundation for future addons.

## Component 2 — `compose-inference-cluster`: the StorageClass

A `compose_efs_storage_class()` mirroring `compose_rwx_storage_class`: when the
EKS cache uses the managed default (`modelplane-rwx-efs`) and the EKS cluster's
`status.efsFileSystemId` is present, compose a provider-kubernetes `Object`:

```yaml
provisioner: efs.csi.aws.com
parameters:
  provisioningMode: efs-ap        # dynamic access point per PVC
  fileSystemId: <from EKS status>
  directoryPerms: "700"
volumeBindingMode: Immediate
allowVolumeExpansion: false        # EFS access points don't resize
```

Readiness uses `SuccessfulCreate` (StorageClasses have no Ready condition, so
`DeriveFromObject` would hang), and the resource is gated on the filesystem ID —
the same pattern as the Filestore class. An admin-provided `storageClassName`
(anything other than `modelplane-rwx-efs`) is left unmanaged.

## API changes

- **`EKSCluster` XRD:** add `status.efsFileSystemId` (string, optional; populated
  once the filesystem is created).
- **`inferenceclusters` XRD:** no change — the EKS cache `storageClassName`
  already defaults to `modelplane-rwx-efs`.

## Dependencies

- **New:** `provider-aws-efs` (v2.5.0, matching the other AWS provider families),
  added to `crossplane-project.yaml`, followed by a schema regen. It supplies the
  `FileSystem` and `MountTarget` models. The security group, IAM, and EKS
  resources use providers already present (`provider-aws-ec2`, `-iam`, `-eks`).

## Teardown / lifecycle

Crossplane orders deletion by managed-resource references, so the natural chain
holds without extra machinery:

- The StorageClass (and any PVCs) tear down first.
- `MountTarget` → `FileSystem` references ensure mount targets delete before the
  filesystem (EFS refuses to delete a filesystem with live mount targets).
- The security group deletes after the mount targets that use it.
- Pod Identity association, addons, and IAM tear down independently.

If a deletion-ordering gap surfaces during implementation, a `Usage` resource
(the pattern already used for the gateway/CRD ordering) is the fallback. The
implementation plan will confirm whether one is needed.

## Testing

Case-table additions to both functions' `test_fn.py`:

- **`compose-eks-cluster`:** a matched cluster composes the full EFS set
  (filesystem, mount targets, SG + ingress rule, IAM role + attachment, Pod
  Identity agent addon + association, EFS CSI addon) and surfaces
  `status.efsFileSystemId` once the `FileSystem` MR reports its external name.
- **`compose-inference-cluster`:** composes the `modelplane-rwx-efs` StorageClass
  when the EKS cluster reports a filesystem ID; skips it before the ID is present
  and for an admin-provided `storageClassName`.

## Key decisions

| Decision | Choice | Why |
|---|---|---|
| CSI driver IAM | EKS Pod Identity | No OIDC provider/thumbprint; AWS-recommended; reusable foundation |
| EFS throughput | Elastic | Never throttles bursty model loads; ~free when idle |
| Provisioning trigger | Unconditional per EKS cluster | Parity with GKE; avoids StorageClass chicken-and-egg; empty EFS ~free |
| Throughput configurability | Hardcoded (non-goal) | Keep v0.1 API surface small |
