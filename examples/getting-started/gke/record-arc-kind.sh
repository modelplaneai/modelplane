#!/usr/bin/env bash
# Self-playing recording of the getting-started guides against the local kind
# control plane (which provisioned the live GKE fleet). It walks both guides:
#   Part 1 — first-deployment.md : publish hardware, provision a cluster, serve a model
#   Part 2 — scale-to-fleet.md   : grow the fleet, schedule the same model by capability
#
# Snapshot mode: every resource is already live, so we SHOW the manifests (the
# declarative intent) and then the live objects + a real OpenAI curl — no waiting.
# The gateway apply is real and idempotent. The ModelService address is a kind
# MetalLB IP that isn't routable from the host, so the curl reaches the gateway
# through a port-forward we set up on-screen — same /ml-team/qwen path throughout.
set -uo pipefail
cd "$(dirname "$0")" || exit 1
CP="${CP:-kind-crossplane-modelplane}"
NS=ml-team
TYPE_SPEED="${TYPE_SPEED:-0.03}"
READ_PAUSE="${READ_PAUSE:-5}"
for t in kubectl curl jq awk; do command -v "$t" >/dev/null || { echo "missing $t"; exit 1; }; done
trap 'pkill -f "port-forward.*svc/traefik.*8080:80" 2>/dev/null' EXIT

GRN=$'\033[1;32m'; BOLD=$'\033[1m'; DIM=$'\033[2m'; RST=$'\033[0m'
pause(){ if [ -n "${STEP:-}" ]; then read -r _; else sleep "${1:-$READ_PAUSE}"; fi; }
banner(){ printf '\n%s# %s%s\n' "$DIM" "$1" "$RST"; pause 2; }
run(){ printf '%s$ %s%s' "$GRN" "$RST" "$BOLD"; local s=$1 i; for ((i=0;i<${#s};i++)); do printf '%s' "${s:i:1}"; sleep "$TYPE_SPEED"; done; printf '%s\n' "$RST"; sleep 0.5; eval "$s"; pause; }
# Print just the YAML docs of the given kinds from a manifest (keeps inline comments).
manifest(){ awk -v re="$2" 'BEGIN{RS="\n---\n"} $0 ~ ("\nkind: ("re")"){gsub(/^\n+|\n+$/,"");print;print "---"}' "$1"; }

clear
banner "Modelplane separates two roles. The PLATFORM TEAM builds a GPU fleet with published hardware capabilities. The ML TEAM deploys models against those capabilities — by capability, never by cluster name."
banner "The control plane is already running on kind: Crossplane, the Modelplane package, and GCP credentials."

# ============================ Part 1: first deployment ============================
banner "Part 1 (first-deployment.md) — stand up the platform and serve one model."

banner "PLATFORM TEAM installs the control-plane routing gateway. It runs Traefik (+ MetalLB on kind) so every ModelService gets one stable address:"
run 'cat ../../qwen-demo/01-gateway.yaml'
run 'kubectl --context $CP apply -f ../../qwen-demo/01-gateway.yaml'
run 'kubectl --context $CP get inferencegateway default'

banner "PLATFORM TEAM publishes a hardware class (machine type, accelerator, capacity) and a cluster that offers it:"
run "manifest 01-first-deployment.yaml 'InferenceClass|InferenceCluster'"

banner "Both are live — Modelplane provisioned the GKE cluster from that declaration:"
run 'kubectl --context $CP get inferenceclass gke-l4-1x-g2'
run 'kubectl --context $CP get inferencecluster starter -L modelplane.ai/region'

banner "ML TEAM declares a model and one endpoint — the selector asks for GPU memory (>= 20Gi), never a cluster name:"
run "manifest 01-first-deployment.yaml 'ModelDeployment|ModelService'"

banner "Both live; the ModelService exposes one OpenAI endpoint at /ml-team/qwen:"
run 'kubectl --context $CP -n ml-team get modeldeployment qwen-demo'
run 'kubectl --context $CP -n ml-team get modelservice qwen'

banner "That address (172.18.x) is a kind MetalLB IP — not routable from the host. Port-forward the gateway, then it's a plain OpenAI call:"
run 'kubectl --context $CP -n traefik-system port-forward svc/traefik 8080:80 >/tmp/qwen-pf.log 2>&1 &'
run $'curl -s http://localhost:8080/ml-team/qwen/v1/chat/completions \\\n  -H \'content-type: application/json\' \\\n  -d \'{"model":"Qwen/Qwen2.5-0.5B-Instruct","messages":[{"role":"user","content":"What is Crossplane in one sentence?"}],"max_tokens":60}\' | jq -r \'.choices[0].message.content\''

# ========================= Part 2: scale to a fleet =========================
banner "Part 2 (scale-to-fleet.md) — the model needs more GPU. The platform grows; the ML team edits the same deployment. No model swap, no cluster names."

banner "PLATFORM TEAM adds a bigger hardware class (A100 40GB) and two clusters that offer it, in new regions:"
run "manifest 02-scale-to-fleet.yaml 'InferenceClass|InferenceCluster'"

banner "The fleet now spans three regions, alongside the Part-1 L4 starter:"
run 'kubectl --context $CP get inferencecluster -L modelplane.ai/region'

banner "ML TEAM edits the SAME qwen-demo — more replicas and a selector asking for >= 35Gi. The L4 (24Gi) no longer qualifies:"
run "manifest 02-scale-to-fleet.yaml 'ModelDeployment'"
run 'kubectl --context $CP apply -f 02-scale-to-fleet.yaml'

banner "Scheduled by capability — the replicas land on A100 capacity, the L4 starter skipped:"
run 'kubectl --context $CP -n ml-team get modelreplica -L modelplane.ai/deployment,modelplane.ai/cluster'

banner "The endpoint URL never changed — same call, same /ml-team/qwen path, now served from the A100s:"
run $'curl -s http://localhost:8080/ml-team/qwen/v1/chat/completions \\\n  -H \'content-type: application/json\' \\\n  -d \'{"model":"Qwen/Qwen2.5-0.5B-Instruct","messages":[{"role":"user","content":"Reverse a linked list in Python:"}],"max_tokens":60}\' | jq -r \'.choices[0].message.content\''

banner "Platform team publishes capabilities. ML team requests them. Modelplane schedules and serves — one endpoint throughout, no cluster names."
pause 3
