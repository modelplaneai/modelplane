---
title: Register a Cluster
weight: 30
description: A Kubernetes cluster registered with Modelplane for model serving.
---
**API:** [`modelplane.ai/v1alpha1` · InferenceCluster]({{< ref "/reference/inferenceclusters" >}})
<!-- vale write-good.Passive = NO -->
An `InferenceCluster` represents a Kubernetes cluster configured for model
serving. Platform teams create these to provide GPU capacity.


Each cluster has:

- A **cluster source**: `GKE`, `EKS`, `AKS`, `Nebius`, `Vultr` or `Civo` (Modelplane
  provisions the full cluster) or `Existing` (bring a cluster you manage yourself). See
  [Supported Providers]({{< ref "platform/providers.md" >}}) for the clouds and
  neoclouds Modelplane runs on.
- One or more **node pools**, each referencing an `InferenceClass` for its
  hardware capabilities and provisioning recipe.
- **Labels** for organizational metadata: tier, region, provider. These are the
  matching surface for `ModelDeployment.clusterSelector`.

Modelplane installs a serving stack on every cluster it manages, including
existing clusters, which it assumes are solely for its use.

## Ownership and requirements

Modelplane assumes exclusive ownership of every `InferenceCluster`. The fleet
scheduler's capacity accounting relies on Modelplane being the only thing placing
GPU workloads on the cluster, so dedicate each cluster to Modelplane rather than
sharing it with other workloads.

