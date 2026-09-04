# Local end-to-end test (no cloud, no GPU)

Exercise the full Modelplane path — publish capacity, register a cluster, deploy
a model, route a request through the control-plane gateway — on local `kind`
clusters, with **no cloud provider and no GPU**.

This is the integration layer. The composition functions run in-cluster against
real providers, so it catches failures unit tests can't: missing RBAC,
cross-cluster routing, CLI parsing, provider readiness gating, and teardown
ordering. It needs no cloud or registry credentials, only public images — so it
can gate a merge where the cloud e2e can't.

It uses **two clusters**, mirroring a real deployment:

- a **control-plane** cluster (crossplane + the Configuration + the
  `InferenceGateway`), managed by `crossplane project run`;
- a **workload** cluster registered via `source: Existing`, where the serving
  stack and the model run.

Two clusters rather than one because the control-plane `InferenceGateway`
(Traefik) and the workload `ServingStack` (Envoy) both install the Gateway API
CRDs — co-located on one cluster they race for the same cluster-scoped CRDs and
the gateway wedges (`encountered composed resource without required
composition-resource-name annotation`). Separate clusters, as in production,
avoid it.

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
| Control-plane `InferenceGateway` + cross-cluster routing to the replica | |
| Status propagation and foreground-deletion ordering | |

### Why cloud provisioning cannot be tested here

`lean-control-plane.yaml` trims the control plane to what a BYO cluster needs,
and both trims stop a cloud `InferenceCluster` from reconciling at all. Neither
announces itself, so this is what to expect if you point this control plane at a
real cloud:

- The MRAP activates only `*.kubernetes.m.crossplane.io` and
  `*.helm.m.crossplane.io`. A cloud provider's managed resources are then never
  activated, so they sit with **no status conditions at all** — which reads as
  nothing happening rather than as an error.
- The `dormant-cloud-providers` `ImageConfig` maps every
  `xpkg.upbound.io/upbound/provider-*` to a zero-replica runtime config. Editing
  that `ImageConfig` is not enough on its own: it is resolved when a package
  revision reconciles, so existing Deployments keep their replica count until
  something scales them.

Undoing both is possible but leaves a control plane that is no longer the one CI
runs, so prefer a separate control plane for cloud work.

## Prerequisites

