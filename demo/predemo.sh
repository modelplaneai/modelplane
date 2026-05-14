#!/usr/bin/env bash
# One-shot script: cleans up, sets up catalog, pre-deploys working models,
# starts the UI, and waits until everything is ready.
#
# Run this 10-15 minutes before going live.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$SCRIPT_DIR/.."
export PATH="/opt/homebrew/share/google-cloud-sdk/bin:$HOME/bin:$PATH"

# --- Preflight ---
echo "==> Preflight checks"
CTX=$(kubectl config current-context)
if [[ "$CTX" != "kind-modelplane" ]]; then
  echo "    ERROR: kubectl context is '$CTX', expected 'kind-modelplane'"
  echo "    Run: kubectl config use-context kind-modelplane"
  exit 1
fi

IE_READY=$(kubectl get ie gke-us-central -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "False")
IG_READY=$(kubectl get ig default -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "False")
if [[ "$IE_READY" != "True" || "$IG_READY" != "True" ]]; then
  echo "    ERROR: InferenceEnvironment or InferenceGateway not ready"
  kubectl get ie,ig 2>&1
  exit 1
fi
echo "    Context: $CTX"
echo "    InferenceEnvironment: ready"
echo "    InferenceGateway: ready"

# --- Cleanup + Setup ---
echo ""
"$SCRIPT_DIR/cleanup.sh"
echo ""
"$SCRIPT_DIR/setup.sh"

# --- Pre-deploy working models ---
echo ""
echo "==> Pre-deploying working models (Qwen + Llama 8B AWQ)"
kubectl apply -f "$SCRIPT_DIR/manifests/deploy-qwen.yaml"
kubectl apply -f "$SCRIPT_DIR/manifests/deploy-llama-70b-awq.yaml"

echo "==> Waiting for models to become ready (may take 5-10 min if GPU node scaled to zero)..."
kubectl wait md --all -n ml-team --for=condition=Ready --timeout=900s

echo ""
echo "==> Testing chat endpoints"
# Test Qwen
QWEN_OK=$(kubectl run -i --rm test-qwen-$RANDOM --image=curlimages/curl --restart=Never -- \
  curl -sf -o /dev/null -w '%{http_code}' \
  http://172.18.255.200/ml-team/qwen/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen2.5-0.5B-Instruct","messages":[{"role":"user","content":"hi"}],"max_tokens":5}' 2>/dev/null || echo "FAIL")
echo "    Qwen chat: $QWEN_OK"

# Test Llama
LLAMA_OK=$(kubectl run -i --rm test-llama-$RANDOM --image=curlimages/curl --restart=Never -- \
  curl -sf -o /dev/null -w '%{http_code}' \
  http://172.18.255.200/ml-team/llama-8b-awq/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4","messages":[{"role":"user","content":"hi"}],"max_tokens":5}' 2>/dev/null || echo "FAIL")
echo "    Llama 8B AWQ chat: $LLAMA_OK"

echo ""
echo "==> Starting port-forward for gateway (needed on macOS)"
# Kill any existing port-forward on 8888.
lsof -ti:8888 | xargs kill 2>/dev/null || true
kubectl port-forward -n envoy-gateway-system \
  svc/envoy-modelplane-system-modelplane-c1ccdf3e 8888:80 &>/dev/null &
sleep 1
echo "    Port-forward running on localhost:8888"

echo ""
echo "============================================"
echo "  PRE-DEMO READY"
echo "============================================"
echo ""
echo "  Models in catalog:  4 (Qwen 0.5B, Llama 8B AWQ, Llama 70B, Llama 405B)"
echo "  Pre-deployed:       Qwen + Llama 8B AWQ (both serving)"
echo "  Left for live:      Llama 70B + 405B (rejections)"
echo ""
echo "  Start the UI in two terminals:"
echo "    Terminal 1:  cd $ROOT/ui && MODELPLANE_GATEWAY_OVERRIDE=http://localhost:8888 go run ./cmd/proxy/ --kubeconfig ~/.kube/config"
echo "    Terminal 2:  cd $ROOT/ui/frontend && npm run dev"
echo "    Then open:   http://localhost:5173"
echo ""
