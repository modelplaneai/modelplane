#!/usr/bin/env bash
# Full teardown: demo workload + the InferenceCluster created by
# setup.sh. Shared infra (gateway, class, prereqs) stays — those
# are reused by other demos.
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)

# Pin the kubectl context. See demo.sh for rationale.
KCTX="${MODELPLANE_CONTEXT:-$(command kubectl config current-context)}"
kubectl() {
	command kubectl --context="$KCTX" "$@"
}
export MODELPLANE_CONTEXT="$KCTX"
echo "    Using kubectl context: $KCTX"

bash "${DIR}/cleanup-demo.sh"

echo "==> Delete InferenceCluster qwen-cached-demo"
echo "    (deprovisions the GKE cluster; can take several minutes)"
# envsubst not required for delete — the GCP_PROJECT field is opaque
# to kubectl delete. But pipe through for symmetry with setup.sh.
GCP_PROJECT="${GCP_PROJECT:-placeholder}" envsubst <"${DIR}/infra/cluster.yaml" |
	kubectl delete --ignore-not-found -f -

# HACK: Force-finalize stuck workload-cluster Helm releases + Objects.
# These are composed onto the workload cluster via provider-helm /
# provider-kubernetes. Once the GKE cluster starts deprovisioning, the
# providers can't reach the workload API server, so their delete calls
# fail. The MRs end up stuck on finalizers forever.
#
# TODO: file an issue with provider-helm / provider-kubernetes to short-
# circuit deletion when the target cluster is gone. The right fix is
# upstream — propagate "target gone" as success so the finalizer drops.
# Until then, this loop force-removes finalizers so the GKE cluster
# delete cascades cleanly.
for r in $(kubectl get release.helm.m.crossplane.io -A \
	-o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}' 2>/dev/null |
	grep qwen-cached-demo); do
	ns=${r%/*}
	name=${r#*/}
	kubectl patch -n "$ns" release.helm.m.crossplane.io "$name" \
		--type=json -p='[{"op":"remove","path":"/metadata/finalizers"}]' >/dev/null 2>&1 || true
done
for r in $(kubectl get object.kubernetes.m.crossplane.io -A \
	-o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}' 2>/dev/null |
	grep qwen-cached-demo); do
	ns=${r%/*}
	name=${r#*/}
	kubectl patch -n "$ns" object.kubernetes.m.crossplane.io "$name" \
		--type=json -p='[{"op":"remove","path":"/metadata/finalizers"}]' >/dev/null 2>&1 || true
done

echo "==> Teardown complete. Shared infra (gateway/class/prereqs) kept."
