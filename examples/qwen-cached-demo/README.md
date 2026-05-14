# Qwen + ModelCache cold-start demo

Idempotent, scripted end-to-end demo of `ModelCache` accelerating
serving cold-start. The demo deploys Qwen 2.5 0.5B **twice on the
same cluster** — once with `ModelCache` (mounts a pre-staged PVC at
engine boot) and once without (engine fetches weights from
HuggingFace) — and prints the side-by-side cold-start timings.

| Phase | Script | What it does |
|---|---|---|
| Setup | `./setup.sh` | Applies shared prereqs / gateway / class, provisions a 2-node GKE InferenceCluster, waits for Ready (~5–10 min). The 2-node cluster gives each parallel deployment its own GPU. |
| Demo | `./demo.sh` | Hydrates the cache, applies cached + uncached deployments in parallel, polls both for readiness, prints the side-by-side timing, sends a sanity curl to the cached endpoint. |
| Reset demo | `./cleanup-demo.sh` | Removes cached + uncached workloads + cache. Cluster + shared infra stay so `demo.sh` re-runs fast. |
| Teardown | `./cleanup.sh` | Removes all demo workload AND the InferenceCluster (deprovisions GKE). Shared infra stays (may be reused by other demos). |

## Prerequisites

- Modelplane Configuration installed on the control-plane cluster, pointing at this branch's package (`./nix.sh run .#build-crossplane && ./nix.sh run .#push-crossplane`, then `kubectl apply` a Configuration manifest pointing at the pushed tag)
- Crossplane GCP provider configured with credentials that can create GKE clusters in your project
- GPU quota for **2× nvidia-l4** on `g2-standard-8` in `us-central1` (cached + uncached run side-by-side)
- `envsubst` available locally (from gettext)
- `GCP_PROJECT` env var set to your project ID

```sh
export GCP_PROJECT=my-gcp-project
./setup.sh    # 5–10 min: GKE provision + stack install
./demo.sh     # cache hydrate, parallel cached/uncached cold-start, side-by-side timings
```

## What you should see

```
==> Apply ModelCache
==> Wait for cache hydration
    Cache hydrated in <Ns>
==> Apply both deployments + services (cached + uncached)
==> Waiting for both deployments to be Ready (polling)
    [cached]   Ready in <Ms>
    [uncached] Ready in <Ks>      # K > M, by the HuggingFace download time

==> Side-by-side cold-start timings
    Cached    (cache mounted, no weight fetch):  <Ms>
    Uncached  (HF pull at engine boot):          <Ks>
...
```

`Cached` is engine boot only; `Uncached` includes the HuggingFace
pull on the serving pod itself. Delta at Qwen 2.5 0.5B is small
(~10s for a ~1 GB pull); the same shape scales to 70B+ models where
the per-replica fetch is 30–60 min.

## Files

| File | Purpose |
|---|---|
| `infra/cluster.yaml` | InferenceCluster (2× L4 nodes, templated via `envsubst`) |
| `01-cache.yaml` | ModelCache: HuggingFace → per-cluster RWX PVC |
| `02-deployment.yaml` | Cached ModelDeployment (references the cache via `spec.caches`) |
| `02b-deployment-uncached.yaml` | Uncached ModelDeployment (engine fetches from HF) |
| `03-service.yaml` | ModelService for the cached deployment |
| `03b-service-uncached.yaml` | ModelService for the uncached deployment |
| `setup.sh` / `demo.sh` / `cleanup-demo.sh` / `cleanup.sh` | Sequenced lifecycle |
