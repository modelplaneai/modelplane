"""Tests for compose-model-replica backends.

Backend manifests are asserted with a `Case` table (matching the convention in
test_fn.py): each case builds a backend from a ModelReplica and compares the
composed manifests to a full `want`. Backend selection and the Dynamo stub are
dispatch/behaviour tests, not manifest comparisons, so they stay as focused
methods below the table.
"""

import dataclasses
import unittest

from function.backends import base, dynamo, llmd, native
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

_SERVING = "modelplane.ai/serving"
_ROLE = "modelplane.ai/lws-role"


def _replica(name="r", *, tensor=1, pipeline=1, args=None, command=None, namespace="ml-team"):
    container = v1alpha1.Container(
        name="engine",
        image="vllm/vllm-openai:latest",
        args=args if args is not None else ["--model=Qwen/Qwen3-0.6B"],
    )
    if command is not None:
        container.command = command
    return v1alpha1.ModelReplica(
        metadata=metav1.ObjectMeta(name=name, namespace=namespace),
        spec=v1alpha1.SpecModel(
            clusterName="cluster-a",
            workers=v1alpha1.Workers(
                count=1,
                topology=v1alpha1.Topology(tensor=tensor, pipeline=pipeline),
                template=v1alpha1.Template(spec=v1alpha1.Spec(containers=[container])),
            ),
        ),
    )


_CLUSTER = icv1alpha1.InferenceCluster(
    metadata=metav1.ObjectMeta(name="cluster-a"),
    spec=icv1alpha1.Spec(
        cluster=icv1alpha1.Cluster(
            source="Existing", existing=icv1alpha1.Existing(secretRef=icv1alpha1.SecretRef(name="k"))
        )
    ),
    status=icv1alpha1.Status(providerConfigRef=icv1alpha1.ProviderConfigRef(name="cluster-a-pc")),
)


def _route(name):
    """The HTTPRoute is identical across backends — replica-named, prefix-stripped."""
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


_NATIVE_WANT = {
    "model-serving": {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "r", "namespace": "default"},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {_SERVING: "r"}},
            "template": {
                "metadata": {"labels": {_SERVING: "r"}},
                "spec": {
                    "containers": [
                        {
                            "name": "engine",
                            "image": "vllm/vllm-openai:latest",
                            "args": ["--model=Qwen/Qwen3-0.6B"],
                            "ports": [{"containerPort": 8000}],
                            "resources": {"limits": {"nvidia.com/gpu": "2"}},
                            "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                            "readinessProbe": {
                                "httpGet": {"path": "/health", "port": 8000},
                                "initialDelaySeconds": 30,
                                "periodSeconds": 10,
                            },
                        }
                    ],
                    "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                },
            },
        },
    },
    "model-service": {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "r", "namespace": "default"},
        "spec": {"selector": {_SERVING: "r"}, "ports": [{"port": 80, "targetPort": 8000}]},
    },
    "model-route": _route("r"),
}


