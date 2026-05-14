# Qwen + ModelCache cold-start demo

Idempotent, scripted end-to-end demo of `ModelCache` accelerating
serving cold-start. Two phases: **setup** (prereqs + InferenceCluster)
and **demo** (cache → deployment → service → test request). Cleanup
splits into "just the demo workload" and "everything" so you can
iterate without re-provisioning the cluster.

| Phase | Script | What it does |
|---|---|---|
| Setup | `./setup.sh` | Applies shared prereqs / gateway / class, provisions a GKE InferenceCluster, waits for Ready (~5–10 min) |
| Demo | `./demo.sh` | Applies cache → waits for hydration → applies deployment → waits for replica → applies service → curl test. Times each phase. |
| Reset demo | `./cleanup-demo.sh` | Removes service / deployment / cache. Cluster + infra stay so `demo.sh` can re-run fast. |
| Teardown | `./cleanup.sh` | Removes demo workload AND the InferenceCluster (deprovisions GKE). Shared infra stays. |

## Prerequisites

- Modelplane Configuration installed on the control-plane cluster, pointing at this branch's package (`./nix.sh run .#build-crossplane && ./nix.sh run .#push-crossplane`, then `kubectl apply` a Configuration manifest pointing at the pushed tag)
- Crossplane GCP provider configured with credentials that can create GKE clusters in your project
- GPU quota for `nvidia-l4` on `g2-standard-8` in `us-central1`
- `envsubst` available locally (from gettext)
- `GCP_PROJECT` env var set to your project ID

```sh
export GCP_PROJECT=my-gcp-project
./setup.sh    # 5–10 min: GKE provision + stack install
./demo.sh     # cache hydrate + replica boot + curl test
```

## What you should see

```
==> Apply ModelCache
==> Wait for cache hydration
    Cache hydrated in <Ns>
==> Apply ModelDeployment
==> Wait for replica readiness (engine boot only; no weight fetch)
    Replica Ready in <Ms>
...
```

`Replica Ready` is engine boot only — no HuggingFace pull on the
serving pod. Subsequent replica restarts skip the download entirely;
the same shape scales to 70B+ models where the per-replica fetch
would otherwise be 30–60 min.

## Comparing to the un-cached path

To time the contrast directly, copy `02-deployment.yaml` to a sibling
file, remove the `caches:` block, change `metadata.name`, and apply.
That deployment will fetch weights from HuggingFace at engine boot;
the timing delta vs the cached path is the cold-start speedup.

## Files

| File | Purpose |
|---|---|
| `infra/cluster.yaml` | InferenceCluster CR (templated via `envsubst`) |
| `01-cache.yaml` | ModelCache: HuggingFace → per-cluster RWX PVC |
| `02-deployment.yaml` | ModelDeployment referencing the cache via `spec.caches` |
| `03-service.yaml` | ModelService exposing the deployment |
| `setup.sh` / `demo.sh` / `cleanup-demo.sh` / `cleanup.sh` | Sequenced lifecycle |
