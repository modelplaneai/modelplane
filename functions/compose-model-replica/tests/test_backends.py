"""Tests for backend selection and the dispatch predicate."""

import unittest

from function.backends import base, native
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