def _lws(leader_container, worker_container):
    return {
        "apiVersion": "leaderworkerset.x-k8s.io/v1",
        "kind": "LeaderWorkerSet",
        "metadata": {"name": "r", "namespace": "default"},
        "spec": {
            "replicas": 1,
            "leaderWorkerTemplate": {
                "size": 2,
                "leaderTemplate": {
                    "metadata": {"labels": {_SERVING: "r", _ROLE: "leader"}},
                    "spec": {
                        "containers": [leader_container],
                        "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                    },
                },
                "workerTemplate": {
                    "metadata": {"labels": {_SERVING: "r"}},
                    "spec": {
                        "containers": [worker_container],
                        "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                    },
                },
            },
        },
    }


def _engine(command, *, serving, args=None):
    c = {
        "name": "engine",
        "image": "vllm/vllm-openai:latest",
        "resources": {"limits": {"nvidia.com/gpu": "8"}},
        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
        "command": command,
    }
    if args is not None:
        c["args"] = args
    if serving:
        c["ports"] = [{"containerPort": 8000}]
        c["readinessProbe"] = {
            "httpGet": {"path": "/health", "port": 8000},
            "initialDelaySeconds": 30,
            "periodSeconds": 10,
        }
    return c


# vLLM bootstrap: args folded into the leader command (consumed as "$@"); worker
# just joins the Ray cluster.
_LLMD_VLLM_LEADER_CMD = [
    "/bin/sh",
    "-c",
    llmd._LEADER_BOOTSTRAP,
    "vllm",
    "--model=meta-llama/Llama-3.1-405B",
    "--tensor-parallel-size=8",
    "--pipeline-parallel-size=2",
]
_LLMD_VLLM_WANT = {
    "model-serving": _lws(
        _engine(_LLMD_VLLM_LEADER_CMD, serving=True),
        _engine(["/bin/sh", "-c", llmd._WORKER_BOOTSTRAP], serving=False),
    ),
    "model-service": {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "r", "namespace": "default"},
        "spec": {"selector": {_SERVING: "r", _ROLE: "leader"}, "ports": [{"port": 80, "targetPort": 8000}]},
    },
    "model-route": _route("r"),
}

# Escape hatch: the user command runs verbatim on both templates; no Ray
# bootstrap and no vLLM parallelism flags injected.
_SGLANG_CMD = ["python3", "-m", "sglang.launch_server"]
_SGLANG_ARGS = ["--nnodes", "2"]
_LLMD_ESCAPE_WANT = {
    "model-serving": _lws(
        _engine(_SGLANG_CMD, serving=True, args=_SGLANG_ARGS),
        _engine(_SGLANG_CMD, serving=False, args=_SGLANG_ARGS),
    ),
    "model-service": _LLMD_VLLM_WANT["model-service"],
    "model-route": _route("r"),
}


@dataclasses.dataclass
class Case:
    name: str
    backend: object
    replica: v1alpha1.ModelReplica
    want: dict


_CASES = [
    Case(
        name="native single-pod Deployment+Service+Route",
        backend=native.NativeBackend(),
        replica=_replica(tensor=2),
        want=_NATIVE_WANT,
    ),
    Case(
        name="llm-d multi-node injects vLLM/Ray bootstrap",
        backend=llmd.LLMDBackend(),
        replica=_replica(tensor=8, pipeline=2, args=["--model=meta-llama/Llama-3.1-405B"]),
        want=_LLMD_VLLM_WANT,
    ),
    Case(
        name="llm-d user command bypasses bootstrap (non-vLLM escape hatch)",
        backend=llmd.LLMDBackend(),
        replica=_replica(tensor=8, pipeline=2, command=_SGLANG_CMD, args=_SGLANG_ARGS),
        want=_LLMD_ESCAPE_WANT,
    ),
]


class TestBackendManifests(unittest.TestCase):
    def test_manifests(self):
        for case in _CASES:
            with self.subTest(case.name):
                out = case.backend.build(case.replica, _CLUSTER)
                got = {key: obj.spec.forProvider.manifest for key, obj in out.items()}
                self.assertEqual(case.want, got, "-want, +got")

    @staticmethod
    def _names(out):
        return {o.spec.forProvider.manifest["metadata"]["name"] for o in out.values()}

    def test_resources_named_after_replica_avoid_collision(self):
        # Two replicas of one deployment on the same IC must produce distinct
        # workload-resource names (Nic's collision concern).
        a = native.NativeBackend().build(_replica("dep-clusterA"), _CLUSTER)
        b = native.NativeBackend().build(_replica("dep-clusterB"), _CLUSTER)
        self.assertEqual(self._names(a), {"dep-clusterA"})
        self.assertEqual(self._names(b), {"dep-clusterB"})


class TestBackendSelection(unittest.TestCase):
    def test_single_pod_is_native(self):
        self.assertEqual(base.select_backend(_replica(tensor=8, pipeline=1)), base.NATIVE)

    def test_multi_node_is_llmd(self):
        self.assertEqual(base.select_backend(_replica(tensor=8, pipeline=2)), base.LLMD)

    def test_needs_coordination_only_when_multi_node(self):
        self.assertFalse(base.needs_cross_pod_coordination(_replica(tensor=4, pipeline=1)))
        self.assertTrue(base.needs_cross_pod_coordination(_replica(tensor=4, pipeline=3)))

    def test_pipeline_none_defaults_to_single_pod(self):
        replica = _replica(tensor=4, pipeline=1)
        replica.spec.workers.topology.pipeline = None
        self.assertFalse(base.needs_cross_pod_coordination(replica))


class TestDynamoStub(unittest.TestCase):
    def test_not_selected_in_v01(self):
        self.assertNotEqual(base.select_backend(_replica(tensor=8, pipeline=2)), base.DYNAMO)

    def test_build_raises(self):
        with self.assertRaises(NotImplementedError):
            dynamo.DynamoBackend().build(_replica(tensor=8, pipeline=2), _CLUSTER)
