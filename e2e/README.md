# Local end-to-end test (no cloud, no GPU)

Exercise the full Modelplane path — publish capacity, register a cluster, deploy
a model, route a request through the InferenceGateway — on local `kind`
clusters, with **no cloud provider and no GPU**.

This is the integration layer. The composition functions run in-cluster against
real providers, so it catches failures unit tests can't: missing RBAC,
cross-cluster routing, CLI parsing, provider readiness gating, and teardown
ordering. It needs no cloud or registry credentials, only public images — so it
can gate a merge where the cloud e2e can't.

It uses **two clusters**, mirroring a real deployment:

- a **control-plane** cluster (crossplane + the Configuration), managed by
  `crossplane project run`;
- a **workload** cluster registered via `source: Existing`, where the serving
  stack, both gateways and the model run.

Two clusters rather than one because that's the shape Modelplane is for: a
control plane that installs nothing on itself, and clusters that run everything.
Both gateways live on the workload cluster: the InferenceGateway as the front
door and the cluster gateway fronting the engines. The control plane runs only
Crossplane and the providers.

Two Modelplane primitives make it cloud-free:

- **`source: Existing`** — the `InferenceCluster` registers a bring-your-own
  cluster (the workload kind cluster) via a kubeconfig Secret instead of
  provisioning EKS/GKE/Nebius.
- **`claim: DRA` + a fake GPU driver** — the `InferenceClass` advertises a
  `gpu.example.com` device backed by the **dra-example-driver**
  (kubernetes-sigs), which publishes fake GPUs with **no real hardware**. The
  engine's `ResourceClaim` binds a fake device on a GPU-less node, so the *real*
  DRA allocation path runs. (A claimable device is required: the fleet scheduler
  rejects an engine whose only device is `Synthetic`.)

The engine is a **mock server** (a few lines of Python) that answers both
`/v1/chat/completions` (OpenAI) and `/v1/messages` (Anthropic), the way a vLLM
server exposes both, so the pod goes Ready without a real model or GPU.

## What this does and does not test

| Tested | Not tested |
|---|---|
| Fleet scheduling / placement (CEL vs declared capacity) | Real token generation (mock engine) |
| `ModelDeployment` → `ModelReplica` → `ModelEndpoint` → `ModelService` wiring | Real GPU drivers / CUDA (fake DRA devices only) |
| DRA `ResourceClaim` → fake device binding (the real allocation path) | Multi-node / disaggregated (`PrefillDecode`) serving |
| Serving-stack install on a real (BYO) workload cluster | Cloud provisioning (EKS/GKE/Nebius) |
| `InferenceGateway` + cross-cluster routing to the replica | Export to a real metrics backend (debug sink only) |
| Status propagation and foreground-deletion ordering | |
| Telemetry: collector composed, engine discovered, series renamed and attributed | |

### Telemetry

`60-telemetry.yaml` creates a `TelemetryDestination` with the collector's
**debug** exporter, which prints what reached it to the collector's own log. So
the whole path is assertable with `kubectl logs` and needs no metrics backend:
service discovery finds the engine by the labels `compose-model-replica` stamps
on serving pods, the built-in `MetricMapping`s rename its series, the unit
conversion runs, and the identity comes off the pod.

The mock engine serves `/metrics` with two real names — `vllm:num_requests_waiting`
and `DCGM_FI_DEV_FB_USED` — so `--verify` asserts they arrive as
`modelplane_requests_waiting` and `modelplane_gpu_memory_used_bytes`, that 1024
MiB became 1073741824 bytes, that each carries its deployment, engine, role and
cluster, and that the engine's own `vllm:` names did *not* leave the cluster.


### Why cloud provisioning cannot be tested here

`lean-control-plane.yaml` trims the control plane to what the BYO
(`source: Existing`) scenario needs. That's all the e2e runs, and it carries no
cloud credentials, so it never creates a cloud `InferenceCluster` and never
provisions a cloud cluster. This is what that looks like, and what to expect if
you point this control plane at a real cloud without adding one:

- The lean MRAP activates only `*.kubernetes.m.crossplane.io` and
  `*.helm.m.crossplane.io`, and nothing composes a cloud activation policy, so a
  cloud provider's managed resources are never activated. They sit with **no
  status conditions at all** — which reads as nothing happening rather than as
  an error.
- Because those providers declare the safe-start capability, Crossplane scales
  their controllers to zero while their managed resources are inactive, so a
  dormant cloud provider runs no pod at all.

