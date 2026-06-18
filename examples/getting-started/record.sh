#!/usr/bin/env bash
# On-camera stepper for the getting-started demo.
#
# Everything is already provisioned and Ready before you record (see README
# "Pre-flight"). Every command here is an instant read, an idempotent re-apply,
# or a warm curl — nothing waits on infra. Press Enter to run each step; narrate
# between them.
#
# Run from this directory:  cd examples/getting-started && ./record.sh
# Requires: kubectl, curl, jq. Set CP if your control-plane context differs.
set -uo pipefail
CP="${CP:-gke_crossplane-playground_us-central1-a_modelplane-cp}"
NS="${NS:-ml-team}"

# Discover the public endpoints from the ModelServices (instant; already Ready).
STARTER="$(kubectl --context "$CP" -n "$NS" get ms qwen-7b  -o jsonpath='{.status.address}' 2>/dev/null)"
WORKLOAD="$(kubectl --context "$CP" -n "$NS" get ms qwen-14b -o jsonpath='{.status.address}' 2>/dev/null)"

# Print the command as if typed, wait for Enter, then run it.
step() { printf '\n\033[1;32m$ \033[0m\033[1m%s\033[0m' "$1"; read -r _; eval "$1"; }

echo "── Beat 1 — started on one cheap cluster ───────────────────────────────────"
step 'kubectl --context $CP get inferencecluster'
step "curl -s $STARTER/v1/completions -H 'content-type: application/json' \
-d '{\"model\":\"qwen-7b\",\"prompt\":\"Prefill/decode disaggregation, in one sentence:\",\"max_tokens\":40}' | jq -r '.choices[0].text'"

echo "── Beat 2 — the model grew; ask for hardware, not a region ──────────────────"
step "grep -A1 'cel:' stage1-fleet-by-capability.yaml | head -2"
step 'kubectl --context $CP get inferencecluster -L modelplane.ai/region'

echo "── Beat 3 — Modelplane schedules by capability (the money shot) ─────────────"
step 'kubectl --context $CP apply -f stage1-fleet-by-capability.yaml'   # already applied → instant
step "kubectl --context \$CP -n $NS get modelreplica -o wide"            # landed on the A100s, not the L4

echo "── Beat 4 — same endpoint, now on the A100 clusters ─────────────────────────"
step "curl -s $WORKLOAD/v1/completions -H 'content-type: application/json' \
-d '{\"model\":\"qwen-14b\",\"prompt\":\"Reverse a linked list in Python:\",\"max_tokens\":60}' | jq -r '.choices[0].text'"

echo
echo "Wrap: asked for the hardware the model needs; Modelplane found it fleet-wide — no region labels, no tickets."
