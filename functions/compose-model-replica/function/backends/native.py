# Copyright 2026 The Modelplane Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Native single-pod backend: a Kubernetes Deployment for a Standalone engine.

For a single self-contained pod no orchestrator is needed. Weights load
directly: the engine's --model arg is passed through unmodified, so vLLM/SGLang
fetches from its source at startup using credentials from engine.env.

The backend composes the engine's Deployment and the Standalone member's
ResourceClaimTemplate. The shared Service and HTTPRoute that front a replica's
engines are composed once by fn.py, not here.
"""

from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

from function.backends import base


class NativeBackend:
    def build(
        self,
        replica: v1alpha1.ModelReplica,
        engine: v1alpha1.Engine,
        provider_config: str,
        serving_label: str,
    ) -> dict[str, k8sobjv1alpha1.Object]:
        member = base.engine_member(engine, base.ROLE_STANDALONE)
        # select_backend dispatches the native backend only for an engine with a
        # Standalone member, so it's always present here.
        assert member is not None
        engine_container = base.engine_container(member)
        name = base.engine_name(replica, engine)
        # The pod carries two labels: the shared serving label the replica's one
        # Service selects on (the Standalone pod serves the OpenAI API), and a
        # per-workload label this Deployment selects on. The latter must be
        # engine-unique so two Standalone engines of one replica don't share a
        # selector and fight over each other's pods.
        pod_labels = {base.LABEL_SERVING: serving_label, base.LABEL_WORKLOAD: name}
        selector = {base.LABEL_WORKLOAD: name}

        cache_volumes, cache_volume_mounts = base.cache_mounts(replica)
        args = base.apply_cache_args(list(engine_container.args or []), replica, engine_container)

        container = {
            "name": "engine",
            "image": engine_container.image,
            "args": args,
            "ports": [{"containerPort": base.ENGINE_PORT}],
            # vLLM tensor parallelism needs a large /dev/shm.
            "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}, *cache_volume_mounts],
            "readinessProbe": {
                "httpGet": {"path": "/health", "port": base.ENGINE_PORT},
                "initialDelaySeconds": 30,
                "periodSeconds": 10,
                # Kubernetes defaults the probe timeout to 1s. Some engines'
                # /health isn't instant - SGLang's sits right at ~1s - so a 1s
                # timeout flaps the probe and the pod never goes Ready. 5s gives
                # a slow /health room without masking a real hang.
                "timeoutSeconds": 5,
            },
        }
        # GPUs bind via DRA: the container references the pod-level claim
        # backed by the member's ResourceClaimTemplate. A Standalone member
        # always claims (its engine has no other member to claim, and the XRD
        # requires some member to), so this is belt and braces.
        if member.deviceRequests:
            container["resources"] = base.engine_resources()
        if engine_container.command:
            container["command"] = list(engine_container.command)
        if engine_container.env:
            container["env"] = [e.model_dump(exclude_none=True) for e in engine_container.env]

        pod_spec = {
            "containers": [container],
            "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}, *cache_volumes],
        }
        # Pin to the member's scheduled pool and claim GPUs via DRA.
        base.place_pod(pod_spec, replica, engine, member)
        tmpl = member.template
        # The XRD types template.spec as optional, but a member with no spec
        # defines no pod to serve, so reaching here without one is malformed.
        assert tmpl.spec is not None
        if tmpl.spec.imagePullSecrets:
            pod_spec["imagePullSecrets"] = [s.model_dump(exclude_none=True) for s in tmpl.spec.imagePullSecrets]

        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": base.REMOTE_NAMESPACE},
            "spec": {
                "replicas": int(engine.copies or 1),
                "selector": {"matchLabels": selector},
                "template": {"metadata": {"labels": pod_labels}, "spec": pod_spec},
            },
        }

        composed = {
            base.workload_key(engine): base.wrap_object(provider_config, deployment, cel_query=base.AVAILABLE_CEL),
        }
        # Gated like the container's resources above, and for the same reason.
        if member.deviceRequests:
            composed[base.claim_key(engine, member)] = base.resource_claim_template(
                replica, engine, member, provider_config
            )
        return composed