A cloud `InferenceCluster` would compose its own additive policy activating its
cloud's kinds and wake that provider; the e2e just never creates one. Undoing
these trims is possible but leaves a control plane that is no longer the one CI
runs, so prefer a separate control plane for cloud work.

## Cloud serving stacks on kind (`--cloud`)

`nix run .#e2e -- --cloud eks|aks|gke|civo|nebius|vultr|existing` installs that
cloud's serving stack — the AICR-generated component lists for EKS/AKS/GKE, or
the hand-written halves for Civo, Nebius, Vultr and Existing — onto the same
workload kind cluster, with no cloud credentials. The trick is
[KWOK](https://kwok.sigs.k8s.io): bring-up applies its pinned `kwok.yaml` and
`stage-fast.yaml` manifests and two fake Nodes per device pool, labelled and
tainted exactly like that cloud's pools — an NVIDIA GPU pool and a CPU pool
on every leg, plus an AMD GPU pool on Existing, the one cloud that installs
the AMD stack. The real kind node plays the untainted system pool, so system
components install and run for real; each device stack's DaemonSets fan out
to their own pool's fake nodes, where KWOK marks their pods Running — which
is all helm `--wait` and the composition's readiness gates need.

The clouds are one data-driven matrix: each leg in `clouds/__init__.py` holds
everything cloud-specific — each fake pool's labels, the GPU taint value, the
stack's `spec.cloud`, the `spec.draDrivers` its classes would claim (which
filters the stack to exactly those device stacks), and the DaemonSets
expected to fan out to the pools. Adding fidelity for a cloud means editing
its leg, not a copy of the test.

A leg is itself a copy of what its cloud's cluster composition applies, so it
can drift from the composition as well as the stack from it. `e2e/test_legs.py`
pins each leg's taint value to the `_GPU_TAINT*` constants of the composition
it mirrors (GKE's managed taint and BYO pools' come from outside Modelplane),
and the shared CPU taint key to every composition's `_LABEL_CPU` — GKE
included, which sets the CPU taint itself where its GPU taint is managed.
It reads the compositions' source and needs no cluster, so it runs as the
`e2e-legs` check in `nix flake check` rather than here: a composition-only
change triggers the flake checks but not this workflow. The labels are a known
limitation: compositions build them per pool at compose time, so the legs'
copies have to be kept true in review.

The `ServingStack` XR is applied directly rather than through an
`InferenceCluster`, because `source: Existing` hard-wires `cloud: Existing`;
the XRD accepts exactly the shape the cluster compositions compose, so
`compose-serving-stack` is exercised end to end through real
provider-helm/provider-kubernetes. Bring-up also parks the composition
functions the ServingStack pipeline doesn't call at zero replicas, giving the
install the runner's memory.

Cloud mode always verifies: after bring-up, pytest runs `clouds/` against the
clusters (`--no-apply` stops short of the fake nodes and the XR, `--test`
reruns the tests alone). A session fixture waits for the `ServingStack` to go
Ready, then the asserts are two-sided:

- **DaemonSet fan-out**: the leg's expected DaemonSets (the NVIDIA DRA
  kubelet plugin, dra-driver-cpu, NFD worker, node-exporter; the AMD
  operands on Existing; toolkit-hardening on AKS) reach their ready counts —
  a toleration too *narrow* (e.g. the wrong `Equal` value on AKS), a
  selector missing its pool, or a filter that wrongly dropped a claimed
  stack shows up as a shortfall.
- **Placement/toleration audit**: no pod outside a pool's allowlist carries
  a wildcard or that pool's taint toleration, everything on a fake node
  belongs to that node's pool, and pool-only pods (the DRA drivers, the GPU
  operands) never sit anywhere else — a toleration too *broad* (the
  "tolerates every taint" class of bug) fails here even when the scheduler
  happened to keep the pod on the system node.
- **Unclaimed-stack absence**: the legs above claim every stack their cloud
  carries, so a join that ignored `spec.draDrivers` would pass them all.
  The `existing-filtered` leg is the exclusion side: same pools as
  `existing`, AMD fake nodes included, but `gpu.amd.com` left unclaimed —
  the AMD DaemonSets must not exist at all, even though the pool is there
  and Existing's list pins the stack.

What cloud mode does **not** test: AKS's admission rejection of wildcard
tolerations (the audit approximates it client-side), real DRA allocation and
driver/toolkit behaviour on GPU nodes (all faked by KWOK; the model e2e's
dra-example-driver path covers DRA allocation), the EKS EFA driver and GKE's
`nvidia-driver-installer` (installed by the cluster compositions/platform, not
the serving stack), and the model/inference path (covered by the model e2e).

