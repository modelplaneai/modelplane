#!/usr/bin/env bash
# Tear down the demo workload only — keeps the InferenceCluster and
# shared infra so you can re-run demo.sh quickly.
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)

echo "==> Delete ModelService"
kubectl delete --ignore-not-found -f "${DIR}/03-service.yaml"

echo "==> Delete ModelDeployment"
kubectl delete --ignore-not-found -f "${DIR}/02-deployment.yaml"

echo "==> Delete ModelCache"
kubectl delete --ignore-not-found -f "${DIR}/01-cache.yaml"

echo "==> Demo workload removed. Run ./demo.sh to redeploy or"
echo "    ./cleanup.sh to also tear down the InferenceCluster."
