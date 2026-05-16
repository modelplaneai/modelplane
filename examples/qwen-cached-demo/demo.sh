#!/usr/bin/env bash
# Run the qwen-cached-demo: hydrate a ModelCache and bring up a
# multi-node LWS gang (TensorPipeline=1x2) that mounts the cache.
#
# What this exercises:
#  - ModelCache pulling Qwen 2.5 0.5B onto a per-cluster RWX PVC
#  - ModelDeployment.spec.caches → KServe LLMInferenceService
#    model.uri = pvc://modelcache-<name>
#  - LWS gang composition: 2 pods (leader + worker) on 2 separate
#    nodes, both reading from the same cached PVC
#
# The 02b-deployment-uncached.yaml + 03b-service-uncached.yaml in
# this directory are an *optional* side-by-side comparison. Applying
# them requires an extra GPU node in the cluster (current cluster.yaml
# is sized for the 2-pod LWS gang only). To run the side-by-side,
# bump nodeCount to 3 in infra/cluster.yaml and apply 02b + 03b
# manually after demo.sh runs.
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
	local rstatus
	rstatus=$(kubectl get modeldeployment "$name" -n "$NS" \
		-o jsonpath='{.status.conditions[?(@.type=="ReplicasReady")].status}' 2>/dev/null || true)
	[[ "$rstatus" == "True" ]]
}

echo "==> Apply ModelCache"
cache_start=$(date +%s)
kubectl apply -f "${DIR}/01-cache.yaml"

echo "==> Wait for cache hydration"
kubectl wait --for=condition=ArtifactReady --timeout=20m \
	"modelcache/qwen-2-5-0-5b" -n "$NS"
echo "    Cache hydrated in $(elapsed "$cache_start")"

echo "==> Apply ModelDeployment (TensorPipeline 1x2 LWS gang) + ModelService"
deploy_start=$(date +%s)
kubectl apply -f "${DIR}/02-deployment.yaml"
kubectl apply -f "${DIR}/03-service.yaml"

echo "==> Waiting for the 2-pod LWS gang to be Ready"
while ! deployment_ready qwen-cached-demo; do
	sleep 5
done
cached_t=$(elapsed "$deploy_start")
echo "    LWS gang Ready in ${cached_t}"

echo
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

# LWS reports the gang Ready as soon as both pods are 1/1, but vLLM
# inside the leader takes another ~60-120s to load the model from the
# cached PVC and finish CUDA graph capture before Uvicorn opens. Send
# the actual chat completion from a pod that retries internally so
# the demo doesn't race on first-curl.
echo "==> Send a test request (retries until engine is serving)"
serve_start=$(date +%s)
# ModelService.status.address is already a full URL with the path
# prefix (http://<gateway>/<ns>/<svc>); don't prepend scheme or
# re-add the path — just append the OpenAI path.
kubectl run -i --rm curl-test --image=curlimages/curl --restart=Never --quiet -- \
	sh -c "until curl -s -f --max-time 10 '${ADDR}/v1/models' >/dev/null 2>&1; do sleep 5; done; \
	       curl -s --max-time 30 '${ADDR}/v1/chat/completions' \
	       -H 'Content-Type: application/json' \
	       -d '{\"model\":\"qwen\",\"messages\":[{\"role\":\"user\",\"content\":\"What is a model cache?\"}],\"max_tokens\":40}'"
echo
echo "    Engine answered after $(elapsed "$serve_start") of post-gang wait"

echo
echo "==> Demo complete. The LWS gang of 2 pods both serve from the"
echo "    shared cache PVC; neither pod fetched weights from HF at"
echo "    boot — total time from apply to Ready was ${cached_t}."
