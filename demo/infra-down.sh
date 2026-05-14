#!/usr/bin/env bash
# Tear down the GKE InferenceEnvironment to stop burning resources.
# The cleanup takes ~10-15 min as Crossplane deletes the GKE cluster.
set -euo pipefail

echo "==> Deleting demo deployments first (avoids stuck reconciliation)"
kubectl delete modeldeployment --all -n ml-team --ignore-not-found 2>/dev/null || true
sleep 3

echo "==> Deleting InferenceEnvironment"
kubectl delete inferenceenvironment gke-us-central --ignore-not-found

echo ""
echo "  GKE cluster is being torn down by Crossplane (~10-15 min)."
echo "  To re-create before the demo: ./demo/infra-up.sh"
