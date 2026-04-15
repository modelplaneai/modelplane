#!/usr/bin/env bash
# Modelplane Platform Teardown
#
# Deletes all Modelplane resources and the kind cluster.
# IE deletion takes ~10-15 minutes (KServe uninstall + GKE cluster
# delete + VPC cleanup).
#
# Usage:
#   ./demo/platform/teardown.sh

set -euo pipefail

PLATFORM_DIR="$(cd "$(dirname "$0")" && pwd)"
KIND_CLUSTER="modelplane-demo"

info() { echo "==> $*"; }

# Delete consumers before infrastructure.

info "Deleting ModelDeployment..."
kubectl delete -f "$PLATFORM_DIR/../model-deployment.yaml" --ignore-not-found --cascade=foreground --timeout=300s || true

info "Deleting ClusterModel..."
kubectl delete -f "$PLATFORM_DIR/cluster-model.yaml" --ignore-not-found --wait=true || true

# Delete IEs with foreground cascading deletion. This blocks until the IE
# and ALL its composed resources are fully deleted. The Usage resource
# ensures correct deletion order: KServeBackend (Helm releases) deletes
# before GKECluster, so Helm can cleanly uninstall from the still-running
# cluster.
#
# Delete each IE in parallel since they're independent. Each typically
# takes ~10-15 minutes.
info "Deleting InferenceEnvironments (waiting for GKE deprovision)..."
info "(This takes ~10-15 minutes per IE. Crossplane deletes KServe, then the GKE clusters.)"
pids=()
for ie in $(kubectl get ie -o name --ignore-not-found 2>/dev/null); do
	kubectl delete "$ie" --cascade=foreground --timeout=2400s &
	pids+=($!)
done
failed=0
for pid in "${pids[@]}"; do
	if ! wait "$pid"; then
		failed=1
	fi
done
if ((failed)); then
	echo "ERROR: IE deletion timed out. Crossplane is still cleaning up GKE" >&2
	echo "resources. Do NOT delete the kind cluster while this is in progress" >&2
	echo "or the GKE resources will be orphaned. Wait and re-run this script." >&2
	exit 1
fi

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
