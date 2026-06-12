"""Native single-pod backend: a Kubernetes Deployment for a Standalone group.

For a single self-contained pod no orchestrator is needed. Weights load
directly: the engine's --model arg is passed through unmodified, so vLLM/SGLang
fetches from its source at startup using credentials from engine.env.

The backend composes the group's Deployment and the Standalone member's
ResourceClaimTemplate. The shared Service and HTTPRoute that front a replica's
groups are composed once by fn.py, not here.
"""

from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

from function.backends import base


class NativeBackend:
    def build(
        self,
        replica: v1alpha1.ModelReplica,
        group,
        provider_config: str,
        serving_label: str,
    ) -> dict[str, k8sobjv1alpha1.Object]:
        member = base.group_member(group, base.ROLE_STANDALONE)
        engine = base.engine_container(member)
        name = base.group_name(replica, group)
        # The pod carries two labels: the shared serving label the replica's one
        # Service selects on (the Standalone pod serves the OpenAI API), and a
        # per-workload label this Deployment selects on. The latter must be
        # group-unique so two Standalone groups of one replica don't share a
        # selector and fight over each other's pods.
        pod_labels = {base.LABEL_SERVING: serving_label, base.LABEL_WORKLOAD: name}
        selector = {base.LABEL_WORKLOAD: name}

        cache_volumes, cache_volume_mounts = base.cache_mounts(replica)
        args = base.apply_cache_args(list(engine.args or []), replica, engine)

        container = {
            "name": "engine",
            "image": engine.image,
            "args": args,
            "ports": [{"containerPort": base.ENGINE_PORT}],
            # GPUs bind via DRA: the engine references the pod-level claim backed
            # by the member's ResourceClaimTemplate.
            "resources": base.engine_resources(),
            # vLLM tensor parallelism needs a large /dev/shm.
            "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}, *cache_volume_mounts],
            "readinessProbe": {
                "httpGet": {"path": "/health", "port": base.ENGINE_PORT},
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
        # Pin to the scheduled pool and claim GPUs via DRA.
        base.place_pod(pod_spec, replica, group, member)
        tmpl = member.template
        if tmpl.spec.imagePullSecrets:
            pod_spec["imagePullSecrets"] = [s.model_dump(exclude_none=True) for s in tmpl.spec.imagePullSecrets]

        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": base.REMOTE_NAMESPACE},
            "spec": {
                "replicas": int(group.replicas or 1),
                "selector": {"matchLabels": selector},
                "template": {"metadata": {"labels": pod_labels}, "spec": pod_spec},
            },
        }

        return {
            base.workload_key(group): base.wrap_object(provider_config, deployment, cel_query=base.AVAILABLE_CEL),
            base.claim_key(group, member): base.resource_claim_template(replica, group, member, provider_config),
        }
