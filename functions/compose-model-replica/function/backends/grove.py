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

"""Grove multi-pod backend: a PodCliqueScalingGroup for a Leader/Worker engine.

Selected for a Leader/Worker engine on a stack: Dynamo cluster; llmd is the
Standard alternative. Both cliques sit in one scaling group so Grove treats the
gang as a single schedulable, addressable unit, and engine.copies scales the
pair together.

Modelplane is unopinionated about the engine: both members' commands and args
pass through verbatim, so a launch convention Modelplane has never heard of
still works. Routing is layered on afterwards by routing.py.

A member addresses the leader through $(MODELPLANE_LEADER_ADDRESS), which
resolves per gang (see base.grove_leader_address_env). There's no
MODELPLANE_RANK: Grove numbers pods within a clique, not across the gang, so a
command derives its own rank from $((GROVE_PCLQ_POD_INDEX + 1)) on the workers.
Env expansion substitutes strings and can't do the +1, so this needs a
group-wide index from Grove (grove#755) before it can be aliased like the
address. See docs/manifests/concepts/model-deployment-multinode.yaml.

Weight loading mirrors native: the engine names its own model, and with a cache
base.cache_env points HuggingFace at the mount so that name resolves to the
staged weights.
"""

from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

from function.backends import base

# Label set only on the leader clique. Grove propagates clique labels to every
# pod in the clique, so this ends up on the (single) leader pod, alongside the
# serving label the InferencePool selects on. Purely informational - nothing
# reads it - but it mirrors the leader/worker distinction Grove's own clique
# name already carries, for kubectl/observability convenience.
_LABEL_CLIQUE_ROLE = "modelplane.ai/clique-role"


