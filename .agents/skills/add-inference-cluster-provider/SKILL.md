---
name: add-inference-cluster-provider
description: Add a new cloud as a provisioned InferenceCluster source in the Modelplane repo (modelplaneai/modelplane) - a <Cloud>Cluster XR, a compose-<cloud>-cluster Python function, dispatch in compose-inference-cluster, the serving-stack cloud half, and docs. Use when asked to "add <cloud> as an inference cluster provider", "support <cloud> for cluster provisioning", "new InferenceCluster source", or "provision clusters on <cloud>". Not for bring-your-own clusters (source Existing) or for changing an existing provider.
---

# Add a provisioned InferenceCluster provider

You are adding `source: <Cloud>` to `InferenceCluster` so Modelplane provisions
the whole cluster on a new cloud. The rules come from how the existing
providers are built and what review asked for when they were added. Read
the code on `main` for patterns, not the PRs that introduced it: a PR's
earlier revisions contain code review rejected.

Read `CONTRIBUTING.md` first (AGENTS.md requires it). Its commit, PR, test and
writing-style rules override anything here.

## Pick the closest reference, don't copy one blindly

Every existing provider differs. Decide each row from your cloud's facts, then
copy from the function that made the same choice:

| Decision | Options in the repo today |
|---|---|
| Kubeconfig auth | the kubeconfig alone (AKS, Vultr, Civo client certs; EKS via ClusterAuth) / identity layered on the ProviderConfig (GKE, Nebius) |
| Dependents gating | one pass, children reference the cluster by selector (AKS, EKS, GKE, Nebius) / gated on cluster `Ready` with `_dependents_observed()` because they need an observed ID (Vultr, Civo) |
| Autoscaling | server-side per pool (AKS, GKE, Nebius) / in-cluster cluster-autoscaler release (EKS, Civo) / fixed pools |
| GPU stack | generated from AICR (EKS, AKS, GKE) / hand-written half: driver in image (Nebius, UpCloud), managed GPU Operator add-on (Vultr), nothing pre-installed (Civo) |
| GPU metrics | DCGM exporter from the GPU operator (EKS, AKS, GKE, Civo) or the managed add-on (Vultr) / standalone `dcgm-exporter` chart when the driver is in the image (UpCloud) / gap stated (Nebius, which predates the standalone chart) |
| ModelCache RWX | managed FS + `modelplane-rwx-fs` StorageClass (EKS EFS, GKE Filestore, AKS Azure Files, Nebius shared FS) / none (Vultr, Civo, UpCloud) |
| Fabric | EFA (EKS), InfiniBand (AKS, Nebius) / none (GKE, Vultr, Civo, UpCloud) |
| Gateway LB | a plain LoadBalancer Service (most) / Service annotations through `spec.gateway.serviceAnnotations` because the cloud controller needs an explicit LB name or TCP mode (UpCloud) |

## Phase 0: Facts before code

Don't write code until each of these has an answer. Research the provider's docs
and the Crossplane provider's CRDs; ask the user for anything you can't confirm.
Interactive: ask. Unattended: state the assumption and continue, or stop if a
wrong guess would change the API.

1. **Issue.** Modelplane is discuss-first; the PR says `Fixes #N.`
2. **Crossplane provider.** Exact package and version on the Upbound
   Marketplace, and its source repo. Link that repo in the docs, not a
   similarly named one from another vendor.
3. **Managed resources** needed, and their namespaced `*.m.upbound.io` CRD
   plurals. Which ones reference the cluster through a selector, and which need
   an observed ID or the cluster to be Ready?
4. **Credentials.** Connection secret content (client certs vs endpoint + CA)
   and the ProviderConfig secret format.
5. **GPU stack on nodes.** Driver in image? Managed GPU Operator add-on?
6. **Autoscaling.** Server-side, in-cluster, or none?
7. **ModelCache RWX.** A managed ReadWriteMany filesystem usable from GPU nodes?
   If not, ModelCache is left out (see "Optional capabilities").
