"""Tests for the compose-inference-cluster function."""

import dataclasses
import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.inferencecluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-inference-cluster."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


def _eks_ready_extras(want: fnv1.RunFunctionResponse, storage_class: str) -> None:
    """Apply the EKS-ready deltas on top of the EKS first-pass response: mark the
    EKSCluster ready and relay the backing cluster's status.cache up to the
    InferenceCluster's status.cache.storageClassName."""
    want.desired.resources["eks-cluster"].ready = fnv1.READY_TRUE
    status = want.desired.composite.resource.fields["status"].struct_value
    status.fields["cache"].struct_value.fields["storageClassName"].string_value = storage_class


def _replicas_selector(cluster_name: str) -> fnv1.ResourceSelector:
    """The ModelReplica guard requirement: replicas scheduled to a cluster,
    across all namespaces."""
    sel = fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelReplica")
    sel.match_labels.labels.update({"modelplane.ai/cluster": cluster_name})
    return sel


def _replica_item(name: str, namespace: str) -> fnv1.Resource:
    """An observed ModelReplica labelled for test-cluster."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "ModelReplica",
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "labels": {"modelplane.ai/cluster": "test-cluster"},
                },
            }
        )
    )


def _guard_clusterusage() -> fnv1.Resource:
    """The reason-only ClusterUsage the guard composes for test-cluster."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "ClusterUsage",
                "spec": {
                    "of": {
                        "apiVersion": "modelplane.ai/v1alpha1",
                        "kind": "InferenceCluster",
                        "resourceRef": {"name": "test-cluster"},
                    },
                    "reason": "ModelReplicas are scheduled to this InferenceCluster",
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _replica_guard_case(
    base_req: fnv1.RunFunctionRequest, base_want: fnv1.RunFunctionResponse
) -> tuple[fnv1.RunFunctionRequest, fnv1.RunFunctionResponse]:
    """Build the replica-guard case from a base request and response.

    Observes two ModelReplicas in different namespaces, both labelled for the
    cluster, so the function composes a single reason-only ClusterUsage blocking
    the InferenceCluster's deletion regardless of replica count or namespace.
    """
    req = fnv1.RunFunctionRequest()
    req.CopyFrom(base_req)
    req.required_resources["model-replicas"].items.append(_replica_item("deploy-test-cluster-0", "team-a"))
    req.required_resources["model-replicas"].items.append(_replica_item("deploy-test-cluster-0", "team-b"))

    want = fnv1.RunFunctionResponse()
    want.CopyFrom(base_want)
    want.desired.resources["usage-replicas"].CopyFrom(_guard_clusterusage())
    return req, want


def _empty_replicas_case(
    base_req: fnv1.RunFunctionRequest, base_want: fnv1.RunFunctionResponse
) -> tuple[fnv1.RunFunctionRequest, fnv1.RunFunctionResponse]:
    """The guard requirement resolved to zero replicas: no ClusterUsage.

    This is the teardown transition - the last replica is gone, so the function
    stops composing the guard and the cluster becomes deletable. base_want must
    not contain usage-replicas.
    """
    req = fnv1.RunFunctionRequest()
    req.CopyFrom(base_req)
    # An empty-but-present requirement, as Crossplane returns when the selector
    # matched nothing.
    req.required_resources["model-replicas"].ClearField("items")
    return req, base_want


def _early_return_guard_case() -> tuple[fnv1.RunFunctionRequest, fnv1.RunFunctionResponse]:
    """The guard is composed even when compose() returns early.

    resolve_classes() returns False whenever a referenced InferenceClass isn't
    observed yet - a routine transient. The function returns before composing
    the cluster, but the guard runs first, so a referencing replica still blocks
    deletion. This is the case that regresses if the guard is gated behind class
    resolution or cluster source.
    """
    xr = v1alpha1.InferenceCluster(
        metadata=metav1.ObjectMeta(name="test-cluster", namespace="modelplane-system"),
        spec=v1alpha1.Spec(
            cluster=v1alpha1.Cluster(
                source="Existing",
                existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
            ),
            nodePools=[v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4)],
        ),
    )
    # The class requirement is declared but not fulfilled, so resolve_classes
    # gates and compose() returns early.
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(resource=resource.dict_to_struct(xr.model_dump(exclude_none=True, mode="json")))
        ),
    )
    req.required_resources["model-replicas"].items.append(_replica_item("deploy-test-cluster-0", "team-a"))

    want = fnv1.RunFunctionResponse(
        meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
        desired=fnv1.State(resources={"usage-replicas": _guard_clusterusage()}),
        context=structpb.Struct(),
    )
    want.requirements.resources["model-replicas"].CopyFrom(_replicas_selector("test-cluster"))
    want.requirements.resources["class-gpu-l4"].CopyFrom(
        fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4")
    )
    want.conditions.append(
        fnv1.Condition(
            type="ClusterReady",
            status=fnv1.STATUS_CONDITION_FALSE,
            reason="WaitingForClasses",
            message="Waiting for InferenceClasses: gpu-l4",
        )
    )
    want.results.append(fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for InferenceClasses: gpu-l4"))
    return req, want


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    """Tests for FunctionRunner.RunFunction."""

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:  # noqa: PLR0915
        """The function composes an InferenceCluster.

        Many table entries, each exercising a distinct compose path across the
        GKE, EKS, and Existing sources, push this over the statement limit.
        """
        # Shared InferenceClass resource for required_resources.
        inference_class_l4 = {
            "apiVersion": "modelplane.ai/v1alpha1",
            "kind": "InferenceClass",
            "metadata": {"name": "gpu-l4"},
            "spec": {
                "devices": [
                    {
                        "name": "gpu",
                        "claim": "DRA",
                        "driver": "gpu.nvidia.com",
                        "deviceClassName": "gpu.nvidia.com",
                        "count": 1,
                        "capacity": {"memory": {"value": "24Gi"}},
                    },
                ],
                "provisioning": {
                    "provider": "GKE",
                    "gke": {
                        "machineType": "g2-standard-48",
                        "diskSizeGb": 100,
                        "accelerator": {
                            "type": "nvidia-l4",
                            "count": 1,
                        },
                    },
                },
            },
        }

        # Shared resource selector for class requirement.
        class_selector = fnv1.ResourceSelector(
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceClass",
            match_name="gpu-l4",
        )

        # --- Case 1: Existing cluster with secrets composes backend and CPC. ---
        req1 = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        v1alpha1.InferenceCluster(
                            metadata=metav1.ObjectMeta(
                                name="test-cluster",
                                namespace="modelplane-system",
                            ),
                            spec=v1alpha1.Spec(
                                cluster=v1alpha1.Cluster(
                                    source="Existing",
                                    existing=v1alpha1.Existing(
                                        secretRef=v1alpha1.SecretRef(name="my-kubeconfig"),
                                    ),
                                ),
                                nodePools=[
                                    v1alpha1.NodePool(
                                        name="l4-pool",
                                        className="gpu-l4",
                                        nodeCount=2,
                                        maxNodeCount=4,
                                    ),
                                ],
                            ),
                        ).model_dump(exclude_none=True, mode="json")
                    ),
                ),
            ),
        )
        req1.required_resources["class-gpu-l4"].items.append(
            fnv1.Resource(resource=resource.dict_to_struct(inference_class_l4))
        )

        want1 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "status": {
                                "providerConfigRef": {
                                    "name": "test-cluster-cluster-kubeconfig-d0f89",
                                },
                                "namespace": "modelplane-system",
                                "gpuPools": [
                                    {
                                        "name": "l4-pool",
                                        "nodes": 4,
                                        "devices": [
                                            {
                                                "name": "gpu",
                                                "claim": "DRA",
                                                "driver": "gpu.nvidia.com",
                                                "deviceClassName": "gpu.nvidia.com",
                                                "count": 1,
                                                "capacity": {"memory": {"value": "24Gi"}},
                                            },
                                        ],
                                    },
                                ],
                            },
                        }
                    ),
                ),
                resources={
                    "cluster-provider-config-kubernetes": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "ClusterProviderConfig",
                                "metadata": {"name": "test-cluster-cluster-kubeconfig-d0f89"},
                                "spec": {
                                    "credentials": {
                                        "source": "Secret",
                                        "secretRef": {
                                            "namespace": "modelplane-system",
                                            "name": "my-kubeconfig",
                                            "key": "kubeconfig",
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "serving-stack": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "ServingStack",
                                "metadata": {
                                    "name": "test-cluster-serving-stack-fd00b",
                                    "namespace": "modelplane-system",
                                },
                                "spec": {
                                    "secrets": [
                                        {
                                            "type": "Kubeconfig",
                                            "name": "my-kubeconfig",
                                            "key": "kubeconfig",
                                        },
                                    ],
                                },
                            }
                        ),
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="ClusterRunning",
                ),
                fnv1.Condition(
                    type="BackendReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Installing",
                ),
            ],
            context=structpb.Struct(),
        )
        want1.requirements.resources["class-gpu-l4"].CopyFrom(class_selector)

        # --- Case 2: GKE cluster first pass - no observed GKE, classes resolved. ---
        req2 = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        v1alpha1.InferenceCluster(
                            metadata=metav1.ObjectMeta(
                                name="test-cluster",
                                namespace="modelplane-system",
                            ),
                            spec=v1alpha1.Spec(
                                cluster=v1alpha1.Cluster(
                                    source="GKE",
                                    gke=v1alpha1.Gke(
                                        project="my-gcp-project",
                                        region="us-central1",
                                    ),
                                ),
                                nodePools=[
                                    v1alpha1.NodePool(
                                        name="l4-pool",
                                        className="gpu-l4",
                                        nodeCount=2,
                                        maxNodeCount=4,
                                        zones=["us-central1-a"],
                                    ),
                                ],
                            ),
                        ).model_dump(exclude_none=True, mode="json")
                    ),
                ),
            ),
        )
        req2.required_resources["class-gpu-l4"].items.append(
            fnv1.Resource(resource=resource.dict_to_struct(inference_class_l4))
        )

        want2 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "status": {
                                "providerConfigRef": {
                                    "name": "test-cluster-cluster-kubeconfig-d0f89",
                                },
                                "namespace": "modelplane-system",
                                "gpuPools": [
                                    {
                                        "name": "l4-pool",
                                        "nodes": 4,
                                        "devices": [
                                            {
                                                "name": "gpu",
                                                "claim": "DRA",
                                                "driver": "gpu.nvidia.com",
                                                "deviceClassName": "gpu.nvidia.com",
                                                "count": 1,
                                                "capacity": {"memory": {"value": "24Gi"}},
                                            },
                                        ],
                                    },
                                ],
                            },
                        }
                    ),
                ),
                resources={
                    "gke-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "GKECluster",
                                "metadata": {
                                    "name": "test-cluster",
                                    "namespace": "modelplane-system",
                                },
                                "spec": {
                                    "project": "my-gcp-project",
                                    "region": "us-central1",
                                    "kubernetesVersion": "1.35",
                                    "nodePools": [
                                        {
                                            "name": "l4-pool",
                                            "role": "GPU",
                                            "machineType": "g2-standard-48",
                                            "nodeCount": 2,
                                            "minNodeCount": None,
                                            "maxNodeCount": 4,
                                            "diskSizeGb": 100,
                                            "gpu": {
                                                "acceleratorType": "nvidia-l4",
                                                "acceleratorCount": 1,
                                            },
                                            "zones": ["us-central1-a"],
                                        },
                                    ],
                                },
                            }
                        ),
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                ),
                fnv1.Condition(
                    type="BackendReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForCluster",
                ),
            ],
            context=structpb.Struct(),
        )
        want2.requirements.resources["class-gpu-l4"].CopyFrom(class_selector)

        # --- Case 3: Existing cluster second pass - backend observed ready. ---
        req3 = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        v1alpha1.InferenceCluster(
                            metadata=metav1.ObjectMeta(
                                name="test-cluster",
                                namespace="modelplane-system",
                            ),
                            spec=v1alpha1.Spec(
                                cluster=v1alpha1.Cluster(
                                    source="Existing",
                                    existing=v1alpha1.Existing(
                                        secretRef=v1alpha1.SecretRef(name="my-kubeconfig"),
                                    ),
                                ),
                                nodePools=[
                                    v1alpha1.NodePool(
                                        name="l4-pool",
                                        className="gpu-l4",
                                        nodeCount=2,
                                        maxNodeCount=4,
                                    ),
                                ],
                            ),
                        ).model_dump(exclude_none=True, mode="json")
                    ),
                ),
                resources={
                    "serving-stack": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "ServingStack",
                                "metadata": {"name": "test-cluster-serving-stack-fd00b"},
                                "status": {
                                    "conditions": [{"type": "Ready", "status": "True"}],
                                    "gateway": {"address": "34.55.100.10"},
                                },
                            }
                        ),
                    ),
                },
            ),
        )
        req3.required_resources["class-gpu-l4"].items.append(
            fnv1.Resource(resource=resource.dict_to_struct(inference_class_l4))
        )

        want3 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "status": {
                                "providerConfigRef": {
                                    "name": "test-cluster-cluster-kubeconfig-d0f89",
                                },
                                "namespace": "modelplane-system",
                                "gpuPools": [
                                    {
                                        "name": "l4-pool",
                                        "nodes": 4,
                                        "devices": [
                                            {
                                                "name": "gpu",
                                                "claim": "DRA",
                                                "driver": "gpu.nvidia.com",
                                                "deviceClassName": "gpu.nvidia.com",
                                                "count": 1,
                                                "capacity": {"memory": {"value": "24Gi"}},
                                            },
                                        ],
                                    },
                                ],
                                "gateway": {"address": "34.55.100.10"},
                            },
                        }
                    ),
                ),
                resources={
                    "cluster-provider-config-kubernetes": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "ClusterProviderConfig",
                                "metadata": {"name": "test-cluster-cluster-kubeconfig-d0f89"},
                                "spec": {
                                    "credentials": {
                                        "source": "Secret",
                                        "secretRef": {
                                            "namespace": "modelplane-system",
                                            "name": "my-kubeconfig",
                                            "key": "kubeconfig",
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "serving-stack": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "ServingStack",
                                "metadata": {
                                    "name": "test-cluster-serving-stack-fd00b",
                                    "namespace": "modelplane-system",
                                },
                                "spec": {
                                    "secrets": [
                                        {
                                            "type": "Kubeconfig",
                                            "name": "my-kubeconfig",
                                            "key": "kubeconfig",
                                        },
                                    ],
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="ClusterRunning",
                ),
                fnv1.Condition(
                    type="BackendReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="BackendHealthy",
                ),
            ],
            context=structpb.Struct(),
        )
        want3.requirements.resources["class-gpu-l4"].CopyFrom(class_selector)

        # --- Case 4: EKS cluster first pass - no observed EKS, classes resolved. ---
        inference_class_l4_eks = {
            "apiVersion": "modelplane.ai/v1alpha1",
            "kind": "InferenceClass",
            "metadata": {"name": "gpu-l4-eks"},
            "spec": {
                "devices": [
                    {
                        "name": "gpu",
                        "claim": "DRA",
                        "driver": "gpu.nvidia.com",
                        "deviceClassName": "gpu.nvidia.com",
                        "count": 1,
                        "capacity": {"memory": {"value": "24Gi"}},
                    },
                ],
                "provisioning": {
                    "provider": "EKS",
                    "eks": {
                        "instanceType": "g6.xlarge",
                        "diskSizeGb": 100,
                        "accelerator": {"type": "nvidia-l4", "count": 1},
                    },
                },
            },
        }
        class_selector_eks = fnv1.ResourceSelector(
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceClass",
            match_name="gpu-l4-eks",
        )

        req4 = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        v1alpha1.InferenceCluster(
                            metadata=metav1.ObjectMeta(
                                name="test-cluster",
                                namespace="modelplane-system",
                            ),
                            spec=v1alpha1.Spec(
                                cluster=v1alpha1.Cluster(
                                    source="EKS",
                                    eks=v1alpha1.Eks(region="us-west-2"),
                                ),
                                nodePools=[
                                    v1alpha1.NodePool(
                                        name="l4-pool",
                                        className="gpu-l4-eks",
                                        nodeCount=2,
                                        maxNodeCount=4,
                                        zones=["us-west-2a", "us-west-2b"],
                                    ),
                                ],
                            ),
                        ).model_dump(exclude_none=True, mode="json"),
                    ),
                ),
            ),
        )
        req4.required_resources["class-gpu-l4-eks"].items.append(
            fnv1.Resource(resource=resource.dict_to_struct(inference_class_l4_eks)),
        )

        want4 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "status": {
                                "providerConfigRef": {
                                    "name": "test-cluster-cluster-kubeconfig-d0f89",
                                },
                                "namespace": "modelplane-system",
                                "gpuPools": [
                                    {
                                        "name": "l4-pool",
                                        "nodes": 4,
                                        "devices": [
                                            {
                                                "name": "gpu",
                                                "claim": "DRA",
                                                "driver": "gpu.nvidia.com",
                                                "deviceClassName": "gpu.nvidia.com",
                                                "count": 1,
                                                "capacity": {"memory": {"value": "24Gi"}},
                                            },
                                        ],
                                    },
                                ],
                            },
                        },
                    ),
                ),
                resources={
                    "eks-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "EKSCluster",
                                "metadata": {
                                    "name": "test-cluster",
                                    "namespace": "modelplane-system",
                                },
                                "spec": {
                                    "region": "us-west-2",
                                    "kubernetesVersion": "1.36",
                                    "nodePools": [
                                        {
                                            "name": "l4-pool",
                                            "role": "GPU",
                                            "instanceType": "g6.xlarge",
                                            "nodeCount": 2,
                                            "minNodeCount": None,
                                            "maxNodeCount": 4,
                                            "diskSizeGb": 100,
                                            "gpu": {
                                                "acceleratorType": "nvidia-l4",
                                            },
                                            "zones": ["us-west-2a", "us-west-2b"],
                                        },
                                    ],
                                },
                            },
                        ),
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                ),
                fnv1.Condition(
                    type="BackendReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForCluster",
                ),
            ],
            context=structpb.Struct(),
        )
        want4.requirements.resources["class-gpu-l4-eks"].CopyFrom(class_selector_eks)

        # --- Case 8: EKS first pass with a node pool backed by a Capacity
        # Block. The reservation ID flows through to the EKSCluster node pool's
        # capacityBlock, which compose-eks-cluster turns into a CAPACITY_BLOCK
        # node group. ---
        req8 = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        v1alpha1.InferenceCluster(
                            metadata=metav1.ObjectMeta(
                                name="test-cluster",
                                namespace="modelplane-system",
                            ),
                            spec=v1alpha1.Spec(
                                cluster=v1alpha1.Cluster(
                                    source="EKS",
                                    eks=v1alpha1.Eks(region="us-west-2"),
                                ),
                                nodePools=[
                                    v1alpha1.NodePool(
                                        name="l4-pool",
                                        className="gpu-l4-eks",
                                        nodeCount=2,
                                        maxNodeCount=4,
                                        zones=["us-west-2a"],
                                        capacityBlock=v1alpha1.CapacityBlock(
                                            capacityReservationId="cr-0123456789abcdef0",
                                        ),
                                    ),
                                ],
                            ),
                        ).model_dump(exclude_none=True, mode="json"),
                    ),
                ),
            ),
        )
        req8.required_resources["class-gpu-l4-eks"].items.append(
            fnv1.Resource(resource=resource.dict_to_struct(inference_class_l4_eks)),
        )

        want8 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "status": {
                                "providerConfigRef": {
                                    "name": "test-cluster-cluster-kubeconfig-d0f89",
                                },
                                "namespace": "modelplane-system",
                                "gpuPools": [
                                    {
                                        "name": "l4-pool",
                                        "nodes": 4,
                                        "devices": [
                                            {
                                                "name": "gpu",
                                                "claim": "DRA",
                                                "driver": "gpu.nvidia.com",
                                                "deviceClassName": "gpu.nvidia.com",
                                                "count": 1,
                                                "capacity": {"memory": {"value": "24Gi"}},
                                            },
                                        ],
                                    },
                                ],
                            },
                        },
                    ),
                ),
                resources={
                    "eks-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "EKSCluster",
                                "metadata": {
                                    "name": "test-cluster",
                                    "namespace": "modelplane-system",
                                },
                                "spec": {
                                    "region": "us-west-2",
                                    "kubernetesVersion": "1.36",
                                    "nodePools": [
                                        {
                                            "name": "l4-pool",
                                            "role": "GPU",
                                            "instanceType": "g6.xlarge",
                                            "nodeCount": 2,
                                            "minNodeCount": None,
                                            "maxNodeCount": 4,
                                            "diskSizeGb": 100,
                                            "gpu": {
                                                "acceleratorType": "nvidia-l4",
                                            },
                                            "zones": ["us-west-2a"],
                                            "capacityBlock": {
                                                "capacityReservationId": "cr-0123456789abcdef0",
                                            },
                                        },
                                    ],
                                },
                            },
                        ),
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                ),
                fnv1.Condition(
                    type="BackendReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForCluster",
                ),
            ],
            context=structpb.Struct(),
        )
        want8.requirements.resources["class-gpu-l4-eks"].CopyFrom(class_selector_eks)

        # --- Case 9: EKS first pass with a node pool that opts into the EFA
        # fabric. fabric flows through to the EKSCluster node pool, which
        # compose-eks-cluster turns into EFA launch-template interfaces. ---
        req9 = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        v1alpha1.InferenceCluster(
                            metadata=metav1.ObjectMeta(
                                name="test-cluster",
                                namespace="modelplane-system",
                            ),
                            spec=v1alpha1.Spec(
                                cluster=v1alpha1.Cluster(
                                    source="EKS",
                                    eks=v1alpha1.Eks(region="us-west-2"),
                                ),
                                nodePools=[
                                    v1alpha1.NodePool(
                                        name="l4-pool",
                                        className="gpu-l4-eks",
                                        nodeCount=2,
                                        maxNodeCount=4,
                                        zones=["us-west-2a"],
                                        fabric="EFA",
                                    ),
                                ],
                            ),
                        ).model_dump(exclude_none=True, mode="json"),
                    ),
                ),
            ),
        )
        req9.required_resources["class-gpu-l4-eks"].items.append(
            fnv1.Resource(resource=resource.dict_to_struct(inference_class_l4_eks)),
        )

        want9 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "status": {
                                "providerConfigRef": {
                                    "name": "test-cluster-cluster-kubeconfig-d0f89",
                                },
                                "namespace": "modelplane-system",
                                "gpuPools": [
                                    {
                                        "name": "l4-pool",
                                        "nodes": 4,
                                        "devices": [
                                            {
                                                "name": "gpu",
                                                "claim": "DRA",
                                                "driver": "gpu.nvidia.com",
                                                "deviceClassName": "gpu.nvidia.com",
                                                "count": 1,
                                                "capacity": {"memory": {"value": "24Gi"}},
                                            },
                                        ],
                                    },
                                ],
                            },
                        },
                    ),
                ),
                resources={
                    "eks-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "EKSCluster",
                                "metadata": {
                                    "name": "test-cluster",
                                    "namespace": "modelplane-system",
                                },
                                "spec": {
                                    "region": "us-west-2",
                                    "kubernetesVersion": "1.36",
                                    "nodePools": [
                                        {
                                            "name": "l4-pool",
                                            "role": "GPU",
                                            "instanceType": "g6.xlarge",
                                            "nodeCount": 2,
                                            "minNodeCount": None,
                                            "maxNodeCount": 4,
                                            "diskSizeGb": 100,
                                            "gpu": {
                                                "acceleratorType": "nvidia-l4",
                                            },
                                            "zones": ["us-west-2a"],
                                            "fabric": "EFA",
                                        },
                                    ],
                                },
                            },
                        ),
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                ),
                fnv1.Condition(
                    type="BackendReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForCluster",
                ),
            ],
            context=structpb.Struct(),
        )
        want9.requirements.resources["class-gpu-l4-eks"].CopyFrom(class_selector_eks)

        # --- Case 5: EKS cluster not yet ready (no kubeconfig observed) but a
        # ClusterProviderConfig already exists from a prior reconcile. The CPC
        # is built only from the kubeconfig, so without one it's simply omitted
        # from desired state this reconcile (and recreated once the kubeconfig
        # is observed again) - it is never emitted with an empty secretRef.
        observed_cpc = {
            "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
            "kind": "ClusterProviderConfig",
            "metadata": {"name": "test-cluster-cluster-kubeconfig-d0f89"},
            "spec": {
                "credentials": {
                    "source": "Secret",
                    "secretRef": {
                        "namespace": "modelplane-system",
                        "name": "test-cluster-kubeconfig-abcde",
                        "key": "kubeconfig",
                    },
                },
            },
        }
        req5 = fnv1.RunFunctionRequest()
        req5.CopyFrom(req4)
        req5.observed.resources["cluster-provider-config-kubernetes"].CopyFrom(
            fnv1.Resource(resource=resource.dict_to_struct(observed_cpc)),
        )

        # Desired state is identical to case 4: no ClusterProviderConfig.
        want5 = fnv1.RunFunctionResponse()
        want5.CopyFrom(want4)

        # --- Case 6: GKE cluster ready - composes CPC, backend, usage, and the
        # VPC-pinned modelplane-rwx Filestore StorageClass on the workload
        # cluster (default cache storage class). ---
        observed_gke_ready = {
            "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
            "kind": "GKECluster",
            "metadata": {"name": "test-cluster", "namespace": "modelplane-system"},
            "spec": {
                "project": "my-gcp-project",
                "region": "us-central1",
                "nodePools": [{"name": "system", "role": "System", "machineType": "e2-standard-4"}],
            },
            "status": {
                "conditions": [
                    {
                        "type": "Ready",
                        "status": "True",
                        "reason": "Available",
                        "lastTransitionTime": "2026-06-08T00:00:00Z",
                    },
                ],
                # The backing GKECluster reports its effective RWX StorageClass;
                # the InferenceCluster relays it up to its own status.cache.
                "cache": {"storageClassName": "modelplane-rwx"},
                "secrets": [
                    {"type": "Kubeconfig", "name": "test-cluster-kubeconfig-abcde", "key": "kubeconfig"},
                    {"type": "GCPServiceAccountKey", "name": "test-cluster-sa-key-fghij", "key": "credentials.json"},
                ],
            },
        }
        req6 = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        v1alpha1.InferenceCluster(
                            metadata=metav1.ObjectMeta(
                                name="test-cluster",
                                namespace="modelplane-system",
                            ),
                            spec=v1alpha1.Spec(
                                cluster=v1alpha1.Cluster(
                                    source="GKE",
                                    gke=v1alpha1.Gke(
                                        project="my-gcp-project",
                                        region="us-central1",
                                    ),
                                ),
                                nodePools=[
                                    v1alpha1.NodePool(
                                        name="l4-pool",
                                        className="gpu-l4",
                                        nodeCount=2,
                                        maxNodeCount=4,
                                        zones=["us-central1-a"],
                                    ),
                                ],
                            ),
                        ).model_dump(exclude_none=True, mode="json")
                    ),
                ),
                resources={
                    "gke-cluster": fnv1.Resource(resource=resource.dict_to_struct(observed_gke_ready)),
                },
            ),
        )
        req6.required_resources["class-gpu-l4"].items.append(
            fnv1.Resource(resource=resource.dict_to_struct(inference_class_l4))
        )

        want6 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "status": {
                                "providerConfigRef": {
                                    "name": "test-cluster-cluster-kubeconfig-d0f89",
                                },
                                "namespace": "modelplane-system",
                                "gpuPools": [
                                    {
                                        "name": "l4-pool",
                                        "nodes": 4,
                                        "devices": [
                                            {
                                                "name": "gpu",
                                                "claim": "DRA",
                                                "driver": "gpu.nvidia.com",
                                                "deviceClassName": "gpu.nvidia.com",
                                                "count": 1,
                                                "capacity": {"memory": {"value": "24Gi"}},
                                            }
                                        ],
                                    },
                                ],
                                # Relayed from the backing GKECluster's status.cache.
                                "cache": {"storageClassName": "modelplane-rwx"},
                            },
                        }
                    ),
                ),
                resources={
                    "gke-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "GKECluster",
                                "metadata": {
                                    "name": "test-cluster",
                                    "namespace": "modelplane-system",
                                },
                                "spec": {
                                    "project": "my-gcp-project",
                                    "region": "us-central1",
                                    "kubernetesVersion": "1.35",
                                    "nodePools": [
                                        {
                                            "name": "l4-pool",
                                            "role": "GPU",
                                            "machineType": "g2-standard-48",
                                            "nodeCount": 2,
                                            "minNodeCount": None,
                                            "maxNodeCount": 4,
                                            "diskSizeGb": 100,
                                            "gpu": {
                                                "acceleratorType": "nvidia-l4",
                                                "acceleratorCount": 1,
                                            },
                                            "zones": ["us-central1-a"],
                                        },
                                    ],
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "cluster-provider-config-kubernetes": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "ClusterProviderConfig",
                                "metadata": {"name": "test-cluster-cluster-kubeconfig-d0f89"},
                                "spec": {
                                    "credentials": {
                                        "source": "Secret",
                                        "secretRef": {
                                            "namespace": "modelplane-system",
                                            "name": "test-cluster-kubeconfig-abcde",
                                            "key": "kubeconfig",
                                        },
                                    },
                                    "identity": {
                                        "type": "GoogleApplicationCredentials",
                                        "source": "Secret",
                                        "secretRef": {
                                            "namespace": "modelplane-system",
                                            "name": "test-cluster-sa-key-fghij",
                                            "key": "credentials.json",
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "serving-stack": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "ServingStack",
                                "metadata": {
                                    "name": "test-cluster-serving-stack-fd00b",
                                    "namespace": "modelplane-system",
                                },
                                "spec": {
                                    "secrets": [
                                        {
                                            "type": "Kubeconfig",
                                            "name": "test-cluster-kubeconfig-abcde",
                                            "key": "kubeconfig",
                                        },
                                        {
                                            "type": "GCPServiceAccountKey",
                                            "name": "test-cluster-sa-key-fghij",
                                            "key": "credentials.json",
                                        },
                                    ],
                                    "nvidiaDriverRoot": "/home/kubernetes/bin/nvidia",
                                },
                            }
                        ),
                    ),
                    "usage-gke-by-backend": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "of": {
                                        "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                        "kind": "GKECluster",
                                        "resourceSelector": {"matchControllerRef": True},
                                    },
                                    "by": {
                                        "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                        "kind": "ServingStack",
                                        "resourceSelector": {"matchControllerRef": True},
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="ClusterRunning",
                ),
                fnv1.Condition(
                    type="BackendReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Installing",
                ),
            ],
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="GKE cluster ready, composing backend",
                ),
            ],
            context=structpb.Struct(),
        )
        want6.requirements.resources["class-gpu-l4"].CopyFrom(class_selector)

        # --- Case 7: EKS cluster ready - kubeconfig observed on the EKSCluster
        # status. The function wires the ClusterProviderConfig, composes the
        # ServingStack backend, and emits the Usage that blocks EKSCluster
        # deletion until the ServingStack is gone. ---
        req7 = fnv1.RunFunctionRequest()
        req7.CopyFrom(req4)
        req7.observed.resources["eks-cluster"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {
                        "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                        "kind": "EKSCluster",
                        "metadata": {"name": "test-cluster", "namespace": "modelplane-system"},
                        "spec": {
                            "region": "us-west-2",
                            "nodePools": [
                                {
                                    "name": "l4-pool",
                                    "role": "GPU",
                                    "instanceType": "g6.xlarge",
                                    "nodeCount": 2,
                                },
                            ],
                        },
                        "status": {
                            "conditions": [
                                {
                                    "type": "Ready",
                                    "status": "True",
                                    "reason": "Available",
                                    "lastTransitionTime": "2024-01-01T00:00:00Z",
                                },
                            ],
                            "secrets": [
                                {
                                    "type": "Kubeconfig",
                                    "name": "test-cluster-kubeconfig-abcde",
                                    "key": "kubeconfig",
                                },
                            ],
                            # The backing EKSCluster reports its effective RWX
                            # StorageClass; the InferenceCluster relays it up.
                            "cache": {"storageClassName": "modelplane-rwx-efs"},
                        },
                    }
                ),
            ),
        )

        want7 = fnv1.RunFunctionResponse()
        want7.CopyFrom(want4)
        # Mark the EKSCluster ready and relay its status.cache up to status.cache.
        _eks_ready_extras(want7, "modelplane-rwx-efs")
        want7.desired.resources["cluster-provider-config-kubernetes"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "ClusterProviderConfig",
                        "metadata": {"name": "test-cluster-cluster-kubeconfig-d0f89"},
                        "spec": {
                            "credentials": {
                                "source": "Secret",
                                "secretRef": {
                                    "namespace": "modelplane-system",
                                    "name": "test-cluster-kubeconfig-abcde",
                                    "key": "kubeconfig",
                                },
                            },
                        },
                    }
                ),
                ready=fnv1.READY_TRUE,
            ),
        )
        want7.desired.resources["serving-stack"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {
                        "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                        "kind": "ServingStack",
                        "metadata": {
                            "name": "test-cluster-serving-stack-fd00b",
                            "namespace": "modelplane-system",
                        },
                        "spec": {
                            "secrets": [
                                {
                                    "type": "Kubeconfig",
                                    "name": "test-cluster-kubeconfig-abcde",
                                    "key": "kubeconfig",
                                },
                            ],
                        },
                    }
                ),
            ),
        )
        want7.desired.resources["usage-eks-by-backend"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {
                        "apiVersion": "protection.crossplane.io/v1beta1",
                        "kind": "Usage",
                        "metadata": {"namespace": "modelplane-system"},
                        "spec": {
                            "of": {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "EKSCluster",
                                "resourceSelector": {"matchControllerRef": True},
                            },
                            "by": {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "ServingStack",
                                "resourceSelector": {"matchControllerRef": True},
                            },
                            "replayDeletion": True,
                        },
                    }
                ),
                ready=fnv1.READY_TRUE,
            ),
        )
        del want7.conditions[:]
        want7.conditions.extend(
            [
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="ClusterRunning",
                ),
                fnv1.Condition(
                    type="BackendReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Installing",
                ),
            ]
        )
        want7.results.append(
            fnv1.Result(
                severity=fnv1.SEVERITY_NORMAL,
                message="EKS cluster ready, composing backend",
            )
        )

        # Every compose path emits the ModelReplica guard requirement.
        for want in (want1, want2, want3, want4, want5, want6, want7, want8, want9):
            want.requirements.resources["model-replicas"].CopyFrom(_replicas_selector("test-cluster"))

        # The guard cases reuse case 1's request and response.
        guard_cases = [
            Case(
                "ModelReplicas scheduled to the cluster compose the deletion guard", *_replica_guard_case(req1, want1)
            ),
            Case("no ModelReplicas leaves the cluster deletable", *_empty_replicas_case(req1, want1)),
            Case("guard is composed even when compose returns early", *_early_return_guard_case()),
        ]

        cases = [
            Case(name="existing cluster with secrets composes backend and CPC", req=req1, want=want1),
            Case(name="GKE cluster first pass composes GKECluster XR only", req=req2, want=want2),
            Case(name="existing cluster second pass with backend ready", req=req3, want=want3),
            Case(name="EKS cluster first pass composes EKSCluster XR only", req=req4, want=want4),
            Case(name="EKS cluster not ready re-emits existing CPC unchanged", req=req5, want=want5),
            Case(name="GKE cluster ready composes CPC, backend, usage, and RWX StorageClass", req=req6, want=want6),
            Case(name="EKS cluster ready composes ServingStack and Usage", req=req7, want=want7),
            Case(
                name="EKS node pool with a Capacity Block sets capacityBlock on the EKSCluster pool",
                req=req8,
                want=want8,
            ),
            Case(
                name="EKS node pool with fabric EFA sets fabric on the EKSCluster pool",
                req=req9,
                want=want9,
            ),
            *guard_cases,
        ]

        for case in cases:
            with self.subTest(case.name):
                got = await self.runner.RunFunction(case.req, None)
                self.assertEqual(
                    json_format.MessageToDict(case.want),
                    json_format.MessageToDict(got),
                    "-want, +got",
                )
