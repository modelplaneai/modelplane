#!/usr/bin/env bash
# Two-cluster local e2e (no cloud, no GPU). Usually invoked via
# `nix run .#e2e` (which provides the tooling and the Nix-built function
# images). See README.md.
#
# Two clusters, because the control-plane InferenceGateway (Traefik) and the
# workload ServingStack (Envoy) both install the Gateway API CRDs — on one
# cluster they race for the same cluster-scoped CRDs and the gateway wedges. So:
#   - a workload kind cluster (this script creates it), registered via
#     source: Existing, where the serving stack + model run;
#   - a control-plane cluster (crossplane project run manages it) with crossplane
#     + the config + the InferenceGateway.
set -euo pipefail

CP=modelplane-e2e-local
WL=modelplane-e2e-workload
# Pinned so the workload cluster has the DRA APIs the serving stack's NVIDIA DRA
# driver needs (resource.k8s.io, GA in k8s 1.34). The control-plane cluster that
# project run creates needs no DRA, so its image doesn't matter here.
WL_NODE_IMAGE=kindest/node:v1.34.0@sha256:7416a61b42b1662ca6ca89f02028ac133a309a2a30ba309614e8ec94d976dc5a
METALLB_URL=https://raw.githubusercontent.com/metallb/metallb/v0.14.8/config/manifests/metallb-native.yaml
# Pinned by digest (a multi-arch manifest list) so a moving :latest can't flake
# the verify curl pod.
CURL_IMAGE=curlimages/curl@sha256:7c12af72ceb38b7432ab85e1a265cff6ae58e06f95539d539b654f2cfa64bb13
ROOT="$(git rev-parse --show-toplevel)"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

if [ "${1:-}" = "--clean" ]; then
	# Always delete both clusters — don't gate kind delete on project stop's exit
	# code (it can exit 0 without removing the cluster). project stop is
	# best-effort for the local registry it also manages.
	crossplane project stop --control-plane-name "$CP" 2>/dev/null || true
	kind delete cluster --name "$CP" || true
	kind delete cluster --name "$WL" || true
	docker rm -f "${CP}-registry" >/dev/null 2>&1 || true
	exit 0
fi

# One temp dir for everything this run creates (the isolated Docker config and
# the rendered manifests), removed on exit. mktemp -d gives a fresh unique path
# and the trap captures it on the next line, so the rm -rf can never reach a real
# directory. --no-apply clears the trap to keep the dir for manual apply.
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

WLCTX="kind-$WL"
if kind get clusters 2>/dev/null | grep -qx "$WL"; then
	# Reuse an existing workload cluster only if it's the pinned version. An
	# older one lacks the DRA APIs and would fail the run confusingly later.
	ver="$(kubectl --context "$WLCTX" get nodes -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}' 2>/dev/null || true)"
	case "$ver" in
	v1.34.*) log "Reusing workload cluster $WL ($ver)" ;;
	*)
		echo "workload cluster $WL is ${ver:-unreachable}, but v1.34 is required for the DRA APIs; recreate it with: nix run .#e2e -- --clean" >&2
		exit 1
		;;
	esac
else
	log "Creating workload cluster $WL (k8s v1.34, for DRA)"
	kind create cluster --name "$WL" --image "$WL_NODE_IMAGE"
fi

# Both kind clusters share one Docker network; MetalLB hands out LoadBalancer IPs
# from it, and the control plane must ROUTE to the workload gateway's IP across
# it. So the pools must sit inside the *actual* kind subnet — normally
# 172.18.0.0/16, but kind bumps to 172.19/172.20/... when earlier Docker networks
# already hold 172.18. Detect it and derive both pools (this one and the
# InferenceGateway's) from the same prefix; a hardcoded 172.18 leaves the LB IP
# off-subnet and silently breaks cross-cluster routing (curl times out).
# `|| true` so a detection miss (grep finds nothing) doesn't trip set -e here —
# the explicit check below then reports it instead of an opaque abort.
SUBNET="$(docker network inspect kind -f '{{range .IPAM.Config}}{{println .Subnet}}{{end}}' | grep -E '^[0-9]+\.' | head -1 || true)"
PREFIX="$(printf '%s' "$SUBNET" | cut -d. -f1-2)"
[ -n "$PREFIX" ] || {
	echo "could not detect the kind Docker subnet" >&2
	exit 1
}
log "kind Docker subnet ${SUBNET} -> MetalLB pools ${PREFIX}.255.x"

