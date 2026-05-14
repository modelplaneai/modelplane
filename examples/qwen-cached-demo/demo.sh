#!/usr/bin/env bash
# Run the qwen-cached-demo: stage the cache, deploy, hit the endpoint.
# Times each phase so the cold-start delta is visible.
#
# Re-runnable: kubectl apply is idempotent; resources already in
# place skip cleanly. Run cleanup-demo.sh first if you want a true
# from-scratch timing.
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)
NS=ml-team

elapsed() {
	local start=$1
	echo "$(($(date +%s) - start))s"
}

echo "==> Apply ModelCache"
start=$(date +%s)
kubectl apply -f "${DIR}/01-cache.yaml"

echo "==> Wait for cache hydration"
kubectl wait --for=condition=ArtifactReady --timeout=10m \
	"modelcache/qwen-2-5-0-5b" -n "$NS"
echo "    Cache hydrated in $(elapsed $start)"

echo "==> Apply ModelDeployment"
start=$(date +%s)
kubectl apply -f "${DIR}/02-deployment.yaml"

echo "==> Wait for replica readiness (engine boot only; no weight fetch)"
kubectl wait --for=condition=ReplicasReady --timeout=5m \
	"modeldeployment/qwen-cached-demo" -n "$NS"
echo "    Replica Ready in $(elapsed $start)"

echo "==> Apply ModelService"
kubectl apply -f "${DIR}/03-service.yaml"

echo "==> Wait for service address"
for _ in $(seq 1 60); do
	ADDR=$(kubectl get ms qwen-cached-demo -n "$NS" -o jsonpath='{.status.address}' 2>/dev/null || true)
	if [[ -n "${ADDR:-}" ]]; then break; fi
	sleep 5
done
if [[ -z "${ADDR:-}" ]]; then
	echo "    Service address not assigned within 5 min" >&2
	exit 1
fi
echo "    Service ready at ${ADDR}"

echo "==> Send a test request"
kubectl run -i --rm curl-test --image=curlimages/curl --restart=Never -- \
	curl -s --max-time 30 \
	"http://${ADDR}/${NS}/qwen-cached-demo/v1/chat/completions" \
	-H "Content-Type: application/json" \
	-d '{"model":"qwen","messages":[{"role":"user","content":"What is a model cache?"}],"max_tokens":40}'

echo
echo "==> Demo complete."
