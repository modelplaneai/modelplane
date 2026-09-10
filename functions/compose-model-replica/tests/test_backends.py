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

"""Tests for compose-model-replica backends.

A backend builds the workload (Deployment, LeaderWorkerSet, or PodCliqueSet) and the
ResourceClaimTemplates for one worker engine; the InferencePool, endpoint picker,
and HTTPRoute that front a replica's engines are built by routing.apply. Manifests are
asserted with a `Case` table: each case builds an engine's backend and compares
the composed manifests to a full `want`. Backend selection and serving are
dispatch/behaviour tests below the table.
"""

import dataclasses
import unittest
from typing import Any, ClassVar

from crossplane.function import resource
from function import routing
from function.backends import base, grove, llmd, native
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

_SERVING = "modelplane.ai/serving"
_WORKLOAD = "modelplane.ai/workload"
_CLIQUE_ROLE = "modelplane.ai/clique-role"
_QUEUE_LABEL = "kai.scheduler/queue"
_QUEUE = "modelplane"
_SCHEDULER = "kai-scheduler"

# A GPU device request (claim: DRA), as compose-model-deployment stamps it.
_GPU_CEL = 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'


def _gpu_request(count: int) -> v1alpha1.DeviceRequest:
    return v1alpha1.DeviceRequest(
        name="gpu",
        deviceClassName="gpu.nvidia.com",
        count=count,
        selectors=[v1alpha1.Selector(cel=_GPU_CEL)],
    )


def _standalone_engine(
    name: str = "main",
    *,
    copies: int = 1,
    args: list[str] | None = None,
    command: list[str] | None = None,
    device_requests: list[v1alpha1.DeviceRequest] | None = None,
) -> v1alpha1.Engine:
    """A single Standalone-member engine."""
    container = v1alpha1.Container(
        name="engine",
        image="vllm/vllm-openai:latest",
        args=args if args is not None else ["--model=Qwen/Qwen3-0.6B"],
    )
    if command is not None:
        container.command = command
    return v1alpha1.Engine(
        name=name,
        copies=copies,
        members=[
            v1alpha1.Member(
                role="Standalone",
                nodePoolName="frontier",
                deviceRequests=device_requests if device_requests is not None else [_gpu_request(1)],
                template=v1alpha1.Template(spec=v1alpha1.Spec(containers=[container])),
            ),
        ],
    )


def _gang_engine(
    name: str = "main",
    *,
    copies: int = 1,
    nodes: int = 1,
    leader_args: list[str] | None = None,
    leader_command: list[str] | None = None,
    worker_args: list[str] | None = None,
    worker_command: list[str] | None = None,
    leader_device_requests: list[v1alpha1.DeviceRequest] | None = None,
    leader_pool: str = "frontier",
) -> v1alpha1.Engine:
    """A Leader + Worker engine.

    The members carry their own pool pins and device requests, defaulting to a
    homogeneous gang on one pool. leader_device_requests=[] makes the leader
    claimless (a coordinator-only leader); leader_pool moves it to another
    pool.
    """

    def member(
        role: str,
        nodes: int | None,
        args: list[str] | None,
        command: list[str] | None,
        device_requests: list[v1alpha1.DeviceRequest],
        pool: str,
    ) -> v1alpha1.Member:
        container = v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")
        if args is not None:
            container.args = args
        if command is not None:
            container.command = command
        kwargs: dict[str, Any] = {
            "role": role,
            "nodePoolName": pool,
            "template": v1alpha1.Template(spec=v1alpha1.Spec(containers=[container])),
        }
        if device_requests:
            kwargs["deviceRequests"] = device_requests
        if nodes is not None:
            kwargs["worker"] = v1alpha1.Worker(nodes=nodes)
        return v1alpha1.Member(**kwargs)

    leader_requests = leader_device_requests if leader_device_requests is not None else [_gpu_request(8)]
    return v1alpha1.Engine(
        name=name,
        copies=copies,
        members=[
            member("Leader", None, leader_args, leader_command, leader_requests, leader_pool),
            member("Worker", nodes, worker_args, worker_command, [_gpu_request(8)], "frontier"),
        ],
    )


def _replica(
    name: str = "r", *, namespace: str = "ml-team", engines: list[v1alpha1.Engine] | None = None
) -> v1alpha1.ModelReplica:
    if engines is None:
        engines = [_standalone_engine()]
    return v1alpha1.ModelReplica(
        metadata=metav1.ObjectMeta(name=name, namespace=namespace),
        spec=v1alpha1.SpecModel(clusterName="cluster-a", engines=engines),
    )


# The composed workload name for the default replica "r" / engine "main":
# engine-qualified so a multi-engine replica's workloads don't collide.
_WORKLOAD_NAME = resource.child_name("r", "main")

# The Grove PodCliqueSet name for the same replica/engine, budgeted tighter
# than _WORKLOAD_NAME per base.grove_pcs_name.
_GROVE_PCS_NAME = base.grove_pcs_name(_replica(), _gang_engine())


def _claim_template(count: int, *, replica: str = "r", engine: str = "main", role: str = "standalone") -> dict:
    """The ResourceClaimTemplate manifest a member's device requests produce."""
    return {
        "apiVersion": "resource.k8s.io/v1",
        "kind": "ResourceClaimTemplate",
        "metadata": {"name": resource.child_name(replica, engine, role, "devices"), "namespace": "default"},
        "spec": {
            "spec": {
                "devices": {
                    "requests": [
                        {
                            "name": "gpu",
                            "exactly": {
                                "deviceClassName": "gpu.nvidia.com",
                                "count": count,
                                "selectors": [{"cel": {"expression": _GPU_CEL}}],
                            },
                        }
                    ]
                }
            }
        },
    }


_CLUSTER = icv1alpha1.InferenceCluster(
    metadata=metav1.ObjectMeta(name="cluster-a"),
    spec=icv1alpha1.Spec(
        cluster=icv1alpha1.Cluster(
            source="Existing", existing=icv1alpha1.Existing(secretRef=icv1alpha1.SecretRef(name="k"))
        )
    ),
    status=icv1alpha1.Status(providerConfigRef=icv1alpha1.ProviderConfigRef(name="cluster-a-pc")),
)

_PC = "cluster-a-pc"


_NATIVE_WANT = {
    "model-serving-main": {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": _WORKLOAD_NAME, "namespace": "default"},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {_WORKLOAD: _WORKLOAD_NAME}},
            "template": {
                "metadata": {"labels": {_SERVING: "r", _WORKLOAD: _WORKLOAD_NAME}},
                "spec": {
                    "containers": [
                        {
                            "name": "engine",
                            "image": "vllm/vllm-openai:latest",
                            "args": ["--model=Qwen/Qwen3-0.6B"],
                            "ports": [{"containerPort": 8000}],
                            "resources": {"claims": [{"name": "devices"}]},
                            "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                            "readinessProbe": {
                                "httpGet": {"path": "/health", "port": 8000},
                                "initialDelaySeconds": 30,
                                "periodSeconds": 10,
                                "timeoutSeconds": 5,
                            },
                        }
                    ],
                    "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                    "nodeSelector": {"modelplane.ai/pool": "frontier"},
                    "resourceClaims": [
                        {
                            "name": "devices",
                            "resourceClaimTemplateName": resource.child_name("r", "main", "standalone", "devices"),
                        }
                    ],
                    "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}],
                },
            },
        },
    },
    "resource-claim-main-standalone": _claim_template(1),
}


def _claims(role: str) -> list[dict]:
    """The pod-level claim referencing a member's ResourceClaimTemplate."""
    return [
        {
            "name": "devices",
            "resourceClaimTemplateName": resource.child_name("r", "main", role, "devices"),
        }
    ]


