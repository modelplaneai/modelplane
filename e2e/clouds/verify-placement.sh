#!/usr/bin/env bash
# Verify serving-stack DaemonSet fan-out, pod placement and tolerations on
# the workload cluster. Invoked by the Chainsaw test as a script step;
# Chainsaw injects KUBECONFIG for the workload cluster, and the values file
# supplies EXPECTED_DAEMONSETS (namespace/name=readyCount, space-separated).
# Usage: verify-placement.sh <cloud-slug>.
#
# Two-sided check. The fan-out wait catches tolerations too narrow to reach
# the tainted GPU pool (e.g. an Equal toleration whose value doesn't match
# the taint). The audits catch the opposite class: pods tolerating taints
# they shouldn't. Real AKS rejects wildcard tolerations at admission and
# every cloud taints its GPU pools, so a system-pool pod carrying a wildcard
# or nvidia.com/gpu toleration is a bug even when the scheduler happened to
# keep it on the system node.
set -euo pipefail

cloud="${1:?usage: verify-placement.sh <cloud-slug>}"

# 1. Fan-out: each expected DaemonSet reaches its ready count. Charts without
# dependents get no helm --wait, so rollout may lag the ServingStack Ready
# condition; poll with one shared deadline.
deadline=$((SECONDS + 300))
for entry in ${EXPECTED_DAEMONSETS:?EXPECTED_DAEMONSETS is not set}; do
	ns="${entry%%/*}"
	rest="${entry#*/}"
	name="${rest%%=*}"
	want="${rest#*=}"
	ready=""
	while [ "$SECONDS" -lt "$deadline" ]; do
		ready="$(kubectl -n "$ns" get daemonset "$name" -o jsonpath='{.status.numberReady}' 2>/dev/null || true)"
		[ "$ready" = "$want" ] && break
		sleep 5
	done
	if [ "$ready" != "$want" ]; then
		echo "FAIL: DaemonSet $ns/$name numberReady is ${ready:-absent}, want $want"
		kubectl -n "$ns" get daemonset "$name" -o wide 2>/dev/null || true
		exit 1
	fi
	echo "OK: DaemonSet $ns/$name is $want/$want ready"
done

# Pods expected to carry the GPU toleration and land on the fake GPU nodes:
# the GPU-reaching DaemonSets from generate.py's TOLERATIONS table (NFD
# worker, node-exporter, DRA kubelet plugin, gpu-operator operands) plus
# k8s-ephemeral-storage-metrics, a Deployment that carries the GPU
# toleration. Unanchored where the generated and hand-written halves name
# the same workload differently. The nvsentinel namespace is exempted
# wholesale below: its node monitors carry the GPU toleration and its
# system pods carry a deliberate wildcard (global.systemNodeTolerations is
# [{operator: Exists}] upstream), and its workload names (labeler,
# platform-connectors, ...) don't share a prefix to match on.
gpu_allow='(node-feature-discovery-worker|prometheus-node-exporter|kubelet-plugin|^gpu-feature-discovery|^nvidia-dcgm|^nvidia-mig-manager|^nvidia-operator-validator|^nvidia-cuda-validator|k8s-ephemeral-storage-metrics)'
allow_ns='^nvsentinel$'
case "$cloud" in
aks) gpu_allow="${gpu_allow}|^nvidia-toolkit-hardening" ;;
esac

# GPU-only pods must never run on the system pool: they'd only land there by
# a selector/affinity regression. NFD worker and node-exporter are excluded —
# they legitimately run on every node.
gpu_only='(kubelet-plugin|^gpu-feature-discovery|^nvidia-dcgm|^nvidia-mig-manager|^nvidia-operator-validator|^nvidia-cuda-validator|^nvidia-toolkit-hardening)'

# Cluster infrastructure the serving stack doesn't manage.
skip_ns='^(kube-system|metallb-system|local-path-storage)$'

pods="$(kubectl get pods -A -o json)"
fake_json="$(kubectl get nodes -l type=kwok -o json | jq -c '[.items[].metadata.name]')"
fake_nodes="$(jq -r '.[]' <<<"$fake_json")"

fail=0

# 2. Toleration audit: no pod outside the allowlist may carry a wildcard or
# an nvidia.com/gpu toleration, wherever the scheduler put it.
bad="$(jq -r --arg skip "$skip_ns" --arg gpu "$gpu_allow" --arg allowns "$allow_ns" '
	.items[]
	| select((.metadata.namespace | test($skip)) | not)
	| select((.metadata.namespace | test($allowns)) | not)
	| . as $p
	| (.spec.tolerations // [])
	| map(select(
		((.operator == "Exists") and ((.key // "") == ""))
		or ((.key == "nvidia.com/gpu") and (($p.metadata.name | test($gpu)) | not))
	))
	| select(length > 0)
	| "\($p.metadata.namespace)/\($p.metadata.name): \(tojson)"
' <<<"$pods")"
if [ -n "$bad" ]; then
	echo "FAIL: pods carrying tolerations the system pool must not have:"
	echo "$bad"
	fail=1
fi

# 3. Placement audit: everything on a fake GPU node must be an expected GPU
# workload, and GPU-only workloads must not run on the real (system) node.
for node in $fake_nodes; do
	bad="$(jq -r --arg node "$node" --arg skip "$skip_ns" --arg gpu "$gpu_allow" --arg allowns "$allow_ns" '
		.items[]
		| select(.spec.nodeName == $node)
		| select((.metadata.namespace | test($skip)) | not)
		| select((.metadata.namespace | test($allowns)) | not)
		| select((.metadata.name | test($gpu)) | not)
		| "\(.metadata.namespace)/\(.metadata.name) on \($node)"
	' <<<"$pods")"
	if [ -n "$bad" ]; then
		echo "FAIL: unexpected pods scheduled onto fake GPU node $node:"
		echo "$bad"
		fail=1
	fi
done

bad="$(jq -r --arg gpu_only "$gpu_only" --argjson fake "$fake_json" '
	.items[]
	| select(.spec.nodeName != null)
	| select([.spec.nodeName] | inside($fake) | not)
	| select(.metadata.name | test($gpu_only))
	| "\(.metadata.namespace)/\(.metadata.name) on \(.spec.nodeName)"
' <<<"$pods")"
if [ -n "$bad" ]; then
	echo "FAIL: GPU-only pods scheduled onto the system pool:"
	echo "$bad"
	fail=1
fi

if [ "$fail" = 0 ]; then
	echo "OK: placement and tolerations match the $cloud expectations"
fi
exit "$fail"
