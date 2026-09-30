# Multiple accelerator vendors in the serving stack

**Status:** Proposed
**Date:** September 2026
**Author:** Christopher Haar

## Summary

Modelplane's serving stack is NVIDIA-only by construction: every cloud's
component list installs the NVIDIA operator or DRA driver, the GPU taint and
toleration are `nvidia.com/gpu`, and nothing checks that the GPUs a class
declares match the stack the cluster gets. That assumption is expiring on the
clouds Modelplane already provisions: Vultr sells AMD Instinct GPUs alongside
NVIDIA, and other accelerator families (TPU, Trainium) sit behind the same
architectural gap. One cluster may carry devices from more than one vendor,
and the serving stack must install each vendor's device stack exactly where
that vendor's devices are.

I propose we treat the accelerator vendor as data that flows from the
InferenceClass to the serving stack: classes already name the vendor through
their devices' DRA driver (`gpu.amd.com`, `gpu.nvidia.com`), the cluster
composition derives the vendor set and stamps it on the ServingStack, and the
stack's component join filters vendor-tagged components to the vendors present.
Which components make up a vendor's stack, and which vendors a cloud supports,
stay reviewed, pinned data, the same discipline the serving stack generation
design established.

## Goals

- One cluster source can install more than one vendor's device stack, and only
  the vendors whose devices its classes actually name.
- A class naming a vendor its cloud can't drive fails early, with a condition
  and event that say why.
- Clouds that stay single-vendor change in no way: same components, same specs,
  same goldens.

## Non-goals

- Vendor-tagging the aicr-generated cloud halves (see "What doesn't change").
- Scheduling awareness of vendors beyond taints: DRA already matches devices by
  driver and class, so placement needs no vendor concept.
- Mixed-vendor node pools. A pool has one accelerator type; a cluster mixes
  vendors across pools.

## Design

**Vendor derivation.** An InferenceClass's `devices[].driver` is the DRA driver
that serves the device, and its domain names the vendor. `compose-inference-
cluster` derives the set of vendors across a cluster's referenced classes and
sets it as `ServingStack spec.accelerators` (`[AMD]`, `[NVIDIA]`, or both). No
new user-facing field: the vendor was always in the class, this just reads it.

A class describing an AMD pool, and what the cluster composition derives from
it, look like this — the only vendor-specific inputs are the driver and device
class the class already carries:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceClass
metadata:
  name: amd-mi355x-8x
spec:
  provisioning:
    provider: Vultr        # or any provider whose stack can install AMD
    # ...
  devices:
  - name: gpu
    claim: DRA
    driver: gpu.amd.com          # names the vendor
    deviceClassName: gpu.amd.com
    count: 8
---
# Derived by compose-inference-cluster onto the (machine-generated) ServingStack:
spec:
  cloud: Vultr
  accelerators: [AMD]      # filters the cloud's vendor-tagged components
```

**Component tags.** `Chart` and `Manifests` entries gain an optional
`accelerator_vendor`. A tagged component is part of one vendor's device stack:
driver install, operator, DRA driver, and anything only that stack needs. An
untagged component always installs. The join filters tagged components by
`spec.accelerators`; when the field is unset nothing is filtered, so every
cloud behaves exactly as before until it opts in.

```python
Chart(
    key="amd-gpu-operator",
    # ...
    accelerator_vendor="AMD",   # dropped unless AMD devices are present
),
Chart(
    key="cert-manager",
    # ...                       # untagged: every cluster needs it
),
```

**Supported vendors per cloud.** The stacks package carries
`ACCELERATOR_VENDORS`, a table mapping each cloud to the vendors its component
list can install. A single-vendor cloud keeps its device components untagged,
there is nothing to filter. A cloud that gains a second vendor tags its device
components, flips its table entry, and starts receiving `spec.accelerators`.
Existing (BYO) supports any vendor: the cluster's operator manages the device
stack, Modelplane doesn't install one.

**Failing early, in the functions.** A class whose devices name a vendor its
cloud's stack can't install would provision devices nothing can drive, so
three layers reject the mismatch, each at the earliest point it can see the
problem, none of them admission webhooks:

1. `compose-inference-class` marks a class `Accepted=False` (reason
   `UnsupportedDevices`) when its own `provisioning.provider` can't drive the
   vendors its devices name.
2. `compose-inference-cluster` gates with `ClusterReady=False` on the pairing a
   class alone can't see, a class valid for one provider referenced from a
   cluster on another.
3. The component join raises if a ServingStack is composed with a vendor its
   cloud has no stack for, and if any component depends on another vendor's
   tagged component (which filtering could remove from under it).

The per-cloud table is mirrored in the two composition functions that need it,
with kept-in-sync comments pointing at the stacks package's copy; functions are
self-contained by design, so a shared library isn't on the table.

**Scheduling.** The ModelDeployment path needs no changes: DRA device classes,
drivers, and selectors already flow from the class's devices. Node taints
follow the vendor (`nvidia.com/gpu`, `amd.com/gpu`), and the pod GPU toleration
becomes a per-vendor list; tolerating a taint a pool never carries is harmless,
so the vendor isn't threaded through placement.

## What doesn't change

The aicr-generated cloud halves (EKS, AKS, GKE) are untouched. Their components
stay untagged, which under the filter semantics means "always installs",
exactly today's behavior, since those clouds don't receive `spec.accelerators`.
Tagging them means teaching `generate.py` a vendor classification (fail-closed
like its ALLOW/DROP tables) and carrying it through every aicr bump; that cost
buys nothing until a second vendor is installable on a managed cloud, so it
lands with that work, not before.

## Future work

- **A first multi-vendor cloud**: tag its device components, flip its
  `ACCELERATOR_VENDORS` entry, and start deriving `spec.accelerators` for it.
  Vultr is the likely first: it already sells AMD Instinct alongside NVIDIA.
- **AMD on the generated clouds** (EKS, AKS, GKE): tag the generated halves via
  `generate.py` and extend the derivation to every cloud, once one of them
  offers AMD shapes.
- **Non-GPU accelerators** (TPU, Trainium): the mechanism is named for this,
  `AcceleratorVendor` grows new values, a cloud's list gains that vendor's
  components, and the same derivation reads the vendor from the class's DRA
  driver domain.
- **Table generation**: if the mirrored vendor tables grow past two vendors and
  a handful of clouds, generate the mirrors from the stacks package's copy
  instead of keeping them in sync by comment.

## Alternatives considered

**Reject at admission (CEL on the InferenceClass XRD).** Rejected: the class
alone doesn't know which cluster will reference it, so admission can only catch
the self-contradictory class, silently misses the cross-resource pairing, and
splits validation across two mechanisms. The composition functions see both and
report through conditions and events, which is where Modelplane's diagnostics
already live.

**One component list per (cloud, vendor).** Rejected: it multiplies the lists
(and the generated ones with them) for what is a per-component property, and
the shared, vendor-neutral majority of every list would be duplicated per
vendor.

**Blanket-tag everything in a vendor-flavored list.** Rejected: most of every
list is vendor-neutral infrastructure (cert-manager, NFD, monitoring), and
tagging it would let a future filter strip a cluster's core components. The tag
means "part of this vendor's device stack", nothing broader, and the join's
dependency check enforces that an untagged component never depends on a tagged
one.
