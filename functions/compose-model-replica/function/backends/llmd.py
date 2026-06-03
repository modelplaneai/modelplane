"""llm-d multi-pod backend: LeaderWorkerSet + GAIE routing (InferencePool/EPP/HTTPRoute).

Selected only for multi-node replicas (pipeline > 1), so this always renders a
LeaderWorkerSet whose gang size is the per-worker node count.

The `llm-d-modelservice` Helm chart is DEPRECATED in llm-d v0.7, so this backend
renders the workload as provider-kubernetes Objects directly (mirroring native.py),
NOT a Helm Release. GAIE field names/versions follow the verified v0.7 / GAIE
v1.5.0 surface in docs/superpowers/notes/llm-d-v0.7-surface.md.

KNOWN FOLLOW-UP (out of scope for v0.1): the exact multi-node vLLM/Ray
leader+worker bootstrap command must be validated against a live multi-node GPU
cluster (see spec "Open items" and the spike notes). v0.1 renders the structural
manifest with the parallelism flags injected; the engine command may still need a
Ray bootstrap wrapper (the leader starts the Ray head, workers join, then `vllm
serve` runs on the leader). Unit tests validate structure, not live execution.
Do not block on getting Ray exactly right here.

Weight loading mirrors native: the engine's --model arg is passed through
unmodified (no hf:// rewrite), so the engine fetches from its source at startup
using credentials from engine.env.
"""

import copy

from crossplane.function import resource
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

from function.backends import base

# Namespace for serving workloads on remote clusters.
_REMOTE_NAMESPACE = "default"

# Port the engine serves the OpenAI-compatible API on. Must match the
# InferencePool targetPorts[].number (GAIE join contract, spike notes §2f/§3a).
_ENGINE_PORT = 8000

# EPP (endpoint-picker) ext-proc port — GAIE inferencepool chart default (spike §3c).
_EPP_PORT = 9002

# Label joining the LWS pods to the InferencePool selector (spike notes §2g/§3a).
# llm-d's well-lit path keys on this exact label.
_LABEL_INFERENCE = "llm-d.ai/inference-serving"

# Modelplane's own serving label (mirrors native.py).
_LABEL_SERVING = "modelplane.ai/serving"

# GAIE endpoint-picker image. Pinned to GAIE v1.5.0 (the version llm-d v0.7.0
# pins, spike §0). The spike notes do not give an exact image ref for the EPP, so
# this uses the conventional GAIE ref.
# TODO: verify EPP image ref against the GAIE v1.5.0 release, and add a
# readiness/liveness probe to the EPP Deployment once the image's health
# endpoint is confirmed (a wedged EPP otherwise reports Ready).
_EPP_IMAGE = "registry.k8s.io/gateway-api-inference-extension/epp:v1.5.0"

# GA group for the InferencePool / HTTPRoute backendRef (spike §3a).
_INFERENCE_GROUP = "inference.networking.k8s.io"


def _object(provider_config: str, manifest: dict) -> k8sobjv1alpha1.Object:
    return k8sobjv1alpha1.Object(
        spec=k8sobjv1alpha1.Spec(
            providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                kind="ClusterProviderConfig",
                name=provider_config,
            ),
            readiness=k8sobjv1alpha1.Readiness(policy="DeriveFromObject"),
            forProvider=k8sobjv1alpha1.ForProvider(manifest=manifest),
        ),
    )


def _engine_args(engine, tensor: int, pipeline: int) -> list[str]:
    """Engine args with parallelism flags injected (only if not already set).

    --model is passed through unmodified (no hf:// rewrite).
    """
    args = list(engine.args or [])
    if not any(a.startswith("--tensor-parallel-size") for a in args):
        args.append(f"--tensor-parallel-size={tensor}")
    if pipeline > 1 and not any(a.startswith("--pipeline-parallel-size") for a in args):
        args.append(f"--pipeline-parallel-size={pipeline}")
    return args


