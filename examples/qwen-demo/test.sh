#!/usr/bin/env bash
# Test the qwen-demo endpoint by running an ephemeral curl pod inside
# the control plane cluster. This works regardless of whether the
# Gateway's MetalLB IP is routable from the host.
#
# Uses kubectl create + wait + logs instead of `kubectl run -i --rm` to
# avoid the attach race where output is lost when the pod exits before
# the stdout stream is bound.
set -euo pipefail

pod=curl-test
namespace=default

cleanup() {
	kubectl delete pod "$pod" -n "$namespace" --wait=false --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup EXIT

address=$(kubectl get ms qwen-demo -n ml-team -o jsonpath='{.status.address}')
if [[ -z "$address" ]]; then
	echo "ModelService qwen-demo has no status.address yet" >&2
	exit 1
fi

echo "ModelService qwen-demo has status.address: $address"
echo

url="$address/v1/chat/completions"
body='{"model":"qwen","messages":[{"role":"user","content":"What is Crossplane?"}],"max_tokens":40}'

# Make sure no stale pod is hanging around from a previous run.
kubectl delete pod "$pod" -n "$namespace" --wait=true --ignore-not-found >/dev/null 2>&1 || true

kubectl run "$pod" -n "$namespace" --image=curlimages/curl --restart=Never \
	--command -- curl -sS --max-time 30 "$url" \
	-H "Content-Type: application/json" \
	-d "$body" >/dev/null

# Wait for the pod to finish (Ready=false with Succeeded/Failed phase).
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded \
	pod/"$pod" -n "$namespace" --timeout=120s >/dev/null 2>&1 || {
	echo "curl pod did not succeed; logs:" >&2
	kubectl logs "$pod" -n "$namespace" 2>&1 >&2 || true
	exit 1
}

kubectl logs "$pod" -n "$namespace"
echo
