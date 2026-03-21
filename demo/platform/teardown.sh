#!/usr/bin/env bash
# Modelplane Platform Teardown
#
# Deletes all Modelplane resources and the kind cluster.
# GKE cluster deletion takes a few minutes.
#
# Usage:
#   ./demo/platform/teardown.sh

set -euo pipefail

PLATFORM_DIR="$(cd "$(dirname "$0")" && pwd)"
KIND_CLUSTER="modelplane-demo"

info() { echo "==> $*"; }

info "Deleting ModelDeployment..."
kubectl delete -f "$PLATFORM_DIR/../model-deployment.yaml" --ignore-not-found 2>/dev/null || true

info "Deleting InferenceEnvironments (GKE clusters will be deprovisioned)..."
kubectl delete -f "$PLATFORM_DIR/inference-environments.yaml" --ignore-not-found 2>/dev/null || true

info "Deleting ClusterModel..."
kubectl delete -f "$PLATFORM_DIR/cluster-model.yaml" --ignore-not-found 2>/dev/null || true

info "Deleting InferenceGateway..."
kubectl delete -f "$PLATFORM_DIR/inference-gateway.yaml" --ignore-not-found 2>/dev/null || true

info "Waiting for GKE clusters to be deleted (this takes a few minutes)..."
kubectl wait --for=delete ie --all --timeout=600s 2>/dev/null || true

info "Deleting credentials..."
kubectl delete -f "$PLATFORM_DIR/credentials.yaml" --ignore-not-found 2>/dev/null || true
kubectl delete secret gcp-creds -n crossplane-system --ignore-not-found 2>/dev/null || true

info "Deleting Configuration..."
kubectl delete -f "$PLATFORM_DIR/configuration.yaml" --ignore-not-found 2>/dev/null || true

info "Deleting prerequisites..."
kubectl delete -f "$PLATFORM_DIR/prerequisites.yaml" --ignore-not-found 2>/dev/null || true

info "Deleting kind cluster..."
kind delete cluster --name "$KIND_CLUSTER" 2>/dev/null || true

info "Teardown complete."
