# Multiple accelerators in the serving stack

**Status:** Proposed
**Date:** October 2026
**Author:** Christopher Haar

## Summary

Modelplane's serving stack installs NVIDIA's device stack on every cluster and
no other vendor's anywhere, while the clouds it provisions take on AMD GPUs,
CPU inference ([#509](https://github.com/modelplaneai/modelplane/pull/509)),
and further accelerator families. I propose we treat the accelerator as data
that flows from the InferenceClass to the serving stack, identified by the DRA
driver name the class already carries (`gpu.nvidia.com`, `gpu.amd.com`,
`dra.cpu`). The cluster composition collects the drivers a cluster's classes
claim onto `ServingStack spec.accelerators`, each component that is part of
one driver's device stack carries that driver name as a tag, and the component
join installs a tagged component only when its driver is present. Every cloud
participates, hand-written and generated; a class claiming a driver its cloud
can't install fails early in the composition functions; and #509's
dra-driver-cpu becomes the mechanism's first tagged stack.

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
- A class claiming a driver its cloud's stack can't install fails early, with a
  condition and event that say why.
- Every cloud takes the same code path: tags and filter everywhere, no "this
  cloud doesn't participate yet" case.
- CPU is an accelerator like any other: #509 becomes one tagged chart, with no
  per-device-type XRD field or transform.

The design leaves out scheduling awareness beyond taints, because DRA already
matches devices by driver and class, and mixed-accelerator node pools: a pool
has one accelerator type, and a cluster mixes accelerators across pools.

## Proposal

**The accelerator is the DRA driver name.** There is no vendor enum and no
driver-to-vendor map: a class's `devices[].driver` already names the stack
that must serve it, so `compose-inference-cluster` collects the drivers
claimed across a cluster's referenced classes and sets them verbatim as
`ServingStack spec.accelerators`. Only `claim: DRA` devices count. A
`Synthetic` device is described for scheduling and never claimed, and its
`driver` is only the attribute domain `nodeSelector` CEL reads, not an
installable stack; an InfiniBand fabric matters to placement, not to the
component list. Dropping the map also drops the unknown-driver case from
derivation: there is no table to fall off, and a driver no cloud's components
carry is simply unsupported everywhere, failing validation like any other
unsupported driver (see "Failing early").

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
  accelerators: [gpu.amd.com]    # filters the cloud's tagged components
```

**Component tags.** `Chart` and `Manifests` entries gain an optional
`accelerator`, the DRA driver name whose device stack they belong to: driver
install, operator, DRA driver, and anything only that stack needs. An untagged
component is needed regardless of accelerators and always installs. The join
filters tagged components to `spec.accelerators` on every cloud; the field is
always sent, so there is no unset-skips-the-filter case.

```python
Chart(
    key="amd-gpu-operator",
    # ...
    accelerator="gpu.amd.com",  # dropped unless a class claims gpu.amd.com
),
Chart(
    key="cert-manager",
    # ...                       # untagged: every cluster needs it
),
```

The tag is the driver classes claim devices through. A stack's chart may
register secondary drivers along the way (NVIDIA's DRA driver chart can also
serve `resource.nvidia.com` compute domains), and if classes ever claim
devices through one, the chart carries both names and installs when either is
present.

**Generated clouds tag through `generate.py`.** The hand-written clouds tag
their components in place. For the aicr-generated halves (EKS, AKS, GKE), the
`ALLOW` table's values gain the classification, one value per entry it already
lists:

```python
ALLOW = {
    "nfd": ("node-feature-discovery", None),
    "gpu-operator": ("gpu-operator", "gpu.nvidia.com"),
    "nvidia-dra-driver-gpu": ("nvidia-dra-driver-gpu", "gpu.nvidia.com"),
    # ...
}
```

This fails closed exactly like ALLOW/DROP today: an aicr bump that introduces
a component stops the run until someone classifies it, including deciding
which stack it belongs to. That decision is the review the bump already
requires; the tag just records it.

**CPU is just another accelerator.** The CPU inference design installs through
this mechanism rather than a field of its own: dra-driver-cpu is one chart
tagged `accelerator="dra.cpu"`, pinned on every cloud including Existing, and
CPU classes claim `dra.cpu` like any device. That design covers what is genuinely
CPU-specific: the driver's reserved cores, the `modelplane.ai/cpu` taint and
label, and the pod resources a claim implies. CPU is also why there is no
opt-in path: a CPU class can run on any cloud, so every cloud is
multi-accelerator and `spec.accelerators` flows everywhere from the start.

**Supported accelerators per cloud.** A cloud supports the drivers its
component list is tagged with, so in the stacks package the per-cloud table is
derived from the tags rather than declared beside them; it can't drift from
what the list actually installs. Existing (BYO) is a cloud like any other
here: its list pins the NVIDIA DRA driver today, so it supports
`gpu.nvidia.com`, and supporting another driver on BYO clusters means pinning
that stack in `existing.py` like on any cloud. The two composition functions
that validate ahead of the join (`compose-inference-class`,
`compose-inference-cluster`) mirror the table by hand with kept-in-sync
comments pointing at the stacks package's copy; functions are self-contained
by design, so a shared library isn't on the table. A stale mirror fails
closed: it rejects a newly supported driver until someone updates it, and
never admits an unsupported one, because the join's raise backstops that
direction.

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

**Failing early, in the functions.** A class whose devices claim a driver its
cloud's stack can't install would provision devices nothing can drive, so
three layers reject the mismatch, each at the earliest point it can see the
problem:

1. `compose-inference-class` marks a class `Accepted=False` (reason
   `UnsupportedDevices`) when its own `provisioning.provider` can't install a
   driver its DRA devices claim. A typo'd or not-yet-supported driver lands
   here too; unknown and unsupported are the same failure.
2. `compose-inference-cluster` gates with `ClusterReady=False` on the pairing
   a class alone can't see: a class whose provider supports its devices,
   referenced by a cluster on a cloud that doesn't.
3. The component join raises if a ServingStack names an accelerator its cloud
   has no components tagged for, and if a component depends on one that
   filtering could remove from under it: a dependency must be untagged or
   carry the same tag as its dependent.

The example class makes layer 1 concrete. Vultr's list carries no
`gpu.amd.com` components until its AMD stack is pinned (see "Future
improvements"), so `amd-mi355x-8x` is rejected rather than provisioned:

```yaml
status:
  conditions:
  - type: Accepted
    status: "False"
    reason: UnsupportedDevices
    message: "devices claim gpu.amd.com; the Vultr serving stack installs gpu.nvidia.com, dra.cpu"
```

Accepting it is a data change, not a design change: pin the AMD operator and
kernel-module manager on Vultr's list tagged `gpu.amd.com`, and the derived
table, the mirrors, and this condition follow.

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

- **AMD on a first cloud**: pin the AMD operator and kernel-module manager on
  its list, tagged `gpu.amd.com`. Vultr is the likely first: it already sells
  AMD Instinct alongside NVIDIA.
- **AMD on the generated clouds** (EKS, AKS, GKE): classify the AMD components
  in `ALLOW` once one of them offers AMD shapes; the mechanism is already in
  place.
- **Non-GPU accelerators** (TPU, Trainium): new driver names, a cloud's list
  gains that stack's tagged components, and the same derivation reads them
  from the class.
- **Mirror generation**: if the per-cloud tables grow past a handful of
  drivers and clouds, generate the two functions' mirrors from the stacks
  package's derived copy instead of keeping them in sync by comment.

## Alternatives considered

**Install every device stack a cloud supports, unconditionally.** Today's
NVIDIA behavior, generalized: no `spec.accelerators`, no tags, no filter. It
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
on the class could refuse a driver its `provisioning.provider` can't serve, in
place of validation layer 1, and it would fail fastest, at apply time, before
any composition runs. Rejected: the class alone doesn't know which cluster
will reference it, so admission can only catch the self-contradictory class,
silently misses the cross-resource pairing, and splits validation across two
mechanisms. The composition functions see both and report through conditions
and events, where Modelplane's diagnostics already live.

**One component list per (cloud, accelerator).** Separate lists, say
`vultr-nvidia.py` and `vultr-amd.py`, joined by which accelerators are
present. Each list stays plain data with no filter semantics to learn.
Rejected: it multiplies the lists (and the generated ones with them) for what
is a per-component property, and the shared, accelerator-neutral majority of
every list would be duplicated per accelerator.

**Blanket-tag every component in an accelerator-flavored list.** Tag not just
the device stack but everything a GPU cloud's list carries, so one uniform
rule decides the whole list with no untagged case. Rejected: most of every
list is neutral infrastructure (cert-manager, NFD, monitoring), and tagging it
would let the filter strip a cluster's core components. The tag means "part of
this driver's device stack", nothing broader, and the join's dependency check
enforces that a dependency is never filtered out from under its dependent.