# The serving stack doesn't install MetalLB, so the workload cluster needs it
# here. Both gateways live on this cluster now — the cluster gateway fronting the
# engine pods, and the fleet gateway callers reach — so the pool has to be big
# enough for two LoadBalancer Services.
log "Installing MetalLB on the workload cluster (pool ${PREFIX}.255.100-.149)"
kubectl --context "$WLCTX" apply -f "$METALLB_URL"
kubectl --context "$WLCTX" -n metallb-system rollout status deploy/controller --timeout=180s
kubectl --context "$WLCTX" apply -f - <<POOL
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata: { name: kind-pool, namespace: metallb-system }
spec: { addresses: ["${PREFIX}.255.100-${PREFIX}.255.149"] }
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata: { name: kind-l2, namespace: metallb-system }
spec: { ipAddressPools: [kind-pool] }
POOL

# Fake DRA GPUs so a `claim: DRA` engine's ResourceClaim binds on this GPU-less
# node (vendored dra-example-driver — see dra-example-driver.yaml). Without a DRA
# driver the ResourceClaim stays Pending and the engine pod never schedules; the
# fleet scheduler also rejects an engine whose only device is Synthetic.
log "Installing dra-example-driver (fake GPUs) on the workload cluster"
kubectl --context "$WLCTX" apply -f "$ROOT/e2e/dra-example-driver.yaml"
kubectl --context "$WLCTX" -n dra-example-driver rollout status ds/dra-example-driver-kubeletplugin --timeout=120s

# BYO clusters aren't labelled by Modelplane; the gpu-synthetic pool selects on this.
log "Labelling the workload node for pool gpu-synthetic"
kubectl --context "$WLCTX" label node "${WL}-control-plane" modelplane.ai/pool=gpu-synthetic --overwrite

# No system PATH in the nix app, so a Docker config using credsStore "desktop"
# would break package resolution. The provider packages are public.
docker_config="$work/docker"
mkdir -p "$docker_config"
printf '{}' >"$docker_config/config.json"
export DOCKER_CONFIG="$docker_config"

# Render the manifests with the detected subnet prefix (the InferenceGateway's
# MetalLB addressPool is the only IP baked into them). Flags pick the mode:
#   --no-apply  install and finish the control plane but skip the model
#               manifests — for gradual, manual apply/debugging.
#   --verify    after apply, wait for the ModelService and assert a live 200,
#               exiting non-zero on failure. This is exactly what CI runs, so
#               running it locally gives the same pass/fail signal (dev/CI parity).
rendered="$work/rendered"
mkdir -p "$rendered"
cp "$ROOT/e2e/manifests/"*.yaml "$rendered/"
sed -i.bak "s/172\.18\.255/${PREFIX}.255/g" "$rendered/"*.yaml && rm -f "$rendered/"*.bak
cpctx="kind-$CP"
apply_manifests=1
verify=0
case "${1:-}" in
--no-apply) apply_manifests=0 ;;
--verify) verify=1 ;;
esac

log "Building + running the control plane"
cd "$ROOT"
# Install the config with the lean control-plane trims (narrowed MRAP + scale-to-0)
# applied before the providers. prerequisites.yaml is applied afterwards with
# kubectl, not through --init-resources: it opens with a comment-only YAML
# document that `crossplane project run` rejects but kubectl skips.
crossplane project run \
	--control-plane-name "$CP" --cluster-admin --timeout 25m \
	--init-resources "$ROOT/e2e/lean-control-plane.yaml" \
	--crossplane-version=2.3.4