8. **Fabric.** InfiniBand/RDMA or EFA-like? If not, not mapped.
9. **Versions and sizes.** Newest Kubernetes version (DRA needs 1.34+; CPU
   pools claim cores as consumable capacity, on by default from 1.36,
   DRAConsumableCapacity); GPU sizes, a CPU plan for CPU pools, and regions
   for examples.
10. **Node disks.** Is the boot disk bundled with the plan or unbundled from
    it, and is the default big enough for an inference serving image? UKS's
    default GPU plan disk wasn't, and a full disk evicts every pod on
    ephemeral-storage pressure. Unbundled → a `diskSizeGb` pool field mapped
    to the plan's storage block, with the cloud's SSD tier.
11. **Load balancers.** How the cloud controller names a LoadBalancer
    Service's LB — UpCloud truncates `<cluster UUID><namespace><name>` to 64
    chars with no unique suffix, so Modelplane's same-prefixed gateway
    Services collide — and whether its frontends pass TLS through (UpCloud
    hardcodes HTTP mode, which resets the gateway's TLS handshake; it needs
    TCP). Both are fixed with Service annotations through
    `spec.gateway.serviceAnnotations` (Phase 3).
12. **Provider and cloud constraints.** Name length and charset, create-only
    fields, conditional rules in the generated models' docstrings
    (`Required if …`, `Conflicts with …`). Record what you checked; "checked,
    found nothing" is a result.

## Phase 1: Add the dependency (commit 1)

1. `crossplane-project.yaml`: add the provider under `spec.dependsOn`, in the
   existing order.
2. `nix run .#build` (or `./nix.sh run .#build`) regenerates `schemas/python/`
   and `schemas/.lock.json`.
3. `git status` must show only `crossplane-project.yaml`, `schemas/.lock.json`
   and new files under `schemas/python/models/io/upbound/`. Anything else means
   a stale tree; find out why before committing.
4. `git commit -s -m "Add provider-<cloud> to the project dependencies"`. A
   schema regen is the one commit CONTRIBUTING lets go without a body.

## Phase 2: The `<Cloud>Cluster` XR and its function (commit 2)

Files:

- `apis/<cloud>clusters/definition.yaml`: `apiextensions.crossplane.io/v2`,
  group `infrastructure.modelplane.ai`, `scope: Namespaced`, categories include
  `infrastructure` (that places it in the API reference). No `shortNames`.
- `apis/<cloud>clusters/composition.yaml`: `apiextensions.crossplane.io/v1`,
  one Pipeline step, `functionRef.name: modelplane-modelplanecompose-<cloud>-cluster`.
- `functions/compose-<cloud>-cluster/`: `pyproject.toml`, `function/__init__.py`
  and `function/main.py` (boilerplate, copy with the license header),
  `function/fn.py`, `tests/test_fn.py`.
- `flake.nix` `functionNames` and the `crossplane-project.yaml` Tarball entry,
  both alphabetical.
- `uv.lock`: `nix develop -c uv lock` adds the workspace member.
- `nix run .#build` again for the new XRD's model.

