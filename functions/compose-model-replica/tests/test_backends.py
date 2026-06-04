"""Tests for backend selection and the dispatch predicate."""

import unittest

from function.backends import base, dynamo, llmd, native
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


def _replica(*, tensor=1, pipeline=1):
    return v1alpha1.ModelReplica(
        spec=v1alpha1.SpecModel(
            clusterName="c",
            workers=v1alpha1.Workers(
                topology=v1alpha1.Topology(tensor=tensor, pipeline=pipeline),
                template=v1alpha1.Template(
                    spec=v1alpha1.Spec(
                        containers=[v1alpha1.Container(name="engine", image="img")],
                    ),
                ),
            ),
        ),
    )


class TestDispatch(unittest.TestCase):
    def test_single_pod_is_native(self):
        self.assertEqual(base.select_backend(_replica(tensor=8, pipeline=1)), base.NATIVE)

    def test_multi_node_is_llmd(self):
        self.assertEqual(base.select_backend(_replica(tensor=8, pipeline=2)), base.LLMD)

    def test_needs_coordination_only_when_multi_node(self):
        self.assertFalse(base.needs_cross_pod_coordination(_replica(tensor=4, pipeline=1)))
        self.assertTrue(base.needs_cross_pod_coordination(_replica(tensor=4, pipeline=3)))

    def test_pipeline_none_defaults_to_single_pod(self):
        # pipeline is Optional; exercise the `or 1` guard in nodes_per_worker.
        replica = _replica(tensor=4, pipeline=1)
        replica.spec.workers.topology.pipeline = None
        self.assertEqual(base.nodes_per_worker(replica), 1)
        self.assertFalse(base.needs_cross_pod_coordination(replica))