- **Docker** with real headroom — **≥ 16 GB memory** and **plenty of disk**
  (raise Docker Desktop's disk-image size). Two kind clusters, 13 provider
  packages, the built function images, and the serving stack
  (kube-prometheus-stack et al.) add up fast; a full Docker disk surfaces as
  `no space left on device`. Reclaim between runs with `docker builder prune -af`
  and `docker image prune -af`.
- Everything else — `kind`, `kubectl`, `curl`, `git`, and the `crossplane` CLI —
  is provided by the flake via `nix run .#e2e`.

The workload cluster is pinned to **k8s v1.34** (in `run.sh`) for the
`resource.k8s.io` (DRA) APIs, on-by-default in 1.34: both the serving stack's
NVIDIA DRA driver and the dra-example-driver register DeviceClasses, and the
example driver publishes the `ResourceSlice`s the engine's `ResourceClaim` binds
against. The control-plane cluster needs no DRA.

## Run

```bash
nix run .#e2e              # bring up both clusters + deploy the mock model
nix run .#e2e -- --verify  # same, then wait for readiness and assert a live 200
nix run .#e2e -- --clean   # tear both clusters down
```

`crossplane project run` installs the config and applies the resources, then
returns; the serving-stack install and model rollout reconcile in the background.
So wait for the `ModelService` to publish an address before curling. That address
is on the kind Docker subnet, which the host can't route to on macOS, so curl
from a pod on the control plane:

```bash
kubectl -n ml-team get ms mock -w          # wait for ADDRESS to appear
addr=$(kubectl -n ml-team get ms mock -o jsonpath='{.status.address}')

# OpenAI Chat Completions
kubectl run curl -n ml-team --rm -it --image=curlimages/curl@sha256:7c12af72ceb38b7432ab85e1a265cff6ae58e06f95539d539b654f2cfa64bb13 -- \
  curl -s "$addr/v1/chat/completions" -H 'content-type: application/json' \
  -d '{"model":"mock","messages":[{"role":"user","content":"hi"}]}'

# Anthropic Messages API (same address; vLLM and the mock serve both)
kubectl run curl -n ml-team --rm -it --image=curlimages/curl@sha256:7c12af72ceb38b7432ab85e1a265cff6ae58e06f95539d539b654f2cfa64bb13 -- \
  curl -s "$addr/v1/messages" -H 'content-type: application/json' -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"mock","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}'
```

`--verify` runs both of those (OpenAI then Anthropic) and exits non-zero on
failure. It's the exact command the `E2E` CI workflow runs, so a green `--verify`
locally and a green CI run mean the same thing; use the manual curls above to
poke the endpoints interactively.

## How it's structured

`nix run .#e2e` materialises the Nix-built function images and hands off to
`run.sh`, which does the cross-cluster orchestration that `crossplane project
run` flags can't express:

1. Create the **workload** kind cluster (pinned v1.34).
2. Install MetalLB on it (the serving stack doesn't) with a pool inside the
   detected kind subnet and disjoint from the InferenceGateway's, install the
   **dra-example-driver** (fake GPUs), and label its node for the `gpu-synthetic`
   pool.
3. `crossplane project run` for the **control plane**, with
   `lean-control-plane.yaml` as `--init-resources` so the provider trims land
   before the providers install.
4. Finish the setup the getting-started flow does by hand (as the nix run app
   does since #375): `kubectl apply` the RBAC prerequisites, point provider-helm
   at its DeploymentRuntimeConfig, add the workload kubeconfig Secret (`kind get
   kubeconfig --internal`, reachable from control-plane pods over the shared kind
   network), then apply the subnet-templated Modelplane manifests.

Everything the control plane needs is a declarative manifest; the shell in
`run.sh` is only the irreducible cross-cluster setup (a second cluster, its
MetalLB and DRA driver, the cross-cluster kubeconfig).

```
e2e/
  run.sh                     # two-cluster orchestration
  dra-example-driver.yaml    # vendored fake DRA GPU driver (applied to workload)
  manifests/                 # applied to the control plane after setup
    00-namespaces.yaml
    10-inference-gateway.yaml
    20-inference-class.yaml
    30-inference-cluster.yaml # source: Existing -> the workload cluster
    40-model-deployment.yaml
    50-model-service.yaml
```

## Why the extra moving parts

- **MetalLB on both clusters.** Both gateways — control-plane Traefik and the
  workload Envoy Gateway (whose readiness the serving stack gates on,
  `_GATEWAY_READY_CEL`) — need `LoadBalancer` addresses kind can't provide. The
  `InferenceGateway` installs MetalLB on the control plane itself
  (`compose_metallb`, pool `.200-.250`); the serving stack does *not*, so `run.sh`
  installs MetalLB on the workload cluster with a **disjoint** pool (`.100-.149`).
  Both pools sit inside the detected kind Docker subnet (see caveat) so the
  control plane can route across it to the workload gateway's IP.
- **Fake DRA driver.** A `claim: DRA` engine emits a `ResourceClaim`; with no DRA
  driver it stays Pending and the pod never schedules. `run.sh` applies the
  vendored **dra-example-driver**, which publishes fake `gpu.example.com` devices
  so the claim binds on a GPU-less node.
- **Cross-cluster kubeconfig.** `source: Existing` needs a kubeconfig the
  control-plane provider pods can use to reach the workload API server.
  `kind get kubeconfig --internal` gives an address routable across the shared
  kind network; a host kubeconfig (`127.0.0.1:<port>`) wouldn't be.
- **Node label.** On a BYO cluster Modelplane doesn't provision/label pools, so
  `run.sh` labels the workload node `modelplane.ai/pool=gpu-synthetic` (matching
  `nodePools[].name`); without it worker pods stay Pending.

## Caveats / open questions

- **Cross-cluster networking uses the detected kind subnet.** `run.sh` reads the
  `kind` Docker network's subnet (usually 172.18.0.0/16, but kind bumps to
  172.19/... when earlier networks already hold 172.18) and derives both disjoint
  MetalLB pools from it — the workload pool directly, the InferenceGateway's by
  rewriting its manifest. A hardcoded 172.18 would leave the LB IP off-subnet and
  the cross-cluster curl would time out.
- **Reconcile runs after the command returns.** `crossplane project run` waits
  for the config to install, then applies the resources and exits — it doesn't
  block on XR readiness. The serving-stack install (the long pole) and the model
  rollout happen after, so watch the `ModelService` address rather than the
  command's exit. `--timeout` in `run.sh` bounds the build and config install.
- **Two DRA drivers on a GPU-less node.** The serving stack's **NVIDIA** DRA
  driver targets NFD-GPU-labelled nodes, so it sits at 0/0 (inert) yet its Helm
  release still reports Ready. The **dra-example-driver** `run.sh` installs is the
  active one — it publishes the fake `gpu.example.com` devices the engine binds.
- **Serving-stack weight.** cert-manager, Envoy Gateway, Envoy AI Gateway, GAIE
  CRDs, kube-prometheus-stack, LeaderWorkerSet, NFD, DRA driver — all on the
  workload node. Give Docker headroom.
- **Package version.** Installs the **current branch** build (via `crossplane
  project run`), not the published `v0.1.0`, because the `source: Existing` /
  DRA-on-BYO path may postdate that tag.