# Config healthy. Finish the setup the getting-started flow does by hand (as the
# nix run app now does too, PR #375): apply the RBAC prerequisites, then point
# the two providers at the DeploymentRuntimeConfigs they define. Providers
# install before prerequisites.yaml, and an ImageConfig binds only at
# ProviderRevision creation, so provider-helm otherwise comes up without the
# granted RBAC and provider-kubernetes without --sanitize-secrets.
log "Finishing control-plane setup: prerequisites + provider runtime configs"
kubectl --context "$cpctx" apply -f "$ROOT/docs/manifests/getting-started/prerequisites.yaml"
kubectl --context "$cpctx" patch provider.pkg.crossplane.io upbound-provider-helm --type merge \
	-p '{"spec":{"runtimeConfigRef":{"apiVersion":"pkg.crossplane.io/v1beta1","kind":"DeploymentRuntimeConfig","name":"provider-helm-modelplane"}}}'
kubectl --context "$cpctx" patch provider.pkg.crossplane.io upbound-provider-kubernetes --type merge \
	-p '{"spec":{"runtimeConfigRef":{"apiVersion":"pkg.crossplane.io/v1beta1","kind":"DeploymentRuntimeConfig","name":"provider-kubernetes-modelplane"}}}'

# The InferenceCluster (source: Existing) reads this kubeconfig to reach the
# workload cluster; --internal gives an address routable from the control plane's
# provider pods. It lives in modelplane-system, created by prerequisites.yaml above.
{
	printf 'apiVersion: v1\nkind: Secret\nmetadata: {name: local-cluster-kubeconfig, namespace: modelplane-system}\nstringData:\n  kubeconfig: |\n'
	kind get kubeconfig --internal --name "$WL" | sed 's/^/    /'
} | kubectl --context "$cpctx" apply -f -

if [ "$apply_manifests" = 0 ]; then
	trap - EXIT # keep $work so the rendered manifests survive for manual apply
	log "--no-apply: control plane ready; apply manifests from $rendered (kept for you)"
	exit 0
fi

# RBAC is in place, so the compositions can reach the workload cluster. Apply the
# model manifests. Modelplane derives the cluster gateway's name and composes the
# Service that resolves it, so nothing here publishes DNS.
kubectl --context "$cpctx" apply -f "$rendered/"

if [ "$verify" = 0 ]; then
	log "Done. Curl the ModelService per the README; clean up with: nix run .#e2e -- --clean"
	exit 0
fi

# --verify: project run returns once the config is healthy and the resources are
# applied, so the serving-stack install and model rollout are still reconciling.
# Wait for the ModelService to publish an address, then route a real request to
# the engine and assert a 200. Any failure exits non-zero — that is what makes
# this usable as a CI gate.
log "Verifying the model serves end to end"
ns=ml-team
svc=mock

# Wait for the ModelService to report RoutingReady, which means its route is
# composed and applied on every gateway serving it. status.model and the
# gateway's endpoints both publish long before that - neither depends on a
# replica existing - so gating on either would start curling while the engine is
# still rolling out.
ready=""
for _ in $(seq 1 80); do
	ready="$(kubectl --context "$cpctx" -n "$ns" get modelservice "$svc" \
		-o jsonpath='{.status.conditions[?(@.type=="RoutingReady")].status}' 2>/dev/null || true)"
	[ "$ready" = "True" ] && break
	sleep 15
done
[ "$ready" = "True" ] || {
	echo "verify: ModelService $ns/$svc never became RoutingReady" >&2
	kubectl --context "$cpctx" -n "$ns" get modelservice "$svc" -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.reason}: {.message}{"\n"}{end}' >&2 || true
	kubectl --context "$cpctx" -n "$ns" get modelendpoint -o wide >&2 || true
	kubectl --context "$cpctx" -n "$ns" get modelreplica -o wide >&2 || true
	exit 1
}

