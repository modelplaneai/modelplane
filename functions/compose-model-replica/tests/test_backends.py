"""Tests for compose-model-replica backends.

A backend builds the workload (Deployment or LeaderWorkerSet) and the
ResourceClaimTemplates for one worker engine; the shared Service and HTTPRoute
that front a replica's engines are built by base.serving_resources. Manifests are
asserted with a `Case` table: each case builds an engine's backend and compares
the composed manifests to a full `want`. Backend selection, serving, and the
Dynamo stub are dispatch/behaviour tests below the table.
"""

import dataclasses
import unittest

from crossplane.function import resource
from function import routing
from function.backends import base, dynamo, llmd, native
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

_SERVING = "modelplane.ai/serving"
_WORKLOAD = "modelplane.ai/workload"
_ROLE = "modelplane.ai/lws-role"
_LEADER_ENV = {"name": "MODELPLANE_LEADER_ADDRESS", "value": "$(LWS_LEADER_ADDRESS)"}

# A GPU device request (claim: DRA), as compose-model-deployment stamps it.
_GPU_CEL = 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'


def _gpu_request(count):
    return v1alpha1.DeviceRequest(
        name="gpu",
        deviceClassName="gpu.nvidia.com",
        count=count,
        selectors=[v1alpha1.Selector(cel=_GPU_CEL)],
    )


