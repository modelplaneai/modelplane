"""Tests for backend selection and the dispatch predicate."""

import unittest

from function.backends import base
from models.ai.modelplane.modelreplica import v1alpha1


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
