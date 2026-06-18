#!/usr/bin/env bash
# Self-playing screencast for the getting-started demo (GKE track).
#
# Run it, screen-capture the terminal, voice over afterward. It types and runs
# each command itself, with reading pauses between them. Everything is
# pre-provisioned and Ready (see ../README.md "Pre-flight"), so every command is
# an instant read, an idempotent re-apply, or a warm curl — nothing waits on
# infra on camera.
#
#   cd examples/getting-started/gke && ./record.sh
#   (or from anywhere: examples/getting-started/gke/record.sh — it cd's to its own dir)
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

# Pre-flight: tools present, and the endpoint actually has an address. Exit
# cleanly with guidance rather than capturing a broken take.
for t in kubectl curl jq; do
  command -v "$t" >/dev/null || { echo "record.sh: missing required tool '$t'"; exit 1; }
done
QWEN="$(kubectl --context "$CP" -n "$NS" get ms qwen -o jsonpath='{.status.address}' 2>/dev/null)"
if [ -z "$QWEN" ]; then
  echo "record.sh: ModelService 'qwen' has no address yet."
  echo "Finish the pre-flight (clusters + deployment Ready, endpoint warmed) first."
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

banner "Part 1 — started on one cheap L4 cluster, one small model."
run 'kubectl --context $CP get inferencecluster'
run "curl -s \$QWEN/v1/chat/completions -H 'content-type: application/json' -d '{\"model\":\"Qwen/Qwen2.5-0.5B-Instruct\",\"messages\":[{\"role\":\"user\",\"content\":\"What is Crossplane in one sentence?\"}],\"max_tokens\":80}' | jq -r '.choices[0].message.content'"

banner "Part 2 — the fleet grew. Ask for the hardware you need, not a region."
run "grep -A1 'cel:' 02-scale-to-fleet.yaml | head -2"
run 'kubectl --context $CP get inferencecluster -L modelplane.ai/region'

banner "Edit the same deployment in place — watch where the replicas land."
run 'kubectl --context $CP apply -f 02-scale-to-fleet.yaml'
run 'kubectl --context $CP -n ml-team get modelreplica -L modelplane.ai/deployment,modelplane.ai/cluster'

banner "Same endpoint, same model name — now served from the A100 clusters."
run "curl -s \$QWEN/v1/chat/completions -H 'content-type: application/json' -d '{\"model\":\"Qwen/Qwen2.5-0.5B-Instruct\",\"messages\":[{\"role\":\"user\",\"content\":\"Reverse a linked list in Python:\"}],\"max_tokens\":80}' | jq -r '.choices[0].message.content'"

banner "Right hardware fleet-wide, one endpoint — no cluster names, no region labels, no tickets."
pause 3
