#!/usr/bin/env bash
# Provision the GKE InferenceEnvironment and wait for it to be ready.
# Run this 30-40 min before the demo if the environment was torn down.
set -euo pipefail

echo "==> Checking prerequisites"
CTX=$(kubectl config current-context)
if [[ "$CTX" != "kind-modelplane" ]]; then
  echo "    ERROR: kubectl context is '$CTX', expected 'kind-modelplane'"
  exit 1
fi

IG_READY=$(kubectl get ig default -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "False")
if [[ "$IG_READY" != "True" ]]; then
  echo "    ERROR: InferenceGateway not ready. Create it first:"
  echo "      kubectl apply -f examples/platform/inference-gateway.yaml"
  exit 1
fi
echo "    InferenceGateway: ready"

echo "==> Creating InferenceEnvironment (GKE + L4 GPU)"
kubectl apply -f - <<'EOF'
apiVersion: modelplane.ai/v1alpha1
kind: InferenceEnvironment
metadata:
  name: gke-us-central
  labels:
    modelplane.ai/environment: "true"
    modelplane.ai/region: us-central
spec:
  backend: KServe
  kserve:
    version: v0.16.0
    cluster:
      source: GKE
      gke:
        project: crossplane-playground
        region: us-central1
        nodePools:
          - name: system
            role: System
            machineType: e2-standard-4
            nodeCount: 1
            minNodeCount: 1
            maxNodeCount: 2
          - name: gpu-l4
            role: GPU
            machineType: g2-standard-8
            gpu:
              acceleratorType: nvidia-l4
              acceleratorCount: 1
            nodeCount: 1
            minNodeCount: 0
            maxNodeCount: 4
            zones:
              - us-central1-a
              - us-central1-c
EOF

echo "==> Waiting for InferenceEnvironment to be ready (~20-30 min)..."
kubectl wait inferenceenvironment gke-us-central --for=condition=Ready --timeout=2400s

echo ""
echo "============================================"
echo "  INFRA READY"
echo "============================================"
echo ""
echo "  Now run: ./demo/predemo.sh"
