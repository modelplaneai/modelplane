#!/usr/bin/env bash
# Self-playing screencast for the getting-started demo.
#
# Run it, screen-capture the terminal, voice over afterward. It types and runs
# each command itself, with reading pauses between them. Everything is
# pre-provisioned and Ready (see README "Pre-flight"), so every command is an
# instant read, an idempotent re-apply, or a warm curl — nothing waits on infra.
#
#   cd examples/getting-started && ./record.sh
#   (or from anywhere: examples/getting-started/record.sh — it cd's to its own dir)
#
# Requires: kubectl, curl, jq.
# Tunables (env): TYPE_SPEED (sec/char), READ_PAUSE (sec after each output),
#   CP (control-plane context), NS. Set STEP=1 to advance on Enter (dry run).
set -uo pipefail
cd "$(dirname "$0")" || exit 1                 # so relative manifest paths always resolve

CP="${CP:-gke_crossplane-playground_us-central1-a_modelplane-cp}"
NS="${NS:-ml-team}"
TYPE_SPEED="${TYPE_SPEED:-0.03}"
READ_PAUSE="${READ_PAUSE:-6}"

# Pre-flight: tools present, and the endpoints actually have addresses. Exit
# cleanly with guidance rather than capturing a broken take.
for t in kubectl curl jq; do
  command -v "$t" >/dev/null || { echo "record.sh: missing required tool '$t'"; exit 1; }
done
STARTER="$(kubectl --context "$CP" -n "$NS" get ms qwen-7b  -o jsonpath='{.status.address}' 2>/dev/null)"
WORKLOAD="$(kubectl --context "$CP" -n "$NS" get ms qwen-14b -o jsonpath='{.status.address}' 2>/dev/null)"
if [ -z "$STARTER" ] || [ -z "$WORKLOAD" ]; then
  echo "record.sh: ModelService address not ready (qwen-7b='$STARTER' qwen-14b='$WORKLOAD')."
  echo "Finish the README pre-flight (clusters + deployments Ready, endpoints warmed) first."
  exit 1
fi

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

banner "Stage 0 — started on one cheap L4 cluster"
run 'kubectl --context $CP get inferencecluster'
run "curl -s \$STARTER/v1/completions -H 'content-type: application/json' -d '{\"model\":\"qwen-7b\",\"prompt\":\"Prefill/decode disaggregation, in one sentence:\",\"max_tokens\":40}' | jq -r '.choices[0].text'"

banner "Stage 1 — the model grew. Ask for the hardware it needs, not a region."
run "grep -A1 'cel:' stage1-fleet-by-capability.yaml | head -2"
run 'kubectl --context $CP get inferencecluster -L modelplane.ai/region'

banner "Modelplane schedules by capability — watch where the 14B lands."
run 'kubectl --context $CP apply -f stage1-fleet-by-capability.yaml'
run 'kubectl --context $CP -n ml-team get modelreplica -L modelplane.ai/deployment,modelplane.ai/cluster'

banner "Same endpoint contract — now served from the A100 clusters."
run "curl -s \$WORKLOAD/v1/completions -H 'content-type: application/json' -d '{\"model\":\"qwen-14b\",\"prompt\":\"Reverse a linked list in Python:\",\"max_tokens\":60}' | jq -r '.choices[0].text'"

banner "Asked for the hardware the model needs — Modelplane found it fleet-wide. No region labels, no tickets."
pause 3
