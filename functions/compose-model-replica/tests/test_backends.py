"""Tests for compose-model-replica backends.

A backend builds the workload (Deployment or LeaderWorkerSet) and the
ResourceClaimTemplates for one worker group; the shared Service and HTTPRoute
that front a replica's groups are built by base.serving_resources. Manifests are
asserted with a `Case` table: each case builds a group's backend and compares
the composed manifests to a full `want`. Backend selection, serving, and the
Dynamo stub are dispatch/behaviour tests below the table.
"""

import dataclasses
import unittest

from crossplane.function import resource
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


def _standalone_group(
    name="main",
    *,
    replicas=1,
    args=None,
    command=None,
    device_requests=None,
):
    """A single Standalone-member group."""
    container = v1alpha1.Container(
        name="engine",
        image="vllm/vllm-openai:latest",
        args=args if args is not None else ["--model=Qwen/Qwen3-0.6B"],
    )
    if command is not None:
        container.command = command
    return v1alpha1.Worker(
        name=name,
        replicas=replicas,
        nodePoolName="frontier",
        members=[
            v1alpha1.Member(
                role="Standalone",
                deviceRequests=device_requests if device_requests is not None else [_gpu_request(1)],
                template=v1alpha1.Template(spec=v1alpha1.Spec(containers=[container])),
            ),
        ],
    )


def _gang_group(
    name="main",
    *,
    replicas=1,
    workers=1,
    leader_args=None,
    leader_command=None,
    worker_args=None,
    worker_command=None,
    device_requests=None,
):
    """A Leader + Worker group."""
    dr = device_requests if device_requests is not None else [_gpu_request(8)]

    def member(role, count, args, command):
        container = v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")
        if args is not None:
            container.args = args
        if command is not None:
            container.command = command
        kwargs = {
            "role": role,
            "deviceRequests": dr,
            "template": v1alpha1.Template(spec=v1alpha1.Spec(containers=[container])),
        }
        if count is not None:
            kwargs["count"] = count
        return v1alpha1.Member(**kwargs)

    return v1alpha1.Worker(
        name=name,
        replicas=replicas,
        nodePoolName="frontier",
        members=[
            member("Leader", None, leader_args, leader_command),
            member("Worker", workers, worker_args, worker_command),
        ],
    )


def _replica(name="r", *, namespace="ml-team", groups=None):
    if groups is None:
        groups = [_standalone_group()]
    return v1alpha1.ModelReplica(
        metadata=metav1.ObjectMeta(name=name, namespace=namespace),
        spec=v1alpha1.SpecModel(clusterName="cluster-a", workers=groups),
    )


# The composed workload name for the default replica "r" / group "main".
# Always group-qualified, and so always distinct from the replica name the
# serving Service uses - see base.group_name on why that matters for LWS.
_WORKLOAD_NAME = resource.child_name("r", "main")


def _claim_template(role, count, *, replica="r", group="main"):
    """The ResourceClaimTemplate manifest a member's device requests produce."""
    return {
        "apiVersion": "resource.k8s.io/v1",
        "kind": "ResourceClaimTemplate",
        "metadata": {"name": resource.child_name(replica, group, role, "devices"), "namespace": "default"},
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
    "resource-claim-main-standalone": _claim_template("standalone", 1),
}


