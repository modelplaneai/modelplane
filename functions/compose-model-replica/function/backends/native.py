"""Native single-pod backend: plain Kubernetes Deployment + Service + HTTPRoute.

For a single self-contained pod no orchestrator is needed. Weights load
directly: the engine's --model arg is passed through unmodified, so vLLM/SGLang
fetches from its source at startup using credentials from engine.env.
"""

from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

from function.backends import base

# Namespace for serving workloads on remote clusters.
_REMOTE_NAMESPACE = base.REMOTE_NAMESPACE

# Port the engine serves the OpenAI-compatible API on.
_ENGINE_PORT = 8000

# Label joining the Deployment, its pods, and the Service selector.
_LABEL_SERVING = "modelplane.ai/serving"


class NativeBackend:
    def build(
        self,
        replica: v1alpha1.ModelReplica,
        cluster: icv1alpha1.InferenceCluster,
    ) -> dict[str, k8sobjv1alpha1.Object]:
        engine = base.engine_container(replica)
        pc = cluster.status.providerConfigRef.name
        # Name workload resources after the replica (unique per placement), not
        # the deployment — so multiple replicas of one deployment can co-exist on
        # the same InferenceCluster without colliding on the remote cluster.
        name = replica.metadata.name
        labels = {_LABEL_SERVING: name}

        cache_volumes, cache_volume_mounts = base.cache_mounts(replica)
        args = base.apply_cache_args(list(engine.args or []), replica, engine)

        container = {
            "name": "engine",
            "image": engine.image,
            "args": args,
            "ports": [{"containerPort": _ENGINE_PORT}],
            # GPUs bind via DRA when the replica has device requests, else via the
            # nvidia.com/gpu device-plugin limit.
            "resources": base.engine_resources(replica),
            # vLLM tensor parallelism needs a large /dev/shm.
            "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}, *cache_volume_mounts],
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
            "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}, *cache_volumes],
        }
        # Reference the per-replica ResourceClaimTemplate (no-op without DRA).
        base.attach_device_claims(pod_spec, replica)
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
                                    "value": f"/{replica.metadata.namespace}/{name}/",
                                }
                            }
                        ],
                        # The control plane rewrites the public /<ns>/<service>/
                        # prefix to this replica's /<ns>/<replica>/ (per-IC
                        # addressing, so it survives to this gateway and routes
                        # among deployments); strip it here so the engine sees /v1/.
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

        out = {
            "model-serving": base.wrap_object(pc, deployment),
            "model-service": base.wrap_object(pc, service),
            "model-route": base.wrap_object(pc, http_route),
        }
        # Emit a DRA ResourceClaimTemplate when the replica has device requests.
        claim = base.resource_claim_template(replica, pc)
        if claim is not None:
            out[base.RESOURCE_CLAIM_KEY] = claim
        return out
