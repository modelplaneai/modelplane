#!/usr/bin/env bash
# Self-playing screencast for the getting-started demo (GKE track), recorded
# against the local kind control plane.
#
# Run it, screen-capture the terminal, voice over afterward. It types and runs
# each command itself, with reading pauses between them. Everything is
# pre-provisioned and Ready (see ../README.md "Pre-flight"), so every apply is an
# idempotent re-apply and every read/curl is instant — nothing waits on infra on
# camera. Banners carry the time estimates from the guide so the timing is honest.
#
# It applies the SAME manifests the getting-started docs ship — straight out of
# docs/manifests/getting-started/ — so the demo and the guide can never drift.
#
# The kind control plane's gateway is a MetalLB IP that isn't routable from the
# host, so the curls reach it through a port-forward to Traefik (set up below).
#
#   cd examples/getting-started/gke && ./record.sh
#   (or from anywhere: examples/getting-started/gke/record.sh — it cd's to its own dir)
#
# Requires: kubectl, curl, jq.
# Tunables (env): TYPE_SPEED (sec/char), READ_PAUSE (sec after each output),
#   CP (control-plane context), NS, PROJECT (GCP project), GW_PORT.
#   Set STEP=1 to advance on Enter (dry run).
set -uo pipefail
cd "$(dirname "$0")" || exit 1                 # so the relative manifest path always resolves

CP="${CP:-kind-crossplane-modelplane}"
NS="${NS:-ml-team}"
PROJECT="${PROJECT:-crossplane-playground}"     # substituted for the my-gcp-project placeholder
MF="../../../docs/manifests/getting-started"    # the canonical docs manifests
TYPE_SPEED="${TYPE_SPEED:-0.012}"
READ_PAUSE="${READ_PAUSE:-6}"
GW_PORT="${GW_PORT:-8080}"

# Pre-flight: tools present, manifests reachable, and the endpoint actually has
# an address. Exit cleanly with guidance rather than capturing a broken take.
for t in kubectl curl jq bat; do
  command -v "$t" >/dev/null || { echo "record.sh: missing required tool '$t'"; exit 1; }
done
[ -f "$MF/gke/platform.yaml" ] || { echo "record.sh: canonical manifests not found at $MF"; exit 1; }
if [ -z "$(kubectl --context "$CP" -n "$NS" get ms qwen -o jsonpath='{.status.address}' 2>/dev/null)" ]; then
  echo "record.sh: ModelService 'qwen' has no address yet."
  echo "Finish the pre-flight (clusters + deployments Ready, endpoint warmed) first."
  exit 1
fi

# The kind gateway IP isn't routable from the host; reach it via a port-forward.
kubectl --context "$CP" -n traefik-system port-forward svc/traefik "$GW_PORT:80" >/dev/null 2>&1 &
PF=$!; trap 'kill "$PF" 2>/dev/null' EXIT
sleep 3
QWEN="http://localhost:$GW_PORT/$NS/qwen"

GRN=$'\033[1;32m'; BOLD=$'\033[1m'; DIM=$'\033[2m'; RST=$'\033[0m'

pause() { if [ -n "${STEP:-}" ]; then read -r _; else sleep "${1:-$READ_PAUSE}"; fi; }
banner() { printf '\n%s# %s%s\n' "$DIM" "$1" "$RST"; pause 2; }
run() {  # type the command like a human, then run it
  printf '%s$ %s%s' "$GRN" "$RST" "$BOLD"
  local s=$1 i
  for ((i = 0; i < ${#s}; i++)); do printf '%s' "${s:i:1}"; sleep "$TYPE_SPEED"; done
  printf '%s\n' "$RST"; sleep 0.5
  eval "$s"
  pause
}

clear

# ---- Part 1: platform team builds one cluster, ML team deploys a model --------
banner "Platform team — publish the GPU hardware (InferenceClass) and provision a starter cluster."
run "bat -pp --color=always \$MF/gke/platform.yaml"
run "sed 's/my-gcp-project/\$PROJECT/' \$MF/gke/platform.yaml | kubectl --context \$CP apply -f -"
banner "Modelplane provisions the GKE cluster + serving stack — about 15 minutes (pre-provisioned here)."
run 'kubectl --context $CP get inferencecluster starter'

banner "ML team — declare what the model needs (a GPU >= 20Gi); no cluster details."
run "bat -pp --color=always \$MF/gke/model-deployment.yaml"
run 'kubectl --context $CP apply -f $MF/gke/model-deployment.yaml'
run 'kubectl --context $CP apply -f $MF/model-service.yaml'
run 'kubectl --context $CP -n ml-team get modelreplica -l modelplane.ai/deployment=qwen-demo -L modelplane.ai/cluster'

banner "Call the OpenAI endpoint."
run "curl -s \$QWEN/v1/chat/completions -H 'content-type: application/json' -d '{\"model\":\"Qwen/Qwen2.5-0.5B-Instruct\",\"messages\":[{\"role\":\"user\",\"content\":\"What is Kubernetes in one sentence?\"}],\"max_tokens\":100}' | jq -r '.choices[0].message.content'"

# ---- Part 2: platform grows the fleet, ML team scales onto it -----------------
banner "Platform team — grow the fleet: add two A100 regions."
run "sed 's/my-gcp-project/\$PROJECT/' \$MF/gke/platform-scale.yaml | kubectl --context \$CP apply -f -"
banner "Provisioning two more clusters — about 10 to 15 minutes (pre-provisioned here)."
run 'kubectl --context $CP get inferencecluster -L modelplane.ai/region'

banner "ML team — add a second deployment, pinned to us-west, asking for a bigger GPU."
run "sed -n '/clusterSelector/,/quantity/p' \$MF/gke/model-deployment-west.yaml | bat -ppl yaml --color=always"
run 'kubectl --context $CP apply -f $MF/gke/model-deployment-west.yaml'

banner "One ModelService fronts both deployments — the endpoint never changes."
run 'kubectl --context $CP apply -f $MF/model-service-multi.yaml'
run 'kubectl --context $CP -n ml-team get modelreplica -L modelplane.ai/deployment,modelplane.ai/cluster'

banner "Same endpoint, same model name — now load-balancing across two regions."
run "curl -s \$QWEN/v1/chat/completions -H 'content-type: application/json' -d '{\"model\":\"Qwen/Qwen2.5-0.5B-Instruct\",\"messages\":[{\"role\":\"user\",\"content\":\"What is Kubernetes in one sentence?\"}],\"max_tokens\":100}' | jq -r '.choices[0].message.content'"

banner "qwen-demo on the L4, qwen-west on the A100 — one endpoint, two regions, your HA posture too."
pause 3