Modelplane also expects the cluster to provide a recent Kubernetes version and
GPUs exposed through Dynamic Resource Allocation. On a provisioned cluster
Modelplane meets these requirements for you. On an existing cluster you meet them
yourself, as [Requirements for an existing
cluster](#requirements-for-an-existing-cluster) sets out.

## Provisioned and existing clusters

The `cluster.source` discriminator picks one of two models:

- **Provisioned (`GKE`, `EKS`, `AKS`, `Nebius`, `Vultr`, `Civo`).** Modelplane creates the cluster and its GPU node
  pools from each pool's `InferenceClass`, labels the pool's nodes so the
  scheduler's placement is enforced, and provisions the storage class for model
  weights. It also injects a non-GPU **system pool** with opinionated defaults to
  run the inference stack, so you only declare the GPU pools you want.
- **Existing (`Existing`).** A kubeconfig `Secret` provides access to a cluster
  you run yourself. Modelplane installs the serving stack it needs but doesn't
  provision infrastructure, and each pool's `InferenceClass` provides hardware
  capabilities for scheduling only. You're responsible for the cluster meeting
  [Modelplane's requirements](#requirements-for-an-existing-cluster).

## Requirements for an existing cluster

Modelplane installs a device stack for each DRA driver a pool's
`InferenceClass` claims: NVIDIA's DRA driver for `gpu.nvidia.com`, the AMD GPU
operator in DRA mode for `gpu.amd.com`, and `dra-driver-cpu` for `dra.cpu`. The
AMD stack installs only on an existing cluster. Beyond that, an existing cluster
must meet what Modelplane would otherwise set up for you:

- **Kubernetes 1.34.2 or newer, or 1.36 for a CPU pool.** Modelplane binds
  devices with the generally available Dynamic Resource Allocation API
  (`resource.k8s.io/v1`), first served in Kubernetes 1.34, and NVIDIA's DRA
  driver wants 1.34.2. A CPU pool's cores are claimed as consumable capacity,
  which Kubernetes enables by default from 1.36. Prefer a [maintained
  release](https://kubernetes.io/releases/).
- **NVIDIA GPU nodes that already run the NVIDIA driver.** Modelplane installs
  NVIDIA's DRA driver, but not the GPU driver itself. Its
  [prerequisites](https://dra-driver-nvidia-gpu.sigs.k8s.io/docs/prerequisites/)
  call for NVIDIA GPU driver v565 or newer and NVIDIA Container Toolkit v1.18.0
  or newer, which provides the CDI the driver uses to expose GPUs to the
  container runtime. A provisioned cluster's node image includes this.
- **AMD GPU nodes that already run the amdgpu kernel driver.** The AMD GPU
  operator Modelplane installs has its driver management turned off.
- **A `modelplane.ai/cpu=true` label on CPU pool nodes.** `dra-driver-cpu` runs
  only on nodes with that label, so without it a CPU pool doesn't publish its
  cores.
- **A `modelplane.ai/pool=<pool-name>` label on each pool's nodes**, matching the
  pool's `name`. Modelplane provisions no nodes here, so you apply the label
  yourself. The [scheduler pins each pool's pods to
  it]({{< ref "/architecture/scheduling.md#pinning-placement-to-a-pool" >}}), so
  worker pods stay Pending without it.
- **The `nvidia.com/gpu`, `amd.com/gpu` or `modelplane.ai/cpu` taint key, if you
  taint pool nodes.** Modelplane's engine pods tolerate those keys, and a
  different taint keeps them off the nodes. The AMD operator's per-node
  components tolerate only `amd.com/gpu` and `dra-driver-cpu` only
  `modelplane.ai/cpu`, so taint AMD and CPU nodes with their own key or not at
  all.
- **No AMD GPU operator or `dra-driver-cpu` of your own** for a driver a class
  claims. Modelplane installs its own, which would run a second copy beside
  yours.
- **A load balancer.** Modelplane exposes the cluster's serving gateway through a
  `LoadBalancer` Service. The cluster needs a load balancer that assigns the
  Service an external address.
- **No conflicting Gateway controller.** Modelplane installs Envoy Gateway and
  owns its `GatewayClass`. Don't run another controller claiming the same class.
- **A `ReadWriteMany` StorageClass**, if you use a `ModelCache`. See
  [Cache storage](#cache-storage).
- **Any multi-node fabric you need.** For multi-node serving you provide and
  configure the RDMA or InfiniBand fabric and its drivers. Modelplane installs
  those only on the clouds it provisions.
- **A DCGM exporter, if you want NVIDIA GPU metrics.** Modelplane doesn't
  install one here because your cluster may already run one, as a GPU Operator
  install does by default, and a second exporter would collect every GPU twice.
  Without one, NVIDIA pools' `modelplane_gpu_*` series are empty. The AMD stack
  brings its own exporter. [GPUs]({{< ref "/platform/telemetry.md#gpus" >}})
  describes how the collector discovers the exporter, the DCGM fields it
  needs, and how to check what yours publishes.

## Serving stack

`spec.stack` selects the serving layer the cluster runs: `Standard` (the default)
or `Dynamo`. The field is immutable, so recreate the cluster to change it.

Standard composes a single-node engine as a Deployment and a multi-node gang as
a LeaderWorkerSet. An endpoint picker selects an engine for each request
through an InferencePool.

Dynamo installs the Dynamo operator, [Grove](https://github.com/ai-dynamo/grove),
the [KAI Scheduler](https://github.com/NVIDIA/KAI-Scheduler), and
[ModelExpress](https://github.com/ai-dynamo/modelexpress). Modelplane composes one
`DynamoGraphDeployment` per `ModelReplica`, including a shared frontend and a
component for every engine. The operator owns runtime discovery, workloads,
probes, and Grove gang scheduling. Modelplane owns device claims and placement.

Dynamo engines must use a Dynamo runtime image and launch `dynamo.vllm`,
`dynamo.sglang`, or `dynamo.trtllm`. All engines in one graph use the same backend.
Explicit commands, arguments, environment, and existing sidecars pass through.
When command is omitted, Modelplane supplies `python3 -m dynamo.<backend>`;
the member must provide its runtime arguments. An image without command or
arguments is rejected. The engine
container becomes `main` in the DGD. Do not use `main` as a sidecar's name.
The stack pins the 1.7 nightly chart `1.7.0-dev.20261103.ge37f0b9`, with runtime
images tagged `20261103-e37f0b9`. For those images Modelplane sets
`runtimeVersionOverride: 1.7.0` on DGD components because the date tag has no
semantic version. The component override accounts for every member image,
independent of member order. Mixing that nightly with semantic tags requires
all roles to identify runtime version `1.7.0`; incompatible or unversioned role
images are rejected. Other image tags must identify the Dynamo runtime version
so DGD admission can resolve compatibility. The frontend uses the first engine's runtime image,
image pull secrets, environment, and model cache.

Omit `serving.router` and `serving.endpointPicker` on Dynamo. Its frontend handles
both Unified and PrefillDecode serving, including KV transfer. On Standard,
PrefillDecode still requires an endpoint picker and its engine-side KV connector.
This cluster-dependent requirement is checked during composition.

Changing the backend, component set, roles, or node count replaces the graph:
these fields are immutable in DGD. The replacement also covers changes to
GPU power limits and their allocation. Commands, copy counts, and ordinary
runtime image updates keep the same graph identity. During replacement the old
graph is deleted so its GPUs can be reassigned; serving can be interrupted until
the new frontend and engines are ready.

For a multi-node Dynamo engine, `$(MODELPLANE_LEADER_ADDRESS)` and
`$(MODELPLANE_RANK)` alias the operator's topology variables. Each member keeps
its own command, sidecars, node pool, and DRA claim. See
[multi-node serving on Dynamo]({{< ref "/guides/serving-multi-node-on-dynamo.md" >}}).

Dynamo engines can opt into peer-to-peer weight loading with
`--load-format modelexpress`. See
[model caching]({{< ref "/models/model-cache.md#accelerating-with-modelexpress" >}}).

## Capacity reservations

Large GPU machines are often only available through a capacity reservation. On
GKE and EKS a node pool can consume one you've already bought. Register the
reservation as a `CapacityReservation`, then reference it from the pool's
`reservationRef`:

{{< manifests path="concepts/capacity-reservation-gke.yaml" apply="false" >}}

A pool that references a reservation only gets nodes from that reservation, and
never falls back to on-demand capacity. Keep its `nodeCount` within the
reserved instance count. On GKE, restrict the pool's `zones` to the
reservation's zone. On EKS, restrict them to the reservation's Availability
Zone.

A `CapacityReservation` describes any GKE reservation, including a future
reservation in calendar mode once its reservation period starts. On EKS it
describes a Capacity Block for ML (`type: CapacityBlock`) or an On-Demand
Capacity Reservation (`type: OnDemand`). Modelplane doesn't create, change, or
delete the reservation itself. It blocks deleting a `CapacityReservation`, or an
`InferenceClass`, while a node pool references it.

When a Capacity Block or a calendar-mode reservation ends, the cloud removes
the nodes that consume it. A pool's reservation can't change, and neither can
a `CapacityReservation`'s. To move to a new reservation, add a node pool that
references it, ideally before the old reservation ends, then remove the old
pool.

## Examples

{{< tabs >}}
{{< tab "GKE" >}}
{{< manifests path="concepts/inference-cluster-gke.yaml" apply="false" >}}
{{< /tab >}}
{{< tab "EKS" >}}
{{< manifests path="concepts/inference-cluster-eks.yaml" apply="false" >}}
{{< /tab >}}
{{< tab "AKS" >}}
{{< manifests path="concepts/inference-cluster-aks.yaml" apply="false" >}}
{{< /tab >}}
{{< tab "Nebius" >}}
{{< manifests path="concepts/inference-cluster-nebius.yaml" apply="false" >}}
{{< /tab >}}
{{< tab "Vultr" >}}
{{< manifests path="concepts/inference-cluster-vultr.yaml" apply="false" >}}
{{< /tab >}}
{{< tab "Civo" >}}
{{< manifests path="concepts/inference-cluster-civo.yaml" apply="false" >}}
{{< /tab >}}
{{< tab "Existing" >}}
{{< manifests path="concepts/inference-cluster-existing.yaml" apply="false" >}}
{{< /tab >}}
{{< /tabs >}}

## Cache storage

A [ModelCache]({{< ref "/models/model-cache.md" >}}) stages model weights on a
`ReadWriteMany` (RWX) StorageClass on the workload cluster. Where that comes from
depends on the source:

<!-- vale Google.Acronyms = NO -->
- **`GKE`** (Filestore Enterprise), **`EKS`** (EFS), **`AKS`** (Azure Files),
  and **`Nebius`** (shared filesystem): auto-provisioned. Those classes are
  fixed; nothing for the admin to do.
- **`Vultr`** and **`Civo`**: none. VKE's built-in RWX class (Vultr File
  System) isn't usable on GPU nodes, Civo's `civo-volume` class is
  `ReadWriteOnce` only, and Modelplane doesn't provision an alternative
  on either. Deploy single-node engines without a `ModelCache` and let
  them pull weights directly from the model source.
- **`Existing`**: bring your own. Create an RWX StorageClass on the cluster, with
  any backend that supports automatic PVC provisioning (WekaIO, NetApp Trident,
  `FSx` for NetApp, and similar), and name it in
  `cluster.existing.cache.storageClassName`.
<!-- vale Google.Acronyms = YES -->

The ML team's `ModelCache` and `ModelDeployment` specs are the same regardless of
which backing storage a cluster uses.
<!-- vale write-good.Passive = YES -->