**Keep the XR API minimal.** Fields are what a user must choose; operator-owned
values (CNI, system pool, chart pins, add-on removal) are constants in `fn.py`.
Every extra field is API you support forever. The usual spec: `region`,
optional `kubernetesVersion` (default the newest, or unset when the cloud wants
exact build strings), `credentials` (`ProviderConfig|ClusterProviderConfig`,
default `ClusterProviderConfig/default`), `nodePools[]` with `role`
(`System`, `GPU`, `CPU`), size, `nodeCount`, optional
`minNodeCount`/`maxNodeCount`, `diskSizeGb` when the cloud unbundles disks
from the plan, `gpu` required for GPU pools via CEL, and `name != 'system'`
via CEL — `system` is the injected system pool's name on every provisioned
cluster (the same rule guards InferenceCluster's `nodePools`). Status:
`secrets[]` (`type: Kubeconfig`), `cache` only if the cloud has RWX storage,
plus whatever observed identifiers Phase 0 item 11 needs (UpCloud publishes
`status.clusterId` for account-unique LB names).

**Test first.** Write the case table before `fn.py` and watch `nix run .#test
-- compose-<cloud>-cluster` fail for the reason you intend (missing resource,
wrong field). An import error or a broken case is not RED. Then implement until
it passes. Follow CONTRIBUTING's Tests section exactly;
`hack/check_function_tests.py` rejects departures. Case names carry no cloud
prefix here. Cover every input shape and observed-state branch your function
has: always `FirstPass` and `CustomCredentials`, a ready state
(`ResourcesReady` or `ClusterReady`), and one case per branch you add (Ready
regression if you gate, fixed vs autoscaled pool, a CPU pool, the autoscaler
removed after its release was observed, observed IDs, GPU observer, cache,
fabric, credential fallback).

**`fn.py`:**

- Module docstring stating the cloud's real constraints: credentials,
  autoscaling, GPU stack, cache present or absent and why. Comments must give
  the real reason and describe only what exists (`modelplane.ai/pool` pins
  pods to pools; `modelplane.ai/gpu` doesn't, whatever older comments say).
- Managed resources set `forProvider` and what the project needs: explicit
  `providerConfigRef` from `spec.credentials`, `writeConnectionSecretToRef` on
  the cluster so its kubeconfig reaches the function and the ProviderConfigs.
  A missing connection detail never falls back to a default.
- Prefer selectors (`*IdSelector: matchControllerRef`) over reading another
  resource's status. Gate on cluster `Ready` only where a child needs an
  observed value; then keep gated children once observed
  (`_dependents_observed()`), or a Ready regression deletes node pools.
- Composition keys (`cluster`, `node-pool-<name>`, …) become resource names;
  pick them once, renaming later is a migration.
- In-cluster resources (Helm releases, Objects) are orphan-managed
  (`managementPolicies` without `Delete`) so teardown doesn't wedge on a
  deleted kubeconfig. Autoscaled pools: seed `nodeCount` through
  `initProvider` and leave `LateInitialize` out, or Crossplane fights the
  autoscaler.
- Orphan-managed also means removal from the spec can't just drop the
  resource from desired state: once the cluster-autoscaler release is
  observed, keep composing it even after `spec.autoscaler` is gone —
  neutralized (empty node group list renders no Deployment) — or the orphaned
  autoscaler keeps scaling its old groups against the `nodeCount` Crossplane
  now enforces on the fixed pools.
- GPU pools carry `modelplane.ai/pool` and the GPU taint the serving stack
  tolerates (the functions use `nvidia.com/gpu=true:NoSchedule`; check whether
  the cloud adds one itself, as GKE does). The DRA driver is the only GPU
  allocator. CPU pools carry `modelplane.ai/pool` plus the `modelplane.ai/cpu`
  label (dra-driver-cpu selects its nodes by it) and taint.
- Optional status fields are assigned only when observed; an explicit `None`
  serializes as a null tombstone under `exclude_unset`.
- Implicit fallbacks emit `response.warning`.
- Before finishing, check every Kind you compose against Phase 0 item 12.

Commit: imperative subject, no `feat:` prefix, body leading with the problem,
`-s`. Example: `Add CivoCluster to provision Civo Kubernetes clusters`.

## Phase 3: Wire into InferenceCluster and the serving stack (commit 3)

`git grep -n -i "<newest existing cloud>"` over `apis/` and the shared functions
is your checklist: every hit is a place the new cloud likely belongs.

**`apis/inferenceclusters/definition.yaml`**: add `<Cloud>` to
`spec.cluster.source`, a CEL rule `self.source != '<Cloud>' || has(self.<cloud>)`,
and a `<cloud>` block with `region`, `kubernetesVersion` and **`credentials`**.
Every provisioned source has one; without it users can only set credentials
by creating the `<Cloud>Cluster` XR directly.

**`apis/inferenceclasses/definition.yaml`**: `<Cloud>` in
`spec.provisioning.provider`, the matching CEL rule, and a `<cloud>` block:
size or machine type, `diskSizeGb` when the cloud unbundles disks, and
optional `accelerator {type, count}` described as provisioning input only
(the scheduler matches `spec.devices`). Whether `accelerator` is set decides
whether the pool is provisioned GPU or CPU, so copy the paired CEL rules
every cloud has: `accelerator` required when a device claims a DRA driver
other than `dra.cpu`, and forbidden when none does.

**`apis/servingstacks/definition.yaml`**: `<Cloud>` in `spec.cloud`.

**`functions/compose-inference-cluster/function/fn.py`**:

- Import the model, extend the `Cloud` Literal, add `CLUSTER_SOURCE_<CLOUD>`,
  and the cloud's entry in the `_SUPPORTED` drivers map (usually
  `{"gpu.nvidia.com", "dra.cpu"}`).
- `_ACTIVATE_<CLOUD>`: every MR CRD the cluster function composes (the
  per-cloud activation policy; `apis/mrap.yaml` is not touched).
- Dispatch branch and `compose_<cloud>()`: activation gate →
  `compose_<cloud>_cluster()` → ClusterProviderConfig from the kubeconfig
  secret (plus identity if the kubeconfig lacks client certs) → ServingStack →
  Usage blocking cluster deletion until the ServingStack is gone → status and
  conditions.
- `compose_<cloud>_cluster()` maps each pool's class provisioning block; the
  class's `accelerator` presence decides the pool's role (`GPU` vs `CPU`); a
  missing block sets `ClusterReady=False`/`InvalidNodePool` and warns.
  Autoscaling bounds only when set; no fabric → say so in a comment.
- If Phase 0 item 11 found the cloud's LBs need annotations: a
  `<cloud>_gateway_service_annotations()` helper builds them from the cluster
  XR's observed status (UpCloud: `{"name": "<clusterId>-{gateway}",
  "frontends": [{"mode": "tcp"}]}` in the LB config annotation), set on both
  the ServingStack's `spec.gateway.serviceAnnotations` and the
  InferenceCluster's `status.gateway.serviceAnnotations` — published
  independently of, and before, the gateway address, so
  compose-inference-gateway has them before any Service exists. Downstream
  (compose-serving-stack's per-gateway EnvoyProxy, compose-inference-gateway)
  replaces `{gateway}` with each Gateway's name; that plumbing already
  exists, you only write the helper.
- Secret helpers (`resolve_…_backend_secrets`, `observed_…_secrets`,
  `observed_…_secret`).
- Cache: see "Optional capabilities".

**Serving stack.** Read `design/serving-stack-generation.md` first; it has the
full rules.
In short: a new cloud is a hand-written cloud half,
`functions/compose-serving-stack/function/stacks/clouds/<cloud>.py`, joined
with `common.py` and the stack file (`standard.py` / `dynamo.py`). You don't
touch those, the generator, or `generated/aicr/` (EKS, AKS, GKE only). The
half supplies the hardware surface the stack contract needs: cert-manager,
kube-prometheus-stack, NFD unless the cloud runs one, the GPU Operator only
if the cloud doesn't provide a driver, `nvidia-dra-driver-gpu` with the
right `nvidiaDriverRoot`, and a DCGM exporter — the operator's or the cloud's
where one exists, else the standalone `dcgm-exporter` chart as `upcloud.py`
has it (the shared `DCGM_METRICS_CSV` in `customMetrics`, `kubernetesDRA`
enabled so series attribute to DRA-allocated pods, the GPU taint toleration,
the DRA kubelet plugin's PCI affinity terms, no ServiceMonitor). Empty
`modelplane_gpu_*` series is not an acceptable default; Nebius's gap predates
the standalone chart. GPU-only charts (NFD, the DRA driver, the exporter)
carry `dra_drivers=["gpu.nvidia.com"]` so they stay off clusters whose
classes claim no GPUs, and the half declares `EXTERNAL_DRIVERS` (drivers
arriving outside the stack, usually empty). Then:

- `stacks/components.py`: extend the `Cloud` Literal.
- `stacks/__init__.py`: import the module, add it to `_CLOUDS` and the
  supported-drivers table (it folds in `EXTERNAL_DRIVERS`).
- `apis/servingstacks/definition.yaml`: `<Cloud>` in `spec.cloud`.
- `fn.py`: no cloud branch unless the cluster projects per-cluster state into
  the ServingStack (Civo's `spec.gpu` NVLink pools). Add that only with a
  field on the ServingStack XRD and a transform in the cloud's module.
- Tests: the parametrized invariants in `tests/test_stacks.py` pick the cloud
  up through `stacks.clouds()` automatically; add a `SupportedDriversCase`
  there, plus literal cases for anything the half pins by hand (UpCloud
  asserts the exporter's CSV, affinity terms and `kubernetesDRA` as
  literals, so a drive-by change fails in review rather than on a cluster),
  and `<Cloud>Standard`/`<Cloud>Dynamo` in `COMPOSED_RESOURCE_KEYS_CASES`
  and the per-cloud cases in `tests/test_fn.py`.
- **e2e leg.** Add the cloud to `e2e/clouds/__init__.py` `CLOUDS`: the fake
  GPU nodes' labels (what the cluster function stamps, plus the NFD and OS
  release labels the GPU operator reads — take the OS release values from a
  live node of the cloud, not a guess), `cpu_labels` and `dra_drivers` for
  the CPU pool, the GPU taint value, and the DaemonSets that must fan out to
  the device pools (including the DCGM exporter's). Add the cloud to
  `COMPOSITIONS` in `e2e/test_legs.py` (pins the leg's taint to the
  function's `_GPU_TAINT*` constants) and to the matrix in
  `.github/workflows/e2e-clouds.yml`. Run `nix run .#e2e -- --cloud <cloud>`:
  it installs the real serving stack on KWOK-faked GPU nodes with no cloud
  credentials, and catches toleration and placement mistakes before anyone
  spends money on a cluster.