# A caller names a ModelService as the request's model, so read it from status.
model="$(kubectl --context "$cpctx" -n "$ns" get modelservice "$svc" -o jsonpath='{.status.model}')"
[ -n "$model" ] || {
	echo "verify: ModelService $ns/$svc published no model name" >&2
	exit 1
}

base=""
for _ in $(seq 1 80); do
	base="$(kubectl --context "$cpctx" get inferencegateway local -o jsonpath='{.status.endpoints.openAI}' 2>/dev/null || true)"
	[ -n "$base" ] && break
	sleep 15
done
[ -n "$base" ] || {
	echo "verify: InferenceGateway local never published an OpenAI endpoint" >&2
	exit 1
}
log "Gateway ${base}, model ${model}"

# The address is on the kind Docker subnet the host can't route to on macOS, so
# curl from a pod on the control plane, reading the status from the pod's logs
# (not `run -i`, whose attach drops output on a headless runner). curl_status
# runs one throwaway pod per call and echoes the HTTP code; it polls the logs
# (curl writes the code once, then exits) so a failed attempt costs seconds, and
# a unique pod name per call keeps retries from reading a prior pod's output.
# GET a URL from the workload cluster, reporting curl's own exit code rather
# than an HTTP status. Used to assert a request is refused before there is any
# HTTP response to report. -k skips server verification, so a non-zero exit is
# the server rejecting us rather than us rejecting its certificate.
wl_curl_exit() {
	local pod="$1" url="$2"
	kubectl --context "$WLCTX" -n default run "$pod" --restart=Never \
		--labels=app.kubernetes.io/name=e2e-verify --image="$CURL_IMAGE" \
		--command -- sh -c "curl -sS -k --max-time 15 -o /dev/null \"$url\"; echo EXIT=\$?" \
		>/dev/null 2>&1 || true
	local c=""
	for _ in $(seq 1 30); do
		c="$(kubectl --context "$WLCTX" -n default logs "$pod" 2>/dev/null | sed -n 's/.*EXIT=\([0-9]*\).*/\1/p' || true)"
		[ -n "$c" ] && break
		sleep 2
	done
	kubectl --context "$WLCTX" -n default delete pod "$pod" --now >/dev/null 2>&1 || true
	printf '%s' "$c"
}

curl_status() {
	local pod="$1" url="$2"
	shift 2
	kubectl --context "$cpctx" -n "$ns" run "$pod" --restart=Never \
		--labels=app.kubernetes.io/name=e2e-verify --image="$CURL_IMAGE" \
		--command -- curl -sS --max-time 15 -o /dev/null -w '%{http_code}' "$url" "$@" \
		>/dev/null 2>&1 || true
	local c=""
	for _ in $(seq 1 30); do
		c="$(kubectl --context "$cpctx" -n "$ns" logs "$pod" 2>/dev/null | tr -dc '0-9' || true)"
		[ -n "$c" ] && break
		sleep 2
	done
	printf '%s' "$c"
}

# curl_body is the same, but returns the response body. Used where the assertion
# is about what came back rather than only that something did.
curl_body() {
	local pod="$1" url="$2"
	shift 2
	kubectl --context "$cpctx" -n "$ns" run "$pod" --restart=Never \
		--labels=app.kubernetes.io/name=e2e-verify --image="$CURL_IMAGE" \
		--command -- curl -sS --max-time 15 "$url" "$@" >/dev/null 2>&1 || true
	local b=""
	for _ in $(seq 1 30); do
		b="$(kubectl --context "$cpctx" -n "$ns" logs "$pod" 2>/dev/null || true)"
		[ -n "$b" ] && break
		sleep 2
	done
	printf '%s' "$b"
}

