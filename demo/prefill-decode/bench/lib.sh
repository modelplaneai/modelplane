CP="${CP:-gke_crossplane-playground_us-central1-a_modelplane-cp}"
NS="${NS:-ml-team}"

# Discover the workload (disagg) cluster context by name fragment.
workload_ctx() {
  kubectl config get-contexts -o name 2>/dev/null | grep -iE "pd-guide|pd-demo" | head -1
}

# Gateway external IP on the workload cluster.
gateway_ip() {
  local wctx="$1"
  kubectl --context "$wctx" get gateway -n modelplane-system inference-gateway \
    -o jsonpath='{.status.addresses[0].value}' 2>/dev/null
}

# Route path prefix matching a substring (e.g. "qwen").
route_prefix() {
  local wctx="$1" match="$2"
  kubectl --context "$wctx" get httproute -A \
    -o jsonpath='{range .items[*]}{.spec.rules[0].matches[0].path.value}{"\n"}{end}' 2>/dev/null \
    | grep -i "$match" | head -1
}

# Scrape a vllm metric from an engine container. port 8000 (prefill/unified) or 8001 (decode).
engine_metric() {
  local wctx="$1" pod="$2" port="$3" metric="$4"
  kubectl --context "$wctx" exec "$pod" -c engine -- \
    sh -c "curl -s localhost:$port/metrics" 2>/dev/null \
    | awk -v m="^$metric" '$0 ~ m {print $2; exit}'
}
