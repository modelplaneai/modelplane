# Qwen + ModelCache cold-start demo

Demonstrates the cold-start speedup `ModelCache` gives over fetching
weights at engine boot. Two deployments, same model:

| Deployment | Weights source | Cold start (Qwen 2.5 0.5B) |
|---|---|---|
| `qwen-demo` (`../qwen-demo/`) | HuggingFace at engine boot | ~10s download + engine init |
| `qwen-cached-demo` (this dir) | Pre-staged ModelCache PVC | engine init only (~1s) |

The delta is modest at 0.5B parameters. Scale up `--model` to a 70B
variant in the same shape and the delta becomes ~45 min vs seconds.

## Prerequisites

The same infrastructure as `../qwen-demo/`:

```sh
kubectl apply -f ../qwen-demo/00-prerequisites.yaml
kubectl apply -f ../qwen-demo/01-gateway.yaml
kubectl apply -f ../qwen-demo/02-class.yaml
kubectl apply -f ../qwen-demo/03-cluster.yaml
```

Wait for the InferenceCluster(s) to be Ready before continuing.

## Apply the cache + deployment

```sh
kubectl apply -f 01-cache.yaml
# Wait for the cache to hydrate on every matched cluster.
kubectl get modelcache qwen-2-5-0-5b -n ml-team -w
#  expect status.summary.ready to tick to N/N

kubectl apply -f 02-deployment.yaml
kubectl apply -f 03-service.yaml
```

## Observe the speedup

On any matched workload cluster, watch the engine pod come up:

```sh
kubectl get pods -n default -l modelplane.ai/deployment=qwen-cached-demo -w
```

The pod should reach `Running` and pass readiness in seconds —
engine boot only, no weight fetch.

For comparison, applying `../qwen-demo/04-deployment.yaml` (no cache)
on the same cluster will spend the initial ~10s pulling weights from
HuggingFace before the engine starts.

## Send a request

```sh
ADDR=$(kubectl get ms qwen-cached-demo -n ml-team -o jsonpath='{.status.address}')
curl -s "http://${ADDR}/ml-team/qwen-cached-demo/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen","messages":[{"role":"user","content":"hello"}],"max_tokens":40}'
```

## How it works at a glance

1. `ModelCache` pulls Qwen weights once per matched cluster onto an
   RWX PVC. Per-cluster status surfaces in `status.clusters[]`.
2. `ModelDeployment.spec.caches: [{ name: qwen-2-5-0-5b }]` tells the
   serving stack to mount the cache's PVC instead of fetching from
   HuggingFace at engine boot.
3. Subsequent scale-ups and replica restarts read from the pre-staged
   PVC — no thundering-herd HuggingFace pulls, no per-pod download.
