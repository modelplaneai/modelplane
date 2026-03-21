#!/usr/bin/env bash
# Modelplane Platform Setup
#
# Run this as a platform engineer to set up the inference platform.
# Creates a kind cluster, installs Crossplane and Modelplane, configures
# GCP credentials, and provisions two inference environments with KServe
# on GKE clusters.
#
# This takes ~20 minutes (mostly waiting for GKE clusters to provision
# in parallel).
#
# Prerequisites:
#   - kind, kubectl, helm
#   - A GCP service account key (set GCP_KEY to override the default path)
#
# Usage:
#   ./demo/platform/setup.sh

set -euo pipefail

PLATFORM_DIR="$(cd "$(dirname "$0")" && pwd)"
GCP_KEY="${GCP_KEY:-$HOME/secret/crossplane-playground-716b2ea14ff0.json}"
KIND_CLUSTER="modelplane-demo"

info()  { echo "==> $*"; }
warn()  { echo "WARNING: $*" >&2; }

wait_for() {
  local what="$1" check="$2" timeout="${3:-600}"
  info "Waiting for $what (timeout ${timeout}s)..."
  local start=$(date +%s)
  while true; do
    if eval "$check" 2>/dev/null; then
      info "$what: ready."
      return 0
    fi
    local elapsed=$(( $(date +%s) - start ))
    if (( elapsed > timeout )); then
      echo "ERROR: Timed out waiting for $what after ${elapsed}s" >&2
      return 1
    fi
    printf "  %ds elapsed...\r" "$elapsed"
    sleep 15
  done
}

# ---- Validate prerequisites ----
if [[ ! -f "$GCP_KEY" ]]; then
  echo "ERROR: GCP service account key not found at $GCP_KEY" >&2
  echo "Set GCP_KEY to the path of your key file." >&2
  exit 1
fi

for cmd in kind kubectl helm; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "ERROR: $cmd is required but not found in PATH." >&2
    exit 1
  fi
done

# ---- Step 1: Kind cluster ----
info "Creating kind cluster '$KIND_CLUSTER'..."
if kind get clusters 2>/dev/null | grep -q "^${KIND_CLUSTER}$"; then
  info "Cluster already exists, reusing it."
  kubectl config use-context "kind-${KIND_CLUSTER}"
else
  kind create cluster --name "$KIND_CLUSTER"
fi

# ---- Step 2: Crossplane ----
info "Installing Crossplane v2.2.0..."
helm repo add crossplane-stable https://charts.crossplane.io/stable 2>/dev/null || true
helm repo update crossplane-stable 2>/dev/null
if helm list -n crossplane-system 2>/dev/null | grep -q crossplane; then
  info "Crossplane already installed."
else
  helm install crossplane crossplane-stable/crossplane \
    --namespace crossplane-system --create-namespace --version 2.2.0 --wait
fi

# ---- Step 3: Prerequisites ----
info "Applying prerequisites (RBAC, DeploymentRuntimeConfig, ImageConfig)..."
kubectl apply -f "$PLATFORM_DIR/prerequisites.yaml"

# ---- Step 4: Configuration ----
info "Installing Modelplane Configuration..."
kubectl apply -f "$PLATFORM_DIR/configuration.yaml"

wait_for "Configuration" \
  '[ "$(kubectl get configuration modelplane-infra -o jsonpath={.status.conditions[?(@.type==\"Healthy\")].status} 2>/dev/null)" = "True" ]' \
  300

# ---- Step 5: GCP credentials ----
info "Configuring GCP credentials..."
if ! kubectl get secret gcp-creds -n crossplane-system &>/dev/null; then
  kubectl create secret generic gcp-creds \
    --from-file=credentials="$GCP_KEY" \
    -n crossplane-system
fi
kubectl apply -f "$PLATFORM_DIR/credentials.yaml"

# ---- Step 6: InferenceGateway ----
info "Creating InferenceGateway (Envoy Gateway + MetalLB)..."
kubectl apply -f "$PLATFORM_DIR/inference-gateway.yaml"

wait_for "InferenceGateway" \
  '[ "$(kubectl get ig default -o jsonpath={.status.conditions[?(@.type==\"Ready\")].status} 2>/dev/null)" = "True" ]' \
  600

# ---- Step 7: ClusterModel ----
info "Registering ClusterModel (Qwen 2.5 0.5B)..."
kubectl apply -f "$PLATFORM_DIR/cluster-model.yaml"

# ---- Step 8: InferenceEnvironments ----
info "Creating two InferenceEnvironments (us-central1, us-east1)..."
kubectl apply -f "$PLATFORM_DIR/inference-environments.yaml"

info ""
info "Both GKE clusters are provisioning in parallel. This takes ~20 minutes."
info "Watch progress with: kubectl get ie"
info ""

wait_for "InferenceEnvironments (both)" \
  '[ "$(kubectl get ie -o jsonpath="{range .items[*]}{.status.conditions[?(@.type==\"Ready\")].status}{\" \"}{end}" 2>/dev/null | grep -co True)" = "2" ]' \
  1800

# ---- Done ----
echo ""
info "========================================="
info "  Platform setup complete."
info "========================================="
echo ""
kubectl get ig
echo ""
kubectl get ie
echo ""
kubectl get clustermodels
echo ""
info "The platform is ready for ML teams to deploy models."
info "Run: ./demo/deploy.sh"