cleanup_verify_pods() {
	kubectl --context "$cpctx" -n "$ns" delete pod -l app.kubernetes.io/name=e2e-verify --now >/dev/null 2>&1 || true
}

# OpenAI /v1/chat/completions, retried: the gateway can publish an endpoint a
# moment before the route is serving, and a slower CI runner widens that gap.
oai='{"model":"'"$model"'","messages":[{"role":"user","content":"ping"}]}'
code=""
for attempt in $(seq 1 10); do
	code="$(curl_status "e2e-verify-oai-$attempt" "$base/chat/completions" -H 'content-type: application/json' -d "$oai")"
	log "verify attempt $attempt (OpenAI): HTTP ${code:-none}"
	[ "$code" = "200" ] && break
	sleep 10
done
[ "$code" = "200" ] || {
	echo "verify: $base/chat/completions did not return 200 within retries (last: ${code:-none})" >&2
	cleanup_verify_pods
	exit 1
}

# The engine only answers to the name Modelplane started it under, and rejects
# anything else with a 404. So a 200 above already proves the gateway rewrote the
# caller's ModelService name to the deployment's. Assert the response reports the
# served model rather than what the caller asked for, which is the visible half
# of the same mechanism.
body="$(curl_body e2e-verify-served "$base/chat/completions" -H 'content-type: application/json' -d "$oai")"
case "$body" in
*'"model": "ml-team/mock-demo"'* | *'"model":"ml-team/mock-demo"'*)
	log "verify (model rewriting): caller asked for ${model}, engine served ml-team/mock-demo"
	;;
*)
	echo "verify: response did not report the served model; got: $body" >&2
	cleanup_verify_pods
	exit 1
	;;
esac

# A model no ModelService claims must not route anywhere. Catches a route
# matching too broadly, which would send a caller to an arbitrary backend.
ncode="$(curl_status e2e-verify-unknown "$base/chat/completions" -H 'content-type: application/json' \
	-d '{"model":"ml-team/nope","messages":[{"role":"user","content":"ping"}]}')"
log "verify (unknown model): HTTP ${ncode:-none}"
[ "$ncode" = "200" ] && {
	echo "verify: an unclaimed model name was routed and served" >&2
	cleanup_verify_pods
	exit 1
}

# The cluster gateway must refuse a caller that presents no client certificate.
# This is the property the whole mTLS design exists for, and every check above
# goes through the fleet gateway, which does hold a certificate, so none of them
# would notice it lapsing. A ClientTrafficPolicy that stopped applying, or an
# HTTP listener creeping back, would leave the engines open to anything that can
# reach the load balancer.
#
# Run from the workload cluster, where the Service compose-inference-gateway
# composed resolves the gateway's name. A plain GET is enough: the handshake
# fails before any request is sent. The trailing dot skips the pod's search
# domains, which ndots:5 would otherwise try ahead of the name itself. A resolve
# failure returns curl 6, which the checks below reject rather than pass.
cluster_gw_name="$(kubectl --context "$cpctx" get inferencecluster local -o jsonpath='{.status.gateway.hostname}')"
cluster_gw="https://${cluster_gw_name}./v1/models"
ecode="$(wl_curl_exit e2e-verify-nocert "$cluster_gw")"
log "verify (cluster gateway, no client certificate): curl exit ${ecode:-none}"
case "$ecode" in
0)
	echo "verify: the cluster gateway served a caller presenting no client certificate" >&2
	cleanup_verify_pods
	exit 1
	;;
35 | 52 | 55 | 56) ;;
*)
	echo "verify: expected the cluster gateway to refuse an uncertified caller mid-handshake," >&2
	echo "verify: but curl failed with ${ecode:-no exit code}, which is a different failure" >&2
	cleanup_verify_pods
	exit 1
	;;
esac

