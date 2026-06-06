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


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    """Tests for FunctionRunner.RunFunction."""

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:
        """The function composes an InferenceCluster."""
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
                                "capacity": {
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
                                "capacity": {
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
                                "capacity": {
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
                                "capacity": {
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
                                    "kubernetesVersion": "1.31",
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
                # The composed VPC's real name carries a provider-generated
                # suffix; the StorageClass must pin to THIS, not the bare XR name.
                "network": {"name": "test-cluster-abc12"},
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
                                "capacity": {
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
                                },
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
                    "storage-class-rwx": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ClusterProviderConfig",
                                        "name": "test-cluster-cluster-kubeconfig-d0f89",
                                    },
                                    "readiness": {"policy": "SuccessfulCreate"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "storage.k8s.io/v1",
                                            "kind": "StorageClass",
                                            "metadata": {"name": "modelplane-rwx"},
                                            "provisioner": "filestore.csi.storage.gke.io",
                                            "parameters": {
                                                "tier": "enterprise",
                                                "network": "test-cluster-abc12",
                                            },
                                            "volumeBindingMode": "Immediate",
                                            "allowVolumeExpansion": True,
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

        cases = [
            Case(name="existing cluster with secrets composes backend and CPC", req=req1, want=want1),
            Case(name="GKE cluster first pass composes GKECluster XR only", req=req2, want=want2),
            Case(name="existing cluster second pass with backend ready", req=req3, want=want3),
            Case(name="EKS cluster first pass composes EKSCluster XR only", req=req4, want=want4),
            Case(name="EKS cluster not ready re-emits existing CPC unchanged", req=req5, want=want5),
            Case(name="GKE cluster ready composes CPC, backend, usage, and RWX StorageClass", req=req6, want=want6),
        ]

        for case in cases:
            with self.subTest(case.name):
                got = await self.runner.RunFunction(case.req, None)
                self.assertEqual(
                    json_format.MessageToDict(case.want),
                    json_format.MessageToDict(got),
                    "-want, +got",
                )
