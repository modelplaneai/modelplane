# Generating the serving stack

**Status:** Accepted
**Date:** September 2026
**Authors:** Nic Cope (@negz), Christopher Haar (@haarchri)

## Summary

A `ServingStack` installs a fixed set of Helm charts and raw manifests on a
workload cluster. Version fields on its spec pin five of them, a sixth field
does nothing, another version is a module constant, and every values block is a
Python dict in the composition function. Those versions do work: Modelplane's
own recipe catalog, where each recipe is a model served end to end on real
hardware, has exercised them on four clouds and five accelerators. What
Modelplane can't say is that they're the right versions, or that they still
hold once a pin moves, because nothing re-runs a recipe.

We propose generating that list at build time, taking the parts NVIDIA maintains
from [NVIDIA AICR](https://github.com/NVIDIA/aicr) recipes and adding
Modelplane's own alongside them. A Modelplane release then installs one stack
per cloud, the same on every cluster, with nothing to override.

Getting newer cluster software would mean upgrading Modelplane, and upgrading
Modelplane would upgrade the software on every cluster it manages. That cost
buys a stack Modelplane has tested as a whole. A `ModelReplica` composes
resources against the controllers the stack installs.

Four consequences are worth stating as bluntly as the design means them,
because they are the price:

- **No GPU driver setting.** Which kernel driver a GPU node runs, and whether
  the node image or the GPU Operator provides it, is a generated value per
  cloud. No API field reads or writes it, and changing it means a Modelplane
  release. One driver version per cluster, whatever its pools run.
- **No overrides.** No version field, no values override, no way to add,
  remove or substitute a component on one cluster. A platform team that needs
  a different stack takes the break-glass `Composition` and owns the result.
- **No registry override.** Every cluster pulls charts and images from the
  upstream repositories and registries the generated lists name. A cluster
  that must pull from a private mirror, has no field for that either.
- **Upgrades ride releases.** Updating Modelplane updates the stack on every
  cluster its control plane manages, at once, with no way to stage it. A
  cluster cannot hold a component back, and a component cannot move without a
  release, a patch release for a fix, a minor for everything else. The one
  exception is the GPU driver on clouds whose node image supplies it: that
  version follows the image, so it can move as nodes and pools cycle, with no
  release involved.

Together they draw a line through day-zero model support. A model that runs
on the runtime a release already ships works the day it appears. A model that
needs a newer runtime, CUDA major, driver or GPU generation waits for a
Modelplane release, and takes the whole fleet with it.

Each of these is a limit the design accepts. If one of them turns out to bite,
[future improvements](#future-improvements) sketches the path out and the
seams this design keeps open for it.

`spec.versions`, `spec.standard`, `spec.dynamo` and `nvidiaDriverRoot` all go
away. One field replaces them, naming the cloud the `ServingStack` runs on.

## Background

An `InferenceCluster` composes a cloud cluster (`EKSCluster`, `GKECluster` and
so on) and a `ServingStack`. Nodes come from the cloud cluster, along with
anything that needs cloud infrastructure identity: an autoscaler bound to an IAM
role, a `StorageClass` naming a provisioned filesystem, an EFA driver a node
pool asked for. The `ServingStack` installs the rest.

Its spec can change a version and little else, so the components it can't
express live elsewhere. EFA arrives from the EKS composition when a pool asks
for the fabric, and the NVIDIA network operator from the AKS composition on
InfiniBand. Neither has a version field. `nvidiaDriverRoot` is a string that one
composition sets, on one cloud, with a comment explaining that the DRA driver
can't find `nvidia-smi` without it. Nothing in the API says what a cluster runs.

### The contract a stack has to keep

A `ModelDeployment` composes `ModelReplica`s, and a `ModelReplica` composes
resources onto the workload cluster. A controller the serving stack installed
answers each of them:

| What `ModelReplica` composes | Served by |
| --- | --- |
| `leaderworkerset.x-k8s.io/v1` `LeaderWorkerSet` | the `lws` chart, on the Standard stack |
| `grove.io/v1alpha1` `PodCliqueSet` | the `grove-charts` chart, on the Dynamo stack |
| `inference.networking.k8s.io/v1` `InferencePool` | the Gateway API Inference Extension CRDs and Envoy AI Gateway |
| `gateway.networking.k8s.io/v1` `HTTPRoute` | Envoy Gateway |
| a `ResourceClaimTemplate` naming the `gpu.nvidia.com` device class | the NVIDIA GPU DRA driver |

Plain string contracts run beside those API contracts. A replica reaches
ModelExpress at `modelexpress-server:8001`, sets `schedulerName: kai-scheduler`,
and labels pods `kai.scheduler/queue: modelplane` for a `Queue` the serving
stack creates.

Modelplane composes a `grove.io/v1alpha1` resource against a chart pinned at
`v0.1.0-alpha.12-rc2`. Neither an alpha API nor a pre-release chart promises
compatibility across versions.

The coupling is growing. Grove and ModelExpress arrived with the Dynamo serving
stack ([modelplaneai/modelplane#406](https://github.com/modelplaneai/modelplane/pull/406)),
and making `InferenceGateway` an AI gateway would have Modelplane composing
against Envoy AI Gateway's own APIs as well as the Gateway API's.

The APIs a `ModelReplica` targets are what define the stack.

### AICR

Modelplane needs versions for the GPU-adjacent components that somebody has run
on the hardware. NVIDIA AI Cluster Runtime publishes them.

It's a Go CLI and library, Apache-2.0, that NVIDIA began publishing in early
2026 and releases about every two weeks. It emits the configuration a
GPU-accelerated Kubernetes cluster needs, but installs none of it. It is
pre-1.0, its artifact schemas are still `v1alpha2`, and its hardware validation
covers a small number of hardware combinations.

A **recipe** is what AICR resolves. You ask for one by naming five **criteria**:

| Criterion | Means |
| --- | --- |
| `service` | The Kubernetes distribution or managed service |
| `accelerator` | The GPU model |
| `os` | The node image's operating system |
| `intent` | Training or inference |
| `platform` | The inference or training stack installed on top |

Those five together are a **coordinate**. AICR resolves one by layering
**overlays**, files that each state a difference from a base and merge by
component name. An overlay names one parent, and every other overlay whose
criteria the request matches applies too, so a chain runs longer than the parent
links suggest. What comes back names a chart, a version and a values file per
**component**, along with a dependency-ordered install sequence and
**constraints**, which are assertions about the cluster the recipe expects, such
as a minimum Kubernetes version.

`aicr bundle --deployer helm` renders a resolved recipe to a directory per
component, each holding a `values.yaml` with every layer already merged and the
chart references beside it:

```text
005-cert-manager/
  upstream.env    # CHART, REPO, VERSION
  values.yaml     # every overlay already merged
```

Modelplane extends AICR's **catalog**, the overlays and component definitions
the binary embeds, rather than forking it. `--data` layers a directory of
Modelplane's own overlays over AICR's, and criteria values are themselves data,
so a new `platform` value costs nothing upstream.

## Goals

**Choose versions against the hardware, and keep choosing.** Every version
Modelplane installs should be one that was validated on the hardware it
targets, for every coordinate Modelplane supports, and should be validated
again when a pin moves.

**Make what a cluster runs knowable.** For a given release, cloud and stack, it
should be possible to read exactly which components and versions Modelplane
installs. This needs no status field: a stack is a function of those three
inputs, so a release listing them is enough.

**Keep the output vendor-neutral.** AICR covers three of Modelplane's six clouds
and no non-NVIDIA accelerator, so the file format has to be writable by hand.

**Keep the stack simple, predictable and auditable.** The same release, cloud
and stack should mean the same components at the same versions on every
cluster, every change to what a cluster runs should be reviewable as a diff
before it ships, and the reconcile path should hold no decisions of its own.

## Proposal

Everything is fixed at build time. Every version, every values block, every
component membership decision is resolved where a human reviews a diff, and the
reconcile path only renders. This is the one commitment the design treats as
settled. Everything downstream of it, the intermediate format, the
one-driver-per-cluster choice, is a consequence that can be revisited without
touching it.

### Generated Python, iterated by the function

We propose a build-time generator that writes Python modules into the
composition function's own package: one list of components per cloud, which the
function iterates. Today the function holds every chart name, version and
values dict in its own source.
Afterwards it reads a list and builds a `Release` or an `Object` per entry,
deriving readiness from what it built rather than from the hardcoded list of
names it keeps now.

The pipeline is short and every stage has one type: AICR where it has coverage,
hand-written where it doesn't, both landing as a Python list of components the
function's logic iterates. The list is the intermediate representation, AICR
is one producer of it, never the format.

Hand-written files live at the top of `stacks/`, and generated ones under
`generated/`, in a directory named for the tool that produced them. `nix run
.#stacks` writes into `generated/aicr/`:

```text
functions/compose-serving-stack/function/stacks/
  generated/aicr/
    eks.py             # Written by nix run .#stacks. Never edited.
    aks.py
    gke.py             # Needs Modelplane's gke-cos profile fork; see below.
  common.py            # Hand-written. Modelplane's own, on every stack.
  standard.py          # Hand-written. Modelplane's own, where the stacks differ.
  dynamo.py
  nebius.py            # Cloud half, for a cloud AICR doesn't cover.
  vultr.py
  amd.py               # The same, for an accelerator vendor AICR doesn't cover.
  existing.py
  __init__.py          # Maps a cloud and stack to the lists to join.
```

Every file has the same shape, a list of `Chart` or `Manifests` entries:

```python
# functions/compose-serving-stack/function/stacks/generated/aicr/eks.py
# Generated by nix run .#stacks. Do not edit.

COMPONENTS: list[Component] = [
    Chart(
        key="cert-manager",                     # composed resource key
        release="mp-cert-manager",              # provider-helm external name
        namespace="cert-manager",
        chart="cert-manager",
        repository="https://charts.jetstack.io",
        version="v1.20.2",
        values={"crds": {"enabled": True, "keep": False}},
    ),
    ...
]
```

```python
# functions/compose-serving-stack/function/stacks/dynamo.py

COMPONENTS: list[Component] = [
    Manifests(
        key="kai-queue",
        depends_on=["kai-scheduler"],
        manifests=[...],                        # Omitted for brevity.
    ),
    ...
]
```

Some of what Modelplane installs has never been chart-shaped: the `EnvoyProxy`,
`GatewayClass`, `Gateway` and its namespace, the kai-scheduler `Queue` pair, the
ModelExpress server and its CRDs, the Gateway API Inference Extension CRDs, and
the DRA `ResourceQuota`.

The generator emits Python rather than vendored YAML because the function is
Python. A list of objects doesn't need a parser, a schema or a vendored file
format, and `ty` catches a rename in the generator's own types.

#### Component sources

The generator takes a source list per cloud and writes one file into
`generated/aicr/`.

**From AICR.** For clouds AICR covers, the generator resolves Modelplane's
coordinate and bundles it, then reads each component's merged values and chart
references.

**Dropped.** AICR resolves more than Modelplane wants, so the generator holds an
allowlist keyed by AICR component name and drops anything absent from it. It
drops agentgateway and its CRDs, because Modelplane routes through an
`InferencePool` behind Envoy AI Gateway. It drops the AWS EFA device plugin,
because the `EKSCluster` installs an EFA DRA driver instead. It drops the EBS
CSI driver, because Modelplane's only storage need is RWX, which the
`EKSCluster` serves from EFS. The allowlist fails closed, so a component AICR
adds later stops the build rather than appearing on every cluster.

**Added.** AICR resolves less than Modelplane needs, and everything in the
contract table above is in the gap. That means Envoy Gateway, Envoy AI Gateway
and its CRDs, the Gateway API Inference Extension CRDs, LeaderWorkerSet or Grove
and ModelExpress, and Modelplane's own objects. Those are the hand-written
files, which the generator reads to check `depends_on` resolves and otherwise
never touches.

**Clouds AICR doesn't cover.** Nebius and Vultr have no `service` value, and
`Existing` isn't a cloud at all. Each is a whole hand-written file, in the same
shape. The file format has no dependency on AICR,
so a non-NVIDIA accelerator is a cloud whose source list is entirely
hand-written. AICR is unlikely to ever cover one: its accelerator values are
nine NVIDIA SKUs, its fingerprinting maps an AMD device to the empty string, and
it fails closed on a cluster mixing vendors.

That leaves the generator writing three files, EKS, AKS and GKE, out of six
clouds, every cloud AICR has a `service` value for. EKS and AKS resolve and
bundle with Modelplane's managed values as the catalog stands; GKE needs the
profile fork described under [Modelplane's catalog](#modelplanes-catalog). `generate.py`
beside this document prototypes the generator against aicr 0.20.0 and produces
all three, with every managed value asserted in the hydrated bundle and the
Kubernetes floors checked against the XRD defaults. The hand-written half is
prototyped beside them: `stacks/nebius.py` and `vultr.py` carry
Modelplane's own pins for the NVIDIA clouds AICR doesn't cover, stating the
same pin where a component is AICR's on a generated cloud so one review moves
both halves, and `stacks/amd.py` is the same shape resolving to AMD's
DRA driver — the file the vendor-neutrality goal promises.

`generated/aicr/` holds whatever AICR resolves and the allowlist keeps, and the
hand-written files hold everything else. The criterion is where the version
comes from.
The DRA driver targets the contract and is still AICR's, because its version
depends on the hardware.

The allowlist drops kai-scheduler for a different reason. AICR carries it in
its base for every cluster, and taking that placement would even be harmless at
runtime: a scheduler is opt-in, so on a Standard cluster it would only be dead
weight. It is dropped because of where its version should come from.
kai-scheduler is contract surface, not hardware surface — `compose-model-replica`
names it in `schedulerName` and labels pods for `Queue`s Modelplane composes,
its version has nothing to do with the GPU, and the clouds AICR doesn't cover
need a Modelplane pin for it anyway. So Modelplane pins it beside its `Queue`s
in `dynamo.py`, and only Dynamo clusters install it.

Each half then varies along one axis. AICR resolves a coordinate per cloud, so
`generated/aicr/` is per cloud. The hand-written files split three ways:
`common.py` for the components on every stack, a file per stack for the ones
that differ, and a file per cloud AICR doesn't cover. `__init__.py` names which
files a combination joins, so EKS on Dynamo joins `generated/aicr/eks.py` with
`common.py` and `dynamo.py`, and Nebius on Dynamo swaps the first for
`nebius.py`. An AMD cluster swaps in `amd.py` the same way — a non-NVIDIA
vendor is one more value of that axis, not a new mechanism. The generator
overwrites `generated/aicr/` whole and never edits a hand-written file.

#### Ordering and identity

`depends_on` resolves against the joined list rather than one file, because
edges cross the halves. Envoy Gateway is Modelplane's, and the cert-manager it
needs for its webhooks is AICR's. An edge naming a component the join didn't
produce is an error. That catches an allowlist which dropped something another
component needs at build time rather than on a cluster.

`depends_on` drives teardown. Crossplane applies composed resources
concurrently, so today the function hand-writes `Usage`s to sequence deletion: a
`Gateway` before its `GatewayClass` before Envoy Gateway, and the kai-scheduler
release after both `Queue`s. The function derives those from `depends_on` rather
than naming components in Python.

It doesn't order installs. They stay concurrent and rely on Helm retrying, as
they do today, so AICR's install sequence is an input the generator discards.
Today's components tolerate arriving in any order. The one new edge is AICR's
DRA driver depending on GPU Operator, which nothing has had to converge under
retry alone — verifying it does is part of landing the generated stack. If it
turns out not to, `depends_on` already carries the information ordering would
need; the missing piece would be a `Release` that reports ready only once its
workloads run.

`key` and `release` are Modelplane's, not AICR's, so a component's identity
survives whatever upstream renames. The `mp-` prefix keeps chart-derived names
inside the 63-character label limit and reserves a namespace, so Modelplane
can't adopt a same-named release a user already runs. The chart name after that
prefix can't change either: provider-helm upgrades in place only while a release
name holds still, so a generator naming releases after AICR's component names
would rename the DRA driver's release. provider-helm reads that as an uninstall
and a fresh install, deleting the DeviceClass and kubelet plugin every GPU
`ResourceClaim` on the cluster depends on.

### The API change

`ServingStack` loses the four fields nothing can safely set and gains one naming
its cloud:

```yaml
apiVersion: infrastructure.modelplane.ai/v1alpha1
kind: ServingStack
spec:
  cloud: EKS          # new. Mirrors InferenceCluster.spec.cluster.source.
  stack: Dynamo       # unchanged. Standard or Dynamo.
  secrets: [...]      # unchanged.
  gateway: {...}      # unchanged.
# versions:           removed. Five of six are read; nothing reads gatewayApi.
# standard:           removed.
# dynamo:             removed.
# nvidiaDriverRoot:   removed. A generated value, not an API field.
```

`spec.gateway` stays, because a listener is per-cluster configuration rather
than a software version. `spec.stack` stays because the composition already sets
it.

`cloud` reverses an invariant Modelplane states twice today, in the function and
in the XRD, that the serving stack never inspects its own cloud.
`nvidiaDriverRoot` exists to keep it that way, encoding "you're on GKE" as a
driver path the cluster composition passes down. That indirection works while
one string covers the whole difference between clouds, and a generated component
list selects on the cloud directly.

### The resulting stack

This is a Dynamo stack on EKS, set against what Modelplane installs today:

| Component | Today | Generated |
| --- | --- | --- |
| cert-manager, kube-prometheus-stack, node feature discovery, NVIDIA DRA driver | Modelplane's pin | AICR's pin |
| GPU Operator, NVSentinel (GPU fault detection), the nodewright operator (node configuration), Prometheus operator CRDs, Prometheus adapter, ephemeral storage metrics | Absent | AICR's pin |
| agentgateway and its CRDs, AWS EFA device plugin, EBS CSI driver | Absent | Dropped by the allowlist |
| Envoy Gateway, Envoy AI Gateway and its CRDs, the Gateway API Inference Extension CRDs, Grove, ModelExpress, kai-scheduler | Modelplane's pin | Modelplane's pin |

A Standard stack is the same with kai-scheduler dropped and LeaderWorkerSet in
place of Grove and ModelExpress.

GPU Operator is the largest addition. AICR's DRA driver depends on it, so it
comes with driver, toolkit and device plugin turned off, leaving GPU feature
discovery and the MIG manager with DCGM metrics at the chart's own default. The
alternative is a DRA driver configuration nobody upstream tests.

Among the versions that move, cert-manager goes from v1.17.1 to v1.20.2, node
feature discovery from 0.18.3 to 0.19.0 and out of a different chart repository,
and the DRA driver from 0.4.0 to 0.4.1 on the chart Modelplane already uses.

### One stack per release

Modelplane installs exactly one stack per cloud per release, with no version
field, values override or per-cluster substitution anywhere. Concretely, unless
[a future improvement](#future-improvements) ever changes it:

- The GPU driver mode and version cannot be set, per cluster or at all. The
  mode changes only when a Modelplane release regenerates the stack, and so
  does the version where the GPU Operator installs the driver. Where the node
  image supplies it, the version follows the image.
- Nothing can be overridden — not a version, not a values block, not component
  membership — without a Modelplane release that changes the generated or
  hand-written lists.
- Every cluster pulls charts and images from the upstream repositories and
  registries the lists name. A private mirror is not expressible.
- The stack on every managed cluster moves when Modelplane moves, directly and
  together.

A platform team would reasonably rather pin a cluster and upgrade the control
plane. Supporting that means Modelplane vN working against stacks from vN-1 and
earlier, so `compose-model-replica` would emit resources valid against several
controller versions at once. That is a compatibility matrix of Modelplane
versions against stack versions, per stack, per cloud, and it only means
anything if it's tested, so the end-to-end suite multiplies by the depth of the
matrix. The composition functions would branch on the observed stack version,
and some of the APIs they'd branch over are alpha.

The same trade sets the shape of a regulated deployment. Where a serving stack
update must prove out in dev and test before production, the unit of staging is
the control plane: one Modelplane per environment, promoted by upgrading each
in turn. Running and operating a control plane per environment is a real cost
of this design. Letting one control plane roll a stack across environments
gradually means decoupling stack versions from Modelplane versions, the same
compatibility matrix, with the same testing bill, as the per-cluster pin above.

Patch releases and backports let a cluster take a cert-manager fix without
taking a Grove bump, inside a combination Modelplane tested. That means
branching the release, pinning `aicr` to the version that produced it, and
regenerating only the component the fix touches. Modelplane keeps a patch branch
on the current minor, and a component fix merges there and releases without
waiting for the next minor. That patch branch is a prerequisite for removing the
version fields.

The break-glass is a second `Composition`, and it needs one fix. A
`Composition` specifies how Crossplane composes a `ServingStack`, so a platform
team can write their own, backed by a function of their own. It doesn't work
today: the inference cluster function composes the `ServingStack` with neither
a composition reference nor a selector, the XRD doesn't default one, and
Crossplane's selector resolver lists every compatible `Composition` and picks
one at random. The fix is the XRD defaulting to the stock `Composition`, so
installing a second one is inert until it's selected. Selecting it needs no new
Modelplane API: a mutating admission webhook can set the composition reference
on the `ServingStack` as Modelplane composes it, or a Crossplane `ImageConfig`
can rewrite the `compose-serving-stack` function image to one the team owns, so
the stock `Composition` runs their function. Both are advanced mechanisms, and
deliberately so, this is an escape hatch to be reached for knowingly, not a
feature to be configured.

The supported path is the stack Modelplane installs. A team that takes the
break-glass owns the stack and the contract with `ModelReplica`, and Modelplane
can say nothing about whether a `ModelDeployment` will work on it.

### Resolving Modelplane's coordinate

The generator holds a per-cloud table of criteria. `intent` is always
`inference` and `platform` is `modelplane`. `os` appears where a recipe covers
the node image Modelplane uses: GKE's entry names `cos`, and EKS's leaves it
unset because no recipe covers AL2023. `accelerator` is a property of a node
pool rather than a cluster, so the table holds it out:

```console
$ aicr recipe --data ./catalog --service eks --intent inference \
    --platform modelplane |
  yq '.metadata.appliedOverlays, (.componentRefs | length)'
- base
- monitoring-hpa
- eks
- eks-inference
- modelplane-eks-inference
15
```

### Modelplane's catalog

Modelplane needs a `platform: modelplane` criteria value and a few component
overrides that differ from stock. Those overrides carry couplings between values
in different components, so they belong in reviewed data. Modelplane publishes a
`--data` overlay per cloud stating them. How much each overrides depends on the
cloud's default. EKS needs the most, because it has no `gpuStack` profile to
select:

```yaml
# catalog/overlays/modelplane-eks-inference.yaml
kind: RecipeMetadata
apiVersion: aicr.run/v1alpha2
metadata:
  name: modelplane-eks-inference
spec:
  base: eks
  criteria: { service: eks, intent: inference, platform: modelplane }
  componentRefs:
  - name: nvidia-dra-driver-gpu
    overrides:
      gpuResourcesEnabledOverride: true
      resources:
        gpus: { enabled: true }
        computeDomains: { enabled: false }
      nvidiaDriverRoot: /
  - name: gpu-operator
    overrides:
      driver: { enabled: false }
      toolkit: { enabled: false }
      devicePlugin: { enabled: false }
      gdrcopy: { enabled: false }
  - name: nvsentinel
    overrides:
      labeler:
        assumeDriverInstalled: true
```

`gpuResourcesEnabledOverride`, `resources.gpus.enabled` and
`devicePlugin.enabled` together select DRA whole-GPU allocation over the device
plugin, and AICR rejects a partial flip. Turning the operator's driver off then
forces `nvidiaDriverRoot` to name where the node image put the driver, and
forces NVSentinel's labeler to assume a driver it has no pod to observe. AICR's
bundler refuses to render without both. Modelplane doesn't use `computeDomains`,
which is multi-node NVLink, and `gdrcopy`, a kernel module for GPU-direct RDMA,
has no operator-managed driver to build against.

The catalog holds one more file, of a different kind:
`catalog/overlays/gke-cos.yaml`. Everything else in the catalog layers over
AICR's data; that file replaces one piece of it. Selecting DRA on GKE fails
against the stock catalog — the `gpuStack` configuration profile owns the
GPU-advertiser choice, neither of its values advertises through DRA, and a
profile locks the value paths it owns, so an overlay layered via `--data`
can't move them. What `--data` can do is replace an embedded catalog file
wholesale by name, so the fork carries the declaring overlay with one change,
a third profile value `dra` whose sole GPU advertiser is the DRA driver's
ResourceSlices, and the generator resolves GKE with `--profile gpuStack=dra`.
The gap is tracked upstream, and either of two asks under [remaining
work](#remaining-work) deletes the fork when it lands, so carrying it is a
choice rather than a debt: Modelplane accepts re-syncing one high-churn
upstream file on every aicr bump over waiting on NVIDIA.

### Remaining work

**Upstream, non-blocking.** Nothing the design needs from AICR is missing —
where the catalog falls short, a workaround is in place — but the asks filed
from this work would make it cleaner. The detail lives in the issues:

- [NVIDIA/aicr#2512](https://github.com/NVIDIA/aicr/issues/2512) —
  profile-value constraints collide with same-named recipe constraints; until
  fixed, the generator owns GKE's 1.35 floor rather than the fork carrying it.
- [NVIDIA/aicr#2513](https://github.com/NVIDIA/aicr/issues/2513) and
  [#2514](https://github.com/NVIDIA/aicr/issues/2514) — the AKS and EKS cloud
  coordinates can't carry their node OS, so those recipes resolve without OS
  and kernel floors.
- [NVIDIA/aicr#2515](https://github.com/NVIDIA/aicr/issues/2515) and
  [#2517](https://github.com/NVIDIA/aicr/issues/2517) — two routes to deleting
  the gke-cos fork: `--data` adding a profile value without forking the
  declaring overlay, or an upstream `gpuStack` value whose advertiser is DRA.
  Either way, advertising through DRA needs GKE's GPU pools provisioned the way
  [Google's DRA
  guide](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/set-up-dra)
  prescribes — a `compose-gke-cluster` change that ships with the generated GKE
  stack, per #2517.
- [NVIDIA/aicr#2516](https://github.com/NVIDIA/aicr/issues/2516) —
  per-accelerator driver versions in place of the global pin, the upstream
  half of a [future improvement](#future-improvements).
- [NVIDIA/aicr#2500](https://github.com/NVIDIA/aicr/pull/2500) — a pull
  request contributing a LeaderWorkerSet component; once merged and released,
  `lws` moves from Modelplane's hand-written pin to AICR's pin in
  `generated/aicr/` on the clouds AICR covers.

**In-tree, going out with the generator.** A default composition reference on
the `ServingStack` XRD, so the break-glass `Composition` can exist without
being picked at random.

### The GPU driver

A GPU node gets its kernel driver one of two ways: baked into the node image, or
installed at runtime by GPU Operator. Only one can be true at a time, because
two drivers on one node collide.

AICR has no global preference. Its catalog splits about evenly, 52 recipes
having the operator install the driver against 48 taking it from the node image,
and its own documentation says the default follows the cloud provider's default.
So the answer is per cloud, and adopting AICR's recipes means adopting its
answer for each cloud rather than applying a Modelplane policy across all of
them. The mode is a set of values in the generated file, so carrying both costs
no API surface.

AKS and OKE take the node image, and AKS declares the only `gpuStack` profile
that selects driver ownership. GKE has Google supply the driver under both
values of its profile, which chooses the device plugin rather than the driver.
EKS, LKE and BCM have the operator install it.

Modelplane takes the node image on every cloud today, so EKS is where it
diverges. AICR's EKS recipes target Ubuntu, 22 of 36 naming it and none naming
AL2023, and its default installs the driver. Modelplane picks the
`AL2023_x86_64_NVIDIA` AMI and turns the operator's driver off to match.
Conforming means changing the AMI in `compose-eks-cluster` and accepting a
kernel module build on node startup. Not conforming leaves EKS on a combination
AICR never tested, which is the thing this design exists to stop. I'd conform.

**One driver per cluster.** A cluster's node pools can name different
accelerators, and one driver version needn't suit them all. AICR can't express
that. One value in its catalog sets the driver version for every service and
every accelerator, with no override anywhere, chosen to clear the strictest
floor among the GPUs it covers. Its own comment calls this a "global pin". A
recipe also names one accelerator, so nothing in the schema describes a cluster
running two kinds of GPU. Modelplane inherits that limit whichever mode it
takes.

The design takes that limit deliberately rather than working around it, and the
important property is that the choice is internal. The driver mode and version
are values inside a generated file, not fields anywhere in the API, so staying
with one driver per cluster is a decision Modelplane can revisit later without
breaking a user-facing contract. It also matches how AICR resolves today, so
every driver a release ships is one a recipe named. The per-accelerator fan-out
— the GPU Operator's `NVIDIADriver` CR supports multiple driver versions in one
cluster, each targeting nodes by selector, and AICR's hardware layers pin
drivers per accelerator — is described under
[future improvements](#future-improvements).

Where the node image keeps the driver, one layer stays outside the release
cycle. The driver version follows the image, so it moves when nodes or pools
cycle onto a newer one, and no Modelplane release gates or records that.
Modelplane can't say which driver a node runs, and AICR's own measurements only
reveal whether one loaded.

### BYO clusters

On a `source: Existing` cluster Modelplane didn't provision, it can only say
what it adds to whatever is already there.

The BYO contract should state what a cluster must not have, because that failure
is silent. A device plugin already advertising `nvidia.com/gpu` alongside
Modelplane's DRA driver gives two allocators independent ledgers over the same
devices. Nothing reports an error, and workloads contend. AICR rejects exactly
this configuration in its own validator.

Running AICR against a live cluster makes sense here. `aicr snapshot` deploys a
Job reporting what a cluster already has, and `aicr validate` checks a cluster
against a recipe's expectations. A one-shot admission check before Modelplane
adopts a BYO cluster is worth its own design.

## Alternatives considered

**Pre-render the manifests.** Rendering each chart with `helm template` at build
time and applying the output would collapse the Helm and manifest paths into
one. It would also give CRDs a lifecycle Helm doesn't, and remove the
release-identity problem. It loses Helm hooks and `.Capabilities`, and both
losses are fatal for this stack. Six of the eleven charts Modelplane keeps carry
hook annotations in their templates, kai-scheduler with 46 and
kube-prometheus-stack with 26, which `helm template` renders as ordinary
resources with no ordering or wait semantics. Worse, the NVIDIA DRA driver picks
the DRA API version it targets by asking the cluster which of `resource.k8s.io`
`v1`, `v1beta2` or `v1beta1` it serves. `helm template` can't ask, so the
generator would freeze one version at build time and get it wrong on any cluster
whose Kubernetes version differed. kube-prometheus-stack branches on
capabilities in 79 places.

**Keep per-cluster overrides.** A `spec.versions` that a platform team could
actually reach, or a class object naming a different component set, would let a
cluster pin. The cost is the compatibility matrix in "One stack per release":
every combination of Modelplane version and stack version becomes something to
test, and `compose-model-replica` becomes version-conditional against an alpha
API.

**Make the runtime an API.** The GPU runtime could be a pair of cluster-scoped
objects: `InferenceClass` gaining `os` and `fabric`, and a generated
`InferenceClusterClass` carrying a `supports` block, the component set, a
per-accelerator driver fan-out, and resolution from the facts pools already
declare. It answers the costs this design accepts — new hardware without
waiting on a release, per-cluster deviation with its consequences reported,
hand-authored classes for the clouds and vendors AICR doesn't cover. It also
fronts a lot of API surface before anyone has asked for variance, and every
override it permits re-opens the compatibility matrix that "One stack per
release" closes. If that variance ever becomes a real requirement, the shape is
sketched under [future improvements](#future-improvements), and this design
keeps its seams: the per-cloud Python lists are a class's content without the
object, and a hand-written stack file is exactly what a hand-authored class
would hold.

**Run AICR at reconcile time.** A control plane that tracked the catalog without
a release would pick up NVIDIA's fixes sooner. AICR's client is Go and
composition functions are Python, so it means a subprocess or a sidecar in the
reconcile path. It also makes the stack on a cluster a function of when that
cluster last reconciled, rather than which release it's on. And it would weld an
NVIDIA-only toolchain into a loop that must work air-gapped: AICR has no AMD
recipes, and feeding it private ones is a heavier lift than hand-writing a
stack file.

**Have AICR emit Modelplane's modules.** A deployer alongside Helm, Argo CD and
Flux would delete most of the generator. It would also put Modelplane's schema
in AICR's Go, so every field rename becomes a cross-repository review cycle, and
it only covers the three clouds AICR knows about. Worth contributing once the
intermediate format has held still for a couple of releases, not before.

**Keep hardcoding, and automate the bumps.** A dependency bot on the chart pins
addresses staleness without a catalog or an overlay. It leaves Modelplane
choosing the values, validating them once per recipe, and with no answer when
two charts move in ways that only work together.

## Future improvements

Everything above is fixed at build time and exposes nothing, and the summary
names the four limits that buys: no GPU driver setting, no overrides, no
registry override, upgrades that ride releases. None of the items here is planned work. Each is what the
design would do if a real requirement lands against one of those limits — each
loosens exactly one of them, and each is additive, because the seams it needs
already exist in this design.

**Per-accelerator GPU drivers.** The driver is not really a singleton. The GPU
Operator's `NVIDIADriver` CR supports multiple driver versions in one cluster,
each targeting nodes by selector, and it already groups GPU nodes into pools by
OS and kernel. AICR's hardware layers pin drivers per accelerator, and taking
one pin for a mixed cluster discards information NVIDIA produced deliberately:
if the H100 recipe pins 580 and the L40S recipe pins 570, one shared pin means
one pool runs a driver nobody validated it with. Nor is "newest" safe — the
operator's support matrix has floors, holes and feature exclusions no `>=`
reasons about. A fan-out generator would have one rule to follow, **reduce
whole solutions, not fields**: shared values must agree across recipes or fail
closed, per-driver values fan out with the accelerator they were solved for.
Because the driver lives in generated values rather than API, this would be a
change to the generator and the generated files, with no API break. The upstream half is filed as
[NVIDIA/aicr#2516](https://github.com/NVIDIA/aicr/issues/2516): per-accelerator
driver versions in the catalog, in place of the global pin.

**A class-based runtime API.** If per-cluster variance ever becomes a
requirement, the per-cloud lists could become objects: a cluster-scoped
`InferenceClusterClass` carrying `supports` (service, os, accelerators,
fabrics) and the component set, resolved from pool facts with refusal rather
than fallback when a pool falls outside every class. That is what would answer
the release-cadence problem this design accepts: a platform team on hardware
the generated matrix doesn't cover hand-authors a class, or binds one
explicitly with the unsupported fact reported, instead of waiting for a
Modelplane release. It is also where per-cluster policy (`managementPolicy:
Existing | Disabled`, values overrides outside managed paths) would live, with
a cluster that overrides reporting its deviation and dropping its
qualification claim.

The shape, abridged as a generated `eks-ubuntu` class:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceClusterClass       # cluster-scoped; generated or hand-authored
metadata:
  name: eks-ubuntu
  annotations:                    # provenance: generator version and the
    modelplane.ai/generator: aicr@0.20.0   # digested recipes it resolved
spec:
  supports:                       # the facts resolution matches pools against
    service: [eks]
    os: [ubuntu]
    accelerators: [h100, h200, gb200, rtx-pro-6000]
    fabrics: [None, EFA]
  components:
  - name: aws-efa-k8s-device-plugin
    enabledWhen: pools.exists(p, p.fabric == "EFA")   # CEL over pool facts
    placement: [gpu]              # which pools its workloads land on
    namespace: kube-system
    chart:
      repository: https://aws.github.io/eks-charts
      name: aws-efa-k8s-device-plugin
      version: v0.5.29
  - name: gpu-operator
    dependsOn: [node-feature-discovery, cert-manager]
    placement: [gpu]
    namespace: gpu-operator
    chart:
      repository: https://helm.ngc.nvidia.com/nvidia
      name: gpu-operator
      version: v26.3.3
    values: {...}                 # merged values, as in a generated file
    valueOverlays:                # conditional values, keyed on pool facts
    - when: pools.exists(p, p.accelerator in ["gb200", "gb300"])
      values:
        cdi: {enabled: true}
    perAccelerator:               # the driver fan-out, one solved set per GPU
      h100:
        driver: {version: 580.173.02, useOpenKernelModules: true}
      gb200:
        driver:
          version: 580.173.02
          useOpenKernelModules: true
          kernelModuleConfig: {name: nvidia-kernel-module-params}
  # ... NFD, monitoring, the DRA driver, in the same shape.
```

A cluster would bind the one class whose `supports` covers every pool's
declared facts (`service`, `os`, `accelerator`, `fabric`), refuse with the
unsupported fact reported when none does, and take an explicit `classRef` as
the override. The hand-written stack files are what hand-authored classes would
hold — the components list is a class's content without the object — and the
`__init__.py` join is what `supports` resolution replaces.

**`os` and `fabric` as declared facts.** `InferenceClass` implicitly picks a
node image and a fabric today, and no field says so. Declaring `os` and
`fabric` on the pool class is what lets anything — a generator, a class
resolver, an admission check — key on them, and fabric alone is a half-truth on
providers like Nebius where interconnection needs a named regional fabric
domain and per-pool membership fixed at provisioning. If multi-pool
interconnect ever becomes a requirement, the natural shape is a fabric domain
on `InferenceCluster`, membership on `nodePools[]`, and one pool in one
domain.

**Non-NVIDIA accelerators.** Already possible in this design as a hand-written
stack file — AMD's DRA driver (`ROCm/k8s-gpu-dra-driver`, publishing
`gpu.amd.com`) in place of NVIDIA's, NFD beside it, prototyped as
`stacks/amd.py` beside this document. What's missing is everything around it:
membership checks so an AMD pool on an NVIDIA stack is a reported refusal
rather than a wrong install, and an upstream ask — external overlay sources for
AICR's engine — so AMD becomes data resolved by the same machinery rather than
a second pipeline.

**Continuous validation.** `aicr validate` deploys AICR's own health,
performance and conformance checks against a provisioned cluster. Running it
per release for the coordinates the generator produced, or AICR's snapshot
agent as a Job on managed clusters with validate results surfaced as
conditions, would turn the qualification claim from a build-time assertion
into a continuous one, and give BYO clusters a real admission check instead of
a documentation caveat. It puts a Job in the reconcile path and deserves its
own document, as the BYO section notes.

**Staged rollout and pinning.** The bluntest limit is that an upgrade reaches
every cluster at once. If a real requirement for holding a cluster back ever
lands, the mechanisms are known — immutable stack revisions with an update
policy, or the class `classRef` seam — and both re-open the compatibility
matrix, so they wait for someone to actually need them and to say how much of
that matrix they're willing to test.

**Upstreaming.** Three asks, in ascending size. A platform-less inference leaf,
so consumers not adopting Dynamo or NIM have a clean coordinate to resolve —
cheap for NVIDIA and useful to llm-d, KServe and AIBrix integrators too.
External overlay sources, so third-party and private catalogs resolve through
AICR's engine. And ultimately `aicr bundle --deployer modelplane`, which would
delete most of the generator — worth offering once the intermediate format has
held still, since emitting Modelplane's schema from NVIDIA's repository while
it moves puts a cross-repo review cycle in front of every rename.