**Tests**, test first as in Phase 2: in `compose-inference-cluster` the table
covers every cloud, so prefix names with the new one: `<Cloud>FirstPass`,
`<Cloud>Credentials`, `<Cloud>Ready`, `<Cloud>CPUPool`. In
`compose-serving-stack`: `<Cloud>Standard` and `<Cloud>Dynamo`.

Then `nix run .#build`, `nix run .#fix`, `nix flake check`. Example commit:
`Provision InferenceClusters on Civo`.

## Phase 4: Docs (commit 4)

- `docs/content/platform/providers.md`: provisioning column ✓, `repolink` to
  the provider actually used, cloud added to the "Provisioning supported"
  sentence.
- `docs/content/platform/inference-cluster.md`: source lists, a tab for
  `concepts/inference-cluster-<cloud>.yaml`, the ModelCache section.
- `docs/content/platform/inference-class.md`: tab for
  `concepts/inference-class-<cloud>-<gpu>.yaml`.
- `docs/content/getting-started/build-the-platform.md`: prerequisites,
  credentials (secret + `clusterproviderconfig-<cloud>.yaml`), platform (with
  a realistic wait), gateway tabs.
- `docs/content/getting-started/deploying-a-model.md`: a tab for
  `getting-started/<cloud>/model-deployment.yaml`. Vultr and Civo don't have
  one yet, so their getting-started path stops at the gateway.
