#!/usr/bin/env bash
# Set up prerequisites for the qwen-cached-demo.
#
# Idempotent: re-running is a no-op once everything is in place.
# Assumes Modelplane Configuration is already installed on the
# control-plane cluster and Crossplane GCP provider is configured.
#
# Env vars:
#   GCP_PROJECT   GCP project ID for the GKE cluster (required)
set -euo pipefail

if [[ -z "${GCP_PROJECT:-}" ]]; then
	echo "GCP_PROJECT not set. Export your GCP project ID, e.g.:" >&2
	echo "  export GCP_PROJECT=my-gcp-project" >&2
	exit 1
fi

if ! command -v envsubst >/dev/null 2>&1; then
	echo "envsubst not found (install gettext)" >&2
	exit 1
fi

DIR=$(cd "$(dirname "$0")" && pwd)

# Pin the kubectl context. See demo.sh for rationale.
KCTX="${MODELPLANE_CONTEXT:-$(kubectl config current-context)}"
kubectl() {
	command kubectl --context="$KCTX" "$@"
}
echo "    Using kubectl context: $KCTX"

# Filestore CSI provisioner needs the Cloud Filestore API enabled on
# the project, otherwise PVCs sit Pending forever with
# SERVICE_DISABLED. Enable it before provisioning the cluster so the
# first PVC the demo creates can actually bind.
echo "==> Ensuring Cloud Filestore API is enabled on ${GCP_PROJECT}"
if command -v gcloud >/dev/null 2>&1; then
	gcloud services enable file.googleapis.com --project "${GCP_PROJECT}" >/dev/null
else
	echo "    gcloud not found; ensure file.googleapis.com is enabled on ${GCP_PROJECT}" >&2
fi

echo "==> Applying shared infrastructure prereqs (from ../qwen-demo)"
kubectl apply -f "${DIR}/../qwen-demo/00-prerequisites.yaml"
kubectl apply -f "${DIR}/../qwen-demo/01-gateway.yaml"
kubectl apply -f "${DIR}/../qwen-demo/02-class.yaml"
kubectl apply -f "${DIR}/infra/class-t4.yaml"

echo "==> Provisioning InferenceCluster (project=${GCP_PROJECT})"
envsubst <"${DIR}/infra/cluster.yaml" | kubectl apply -f -

# HACK: If a previous teardown left orphan Network / Subnetwork MRs
# in Crossplane's state, the next provisioning attempt hangs forever.
# The provider-gcp records `crossplane.io/external-create-succeeded`
# on the MR after the first successful create. If the underlying GCP
# resource is later deleted (manual cleanup, provider lost track, etc.)
# the provider will NEVER retry the create — it observes 404 forever
# and the subnetwork that depends on the network keeps 404'ing too.
#
# Failure modes to catch:
#   (a) MR.Synced = False  — stuck on a stale "not found" cached error
#   (b) MR.Ready  = True   — Crossplane thinks the resource exists but
#       GCP returns 404. The fix is to clear the
#       `crossplane.io/external-create-{succeeded,pending}` annotations
#       so the provider's create code path runs again.
#
# TODO: provider-gcp / upjet should reconcile internal state with GCP
# ground truth on observe — if observe returns 404 for a resource we
# claim to have created, drop the create-succeeded annotation
# automatically so the next reconcile retries. File upstream issue;
# this defensive cleanup is the workaround.
GCP_PROJECT_FOR_CHECK="${GCP_PROJECT}"
for kind in network.compute.gcp.m.upbound.io subnetwork.compute.gcp.m.upbound.io; do
	for r in $(kubectl get "$kind" -A \
		-o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}' 2>/dev/null |
		grep qwen-cached-demo); do
		ns=${r%/*}
		name=${r#*/}
		synced=$(kubectl get "$kind" "$name" -n "$ns" -o jsonpath='{.status.conditions[?(@.type=="Synced")].status}' 2>/dev/null)
		ready=$(kubectl get "$kind" "$name" -n "$ns" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
		# Case (a): provider stuck on a cached error
		if [[ "$synced" == "False" ]]; then
			echo "    Stuck $kind/$name (Synced=False). Force-recomposing."
			kubectl patch -n "$ns" "$kind" "$name" \
				--type=json -p='[{"op":"remove","path":"/metadata/finalizers"}]' >/dev/null 2>&1 || true
			kubectl delete -n "$ns" "$kind" "$name" --wait=false >/dev/null 2>&1 || true
			continue
		fi
		# Case (b): only checkable for Network (gcloud lookup); for
		# Ready=True but actually 404'd, clear the create annotations
		# so the provider retries the create instead of observing.
		if [[ "$ready" == "True" && "$kind" == "network.compute.gcp.m.upbound.io" ]]; then
			if command -v gcloud >/dev/null 2>&1; then
				if ! gcloud compute networks describe "$name" \
					--project "$GCP_PROJECT_FOR_CHECK" >/dev/null 2>&1; then
					echo "    Zombie $kind/$name (Ready=True but GCP 404). Clearing create annotations to force retry."
					kubectl annotate -n "$ns" "$kind" "$name" \
						crossplane.io/external-create-succeeded- \
						crossplane.io/external-create-pending- >/dev/null 2>&1 || true
				fi
			fi
		fi
	done
done

echo "==> Waiting for InferenceCluster qwen-cached-demo to be Ready"
echo "    (GKE provisioning + stack install typically 5-10 min on first run)"
kubectl wait --for=condition=Ready --timeout=20m inferencecluster/qwen-cached-demo

echo "==> Setup complete. Run ./demo.sh next."
