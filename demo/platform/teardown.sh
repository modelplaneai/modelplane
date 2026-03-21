#!/usr/bin/env bash
# Modelplane Platform Teardown
#
# Deletes all Modelplane resources and the kind cluster.
# GKE cluster deletion takes ~5-10 minutes.
#
# Usage:
#   ./demo/platform/teardown.sh

set -euo pipefail

PLATFORM_DIR="$(cd "$(dirname "$0")" && pwd)"
KIND_CLUSTER="modelplane-demo"

info() { echo "==> $*"; }

# Delete consumers before infrastructure.

info "Deleting ModelDeployment..."
kubectl delete -f "$PLATFORM_DIR/../model-deployment.yaml" --ignore-not-found --wait=true || true

info "Deleting ClusterModel..."
kubectl delete -f "$PLATFORM_DIR/cluster-model.yaml" --ignore-not-found --wait=true || true

# Delete IEs with foreground cascading deletion. This blocks until the IE
# and ALL its composed resources are fully deleted. The Usage resource
# ensures correct deletion order: KServeStack (Helm releases) deletes
# before GKECluster, so Helm can cleanly uninstall from the still-running
# cluster. This is the slow step — typically ~10 minutes.
info "Deleting InferenceEnvironments (waiting for GKE deprovision)..."
info "(This takes ~10 minutes. Crossplane deletes KServe, then the GKE clusters.)"
kubectl delete -f "$PLATFORM_DIR/inference-environments.yaml" --ignore-not-found --cascade=foreground --timeout=1200s || {
  echo "WARNING: Timed out waiting for IE deletion. Some GKE resources may be orphaned." >&2
}

info "Deleting InferenceGateway..."
kubectl delete -f "$PLATFORM_DIR/inference-gateway.yaml" --ignore-not-found --wait=true || true

info "Deleting credentials..."
kubectl delete -f "$PLATFORM_DIR/credentials.yaml" --ignore-not-found || true
kubectl delete secret gcp-creds -n crossplane-system --ignore-not-found || true

info "Deleting Configuration..."
kubectl delete -f "$PLATFORM_DIR/configuration.yaml" --ignore-not-found || true

info "Deleting prerequisites..."
kubectl delete -f "$PLATFORM_DIR/prerequisites.yaml" --ignore-not-found || true

info "Deleting kind cluster..."
kind delete cluster --name "$KIND_CLUSTER" 2>/dev/null || true

info "Teardown complete."
