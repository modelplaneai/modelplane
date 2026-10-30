# Multiple accelerators in the serving stack

**Status:** Proposed
**Date:** October 2026
**Author:** Christopher Haar

## Summary

Modelplane's serving stack installs NVIDIA's device stack on every cluster and
no other vendor's anywhere, while the clouds it provisions take on AMD GPUs,
CPU inference ([#509](https://github.com/modelplaneai/modelplane/pull/509)),
and further accelerator families. I propose we treat the device stacks a
cluster needs as data that flows from the InferenceClass to the serving
stack, keyed by the DRA driver name the class already carries
(`gpu.nvidia.com`, `gpu.amd.com`, `dra.cpu`). The cluster composition
collects the drivers a cluster's classes claim onto `ServingStack
spec.draDrivers`, each component that is part of a driver's device stack
carries the names of the drivers it serves as tags, and the component join
installs a tagged component only when one of its drivers is present. Every
cloud participates, hand-written and generated; a cluster whose classes claim
a driver its cloud can't serve fails early in `compose-inference-cluster`;
and #509's dra-driver-cpu becomes the mechanism's first tagged stack.

## Background

Modelplane installs a serving stack on every inference cluster: a per-cloud
component list of pinned Helm charts and manifests, hand-written for Vultr,
Civo, Nebius, and Existing (BYO), generated from AICR for EKS, AKS, and GKE.
That stack is NVIDIA-only by construction. Every list installs the NVIDIA
operator or DRA driver unconditionally, the accelerator taint and toleration
are `nvidia.com/gpu`, and nothing checks that the devices a class declares
match the stack its cluster gets.

That assumption is expiring on the clouds Modelplane already provisions. Vultr
sells AMD Instinct GPUs alongside NVIDIA. The CPU inference design
([#509](https://github.com/modelplaneai/modelplane/pull/509)) serves models on
CPU-only pools through dra-driver-cpu. TPU and Trainium sit behind the same
gap. One cluster may carry devices served by more than one DRA driver, and the
stack must install each driver's components exactly where that driver's
devices are: an AMD pool without the AMD operator carries GPUs nothing can
drive, and an AMD operator on a cluster without AMD devices runs privileged
DaemonSets and takes version bumps for hardware that isn't there.

The property this design rests on is DRA's naming. Every claimable device is
served by exactly one named driver, an InferenceClass already names that
driver on each `claim: DRA` device, and a device stack exists to serve exactly
one driver: the NVIDIA operator and DRA driver chart serve `gpu.nvidia.com`,
AMD's operator and kernel-module manager serve `gpu.amd.com`, dra-driver-cpu
serves `dra.cpu`. The driver name is a complete, user-visible identifier for
which stack a device needs.

## Goals

- One cluster source can install more than one device stack, and only the
  stacks whose devices its classes actually claim.
- A cluster whose classes claim a driver its cloud can't serve fails early,
  with a condition and event that say why.
- Every cloud takes the same code path: tags and filter everywhere.
- CPU inference is a device stack like any other: #509 becomes one tagged
  chart, with no per-device-type XRD field or transform.

The design leaves out scheduling awareness beyond taints, because DRA already
matches devices by driver and class, and mixed-accelerator node pools: a pool
has one accelerator type, and a cluster mixes accelerators across pools.

## Proposal

**`spec.draDrivers` names the stacks by DRA driver name.** There is no
vendor enum and no driver-to-vendor map: a class's `devices[].driver` already
names the stack that must serve it, so `compose-inference-cluster` collects
the drivers claimed across a cluster's referenced classes and sets them
verbatim as `ServingStack spec.draDrivers`. (The field isn't `accelerators`
— `dra.cpu` isn't one — and isn't plain `drivers`, which `spec.gpu` already
uses to mean the GPU's kernel driver.) Only `claim: DRA` devices count. A
`Synthetic` device is described for scheduling and never claimed, and its
`driver` is only the attribute domain `nodeSelector` CEL reads, not an
installable stack; an InfiniBand fabric matters to placement, not to the
component list. Derivation is verbatim even for a driver the serving stack
won't install itself; which drivers a cloud can serve, and how drivers
installed outside the stack fit, is the per-cloud table's job (see
"Supported drivers per cloud").

A class describing an AMD pool, and what the cluster composition derives from
it:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceClass
metadata:
  name: amd-mi355x-8x
spec:
  provisioning:
    provider: Vultr
    # ...
  devices:
  - name: gpu
    claim: DRA
    driver: gpu.amd.com          # names the device stack
    deviceClassName: gpu.amd.com
    count: 8
---
# Derived by compose-inference-cluster onto the (machine-generated) ServingStack:
spec:
  cloud: Vultr
  draDrivers: [gpu.amd.com]      # filters the cloud's tagged components
```

**Component tags.** `Chart` and `Manifests` entries gain an optional
`dra_drivers` list, the DRA driver names whose device stacks they belong to:
driver install, operator, DRA driver, and anything only those stacks need. An
untagged component is needed regardless of devices and always installs; a
tagged component installs when any of its drivers is in `spec.draDrivers`,
which the join checks on every cloud.

```python
Chart(
    key="amd-gpu-operator",
    # ...
    dra_drivers=["gpu.amd.com"],  # dropped unless a class claims gpu.amd.com
),
Chart(
    key="node-feature-discovery",
    # ...
    dra_drivers=["gpu.nvidia.com", "gpu.amd.com"],  # serves both GPU stacks
),
Chart(
    key="cert-manager",
    # ...                         # untagged: every cluster needs it
),
```

The names are the drivers classes claim devices through, and the list earns
its plural twice. A component can serve several stacks: NVIDIA's DRA driver
and AMD's operator both find their nodes by NFD labels while dra-driver-cpu
doesn't, so NFD carries the two GPU drivers' names and stays off a CPU-only
cluster. And a stack's own chart may register secondary drivers along the way
(NVIDIA's DRA driver chart can also serve `resource.nvidia.com` compute
domains); if classes ever claim devices through one, the chart carries both
names. Dependencies respect the filter: a dependency must be untagged or
carry every driver its dependent carries, so filtering can never remove a
dependency out from under a component it keeps (see "Failing early").

**Generated clouds tag through `generate.py`.** The hand-written clouds tag
their components in place. For the aicr-generated halves (EKS, AKS, GKE), the
`ALLOW` table's values gain the driver list, one per entry it already lists:

```python
ALLOW = {
    "cert-manager": ("cert-manager", []),
    "nfd": ("node-feature-discovery", ["gpu.nvidia.com"]),
    "gpu-operator": ("gpu-operator", ["gpu.nvidia.com"]),
    "nvidia-dra-driver-gpu": ("nvidia-dra-driver-gpu", ["gpu.nvidia.com"]),
    # ...
}
```

This fails closed exactly like ALLOW/DROP today: an aicr bump that introduces
a component stops the run until someone classifies it, including deciding
which stacks it serves. That decision is the review the bump already
requires; the tag just records it.

**CPU is just another device stack.** The CPU inference design installs
through this mechanism rather than a field of its own: dra-driver-cpu is one
chart tagged `dra_drivers=["dra.cpu"]`, pinned on every cloud including
Existing, and CPU classes claim `dra.cpu` like any device. That design covers
what is genuinely CPU-specific: the driver's reserved cores, the
`modelplane.ai/cpu` taint and label, and the pod resources a claim implies.
CPU is also why there is no opt-in path: a CPU class can run on any cloud, so
every cloud filters and `spec.draDrivers` flows everywhere from the start.

One floor is CPU's alone: the driver's grouped mode publishes each NUMA node's
cores as one device whose CPUs are consumable capacity (KEP-5075), which
Kubernetes enables by default from 1.36 and managed control planes can't enable
earlier — on 1.34/1.35 the scheduler allocates the device but strips the
capacity accounting, and the engine pod fails at NodePrepareResources. So a CPU
pool needs Kubernetes 1.36 on a managed cloud, while GPU pools keep the
1.34/1.35 floors; cluster manifests for CPU pools pin the version rather than
taking the cloud's default.

**Supported drivers per cloud.** A cloud supports the drivers its component
list is tagged with, so in the stacks package the per-cloud table is derived
from the tags rather than declared beside them; it can't drift from what the
list records. Not every supported driver arrives through the serving stack,
so a cloud's list can also declare drivers installed outside it. EKS declares
`dra.net`, which `compose-eks-cluster` installs when a pool asks for the EFA
fabric and which the kimi-k2 and qwen3-coder recipes claim today; the
declaration lives in `generate.py` beside the DROP entry that already records
why `aws-efa` is dropped from the bundle. A declared-external driver counts as
supported, and the filter installs nothing for it.

Existing (BYO) is the open-ended case: the user owns the cluster and may have
installed any driver on it, which Modelplane can't enumerate, so on Existing
every driver the list doesn't carry is treated as externally installed. Its
list still pins stacks like on any cloud — NVIDIA's DRA driver and the AMD GPU
operator, beside the dra-driver-cpu every cloud pins — but a claimed driver
beyond them installs nothing and is not rejected. The cost is deferred
failure: a typo'd driver on a BYO cluster surfaces when its ResourceClaim
can't allocate, not as a condition.
e2e runs on exactly this path, registering the kind cluster as Existing and
claiming `gpu.example.com` from a driver the harness installs itself.

The composition function that validates ahead of the join
(`compose-inference-cluster`) mirrors the derived table by hand; functions are
self-contained by design, so a shared library isn't on the table. A flake
check reads the mirror from source and fails when it disagrees with the
stacks package's derived copy.

**Device stacks follow classes.** Today the NVIDIA stack installs
unconditionally; under the filter it installs when a class claims
`gpu.nvidia.com` and not otherwise. That means a cluster's NVIDIA stack
arrives with its first NVIDIA pool and leaves with its last, and a cluster
whose classes claim nothing gets only the untagged components. This is
acceptable: the stack components are per-cluster Helm releases holding no
state worth keeping, the nodes they served leave with the pools that claimed
them, and a stack reinstalls cleanly when a class returns. It is also the
design's point applied to the stack we already have: components mirror
devices.

One teardown hazard is worth naming, not fixing here. On Existing the nodes
stick around when their pool goes away, and nothing orders uninstalling a
driver against deleting the pods whose claims it prepared: remove the last
NVIDIA pool while ModelDeployments still run on it, and if the DRA driver
uninstalls first the kubelet can't unprepare their claims, so the engine pods
hang in `Terminating` until someone force deletes them. The underlying gap is
deleting a pool that still has running replicas, which deserves handling of
its own and would protect every cloud, not just this filter's uninstall.

**Failing early, in the functions.** A cluster whose classes claim a driver
its cloud can't serve would provision devices nothing can drive, so two
layers reject the mismatch:

1. `compose-inference-cluster` gates with `ClusterReady=False` (reason
   `UnsupportedDevices`) when a referenced class claims a driver the
   cluster's cloud neither tags components with nor declares external. A
   typo'd or not-yet-supported driver lands here too; unknown and unsupported
   are the same failure. On Existing nothing lands here at all (see
   "Supported drivers per cloud"). A class is not checked on its own —
   `compose-inference-class` stays a pass-through — so an unsupported driver
   surfaces when a cluster references the class, the first moment it could
   do harm, and the per-cloud table has a single hand-kept mirror. The gate
   stops with a fatal result, because it can fire on a cluster that is
   already running — a shared class edited to claim a new driver — and
   everything composes after it: on a bare return Crossplane would
   garbage-collect the cloud cluster XR, the ServingStack and their Usages.
   A fatal result returns before Crossplane applies desired state or
   collects anything, and the condition still lands on the XR.
2. The component join raises if a ServingStack names a driver its cloud
   neither tags components with nor declares external, and if a component
   depends on one that filtering could remove from under it: a dependency
   must be untagged or carry every driver its dependent carries.

The example class makes layer 1 concrete. Vultr's list carries no
`gpu.amd.com` components until its AMD stack is pinned (see "Future
improvements"), so a cluster referencing `amd-mi355x-8x` gates rather than
provisions:

```yaml
status:
  conditions:
  - type: ClusterReady
    status: "False"
    reason: UnsupportedDevices
    message: "class amd-mi355x-8x claims gpu.amd.com; Vultr clusters support dra.cpu, gpu.nvidia.com"
```

Accepting it is a data change, not a design change: pin the AMD operator and
kernel-module manager on Vultr's list tagged `gpu.amd.com`, and the derived
table, the mirror, and this condition follow.

**Scheduling and tolerations.** The ModelDeployment path needs no changes: DRA
device classes, drivers, and selectors already flow from the class's devices.
Node taints follow the stack (`nvidia.com/gpu`, `amd.com/gpu`,
`modelplane.ai/cpu` for CPU pools), and tolerations follow the taints in two
places. Worker pods tolerate the per-driver taint list; tolerating a taint a
pool never carries is harmless, so the accelerator isn't threaded through
placement. The stack's own node-reaching DaemonSets (NFD's worker, node
exporters, ephemeral-storage metrics) tolerate the same list, pinned in the
hand-written clouds' values and classified fail-closed in `generate.py`'s
`TOLERATIONS` table.

## Future improvements

- **AMD on a first provisioned cloud**: pin the AMD operator and kernel-module
  manager on its list, tagged `gpu.amd.com`, as Existing's list already does.
  Vultr is the likely first: it already sells AMD Instinct alongside NVIDIA.
- **AMD on the generated clouds** (EKS, AKS, GKE): classify the AMD components
  in `ALLOW` and add `gpu.amd.com` to NFD's entry once one of them offers AMD
  shapes; the mechanism is already in place.
- **Non-GPU accelerators** (TPU, Trainium): new driver names, a cloud's list
  gains that stack's tagged components, and the same derivation reads them
  from the class.
- **Mirror generation**: if the per-cloud tables grow past a handful of
  drivers and clouds, generate `compose-inference-cluster`'s mirror from the
  stacks package's derived copy instead of keeping it in sync by hand.

## Alternatives considered

**Install every device stack a cloud supports, unconditionally.** Today's
NVIDIA behavior, generalized: no `spec.draDrivers`, no tags, no filter. It
is the simplest mechanism, and each stack already keeps to its own nodes:
NVIDIA's DRA driver schedules only onto nodes NFD labels as NVIDIA, AMD's
operator selects on `feature.node.kubernetes.io/amd-gpu`, dra-driver-cpu on
#509's CPU label. Rejected because idle stacks aren't free, and the
keeping-to-its-nodes is each chart's convention rather than a contract
Modelplane checks: every stack's controllers run on every cluster's system
pool, every stack's version bumps and CVEs roll through clusters that never
needed them, and a selector that loosens in a bump puts a privileged DaemonSet
or `dra.cpu`'s core reservation on nodes that never asked for it. The
per-cloud table would still exist for validation; the filter is what turns it
from a comment into behavior.

**A vendor enum with a driver-to-vendor map.** `spec.accelerators: [AMD]`,
derived through `{"gpu.amd.com": "AMD"}`. The enum reads well in a spec, and
it would group a vendor's drivers if one vendor ever split across several.
Rejected: the map is a second vocabulary that must grow in lockstep with the
driver names, adds an unknown-driver failure the driver names don't have, and
invents a vendor for accelerators that don't have one (`dra.cpu`). The driver
name is already unique, already in the class, and already what the stack's
components exist to serve.

**A field and transform per device type.** `ServingStack spec.cpu.pools` and a
transform that installs dra-driver-cpu when the list is non-empty, the shape
#509 proposes, following the `spec.gpu.pools` precedent. It is explicit: the
spec says which pools carry which device type. Rejected: every new device type
adds an XRD field, a derivation, and a transform that restate "install this
stack when these devices are present". The tag states it once, and CPU becomes
one tagged chart; #509 rebases onto this mechanism.

**Reject mismatches at admission (CEL on the InferenceClass XRD).** A CEL rule
on the class could refuse a driver its `provisioning.provider` can't serve,
and it would fail fastest, at apply time, before any composition runs.
Rejected: it bakes a second hand-kept copy of the per-cloud table into the
XRD, the class alone offers little to check — a BYO class may have no
`provisioning` block, and on Existing an unlisted driver is legitimate — and
it splits validation across two mechanisms. `compose-inference-cluster` sees
the class and the cluster together and reports through conditions and events,
where Modelplane's diagnostics already live.

**One component list per (cloud, accelerator).** Separate lists, say
`vultr-nvidia.py` and `vultr-amd.py`, joined by which accelerators are
present. Each list stays plain data with no filter semantics to learn.
Rejected: it multiplies the lists (and the generated ones with them) for what
is a per-component property, and the shared, accelerator-neutral majority of
every list would be duplicated per accelerator.

**Blanket-tag every component in an accelerator-flavored list.** Tag not just
the device stack but everything a GPU cloud's list carries, so one uniform
rule decides the whole list with no untagged case. Rejected: most of every
list is neutral infrastructure (cert-manager, monitoring, the gateway), and
tagging it would let the filter strip a cluster's core components. A
component's drivers mean "part of these stacks", nothing broader, and the
join's dependency check enforces that a dependency is never filtered out from
under its dependent.
