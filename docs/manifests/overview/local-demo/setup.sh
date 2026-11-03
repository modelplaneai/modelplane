#!/usr/bin/env bash
# Sets up the Modelplane quickstart on two kind clusters: a control plane, and
# a workload cluster that stands in for a GPU cluster. It ends with the
# platform team's side done (hardware published, cluster registered, gateway
# up) so the reader only deploys models.
#
# Needs: docker (12 GB of memory on Docker Desktop), kind, kubectl, helm.
#   setup.sh          set everything up
#   setup.sh --clean  delete both clusters
set -euo pipefail

DOCS=https://docs.modelplane.ai/examples
REF=$(curl -fsSL "${DOCS}/install/configuration.yaml" | sed -n 's/.*modelplane:\(v[^[:space:]]*\).*/\1/p')
# Comment the line above and uncomment the line below with the latest release for local testing
# REF=v0.6.0 or above
RAW="https://raw.githubusercontent.com/modelplaneai/modelplane/${REF}"
CP=modelplane
WL=modelplane-workload
CPCTX="kind-${CP}"
WLCTX="kind-${WL}"
# Avoid v1.34.0/1: their kubelets deadlock on an idle DRA connection.
NODE_IMAGE=kindest/node:v1.36.4@sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed
METALLB_URL=https://raw.githubusercontent.com/metallb/metallb/v0.14.8/config/manifests/metallb-native.yaml

log() { printf '\n==> %s\n' "$*"; }
# shellcheck disable=SC2086  # $1 is intentionally split on IFS=. into octets.
ip2int() {
	local IFS=.
	set -- $1
	echo $((($1 << 24) + ($2 << 16) + ($3 << 8) + $4))
}
int2ip() { echo "$(($1 >> 24 & 255)).$(($1 >> 16 & 255)).$(($1 >> 8 & 255)).$(($1 & 255))"; }

if [ "${1:-}" = "--clean" ]; then
	kind delete cluster --name "$CP" || true
	kind delete cluster --name "$WL" || true
	docker rm -f modelplane-gateway >/dev/null 2>&1 || true
	exit 0
fi

for bin in docker kind kubectl helm; do
	command -v "$bin" >/dev/null || {
		echo "missing $bin" >&2
		exit 1
	}
done

# --- Workload cluster -------------------------------------------------------
log "Creating the workload cluster"
kind get clusters 2>/dev/null | grep -qx "$WL" ||
	kind create cluster --name "$WL" --image "$NODE_IMAGE"

# MetalLB stands in for a cloud load balancer. The pool sits near the top of
# the kind Docker network, where Docker doesn't hand out container addresses,
# so the control plane can reach the gateway.
SUBNET="$(docker network inspect kind -f '{{range .IPAM.Config}}{{println .Subnet}}{{end}}' |
	grep -E '^[0-9]+\.' | head -1)"
BITS="${SUBNET#*/}"
[ "$BITS" -le 26 ] || {
	echo "kind subnet ${SUBNET} is too small for a MetalLB pool" >&2
	exit 1
}
SIZE=$((1 << (32 - BITS)))
NET=$(($(ip2int "${SUBNET%/*}") & ~(SIZE - 1) & 0xFFFFFFFF))
log "Installing MetalLB"
kubectl --context "$WLCTX" apply -f "$METALLB_URL" >/dev/null
kubectl --context "$WLCTX" -n metallb-system rollout status deploy/controller --timeout=180s
kubectl --context "$WLCTX" apply -f - <<EOF
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata: { name: kind-pool, namespace: metallb-system }
spec: { addresses: ["$(int2ip $((NET + SIZE - 26)))-$(int2ip $((NET + SIZE - 6)))"] }
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata: { name: kind-l2, namespace: metallb-system }
spec: { ipAddressPools: [kind-pool] }
EOF

# Fake GPUs, published through real DRA. Models run on the CPU; the fake GPU
# gives the scheduler a device to place them on.
log "Installing fake GPUs"
kubectl --context "$WLCTX" apply -f "${RAW}/e2e/dra-example-driver.yaml" >/dev/null
kubectl --context "$WLCTX" -n dra-example-driver rollout status ds/dra-example-driver-kubeletplugin --timeout=120s
# Modelplane doesn't label nodes on a cluster it didn't create.
kubectl --context "$WLCTX" label node "${WL}-control-plane" modelplane.ai/pool=gpu-synthetic --overwrite

# --- Control plane ----------------------------------------------------------
log "Creating the control plane"
kind get clusters 2>/dev/null | grep -qx "$CP" ||
	kind create cluster --name "$CP" --image "$NODE_IMAGE"

log "Installing Crossplane"
helm repo add crossplane-stable https://charts.crossplane.io/stable >/dev/null
helm repo update crossplane-stable >/dev/null
helm upgrade --install crossplane crossplane-stable/crossplane \
	--kube-context "$CPCTX" \
	--namespace crossplane-system --create-namespace \
	--set "args={--enable-dependency-version-upgrades}" \
	--set-json 'provider.defaultActivations=[]' \
	--wait

log "Installing Modelplane ${REF}"
kubectl --context "$CPCTX" apply \
	-f "${RAW}/docs/manifests/install/prerequisites.yaml" \
	-f "${RAW}/docs/manifests/install/configuration.yaml"
kubectl --context "$CPCTX" wait configuration/modelplane --for=condition=Healthy --timeout=15m
kubectl --context "$CPCTX" wait xrd --all --for=condition=Established --timeout=5m

# --- The platform team's side ----------------------------------------------
log "Registering the workload cluster and its hardware"
kubectl --context "$CPCTX" -n modelplane-system create secret generic local-cluster-kubeconfig \
	--from-literal=kubeconfig="$(kind get kubeconfig --internal --name "$WL")" \
	--dry-run=client -o yaml | kubectl --context "$CPCTX" apply -f -
kubectl --context "$CPCTX" apply -f "${DOCS}/overview/local-demo/platform.yaml"
# Comment the line above and uncomment the line below for local testing
# kubectl --context "$CPCTX" apply -f docs/manifests/overview/local-demo/platform.yaml
log "Installing the serving stack on the workload cluster"
kubectl --context "$CPCTX" wait ic/local --for=condition=Ready --timeout=15m
kubectl --context "$CPCTX" wait ig/public --for=condition=Ready --timeout=10m
kubectl config use-context "$CPCTX" >/dev/null

# The gateway's address is on kind's Docker network, which the host can't reach
# on macOS or Windows. Forward localhost:8080 to it.
log "Exposing the gateway on localhost:8080"
GW="$(kubectl --context "$CPCTX" get ig public -o jsonpath='{.status.endpoints.openAI}' | sed -E 's#^https?://##; s#/.*##')"
docker rm -f modelplane-gateway >/dev/null 2>&1 || true
docker run -d --name modelplane-gateway --network kind -p 8080:80 \
	alpine/socat:1.8.0.3 TCP-LISTEN:80,fork "TCP:${GW}:80" >/dev/null

log "Ready. kubectl points at the control plane, and the gateway answers on localhost:8080."
