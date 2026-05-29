"""Tests for the scheduling module.

Unit tests for the retain-then-place scheduler. These construct
Pydantic models directly and call schedule() to exercise the core
logic without the protobuf/gRPC ceremony of the fn tests.
"""

import dataclasses
import unittest

from function import scheduling
from function.scheduling import Candidate
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.modeldeployment import v1alpha1 as mdv1alpha1
from models.ai.modelplane.modelreplica import v1alpha1 as mrv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for scheduling.schedule."""

    name: str
    deployment: mdv1alpha1.ModelDeployment
    clusters: list[icv1alpha1.InferenceCluster]
    all_replicas: list[mrv1alpha1.ModelReplica]
    want: list[Candidate]


def _deployment(name: str = "my-model", replicas: int = 1, tensor: int = 1, pipeline: int = 1, count: int = 1):
    """Construct a ModelDeployment with the given topology."""
    return mdv1alpha1.ModelDeployment(
        metadata=metav1.ObjectMeta(name=name, namespace="ml-team"),
        spec=mdv1alpha1.SpecModel(
            replicas=replicas,
            workers=mdv1alpha1.Workers(
                count=count,
                topology=mdv1alpha1.Topology(tensor=tensor, pipeline=pipeline),
                template=mdv1alpha1.Template(
                    spec=mdv1alpha1.Spec(
                        containers=[mdv1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")],
                    ),
                ),
            ),
        ),
    )


def _cluster(
    name: str,
    *,
    ready: bool = True,
    gateway_address: str = "10.0.0.1",
    pools: list[dict] | None = None,
) -> icv1alpha1.InferenceCluster:
    """Construct an InferenceCluster with the given readiness and pools.

    A "ready" cluster has a Ready=True condition and a gateway address.
    Setting ready=False or gateway_address="" produces a degraded cluster
    the scheduler will retain but not pick anew.
    """
    if pools is None:
        pools = [{"countPerNode": 1, "nodes": 2}]

    conditions = []
    if ready:
        conditions.append(
            icv1alpha1.Condition(
                type="Ready",
                status="True",
                reason="Available",
                lastTransitionTime="2025-01-01T00:00:00Z",
            )
        )
    else:
        conditions.append(
            icv1alpha1.Condition(
                type="Ready",
                status="False",
                reason="Unavailable",
                lastTransitionTime="2025-01-01T00:00:00Z",
            )
        )

    return icv1alpha1.InferenceCluster(
        metadata=metav1.ObjectMeta(name=name),
        spec=icv1alpha1.Spec(
            cluster=icv1alpha1.Cluster(
                source="Existing",
                existing=icv1alpha1.Existing(secretRef=icv1alpha1.SecretRef(name="k")),
            ),
        ),
        status=icv1alpha1.Status(
            conditions=conditions,
            gateway=icv1alpha1.Gateway(address=gateway_address) if gateway_address else icv1alpha1.Gateway(),
            providerConfigRef=icv1alpha1.ProviderConfigRef(name=name),
            capacity=icv1alpha1.Capacity(gpuPools=[icv1alpha1.GpuPool(**p) for p in pools]),
        ),
    )


def _replica(
    deployment_name: str,
    cluster_name: str,
    *,
    tensor: int = 1,
    pipeline: int = 1,
    count: int = 1,
) -> mrv1alpha1.ModelReplica:
    """Construct an observed ModelReplica pinned to a cluster."""
    return mrv1alpha1.ModelReplica(
        metadata=metav1.ObjectMeta(
            name=f"{deployment_name}-{cluster_name}",
            namespace="ml-team",
            labels={
                "modelplane.ai/replica": "true",
                "modelplane.ai/deployment": deployment_name,
                "modelplane.ai/cluster": cluster_name,
            },
        ),
        spec=mrv1alpha1.SpecModel(
            clusterName=cluster_name,
            workers=mrv1alpha1.Workers(
                count=count,
                topology=mrv1alpha1.Topology(tensor=tensor, pipeline=pipeline),
                template=mrv1alpha1.Template(
                    spec=mrv1alpha1.Spec(
                        containers=[mrv1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")],
                    ),
                ),
            ),
        ),
    )


class TestSchedule(unittest.TestCase):
    """Tests for scheduling.schedule."""

    def test_schedule(self) -> None:
        """The scheduler retains existing pins and places new replicas."""

        cases = [
            Case(
                name="no clusters returns no candidates",
                deployment=_deployment(),
                clusters=[],
                all_replicas=[],
                want=[],
            ),
            Case(
                name="single ready cluster is picked",
                deployment=_deployment(),
                clusters=[_cluster("cluster-a")],
                all_replicas=[],
                want=[Candidate(name="cluster-a", gateway_address="10.0.0.1")],
            ),
            Case(
                name="not-ready cluster is not picked for a new replica",
                deployment=_deployment(),
                clusters=[_cluster("cluster-a", ready=False)],
                all_replicas=[],
                want=[],
            ),
            Case(
                name="cluster without gateway address is not picked",
                deployment=_deployment(),
                clusters=[_cluster("cluster-a", gateway_address="")],
                all_replicas=[],
                want=[],
            ),
            Case(
                name="cluster with no fitting pool is skipped",
                deployment=_deployment(tensor=8),
                clusters=[_cluster("cluster-a", pools=[{"countPerNode": 1, "nodes": 2}])],
                all_replicas=[],
                want=[],
            ),
            Case(
                name="multi-node deployment needs enough nodes",
                deployment=_deployment(tensor=1, pipeline=4),
                clusters=[_cluster("cluster-a", pools=[{"countPerNode": 1, "nodes": 2}])],
                all_replicas=[],
                want=[],
            ),
            Case(
                name="existing replica is retained on its pinned cluster",
                deployment=_deployment(),
                clusters=[_cluster("cluster-a"), _cluster("cluster-b", gateway_address="10.0.0.2")],
                all_replicas=[_replica("my-model", "cluster-a")],
                # cluster-a wins even though cluster-b is also viable.
                want=[Candidate(name="cluster-a", gateway_address="10.0.0.1")],
            ),
            Case(
                name="degraded pinned cluster is retained with empty gateway",
                deployment=_deployment(),
                clusters=[_cluster("cluster-a", ready=False, gateway_address="")],
                all_replicas=[_replica("my-model", "cluster-a")],
                # Pin survives but gateway_address is empty - callers
                # must not compose routing for it.
                want=[Candidate(name="cluster-a", gateway_address="")],
            ),
            Case(
                name="degraded pinned cluster keeps gateway address if still published",
                deployment=_deployment(),
                clusters=[_cluster("cluster-a", ready=False, gateway_address="10.0.0.1")],
                all_replicas=[_replica("my-model", "cluster-a")],
                # _cluster_ready returns False so this cluster wouldn't
                # be picked anew, but a retained pin still surfaces
                # whatever gateway_address is published.
                want=[Candidate(name="cluster-a", gateway_address="10.0.0.1")],
            ),
            Case(
                name="deleted pinned cluster triggers re-placement",
                deployment=_deployment(),
                clusters=[_cluster("cluster-b", gateway_address="10.0.0.2")],
                all_replicas=[_replica("my-model", "cluster-a")],
                # cluster-a is gone, cluster-b takes its place.
                want=[Candidate(name="cluster-b", gateway_address="10.0.0.2")],
            ),
            Case(
                name="scale up places new replicas on additional clusters",
                deployment=_deployment(replicas=2),
                clusters=[_cluster("cluster-a"), _cluster("cluster-b", gateway_address="10.0.0.2")],
                all_replicas=[_replica("my-model", "cluster-a")],
                want=[
                    Candidate(name="cluster-a", gateway_address="10.0.0.1"),
                    Candidate(name="cluster-b", gateway_address="10.0.0.2"),
                ],
            ),
            Case(
                name="scale up with no extra capacity returns only retained",
                deployment=_deployment(replicas=2),
                clusters=[_cluster("cluster-a")],
                all_replicas=[_replica("my-model", "cluster-a")],
                # Only cluster-a exists - cluster-b can't be placed.
                want=[Candidate(name="cluster-a", gateway_address="10.0.0.1")],
            ),
            Case(
                name="scale down keeps lexicographically earliest pins",
                deployment=_deployment(replicas=1),
                clusters=[_cluster("cluster-a"), _cluster("cluster-b", gateway_address="10.0.0.2")],
                all_replicas=[
                    _replica("my-model", "cluster-b"),
                    _replica("my-model", "cluster-a"),
                ],
                # Trim to 1 - cluster-a wins by alphabetical order.
                want=[Candidate(name="cluster-a", gateway_address="10.0.0.1")],
            ),
            Case(
                name="new placement is alphabetical for determinism",
                deployment=_deployment(replicas=2),
                clusters=[
                    _cluster("cluster-c", gateway_address="10.0.0.3"),
                    _cluster("cluster-a"),
                    _cluster("cluster-b", gateway_address="10.0.0.2"),
                ],
                all_replicas=[],
                want=[
                    Candidate(name="cluster-a", gateway_address="10.0.0.1"),
                    Candidate(name="cluster-b", gateway_address="10.0.0.2"),
                ],
            ),
            Case(
                name="other deployment's replicas consume capacity",
                deployment=_deployment(tensor=2),
                clusters=[_cluster("cluster-a", pools=[{"countPerNode": 2, "nodes": 1}])],
                # other-model occupies all 2 GPUs on cluster-a.
                all_replicas=[_replica("other-model", "cluster-a", tensor=2)],
                want=[],
            ),
            Case(
                name="our own observed replicas don't double-count against us",
                deployment=_deployment(tensor=2),
                clusters=[_cluster("cluster-a", pools=[{"countPerNode": 2, "nodes": 1}])],
                # Our previous replica on cluster-a is excluded from
                # 'used' so we still fit.
                all_replicas=[_replica("my-model", "cluster-a", tensor=2)],
                want=[Candidate(name="cluster-a", gateway_address="10.0.0.1")],
            ),
            Case(
                name="replica labeled for our deployment but pinned to unknown cluster is ignored",
                deployment=_deployment(),
                clusters=[_cluster("cluster-b", gateway_address="10.0.0.2")],
                # cluster-a no longer exists - the orphan pin is dropped
                # and the slot is filled by re-placement on cluster-b.
                all_replicas=[_replica("my-model", "cluster-a")],
                want=[Candidate(name="cluster-b", gateway_address="10.0.0.2")],
            ),
        ]

        for case in cases:
            with self.subTest(case.name):
                got = scheduling.schedule(case.deployment, case.clusters, case.all_replicas)
                self.assertEqual(case.want, got, f"{case.name}: -want, +got")


if __name__ == "__main__":
    unittest.main()
