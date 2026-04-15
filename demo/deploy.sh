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

is_condition_true() {
	local resource="$1" cond_type="$2"
	# $resource is intentionally unquoted so "ig default" splits into two args.
	local conditions
	# shellcheck disable=SC2086
	conditions=$(kubectl get $resource -o jsonpath='{.status.conditions}' 2>/dev/null) || return 1
	echo "$conditions" | grep -qE "\"type\":\"${cond_type}\"[^}]*\"status\":\"True\"|\"status\":\"True\"[^}]*\"type\":\"${cond_type}\""
}

wait_for() {
	local what="$1" check="$2" timeout="${3:-600}"
	info "Waiting for $what (timeout ${timeout}s)..."
	local start
	start=$(date +%s)
	while true; do
		if eval "$check" 2>/dev/null; then
			info "$what: ready."
			return 0
		fi
		local elapsed=$(($(date +%s) - start))
		if ((elapsed > timeout)); then
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

READY_COUNT=$(kubectl get ie -o jsonpath='{.items[*].status.conditions}' 2>/dev/null |
	grep -oE '"type":"Ready"[^}]*"status":"True"|"status":"True"[^}]*"type":"Ready"' | wc -l || true)
if ((READY_COUNT < 2)); then
	echo "ERROR: Expected 2 ready InferenceEnvironments, found $READY_COUNT." >&2
	echo "Wait for platform setup to complete." >&2
	exit 1
fi

# ---- Deploy the model ----
info "Creating ml-team namespace..."
kubectl create namespace ml-team --dry-run=client -o yaml | kubectl apply -f -
kubectl annotate namespace ml-team "ui.modelplane.ai/namespace=ML Team" --overwrite

info "Deploying Qwen 2.5 0.5B to both environments..."
kubectl apply -f "$DEMO_DIR/model-deployment.yaml"

echo ""
info "Waiting for ModelPlacements to become ready..."

# shellcheck disable=SC2016
wait_for "ModelDeployment (2 placements ready)" \
	'[ "$(kubectl get md qwen-demo -n ml-team -o jsonpath="{.status.placements.ready}" 2>/dev/null)" = "2" ]' \
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
	info "Waiting for the model to be ready (image pull + model loading)..."
	info "This takes a few minutes on fresh nodes."

	# Retry the endpoint until we get a 200. The model pods need time to
	# pull the vLLM image and load model weights into GPU memory.
	PROBE_START=$(date +%s)
	while true; do
		RESPONSE=$(
			kubectl run -i --rm "modelplane-probe-$(date +%s)" \
				--image=curlimages/curl --restart=Never --quiet \
				-- curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$ENDPOINT" \
				-H "Content-Type: application/json" \
				-d '{"model":"Qwen/Qwen2.5-0.5B-Instruct","messages":[{"role":"user","content":"ping"}],"max_tokens":1}' 2>/dev/null
		) || true
		if [[ "$RESPONSE" == "200" ]]; then
			info "Model is serving requests."
			break
		fi
		PROBE_ELAPSED=$(($(date +%s) - PROBE_START))
		if ((PROBE_ELAPSED > 600)); then
			echo "WARNING: Model not serving after ${PROBE_ELAPSED}s. Continuing anyway." >&2
			break
		fi
		printf "  %ds elapsed (HTTP %s)...\r" "$PROBE_ELAPSED" "$RESPONSE"
		sleep 15
	done

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
