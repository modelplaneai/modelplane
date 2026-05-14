#!/usr/bin/env bash
# Setup for the Modelplane all-hands demo.
#
# Idempotent — safe to run multiple times. Assumes:
#   - kubectl is pointed at the control plane cluster
#   - InferenceEnvironments and InferenceGateway are already running
#
# Run cleanup.sh first if you're resetting between demo rehearsals.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> Ensuring ml-team namespace exists"
kubectl create namespace ml-team 2>/dev/null || true

echo "==> Applying demo catalog models"
kubectl apply -f "$SCRIPT_DIR/manifests/qwen-0-5b.yaml"
kubectl apply -f "$SCRIPT_DIR/manifests/llama-70b-awq.yaml"
kubectl apply -f "$SCRIPT_DIR/manifests/llama-70b.yaml"
kubectl apply -f "$SCRIPT_DIR/manifests/llama-405b.yaml"

echo "==> Bumping KServe storage initializer memory limit on remote cluster"
# The default 4Gi is too small for models >4GB. Patch to 8Gi.
GKE_CONTEXT=$(kubectl config get-contexts -o name | grep gke_ || true)
if [ -n "$GKE_CONTEXT" ]; then
  kubectl --context "$GKE_CONTEXT" get configmap inferenceservice-config -n kserve -o json \
    | python3 -c "
import sys, json
cm = json.load(sys.stdin)
si = json.loads(cm['data']['storageInitializer'])
si['memoryLimit'] = '8Gi'
cm['data']['storageInitializer'] = json.dumps(si)
json.dump(cm, sys.stdout)
" | kubectl --context "$GKE_CONTEXT" apply -f - >/dev/null 2>&1 && echo "    Patched to 8Gi" || echo "    (skipped — could not reach remote cluster)"
fi

echo "==> Verifying catalog"
kubectl get clustermodels

echo ""
echo "==> Setup complete. Ready for demo."
echo ""
echo "    Pre-deploy the working models before going live:"
echo "      kubectl apply -f demo/manifests/deploy-qwen.yaml"
echo "      kubectl apply -f demo/manifests/deploy-llama-70b-awq.yaml"
echo "      kubectl wait md --all -n ml-team --for=condition=Ready --timeout=600s"
