#!/usr/bin/env bash
# Run the qwen-cached-demo: side-by-side cached vs uncached
# deployments of Qwen 2.5 0.5B, with timings for each so the
# cold-start delta is visible.
#
# Both deployments target the same InferenceCluster (which has 2 L4
# nodes for this reason). The cached deployment mounts the
# pre-staged ModelCache PVC; the uncached deployment fetches weights
# from HuggingFace at boot.
#
# Re-runnable: kubectl apply is idempotent. Run cleanup-demo.sh
# first if you want a true from-scratch timing.
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)
NS=ml-team

elapsed() {
	local start=$1
	echo "$(($(date +%s) - start))s"
}

# Returns 0 once the named ModelDeployment has ReplicasReady=True.
deployment_ready() {
	local name=$1
	local status
	status=$(kubectl get modeldeployment "$name" -n "$NS" \
		-o jsonpath='{.status.conditions[?(@.type=="ReplicasReady")].status}' 2>/dev/null || true)
	[[ "$status" == "True" ]]
}

echo "==> Apply ModelCache"
cache_start=$(date +%s)
kubectl apply -f "${DIR}/01-cache.yaml"

echo "==> Wait for cache hydration"
kubectl wait --for=condition=ArtifactReady --timeout=10m \
	"modelcache/qwen-2-5-0-5b" -n "$NS"
echo "    Cache hydrated in $(elapsed "$cache_start")"

echo "==> Apply both deployments + services (cached + uncached)"
deploy_start=$(date +%s)
kubectl apply -f "${DIR}/02-deployment.yaml"
kubectl apply -f "${DIR}/02b-deployment-uncached.yaml"
kubectl apply -f "${DIR}/03-service.yaml"
kubectl apply -f "${DIR}/03b-service-uncached.yaml"

echo "==> Waiting for both deployments to be Ready (polling)"
cached_t=""
uncached_t=""
while [[ -z "$cached_t" || -z "$uncached_t" ]]; do
	if [[ -z "$cached_t" ]] && deployment_ready qwen-cached-demo; then
		cached_t=$(elapsed "$deploy_start")
		echo "    [cached]   Ready in ${cached_t}"
	fi
	if [[ -z "$uncached_t" ]] && deployment_ready qwen-uncached-demo; then
		uncached_t=$(elapsed "$deploy_start")
		echo "    [uncached] Ready in ${uncached_t}"
	fi
	sleep 2
done

echo
echo "==> Side-by-side cold-start timings"
printf "    Cached    (cache mounted, no weight fetch):  %s\n" "$cached_t"
printf "    Uncached  (HF pull at engine boot):          %s\n" "$uncached_t"

echo
echo "==> Wait for cached service address"
for _ in $(seq 1 60); do
	ADDR=$(kubectl get ms qwen-cached-demo -n "$NS" -o jsonpath='{.status.address}' 2>/dev/null || true)
	if [[ -n "${ADDR:-}" ]]; then break; fi
	sleep 5
done
if [[ -z "${ADDR:-}" ]]; then
	echo "    Service address not assigned within 5 min" >&2
	exit 1
fi
echo "    Cached service ready at ${ADDR}"

echo "==> Send a test request to the cached endpoint"
kubectl run -i --rm curl-test --image=curlimages/curl --restart=Never -- \
	curl -s --max-time 30 \
	"http://${ADDR}/${NS}/qwen-cached-demo/v1/chat/completions" \
	-H "Content-Type: application/json" \
	-d '{"model":"qwen","messages":[{"role":"user","content":"What is a model cache?"}],"max_tokens":40}'

echo
echo "==> Demo complete."
