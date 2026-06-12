"""llm-d multi-pod backend: a LeaderWorkerSet for a Leader/Worker engine.

Selected for an engine with a Leader and a Worker, so this always renders a
LeaderWorkerSet whose gang size is the leader plus the Worker's follower count.

Routing is plain Gateway API — the replica's shared HTTPRoute -> Service, built
by fn.py — NOT a GAIE InferencePool. The Service selects the LWS leader pods
(only the leader serves the OpenAI API; workers just join the gang). Why a
Service, not a GAIE InferencePool: v0.1 does no KV-/load-aware endpoint picking,
so the InferencePool + EPP this path originally emitted aren't needed yet.
Reintroducing them is a workload-gateway concern, deferred with disaggregated
serving.

Modelplane is unopinionated about the engine. Both the leader's and the
worker's commands and args are passed through verbatim - Modelplane injects no
parallelism flags and no bootstrap. A multi-node launch convention Modelplane
has never heard of still works, because the coordination asymmetry between
running the head and joining it lives in the two members' commands, which the
user writes. The follower addresses the leader through
$(MODELPLANE_LEADER_ADDRESS), which Modelplane injects into every engine
container (aliasing LWS_LEADER_ADDRESS for this backend).

Weight loading mirrors native: the engine's --model arg is passed through
unmodified, so the engine fetches from its source at startup using credentials
from engine.env.
"""

from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

from function.backends import base

# Label set only on the LWS leader pod. The Service selects on it so traffic
# reaches the gang leader (the only pod that serves the OpenAI API).
_LABEL_ROLE = "modelplane.ai/lws-role"


class LLMDBackend:
    def build(
        self,
        replica: v1alpha1.ModelReplica,
        engine,
        provider_config: str,
        serving_label: str,
    ) -> dict[str, k8sobjv1alpha1.Object]:
        leader = base.engine_member(engine, base.ROLE_LEADER)
        worker = base.engine_member(engine, base.ROLE_WORKER)
        name = base.engine_name(replica, engine)

        # Gang size: the leader plus the worker's nodes (one follower pod each).
        size = 1 + (int(worker.worker.nodes) if worker.worker else 1)

        cache_volumes, cache_volume_mounts = base.cache_mounts(replica)

        def container(member, *, serving: bool) -> dict:
            engine_container = base.engine_container(member)
            args = list(engine_container.args or [])
            # The turnkey cache --model injection is for the serving engine (the
            # leader) only; a follower joins via its own command and never serves,
            # so injecting --model into it would be a flag it doesn't expect.
            if serving:
                args = base.apply_cache_args(args, replica, engine_container)
            c = {
                "name": "engine",
                "image": engine_container.image,
                # vLLM tensor parallelism needs a large /dev/shm.
                "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}, *cache_volume_mounts],
            }
            # GPUs per pod bound via DRA through the member's claim. A claimless
            # member (a coordinator-only leader) has no pod-level claim for its
            # container to reference.
            if member.deviceRequests:
                c["resources"] = base.engine_resources()
            if engine_container.command:
                c["command"] = list(engine_container.command)
            if args:
                c["args"] = args
            # MODELPLANE_LEADER_ADDRESS ahead of the user's env entries, so they
            # (and commands) can reference $(MODELPLANE_LEADER_ADDRESS). LWS
            # prepends its own LWS_* vars ahead of all of these in the running
            # pod.
            env = [base.leader_address_env()]
            if engine_container.env:
                env.extend(e.model_dump(exclude_none=True) for e in engine_container.env)
            c["env"] = env
            if serving:
                c["ports"] = [{"containerPort": base.ENGINE_PORT}]
                c["readinessProbe"] = {
                    "httpGet": {"path": "/health", "port": base.ENGINE_PORT},
                    "initialDelaySeconds": 30,
                    "periodSeconds": 10,
                }
            return c

        def pod_spec(member, c: dict) -> dict:
            spec = {
                "containers": [c],
                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}, *cache_volumes],
            }
            # Every pod pins to its member's scheduled pool and, if the member
            # claims devices, claims GPUs via the member's DRA template (one
            # fresh claim per pod).
            base.place_pod(spec, replica, engine, member)
            secrets = member.template.spec.imagePullSecrets
            if secrets:
                spec["imagePullSecrets"] = [s.model_dump(exclude_none=True) for s in secrets]
            return spec

        # Only the leader serves the OpenAI API → it carries the serving label
        # the replica's shared Service selects on, plus the role label, the
        # serving port, and the readiness probe.
        leader_pod = {
            "metadata": {"labels": {base.LABEL_SERVING: serving_label, _LABEL_ROLE: "leader"}},
            "spec": pod_spec(leader, container(leader, serving=True)),
        }
        # The worker followers don't serve the OpenAI API, so they carry no
        # serving label - the replica's Service must never route to them. LWS
        # manages their gang membership labels itself.
        worker_pod = {
            "spec": pod_spec(worker, container(worker, serving=False)),
        }

        # LeaderWorkerSet: spec.replicas gangs, each of `size` pods (leader+workers).
        leader_worker_set = {
            "apiVersion": "leaderworkerset.x-k8s.io/v1",
            "kind": "LeaderWorkerSet",
            "metadata": {"name": name, "namespace": base.REMOTE_NAMESPACE},
            "spec": {
                "replicas": int(engine.copies or 1),
                "leaderWorkerTemplate": {
                    "size": size,
                    "leaderTemplate": leader_pod,
                    "workerTemplate": worker_pod,
                },
            },
        }

        composed = {
            base.workload_key(engine): base.wrap_object(
                provider_config, leader_worker_set, cel_query=base.AVAILABLE_CEL
            ),
        }
        # One ResourceClaimTemplate per claiming member. The leader and worker
        # may claim different devices, or one may claim none at all (a
        # coordinator-only leader composes no template).
        for member in (leader, worker):
            if member.deviceRequests:
                composed[base.claim_key(engine, member)] = base.resource_claim_template(
                    replica, engine, member, provider_config
                )
        return composed