- Manifests under `docs/manifests/`: `concepts/inference-cluster-<cloud>.yaml`,
  `concepts/inference-class-<cloud>-<gpu>.yaml`,
  `getting-started/<cloud>/platform.yaml`,
  `getting-started/<cloud>/inference-gateway.yaml`,
  `getting-started/<cloud>/model-deployment.yaml`,
  `getting-started/clusterproviderconfig-<cloud>.yaml`,
  `reference/<cloud>clusters.yaml`. The `docs-manifests` check validates them.
- Re-read every manifest comment after review changes. The Civo
  `clusterproviderconfig-civo.yaml` still describes an `api-key` the
  autoscaler copies, which review removed from the code.
- New terms into `docs/utils/vale/styles/config/vocabularies/Modelplane/accept.txt`.

`nix flake check` again.

## Phase 4b: An apply-able example

Give people one folder they can apply to try the provider end to end. Build
`examples/<cloud>/` at the repo root, modelled on an existing cloud's kit:

- `00-credentials.sh`: creates the provider's credentials Secret from an
  environment variable, in the format the provider parses. No secret in a file.
- `01-clusterproviderconfig.yaml` … `05-model.yaml`: numbered, so
  `kubectl apply -f examples/<cloud>/` applies them in order. Same content as
  the docs manifests, with the smallest GPU size that runs the 0.5B model.