def _clique(manifest: dict, name: str) -> dict:
    """The named clique from a PodCliqueSet manifest."""
    return next(c for c in manifest["spec"]["template"]["cliques"] if c["name"] == name)


def _pcs(leader_container: dict, worker_container: dict, *, worker_replicas: int = 1, copies: int = 1) -> dict:
    node_selector = {"modelplane.ai/pool": "frontier"}
    tolerations = [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]

    def pod_spec(container: dict, role: str) -> dict:
        return {
            "containers": [container],
            "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            "schedulerName": _SCHEDULER,
            "nodeSelector": node_selector,
            "resourceClaims": _claims(role),
            "tolerations": tolerations,
        }

    return {
        "apiVersion": "grove.io/v1alpha1",
        "kind": "PodCliqueSet",
        "metadata": {"name": _GROVE_PCS_NAME, "namespace": "default"},
        "spec": {
            "replicas": 1,
            "template": {
                "cliqueStartupType": "CliqueStartupTypeExplicit",
                "terminationDelay": "4h",
                "headlessServiceConfig": {"publishNotReadyAddresses": True},
                "cliques": [
                    {
                        "name": "leader",
                        "labels": {_SERVING: "r", _QUEUE_LABEL: _QUEUE, _CLIQUE_ROLE: "leader"},
                        "spec": {
                            "roleName": "leader",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": pod_spec(leader_container, "leader"),
                        },
                    },
                    {
                        "name": "worker",
                        "labels": {_QUEUE_LABEL: _QUEUE},
                        "spec": {
                            "roleName": "worker",
                            "replicas": worker_replicas,
                            "minAvailable": worker_replicas,
                            "podSpec": pod_spec(worker_container, "worker"),
                        },
                    },
                ],
                "podCliqueScalingGroups": [
                    {
                        "name": "gang",
                        "cliqueNames": ["leader", "worker"],
                        "replicas": copies,
                        # 1 regardless of copies, so a wedged gang doesn't take
                        # the healthy ones down with it.
                        "minAvailable": 1,
                    }
                ],
            },
        },
    }


def _engine(
    *, serving: bool, args: list[str] | None = None, command: list[str] | None = None, env: list[dict] | None = None
) -> dict[str, Any]:
    c: dict[str, Any] = {
        "name": "engine",
        "image": "vllm/vllm-openai:latest",
        "resources": {"claims": [{"name": "devices"}]},
        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
    }
    if command is not None:
        c["command"] = command
    if args is not None:
        c["args"] = args
    # A container carries an env only when the test gives it one. The Grove
    # backend always injects a leader-address alias (see grove.py); callers
    # composing a Grove _GROVE_WANT container pass it explicitly.
    if env is not None:
        c["env"] = env
    if serving:
        c["ports"] = [{"containerPort": 8000}]
        c["readinessProbe"] = {
            "httpGet": {"path": "/health", "port": 8000},
            "initialDelaySeconds": 30,
            "periodSeconds": 10,
            "timeoutSeconds": 5,
        }
    return c


# A multi-node engine with verbatim leader/worker commands - no flag injection,
# no bootstrap. The follower addresses the leader through
# $(MODELPLANE_LEADER_ADDRESS).
_LEADER_CMD = [
    "/bin/sh",
    "-c",
    "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B "
    "--tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
]
_WORKER_CMD = ["/bin/sh", "-c", "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block"]
_GROVE_WANT = {
    "model-serving-main": _pcs(
        _engine(serving=True, command=_LEADER_CMD, env=[base.grove_leader_address_env()]),
        _engine(serving=False, command=_WORKER_CMD, env=[base.grove_leader_address_env()]),
    ),
    "resource-claim-main-leader": _claim_template(8, role="leader"),
    "resource-claim-main-worker": _claim_template(8, role="worker"),
}


@dataclasses.dataclass
class Case:
    name: str
    backend: base.Backend
    engine: v1alpha1.Engine
    want: dict
    stack: str = "Standard"


_CASES = [
    Case(
        name="native Standalone engine composes a Deployment",
        backend=native.NativeBackend(),
        engine=_standalone_engine(),
        want=_NATIVE_WANT,
    ),
    Case(
        name="Grove Leader/Worker engine composes a PodCliqueSet, commands verbatim",
        backend=grove.GroveBackend(),
        engine=_gang_engine(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD),
        want=_GROVE_WANT,
        stack="Dynamo",
    ),
]