class LLMDBackend:
    def build(
        self,
        replica: v1alpha1.ModelReplica,
        cluster: icv1alpha1.InferenceCluster,
        deployment_name: str,
    ) -> dict[str, k8sobjv1alpha1.Object]:
        engine = base.engine_container(replica)
        pc = cluster.status.providerConfigRef.name
        name = resource.child_name(deployment_name)
        epp_name = f"{name}-epp"

        tensor = int(replica.spec.workers.topology.tensor)
        # nodes_per_worker == pipeline: the LWS gang size (leader + workers).
        size = base.nodes_per_worker(replica)
        pipeline = int(replica.spec.workers.topology.pipeline or 1)

        # Pod labels join the LWS pods to the InferencePool selector, plus
        # Modelplane's own per-replica serving label.
        pod_labels = {_LABEL_INFERENCE: "true", _LABEL_SERVING: name}

        container = {
            "name": "engine",
            "image": engine.image,
            "args": _engine_args(engine, tensor, pipeline),
            "ports": [{"containerPort": _ENGINE_PORT}],
            # GPUs PER POD (one tensor-parallel shard runs per pod in the gang).
            "resources": {"limits": {"nvidia.com/gpu": str(tensor)}},
            # vLLM tensor parallelism needs a large /dev/shm.
            "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
        }
        if engine.env:
            container["env"] = [e.model_dump(exclude_none=True) for e in engine.env]

        pod_spec = {
            "containers": [container],
            "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
        }
        tmpl = replica.spec.workers.template
        if tmpl.spec.imagePullSecrets:
            pod_spec["imagePullSecrets"] = [s.model_dump(exclude_none=True) for s in tmpl.spec.imagePullSecrets]

        pod_template = {"metadata": {"labels": pod_labels}, "spec": pod_spec}

        # LeaderWorkerSet: spec.replicas gangs, each of `size` pods (leader+workers).
        # leaderTemplate and workerTemplate are structurally identical in v0.1 but
        # kept as independent copies: the known Ray-bootstrap follow-up (leader runs
        # the head, workers join) will mutate them divergently, and a shared
        # reference would silently mutate both.
        leader_worker_set = {
            "apiVersion": "leaderworkerset.x-k8s.io/v1",
            "kind": "LeaderWorkerSet",
            "metadata": {"name": name, "namespace": _REMOTE_NAMESPACE},
            "spec": {
                "replicas": int(replica.spec.workers.count or 1),
                "leaderWorkerTemplate": {
                    "size": size,
                    "leaderTemplate": pod_template,
                    "workerTemplate": copy.deepcopy(pod_template),
                },
            },
        }

        # GAIE InferencePool (v1). Field names per spike §3a: targetPorts (list),
        # endpointPickerRef (NOT targetPortNumber / extensionRef).
        inference_pool = {
            "apiVersion": f"{_INFERENCE_GROUP}/v1",
            "kind": "InferencePool",
            "metadata": {"name": name, "namespace": _REMOTE_NAMESPACE},
            "spec": {
                # Scope the pool to THIS replica's pods (both labels), not every
                # llm-d pod on the cluster — otherwise co-located replicas
                # cross-select each other's endpoints.
                "selector": {"matchLabels": {_LABEL_INFERENCE: "true", _LABEL_SERVING: name}},
                "targetPorts": [{"number": _ENGINE_PORT}],
                "endpointPickerRef": {
                    "name": epp_name,
                    "port": {"number": _EPP_PORT},
                    "failureMode": "FailOpen",
                },
            },
        }

        # Per-pool EPP (GAIE endpoint-picker): Deployment + Service exposing 9002.
        epp_labels = {_LABEL_SERVING: epp_name}
        epp_deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": epp_name, "namespace": _REMOTE_NAMESPACE},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": epp_labels},
                "template": {
                    "metadata": {"labels": epp_labels},
                    "spec": {
                        "containers": [
                            {
                                "name": "epp",
                                "image": _EPP_IMAGE,
                                "args": [f"--pool-name={name}", f"--pool-namespace={_REMOTE_NAMESPACE}"],
                                "ports": [{"containerPort": _EPP_PORT}],
                            }
                        ],
                    },
                },
            },
        }
        epp_service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": epp_name, "namespace": _REMOTE_NAMESPACE},
            "spec": {"selector": epp_labels, "ports": [{"port": _EPP_PORT, "targetPort": _EPP_PORT}]},
        }

        # HTTPRoute: route to the InferencePool (not a Service).
        http_route = {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "HTTPRoute",
            "metadata": {"name": name, "namespace": _REMOTE_NAMESPACE},
            "spec": {
                "parentRefs": [{"name": "inference-gateway", "namespace": "modelplane-system"}],
                "rules": [
                    {
                        "matches": [
                            {
                                "path": {
                                    "type": "PathPrefix",
                                    "value": f"/{replica.metadata.namespace}/{deployment_name}/",
                                }
                            }
                        ],
                        # Strip the /<ns>/<deployment>/ routing prefix so the engine
                        # (which serves /v1/...) sees the path it expects.
                        "filters": [
                            {
                                "type": "URLRewrite",
                                "urlRewrite": {"path": {"type": "ReplacePrefixMatch", "replacePrefixMatch": "/"}},
                            }
                        ],
                        "backendRefs": [
                            {
                                "group": _INFERENCE_GROUP,
                                "kind": "InferencePool",
                                "name": name,
                                "port": _ENGINE_PORT,
                            }
                        ],
                    }
                ],
            },
        }

        return {
            "model-serving": _object(pc, leader_worker_set),
            "model-inferencepool": _object(pc, inference_pool),
            "model-epp": _object(pc, epp_deployment),
            "model-epp-svc": _object(pc, epp_service),
            "model-route": _object(pc, http_route),
        }
