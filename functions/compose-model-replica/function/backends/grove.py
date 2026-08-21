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

Selected for a Leader/Worker engine when the target cluster's serving stack is
stack: Dynamo; Standard (the llmd backend) is the default alternative. Renders a
PodCliqueSet whose template holds two cliques - "leader"
(one pod) and "worker" (the Worker member's node count) - grouped into one
PodCliqueScalingGroup gang-scheduled by KAI. engine.copies is the scaling
group's replica count, so each copy is an independent leader+worker gang; the
PodCliqueSet itself has a single replica.

The scaling group (rather than two standalone cliques) is what lets a gang's
pods share a group-wide identity: Grove numbers a scaling group replica's pods
0..N across its member cliques (leader 0, workers 1..N), which is the rank a
multi-node engine wants. It also matches where NVIDIA is taking Grove - the
leader+worker-in-one-scaling-group topology is the one they model (grove#754).

Routing is layered on afterwards by routing.py, the same as the native
backend: a GAIE InferencePool + endpoint picker fronts the leader clique's
pods, selected by the serving label they carry. The worker clique carries no
serving label, so the pool never routes to it.

Modelplane is unopinionated about the engine. Both the leader's and the
worker's commands and args are passed through verbatim - Modelplane injects no
parallelism flags and no bootstrap. A multi-node launch convention Modelplane
has never heard of still works, because the coordination asymmetry between
running the head and joining it lives in the two members' commands, which the
user writes.

The worker reaches the leader, and each pod finds its rank, through the env
vars Grove injects into every gang pod. The leader is the first pod of the
leader clique, reachable at
$(GROVE_PCS_NAME)-$(GROVE_PCS_INDEX)-gang-0-leader-0.$(GROVE_HEADLESS_SERVICE):
Grove names a scaling group replica's pods
<pcs>-<pcsReplica>-<scalingGroup>-<sgReplica>-<clique>-<pod>, and this backend
uses one scaling group ("gang") with one replica (index 0) per gang, so the
leader is at gang-0-leader-0. The rank is 0 on the leader (hard-coded) and
$((GROVE_PCLQ_POD_INDEX + 1)) on the workers, the shell doing the +1 the
downward API can't.

The released Grove this composes against (v0.1.0-alpha.6) leaves the tidier
GROVE_PCSG_NAME / GROVE_PCSG_INDEX empty and offers no group-wide pod index, so
commands can't yet lean on them; the GROVE_PCS_* form above is what resolves
today. Modelplane also doesn't yet wrap any of this in backend-neutral
MODELPLANE_LEADER_ADDRESS / MODELPLANE_RANK env vars: that needs Grove to inject
its vars before template env (grove#753, merged but unreleased) and to expose
GROVE_PCSG_POD_INDEX (grove#755, open). Until those ship in a release, a gang
engine's command references the Grove vars directly; see
docs/manifests/concepts/model-deployment-multinode.yaml.
TODO(grove#753, grove#755): once a Grove release injects GROVE_PCSG_* and a
group-wide pod index, switch the leader address to the GROVE_PCSG_* form and
inject MODELPLANE_LEADER_ADDRESS / MODELPLANE_RANK so commands stop naming
GROVE_* vars.

Weight loading mirrors native: the engine's --model arg is passed through
unmodified, so the engine fetches from its source at startup using credentials
from engine.env.
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
            # The turnkey cache --model injection is for the serving engine (the
            # leader) only; a worker joins via its own command and never serves,
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
            # Modelplane injects no gang-coordination env here: the leader
            # address and rank come from the GROVE_* vars Grove injects into
            # every pod, which a gang engine's command references directly (see
            # the module docstring and the multinode example). ModelExpress env
            # (if the cache distributes that way) applies to every rank, leader
            # and worker alike - each rank publishes itself as a source
            # independently.
            env = list(base.modelexpress_env(replica, stack))
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

        # Only the leader serves the OpenAI API -> its clique carries the
        # serving label the replica's InferencePool selects on, plus the queue
        # label every Grove-composed clique needs (see compose-serving-stack's
        # compose_kai_queues), the serving port, and the readiness probe. The
        # leader member's own template.metadata merges in underneath them; Grove
        # propagates a clique's labels and annotations to its pods.
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
        # The worker clique doesn't serve the OpenAI API, so it carries no
        # serving label - the InferencePool must never route to it. minAvailable
        # equals its full replica count, so a gang is all-or-nothing: Grove (via
        # KAI) won't consider the worker clique available on a partial start.
        # Only the worker member's own template.metadata applies. These
        # per-clique replica counts are per scaling-group replica; Grove
        # multiplies them by the group's replica count (engine.copies).
        #
        # No startsAfter: the leader and worker cliques start together. A
        # multi-node engine's leader only becomes Ready once every worker has
        # joined it (that's what forms the parallel group), so gating the
        # workers on the leader's readiness - Grove's startsAfter waits for the
        # parent clique's minAvailable pods to go Ready - would deadlock: the
        # leader waits for workers it will never get. The gang is co-scheduled
        # regardless (the scaling group's minAvailable), and a worker's command
        # reaches the leader through the leader's stable DNS name, retrying
        # until the leader is listening.
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

        # A single PodCliqueSet replica holds one scaling group that gangs the
        # leader and worker cliques. engine.copies is the scaling group's
        # replica count, so it scales the whole gang: Grove creates copies
        # independent leader+worker groups, each with its own group-wide pod
        # index (the rank), rather than copies loose leader/worker pods. The
        # group's minAvailable matches its replica count, so every copy must be
        # available for the PodCliqueSet to report itself available (the
        # readiness signal GROVE_AVAILABLE_CEL reads). Fields the defaulting
        # webhook would otherwise fill in (minAvailable above, terminationDelay,
        # headlessServiceConfig) are set explicitly so provider-kubernetes
        # doesn't fight the webhook on every reconcile.
        copies = int(engine.copies or 1)
        pod_clique_set = {
            "apiVersion": "grove.io/v1alpha1",
            "kind": "PodCliqueSet",
            "metadata": {"name": name, "namespace": base.REMOTE_NAMESPACE},
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
                            "minAvailable": copies,
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
