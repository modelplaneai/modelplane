#!/usr/bin/env bash
# Set up prerequisites for the qwen-cached-demo.
#
# Idempotent: re-running is a no-op once everything is in place.
# Assumes Modelplane Configuration is already installed on the
# control-plane cluster and Crossplane GCP provider is configured.
#
# Env vars:
#   GCP_PROJECT   GCP project ID for the GKE cluster (required)
set -euo pipefail

if [[ -z "${GCP_PROJECT:-}" ]]; then
	echo "GCP_PROJECT not set. Export your GCP project ID, e.g.:" >&2
	echo "  export GCP_PROJECT=my-gcp-project" >&2
	exit 1
fi

if ! command -v envsubst >/dev/null 2>&1; then
	echo "envsubst not found (install gettext)" >&2
	exit 1
fi

DIR=$(cd "$(dirname "$0")" && pwd)

echo "==> Applying shared infrastructure prereqs (from ../qwen-demo)"
kubectl apply -f "${DIR}/../qwen-demo/00-prerequisites.yaml"
kubectl apply -f "${DIR}/../qwen-demo/01-gateway.yaml"
kubectl apply -f "${DIR}/../qwen-demo/02-class.yaml"

echo "==> Provisioning InferenceCluster (project=${GCP_PROJECT})"
envsubst <"${DIR}/infra/cluster.yaml" | kubectl apply -f -

echo "==> Waiting for InferenceCluster qwen-cached-demo to be Ready"
echo "    (GKE provisioning + stack install typically 5-10 min on first run)"
kubectl wait --for=condition=Ready --timeout=20m inferencecluster/qwen-cached-demo

echo "==> Setup complete. Run ./demo.sh next."
