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

# Delete IEs and wait. kubectl delete --wait blocks until the finalizer
# completes (Crossplane deprovisions the GKE clusters, VPCs, etc.).
# This is the slow step — typically ~5-10 minutes.
info "Deleting InferenceEnvironments (waiting for GKE deprovision)..."
info "(This takes ~5-10 minutes.)"
kubectl delete -f "$PLATFORM_DIR/inference-environments.yaml" --ignore-not-found --wait=true --timeout=1200s || {
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
