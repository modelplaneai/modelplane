#!/usr/bin/env bash
# Full teardown: demo workload + the InferenceCluster created by
# setup.sh. Shared infra (gateway, class, prereqs) stays — those
# are reused by other demos.
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)

bash "${DIR}/cleanup-demo.sh"

echo "==> Delete InferenceCluster qwen-cached-demo"
echo "    (deprovisions the GKE cluster; can take several minutes)"
# envsubst not required for delete — the GCP_PROJECT field is opaque
# to kubectl delete. But pipe through for symmetry with setup.sh.
GCP_PROJECT="${GCP_PROJECT:-placeholder}" envsubst <"${DIR}/infra/cluster.yaml" |
	kubectl delete --ignore-not-found -f -

echo "==> Teardown complete. Shared infra (gateway/class/prereqs) kept."
