#!/usr/bin/env bash
# Test the qwen-demo endpoint by running an ephemeral curl pod inside
# the control plane cluster. This works regardless of whether the
# Gateway's MetalLB IP is routable from the host.
set -euo pipefail

kubectl run -i --rm curl-test --image=curlimages/curl --restart=Never -- \
  curl -s --max-time 30 http://172.18.255.200/ml-team/qwen-demo/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen","messages":[{"role":"user","content":"What is Crossplane?"}],"max_tokens":40}'