- `test.sh`: waits for cluster, gateway and deployment Ready, then sends one
  chat completion through the gateway.
- `teardown.sh`: model, gateway, cluster, with `--cascade=foreground`.
- `README.md`: prerequisites, apply, expected wait, cleanup, and what the
  cloud doesn't support (cache, fabric, autoscaling).

The repo's `.gitignore` excludes the top-level `/examples/` (local demo
manifests may hold real credentials), so this folder is the user's local kit
for validation and isn't committed. The committed, published copies are the
`docs/manifests/getting-started/<cloud>/` files above, which the docs render
with `kubectl apply -f <url>`. Keep the two in step, and validate the kit's
Modelplane manifests against the generated models the same way the
`docs-manifests` check does.

## Optional capabilities: leave out what the cloud lacks

Ship what the cloud genuinely supports, declare the gap, don't paper over it
with a self-hosted substitute.

**ModelCache.**
- *Available*: compose the filesystem if it isn't on-demand, and the
  `modelplane-rwx-fs` StorageClass as an orphan-managed Object with
  `readiness.policy: SuccessfulCreate`; `status.cache.storageClassName` in the
  XRD and published; the cloud added to the `provisioned` map in
  `cache_storage_class()` and the union in `_observed_cluster_cache_class()`;
  listed as auto-provisioned in `inference-cluster.md`.
- *Not available*: no `status.cache`, StorageClass, CSI driver, Longhorn or
  similar. Add the cloud to the "absent" sentence in `cache_storage_class()`'s
  docstring and the "none" bullet in `inference-cluster.md` with the concrete
  reason, and state it in the PR. A substitute is a separate design with a
  measured bar (N engines, model size S, ready within T).

**Fabric.** Map only if the cloud has it; otherwise a comment that it isn't
mapped, and no XRD field.

**Autoscaler.** Server-side → the pool's autoscaling fields. None → the
upstream cluster-autoscaler Helm release if it supports the cloud,
orphan-managed — which means a removed `spec.autoscaler` keeps the observed
release composed with an empty group list rather than dropping it (see the
fn.py rules in Phase 2). Neither → fixed pools, stated in the docstring and
docs.

**GPU readiness gate.** Only when the cloud pre-installs the GPU operator.

## Phase 5: Verify and report

Name the layer you reached and claim nothing beyond it:

1. `nix flake check` (lint, types, unit tests, docs manifests, Vale): the
   function renders what the tests assert.
2. `nix run .#e2e -- --cloud <cloud>`: the new serving-stack half installs
   on KWOK-faked GPU nodes shaped like the cloud's, and every GPU DaemonSet
   lands on them. No cloud resources, no credentials. This is the layer CI's
   `E2E Clouds` workflow runs.
3. `nix run .#run` on a local kind control plane: the package installs and the
   XRs are accepted. No cloud resources.
4. A real cloud account: only the user runs this, with the Phase 4b kit
   (`00-credentials.sh`, `kubectl apply -f .`, `test.sh`, `teardown.sh`).
   Never create cloud resources yourself. Ask for the trace and the served
   request to put in the PR.

Report the commands with their own exit codes, which Kinds you checked against
provider rules, and what remains unverified.

## Phase 6: PR

Follow CONTRIBUTING: from the user's fork, `Fixes #N.` first, then the problem,
the change, and the cloud-specific differences (GPU stack, autoscaling, cache,
fabric, credentials). Breaking changes stated plainly. End-to-end validation
only if the user ran it; never invent results. No hard-wrapped body.

Before handing off, re-verify every phase's output and apply the
`reviewing-pull-requests` skill's standards to your own change.

Main moves while the PR is open. After every rebase, re-run the Phase 3
`git grep` parity sweep over the newest existing cloud: UpCloud had to
retrofit CPU pools, `dra_drivers` chart tags and a new common component when
a platform PR merged mid-review. A branch that merely still compiles after a
rebase is not yet at parity.

Fold review fixes into the commit they belong to and force-push. No "address
review comments" commits, no `feat(...)` prefixes.