class TestBackendManifests(unittest.TestCase):
    def test_manifests(self) -> None:
        for case in _CASES:
            with self.subTest(case.name):
                replica = _replica(engines=[case.engine])
                out = case.backend.build(replica, case.engine, _PC, base.serving_label(replica), case.stack)
                got = {key: obj.spec.forProvider.manifest for key, obj in out.items()}
                self.assertEqual(case.want, got, "-want, +got")

    def test_leader_address_env_injected_but_not_rank(self) -> None:
        # The Grove backend injects MODELPLANE_LEADER_ADDRESS (aliasing Grove's
        # own GROVE_PCSG_* vars) but not MODELPLANE_RANK: Grove exposes no
        # group-wide pod index yet (grove#755, open), so a gang engine's
        # command computes its own rank from GROVE_PCLQ_POD_INDEX directly
        # (see grove.py and the multinode example).
        engine = _gang_engine(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD)
        replica = _replica(engines=[engine])
        out = grove.GroveBackend().build(replica, engine, _PC, base.serving_label(replica), "Dynamo")
        manifest = out["model-serving-main"].spec.forProvider.manifest
        # Spelled out rather than compared against grove_leader_address_env(),
        # which would pass whatever that function returned. The PCSG vars are
        # what make the address vary per gang; the PCS-scoped ones are
        # identical across gangs and would silently point every copy at gang
        # 0's leader.
        want = {
            "name": "MODELPLANE_LEADER_ADDRESS",
            "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
        }
        for clique_name in ("leader", "worker"):
            container = _clique(manifest, clique_name)["spec"]["podSpec"]["containers"][0]
            self.assertEqual(container["env"], [want])

    def test_user_env_passed_through(self) -> None:
        # A member's own env passes through verbatim, after the leader-address
        # alias (see test_leader_address_env_injected_but_not_rank).
        engine = _gang_engine(
            leader_command=_LEADER_CMD,
            worker_command=_WORKER_CMD,
        )
        spec = engine.members[0].template.spec
        assert spec is not None
        spec.containers[0].env = [v1alpha1.EnvItem(name="HF_TOKEN", value="x")]
        replica = _replica(engines=[engine])
        out = grove.GroveBackend().build(replica, engine, _PC, base.serving_label(replica), "Dynamo")
        manifest = out["model-serving-main"].spec.forProvider.manifest
        leader = _clique(manifest, "leader")["spec"]["podSpec"]
        env = leader["containers"][0]["env"]
        self.assertEqual(env, [base.grove_leader_address_env(), {"name": "HF_TOKEN", "value": "x"}])

    def test_fieldref_env_passes_through(self) -> None:
        # A pod-field env (e.g. VLLM_HOST_IP from status.podIP, which multi-NIC
        # RDMA nodes need so the engine binds the right interface — #141) survives
        # model_dump into the composed manifest.
        engine = _gang_engine(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD)
        spec = engine.members[0].template.spec
        assert spec is not None
        spec.containers[0].env = [
            v1alpha1.EnvItem(
                name="VLLM_HOST_IP",
                valueFrom=v1alpha1.ValueFrom(fieldRef=v1alpha1.FieldRef(fieldPath="status.podIP")),
            )
        ]
        replica = _replica(engines=[engine])
        out = grove.GroveBackend().build(replica, engine, _PC, base.serving_label(replica), "Dynamo")
        manifest = out["model-serving-main"].spec.forProvider.manifest
        leader = _clique(manifest, "leader")["spec"]["podSpec"]
        env = leader["containers"][0]["env"]
        self.assertEqual(
            env,
            [
                base.grove_leader_address_env(),
                {"name": "VLLM_HOST_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}},
            ],
        )

    def test_member_metadata_propagates_to_native_pod_template(self) -> None:
        # A Standalone member's template.metadata labels and annotations land
        # on the Deployment's pod template, merged with the managed labels
        # (#378).
        engine = _standalone_engine()
        engine.members[0].template.metadata = v1alpha1.Metadata(
            labels={"example.com/role": "standalone"},
            annotations={"example.com/config": "standalone"},
        )
        replica = _replica(engines=[engine])
        out = native.NativeBackend().build(replica, engine, _PC, base.serving_label(replica), "Standard")
        meta = out["model-serving-main"].spec.forProvider.manifest["spec"]["template"]["metadata"]
        self.assertEqual(
            meta["labels"],
            {"example.com/role": "standalone", _SERVING: "r", _WORKLOAD: _WORKLOAD_NAME},
        )
        self.assertEqual(meta["annotations"], {"example.com/config": "standalone"})

    def test_member_metadata_propagates_to_cliques_independently(self) -> None:
        # Leader metadata lands on the leader clique and worker metadata on the
        # worker clique; neither leaks into the other. Grove propagates a
        # clique's labels and annotations to its pods (#378).
        engine = _gang_engine(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD)
        engine.members[0].template.metadata = v1alpha1.Metadata(
            labels={"example.com/role": "leader"}, annotations={"example.com/config": "leader"}
        )
        engine.members[1].template.metadata = v1alpha1.Metadata(
            labels={"example.com/role": "worker"}, annotations={"example.com/config": "worker"}
        )
        replica = _replica(engines=[engine])
        out = grove.GroveBackend().build(replica, engine, _PC, base.serving_label(replica), "Dynamo")
        manifest = out["model-serving-main"].spec.forProvider.manifest
        leader = _clique(manifest, "leader")
        self.assertEqual(
            leader["labels"],
            {"example.com/role": "leader", _SERVING: "r", _QUEUE_LABEL: _QUEUE, _CLIQUE_ROLE: "leader"},
        )
        self.assertEqual(leader["annotations"], {"example.com/config": "leader"})
        worker = _clique(manifest, "worker")
        self.assertEqual(worker["labels"], {"example.com/role": "worker", _QUEUE_LABEL: _QUEUE})
        self.assertEqual(worker["annotations"], {"example.com/config": "worker"})

    def test_worker_without_metadata_composes_only_managed_labels(self) -> None:
        # A worker member with no template.metadata composes a worker clique
        # carrying only the managed queue label and no annotations key.
        engine = _gang_engine(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD)
        replica = _replica(engines=[engine])
        out = grove.GroveBackend().build(replica, engine, _PC, base.serving_label(replica), "Dynamo")
        manifest = out["model-serving-main"].spec.forProvider.manifest
        worker = _clique(manifest, "worker")
        self.assertEqual(worker["labels"], {_QUEUE_LABEL: _QUEUE})
        self.assertNotIn("annotations", worker)

    @staticmethod
    def _names(out: dict[str, k8sobjv1alpha1.Object]) -> set[str]:
        return {o.spec.forProvider.manifest["metadata"]["name"] for o in out.values()}

    def test_co_located_replicas_get_distinct_names(self) -> None:
        # Two replicas of one deployment on the same cluster must produce
        # distinct resource names on the remote cluster.
        a = _replica("dep-clusterA")
        b = _replica("dep-clusterB")
        out_a = native.NativeBackend().build(a, a.spec.engines[0], _PC, base.serving_label(a), "Standard")
        out_b = native.NativeBackend().build(b, b.spec.engines[0], _PC, base.serving_label(b), "Standard")
        self.assertEqual(self._names(out_a) & self._names(out_b), set())

    def test_multi_engine_qualifies_workload_names(self) -> None:
        # A replica with two engines names each engine's workload distinctly so
        # they don't collide on the remote cluster.
        engines = [_standalone_engine("prefill"), _standalone_engine("decode")]
        replica = _replica(engines=engines)
        names = set()
        for g in engines:
            out = native.NativeBackend().build(replica, g, _PC, base.serving_label(replica), "Standard")
            names |= self._names(out)
        self.assertEqual(len(names), 4)  # 2 deployments + 2 claim templates

    def test_workload_readiness_policies(self) -> None:
        # A Deployment reports readiness from its Available condition; a
        # PodCliqueSet publishes no such condition, so it's derived from its
        # replica counters instead (base.GROVE_AVAILABLE_CEL). Either way the
        # claim templates are ready on create.
        for name, backend, engine, stack, want_cel in (
            ("native", native.NativeBackend(), _standalone_engine(), "Standard", base.AVAILABLE_CEL),
            (
                "grove",
                grove.GroveBackend(),
                _gang_engine(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD),
                "Dynamo",
                base.GROVE_AVAILABLE_CEL,
            ),
        ):
            with self.subTest(name):
                replica = _replica(engines=[engine])
                out = backend.build(replica, engine, _PC, base.serving_label(replica), stack)
                serving = out["model-serving-main"].spec.readiness
                assert serving is not None
                self.assertEqual(serving.policy, "DeriveFromCelQuery")
                self.assertEqual(serving.celQuery, want_cel)
                for key, obj in out.items():
                    if key.startswith("resource-claim"):
                        readiness = obj.spec.readiness
                        assert readiness is not None
                        self.assertEqual(readiness.policy, "SuccessfulCreate")

    def test_multiple_device_requests_single_container_claim(self) -> None:
        # resources.claims is a list-map keyed on name alone, so N device
        # requests must NOT produce N container claims all named "devices". The
        # container references the whole pod claim once; the template carries all
        # requests.
        engine = _standalone_engine(
            device_requests=[
                v1alpha1.DeviceRequest(name="gpu", deviceClassName="gpu.nvidia.com", count=8),
                v1alpha1.DeviceRequest(name="nic", deviceClassName="nic.nvidia.com", count=8),
            ],
        )
        replica = _replica(engines=[engine])
        out = native.NativeBackend().build(replica, engine, _PC, base.serving_label(replica), "Standard")
        pod = out["model-serving-main"].spec.forProvider.manifest["spec"]["template"]["spec"]
        claims = pod["containers"][0]["resources"]["claims"]
        self.assertEqual(claims, [{"name": "devices"}])
        self.assertEqual(pod["resourceClaims"][0]["name"], "devices")
        template = out["resource-claim-main-standalone"].spec.forProvider.manifest
        template_requests = template["spec"]["spec"]["devices"]["requests"]
        self.assertEqual([r["name"] for r in template_requests], ["gpu", "nic"])
        claim_readiness = out["resource-claim-main-standalone"].spec.readiness
        assert claim_readiness is not None
        self.assertEqual(claim_readiness.policy, "SuccessfulCreate")

    def test_claimless_leader_gets_no_claim(self) -> None:
        # A coordinator-only leader (e.g. a vLLM DP head running
        # --data-parallel-size-local=0) carries no deviceRequests. Its pod must
        # get no resourceClaims, its container no resources.claims, and no
        # leader ResourceClaimTemplate must be composed - only the worker's.
        # It still pins to its pool and tolerates the GPU taint.
        engine = _gang_engine(
            leader_command=_LEADER_CMD,
            worker_command=_WORKER_CMD,
            leader_device_requests=[],
        )
        replica = _replica(engines=[engine])
        out = grove.GroveBackend().build(replica, engine, _PC, base.serving_label(replica), "Dynamo")

        self.assertNotIn("resource-claim-main-leader", out)
        self.assertIn("resource-claim-main-worker", out)

        manifest = out["model-serving-main"].spec.forProvider.manifest
        leader = _clique(manifest, "leader")["spec"]["podSpec"]
        self.assertNotIn("resourceClaims", leader)
        self.assertNotIn("resources", leader["containers"][0])
        self.assertEqual(leader["nodeSelector"], {"modelplane.ai/pool": "frontier"})
        self.assertEqual(
            leader["tolerations"], [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]
        )

        worker = _clique(manifest, "worker")["spec"]["podSpec"]
        self.assertEqual(worker["resourceClaims"], _claims("worker"))
        self.assertEqual(worker["containers"][0]["resources"], {"claims": [{"name": "devices"}]})

    def test_members_pin_to_their_own_pools(self) -> None:
        # The scheduler may split a gang across pools when no single pool
        # satisfies every member. Each member's pods must pin to that member's
        # pool, not a shared engine-wide one.
        engine = _gang_engine(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD, leader_pool="head")
        replica = _replica(engines=[engine])
        out = grove.GroveBackend().build(replica, engine, _PC, base.serving_label(replica), "Dynamo")
        manifest = out["model-serving-main"].spec.forProvider.manifest
        self.assertEqual(_clique(manifest, "leader")["spec"]["podSpec"]["nodeSelector"], {"modelplane.ai/pool": "head"})
        self.assertEqual(
            _clique(manifest, "worker")["spec"]["podSpec"]["nodeSelector"], {"modelplane.ai/pool": "frontier"}
        )


class TestLLMDBackend(unittest.TestCase):
    """The LeaderWorkerSet backend for a Leader/Worker gang engine."""

    _LWS_ROLE = "modelplane.ai/lws-role"

    @staticmethod
    def _lws(engine: v1alpha1.Engine, replica: v1alpha1.ModelReplica) -> dict:
        out = llmd.LLMDBackend().build(replica, engine, _PC, base.serving_label(replica), "Standard")
        return out["model-serving-main"].spec.forProvider.manifest

    def test_leader_worker_set_shape(self) -> None:
        engine = _gang_engine(nodes=3, copies=2)
        replica = _replica(engines=[engine])
        manifest = self._lws(engine, replica)
        self.assertEqual(manifest["apiVersion"], "leaderworkerset.x-k8s.io/v1")
        self.assertEqual(manifest["kind"], "LeaderWorkerSet")
        self.assertEqual(manifest["metadata"], {"name": _WORKLOAD_NAME, "namespace": "default"})
        self.assertEqual(manifest["spec"]["replicas"], 2)
        # Gang size is the leader plus the worker's node count.
        self.assertEqual(manifest["spec"]["leaderWorkerTemplate"]["size"], 4)

    def test_only_leader_carries_serving_label(self) -> None:
        engine = _gang_engine()
        replica = _replica(engines=[engine])
        lwt = self._lws(engine, replica)["spec"]["leaderWorkerTemplate"]
        leader_labels = lwt["leaderTemplate"]["metadata"]["labels"]
        self.assertEqual(leader_labels[_SERVING], "r")
        self.assertEqual(leader_labels[self._LWS_ROLE], "leader")
        # The worker followers never serve, so they carry no metadata at all.
        self.assertNotIn("metadata", lwt["workerTemplate"])

    def test_leader_address_and_rank_env_injected(self) -> None:
        # Every gang container leads with the backend-neutral coordination vars
        # aliasing LWS_LEADER_ADDRESS / LWS_WORKER_INDEX.
        engine = _gang_engine()
        replica = _replica(engines=[engine])
        lwt = self._lws(engine, replica)["spec"]["leaderWorkerTemplate"]
        for tmpl in (lwt["leaderTemplate"], lwt["workerTemplate"]):
            env = tmpl["spec"]["containers"][0]["env"]
            self.assertEqual(env[0], {"name": "MODELPLANE_LEADER_ADDRESS", "value": "$(LWS_LEADER_ADDRESS)"})
            self.assertEqual(env[1], {"name": "MODELPLANE_RANK", "value": "$(LWS_WORKER_INDEX)"})

    def test_no_modelexpress_env_even_with_a_cache(self) -> None:
        # The llm-d (Standard) backend never injects ModelExpress env: that P2P
        # wiring is the Grove (Dynamo) backend's, gated on the cluster stack.
        engine = _gang_engine()
        replica = v1alpha1.ModelReplica(
            metadata=metav1.ObjectMeta(name="r", namespace="ml-team"),
            spec=v1alpha1.SpecModel(
                clusterName="cluster-a",
                modelCacheRef=v1alpha1.ModelCacheRef(name="c"),
                engines=[engine],
            ),
        )
        lwt = self._lws(engine, replica)["spec"]["leaderWorkerTemplate"]
        for tmpl in (lwt["leaderTemplate"], lwt["workerTemplate"]):
            container = tmpl["spec"]["containers"][0]
            env_names = [e["name"] for e in container["env"]]
            # HF_HUB_CACHE is the cache's own env (every stack); the MX bundle
            # is not.
            self.assertEqual(env_names, ["MODELPLANE_LEADER_ADDRESS", "MODELPLANE_RANK", "HF_HUB_CACHE"])
            self.assertNotIn("MX_SERVER_ADDRESS", env_names)
            self.assertNotIn("securityContext", container)

    def test_workload_readiness_uses_available_cel(self) -> None:
        engine = _gang_engine()
        replica = _replica(engines=[engine])
        out = llmd.LLMDBackend().build(replica, engine, _PC, base.serving_label(replica), "Standard")
        readiness = out["model-serving-main"].spec.readiness
        assert readiness is not None
        self.assertEqual(readiness.policy, "DeriveFromCelQuery")
        self.assertEqual(readiness.celQuery, base.AVAILABLE_CEL)


class TestBackendSelection(unittest.TestCase):
    def test_standalone_engine_is_native(self) -> None:
        # A Standalone engine is native regardless of the cluster's stack.
        self.assertEqual(base.select_backend(_standalone_engine(), "Standard"), base.NATIVE)
        self.assertEqual(base.select_backend(_standalone_engine(), "Dynamo"), base.NATIVE)

    def test_leader_worker_engine_is_llmd(self) -> None:
        self.assertEqual(base.select_backend(_gang_engine(), "Standard"), base.LLMD)

    def test_leader_worker_engine_is_grove(self) -> None:
        self.assertEqual(base.select_backend(_gang_engine(), "Dynamo"), base.GROVE)


class TestCacheMounts(unittest.TestCase):
    def _replica(
        self, *, cache: str | None = None, args: list[str] | None = None, command: list[str] | None = None
    ) -> v1alpha1.ModelReplica:
        engine = _standalone_engine(args=args or [], command=command)
        modelcache = v1alpha1.ModelCacheRef(name=cache) if cache else None
        return v1alpha1.ModelReplica(
            metadata=metav1.ObjectMeta(namespace="ml-team"),
            spec=v1alpha1.SpecModel(clusterName="c", modelCacheRef=modelcache, engines=[engine]),
        )

    @staticmethod
    def _engine(replica: v1alpha1.ModelReplica) -> v1alpha1.Container:
        spec = replica.spec.engines[0].members[0].template.spec
        assert spec is not None
        return spec.containers[0]

    def test_no_cache_no_mounts(self) -> None:
        volumes, mounts = base.cache_mounts(self._replica())
        self.assertEqual((volumes, mounts), ([], []))

    def test_cache_adds_volume_and_mount(self) -> None:
        volumes, mounts = base.cache_mounts(self._replica(cache="qwen"))
        self.assertEqual(
            volumes,
            [{"name": "model-cache", "persistentVolumeClaim": {"claimName": "modelcache-ml-team-qwen-17db2"}}],
        )
        self.assertEqual(mounts, [{"name": "model-cache", "mountPath": "/mnt/models"}])

    def test_cache_env_points_huggingface_at_the_mount(self) -> None:
        # The cache is staged in HuggingFace's cache layout, so pointing
        # HF_HUB_CACHE at the mount is what lets an engine's own --model=<repo>
        # resolve against it instead of pulling from HuggingFace (#407).
        self.assertEqual(
            base.cache_env(self._replica(cache="qwen")),
            [{"name": "HF_HUB_CACHE", "value": "/mnt/models"}],
        )

    def test_cache_env_empty_without_cache(self) -> None:
        self.assertEqual(base.cache_env(self._replica()), [])

    def test_cache_env_sets_no_offline_flag(self) -> None:
        # HF_HUB_OFFLINE would break an engine that fetches a *different* repo
        # at startup (kimi-k2's separately-gated tokenizer), and resolution
        # doesn't need it.
        names = {e["name"] for e in base.cache_env(self._replica(cache="qwen"))}
        self.assertNotIn("HF_HUB_OFFLINE", names)


class TestNativeBackendCache(unittest.TestCase):
    def _replica(self) -> v1alpha1.ModelReplica:
        engine = _standalone_engine(args=[])
        return v1alpha1.ModelReplica(
            metadata=metav1.ObjectMeta(name="r", namespace="ml-team"),
            spec=v1alpha1.SpecModel(
                clusterName="cluster-a",
                modelCacheRef=v1alpha1.ModelCacheRef(name="qwen"),
                engines=[engine],
            ),
        )

    def test_mounts_pvc_and_sets_cache_env(self) -> None:
        # A cache contributes a volume, a mount, and the HF_HUB_CACHE that makes
        # the engine's own --model=<repo> resolve against it. Modelplane injects
        # no --model of its own: naming the model is the command's job.
        replica = self._replica()
        out = native.NativeBackend().build(
            replica, replica.spec.engines[0], _PC, base.serving_label(replica), "Standard"
        )
        dep = out["model-serving-main"].spec.forProvider.manifest
        pod = dep["spec"]["template"]["spec"]
        vol_names = {v["name"] for v in pod["volumes"]}
        self.assertIn("model-cache", vol_names)
        container = pod["containers"][0]
        self.assertIn({"name": "model-cache", "mountPath": "/mnt/models"}, container["volumeMounts"])
        self.assertIn({"name": "HF_HUB_CACHE", "value": "/mnt/models"}, container["env"])
        self.assertEqual(container["args"], [])

    def test_user_env_comes_after_cache_env(self) -> None:
        # Kubernetes expands $(VAR) left to right, so Modelplane's own entries
        # must precede the user's for a user entry to reference them.
        replica = self._replica()
        engine = replica.spec.engines[0]
        spec = engine.members[0].template.spec
        assert spec is not None
        spec.containers[0].env = [v1alpha1.EnvItem(name="HF_TOKEN", value="x")]
        out = native.NativeBackend().build(replica, engine, _PC, base.serving_label(replica), "Standard")
        container = out["model-serving-main"].spec.forProvider.manifest["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(
            container["env"],
            [{"name": "HF_HUB_CACHE", "value": "/mnt/models"}, {"name": "HF_TOKEN", "value": "x"}],
        )


class TestGroveBackendCache(unittest.TestCase):
    def _replica(
        self,
        *,
        leader_command: list[str] | None = None,
        worker_command: list[str] | None = None,
        leader_args: list[str] | None = None,
        worker_args: list[str] | None = None,
    ) -> v1alpha1.ModelReplica:
        engine = _gang_engine(
            leader_command=leader_command,
            worker_command=worker_command,
            leader_args=leader_args,
            worker_args=worker_args,
        )
        return v1alpha1.ModelReplica(
            metadata=metav1.ObjectMeta(name="r", namespace="ml-team"),
            spec=v1alpha1.SpecModel(
                clusterName="cluster-a",
                modelCacheRef=v1alpha1.ModelCacheRef(name="kimi"),
                engines=[engine],
            ),
        )

    def test_both_grove_cliques_mount_cache(self) -> None:
        replica = self._replica(leader_args=[], worker_command=["/bin/sh", "-c", "join"])
        manifest = (
            grove.GroveBackend()
            .build(replica, replica.spec.engines[0], _PC, base.serving_label(replica), "Dynamo")["model-serving-main"]
            .spec.forProvider.manifest
        )
        for clique_name in ("leader", "worker"):
            pod = _clique(manifest, clique_name)["spec"]["podSpec"]
            self.assertIn("model-cache", {v["name"] for v in pod["volumes"]})
            self.assertIn(
                {"name": "model-cache", "mountPath": "/mnt/models"},
                pod["containers"][0]["volumeMounts"],
            )

    def test_sets_cache_env_on_every_clique_and_injects_no_model(self) -> None:
        # A cache gives both cliques HF_HUB_CACHE so their own --model=<repo>
        # resolves against the mount; Modelplane adds no --model itself.
        replica = self._replica(leader_args=[], worker_command=["/bin/sh", "-c", "join"])
        manifest = (
            grove.GroveBackend()
            .build(replica, replica.spec.engines[0], _PC, base.serving_label(replica), "Dynamo")["model-serving-main"]
            .spec.forProvider.manifest
        )
        for clique_name in ("leader", "worker"):
            container = _clique(manifest, clique_name)["spec"]["podSpec"]["containers"][0]
            self.assertIn({"name": "HF_HUB_CACHE", "value": "/mnt/models"}, container["env"])
            self.assertNotIn("--model=/mnt/models", container.get("args", []))

    def test_command_engine_mounts_cache_without_injecting_model(self) -> None:
        # A member with its own command keeps it verbatim and gets no injected
        # --model (it points at the cache with its own flag).
        leader_cmd = [
            "/bin/sh",
            "-c",
            "python3 -m sglang.launch_server --model-path /mnt/models --tp 16",
        ]
        replica = self._replica(leader_command=leader_cmd, worker_command=["/bin/sh", "-c", "join"])
        manifest = (
            grove.GroveBackend()
            .build(replica, replica.spec.engines[0], _PC, base.serving_label(replica), "Dynamo")["model-serving-main"]
            .spec.forProvider.manifest
        )
        leader = _clique(manifest, "leader")["spec"]["podSpec"]["containers"][0]
        self.assertIn(
            {"name": "model-cache", "mountPath": "/mnt/models"},
            leader["volumeMounts"],
        )
        self.assertEqual(leader["command"], leader_cmd)


class TestDisaggregated(unittest.TestCase):
    """serving.mode: PrefillDecode routing layers an InferencePool + endpoint
    picker over two engines, role-labels them, and sidecars decode — no unified
    Service. Mirrors how fn.py composes engines then calls routing.apply."""

    def _apply(self) -> dict[str, k8sobjv1alpha1.Object]:
        prefill = _standalone_engine(name="prefill")
        prefill.phase = "Prefill"
        decode = _standalone_engine(name="decode")
        decode.phase = "Decode"
        replica = _replica(engines=[prefill, decode])
        replica.spec.serving = v1alpha1.Serving(mode="PrefillDecode")
        composed = {}
        for engine in replica.spec.engines:
            composed.update(native.NativeBackend().build(replica, engine, _PC, base.serving_label(replica), "Standard"))
        return routing.apply(composed, replica, _PC)

    def _serving_pod(self, out: dict[str, k8sobjv1alpha1.Object], engine_name: str) -> dict:
        return out[f"model-serving-{engine_name}"].spec.forProvider.manifest["spec"]["template"]

    def test_replaces_unified_service_with_pool_and_epp(self) -> None:
        out = self._apply()
        self.assertIn("inference-pool", out)
        self.assertIn("epp", out)
        self.assertIn("epp-config", out)
        pool = out["inference-pool"].spec.forProvider.manifest
        self.assertEqual(pool["kind"], "InferencePool")
        self.assertEqual(pool["spec"]["endpointPickerRef"]["name"], "r-epp")

    def test_injects_nixl_plumbing(self) -> None:
        """Both disagg engines get the NIXL plumbing the schema can't express:
        a Memory /dev/shm and VLLM_NIXL_SIDE_CHANNEL_HOST = pod IP."""
        out = self._apply()
        for role in ("prefill", "decode"):
            pod = self._serving_pod(out, role)["spec"]
            self.assertTrue(
                any(v.get("emptyDir", {}).get("medium") == "Memory" for v in pod["volumes"]),
                f"{role} missing Memory /dev/shm volume",
            )
            engine = next(c for c in pod["containers"] if c["name"] == "engine")
            self.assertIn("/dev/shm", [m["mountPath"] for m in engine["volumeMounts"]])
            host = next((e for e in engine["env"] if e["name"] == "VLLM_NIXL_SIDE_CHANNEL_HOST"), None)
            assert host is not None, f"{role} missing VLLM_NIXL_SIDE_CHANNEL_HOST"
            self.assertEqual(host["valueFrom"]["fieldRef"]["fieldPath"], "status.podIP")
            self.assertIn("VLLM_NIXL_SIDE_CHANNEL_PORT", [e["name"] for e in engine["env"]])

    def test_epp_config_arms_the_pd_decider(self) -> None:
        """PrefillDecode silently serves decode-only unless the PD decider is armed.

        Selective prefix-based-pd-decider needs all of: nonCachedTokens > 0 (0 =
        disabled), the approx-prefix-cache-producer plugin that populates the
        attribute it reads, and that producer pinned to autoTune: false (the
        true default never populates). And it must NOT carry the prepareDataPlugins
        feature gate, which the v0.8.0 EPP image rejects and crashloops on.
        """
        cfg = self._apply()["epp-config"].spec.forProvider.manifest["data"]["epp-config.yaml"]
        self.assertIn("prefix-based-pd-decider", cfg)
        self.assertIn("nonCachedTokens: 16", cfg)
        self.assertIn("approx-prefix-cache-producer", cfg)
        self.assertIn("autoTune: false", cfg)
        self.assertNotIn("nonCachedTokens: 0", cfg)
        self.assertNotIn("prepareDataPlugins", cfg)

    def test_epp_and_sidecar_images_and_config_group_are_pinned(self) -> None:
        """Lock the picker + sidecar images and the EndpointPickerConfig API group.

        Nothing else asserts these, so a wrong tag/registry path or a stale config
        group passes CI and only surfaces as an EPP/sidecar crashloop at deploy.
        These are deliberate literals, not routing._* constants: comparing to the
        constant would be tautological (it can't catch a typo in the constant), and
        a literal forces a bump to show up here and be reviewed.
        """
        out = self._apply()
        epp = out["epp"].spec.forProvider.manifest["spec"]["template"]["spec"]["containers"]
        self.assertEqual(
            next(c["image"] for c in epp if c["name"] == "epp"),
            "ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0",
        )
        sidecar = next(c for c in self._serving_pod(out, "decode")["spec"]["containers"] if c["name"] == "pd-sidecar")
        self.assertEqual(sidecar["image"], "ghcr.io/llm-d/llm-d-router-disagg-sidecar:v0.9.0")
        cfg = out["epp-config"].spec.forProvider.manifest["data"]["epp-config.yaml"]
        self.assertIn("apiVersion: llm-d.ai/v1alpha1", cfg)

    def test_epp_role_watches_inferenceobjectives(self) -> None:
        """The picker watches InferenceObjectives (GIE x-k8s.io group); the Role must allow it."""
        rules = self._apply()["epp-role"].spec.forProvider.manifest["rules"]
        self.assertTrue(
            any(
                "inference.networking.x-k8s.io" in r["apiGroups"] and "inferenceobjectives" in r["resources"]
                for r in rules
            ),
            f"EPP Role missing inferenceobjectives watch: {rules}",
        )

    def test_decode_port_follows_user_arg(self) -> None:
        """The sidecar and the decode container port track the user's --port, not a hardcoded one."""
        prefill = _standalone_engine(name="prefill")
        prefill.phase = "Prefill"
        decode = _standalone_engine(name="decode", args=["--model=m", "--port=9000"])
        decode.phase = "Decode"
        replica = _replica(engines=[prefill, decode])
        replica.spec.serving = v1alpha1.Serving(mode="PrefillDecode")
        composed = {}
        for e in replica.spec.engines:
            composed.update(native.NativeBackend().build(replica, e, _PC, base.serving_label(replica), "Standard"))
        out = routing.apply(composed, replica, _PC)
        containers = self._serving_pod(out, "decode")["spec"]["containers"]
        engine = next(c for c in containers if c["name"] == "engine")
        sidecar = next(c for c in containers if c["name"] == "pd-sidecar")
        self.assertEqual(engine["ports"][0]["containerPort"], 9000)
        self.assertIn("--vllm-port=9000", sidecar["args"])
        self.assertEqual(sidecar["ports"][0]["containerPort"], 8000)

    def test_engines_role_labeled(self) -> None:
        out = self._apply()
        self.assertEqual(self._serving_pod(out, "prefill")["metadata"]["labels"]["llm-d.ai/role"], "prefill")
        decode_labels = self._serving_pod(out, "decode")["metadata"]["labels"]
        self.assertEqual(decode_labels["llm-d.ai/role"], "decode")
        self.assertEqual(decode_labels["app"], "r")

    def test_decode_gets_sidecar_and_moves_engine_port(self) -> None:
        out = self._apply()
        containers = self._serving_pod(out, "decode")["spec"]["containers"]
        names = [c["name"] for c in containers]
        self.assertEqual(names, ["engine", "pd-sidecar"])
        engine = next(c for c in containers if c["name"] == "engine")
        self.assertEqual(engine["ports"][0]["containerPort"], 8001)
        self.assertEqual(engine["readinessProbe"]["timeoutSeconds"], 5)
        sidecar = next(c for c in containers if c["name"] == "pd-sidecar")
        self.assertEqual(sidecar["ports"][0]["containerPort"], 8000)
        self.assertEqual(sidecar["readinessProbe"]["timeoutSeconds"], 5)
        self.assertIn("--secure-proxy=false", sidecar["args"])

    def test_prefill_has_no_sidecar(self) -> None:
        containers = self._serving_pod(self._apply(), "prefill")["spec"]["containers"]
        self.assertEqual([c["name"] for c in containers], ["engine"])

    def test_route_targets_inference_pool(self) -> None:
        route = self._apply()[base.ROUTE_KEY].spec.forProvider.manifest
        rule = route["spec"]["rules"][0]
        ref = rule["backendRefs"][0]
        self.assertEqual(ref["kind"], "InferencePool")
        self.assertEqual(ref["name"], "r-pool")
        # Disable the request timeout so long token streams aren't severed.
        self.assertEqual(rule["timeouts"]["request"], "0s")

    def test_selects_engines_by_phase_not_name(self) -> None:
        """Roles come from each engine's phase, not its name."""
        decode = _standalone_engine(name="alpha")
        decode.phase = "Decode"
        prefill = _standalone_engine(name="beta")
        prefill.phase = "Prefill"
        replica = _replica(engines=[decode, prefill])
        replica.spec.serving = v1alpha1.Serving(mode="PrefillDecode")
        composed = {}
        for e in replica.spec.engines:
            composed.update(native.NativeBackend().build(replica, e, _PC, base.serving_label(replica), "Standard"))
        out = routing.apply(composed, replica, _PC)
        # alpha is Decode -> sidecar; beta is Prefill -> none, despite their names.
        self.assertEqual(
            [c["name"] for c in self._serving_pod(out, "alpha")["spec"]["containers"]], ["engine", "pd-sidecar"]
        )
        self.assertEqual([c["name"] for c in self._serving_pod(out, "beta")["spec"]["containers"]], ["engine"])
        self.assertEqual(self._serving_pod(out, "alpha")["metadata"]["labels"]["llm-d.ai/role"], "decode")
        self.assertEqual(self._serving_pod(out, "beta")["metadata"]["labels"]["llm-d.ai/role"], "prefill")

    def test_decode_can_be_a_grove_gang(self) -> None:
        """A PrefillDecode engine can itself be a Leader/Worker gang, so routing
        must decorate a Grove PodCliqueSet's leader clique - role label, serving
        label, pd-sidecar, NIXL plumbing - exactly like a Deployment's pod
        template. Exercises the _serving_pod_templates normalization that lets
        one routing layer decorate both workload shapes."""
        prefill = _standalone_engine(name="prefill")
        prefill.phase = "Prefill"
        decode = _gang_engine(name="decode", leader_command=_LEADER_CMD, worker_command=_WORKER_CMD)
        decode.phase = "Decode"
        replica = _replica(engines=[prefill, decode])
        replica.spec.serving = v1alpha1.Serving(mode="PrefillDecode")
        composed = {
            **native.NativeBackend().build(replica, prefill, _PC, base.serving_label(replica), "Standard"),
            **grove.GroveBackend().build(replica, decode, _PC, base.serving_label(replica), "Dynamo"),
        }
        out = routing.apply(composed, replica, _PC)

        manifest = out["model-serving-decode"].spec.forProvider.manifest
        leader_clique = _clique(manifest, "leader")
        self.assertEqual(leader_clique["labels"]["llm-d.ai/role"], "decode")
        self.assertEqual(leader_clique["labels"]["app"], "r")
        leader = leader_clique["spec"]["podSpec"]
        self.assertEqual([c["name"] for c in leader["containers"]], ["engine", "pd-sidecar"])
        self.assertTrue(
            any(v.get("emptyDir", {}).get("medium") == "Memory" for v in leader["volumes"]),
            "leader clique missing Memory /dev/shm volume for NIXL",
        )
        engine = next(c for c in leader["containers"] if c["name"] == "engine")
        self.assertIn("VLLM_NIXL_SIDE_CHANNEL_HOST", [e["name"] for e in engine["env"]])

        # The worker clique never serves; routing must not touch it at all -
        # its labels stay exactly what the Grove backend composed (just the
        # queue label), with no role or serving label added.
        worker_clique = _clique(manifest, "worker")
        worker = worker_clique["spec"]["podSpec"]
        self.assertEqual([c["name"] for c in worker["containers"]], ["engine"])
        self.assertEqual(worker_clique["labels"], {_QUEUE_LABEL: _QUEUE})


class TestUnifiedRouting(unittest.TestCase):
    """Unified serving (or no serving block) fronts the pods with an
    InferencePool + endpoint picker in place of a plain Service, so requests
    route by prefix cache and load rather than round-robin - one pod or many.
    Mirrors how fn.py composes engines then calls routing.apply."""

    def _apply(self, copies: int = 1) -> dict[str, k8sobjv1alpha1.Object]:
        engine = _standalone_engine(copies=copies)
        replica = _replica(engines=[engine])
        composed = native.NativeBackend().build(replica, engine, _PC, base.serving_label(replica), "Standard")
        return routing.apply(composed, replica, _PC)

    def test_fronts_with_pool_and_epp(self) -> None:
        out = self._apply()
        self.assertIn("inference-pool", out)
        self.assertIn("epp", out)
        self.assertIn("epp-config", out)
        pool = out["inference-pool"].spec.forProvider.manifest
        self.assertEqual(pool["kind"], "InferencePool")
        self.assertEqual(pool["spec"]["endpointPickerRef"]["name"], "r-epp")

    def test_single_pod_also_pools(self) -> None:
        """A single serving pod has nothing to pick between, but still gets the
        pool. Always fronting with one avoids swapping a Service for a pool when a
        second pod appears - a swap that would drop in-flight requests."""
        for copies in (1, 2):
            with self.subTest(copies=copies):
                out = self._apply(copies=copies)
                self.assertIn("inference-pool", out)
                self.assertIn("epp", out)

    def test_fronts_a_leader_worker_set(self) -> None:
        """A Standard multi-node engine composes a LeaderWorkerSet, and unified
        routing must handle that shape too: it reads the engine args for the KV
        block size through _serving_pod_templates, which has to normalize a
        LeaderWorkerSet's leaderTemplate alongside a Deployment's pod template
        and a Grove PodCliqueSet's leader clique. Regression for a shape
        normalization that only knew Deployment and PodCliqueSet and raised
        KeyError on a LeaderWorkerSet."""
        engine = _gang_engine(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD)
        replica = _replica(engines=[engine])
        composed = llmd.LLMDBackend().build(replica, engine, _PC, base.serving_label(replica), "Standard")
        out = routing.apply(composed, replica, _PC)
        self.assertIn("inference-pool", out)
        self.assertEqual(out["model-serving-main"].spec.forProvider.manifest["kind"], "LeaderWorkerSet")

    def test_pool_selects_pods_by_the_serving_label(self) -> None:
        """The pool selects the pods by the serving label they already carry, so
        no relabeling is needed."""
        pool = self._apply()["inference-pool"].spec.forProvider.manifest
        self.assertEqual(pool["spec"]["selector"]["matchLabels"], {base.LABEL_SERVING: "r"})

    def test_route_targets_inference_pool(self) -> None:
        route = self._apply()[base.ROUTE_KEY].spec.forProvider.manifest
        ref = route["spec"]["rules"][0]["backendRefs"][0]
        self.assertEqual(ref["kind"], "InferencePool")
        self.assertEqual(ref["name"], "r-pool")

    def test_epp_config_is_unified_not_disaggregated(self) -> None:
        """The unified picker scores by prefix cache and queue depth in a single
        profile, with no prefill/decode split, and still needs the
        approx-prefix-cache-producer that feeds the prefix-cache scorer."""
        cfg = self._apply()["epp-config"].spec.forProvider.manifest["data"]["epp-config.yaml"]
        self.assertIn("prefix-cache-scorer", cfg)
        self.assertIn("queue-scorer", cfg)
        self.assertIn("approx-prefix-cache-producer", cfg)
        self.assertNotIn("prefill", cfg)
        self.assertNotIn("decider", cfg)

    def test_epp_image_and_config_group_are_pinned(self) -> None:
        """Lock the picker image and the EndpointPickerConfig API group for the
        unified path too. A deliberate literal (not routing._EPP_IMAGE) so a wrong
        tag/registry or a stale config group is caught in review, not as a
        deploy-time crashloop. Unified has no sidecar, so only the EPP is checked.
        """
        out = self._apply()
        epp = out["epp"].spec.forProvider.manifest["spec"]["template"]["spec"]["containers"]
        self.assertEqual(
            next(c["image"] for c in epp if c["name"] == "epp"),
            "ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0",
        )
        cfg = out["epp-config"].spec.forProvider.manifest["data"]["epp-config.yaml"]
        self.assertIn("apiVersion: llm-d.ai/v1alpha1", cfg)

    def test_epp_pod_carries_config_checksum(self) -> None:
        """The EPP reads its config once at startup, so a config change must roll
        the pod. The pod template carries a sha256 of the rendered config to drive
        that rollout."""
        template = self._apply()["epp"].spec.forProvider.manifest["spec"]["template"]
        checksum = template["metadata"]["annotations"]["modelplane.ai/epp-config-checksum"]
        self.assertEqual(len(checksum), 64)


class TestModelExpressEnv(unittest.TestCase):
    """On a Dynamo cluster the native (Standalone) and Grove (Leader/Worker)
    backends inject the ModelExpress P2P env (MX_SERVER_ADDRESS/MODEL_EXPRESS_URL/
    MX_MODEL_REVISION/MX_P2P_METADATA/POD_*) and the IPC_LOCK security context
    into every engine container of a replica that references a cache. The env is
    inert unless the engine command opts in with --load-format modelexpress. It's
    gated on the cluster's Dynamo stack: on Standard neither backend injects it
    (the portable engine command falls back), and the llm-d backend never does.

    HF_HUB_CACHE is deliberately NOT in this set: it's the cache's own env, on
    every stack (see base.cache_env), and ModelExpress reads it only as a
    fallback for its cache root. Keeping it out here is what makes these
    assertions fail if it ever leaks back into modelexpress_env as a duplicate."""

    _MODELEXPRESS_ENV_NAMES: ClassVar[set[str]] = {
        "MX_SERVER_ADDRESS",
        "MODEL_EXPRESS_URL",
        "MX_MODEL_REVISION",
        "MX_P2P_METADATA",
        "POD_NAME",
        "POD_UID",
        "POD_NAMESPACE",
    }
    # What a cache-referencing engine carries on Dynamo: the cache's env plus
    # the MX bundle, and nothing else.
    _CACHE_ENV_NAME: ClassVar[str] = "HF_HUB_CACHE"

    def _replica(self, *, cache: bool = True, engines: list[v1alpha1.Engine] | None = None) -> v1alpha1.ModelReplica:
        engines = engines if engines is not None else [_standalone_engine(args=[])]
        return v1alpha1.ModelReplica(
            metadata=metav1.ObjectMeta(name="r", namespace="ml-team"),
            spec=v1alpha1.SpecModel(
                clusterName="cluster-a",
                modelCacheRef=v1alpha1.ModelCacheRef(name="qwen") if cache else None,
                engines=engines,
            ),
        )

    def test_grove_gang_gets_modelexpress_env_on_both_cliques(self) -> None:
        engine = _gang_engine(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD)
        replica = self._replica(engines=[engine])
        out = grove.GroveBackend().build(replica, engine, _PC, base.serving_label(replica), "Dynamo")
        manifest = out["model-serving-main"].spec.forProvider.manifest
        # Grove also gets the leader-address alias, unconditional on a cache,
        # ahead of the cache env and the ModelExpress bundle.
        want_env_names = self._MODELEXPRESS_ENV_NAMES | {base.LEADER_ADDRESS_ENV, self._CACHE_ENV_NAME}
        for clique_name in ("leader", "worker"):
            container = _clique(manifest, clique_name)["spec"]["podSpec"]["containers"][0]
            env_names = {e["name"] for e in container["env"]}
            self.assertEqual(env_names, want_env_names, f"{clique_name}: {env_names}")
            self.assertEqual(container["env"][0], base.grove_leader_address_env())
            server_env = next(e for e in container["env"] if e["name"] == "MX_SERVER_ADDRESS")
            # The per-cluster shared server's well-known Service.
            self.assertEqual(server_env["value"], "modelexpress-server:8001")
            mxurl_env = next(e for e in container["env"] if e["name"] == "MODEL_EXPRESS_URL")
            self.assertEqual(mxurl_env["value"], server_env["value"])
            self.assertEqual(container["securityContext"], {"capabilities": {"add": ["IPC_LOCK"]}})

    def test_grove_gang_without_cache_gets_no_modelexpress_env(self) -> None:
        # No cache means no ModelExpress env or security context, but the
        # leader-address alias is unconditional (it doesn't depend on a cache).
        engine = _gang_engine(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD)
        replica = self._replica(cache=False, engines=[engine])
        out = grove.GroveBackend().build(replica, engine, _PC, base.serving_label(replica), "Dynamo")
        manifest = out["model-serving-main"].spec.forProvider.manifest
        for clique_name in ("leader", "worker"):
            container = _clique(manifest, clique_name)["spec"]["podSpec"]["containers"][0]
            self.assertEqual(container["env"], [base.grove_leader_address_env()])
            self.assertNotIn("securityContext", container)

    def test_native_engine_gets_modelexpress_env_on_dynamo(self) -> None:
        # A Standalone engine on a Dynamo cluster with a cache is as valid a P2P
        # peer set as a gang, so it gets the full ModelExpress env and the
        # IPC_LOCK security context on its engine container.
        replica = self._replica()
        out = native.NativeBackend().build(replica, replica.spec.engines[0], _PC, base.serving_label(replica), "Dynamo")
        container = out["model-serving-main"].spec.forProvider.manifest["spec"]["template"]["spec"]["containers"][0]
        env = {e["name"]: e for e in container["env"]}
        self.assertEqual(set(env), self._MODELEXPRESS_ENV_NAMES | {self._CACHE_ENV_NAME})
        self.assertEqual(env["MX_SERVER_ADDRESS"]["value"], "modelexpress-server:8001")
        self.assertEqual(env["MX_P2P_METADATA"]["value"], "1")
        self.assertEqual(env["HF_HUB_CACHE"]["value"], "/mnt/models")
        # Isolates this cache's P2P source identity, qualified by the
        # Modelplane namespace (like cache_pvc_name) so two namespaces' caches
        # of the same name can't collide in the workload cluster's shared
        # `default` namespace.
        self.assertEqual(env["MX_MODEL_REVISION"]["value"], base.cache_pvc_name("ml-team", "qwen"))
        for name, field in (
            ("POD_NAME", "metadata.name"),
            ("POD_UID", "metadata.uid"),
            ("POD_NAMESPACE", "metadata.namespace"),
        ):
            self.assertEqual(env[name]["valueFrom"]["fieldRef"]["fieldPath"], field)
        self.assertEqual(container["securityContext"], {"capabilities": {"add": ["IPC_LOCK"]}})

    def test_native_engine_gets_no_modelexpress_env_on_standard(self) -> None:
        # The same cached Standalone engine on a Standard cluster gets no
        # ModelExpress env and no security context: the portable engine command
        # falls back. It keeps the cache's own HF_HUB_CACHE, which is not part
        # of the ModelExpress bundle and applies on every stack.
        replica = self._replica()
        out = native.NativeBackend().build(
            replica, replica.spec.engines[0], _PC, base.serving_label(replica), "Standard"
        )
        container = out["model-serving-main"].spec.forProvider.manifest["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["env"], [{"name": "HF_HUB_CACHE", "value": "/mnt/models"}])
        self.assertNotIn("securityContext", container)
        self.assertEqual(container["args"], [])


class TestKvBlockSize(unittest.TestCase):
    """The EPP prefix-cache producer's blockSizeTokens is derived best-effort
    from the engine flags (#179) so it matches the engine's KV block size."""

    def test_defaults_to_16_when_absent(self) -> None:
        self.assertEqual(routing._kv_block_size([]), 16)
        self.assertEqual(routing._kv_block_size(["--model=/mnt/models"]), 16)

    def test_reads_vllm_block_size(self) -> None:
        self.assertEqual(routing._kv_block_size(["--block-size", "32"]), 32)
        self.assertEqual(routing._kv_block_size(["--model=/m", "--block-size=8"]), 8)

    def test_reads_sglang_page_size(self) -> None:
        self.assertEqual(routing._kv_block_size(["--page-size=64"]), 64)

    def test_non_integer_falls_back_to_default(self) -> None:
        self.assertEqual(routing._kv_block_size(["--block-size", "auto"]), 16)

    def test_rendered_config_uses_block_size(self) -> None:
        cfg = routing._disaggregated_epp_config_yaml(32)
        self.assertIn("blockSizeTokens: 32", cfg)
        self.assertNotIn("BLOCK_SIZE_TOKENS", cfg)
