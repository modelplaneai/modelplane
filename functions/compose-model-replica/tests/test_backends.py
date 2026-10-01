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
from typing import Any

import pytest
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
        "metadata": {"name": resource.child_name(replica, engine, role, "devices"), "namespace": "mp-ml-team-51733"},
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
        "metadata": {"name": _WORKLOAD_NAME, "namespace": "mp-ml-team-51733"},
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
        "metadata": {"name": _GROVE_PCS_NAME, "namespace": "mp-ml-team-51733"},
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


MANIFESTS_CASES = [
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


@pytest.mark.parametrize("case", MANIFESTS_CASES, ids=lambda case: case.name)
def test_manifests(case: Case) -> None:
    """A backend composes an engine's manifests."""
    replica = _replica(engines=[case.engine])
    out = case.backend.build(replica, case.engine, _PC, base.serving_label(replica), case.stack)
    got = {key: obj.spec.forProvider.manifest for key, obj in out.items()}
    assert got == case.want


def test_leader_address_env_injected_but_not_rank() -> None:
    """The Grove backend injects a leader address alias, but no rank."""
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
        assert container["env"] == [want]


def test_user_env_passed_through() -> None:
    """A Grove member's own env follows the leader address alias."""
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
    assert env == [base.grove_leader_address_env(), {"name": "HF_TOKEN", "value": "x"}]


def test_fieldref_env_passes_through() -> None:
    """A Grove member's pod-field env survives into the composed manifest."""
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
    assert env == [
        base.grove_leader_address_env(),
        {"name": "VLLM_HOST_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}},
    ]


def test_member_metadata_propagates_to_native_pod_template() -> None:
    """A Standalone member's template metadata lands on the Deployment's pod template."""
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
    assert meta["labels"] == {"example.com/role": "standalone", _SERVING: "r", _WORKLOAD: _WORKLOAD_NAME}
    assert meta["annotations"] == {"example.com/config": "standalone"}


def test_member_metadata_propagates_to_cliques_independently() -> None:
    """Each Grove member's template metadata lands on its own clique only."""
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
    assert leader["labels"] == {
        "example.com/role": "leader",
        _SERVING: "r",
        _QUEUE_LABEL: _QUEUE,
        _CLIQUE_ROLE: "leader",
    }
    assert leader["annotations"] == {"example.com/config": "leader"}
    worker = _clique(manifest, "worker")
    assert worker["labels"] == {"example.com/role": "worker", _QUEUE_LABEL: _QUEUE}
    assert worker["annotations"] == {"example.com/config": "worker"}


def test_worker_without_metadata_composes_only_managed_labels() -> None:
    """A Grove worker with no template metadata carries only the queue label."""
    # A worker member with no template.metadata composes a worker clique
    # carrying only the managed queue label and no annotations key.
    engine = _gang_engine(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD)
    replica = _replica(engines=[engine])
    out = grove.GroveBackend().build(replica, engine, _PC, base.serving_label(replica), "Dynamo")
    manifest = out["model-serving-main"].spec.forProvider.manifest
    worker = _clique(manifest, "worker")
    assert worker["labels"] == {_QUEUE_LABEL: _QUEUE}
    assert "annotations" not in worker


def _names(out: dict[str, k8sobjv1alpha1.Object]) -> set[str]:
    """The names of the manifests a backend composed."""
    return {o.spec.forProvider.manifest["metadata"]["name"] for o in out.values()}


def test_co_located_replicas_get_distinct_names() -> None:
    """Two replicas on one cluster compose distinct resource names."""
    # Two replicas of one deployment on the same cluster must produce
    # distinct resource names on the remote cluster.
    a = _replica("dep-clusterA")
    b = _replica("dep-clusterB")
    out_a = native.NativeBackend().build(a, a.spec.engines[0], _PC, base.serving_label(a), "Standard")
    out_b = native.NativeBackend().build(b, b.spec.engines[0], _PC, base.serving_label(b), "Standard")
    assert _names(out_a) & _names(out_b) == set()


def test_multi_engine_qualifies_workload_names() -> None:
    """Each engine of a multi-engine replica composes distinctly named resources."""
    # A replica with two engines names each engine's workload distinctly so
    # they don't collide on the remote cluster.
    engines = [_standalone_engine("prefill"), _standalone_engine("decode")]
    replica = _replica(engines=engines)
    names = set()
    for g in engines:
        out = native.NativeBackend().build(replica, g, _PC, base.serving_label(replica), "Standard")
        names |= _names(out)
    assert len(names) == 4  # 2 deployments + 2 claim templates


@pytest.mark.parametrize(
    ("backend", "engine", "stack", "want_cel"),
    [
        pytest.param(native.NativeBackend(), _standalone_engine(), "Standard", base.AVAILABLE_CEL, id="native"),
        pytest.param(
            grove.GroveBackend(),
            _gang_engine(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD),
            "Dynamo",
            base.GROVE_AVAILABLE_CEL,
            id="grove",
        ),
    ],
)
def test_workload_readiness_policies(backend: base.Backend, engine: v1alpha1.Engine, stack: str, want_cel: str) -> None:
    """A workload's readiness derives from its status, and a claim template's from its creation."""
    # A Deployment reports readiness from its Available condition; a
    # PodCliqueSet publishes no such condition, so it's derived from its
    # replica counters instead (base.GROVE_AVAILABLE_CEL). Either way the
    # claim templates are ready on create.
    replica = _replica(engines=[engine])
    out = backend.build(replica, engine, _PC, base.serving_label(replica), stack)
    serving = out["model-serving-main"].spec.readiness
    assert serving is not None
    assert serving.policy == "DeriveFromCelQuery"
    assert serving.celQuery == want_cel
    for key, obj in out.items():
        if key.startswith("resource-claim"):
            readiness = obj.spec.readiness
            assert readiness is not None
            assert readiness.policy == "SuccessfulCreate"


def test_multiple_device_requests_single_container_claim() -> None:
    """Several device requests compose one container claim and one template carrying them all."""
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
    assert claims == [{"name": "devices"}]
    assert pod["resourceClaims"][0]["name"] == "devices"
    template = out["resource-claim-main-standalone"].spec.forProvider.manifest
    template_requests = template["spec"]["spec"]["devices"]["requests"]
    assert [r["name"] for r in template_requests] == ["gpu", "nic"]
    claim_readiness = out["resource-claim-main-standalone"].spec.readiness
    assert claim_readiness is not None
    assert claim_readiness.policy == "SuccessfulCreate"


def test_claimless_leader_gets_no_claim() -> None:
    """A Grove leader with no device requests composes no claim, but still pins and tolerates."""
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

    assert "resource-claim-main-leader" not in out
    assert "resource-claim-main-worker" in out

    manifest = out["model-serving-main"].spec.forProvider.manifest
    leader = _clique(manifest, "leader")["spec"]["podSpec"]
    assert "resourceClaims" not in leader
    assert "resources" not in leader["containers"][0]
    assert leader["nodeSelector"] == {"modelplane.ai/pool": "frontier"}
    assert leader["tolerations"] == [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]

    worker = _clique(manifest, "worker")["spec"]["podSpec"]
    assert worker["resourceClaims"] == _claims("worker")
    assert worker["containers"][0]["resources"] == {"claims": [{"name": "devices"}]}


def test_members_pin_to_their_own_pools() -> None:
    """Each Grove member's pods pin to that member's own pool."""
    # The scheduler may split a gang across pools when no single pool
    # satisfies every member. Each member's pods must pin to that member's
    # pool, not a shared engine-wide one.
    engine = _gang_engine(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD, leader_pool="head")
    replica = _replica(engines=[engine])
    out = grove.GroveBackend().build(replica, engine, _PC, base.serving_label(replica), "Dynamo")
    manifest = out["model-serving-main"].spec.forProvider.manifest
    assert _clique(manifest, "leader")["spec"]["podSpec"]["nodeSelector"] == {"modelplane.ai/pool": "head"}
    assert _clique(manifest, "worker")["spec"]["podSpec"]["nodeSelector"] == {"modelplane.ai/pool": "frontier"}


# The LeaderWorkerSet backend for a Leader/Worker gang engine.

_LWS_ROLE = "modelplane.ai/lws-role"


def _llmd_lws(engine: v1alpha1.Engine, replica: v1alpha1.ModelReplica) -> dict:
    """The LeaderWorkerSet manifest the llm-d backend composes for engine."""
    out = llmd.LLMDBackend().build(replica, engine, _PC, base.serving_label(replica), "Standard")
    return out["model-serving-main"].spec.forProvider.manifest


def test_llmd_leader_worker_set_shape() -> None:
    """The llm-d backend composes a LeaderWorkerSet of copies gangs, each the leader plus its workers."""
    engine = _gang_engine(nodes=3, copies=2)
    replica = _replica(engines=[engine])
    manifest = _llmd_lws(engine, replica)
    assert manifest["apiVersion"] == "leaderworkerset.x-k8s.io/v1"
    assert manifest["kind"] == "LeaderWorkerSet"
    assert manifest["metadata"] == {"name": _WORKLOAD_NAME, "namespace": "mp-ml-team-51733"}
    assert manifest["spec"]["replicas"] == 2
    # Gang size is the leader plus the worker's node count.
    assert manifest["spec"]["leaderWorkerTemplate"]["size"] == 4


def test_llmd_only_leader_carries_serving_label() -> None:
    """Only the LeaderWorkerSet's leader carries the serving label."""
    engine = _gang_engine()
    replica = _replica(engines=[engine])
    lwt = _llmd_lws(engine, replica)["spec"]["leaderWorkerTemplate"]
    leader_labels = lwt["leaderTemplate"]["metadata"]["labels"]
    assert leader_labels[_SERVING] == "r"
    assert leader_labels[_LWS_ROLE] == "leader"
    # The worker followers never serve, so they carry no metadata at all.
    assert "metadata" not in lwt["workerTemplate"]


def test_llmd_leader_address_and_rank_env_injected() -> None:
    """Every LeaderWorkerSet container leads with the leader address and rank aliases."""
    # Every gang container leads with the backend-neutral coordination vars
    # aliasing LWS_LEADER_ADDRESS / LWS_WORKER_INDEX.
    engine = _gang_engine()
    replica = _replica(engines=[engine])
    lwt = _llmd_lws(engine, replica)["spec"]["leaderWorkerTemplate"]
    for tmpl in (lwt["leaderTemplate"], lwt["workerTemplate"]):
        env = tmpl["spec"]["containers"][0]["env"]
        assert env[0] == {"name": "MODELPLANE_LEADER_ADDRESS", "value": "$(LWS_LEADER_ADDRESS)"}
        assert env[1] == {"name": "MODELPLANE_RANK", "value": "$(LWS_WORKER_INDEX)"}


def test_llmd_no_modelexpress_env_even_with_a_cache() -> None:
    """The llm-d backend injects no ModelExpress env, even for a replica with a cache."""
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
    lwt = _llmd_lws(engine, replica)["spec"]["leaderWorkerTemplate"]
    for tmpl in (lwt["leaderTemplate"], lwt["workerTemplate"]):
        container = tmpl["spec"]["containers"][0]
        env_names = [e["name"] for e in container["env"]]
        # HF_HUB_CACHE is the cache's own env (every stack); the MX bundle
        # is not.
        assert env_names == ["MODELPLANE_LEADER_ADDRESS", "MODELPLANE_RANK", "HF_HUB_CACHE"]
        assert "MX_SERVER_ADDRESS" not in env_names
        assert "securityContext" not in container


def test_llmd_workload_readiness_uses_available_cel() -> None:
    """The LeaderWorkerSet's readiness derives from its Available condition."""
    engine = _gang_engine()
    replica = _replica(engines=[engine])
    out = llmd.LLMDBackend().build(replica, engine, _PC, base.serving_label(replica), "Standard")
    readiness = out["model-serving-main"].spec.readiness
    assert readiness is not None
    assert readiness.policy == "DeriveFromCelQuery"
    assert readiness.celQuery == base.AVAILABLE_CEL


def test_select_backend_standalone_engine_is_native() -> None:
    """A Standalone engine selects the native backend."""
    # A Standalone engine is native regardless of the cluster's stack.
    assert base.select_backend(_standalone_engine(), "Standard") == base.NATIVE
    assert base.select_backend(_standalone_engine(), "Dynamo") == base.NATIVE


def test_select_backend_leader_worker_engine_is_llmd() -> None:
    """A Leader/Worker engine on a Standard cluster selects the llm-d backend."""
    assert base.select_backend(_gang_engine(), "Standard") == base.LLMD


def test_select_backend_leader_worker_engine_is_grove() -> None:
    """A Leader/Worker engine on a Dynamo cluster selects the Grove backend."""
    assert base.select_backend(_gang_engine(), "Dynamo") == base.GROVE


def _cache_replica(
    *, cache: str | None = None, args: list[str] | None = None, command: list[str] | None = None
) -> v1alpha1.ModelReplica:
    """A replica with one Standalone engine, referencing cache if one's given."""
    engine = _standalone_engine(args=args or [], command=command)
    modelcache = v1alpha1.ModelCacheRef(name=cache) if cache else None
    return v1alpha1.ModelReplica(
        metadata=metav1.ObjectMeta(namespace="ml-team"),
        spec=v1alpha1.SpecModel(clusterName="c", modelCacheRef=modelcache, engines=[engine]),
    )


def test_no_cache_no_mounts() -> None:
    """A replica with no cache mounts nothing."""
    volumes, mounts = base.cache_mounts(_cache_replica())
    assert (volumes, mounts) == ([], [])


def test_cache_adds_volume_and_mount() -> None:
    """A replica with a cache mounts the cache's PVC."""
    volumes, mounts = base.cache_mounts(_cache_replica(cache="qwen"))
    assert volumes == [{"name": "model-cache", "persistentVolumeClaim": {"claimName": "modelcache-ml-team-qwen-17db2"}}]
    assert mounts == [{"name": "model-cache", "mountPath": "/mnt/models"}]


def test_cache_env_points_huggingface_at_the_mount() -> None:
    """A replica with a cache points HF_HUB_CACHE at the mount."""
    # The cache is staged in HuggingFace's cache layout, so pointing
    # HF_HUB_CACHE at the mount is what lets an engine's own --model=<repo>
    # resolve against it instead of pulling from HuggingFace (#407).
    assert base.cache_env(_cache_replica(cache="qwen")) == [{"name": "HF_HUB_CACHE", "value": "/mnt/models"}]


def test_cache_env_empty_without_cache() -> None:
    """A replica with no cache gets no cache env."""
    assert base.cache_env(_cache_replica()) == []


def test_cache_env_sets_no_offline_flag() -> None:
    """A replica with a cache doesn't set HF_HUB_OFFLINE."""
    # HF_HUB_OFFLINE would break an engine that fetches a *different* repo
    # at startup (kimi-k2's separately-gated tokenizer), and resolution
    # doesn't need it.
    names = {e["name"] for e in base.cache_env(_cache_replica(cache="qwen"))}
    assert "HF_HUB_OFFLINE" not in names


def _native_cache_replica() -> v1alpha1.ModelReplica:
    """A replica with one Standalone engine, referencing the qwen cache."""
    engine = _standalone_engine(args=[])
    return v1alpha1.ModelReplica(
        metadata=metav1.ObjectMeta(name="r", namespace="ml-team"),
        spec=v1alpha1.SpecModel(
            clusterName="cluster-a",
            modelCacheRef=v1alpha1.ModelCacheRef(name="qwen"),
            engines=[engine],
        ),
    )


def test_native_cache_mounts_pvc_and_sets_cache_env() -> None:
    """The native backend mounts a cache and points HF_HUB_CACHE at it, injecting no --model."""
    # A cache contributes a volume, a mount, and the HF_HUB_CACHE that makes
    # the engine's own --model=<repo> resolve against it. Modelplane injects
    # no --model of its own: naming the model is the command's job.
    replica = _native_cache_replica()
    out = native.NativeBackend().build(replica, replica.spec.engines[0], _PC, base.serving_label(replica), "Standard")
    dep = out["model-serving-main"].spec.forProvider.manifest
    pod = dep["spec"]["template"]["spec"]
    vol_names = {v["name"] for v in pod["volumes"]}
    assert "model-cache" in vol_names
    container = pod["containers"][0]
    assert {"name": "model-cache", "mountPath": "/mnt/models"} in container["volumeMounts"]
    assert {"name": "HF_HUB_CACHE", "value": "/mnt/models"} in container["env"]
    assert container["args"] == []


def test_native_cache_user_env_comes_after_cache_env() -> None:
    """A Standalone member's own env follows the cache env."""
    # Kubernetes expands $(VAR) left to right, so Modelplane's own entries
    # must precede the user's for a user entry to reference them.
    replica = _native_cache_replica()
    engine = replica.spec.engines[0]
    spec = engine.members[0].template.spec
    assert spec is not None
    spec.containers[0].env = [v1alpha1.EnvItem(name="HF_TOKEN", value="x")]
    out = native.NativeBackend().build(replica, engine, _PC, base.serving_label(replica), "Standard")
    container = out["model-serving-main"].spec.forProvider.manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["env"] == [{"name": "HF_HUB_CACHE", "value": "/mnt/models"}, {"name": "HF_TOKEN", "value": "x"}]


def _grove_cache_replica(
    *,
    leader_command: list[str] | None = None,
    worker_command: list[str] | None = None,
    leader_args: list[str] | None = None,
    worker_args: list[str] | None = None,
) -> v1alpha1.ModelReplica:
    """A replica with one Leader/Worker engine, referencing the kimi cache."""
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


def test_grove_cache_both_cliques_mount_cache() -> None:
    """The Grove backend mounts a cache on both cliques."""
    replica = _grove_cache_replica(leader_args=[], worker_command=["/bin/sh", "-c", "join"])
    manifest = (
        grove.GroveBackend()
        .build(replica, replica.spec.engines[0], _PC, base.serving_label(replica), "Dynamo")["model-serving-main"]
        .spec.forProvider.manifest
    )
    for clique_name in ("leader", "worker"):
        pod = _clique(manifest, clique_name)["spec"]["podSpec"]
        assert "model-cache" in {v["name"] for v in pod["volumes"]}
        assert {"name": "model-cache", "mountPath": "/mnt/models"} in pod["containers"][0]["volumeMounts"]


def test_grove_cache_sets_cache_env_on_every_clique_and_injects_no_model() -> None:
    """The Grove backend points both cliques' HF_HUB_CACHE at a cache, injecting no --model."""
    # A cache gives both cliques HF_HUB_CACHE so their own --model=<repo>
    # resolves against the mount; Modelplane adds no --model itself.
    replica = _grove_cache_replica(leader_args=[], worker_command=["/bin/sh", "-c", "join"])
    manifest = (
        grove.GroveBackend()
        .build(replica, replica.spec.engines[0], _PC, base.serving_label(replica), "Dynamo")["model-serving-main"]
        .spec.forProvider.manifest
    )
    for clique_name in ("leader", "worker"):
        container = _clique(manifest, clique_name)["spec"]["podSpec"]["containers"][0]
        assert {"name": "HF_HUB_CACHE", "value": "/mnt/models"} in container["env"]
        assert "--model=/mnt/models" not in container.get("args", [])


def test_grove_cache_command_engine_mounts_cache_without_injecting_model() -> None:
    """A Grove member with its own command mounts a cache and keeps its command verbatim."""
    # A member with its own command keeps it verbatim and gets no injected
    # --model (it points at the cache with its own flag).
    leader_cmd = [
        "/bin/sh",
        "-c",
        "python3 -m sglang.launch_server --model-path /mnt/models --tp 16",
    ]
    replica = _grove_cache_replica(leader_command=leader_cmd, worker_command=["/bin/sh", "-c", "join"])
    manifest = (
        grove.GroveBackend()
        .build(replica, replica.spec.engines[0], _PC, base.serving_label(replica), "Dynamo")["model-serving-main"]
        .spec.forProvider.manifest
    )
    leader = _clique(manifest, "leader")["spec"]["podSpec"]["containers"][0]
    assert {"name": "model-cache", "mountPath": "/mnt/models"} in leader["volumeMounts"]
    assert leader["command"] == leader_cmd


# serving.mode: PrefillDecode routing layers an InferencePool + endpoint
# picker over two engines, role-labels them, and sidecars decode — no unified
# Service. Mirrors how fn.py composes engines then calls routing.apply.


def _disaggregated_apply() -> dict[str, k8sobjv1alpha1.Object]:
    """Routing for a PrefillDecode replica of two native engines."""
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


def _serving_pod(out: dict[str, k8sobjv1alpha1.Object], engine_name: str) -> dict:
    """The pod template of an engine's Deployment."""
    return out[f"model-serving-{engine_name}"].spec.forProvider.manifest["spec"]["template"]


def test_disaggregated_replaces_unified_service_with_pool_and_epp() -> None:
    """PrefillDecode routing fronts the engines with an InferencePool and endpoint picker."""
    out = _disaggregated_apply()
    assert "inference-pool" in out
    assert "epp" in out
    assert "epp-config" in out
    pool = out["inference-pool"].spec.forProvider.manifest
    assert pool["kind"] == "InferencePool"
    assert pool["spec"]["endpointPickerRef"]["name"] == "r-epp"


def test_disaggregated_injects_nixl_plumbing() -> None:
    """Both PrefillDecode engines get the NIXL plumbing the schema can't express."""
    # The plumbing is a Memory /dev/shm and VLLM_NIXL_SIDE_CHANNEL_HOST = pod IP.
    out = _disaggregated_apply()
    for role in ("prefill", "decode"):
        pod = _serving_pod(out, role)["spec"]
        assert any(v.get("emptyDir", {}).get("medium") == "Memory" for v in pod["volumes"]), (
            f"{role} missing Memory /dev/shm volume"
        )
        engine = next(c for c in pod["containers"] if c["name"] == "engine")
        assert "/dev/shm" in [m["mountPath"] for m in engine["volumeMounts"]]
        host = next((e for e in engine["env"] if e["name"] == "VLLM_NIXL_SIDE_CHANNEL_HOST"), None)
        assert host is not None, f"{role} missing VLLM_NIXL_SIDE_CHANNEL_HOST"
        assert host["valueFrom"]["fieldRef"]["fieldPath"] == "status.podIP"
        assert "VLLM_NIXL_SIDE_CHANNEL_PORT" in [e["name"] for e in engine["env"]]


def test_disaggregated_epp_config_arms_the_pd_decider() -> None:
    """PrefillDecode silently serves decode-only unless the PD decider is armed."""
    # Selective prefix-based-pd-decider needs all of: nonCachedTokens > 0 (0 =
    # disabled), the approx-prefix-cache-producer plugin that populates the
    # attribute it reads, and that producer pinned to autoTune: false (the
    # true default never populates). And it must NOT carry the prepareDataPlugins
    # feature gate, which the v0.8.0 EPP image rejects and crashloops on.
    cfg = _disaggregated_apply()["epp-config"].spec.forProvider.manifest["data"]["epp-config.yaml"]
    assert "prefix-based-pd-decider" in cfg
    assert "nonCachedTokens: 16" in cfg
    assert "approx-prefix-cache-producer" in cfg
    assert "autoTune: false" in cfg
    assert "nonCachedTokens: 0" not in cfg
    assert "prepareDataPlugins" not in cfg


def test_disaggregated_epp_and_sidecar_images_and_config_group_are_pinned() -> None:
    """Lock the picker and sidecar images and the EndpointPickerConfig API group."""
    # Nothing else asserts these, so a wrong tag/registry path or a stale config
    # group passes CI and only surfaces as an EPP/sidecar crashloop at deploy.
    # These are deliberate literals, not routing._* constants: comparing to the
    # constant would be tautological (it can't catch a typo in the constant), and
    # a literal forces a bump to show up here and be reviewed.
    out = _disaggregated_apply()
    epp = out["epp"].spec.forProvider.manifest["spec"]["template"]["spec"]["containers"]
    assert next(c["image"] for c in epp if c["name"] == "epp") == "ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0"
    sidecar = next(c for c in _serving_pod(out, "decode")["spec"]["containers"] if c["name"] == "pd-sidecar")
    assert sidecar["image"] == "ghcr.io/llm-d/llm-d-router-disagg-sidecar:v0.9.0"
    cfg = out["epp-config"].spec.forProvider.manifest["data"]["epp-config.yaml"]
    assert "apiVersion: llm-d.ai/v1alpha1" in cfg


def test_disaggregated_epp_role_watches_inferenceobjectives() -> None:
    """The picker watches InferenceObjectives (GIE x-k8s.io group); the Role must allow it."""
    rules = _disaggregated_apply()["epp-role"].spec.forProvider.manifest["rules"]
    assert any(
        "inference.networking.x-k8s.io" in r["apiGroups"] and "inferenceobjectives" in r["resources"] for r in rules
    ), f"EPP Role missing inferenceobjectives watch: {rules}"


def test_disaggregated_decode_port_follows_user_arg() -> None:
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
    containers = _serving_pod(out, "decode")["spec"]["containers"]
    engine = next(c for c in containers if c["name"] == "engine")
    sidecar = next(c for c in containers if c["name"] == "pd-sidecar")
    assert engine["ports"][0]["containerPort"] == 9000
    assert "--vllm-port=9000" in sidecar["args"]
    assert sidecar["ports"][0]["containerPort"] == 8000


def test_disaggregated_engines_role_labeled() -> None:
    """PrefillDecode routing labels each engine's pods with its role."""
    out = _disaggregated_apply()
    assert _serving_pod(out, "prefill")["metadata"]["labels"]["llm-d.ai/role"] == "prefill"
    decode_labels = _serving_pod(out, "decode")["metadata"]["labels"]
    assert decode_labels["llm-d.ai/role"] == "decode"
    assert decode_labels["app"] == "r"


def test_disaggregated_decode_gets_sidecar_and_moves_engine_port() -> None:
    """The decode engine gets the pd-sidecar on the serving port, and moves to another."""
    out = _disaggregated_apply()
    containers = _serving_pod(out, "decode")["spec"]["containers"]
    names = [c["name"] for c in containers]
    assert names == ["engine", "pd-sidecar"]
    engine = next(c for c in containers if c["name"] == "engine")
    assert engine["ports"][0]["containerPort"] == 8001
    assert engine["readinessProbe"]["timeoutSeconds"] == 5
    sidecar = next(c for c in containers if c["name"] == "pd-sidecar")
    assert sidecar["ports"][0]["containerPort"] == 8000
    assert sidecar["readinessProbe"]["timeoutSeconds"] == 5
    assert "--secure-proxy=false" in sidecar["args"]


def test_disaggregated_prefill_has_no_sidecar() -> None:
    """The prefill engine gets no sidecar."""
    containers = _serving_pod(_disaggregated_apply(), "prefill")["spec"]["containers"]
    assert [c["name"] for c in containers] == ["engine"]


def test_disaggregated_route_targets_inference_pool() -> None:
    """PrefillDecode routing points the HTTPRoute at the InferencePool, with no request timeout."""
    route = _disaggregated_apply()[base.ROUTE_KEY].spec.forProvider.manifest
    rule = route["spec"]["rules"][0]
    ref = rule["backendRefs"][0]
    assert ref["kind"] == "InferencePool"
    assert ref["name"] == "r-pool"
    # Disable the request timeout so long token streams aren't severed.
    assert rule["timeouts"]["request"] == "0s"


def test_disaggregated_selects_engines_by_phase_not_name() -> None:
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
    assert [c["name"] for c in _serving_pod(out, "alpha")["spec"]["containers"]] == ["engine", "pd-sidecar"]
    assert [c["name"] for c in _serving_pod(out, "beta")["spec"]["containers"]] == ["engine"]
    assert _serving_pod(out, "alpha")["metadata"]["labels"]["llm-d.ai/role"] == "decode"
    assert _serving_pod(out, "beta")["metadata"]["labels"]["llm-d.ai/role"] == "prefill"


def test_disaggregated_decode_can_be_a_grove_gang() -> None:
    """PrefillDecode routing decorates a Grove decode gang's leader clique, and leaves its worker alone."""
    # A PrefillDecode engine can itself be a Leader/Worker gang, so routing
    # must decorate a Grove PodCliqueSet's leader clique - role label, serving
    # label, pd-sidecar, NIXL plumbing - exactly like a Deployment's pod
    # template. Exercises the _serving_pod_templates normalization that lets
    # one routing layer decorate both workload shapes.
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
    assert leader_clique["labels"]["llm-d.ai/role"] == "decode"
    assert leader_clique["labels"]["app"] == "r"
    leader = leader_clique["spec"]["podSpec"]
    assert [c["name"] for c in leader["containers"]] == ["engine", "pd-sidecar"]
    assert any(v.get("emptyDir", {}).get("medium") == "Memory" for v in leader["volumes"]), (
        "leader clique missing Memory /dev/shm volume for NIXL"
    )
    engine = next(c for c in leader["containers"] if c["name"] == "engine")
    assert "VLLM_NIXL_SIDE_CHANNEL_HOST" in [e["name"] for e in engine["env"]]

    # The worker clique never serves; routing must not touch it at all -
    # its labels stay exactly what the Grove backend composed (just the
    # queue label), with no role or serving label added.
    worker_clique = _clique(manifest, "worker")
    worker = worker_clique["spec"]["podSpec"]
    assert [c["name"] for c in worker["containers"]] == ["engine"]
    assert worker_clique["labels"] == {_QUEUE_LABEL: _QUEUE}


# Unified serving (or no serving block) fronts the pods with an
# InferencePool + endpoint picker in place of a plain Service, so requests
# route by prefix cache and load rather than round-robin - one pod or many.
# Mirrors how fn.py composes engines then calls routing.apply.


def _unified_apply(copies: int = 1) -> dict[str, k8sobjv1alpha1.Object]:
    """Routing for a Unified replica of one native engine."""
    engine = _standalone_engine(copies=copies)
    replica = _replica(engines=[engine])
    composed = native.NativeBackend().build(replica, engine, _PC, base.serving_label(replica), "Standard")
    return routing.apply(composed, replica, _PC)


def test_unified_fronts_with_pool_and_epp() -> None:
    """Unified routing fronts the engine with an InferencePool and endpoint picker."""
    out = _unified_apply()
    assert "inference-pool" in out
    assert "epp" in out
    assert "epp-config" in out
    pool = out["inference-pool"].spec.forProvider.manifest
    assert pool["kind"] == "InferencePool"
    assert pool["spec"]["endpointPickerRef"]["name"] == "r-epp"


@pytest.mark.parametrize("copies", [1, 2])
def test_unified_single_pod_also_pools(copies: int) -> None:
    """A single serving pod gets a pool too, as several do."""
    # A single serving pod has nothing to pick between, but still gets the
    # pool. Always fronting with one avoids swapping a Service for a pool when a
    # second pod appears - a swap that would drop in-flight requests.
    out = _unified_apply(copies=copies)
    assert "inference-pool" in out
    assert "epp" in out


def test_unified_fronts_a_leader_worker_set() -> None:
    """Unified routing fronts a LeaderWorkerSet."""
    # A Standard multi-node engine composes a LeaderWorkerSet, and unified
    # routing must handle that shape too: it reads the engine args for the KV
    # block size through _serving_pod_templates, which has to normalize a
    # LeaderWorkerSet's leaderTemplate alongside a Deployment's pod template
    # and a Grove PodCliqueSet's leader clique. Regression for a shape
    # normalization that only knew Deployment and PodCliqueSet and raised
    # KeyError on a LeaderWorkerSet.
    engine = _gang_engine(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD)
    replica = _replica(engines=[engine])
    composed = llmd.LLMDBackend().build(replica, engine, _PC, base.serving_label(replica), "Standard")
    out = routing.apply(composed, replica, _PC)
    assert "inference-pool" in out
    assert out["model-serving-main"].spec.forProvider.manifest["kind"] == "LeaderWorkerSet"


def test_unified_pool_selects_pods_by_the_serving_label() -> None:
    """The pool selects the pods by the serving label they already carry, so no relabeling is needed."""
    pool = _unified_apply()["inference-pool"].spec.forProvider.manifest
    assert pool["spec"]["selector"]["matchLabels"] == {base.LABEL_SERVING: "r"}


def test_unified_route_targets_inference_pool() -> None:
    """Unified routing points the HTTPRoute at the InferencePool."""
    route = _unified_apply()[base.ROUTE_KEY].spec.forProvider.manifest
    ref = route["spec"]["rules"][0]["backendRefs"][0]
    assert ref["kind"] == "InferencePool"
    assert ref["name"] == "r-pool"


def test_unified_epp_config_is_unified_not_disaggregated() -> None:
    """The unified picker scores by prefix cache and queue depth, with no prefill/decode split."""
    # It scores in a single profile, and still needs the
    # approx-prefix-cache-producer that feeds the prefix-cache scorer.
    cfg = _unified_apply()["epp-config"].spec.forProvider.manifest["data"]["epp-config.yaml"]
    assert "prefix-cache-scorer" in cfg
    assert "queue-scorer" in cfg
    assert "approx-prefix-cache-producer" in cfg
    assert "prefill" not in cfg
    assert "decider" not in cfg


def test_unified_epp_image_and_config_group_are_pinned() -> None:
    """Lock the picker image and the EndpointPickerConfig API group for the unified path too."""
    # A deliberate literal (not routing._EPP_IMAGE) so a wrong
    # tag/registry or a stale config group is caught in review, not as a
    # deploy-time crashloop. Unified has no sidecar, so only the EPP is checked.
    out = _unified_apply()
    epp = out["epp"].spec.forProvider.manifest["spec"]["template"]["spec"]["containers"]
    assert next(c["image"] for c in epp if c["name"] == "epp") == "ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0"
    cfg = out["epp-config"].spec.forProvider.manifest["data"]["epp-config.yaml"]
    assert "apiVersion: llm-d.ai/v1alpha1" in cfg


def test_unified_epp_pod_carries_config_checksum() -> None:
    """The EPP pod template carries a sha256 of its config, so a config change rolls the pod."""
    # The EPP reads its config once at startup, so a config change must roll
    # the pod. The pod template carries a sha256 of the rendered config to drive
    # that rollout.
    template = _unified_apply()["epp"].spec.forProvider.manifest["spec"]["template"]
    checksum = template["metadata"]["annotations"]["modelplane.ai/epp-config-checksum"]
    assert len(checksum) == 64


# On a Dynamo cluster the native (Standalone) and Grove (Leader/Worker)
# backends inject the ModelExpress P2P env (MX_SERVER_ADDRESS/MODEL_EXPRESS_URL/
# MX_MODEL_REVISION/MX_P2P_METADATA/POD_*) and the IPC_LOCK security context
# into every engine container of a replica that references a cache. The env is
# inert unless the engine command opts in with --load-format modelexpress. It's
# gated on the cluster's Dynamo stack: on Standard neither backend injects it
# (the portable engine command falls back), and the llm-d backend never does.
#
# HF_HUB_CACHE is deliberately NOT in this set: it's the cache's own env, on
# every stack (see base.cache_env), and ModelExpress reads it only as a
# fallback for its cache root. Keeping it out here is what makes these
# assertions fail if it ever leaks back into modelexpress_env as a duplicate.

_MODELEXPRESS_ENV_NAMES = {
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
_CACHE_ENV_NAME = "HF_HUB_CACHE"


def _modelexpress_replica(*, cache: bool = True, engines: list[v1alpha1.Engine] | None = None) -> v1alpha1.ModelReplica:
    """A replica of engines, referencing the qwen cache unless cache is False."""
    engines = engines if engines is not None else [_standalone_engine(args=[])]
    return v1alpha1.ModelReplica(
        metadata=metav1.ObjectMeta(name="r", namespace="ml-team"),
        spec=v1alpha1.SpecModel(
            clusterName="cluster-a",
            modelCacheRef=v1alpha1.ModelCacheRef(name="qwen") if cache else None,
            engines=engines,
        ),
    )


def test_grove_gang_gets_modelexpress_env_on_both_cliques() -> None:
    """A cached Grove gang on Dynamo gets the ModelExpress env and IPC_LOCK on both cliques."""
    engine = _gang_engine(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD)
    replica = _modelexpress_replica(engines=[engine])
    out = grove.GroveBackend().build(replica, engine, _PC, base.serving_label(replica), "Dynamo")
    manifest = out["model-serving-main"].spec.forProvider.manifest
    # Grove also gets the leader-address alias, unconditional on a cache,
    # ahead of the cache env and the ModelExpress bundle.
    want_env_names = _MODELEXPRESS_ENV_NAMES | {base.LEADER_ADDRESS_ENV, _CACHE_ENV_NAME}
    for clique_name in ("leader", "worker"):
        container = _clique(manifest, clique_name)["spec"]["podSpec"]["containers"][0]
        env_names = {e["name"] for e in container["env"]}
        assert env_names == want_env_names, f"{clique_name}: {env_names}"
        assert container["env"][0] == base.grove_leader_address_env()
        server_env = next(e for e in container["env"] if e["name"] == "MX_SERVER_ADDRESS")
        # The per-cluster shared server's well-known Service, qualified by
        # its namespace because the engine runs in its team's namespace.
        assert server_env["value"] == "modelexpress-server.default.svc:8001"
        mxurl_env = next(e for e in container["env"] if e["name"] == "MODEL_EXPRESS_URL")
        assert mxurl_env["value"] == server_env["value"]
        assert container["securityContext"] == {"capabilities": {"add": ["IPC_LOCK"]}}


def test_grove_gang_without_cache_gets_no_modelexpress_env() -> None:
    """A Grove gang with no cache gets only the leader address alias, and no security context."""
    # No cache means no ModelExpress env or security context, but the
    # leader-address alias is unconditional (it doesn't depend on a cache).
    engine = _gang_engine(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD)
    replica = _modelexpress_replica(cache=False, engines=[engine])
    out = grove.GroveBackend().build(replica, engine, _PC, base.serving_label(replica), "Dynamo")
    manifest = out["model-serving-main"].spec.forProvider.manifest
    for clique_name in ("leader", "worker"):
        container = _clique(manifest, clique_name)["spec"]["podSpec"]["containers"][0]
        assert container["env"] == [base.grove_leader_address_env()]
        assert "securityContext" not in container


def test_native_engine_gets_modelexpress_env_on_dynamo() -> None:
    """A cached Standalone engine on Dynamo gets the ModelExpress env and IPC_LOCK."""
    # A Standalone engine on a Dynamo cluster with a cache is as valid a P2P
    # peer set as a gang, so it gets the full ModelExpress env and the
    # IPC_LOCK security context on its engine container.
    replica = _modelexpress_replica()
    out = native.NativeBackend().build(replica, replica.spec.engines[0], _PC, base.serving_label(replica), "Dynamo")
    container = out["model-serving-main"].spec.forProvider.manifest["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e for e in container["env"]}
    assert set(env) == _MODELEXPRESS_ENV_NAMES | {_CACHE_ENV_NAME}
    assert env["MX_SERVER_ADDRESS"]["value"] == "modelexpress-server.default.svc:8001"
    assert env["MX_P2P_METADATA"]["value"] == "1"
    assert env["HF_HUB_CACHE"]["value"] == "/mnt/models"
    # Isolates this cache's P2P source identity, qualified by the
    # Modelplane namespace (like cache_pvc_name) so two namespaces' caches
    # of the same name can't collide at the cluster's one shared server.
    assert env["MX_MODEL_REVISION"]["value"] == base.cache_pvc_name("ml-team", "qwen")
    for name, field in (
        ("POD_NAME", "metadata.name"),
        ("POD_UID", "metadata.uid"),
        ("POD_NAMESPACE", "metadata.namespace"),
    ):
        assert env[name]["valueFrom"]["fieldRef"]["fieldPath"] == field
    assert container["securityContext"] == {"capabilities": {"add": ["IPC_LOCK"]}}


def test_native_engine_gets_no_modelexpress_env_on_standard() -> None:
    """A cached Standalone engine on Standard gets only the cache env, and no security context."""
    # The same cached Standalone engine on a Standard cluster gets no
    # ModelExpress env and no security context: the portable engine command
    # falls back. It keeps the cache's own HF_HUB_CACHE, which is not part
    # of the ModelExpress bundle and applies on every stack.
    replica = _modelexpress_replica()
    out = native.NativeBackend().build(replica, replica.spec.engines[0], _PC, base.serving_label(replica), "Standard")
    container = out["model-serving-main"].spec.forProvider.manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["env"] == [{"name": "HF_HUB_CACHE", "value": "/mnt/models"}]
    assert "securityContext" not in container
    assert container["args"] == []


# The EPP prefix-cache producer's blockSizeTokens is derived best-effort
# from the engine flags (#179) so it matches the engine's KV block size.


def test_kv_block_size_defaults_to_16_when_absent() -> None:
    """The KV block size defaults to 16 when no flag sets it."""
    assert routing._kv_block_size([]) == 16
    assert routing._kv_block_size(["--model=/mnt/models"]) == 16


def test_kv_block_size_reads_vllm_block_size() -> None:
    """The KV block size comes from vLLM's --block-size."""
    assert routing._kv_block_size(["--block-size", "32"]) == 32
    assert routing._kv_block_size(["--model=/m", "--block-size=8"]) == 8


def test_kv_block_size_reads_sglang_page_size() -> None:
    """The KV block size comes from SGLang's --page-size."""
    assert routing._kv_block_size(["--page-size=64"]) == 64


def test_kv_block_size_non_integer_falls_back_to_default() -> None:
    """A non-integer block size falls back to 16."""
    assert routing._kv_block_size(["--block-size", "auto"]) == 16


def test_kv_block_size_rendered_config_uses_block_size() -> None:
    """The rendered EPP config carries the block size in place of its placeholder."""
    cfg = routing._disaggregated_epp_config_yaml(32)
    assert "blockSizeTokens: 32" in cfg
    assert "BLOCK_SIZE_TOKENS" not in cfg


# The mirrored namespace a replica's objects land in. The expected names are
# spelled out, because compose-inference-cluster creates the namespace and
# compose-model-route and compose-model-cache land objects in it by the same
# derivation, and all four must agree.

REMOTE_NAMESPACE_CASES = [
    pytest.param("ml-team", "mp-ml-team-51733", id="a short namespace keeps its name, prefixed and hashed"),
    pytest.param(
        # 63 is the longest a namespace can be, so mp- plus it can't be
        # used as is. It's truncated to leave room for the hash.
        "a" * 63,
        "mp-" + "a" * 54 + "-38bfb",
        id="the longest valid namespace still yields a valid one",
    ),
]


@pytest.mark.parametrize(("namespace", "want"), REMOTE_NAMESPACE_CASES)
def test_remote_namespace(namespace: str, want: str) -> None:
    """A replica's objects land in a namespace mirroring its own."""
    got = base.remote_namespace(_replica(namespace=namespace))
    assert got == want
    assert len(got) <= 63