class GroveBackend:
    def build(
        self,
        replica: v1alpha1.ModelReplica,
        engine: v1alpha1.Engine,
        provider_config: str,
        serving_label: str,
        stack: str,
    ) -> dict[str, k8sobjv1alpha1.Object]:
        leader = base.engine_member(engine, base.ROLE_LEADER)
        worker = base.engine_member(engine, base.ROLE_WORKER)
        # select_backend dispatches the Grove backend for a non-Standalone
        # engine, and the XRD requires such an engine to carry exactly one
        # Leader and one Worker member, so both are always present here.
        assert leader is not None
        assert worker is not None
        name = base.grove_pcs_name(replica, engine)

        # The worker clique's pod count: one follower pod per node.
        worker_replicas = int(worker.worker.nodes) if worker.worker else 1

        cache_volumes, cache_volume_mounts = base.cache_mounts(replica)

        def container(member: v1alpha1.Member, *, serving: bool) -> dict:
            engine_container = base.engine_container(member)
            args = list(engine_container.args or [])
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
            # Modelplane's entries lead so the user's can reference them:
            # $(VAR) expansion is left to right. ModelExpress applies to workers
            # too, since each pod publishes itself as a source independently.
            env = [
                base.grove_leader_address_env(),
                *base.cache_env(replica),
                *base.modelexpress_env(replica, stack),
            ]
            if engine_container.env:
                env.extend(e.model_dump(exclude_none=True) for e in engine_container.env)
            if env:
                c["env"] = env
            security_context = base.modelexpress_security_context(replica, stack)
            if security_context:
                c["securityContext"] = security_context
            if serving:
                c["ports"] = [{"containerPort": base.ENGINE_PORT}]
                c["readinessProbe"] = {
                    "httpGet": {"path": "/health", "port": base.ENGINE_PORT},
                    "initialDelaySeconds": 30,
                    "periodSeconds": 10,
                    # A slow /health (e.g. SGLang's ~1s) flaps the probe under the
                    # 1s Kubernetes default; 5s gives it room.
                    "timeoutSeconds": 5,
                }
            return c

        def pod_spec(member: v1alpha1.Member, c: dict) -> dict:
            spec = {
                "containers": [c],
                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}, *cache_volumes],
                # Every clique's pods gang-schedule through KAI: KAI is the
                # scheduler Grove hands its PodGangs to, and neither Grove nor
                # KAI sets this for us (see compose-serving-stack).
                "schedulerName": base.GROVE_SCHEDULER_NAME,
            }
            # Every pod pins to its member's scheduled pool and, if the member
            # claims devices, claims GPUs via the member's DRA template (one
            # fresh claim per pod).
            base.place_pod(spec, replica, engine, member)
            # The XRD types template.spec as optional, but a member with no spec
            # defines no pod to serve, so reaching here without one is malformed.
            assert member.template.spec is not None
            secrets = member.template.spec.imagePullSecrets
            if secrets:
                spec["imagePullSecrets"] = [s.model_dump(exclude_none=True) for s in secrets]
            return spec

        # Only the leader serves the OpenAI API, so only its clique carries the
        # serving label the InferencePool selects on. The queue label is what
        # KAI schedules against (see compose-serving-stack's compose_kai_queues).
        # Grove propagates a clique's labels to its pods.
        leader_clique = {
            "name": base.GROVE_LEADER_CLIQUE,
            **base.pod_metadata(
                leader,
                {
                    base.LABEL_SERVING: serving_label,
                    base.GROVE_QUEUE_LABEL: base.GROVE_QUEUE,
                    _LABEL_CLIQUE_ROLE: "leader",
                },
            ),
            "spec": {
                "roleName": base.GROVE_LEADER_CLIQUE,
                "replicas": 1,
                "minAvailable": 1,
                "podSpec": pod_spec(leader, container(leader, serving=True)),
            },
        }
        # No serving label: the InferencePool must never route to a worker.
        # minAvailable equals the replica count so a partial start doesn't count
        # as available. These counts are per scaling-group replica, which Grove
        # multiplies by engine.copies.
        #
        # No startsAfter. It would gate the workers on the leader going Ready,
        # which deadlocks: the leader isn't Ready until the workers have joined
        # it. They start together instead, and a worker retries the leader's
        # stable DNS name until it's listening.
        worker_clique = {
            "name": base.GROVE_WORKER_CLIQUE,
            **base.pod_metadata(worker, {base.GROVE_QUEUE_LABEL: base.GROVE_QUEUE}),
            "spec": {
                "roleName": base.GROVE_WORKER_CLIQUE,
                "replicas": worker_replicas,
                "minAvailable": worker_replicas,
                "podSpec": pod_spec(worker, container(worker, serving=False)),
            },
        }

        # engine.copies is the scaling group's replica count, so each copy is an
        # independent leader+worker gang. minAvailable is 1 whatever copies is:
        # Grove puts every replica below the threshold into one shared PodGang,
        # so matching it to copies gang-schedules all of them as a single unit
        # and deletes every copy once any one sits below it for
        # terminationDelay. A copy is still all-or-nothing through its cliques'
        # own minAvailable. Fields the defaulting webhook would fill in are set
        # explicitly so provider-kubernetes doesn't fight it on every reconcile.
        #
        # NOTE(negz): the cost is that a replica reports available once one copy
        # serves, since PodCliqueSet.status.availableReplicas is the only signal
        # Grove populates - status.podGangStatuses exists on the type but
        # nothing writes it.
        copies = int(engine.copies or 1)
        pod_clique_set = {
            "apiVersion": "grove.io/v1alpha1",
            "kind": "PodCliqueSet",
            "metadata": {"name": name, "namespace": base.remote_namespace(replica)},
            "spec": {
                "replicas": 1,
                "template": {
                    "cliqueStartupType": "CliqueStartupTypeExplicit",
                    "terminationDelay": "4h",
                    "headlessServiceConfig": {"publishNotReadyAddresses": True},
                    "cliques": [leader_clique, worker_clique],
                    "podCliqueScalingGroups": [
                        {
                            "name": base.GROVE_PCSG,
                            "cliqueNames": [base.GROVE_LEADER_CLIQUE, base.GROVE_WORKER_CLIQUE],
                            "replicas": copies,
                            "minAvailable": 1,
                        }
                    ],
                },
            },
        }

        composed = {
            base.workload_key(engine): base.wrap_object(
                provider_config, pod_clique_set, cel_query=base.GROVE_AVAILABLE_CEL
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