class TestNativeBackend(unittest.TestCase):
    def setUp(self):
        self.replica = v1alpha1.ModelReplica(
            metadata=metav1.ObjectMeta(name="r", namespace="ml-team"),
            spec=v1alpha1.SpecModel(
                clusterName="cluster-a",
                workers=v1alpha1.Workers(
                    topology=v1alpha1.Topology(tensor=2, pipeline=1),
                    template=v1alpha1.Template(
                        spec=v1alpha1.Spec(
                            containers=[
                                v1alpha1.Container(
                                    name="engine",
                                    image="vllm/vllm-openai:latest",
                                    args=["--model=Qwen/Qwen3-0.6B"],
                                ),
                            ],
                        ),
                    ),
                ),
            ),
        )
        self.cluster = icv1alpha1.InferenceCluster(
            metadata=metav1.ObjectMeta(name="cluster-a"),
            spec=icv1alpha1.Spec(
                cluster=icv1alpha1.Cluster(
                    source="Existing", existing=icv1alpha1.Existing(secretRef=icv1alpha1.SecretRef(name="k"))
                ),
            ),
            status=icv1alpha1.Status(
                providerConfigRef=icv1alpha1.ProviderConfigRef(name="cluster-a-pc"),
            ),
        )

    def test_emits_deployment_service_route(self):
        out = native.NativeBackend().build(self.replica, self.cluster, "my-deployment")
        kinds = sorted(o.spec.forProvider.manifest["kind"] for o in out.values())
        self.assertEqual(kinds, ["Deployment", "HTTPRoute", "Service"])

    def test_engine_args_passed_through_unmodified(self):
        out = native.NativeBackend().build(self.replica, self.cluster, "my-deployment")
        dep = next(o for o in out.values() if o.spec.forProvider.manifest["kind"] == "Deployment")
        container = dep.spec.forProvider.manifest["spec"]["template"]["spec"]["containers"][0]
        # No hf:// rewrite, no --model stripping: the engine fetches directly.
        self.assertIn("--model=Qwen/Qwen3-0.6B", container["args"])
        self.assertEqual(container["resources"]["limits"]["nvidia.com/gpu"], "2")

    def test_http_route_path_matches_namespace_and_deployment(self):
        out = native.NativeBackend().build(self.replica, self.cluster, "my-deployment")
        route = next(o for o in out.values() if o.spec.forProvider.manifest["kind"] == "HTTPRoute")
        rule = route.spec.forProvider.manifest["spec"]["rules"][0]
        self.assertEqual(rule["matches"][0]["path"]["value"], "/ml-team/my-deployment/")
        # The prefix must be stripped before the engine (serves /v1/...).
        self.assertEqual(rule["filters"][0]["type"], "URLRewrite")
        self.assertEqual(rule["filters"][0]["urlRewrite"]["path"]["replacePrefixMatch"], "/")

    def test_engine_env_passed_through(self):
        self.replica.spec.workers.template.spec.containers[0].env = [
            v1alpha1.EnvItem(name="HF_TOKEN", value="secret"),
        ]
        out = native.NativeBackend().build(self.replica, self.cluster, "my-deployment")
        dep = next(o for o in out.values() if o.spec.forProvider.manifest["kind"] == "Deployment")
        container = dep.spec.forProvider.manifest["spec"]["template"]["spec"]["containers"][0]
        self.assertIn({"name": "HF_TOKEN", "value": "secret"}, container["env"])

    def test_engine_command_passed_through(self):
        # A user-supplied command overrides the image entrypoint (single-pod).
        self.replica.spec.workers.template.spec.containers[0].command = ["python3", "-m", "my.server"]
        out = native.NativeBackend().build(self.replica, self.cluster, "my-deployment")
        dep = next(o for o in out.values() if o.spec.forProvider.manifest["kind"] == "Deployment")
        container = dep.spec.forProvider.manifest["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["command"], ["python3", "-m", "my.server"])


class TestLLMDBackend(unittest.TestCase):
    def setUp(self):
        self.replica = v1alpha1.ModelReplica(
            metadata=metav1.ObjectMeta(name="r", namespace="ml-team"),
            spec=v1alpha1.SpecModel(
                clusterName="cluster-a",
                workers=v1alpha1.Workers(
                    count=1,
                    topology=v1alpha1.Topology(tensor=8, pipeline=2),
                    template=v1alpha1.Template(
                        spec=v1alpha1.Spec(
                            containers=[
                                v1alpha1.Container(
                                    name="engine",
                                    image="vllm/vllm-openai:latest",
                                    args=["--model=meta-llama/Llama-3.1-405B"],
                                ),
                            ],
                        ),
                    ),
                ),
            ),
        )
        self.cluster = icv1alpha1.InferenceCluster(
            metadata=metav1.ObjectMeta(name="cluster-a"),
            spec=icv1alpha1.Spec(
                cluster=icv1alpha1.Cluster(
                    source="Existing", existing=icv1alpha1.Existing(secretRef=icv1alpha1.SecretRef(name="k"))
                ),
            ),
            status=icv1alpha1.Status(
                providerConfigRef=icv1alpha1.ProviderConfigRef(name="cluster-a-pc"),
            ),
        )

    def _build(self):
        return llmd.LLMDBackend().build(self.replica, self.cluster, "my-deployment")

    def _manifest(self, out, kind):
        return next(o.spec.forProvider.manifest for o in out.values() if o.spec.forProvider.manifest["kind"] == kind)

    def _lws(self, out):
        return self._manifest(out, "LeaderWorkerSet")

    def _leader(self, out):
        return self._lws(out)["spec"]["leaderWorkerTemplate"]["leaderTemplate"]

    def _worker(self, out):
        return self._lws(out)["spec"]["leaderWorkerTemplate"]["workerTemplate"]

    def test_emits_expected_kinds(self):
        # Plain Gateway-API routing (Traefik-compatible): LWS + Service +
        # HTTPRoute. No GAIE InferencePool / EPP.
        out = self._build()
        kinds = sorted(o.spec.forProvider.manifest["kind"] for o in out.values())
        self.assertEqual(kinds, ["HTTPRoute", "LeaderWorkerSet", "Service"])
        self.assertEqual(set(out.keys()), {"model-serving", "model-service", "model-route"})

    def test_no_gaie_resources(self):
        kinds = {o.spec.forProvider.manifest["kind"] for o in self._build().values()}
        self.assertNotIn("InferencePool", kinds)

    def test_lws_size(self):
        self.assertEqual(self._lws(self._build())["spec"]["leaderWorkerTemplate"]["size"], 2)

    def test_leader_runs_ray_head_then_engine(self):
        cmd = self._leader(self._build())["spec"]["containers"][0]["command"]
        self.assertEqual(cmd[:2], ["/bin/sh", "-c"])
        self.assertIn("ray start --head", cmd[2])
        self.assertIn("vllm.entrypoints.openai.api_server", cmd[2])
        # Engine args are folded into the command (consumed as "$@"), not a
        # separate container args field.
        self.assertEqual(cmd[3], "vllm")
        self.assertIn("--tensor-parallel-size=8", cmd[4:])
        self.assertIn("--pipeline-parallel-size=2", cmd[4:])
        # No hf:// rewrite: --model passed through as-is.
        self.assertIn("--model=meta-llama/Llama-3.1-405B", cmd[4:])

    def test_worker_joins_leader_ray_cluster(self):
        worker = self._worker(self._build())["spec"]["containers"][0]
        self.assertIn('ray start --address="$LWS_LEADER_ADDRESS:6379"', worker["command"][2])
        self.assertIn("--block", worker["command"][2])
        # The worker doesn't serve the API: no port, no readiness probe.
        self.assertNotIn("ports", worker)
        self.assertNotIn("readinessProbe", worker)

    def test_only_leader_labeled_for_service(self):
        out = self._build()
        self.assertEqual(self._leader(out)["metadata"]["labels"]["modelplane.ai/lws-role"], "leader")
        self.assertNotIn("modelplane.ai/lws-role", self._worker(out)["metadata"]["labels"])

    def test_service_selects_leader_pods(self):
        out = self._build()
        svc = self._manifest(out, "Service")
        lws_name = self._lws(out)["metadata"]["name"]
        self.assertEqual(svc["spec"]["selector"]["modelplane.ai/lws-role"], "leader")
        self.assertEqual(svc["spec"]["selector"]["modelplane.ai/serving"], lws_name)
        self.assertEqual(svc["spec"]["ports"], [{"port": 80, "targetPort": 8000}])

    def test_httproute_targets_service(self):
        out = self._build()
        backend_ref = self._manifest(out, "HTTPRoute")["spec"]["rules"][0]["backendRefs"][0]
        # Plain Service backendRef — no InferencePool kind/group.
        self.assertEqual(backend_ref, {"name": self._lws(out)["metadata"]["name"], "port": 80})

    def test_httproute_path_matches_and_strips_prefix(self):
        rule = self._manifest(self._build(), "HTTPRoute")["spec"]["rules"][0]
        self.assertEqual(rule["matches"][0]["path"]["value"], "/ml-team/my-deployment/")
        # The prefix must be stripped before the engine (serves /v1/...).
        self.assertEqual(rule["filters"][0]["type"], "URLRewrite")
        self.assertEqual(rule["filters"][0]["urlRewrite"]["path"]["replacePrefixMatch"], "/")

    def test_parallelism_flags_not_double_injected(self):
        # User-supplied parallelism flags must be respected, not duplicated.
        self.replica.spec.workers.template.spec.containers[0].args = [
            "--model=m",
            "--tensor-parallel-size=4",
        ]
        cmd = self._leader(self._build())["spec"]["containers"][0]["command"]
        self.assertEqual(cmd.count("--tensor-parallel-size=4"), 1)
        self.assertEqual(len([a for a in cmd if a.startswith("--tensor-parallel-size")]), 1)

    def test_user_command_override_bypasses_bootstrap(self):
        # Escape hatch for non-vLLM engines: a user command runs verbatim on
        # both templates; no Ray bootstrap, no vLLM parallelism flags injected.
        self.replica.spec.workers.template.spec.containers[0].command = ["python3", "-m", "sglang.launch_server"]
        self.replica.spec.workers.template.spec.containers[0].args = [
            "--nnodes",
            "$(LWS_GROUP_SIZE)",
            "--node-rank",
            "$(LWS_WORKER_INDEX)",
        ]
        out = self._build()
        leader = self._leader(out)["spec"]["containers"][0]
        worker = self._worker(out)["spec"]["containers"][0]
        # Same user command on both roles (symmetric); no /bin/sh bootstrap wrapper.
        self.assertEqual(leader["command"], ["python3", "-m", "sglang.launch_server"])
        self.assertEqual(worker["command"], leader["command"])
        # Args passed through verbatim; vLLM parallelism flags NOT injected.
        self.assertEqual(leader["args"], ["--nnodes", "$(LWS_GROUP_SIZE)", "--node-rank", "$(LWS_WORKER_INDEX)"])
        self.assertFalse(any(a.startswith("--tensor-parallel-size") for a in leader["args"]))


class TestDynamoStub(unittest.TestCase):
    def test_not_selected_in_v01(self):
        # No Dynamo-only capability is wired, so dispatch never returns DYNAMO.
        self.assertNotEqual(base.select_backend(_replica(tensor=8, pipeline=2)), base.DYNAMO)

    def test_build_raises(self):
        cluster = icv1alpha1.InferenceCluster(spec=icv1alpha1.Spec(cluster=icv1alpha1.Cluster(source="Existing")))
        with self.assertRaises(NotImplementedError):
            dynamo.DynamoBackend().build(_replica(tensor=8, pipeline=2), cluster, "d")
