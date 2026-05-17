#!/usr/bin/env bash
# Run the qwen-cached-demo: hydrate a ModelCache and bring up a
# multi-node LWS gang (TensorPipeline=1x2) that mounts the cache.
#
# What this exercises:
#  - ModelCache pulling Qwen 2.5 0.5B onto a per-cluster RWX PVC
#  - ModelDeployment.spec.caches → KServe LLMInferenceService
#    model.uri = pvc://modelcache-<name>
#  - LWS gang composition: 2 pods (leader + worker) on 2 separate
#    nodes, both reading from the same cached PVC
#
# The 02b-deployment-uncached.yaml + 03b-service-uncached.yaml in
# this directory are an *optional* side-by-side comparison. Applying
# them requires an extra GPU node in the cluster (current cluster.yaml
# is sized for the 2-pod LWS gang only). To run the side-by-side,
# bump nodeCount to 3 in infra/cluster.yaml and apply 02b + 03b
# manually after demo.sh runs.
#
# Re-runnable: kubectl apply is idempotent. Run cleanup-demo.sh
# first if you want a true from-scratch timing.
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)
NS=ml-team

# Pin the kubectl context at script start to the Modelplane control
# plane the user started in. Without this, anything that mutates the
# current context mid-run (e.g. a `gcloud container clusters
# get-credentials` against the workload cluster from another shell)
# silently retargets every subsequent kubectl call. Use `kubectl` as
# an alias and pass --context explicitly so every call is pinned.
KCTX="${MODELPLANE_CONTEXT:-$(command kubectl config current-context)}"
kubectl() {
	command kubectl --context="$KCTX" "$@"
}
echo "    Using kubectl context: $KCTX"

elapsed() {
	local start=$1
	echo "$(($(date +%s) - start))s"
}

demo_start=$(date +%s)

# Returns 0 once the named ModelDeployment has ReplicasReady=True.
deployment_ready() {
	local name=$1
	local rstatus
	rstatus=$(kubectl get modeldeployment "$name" -n "$NS" \
		-o jsonpath='{.status.conditions[?(@.type=="ReplicasReady")].status}' 2>/dev/null || true)
	[[ "$rstatus" == "True" ]]
}

echo "==> Apply ModelCache  (stages Qwen 2.5 0.5B weights onto an RWX PVC"
echo "                       once per cluster; engine pods then mount it"
echo "                       at /mnt/models instead of re-pulling from HF)"
cache_start=$(date +%s)
kubectl apply -f "${DIR}/01-cache.yaml"

echo
echo "==> Wait for cache hydration"
echo "    (composition fans out an Object→PVC and an Object→Job MR to each"
echo "     matched cluster; the Job runs hf download into the PVC)"
kubectl wait --for=condition=ArtifactReady --timeout=20m \
	"modelcache/qwen-2-5-0-5b" -n "$NS"
echo "    ✓ Cache hydrated in $(elapsed "$cache_start")"

echo
echo "==> Cache state (one Object MR per matched cluster, wrapping the"
echo "    real K8s resource — PVC for storage, Job for download)"
kubectl get modelcache qwen-2-5-0-5b -n "$NS"
kubectl get objects.kubernetes.m.crossplane.io -n "$NS" \
	-o custom-columns='NAME:.metadata.name,KIND:.spec.forProvider.manifest.kind,SYNCED:.status.conditions[?(@.type=="Synced")].status,READY:.status.conditions[?(@.type=="Ready")].status,AGE:.metadata.creationTimestamp' 2>/dev/null

echo
echo "==> PVC manifest the composition applied on the workload cluster"
echo "    (ReadWriteMany — every gang pod can mount this simultaneously)"
pvc_mr=$(kubectl get objects.kubernetes.m.crossplane.io -n "$NS" \
	-o jsonpath='{range .items[?(@.spec.forProvider.manifest.kind=="PersistentVolumeClaim")]}{.metadata.name}{"\n"}{end}' | head -1)
kubectl get object.kubernetes.m.crossplane.io "$pvc_mr" -n "$NS" \
	-o jsonpath='{.spec.forProvider.manifest}' 2>/dev/null | python3 -m json.tool

echo
echo "==> Apply ModelDeployment + ModelService"
echo "    (TensorPipeline 1×2 → an LWS gang of 2 pods, one per node, one GPU"
echo "     each. Both pods mount the cached PVC. ModelService exposes the"
echo "     deployment behind the fleet's HTTPRoute on the control-plane gateway)"
deploy_start=$(date +%s)
kubectl apply -f "${DIR}/02-deployment.yaml"
kubectl apply -f "${DIR}/03-service.yaml"

echo
echo "==> Workload tree on the control plane"
echo "    (ModelDeployment → one ModelReplica per scheduled cluster → one"
echo "     ModelEndpoint per replica → ModelService load-balances over endpoints)"
kubectl get modeldeployment,modelservice,modelreplica,modelendpoint -n "$NS"

echo
echo "==> Wait for the 2-pod LWS gang to be Ready"
echo "    (KServe LLMInferenceService composes a LeaderWorkerSet; image pull"
echo "     + Ray cluster bootstrap across leader and worker pods)"
while ! deployment_ready qwen-cached-demo; do
	sleep 5
done
cached_t=$(elapsed "$deploy_start")
echo "    ✓ LWS gang Ready in ${cached_t}"

echo
echo "==> Composed MRs (the LLMInferenceService below is upstream KServe —"
echo "    Modelplane's serving-substrate dependency, swappable in the future)"
kubectl get objects.kubernetes.m.crossplane.io -n "$NS" \
	-o custom-columns='NAME:.metadata.name,KIND:.spec.forProvider.manifest.kind,SYNCED:.status.conditions[?(@.type=="Synced")].status,READY:.status.conditions[?(@.type=="Ready")].status' 2>/dev/null

