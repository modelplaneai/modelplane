#!/usr/bin/env bash
# Single entrypoint for the prefill/decode guide. Run from the repo root:
#   demo/prefill-decode/run.sh {deploy|prove|bench|promote|rollback}
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$here/bench/lib.sh"
M="$here/manifests"

deploy() {
  echo "== applying cluster + cache + deployments + service to $CP =="
  kubectl --context "$CP" apply -f "$M/01-inference-class.yaml" -f "$M/02-cluster.yaml"
  kubectl --context "$CP" -n "$NS" apply -f "$M/03-modelcache.yaml"
  kubectl --context "$CP" -n "$NS" apply -f "$M/10-qwen-unified.yaml" \
    -f "$M/11-qwen-pd.yaml" -f "$M/20-modelservice.yaml"
  echo "qwen-pd comes up at 1 replica behind the shared ModelService (an immediate ~1/3 canary)."
  echo "Watch: kubectl --context $CP -n $NS get modeldeployment,modelservice"
}

prove() {
  local wctx; wctx="$(workload_ctx)"; : "${wctx:?no workload cluster context found}"
  local gw; gw="$(gateway_ip "$wctx")"; local pre; pre="$(route_prefix "$wctx" qwen)"
  echo "== firing long prompts at $gw$pre (forces disaggregation) =="
  for i in 1 2 3 4 5; do
    p="Dossier $i: $(python3 -c "print(' '.join(['the orchestrator dispatched workload %d to a remote shard and tracked completion latency.'%$i]*60))")"
    curl -s -m 90 -o /dev/null -w "req $i: HTTP %{http_code}\n" \
      "http://$gw$pre/v1/completions" -H 'Content-Type: application/json' \
      -d "$(python3 -c "import json,sys;print(json.dumps({'model':'qwen','prompt':sys.argv[1],'max_tokens':16,'temperature':0}))" "$p")"
  done
  echo "== NIXL offload counters (prefill produces, decode consumes) =="
  for pod in $(kubectl --context "$wctx" get pods -l llm-d.ai/role=prefill -o name 2>/dev/null); do
    pod=${pod#pod/}
    echo "PREFILL $pod: prompt=$(engine_metric "$wctx" "$pod" 8000 'vllm:prompt_tokens_total') gen=$(engine_metric "$wctx" "$pod" 8000 'vllm:generation_tokens_total')"
  done
  for pod in $(kubectl --context "$wctx" get pods -l llm-d.ai/role=decode -o name 2>/dev/null); do
    pod=${pod#pod/}
    echo "DECODE  $pod: nixl_xfers=$(engine_metric "$wctx" "$pod" 8001 'vllm:nixl_xfer_time_seconds_count') nixl_bytes=$(engine_metric "$wctx" "$pod" 8001 'vllm:nixl_bytes_transferred_sum') gen=$(engine_metric "$wctx" "$pod" 8001 'vllm:generation_tokens_total')"
  done
}

bench() {
  local wctx; wctx="$(workload_ctx)"; : "${wctx:?no workload cluster context found}"
  local gw; gw="$(gateway_ip "$wctx")"; local pre; pre="$(route_prefix "$wctx" qwen)"
  : "${GUIDELLM:=uvx guidellm}"
  echo "== replaying $here/bench/replay-trace.jsonl against http://$gw$pre =="
  $GUIDELLM benchmark run \
    --target "http://$gw$pre" --backend openai_http --model qwen \
    --processor Qwen/Qwen2.5-14B-Instruct-AWQ \
    --rate-type concurrent --rate "4,16,64" --max-seconds 50 \
    --data "$here/bench/replay-trace.jsonl" \
    --output-path "$here/bench/results/replay.json"
  echo "Report: $here/bench/results/replay.json"
}

promote() {
  echo "== shifting capacity toward P/D: unified 2->1, qwen-pd 1->2 =="
  kubectl --context "$CP" -n "$NS" scale modeldeployment qwen-unified --replicas=1
  kubectl --context "$CP" -n "$NS" scale modeldeployment qwen-pd --replicas=2
  # replicas can't go to 0 (schema min is 1), so a full cutover retires the old
  # deployment, taking it out of the shared ModelService entirely:
  echo "Cut over fully with: kubectl --context $CP -n $NS delete modeldeployment qwen-unified"
}

rollback() {
  # Pull P/D out of rotation. replicas can't be 0, so deleting qwen-pd removes its
  # endpoints from the shared ModelService; traffic snaps back to unified.
  echo "== rolling back: delete qwen-pd, restore unified to 2 =="
  kubectl --context "$CP" -n "$NS" delete modeldeployment qwen-pd --ignore-not-found
  kubectl --context "$CP" -n "$NS" scale modeldeployment qwen-unified --replicas=2
}

case "${1:-}" in
  deploy) deploy ;;
  prove) prove ;;
  bench) bench ;;
  promote) promote ;;
  rollback) rollback ;;
  *) echo "usage: $0 {deploy|prove|bench|promote|rollback}"; exit 2 ;;
esac