def _lws(leader_container, worker_container):
    node_selector = {"modelplane.ai/pool": "frontier"}

    def claims(role):
        return [
            {
                "name": "devices",
                "resourceClaimTemplateName": resource.child_name("r", "main", role, "devices"),
            }
        ]

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
                        "resourceClaims": claims("leader"),
                        "tolerations": tolerations,
                    },
                },
                "workerTemplate": {
                    "spec": {
                        "containers": [worker_container],
                        "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                        "nodeSelector": node_selector,
                        "resourceClaims": claims("worker"),
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


# A multi-node group with verbatim leader/worker commands - no flag injection,
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
    "resource-claim-main-leader": _claim_template("leader", 8),
    "resource-claim-main-worker": _claim_template("worker", 8),
}


@dataclasses.dataclass
class Case:
    name: str
    backend: object
    group: v1alpha1.Worker
    want: dict


_CASES = [
    Case(
        name="native Standalone group composes a Deployment",
        backend=native.NativeBackend(),
        group=_standalone_group(),
        want=_NATIVE_WANT,
    ),
    Case(
        name="llm-d Leader/Worker group composes a LeaderWorkerSet, commands verbatim",
        backend=llmd.LLMDBackend(),
        group=_gang_group(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD),
        want=_LLMD_WANT,
    ),
]


class TestBackendManifests(unittest.TestCase):
    def test_manifests(self):
        for case in _CASES:
            with self.subTest(case.name):
                replica = _replica(groups=[case.group])
                out = case.backend.build(replica, case.group, _PC, base.serving_label(replica))
                got = {key: obj.spec.forProvider.manifest for key, obj in out.items()}
                self.assertEqual(case.want, got, "-want, +got")

    def test_serving_resources(self):
        # The shared Service + HTTPRoute front a replica regardless of how many
        # groups it has, named after the replica.
        replica = _replica()
        out = base.serving_resources(replica, _PC)
        got = {key: obj.spec.forProvider.manifest for key, obj in out.items()}
        self.assertEqual({"model-service": _service("r"), "model-route": _route("r")}, got)

    def test_leader_address_injected_into_gang_engines(self):
        # Every engine container in a multi-node gang gets
        # MODELPLANE_LEADER_ADDRESS, aliasing LWS_LEADER_ADDRESS, ahead of the
        # user's own env so commands can reference $(MODELPLANE_LEADER_ADDRESS).
        group = _gang_group(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD)
        replica = _replica(groups=[group])
        out = llmd.LLMDBackend().build(replica, group, _PC, base.serving_label(replica))
        tmpl = out["model-serving-main"].spec.forProvider.manifest["spec"]["leaderWorkerTemplate"]
        for role in ("leaderTemplate", "workerTemplate"):
            env = tmpl[role]["spec"]["containers"][0]["env"]
            self.assertEqual(env[0], _LEADER_ENV)

    def test_user_env_preserved_after_leader_address(self):
        group = _gang_group(
            leader_command=_LEADER_CMD,
            worker_command=_WORKER_CMD,
        )
        group.members[0].template.spec.containers[0].env = [v1alpha1.EnvItem(name="HF_TOKEN", value="x")]
        replica = _replica(groups=[group])
        out = llmd.LLMDBackend().build(replica, group, _PC, base.serving_label(replica))
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
        out_a = native.NativeBackend().build(a, a.spec.workers[0], _PC, base.serving_label(a))
        out_b = native.NativeBackend().build(b, b.spec.workers[0], _PC, base.serving_label(b))
        self.assertEqual(self._names(out_a) & self._names(out_b), set())

    def test_lws_name_differs_from_serving_service_name(self):
        # Regression: LWS's controller creates a headless Service named after
        # the LWS for gang pod DNS - but only if no Service of that name
        # exists. When the LWS shared the serving Service's name (the replica
        # name), that headless Service was never created, the followers could
        # never resolve the leader, and the gang deadlocked. The workload name
        # must differ from the Service's.
        group = _gang_group(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD)
        replica = _replica(groups=[group])
        workload = llmd.LLMDBackend().build(replica, group, _PC, base.serving_label(replica))
        serving = base.serving_resources(replica, _PC)
        lws_name = workload["model-serving-main"].spec.forProvider.manifest["metadata"]["name"]
        service_name = serving["model-service"].spec.forProvider.manifest["metadata"]["name"]
        self.assertNotEqual(lws_name, service_name)

    def test_multi_group_qualifies_workload_names(self):
        # A replica with two groups names each group's workload distinctly so
        # they don't collide on the remote cluster.
        groups = [_standalone_group("prefill"), _standalone_group("decode")]
        replica = _replica(groups=groups)
        names = set()
        for g in groups:
            out = native.NativeBackend().build(replica, g, _PC, base.serving_label(replica))
            names |= self._names(out)
        self.assertEqual(len(names), 4)  # 2 deployments + 2 claim templates

    def test_workload_readiness_policies(self):
        # The workload reports readiness from its Available condition via a CEL
        # query; the claim templates are ready on create.
        for name, backend, group in (
            ("native", native.NativeBackend(), _standalone_group()),
            ("llm-d", llmd.LLMDBackend(), _gang_group(leader_command=_LEADER_CMD, worker_command=_WORKER_CMD)),
        ):
            with self.subTest(name):
                replica = _replica(groups=[group])
                out = backend.build(replica, group, _PC, base.serving_label(replica))
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
        group = _standalone_group(
            device_requests=[
                v1alpha1.DeviceRequest(name="gpu", deviceClassName="gpu.nvidia.com", count=8),
                v1alpha1.DeviceRequest(name="nic", deviceClassName="nic.nvidia.com", count=8),
            ],
        )
        replica = _replica(groups=[group])
        out = native.NativeBackend().build(replica, group, _PC, base.serving_label(replica))
        pod = out["model-serving-main"].spec.forProvider.manifest["spec"]["template"]["spec"]
        claims = pod["containers"][0]["resources"]["claims"]
        self.assertEqual(claims, [{"name": "devices"}])
        self.assertEqual(pod["resourceClaims"][0]["name"], "devices")
        template = out["resource-claim-main-standalone"].spec.forProvider.manifest
        template_requests = template["spec"]["spec"]["devices"]["requests"]
        self.assertEqual([r["name"] for r in template_requests], ["gpu", "nic"])
        self.assertEqual(out["resource-claim-main-standalone"].spec.readiness.policy, "SuccessfulCreate")


class TestBackendSelection(unittest.TestCase):
    def test_standalone_group_is_native(self):
        self.assertEqual(base.select_backend(_standalone_group()), base.NATIVE)

    def test_leader_worker_group_is_llmd(self):
        self.assertEqual(base.select_backend(_gang_group()), base.LLMD)


class TestDynamoStub(unittest.TestCase):
    def test_not_selected_in_v01(self):
        self.assertNotEqual(base.select_backend(_gang_group()), base.DYNAMO)

    def test_build_raises(self):
        group = _gang_group()
        replica = _replica(groups=[group])
        with self.assertRaises(NotImplementedError):
            dynamo.DynamoBackend().build(replica, group, _PC, base.serving_label(replica))


class TestCacheMounts(unittest.TestCase):
    def _replica(self, *, cache=None, args=None, command=None):
        group = _standalone_group(args=args or [], command=command)
        modelcache = v1alpha1.ModelCacheRef(name=cache) if cache else None
        return v1alpha1.ModelReplica(
            metadata=metav1.ObjectMeta(namespace="ml-team"),
            spec=v1alpha1.SpecModel(clusterName="c", modelCacheRef=modelcache, workers=[group]),
        )

    @staticmethod
    def _engine(replica):
        return replica.spec.workers[0].members[0].template.spec.containers[0]

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
        group = _standalone_group(args=[])
        return v1alpha1.ModelReplica(
            metadata=metav1.ObjectMeta(name="r", namespace="ml-team"),
            spec=v1alpha1.SpecModel(
                clusterName="cluster-a",
                modelCacheRef=v1alpha1.ModelCacheRef(name="qwen"),
                workers=[group],
            ),
        )

    def test_mounts_pvc_and_injects_model(self):
        replica = self._replica()
        out = native.NativeBackend().build(replica, replica.spec.workers[0], _PC, base.serving_label(replica))
        dep = out["model-serving-main"].spec.forProvider.manifest
        pod = dep["spec"]["template"]["spec"]
        vol_names = {v["name"] for v in pod["volumes"]}
        self.assertIn("model-cache", vol_names)
        container = pod["containers"][0]
        self.assertIn({"name": "model-cache", "mountPath": "/mnt/models"}, container["volumeMounts"])
        self.assertIn("--model=/mnt/models", container["args"])


class TestLLMDBackendCache(unittest.TestCase):
    def _replica(self, *, leader_command=None, worker_command=None, leader_args=None, worker_args=None):
        group = _gang_group(
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
                workers=[group],
            ),
        )

    def test_both_lws_templates_mount_cache(self):
        replica = self._replica(leader_args=[], worker_command=["/bin/sh", "-c", "join"])
        lws = (
            llmd.LLMDBackend()
            .build(replica, replica.spec.workers[0], _PC, base.serving_label(replica))["model-serving-main"]
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
            .build(replica, replica.spec.workers[0], _PC, base.serving_label(replica))["model-serving-main"]
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
            .build(replica, replica.spec.workers[0], _PC, base.serving_label(replica))["model-serving-main"]
            .spec.forProvider.manifest
        )
        leader = lws["spec"]["leaderWorkerTemplate"]["leaderTemplate"]["spec"]["containers"][0]
        self.assertIn(
            {"name": "model-cache", "mountPath": "/mnt/models"},
            leader["volumeMounts"],
        )
        self.assertEqual(leader["command"], leader_cmd)


if __name__ == "__main__":
    unittest.main()