## Prerequisites

- **Docker** with real headroom — **≥ 16 GB memory** and **plenty of disk**
  (raise Docker Desktop's disk-image size). Two kind clusters, 13 provider
  packages, the built function images, and the serving stack
  (kube-prometheus-stack et al.) add up fast; a full Docker disk surfaces as
  `no space left on device`. Reclaim between runs with `docker builder prune -af`
  and `docker image prune -af`.
- Everything else — `kind`, `kubectl`, the `docker` CLI, Python and pytest, and
  the `crossplane` CLI — is provided by the flake via `nix run .#e2e`.

The workload cluster is pinned to **k8s v1.34** (in `environment.py`) for the
`resource.k8s.io` (DRA) APIs, on-by-default in 1.34: both the serving stack's
NVIDIA DRA driver and the dra-example-driver register DeviceClasses, and the
example driver publishes the `ResourceSlice`s the engine's `ResourceClaim` binds
against. The control-plane cluster needs no DRA.

## Run

```bash
nix run .#e2e                  # bring up both clusters + deploy the mock model
nix run .#e2e -- --verify      # same, then run the tests
nix run .#e2e -- --test        # run the tests against clusters already up
nix run .#e2e -- --cloud eks   # one cloud's serving stack on KWOK-faked device pools (see below)
nix run .#e2e -- --clean       # tear both clusters down
```

Arguments after `--verify` go to pytest, so `nix run .#e2e -- --verify -k usage`
runs only the tests whose names match. Bring-up reuses clusters that are already
up. To rerun the tests against an environment that's up, without bringing it up
again, use `--test`:

```bash
nix run .#e2e -- --test -k usage
```

`crossplane project run` installs the config and applies the resources, then
returns; the serving-stack install and model rollout reconcile in the background.
So wait for the `ModelService` to become ready before curling. The gateway's
address is on the kind Docker subnet, which the host can't route to on macOS, so
curl from a pod on the control plane. The gateway authenticates callers, so send
the key from `manifests/10-inference-gateway.yaml`:

```bash
kubectl -n ml-team get ms mock -w          # wait for READY to be True
oai=$(kubectl get ig local -o jsonpath='{.status.endpoints.openAI}')
ant=$(kubectl get ig local -o jsonpath='{.status.endpoints.anthropic}')

# OpenAI Chat Completions
kubectl run curl -n ml-team --rm -it --image=curlimages/curl@sha256:7c12af72ceb38b7432ab85e1a265cff6ae58e06f95539d539b654f2cfa64bb13 -- \
  curl -s "$oai/chat/completions" -H 'authorization: Bearer sk-e2e-caller' \
  -H 'content-type: application/json' \
  -d '{"model":"ml-team/mock","messages":[{"role":"user","content":"hi"}]}'

# Anthropic Messages API, with the key in x-api-key
kubectl run curl -n ml-team --rm -it --image=curlimages/curl@sha256:7c12af72ceb38b7432ab85e1a265cff6ae58e06f95539d539b654f2cfa64bb13 -- \
  curl -s "$ant/messages" -H 'x-api-key: sk-e2e-caller' \
  -H 'content-type: application/json' -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"ml-team/mock","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}'
```

`--verify` runs the tests in `e2e/tests/`, which send both of those requests among
others, and exits non-zero if any fails. It's the exact command the `E2E` CI
workflow runs, so a green `--verify` locally and a green CI run mean the same
thing; use the manual curls above to poke the endpoints interactively.

## How it's structured

`nix run .#e2e` materialises the Nix-built function images and hands off to
`environment.py`, which does the cross-cluster orchestration that `crossplane
project run` flags can't express:

1. Create the **workload** kind cluster (pinned v1.34).
2. Install MetalLB on it (the serving stack doesn't) with a pool inside the
   detected kind subnet, install the **dra-example-driver** (fake GPUs), and
   label its node for the `gpu-synthetic` pool.
3. `crossplane project run` for the **control plane**, with
   `lean-control-plane.yaml` as `--init-resources` so the provider trims land
   before the providers install.
4. Finish the setup the getting-started flow does by hand (as the nix run app
   does since #375): `kubectl apply` the RBAC prerequisites, point provider-helm
   and provider-kubernetes at their DeploymentRuntimeConfigs, add the workload
   kubeconfig Secret (`kind get kubeconfig --internal`, reachable from
   control-plane pods over the shared kind network), then apply the
   Modelplane manifests.

Everything the control plane needs is a declarative manifest; `environment.py`
is only the irreducible cross-cluster setup (a second cluster, its MetalLB and
DRA driver, and the cross-cluster kubeconfig). With `--verify`, pytest then runs
the tests in `tests/`, one file per area. Fixtures in `conftest.py` start a curl pod on each
cluster to send requests from, and wait for the model to route and to serve. The
tests read the clusters through the Kubernetes API, with the official Python
client, while bring-up drives the kind, crossplane, docker and kubectl CLIs.

```
e2e/
  environment.py             # two-cluster bring-up and teardown
  kube.py, gateway.py        # Kubernetes API and curl helpers
  wait.py                    # polling until a condition holds
  tests/
    conftest.py              # fixtures: the clusters, curl pods, readiness
    test_auth.py             # the InferenceGateway refuses unknown callers
    test_routing.py          # it routes OpenAI and Anthropic requests
    test_serving.py          # requests and responses cross it intact, bad ones fail
    test_metering.py         # it logs a usage record per request
    test_cluster_gateway.py  # the cluster gateway requires a client certificate
    test_telemetry.py        # the engine's series reach the collector's sink
  clouds/                    # --cloud mode: cloud stacks on KWOK-faked device pools
    __init__.py              # the matrix: per-cloud node shape, taint, expected DaemonSets
    conftest.py              # fixtures: the clusters, the ServingStack Ready wait
    test_placement.py        # DaemonSet fan-out + placement/toleration audits
  dra-example-driver.yaml    # vendored fake DRA GPU driver (applied to workload)
  manifests/                 # applied to the control plane after setup
    00-namespaces.yaml
    10-inference-gateway.yaml
    20-inference-class.yaml
    30-inference-cluster.yaml # source: Existing -> the workload cluster
    40-model-deployment.yaml
    50-model-service.yaml
    60-telemetry.yaml         # a TelemetryDestination with a debug sink
```

## Why the extra moving parts

- **MetalLB on the workload cluster.** Both gateways run there, and both need
  `LoadBalancer` addresses kind can't provide: the serving stack gates the
  cluster gateway's readiness on having one (`READY_CEL` in its `gateway.py`).
  Nothing Modelplane composes installs MetalLB, so bring-up does, with a pool
  inside the detected kind Docker subnet (see caveat) so the control plane can
  route to the addresses it hands out.
- **Fake DRA driver.** A `claim: DRA` engine emits a `ResourceClaim`; with no DRA
  driver it stays Pending and the pod never schedules. Bring-up applies the
  vendored **dra-example-driver**, which publishes fake `gpu.example.com` devices
  so the claim binds on a GPU-less node.
- **Cross-cluster kubeconfig.** `source: Existing` needs a kubeconfig the
  control-plane provider pods can use to reach the workload API server.
  `kind get kubeconfig --internal` gives an address routable across the shared
  kind network; a host kubeconfig (`127.0.0.1:<port>`) wouldn't be.
- **Node label.** On a BYO cluster Modelplane doesn't provision/label pools, so
  bring-up labels the workload node `modelplane.ai/pool=gpu-synthetic` (matching
  `nodePools[].name`); without it worker pods stay Pending.

## Caveats / open questions

- **Cross-cluster networking uses the detected kind subnet.** Bring-up reads the
  `kind` Docker network's subnet (usually 172.18.0.0/16, but kind bumps to
  172.19/... when earlier networks already hold 172.18) and derives the
  workload cluster's MetalLB pool from it. A hardcoded 172.18 would leave the LB
  IPs off-subnet and the cross-cluster curl would time out.
- **Reconcile runs after the command returns.** `crossplane project run` waits
  for the config to install, then applies the resources and exits — it doesn't
  block on XR readiness. The serving-stack install (the long pole) and the model
  rollout happen after, so watch the `ModelService`'s `RoutingReady` rather than
  the command's exit. `--timeout` in `environment.py` bounds the build and config
  install.
- **Two DRA drivers on a GPU-less node.** The serving stack's **NVIDIA** DRA
  driver targets NFD-GPU-labelled nodes, so it sits at 0/0 (inert) yet its Helm
  release still reports Ready. The **dra-example-driver** bring-up installs is the
  active one — it publishes the fake `gpu.example.com` devices the engine binds.
- **Serving-stack weight.** cert-manager, Envoy Gateway, Envoy AI Gateway, GAIE
  CRDs, kube-prometheus-stack, LeaderWorkerSet, NFD, DRA driver — all on the
  workload node. Give Docker headroom.
- **Package version.** Installs the **current branch** build (via `crossplane
  project run`), not the published `v0.1.0`, because the `source: Existing` /
  DRA-on-BYO path may postdate that tag.