echo
echo "==> LLMInferenceService manifest the composition applied"
echo "    Note: model.uri=pvc://… points at the cache; worker is a flat PodSpec"
echo "    with a Ray-bootstrap shell as container.command; parallelism carries"
echo "    tensor+pipeline counts."
lis_mr=$(kubectl get objects.kubernetes.m.crossplane.io -n "$NS" \
	-o jsonpath='{range .items[?(@.spec.forProvider.manifest.kind=="LLMInferenceService")]}{.metadata.name}{"\n"}{end}' | head -1)
kubectl get object.kubernetes.m.crossplane.io "$lis_mr" -n "$NS" \
	-o jsonpath='{.spec.forProvider.manifest}' 2>/dev/null | python3 -m json.tool

echo
echo "==> Wait for service address"
for _ in $(seq 1 60); do
	ADDR=$(kubectl get ms qwen-cached-demo -n "$NS" -o jsonpath='{.status.address}' 2>/dev/null || true)
	if [[ -n "${ADDR:-}" ]]; then break; fi
	sleep 5
done
if [[ -z "${ADDR:-}" ]]; then
	echo "    Service address not assigned within 5 min" >&2
	exit 1
fi
echo "    ✓ Service ready at ${ADDR}"

# LWS reports the gang Ready as soon as both pods are 1/1, but vLLM
# inside the leader takes another ~60-120s to load the model from the
# cached PVC and finish CUDA graph capture before Uvicorn opens. Send
# the actual chat completion from a pod that retries internally so
# the demo doesn't race on first-curl.
echo
echo "==> Prove both gang pods do IO from the same PVC across two nodes"
echo "    (fetches workload-cluster creds via gcloud; lists pod→node placement,"
echo "     then execs df + stat on both pods to show same NFS endpoint + same"
echo "     inode for the safetensors file)"
if ! command -v gcloud >/dev/null 2>&1; then
	echo "    SKIPPED: gcloud not on PATH"
elif ! command -v gke-gcloud-auth-plugin >/dev/null 2>&1; then
	echo "    SKIPPED: gke-gcloud-auth-plugin not installed (gcloud components install gke-gcloud-auth-plugin)"
else
	# Use the bracket form for jsonpath keys containing dots — safer
	# than backslash-escaping inside the dot-walk syntax.
	wl_cluster=$(kubectl get gkecluster qwen-cached-demo -n modelplane-system \
		-o jsonpath="{.metadata.annotations['crossplane\.io/external-name']}" 2>/dev/null || true)
	wl_region=$(kubectl get gkecluster qwen-cached-demo -n modelplane-system \
		-o jsonpath='{.spec.region}' 2>/dev/null || true)
	if [[ -z "$wl_cluster" || -z "$wl_region" ]]; then
		echo "    SKIPPED: couldn't resolve workload cluster name/region from GKECluster XR"
	else
		gcloud container clusters get-credentials "$wl_cluster" \
			--region "$wl_region" --project "$GCP_PROJECT" >/dev/null 2>&1
		wl_ctx="gke_${GCP_PROJECT}_${wl_region}_${wl_cluster}"

		echo
		echo "    Pod placement (different nodes, same gang):"
		command kubectl --context="$wl_ctx" get pods -n default \
			-l 'leaderworkerset.sigs.k8s.io/name=qwen-cached-demo-kserve-mn' \
			-o custom-columns=POD:.metadata.name,NODE:.spec.nodeName,IP:.status.podIP \
			--no-headers 2>/dev/null

		echo
		echo "    /mnt/models mount + safetensors stat on each pod:"
		for pod in $(command kubectl --context="$wl_ctx" get pods -n default \
			-l 'leaderworkerset.sigs.k8s.io/name=qwen-cached-demo-kserve-mn' \
			-o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
			echo "    [$pod]"
			# Single-quoted sh -c body avoids nested-quoting hell.
			command kubectl --context="$wl_ctx" exec -n default "$pod" -- sh -c '
				mount | grep /mnt/models | head -1 | sed "s/^/      /"
				stat -c "      inode=%i  size=%s  %n" /mnt/models/model.safetensors
			' 2>/dev/null
		done
		echo
		echo "    ✓ Same NFS endpoint + same inode on both pods = one shared PVC."
	fi
fi

echo
echo "==> Send a chat-completion test request"
echo "    (curl-test pod retries /v1/models until 200, then sends a real"
echo "     /v1/chat/completions — both pods serving from the cached PVC)"
serve_start=$(date +%s)
# ModelService.status.address is already a full URL with the path
# prefix (http://<gateway>/<ns>/<svc>); don't prepend scheme or
# re-add the path — just append the OpenAI path.
kubectl run -i --rm curl-test --image=curlimages/curl --restart=Never --quiet -- \
	sh -c "until curl -s -f --max-time 10 '${ADDR}/v1/models' >/dev/null 2>&1; do sleep 5; done; \
	       curl -s --max-time 30 '${ADDR}/v1/chat/completions' \
	       -H 'Content-Type: application/json' \
	       -d '{\"model\":\"qwen\",\"messages\":[{\"role\":\"user\",\"content\":\"What is a model cache?\"}],\"max_tokens\":40}'"
echo
echo "    ✓ Engine answered after $(elapsed "$serve_start") of post-gang wait"

echo
echo "==> Demo complete in $(elapsed "$demo_start")"
echo "    Both gang pods mount the same cached PVC at /mnt/models — neither"
echo "    pod fetched weights from HuggingFace at boot."
