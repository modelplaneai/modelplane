#!/usr/bin/env bash
# Tear down both demo workloads (cached + uncached) — keeps the
# InferenceCluster and shared infra so you can re-run demo.sh
# quickly.
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)

# Pin the kubectl context. See demo.sh for rationale.
KCTX="${MODELPLANE_CONTEXT:-$(command kubectl config current-context)}"
kubectl() {
	command kubectl --context="$KCTX" "$@"
}
echo "    Using kubectl context: $KCTX"

echo "==> Delete ModelServices"
kubectl delete --ignore-not-found -f "${DIR}/03-service.yaml"
kubectl delete --ignore-not-found -f "${DIR}/03b-service-uncached.yaml"

echo "==> Delete ModelDeployments"
kubectl delete --ignore-not-found -f "${DIR}/02-deployment.yaml"
kubectl delete --ignore-not-found -f "${DIR}/02b-deployment-uncached.yaml"

echo "==> Delete ModelCache"
kubectl delete --ignore-not-found -f "${DIR}/01-cache.yaml"

echo "==> Demo workload removed. Run ./demo.sh to redeploy or"
echo "    ./cleanup.sh to also tear down the InferenceCluster."
