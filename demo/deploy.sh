#!/usr/bin/env bash
# Modelplane Model Deployment
#
# Run this as an ML team member to deploy a model. The platform must
# already be set up (run ./demo/platform/setup.sh first).
#
# Deploys Qwen 2.5 0.5B to both inference environments and tests the
# unified endpoint.
#
# Usage:
#   ./demo/deploy.sh

set -euo pipefail

DEMO_DIR="$(cd "$(dirname "$0")" && pwd)"

info() { echo "==> $*"; }

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
    sleep 10
  done
}

# ---- Check platform is ready ----
info "Checking platform is ready..."
if ! kubectl get ie &>/dev/null; then
  echo "ERROR: No InferenceEnvironments found. Run ./demo/platform/setup.sh first." >&2
  exit 1
fi

READY_COUNT=$(kubectl get ie -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' | grep -c True || true)
if (( READY_COUNT < 2 )); then
  echo "ERROR: Expected 2 ready InferenceEnvironments, found $READY_COUNT." >&2
  echo "Wait for platform setup to complete." >&2
  exit 1
fi

# ---- Deploy the model ----
info "Creating ml-team namespace..."
kubectl create namespace ml-team --dry-run=client -o yaml | kubectl apply -f -

info "Deploying Qwen 2.5 0.5B to both environments..."
kubectl apply -f "$DEMO_DIR/model-deployment.yaml"

echo ""
info "Waiting for ModelPlacements to become ready..."
wait_for "ModelDeployment (2 placements ready)" \
  '[ "$(kubectl get md qwen-demo -n ml-team -o jsonpath={.status.placements.ready} 2>/dev/null)" = "2" ]' \
  600

# ---- Show results ----
echo ""
info "========================================="
info "  Model deployed successfully."
info "========================================="
echo ""
kubectl get md -n ml-team
echo ""
kubectl get mp -n ml-team
echo ""

ENDPOINT=$(kubectl get md qwen-demo -n ml-team -o jsonpath='{.status.endpoint.url}' 2>/dev/null)
if [[ -n "$ENDPOINT" ]]; then
  info "Unified endpoint: $ENDPOINT"
  echo ""
  info "Testing the endpoint..."
  echo ""
  kubectl run -i --rm modelplane-demo-curl \
    --image=curlimages/curl \
    --restart=Never \
    -- curl -s --max-time 30 "$ENDPOINT" \
      -H "Content-Type: application/json" \
      -d '{"model":"Qwen/Qwen2.5-0.5B-Instruct","messages":[{"role":"user","content":"What is Crossplane in one sentence?"}],"max_tokens":100}'
  echo ""
fi