# And nothing on port 80. HTTPS replaces the HTTP listener rather than joining
# it, because the serving HTTPRoutes carry no sectionName and so attach to every
# listener there is. The load balancer publishes a port per listener, so with
# only an HTTPS listener nothing is listening on 80 and the connection is
# refused.
hcode="$(wl_curl_exit e2e-verify-plaintext "http://${cluster_gw_name}./v1/models")"
log "verify (cluster gateway, plaintext): curl exit ${hcode:-none}"
case "$hcode" in
0)
	echo "verify: the cluster gateway served plaintext HTTP on port 80" >&2
	cleanup_verify_pods
	exit 1
	;;
7 | 28 | 35 | 52 | 56) ;;
*)
	echo "verify: expected no listener on port 80, but curl failed with ${hcode:-no exit code}," >&2
	echo "verify: which is a different failure" >&2
	cleanup_verify_pods
	exit 1
	;;
esac

# /v1/models lists what this gateway serves. Only exact model matches appear, so
# this also proves the route matches exactly rather than by pattern.
models="$(curl_body e2e-verify-models "$base/models")"
case "$models" in
*"$model"*) log "verify (/v1/models): lists ${model}" ;;
*)
	echo "verify: /v1/models did not list $model; got: $models" >&2
	cleanup_verify_pods
	exit 1
	;;
esac

# Anthropic's Messages API on the same gateway, translated to the OpenAI the mock
# engine speaks. The engine has no Anthropic route, so a 200 here is translation
# rather than passthrough.
anthropic_base="${base%/v1}/anthropic/v1"
ant='{"model":"'"$model"'","max_tokens":16,"messages":[{"role":"user","content":"ping"}]}'
mcode="$(curl_status e2e-verify-anthropic "$anthropic_base/messages" -H 'content-type: application/json' -H 'anthropic-version: 2023-06-01' -d "$ant")"
log "verify (Anthropic /v1/messages): HTTP ${mcode:-none}"
[ "$mcode" = "200" ] || {
	echo "verify: $anthropic_base/messages did not return 200 (got: ${mcode:-none})" >&2
	cleanup_verify_pods
	exit 1
}

# The usage record is the only place a token count and the tenant that incurred
# it are visible together, so the whole metering story rests on the gateway
# emitting one. Read it off the fleet gateway's Envoy.
envoy_pod="$(kubectl --context "$WLCTX" -n envoy-gateway-system get pods \
	-l gateway.envoyproxy.io/owning-gateway-name=fleet-gateway -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
[ -n "$envoy_pod" ] || {
	echo "verify: could not find the fleet gateway's Envoy pod" >&2
	cleanup_verify_pods
	exit 1
}
usage="$(kubectl --context "$WLCTX" -n envoy-gateway-system logs "$envoy_pod" -c envoy --tail=200 2>/dev/null |
	grep '"input_tokens":12' | tail -1 || true)"
# Check each field on its own. The access log serialises its keys
# alphabetically, so a single glob spanning two of them depends on that order.
#
# caller is deliberately not asserted: this gateway sets no auth, so no caller
# is authenticated and none is stamped. Metering per caller is covered by the
# unit tests and was verified by hand against a gateway that does authenticate.
missing=""
for want in \
	'"service":"'"$model"'"' \
	'"endpoint":"modelplane-system/ml-team-mock' \
	'"served_model":"ml-team/mock-demo"' \
	'"input_tokens":12' \
	'"output_tokens":9' \
	'"total_tokens":21' \
	'"status":200'; do
	case "$usage" in
	*"$want"*) ;;
	*) missing="$missing $want" ;;
	esac
done
[ -z "$missing" ] || {
	echo "verify: usage record missing:$missing" >&2
	echo "verify: record was: ${usage:-none}" >&2
	cleanup_verify_pods
	exit 1
}
log "verify (usage record): ${usage}"

cleanup_verify_pods
log "End to end OK: ${base} serves ${model} over OpenAI and Anthropic, rewrites the model, and meters it"