def _standalone_engine(
    name="main",
    *,
    copies=1,
    args=None,
    command=None,
    device_requests=None,
):
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
    name="main",
    *,
    copies=1,
    nodes=1,
    leader_args=None,
    leader_command=None,
    worker_args=None,
    worker_command=None,
    leader_device_requests=None,
    leader_pool="frontier",
):
    """A Leader + Worker engine.

    The members carry their own pool pins and device requests, defaulting to a
    homogeneous gang on one pool. leader_device_requests=[] makes the leader
    claimless (a coordinator-only leader); leader_pool moves it to another
    pool.
    """

    def member(role, nodes, args, command, device_requests, pool):
        container = v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")
        if args is not None:
            container.args = args
        if command is not None:
            container.command = command
        kwargs = {
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


def _replica(name="r", *, namespace="ml-team", engines=None):
    if engines is None:
        engines = [_standalone_engine()]
    return v1alpha1.ModelReplica(
        metadata=metav1.ObjectMeta(name=name, namespace=namespace),
        spec=v1alpha1.SpecModel(clusterName="cluster-a", engines=engines),
    )


# The composed workload name for the default replica "r" / engine "main".
# Always engine-qualified, and so always distinct from the replica name the
# serving Service uses - see base.engine_name on why that matters for LWS.
_WORKLOAD_NAME = resource.child_name("r", "main")


def _claim_template(count, *, replica="r", engine="main", role="standalone"):
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


def _route(name):
    """The replica's HTTPRoute — replica-named, prefix-stripped."""
    return {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {"name": name, "namespace": "default"},
        "spec": {
            "parentRefs": [{"name": "inference-gateway", "namespace": "modelplane-system"}],
            "rules": [
                {
                    "matches": [{"path": {"type": "PathPrefix", "value": f"/ml-team/{name}/"}}],
                    "timeouts": {"request": "0s"},
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


def _service(name):
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": "default"},
        "spec": {"selector": {_SERVING: name}, "ports": [{"port": 80, "targetPort": 8000}]},
    }


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


def _claims(role):
    """The pod-level claim referencing a member's ResourceClaimTemplate."""
    return [
        {
            "name": "devices",
            "resourceClaimTemplateName": resource.child_name("r", "main", role, "devices"),
        }
    ]


def _lws(leader_container, worker_container):
    node_selector = {"modelplane.ai/pool": "frontier"}

    tolerations = [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]
    return {
        "apiVersion": "leaderworkerset.x-k8s.io/v1",
        "kind": "LeaderWorkerSet",
        "metadata": {"name": _WORKLOAD_NAME, "namespace": "default"},
        "spec": {
            "replicas": 1,
            "leaderWorkerTemplate": {
                "size": 2,
                "leaderTemplate": {
                    "metadata": {"labels": {_SERVING: "r", _ROLE: "leader"}},
                    "spec": {
                        "containers": [leader_container],
                        "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                        "nodeSelector": node_selector,
                        "resourceClaims": _claims("leader"),
                        "tolerations": tolerations,
                    },
                },
                "workerTemplate": {
                    "spec": {
                        "containers": [worker_container],
                        "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                        "nodeSelector": node_selector,
                        "resourceClaims": _claims("worker"),
                        "tolerations": tolerations,
                    },
                },
            },
        },
    }


def _engine(*, serving, args=None, command=None, env=None):
    c = {
        "name": "engine",
        "image": "vllm/vllm-openai:latest",
        "resources": {"claims": [{"name": "devices"}]},
        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
    }
    if command is not None:
        c["command"] = command
    if args is not None:
        c["args"] = args
    c["env"] = env if env is not None else [_LEADER_ENV]
    if serving:
        c["ports"] = [{"containerPort": 8000}]
        c["readinessProbe"] = {
            "httpGet": {"path": "/health", "port": 8000},
            "initialDelaySeconds": 30,
            "periodSeconds": 10,
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
_LLMD_WANT = {
    "model-serving-main": _lws(
        _engine(serving=True, command=_LEADER_CMD),
        _engine(serving=False, command=_WORKER_CMD),
    ),
    "resource-claim-main-leader": _claim_template(8, role="leader"),
    "resource-claim-main-worker": _claim_template(8, role="worker"),
}


@dataclasses.dataclass
class Case:
    name: str
    backend: object
    engine: v1alpha1.Worker
    want: dict


_CASES = [
    Case(
        name="native Standalone engine composes a Deployment",
        backend=native.NativeBackend(),
        engine=_standalone_engine(),
        want=_NATIVE_WANT,
    ),
    Case(
        name="llm-d Leader/Worker engine composes a LeaderWorkerSet, commands verbatim",
        backend=llmd.LLMDBackend(),
        engine=_gang_engine(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD),
        want=_LLMD_WANT,
    ),
]


class TestBackendManifests(unittest.TestCase):
    def test_manifests(self):
        for case in _CASES:
            with self.subTest(case.name):
                replica = _replica(engines=[case.engine])
                out = case.backend.build(replica, case.engine, _PC, base.serving_label(replica))
                got = {key: obj.spec.forProvider.manifest for key, obj in out.items()}
                self.assertEqual(case.want, got, "-want, +got")

    def test_serving_resources(self):
        # The shared Service + HTTPRoute front a replica regardless of how many
        # engines it has, named after the replica.
        replica = _replica()
        out = base.serving_resources(replica, _PC)
        got = {key: obj.spec.forProvider.manifest for key, obj in out.items()}
        self.assertEqual({"model-service": _service("r"), "model-route": _route("r")}, got)

    def test_leader_address_injected_into_gang_engines(self):
        # Every engine container in a multi-node gang gets
        # MODELPLANE_LEADER_ADDRESS, aliasing LWS_LEADER_ADDRESS, ahead of the
        # user's own env so commands can reference $(MODELPLANE_LEADER_ADDRESS).
        engine = _gang_engine(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD)
        replica = _replica(engines=[engine])
        out = llmd.LLMDBackend().build(replica, engine, _PC, base.serving_label(replica))
        tmpl = out["model-serving-main"].spec.forProvider.manifest["spec"]["leaderWorkerTemplate"]
        for role in ("leaderTemplate", "workerTemplate"):
            env = tmpl[role]["spec"]["containers"][0]["env"]
            self.assertEqual(env[0], _LEADER_ENV)

    def test_user_env_preserved_after_leader_address(self):
        engine = _gang_engine(
            leader_command=_LEADER_CMD,
            worker_command=_WORKER_CMD,
        )
        engine.members[0].template.spec.containers[0].env = [v1alpha1.EnvItem(name="HF_TOKEN", value="x")]
        replica = _replica(engines=[engine])
        out = llmd.LLMDBackend().build(replica, engine, _PC, base.serving_label(replica))
        leader = out["model-serving-main"].spec.forProvider.manifest["spec"]["leaderWorkerTemplate"]["leaderTemplate"]
        env = leader["spec"]["containers"][0]["env"]
        self.assertEqual(env, [_LEADER_ENV, {"name": "HF_TOKEN", "value": "x"}])

    @staticmethod
    def _names(out):
        return {o.spec.forProvider.manifest["metadata"]["name"] for o in out.values()}

    def test_co_located_replicas_get_distinct_names(self):
        # Two replicas of one deployment on the same cluster must produce
        # distinct resource names on the remote cluster.
        a = _replica("dep-clusterA")
        b = _replica("dep-clusterB")
        out_a = native.NativeBackend().build(a, a.spec.engines[0], _PC, base.serving_label(a))
        out_b = native.NativeBackend().build(b, b.spec.engines[0], _PC, base.serving_label(b))
        self.assertEqual(self._names(out_a) & self._names(out_b), set())

    def test_lws_name_differs_from_serving_service_name(self):
        # Regression: LWS's controller creates a headless Service named after
        # the LWS for gang pod DNS - but only if no Service of that name
        # exists. When the LWS shared the serving Service's name (the replica
        # name), that headless Service was never created, the followers could
        # never resolve the leader, and the gang deadlocked. The workload name
        # must differ from the Service's.
        engine = _gang_engine(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD)
        replica = _replica(engines=[engine])
        workload = llmd.LLMDBackend().build(replica, engine, _PC, base.serving_label(replica))
        serving = base.serving_resources(replica, _PC)
        lws_name = workload["model-serving-main"].spec.forProvider.manifest["metadata"]["name"]
        service_name = serving["model-service"].spec.forProvider.manifest["metadata"]["name"]
        self.assertNotEqual(lws_name, service_name)

    def test_multi_engine_qualifies_workload_names(self):
        # A replica with two engines names each engine's workload distinctly so
        # they don't collide on the remote cluster.
        engines = [_standalone_engine("prefill"), _standalone_engine("decode")]
        replica = _replica(engines=engines)
        names = set()
        for g in engines:
            out = native.NativeBackend().build(replica, g, _PC, base.serving_label(replica))
            names |= self._names(out)
        self.assertEqual(len(names), 4)  # 2 deployments + 2 claim templates

    def test_workload_readiness_policies(self):
        # The workload reports readiness from its Available condition via a CEL
        # query; the claim templates are ready on create.
        for name, backend, engine in (
            ("native", native.NativeBackend(), _standalone_engine()),
            ("llm-d", llmd.LLMDBackend(), _gang_engine(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD)),
        ):
            with self.subTest(name):
                replica = _replica(engines=[engine])
                out = backend.build(replica, engine, _PC, base.serving_label(replica))
                serving = out["model-serving-main"].spec.readiness
                self.assertEqual(serving.policy, "DeriveFromCelQuery")
                self.assertEqual(serving.celQuery, base.AVAILABLE_CEL)
                for key, obj in out.items():
                    if key.startswith("resource-claim"):
                        self.assertEqual(obj.spec.readiness.policy, "SuccessfulCreate")

    def test_serving_readiness_policies(self):
        replica = _replica()
        out = base.serving_resources(replica, _PC)
        self.assertEqual(out["model-service"].spec.readiness.policy, "SuccessfulCreate")
        self.assertEqual(out["model-route"].spec.readiness.policy, "SuccessfulCreate")

    def test_multiple_device_requests_single_container_claim(self):
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
        out = native.NativeBackend().build(replica, engine, _PC, base.serving_label(replica))
        pod = out["model-serving-main"].spec.forProvider.manifest["spec"]["template"]["spec"]
        claims = pod["containers"][0]["resources"]["claims"]
        self.assertEqual(claims, [{"name": "devices"}])
        self.assertEqual(pod["resourceClaims"][0]["name"], "devices")
        template = out["resource-claim-main-standalone"].spec.forProvider.manifest
        template_requests = template["spec"]["spec"]["devices"]["requests"]
        self.assertEqual([r["name"] for r in template_requests], ["gpu", "nic"])
        self.assertEqual(out["resource-claim-main-standalone"].spec.readiness.policy, "SuccessfulCreate")

    def test_claimless_leader_gets_no_claim(self):
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
        out = llmd.LLMDBackend().build(replica, engine, _PC, base.serving_label(replica))

        self.assertNotIn("resource-claim-main-leader", out)
        self.assertIn("resource-claim-main-worker", out)

        tmpl = out["model-serving-main"].spec.forProvider.manifest["spec"]["leaderWorkerTemplate"]
        leader = tmpl["leaderTemplate"]["spec"]
        self.assertNotIn("resourceClaims", leader)
        self.assertNotIn("resources", leader["containers"][0])
        self.assertEqual(leader["nodeSelector"], {"modelplane.ai/pool": "frontier"})
        self.assertEqual(
            leader["tolerations"], [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]
        )

        worker = tmpl["workerTemplate"]["spec"]
        self.assertEqual(worker["resourceClaims"], _claims("worker"))
        self.assertEqual(worker["containers"][0]["resources"], {"claims": [{"name": "devices"}]})

    def test_members_pin_to_their_own_pools(self):
        # The scheduler may split a gang across pools when no single pool
        # satisfies every member. Each member's pods must pin to that member's
        # pool, not a shared engine-wide one.
        engine = _gang_engine(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD, leader_pool="head")
        replica = _replica(engines=[engine])
        out = llmd.LLMDBackend().build(replica, engine, _PC, base.serving_label(replica))
        tmpl = out["model-serving-main"].spec.forProvider.manifest["spec"]["leaderWorkerTemplate"]
        self.assertEqual(tmpl["leaderTemplate"]["spec"]["nodeSelector"], {"modelplane.ai/pool": "head"})
        self.assertEqual(tmpl["workerTemplate"]["spec"]["nodeSelector"], {"modelplane.ai/pool": "frontier"})


class TestBackendSelection(unittest.TestCase):
    def test_standalone_engine_is_native(self):
        self.assertEqual(base.select_backend(_standalone_engine()), base.NATIVE)

    def test_leader_worker_engine_is_llmd(self):
        self.assertEqual(base.select_backend(_gang_engine()), base.LLMD)


class TestDynamoStub(unittest.TestCase):
    def test_not_selected_in_v01(self):
        self.assertNotEqual(base.select_backend(_gang_engine()), base.DYNAMO)

    def test_build_raises(self):
        engine = _gang_engine()
        replica = _replica(engines=[engine])
        with self.assertRaises(NotImplementedError):
            dynamo.DynamoBackend().build(replica, engine, _PC, base.serving_label(replica))


class TestCacheMounts(unittest.TestCase):
    def _replica(self, *, cache=None, args=None, command=None):
        engine = _standalone_engine(args=args or [], command=command)
        modelcache = v1alpha1.ModelCacheRef(name=cache) if cache else None
        return v1alpha1.ModelReplica(
            metadata=metav1.ObjectMeta(namespace="ml-team"),
            spec=v1alpha1.SpecModel(clusterName="c", modelCacheRef=modelcache, engines=[engine]),
        )

    @staticmethod
    def _engine(replica):
        return replica.spec.engines[0].members[0].template.spec.containers[0]

    def test_no_cache_no_mounts(self):
        volumes, mounts = base.cache_mounts(self._replica())
        self.assertEqual((volumes, mounts), ([], []))

    def test_cache_adds_volume_and_mount(self):
        volumes, mounts = base.cache_mounts(self._replica(cache="qwen"))
        self.assertEqual(
            volumes,
            [{"name": "model-cache", "persistentVolumeClaim": {"claimName": "modelcache-ml-team-qwen-17db2"}}],
        )
        self.assertEqual(mounts, [{"name": "model-cache", "mountPath": "/mnt/models"}])

    def test_apply_cache_injects_model_when_absent(self):
        r = self._replica(cache="qwen")
        args = base.apply_cache_args(["--trust-remote-code"], r, self._engine(r))
        self.assertIn("--model=/mnt/models", args)

    def test_apply_cache_respects_user_model(self):
        r = self._replica(cache="qwen", args=["--model=/mnt/models"])
        args = base.apply_cache_args(["--model=/mnt/models"], r, self._engine(r))
        self.assertEqual(args.count("--model=/mnt/models"), 1)

    def test_apply_cache_noop_without_cache(self):
        r = self._replica()
        args = base.apply_cache_args(["--trust-remote-code"], r, self._engine(r))
        self.assertEqual(args, ["--trust-remote-code"])

    def test_apply_cache_skips_when_engine_has_command(self):
        # Non-vLLM engine (e.g. SGLang) owns its args via a command and uses
        # --model-path, not --model: we must not inject --model.
        r = self._replica(cache="qwen", args=["--model-path=/mnt/models"], command=["/bin/sh", "-c", "..."])
        args = base.apply_cache_args(["--model-path=/mnt/models"], r, self._engine(r))
        self.assertNotIn("--model=/mnt/models", args)
        self.assertEqual(args, ["--model-path=/mnt/models"])


class TestNativeBackendCache(unittest.TestCase):
    def _replica(self):
        engine = _standalone_engine(args=[])
        return v1alpha1.ModelReplica(
            metadata=metav1.ObjectMeta(name="r", namespace="ml-team"),
            spec=v1alpha1.SpecModel(
                clusterName="cluster-a",
                modelCacheRef=v1alpha1.ModelCacheRef(name="qwen"),
                engines=[engine],
            ),
        )

    def test_mounts_pvc_and_injects_model(self):
        replica = self._replica()
        out = native.NativeBackend().build(replica, replica.spec.engines[0], _PC, base.serving_label(replica))
        dep = out["model-serving-main"].spec.forProvider.manifest
        pod = dep["spec"]["template"]["spec"]
        vol_names = {v["name"] for v in pod["volumes"]}
        self.assertIn("model-cache", vol_names)
        container = pod["containers"][0]
        self.assertIn({"name": "model-cache", "mountPath": "/mnt/models"}, container["volumeMounts"])
        self.assertIn("--model=/mnt/models", container["args"])


class TestLLMDBackendCache(unittest.TestCase):
    def _replica(self, *, leader_command=None, worker_command=None, leader_args=None, worker_args=None):
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

    def test_both_lws_templates_mount_cache(self):
        replica = self._replica(leader_args=[], worker_command=["/bin/sh", "-c", "join"])
        lws = (
            llmd.LLMDBackend()
            .build(replica, replica.spec.engines[0], _PC, base.serving_label(replica))["model-serving-main"]
            .spec.forProvider.manifest
        )
        tmpl = lws["spec"]["leaderWorkerTemplate"]
        for role in ("leaderTemplate", "workerTemplate"):
            pod = tmpl[role]["spec"]
            self.assertIn("model-cache", {v["name"] for v in pod["volumes"]})
            self.assertIn(
                {"name": "model-cache", "mountPath": "/mnt/models"},
                pod["containers"][0]["volumeMounts"],
            )

    def test_injects_model_into_leader_args_for_vllm(self):
        # The leader has no command and no --model arg, so the cache --model is
        # injected into its args.
        replica = self._replica(leader_args=[], worker_command=["/bin/sh", "-c", "join"])
        lws = (
            llmd.LLMDBackend()
            .build(replica, replica.spec.engines[0], _PC, base.serving_label(replica))["model-serving-main"]
            .spec.forProvider.manifest
        )
        leader_args = lws["spec"]["leaderWorkerTemplate"]["leaderTemplate"]["spec"]["containers"][0]["args"]
        self.assertIn("--model=/mnt/models", leader_args)

    def test_command_engine_mounts_cache_without_injecting_model(self):
        # A member with its own command keeps it verbatim and gets no injected
        # --model (it points at the cache with its own flag).
        leader_cmd = [
            "/bin/sh",
            "-c",
            "python3 -m sglang.launch_server --model-path /mnt/models --tp 16",
        ]
        replica = self._replica(leader_command=leader_cmd, worker_command=["/bin/sh", "-c", "join"])
        lws = (
            llmd.LLMDBackend()
            .build(replica, replica.spec.engines[0], _PC, base.serving_label(replica))["model-serving-main"]
            .spec.forProvider.manifest
        )
        leader = lws["spec"]["leaderWorkerTemplate"]["leaderTemplate"]["spec"]["containers"][0]
        self.assertIn(
            {"name": "model-cache", "mountPath": "/mnt/models"},
            leader["volumeMounts"],
        )
        self.assertEqual(leader["command"], leader_cmd)


class TestDisaggregated(unittest.TestCase):
    """serving.mode: PrefillDecode routing layers an InferencePool + endpoint
    picker over two engines, role-labels them, and sidecars decode — no unified
    Service. Mirrors how fn.py composes engines then calls routing.apply."""

    def _apply(self):
        prefill = _standalone_engine(name="prefill")
        prefill.phase = "Prefill"
        decode = _standalone_engine(name="decode")
        decode.phase = "Decode"
        replica = _replica(engines=[prefill, decode])
        replica.spec.serving = v1alpha1.Serving(mode="PrefillDecode")
        composed = {}
        for engine in replica.spec.engines:
            composed.update(native.NativeBackend().build(replica, engine, _PC, base.serving_label(replica)))
        return routing.apply(composed, replica, _PC)

    def _serving_pod(self, out, engine_name):
        return out[f"model-serving-{engine_name}"].spec.forProvider.manifest["spec"]["template"]

    def test_replaces_unified_service_with_pool_and_epp(self):
        out = self._apply()
        self.assertNotIn(base.SERVICE_KEY, out)  # unified Service gone
        self.assertIn("inference-pool", out)
        self.assertIn("epp", out)
        self.assertIn("epp-config", out)
        pool = out["inference-pool"].spec.forProvider.manifest
        self.assertEqual(pool["kind"], "InferencePool")
        self.assertEqual(pool["spec"]["endpointPickerRef"]["name"], "r-epp")

    def test_injects_nixl_plumbing(self):
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
            self.assertIsNotNone(host, f"{role} missing VLLM_NIXL_SIDE_CHANNEL_HOST")
            self.assertEqual(host["valueFrom"]["fieldRef"]["fieldPath"], "status.podIP")
            self.assertIn("VLLM_NIXL_SIDE_CHANNEL_PORT", [e["name"] for e in engine["env"]])

    def test_epp_config_arms_the_pd_decider(self):
        """PrefillDecode silently serves decode-only unless the PD decider is armed.

        Selective prefix-based-pd-decider needs all of: nonCachedTokens > 0 (0 =
        disabled), the approx-prefix-cache-producer plugin that populates the
        attribute it reads, and that producer pinned to autoTune: false (the
        true default never populates). And it must NOT carry the prepareDataPlugins
        feature gate, which the v0.8.0 EPP image rejects and crashloops on.
        """
        cfg = self._apply()["epp-config"].spec.forProvider.manifest["data"]["pd-epp-config.yaml"]
        self.assertIn("prefix-based-pd-decider", cfg)
        self.assertIn("nonCachedTokens: 16", cfg)
        self.assertIn("approx-prefix-cache-producer", cfg)
        self.assertIn("autoTune: false", cfg)
        self.assertNotIn("nonCachedTokens: 0", cfg)
        self.assertNotIn("prepareDataPlugins", cfg)

    def test_epp_role_watches_inferenceobjectives(self):
        """The picker watches InferenceObjectives (GIE x-k8s.io group); the Role must allow it."""
        rules = self._apply()["epp-role"].spec.forProvider.manifest["rules"]
        self.assertTrue(
            any(
                "inference.networking.x-k8s.io" in r["apiGroups"] and "inferenceobjectives" in r["resources"]
                for r in rules
            ),
            f"EPP Role missing inferenceobjectives watch: {rules}",
        )

    def test_decode_port_follows_user_arg(self):
        """The sidecar and the decode container port track the user's --port, not a hardcoded one."""
        prefill = _standalone_engine(name="prefill")
        prefill.phase = "Prefill"
        decode = _standalone_engine(name="decode", args=["--model=m", "--port=9000"])
        decode.phase = "Decode"
        replica = _replica(engines=[prefill, decode])
        replica.spec.serving = v1alpha1.Serving(mode="PrefillDecode")
        composed = {}
        for e in replica.spec.engines:
            composed.update(native.NativeBackend().build(replica, e, _PC, base.serving_label(replica)))
        out = routing.apply(composed, replica, _PC)
        containers = self._serving_pod(out, "decode")["spec"]["containers"]
        engine = next(c for c in containers if c["name"] == "engine")
        sidecar = next(c for c in containers if c["name"] == "pd-sidecar")
        self.assertEqual(engine["ports"][0]["containerPort"], 9000)
        self.assertIn("--vllm-port=9000", sidecar["args"])
        self.assertEqual(sidecar["ports"][0]["containerPort"], 8000)

    def test_engines_role_labeled(self):
        out = self._apply()
        self.assertEqual(self._serving_pod(out, "prefill")["metadata"]["labels"]["llm-d.ai/role"], "prefill")
        decode_labels = self._serving_pod(out, "decode")["metadata"]["labels"]
        self.assertEqual(decode_labels["llm-d.ai/role"], "decode")
        self.assertEqual(decode_labels["app"], "r")

    def test_decode_gets_sidecar_and_moves_engine_port(self):
        out = self._apply()
        containers = self._serving_pod(out, "decode")["spec"]["containers"]
        names = [c["name"] for c in containers]
        self.assertEqual(names, ["engine", "pd-sidecar"])
        engine = next(c for c in containers if c["name"] == "engine")
        self.assertEqual(engine["ports"][0]["containerPort"], 8001)
        sidecar = next(c for c in containers if c["name"] == "pd-sidecar")
        self.assertEqual(sidecar["ports"][0]["containerPort"], 8000)
        self.assertIn("--secure-proxy=false", sidecar["args"])

    def test_prefill_has_no_sidecar(self):
        containers = self._serving_pod(self._apply(), "prefill")["spec"]["containers"]
        self.assertEqual([c["name"] for c in containers], ["engine"])

    def test_route_targets_inference_pool(self):
        route = self._apply()[base.ROUTE_KEY].spec.forProvider.manifest
        rule = route["spec"]["rules"][0]
        ref = rule["backendRefs"][0]
        self.assertEqual(ref["kind"], "InferencePool")
        self.assertEqual(ref["name"], "r-pool")
        # Disable the request timeout so long token streams aren't severed.
        self.assertEqual(rule["timeouts"]["request"], "0s")

    def test_selects_engines_by_phase_not_name(self):
        """Roles come from each engine's phase, not its name."""
        decode = _standalone_engine(name="alpha")
        decode.phase = "Decode"
        prefill = _standalone_engine(name="beta")
        prefill.phase = "Prefill"
        replica = _replica(engines=[decode, prefill])
        replica.spec.serving = v1alpha1.Serving(mode="PrefillDecode")
        composed = {}
        for e in replica.spec.engines:
            composed.update(native.NativeBackend().build(replica, e, _PC, base.serving_label(replica)))
        out = routing.apply(composed, replica, _PC)
        # alpha is Decode -> sidecar; beta is Prefill -> none, despite their names.
        self.assertEqual(
            [c["name"] for c in self._serving_pod(out, "alpha")["spec"]["containers"]], ["engine", "pd-sidecar"]
        )
        self.assertEqual([c["name"] for c in self._serving_pod(out, "beta")["spec"]["containers"]], ["engine"])
        self.assertEqual(self._serving_pod(out, "alpha")["metadata"]["labels"]["llm-d.ai/role"], "decode")
        self.assertEqual(self._serving_pod(out, "beta")["metadata"]["labels"]["llm-d.ai/role"], "prefill")


class TestUnifiedRouting(unittest.TestCase):
    """With no serving block (or mode Unified), routing.apply adds a plain
    Service + HTTPRoute and no InferencePool."""

    def test_adds_service_and_route(self):
        engine = _standalone_engine(name="main")
        replica = _replica(engines=[engine])
        composed = native.NativeBackend().build(replica, engine, _PC, base.serving_label(replica))
        out = routing.apply(composed, replica, _PC)
        self.assertIn(base.SERVICE_KEY, out)
        self.assertIn(base.ROUTE_KEY, out)
        self.assertNotIn("inference-pool", out)


class TestKvBlockSize(unittest.TestCase):
    """The EPP prefix-cache producer's blockSizeTokens is derived best-effort
    from the engine flags (#179) so it matches the engine's KV block size."""

    def test_defaults_to_16_when_absent(self):
        self.assertEqual(routing._kv_block_size([]), 16)
        self.assertEqual(routing._kv_block_size(["--model=/mnt/models"]), 16)

    def test_reads_vllm_block_size(self):
        self.assertEqual(routing._kv_block_size(["--block-size", "32"]), 32)
        self.assertEqual(routing._kv_block_size(["--model=/m", "--block-size=8"]), 8)

    def test_reads_sglang_page_size(self):
        self.assertEqual(routing._kv_block_size(["--page-size=64"]), 64)

    def test_non_integer_falls_back_to_default(self):
        self.assertEqual(routing._kv_block_size(["--block-size", "auto"]), 16)

    def test_rendered_config_uses_block_size(self):
        cfg = routing._epp_config_yaml(32)
        self.assertIn("blockSizeTokens: 32", cfg)
        self.assertNotIn("BLOCK_SIZE_TOKENS", cfg)


if __name__ == "__main__":
    unittest.main()
