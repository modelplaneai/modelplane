"""Native single-pod backend: plain Kubernetes Deployment + Service + HTTPRoute.

For a single self-contained pod no orchestrator is needed. Weights load
directly: the engine's --model arg is passed through unmodified, so vLLM/SGLang
fetches from its source at startup using credentials from engine.env.
"""

from crossplane.function import resource
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

from function.backends import base

# Namespace for serving workloads on remote clusters.
_REMOTE_NAMESPACE = "default"

# Port the engine serves the OpenAI-compatible API on.
_ENGINE_PORT = 8000

# Label joining the Deployment, its pods, and the Service selector.
_LABEL_SERVING = "modelplane.ai/serving"


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


class NativeBackend:
    def build(
        self,
        replica: v1alpha1.ModelReplica,
        cluster: icv1alpha1.InferenceCluster,
        deployment_name: str,
    ) -> dict[str, k8sobjv1alpha1.Object]:
        engine = base.engine_container(replica)
        pc = cluster.status.providerConfigRef.name
        name = resource.child_name(deployment_name)
        labels = {_LABEL_SERVING: name}

        container = {
            "name": "engine",
            "image": engine.image,
            "args": list(engine.args or []),
            "ports": [{"containerPort": _ENGINE_PORT}],
            "resources": {"limits": {"nvidia.com/gpu": str(replica.spec.workers.topology.tensor)}},
            # vLLM tensor parallelism needs a large /dev/shm.
            "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
            "readinessProbe": {
                "httpGet": {"path": "/health", "port": _ENGINE_PORT},
                "initialDelaySeconds": 30,
                "periodSeconds": 10,
            },
        }
        if engine.command:
            container["command"] = list(engine.command)
        if engine.env:
            container["env"] = [e.model_dump(exclude_none=True) for e in engine.env]

        pod_spec = {
            "containers": [container],
            "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
        }
        tmpl = replica.spec.workers.template
        if tmpl.spec.imagePullSecrets:
            pod_spec["imagePullSecrets"] = [s.model_dump(exclude_none=True) for s in tmpl.spec.imagePullSecrets]

        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": _REMOTE_NAMESPACE},
            "spec": {
                "replicas": int(replica.spec.workers.count or 1),
                "selector": {"matchLabels": labels},
                "template": {"metadata": {"labels": labels}, "spec": pod_spec},
            },
        }

        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "namespace": _REMOTE_NAMESPACE},
            "spec": {"selector": labels, "ports": [{"port": 80, "targetPort": _ENGINE_PORT}]},
        }

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
                        "backendRefs": [{"name": name, "port": 80}],
                    }
                ],
            },
        }

        return {
            "model-serving": _object(pc, deployment),
            "model-service": _object(pc, service),
            "model-route": _object(pc, http_route),
        }
