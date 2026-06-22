#!/usr/bin/env bash
# Create a local kind cluster to use as a Modelplane inference (workload)
# cluster — separate from the control plane — with two node pools of different
# (fake) GPU sizes, so you can watch Modelplane schedule a model by capability.
#
# It: creates a 2-worker kind cluster, installs a mock DRA driver (advertises
# fake GPUs so a GPU-less node satisfies the scheduler), labels each worker into
# a pool, and registers the cluster's kubeconfig as a Secret on the control
# plane for a `source: Existing` InferenceCluster.
#
# Prereqs: a separate kind cluster running the Modelplane control plane, plus
# kind, kubectl, helm, and docker. After this, apply the manifests here.
set -euo pipefail

CLUSTER="${CLUSTER:-mp-fleet}"                          # workload kind cluster name
CP_CONTEXT="${CP_CONTEXT:-kind-crossplane-modelplane}"  # control-plane kube context
SECRET="${SECRET:-mp-fleet-kubeconfig}"                 # secret name in modelplane-system

echo ">> creating 2-worker workload kind cluster '$CLUSTER'"
cat <<KIND | kind create cluster --name "$CLUSTER" --config -
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
- role: worker
- role: worker
KIND
WCTX="kind-$CLUSTER"
kubectl --context "$WCTX" wait --for=condition=Ready nodes --all --timeout=120s

echo ">> installing the mock DRA driver (fake GPUs, no hardware)"
helm install dra-example-driver \
  oci://registry.k8s.io/dra-example-driver/charts/dra-example-driver \
  --kube-context "$WCTX" --namespace dra-example-driver --create-namespace --wait

echo ">> installing MetalLB (the workload's gateway needs a LoadBalancer IP; kind has no cloud LB)"
# Pool is in the kind docker network (172.18.0.0/16), distinct from the control
# plane's own MetalLB range. Adjust if your kind network differs:
#   docker network inspect kind --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}'
kubectl --context "$WCTX" apply -f https://raw.githubusercontent.com/metallb/metallb/v0.14.9/config/manifests/metallb-native.yaml
kubectl --context "$WCTX" -n metallb-system wait --for=condition=Available deploy/controller --timeout=180s
kubectl --context "$WCTX" apply -f - <<MLB
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata: { name: kind-pool, namespace: metallb-system }
spec:
  addresses: ["172.18.255.150-172.18.255.199"]
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata: { name: l2, namespace: metallb-system }
spec:
  ipAddressPools: [kind-pool]
MLB

echo ">> labeling the two workers into pools (fake-l4 / fake-a100)"
# Read into an array without mapfile/readarray (bash 4+), so this also runs on
# the bash 3.2 that ships with macOS.
WORKERS=()
while IFS= read -r w; do WORKERS+=("$w"); done < <(kubectl --context "$WCTX" get nodes \
  -l '!node-role.kubernetes.io/control-plane' -o name | cut -d/ -f2)
kubectl --context "$WCTX" label node "${WORKERS[0]}" modelplane.ai/pool=fake-l4   --overwrite
kubectl --context "$WCTX" label node "${WORKERS[1]}" modelplane.ai/pool=fake-a100 --overwrite

echo ">> registering the workload kubeconfig on the control plane"
# --internal gives the in-docker-network address, reachable from the control
# plane's pods (the default kubeconfig points at 127.0.0.1, which they can't reach).
kind get kubeconfig --name "$CLUSTER" --internal > "/tmp/${CLUSTER}-kubeconfig"
kubectl --context "$CP_CONTEXT" -n modelplane-system create secret generic "$SECRET" \
  --from-file="kubeconfig=/tmp/${CLUSTER}-kubeconfig" \
  --dry-run=client -o yaml | kubectl --context "$CP_CONTEXT" apply -f -
rm -f "/tmp/${CLUSTER}-kubeconfig"

cat <<EOF

Done. Workload cluster '$CLUSTER' (pools fake-l4 + fake-a100) is registered as
Secret 'modelplane-system/$SECRET' on the control plane.

Next, against the control plane ($CP_CONTEXT):
  kubectl create namespace ml-team
  kubectl apply -f .
EOF
