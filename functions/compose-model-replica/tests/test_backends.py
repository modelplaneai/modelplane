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

    def test_engine_env_passed_through(self):
        self.replica.spec.workers.template.spec.containers[0].env = [
            v1alpha1.EnvItem(name="HF_TOKEN", value="secret"),
        ]
        out = native.NativeBackend().build(self.replica, self.cluster, "my-deployment")
        dep = next(o for o in out.values() if o.spec.forProvider.manifest["kind"] == "Deployment")
        container = dep.spec.forProvider.manifest["spec"]["template"]["spec"]["containers"][0]
        self.assertIn({"name": "HF_TOKEN", "value": "secret"}, container["env"])


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

    def test_emits_expected_kinds(self):
        out = self._build()
        kinds = {o.spec.forProvider.manifest["kind"] for o in out.values()}
        self.assertEqual(kinds, {"LeaderWorkerSet", "InferencePool", "Deployment", "Service", "HTTPRoute"})

    def test_inferencepool_uses_v1_field_names(self):
        out = self._build()
        pool = self._manifest(out, "InferencePool")
        self.assertEqual(pool["apiVersion"], "inference.networking.k8s.io/v1")
        # v1: targetPorts is a LIST of {number}, not the pre-v1 scalar targetPortNumber.
        self.assertEqual(pool["spec"]["targetPorts"], [{"number": 8000}])
        self.assertNotIn("targetPortNumber", pool["spec"])
        # v1: endpointPickerRef, not the v1alpha2 extensionRef.
        self.assertNotIn("extensionRef", pool["spec"])
        self.assertEqual(pool["spec"]["endpointPickerRef"]["failureMode"], "FailOpen")
        # The pool must select THIS replica's pods exactly (per-replica scoping),
        # so co-located replicas don't cross-select. Selector == LWS pod labels.
        lws = self._manifest(out, "LeaderWorkerSet")
        pod_labels = lws["spec"]["leaderWorkerTemplate"]["leaderTemplate"]["metadata"]["labels"]
        self.assertEqual(pool["spec"]["selector"]["matchLabels"]["llm-d.ai/inference-serving"], "true")
        self.assertIn("modelplane.ai/serving", pool["spec"]["selector"]["matchLabels"])
        self.assertEqual(pool["spec"]["selector"]["matchLabels"], pod_labels)

    def test_lws_size_and_parallelism_flags(self):
        out = self._build()
        lws = self._manifest(out, "LeaderWorkerSet")
        self.assertEqual(lws["spec"]["leaderWorkerTemplate"]["size"], 2)
        leader = lws["spec"]["leaderWorkerTemplate"]["leaderTemplate"]["spec"]["containers"][0]
        self.assertIn("--tensor-parallel-size=8", leader["args"])
        self.assertIn("--pipeline-parallel-size=2", leader["args"])

    def test_model_arg_passed_through_unmodified(self):
        out = self._build()
        lws = self._manifest(out, "LeaderWorkerSet")
        leader = lws["spec"]["leaderWorkerTemplate"]["leaderTemplate"]["spec"]["containers"][0]
        # No hf:// rewrite: --model passed through as-is.
        self.assertIn("--model=meta-llama/Llama-3.1-405B", leader["args"])

    def test_httproute_targets_inferencepool(self):
        out = self._build()
        route = self._manifest(out, "HTTPRoute")
        backend_ref = route["spec"]["rules"][0]["backendRefs"][0]
        self.assertEqual(backend_ref["kind"], "InferencePool")
        self.assertEqual(backend_ref["group"], "inference.networking.k8s.io")

    def test_httproute_path_matches_namespace_and_deployment(self):
        out = self._build()
        route = self._manifest(out, "HTTPRoute")
        self.assertEqual(
            route["spec"]["rules"][0]["matches"][0]["path"]["value"],
            "/ml-team/my-deployment/",
        )

    def test_parallelism_flags_not_double_injected(self):
        # User-supplied parallelism flags must be respected, not duplicated.
        self.replica.spec.workers.template.spec.containers[0].args = [
            "--model=m",
            "--tensor-parallel-size=4",
        ]
        out = self._build()
        lws = self._manifest(out, "LeaderWorkerSet")
        args = lws["spec"]["leaderWorkerTemplate"]["leaderTemplate"]["spec"]["containers"][0]["args"]
        self.assertEqual(args.count("--tensor-parallel-size=4"), 1)
        self.assertEqual(len([a for a in args if a.startswith("--tensor-parallel-size")]), 1)

    def test_epp_service_selects_epp_deployment(self):
        out = self._build()
        dep = self._manifest(out, "Deployment")
        svc = self._manifest(out, "Service")
        self.assertEqual(dep["spec"]["selector"]["matchLabels"], svc["spec"]["selector"])


class TestDynamoStub(unittest.TestCase):
    def test_not_selected_in_v01(self):
        # No Dynamo-only capability is wired, so dispatch never returns DYNAMO.
        self.assertNotEqual(base.select_backend(_replica(tensor=8, pipeline=2)), base.DYNAMO)

    def test_build_raises(self):
        cluster = icv1alpha1.InferenceCluster(spec=icv1alpha1.Spec(cluster=icv1alpha1.Cluster(source="Existing")))
        with self.assertRaises(NotImplementedError):
            dynamo.DynamoBackend().build(_replica(tensor=8, pipeline=2), cluster, "d")
