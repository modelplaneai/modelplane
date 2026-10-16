# Copyright 2026 The Modelplane Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the compose-inference-cluster function."""

import asyncio
import dataclasses
import json

import pytest
from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format, message
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.inferencecluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class ComposeCase:
    """A test case for RunFunction."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


@dataclasses.dataclass
class GatewayHostnameCase:
    """A test case for _gateway_hostname."""

    name: str
    reason: str
    cluster_name: str
    want: str


def _inference_cluster(*, cluster: v1alpha1.Cluster, node_pools: list[v1alpha1.NodePool] | None) -> fnv1.Resource:
    """The observed InferenceCluster XR, test-cluster, with node_pools unless they're None."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            v1alpha1.InferenceCluster(
                metadata=metav1.ObjectMeta(name="test-cluster", namespace="modelplane-system"),
                spec=v1alpha1.Spec(cluster=cluster, nodePools=node_pools),
            ).model_dump(exclude_none=True, mode="json", by_alias=True)
        ),
    )


def _desired_inference_cluster(*, gpu_pools: list[dict], cache: dict | None, gateway: dict | None) -> fnv1.Resource:
    """The desired InferenceCluster XR's status, with its cache and gateway unless they're None."""
    status: dict = {
        "providerConfigRef": {"name": "test-cluster-cluster-kubeconfig-d0f89"},
        "namespace": "modelplane-system",
        "gpuPools": gpu_pools,
        "routers": ["EndpointPicker"],
    }
    if cache is not None:
        status["cache"] = cache
    if gateway is not None:
        status["gateway"] = gateway
    return fnv1.Resource(resource=resource.dict_to_struct({"status": status}))


def _inference_class(*, name: str, count: int, memory: str, provisioning: dict) -> fnv1.Resource:
    """An InferenceClass of count DRA-claimed NVIDIA GPUs with memory each, as its class requirement returns it."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "InferenceClass",
                "metadata": {"name": name},
                "spec": {
                    "devices": [
                        {
                            "name": "gpu",
                            "claim": "DRA",
                            "driver": "gpu.nvidia.com",
                            "deviceClassName": "gpu.nvidia.com",
                            "count": count,
                            "capacity": {"memory": {"value": memory}},
                        },
                    ],
                    "provisioning": provisioning,
                },
            }
        )
    )


def _inference_gateway(*, name: str, cluster: str, status: dict | None) -> fnv1.Resource:
    """An InferenceGateway on cluster, as the gateways requirement returns it, with status unless it's None."""
    gateway: dict = {
        "apiVersion": "modelplane.ai/v1alpha1",
        "kind": "InferenceGateway",
        "metadata": {"name": name},
        "spec": {"clusterName": cluster},
    }
    if status is not None:
        gateway["status"] = status
    return fnv1.Resource(resource=resource.dict_to_struct(gateway))


def _model_cache(*, name: str, namespace: str, cluster: str) -> fnv1.Resource:
    """A ModelCache staged onto cluster, as the model-caches requirement returns it."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "ModelCache",
                "metadata": {"name": name, "namespace": namespace},
                "spec": {"source": "HuggingFace"},
                "status": {"clusters": [{"name": cluster, "phase": "Ready"}]},
            }
        )
    )


def _observed_activation_policy(*, activated: list[str]) -> fnv1.Resource:
    """The observed ManagedResourceActivationPolicy, reporting the kinds it has activated."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "apiextensions.crossplane.io/v1alpha1",
                "kind": "ManagedResourceActivationPolicy",
                "status": {"activated": activated},
            }
        )
    )


def _observed_serving_stack(*, gateway: dict) -> fnv1.Resource:
    """The observed ServingStack, Ready, with the gateway status it has published."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                "kind": "ServingStack",
                "metadata": {"name": "test-cluster-serving-stack-fd00b"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}], "gateway": gateway},
            }
        )
    )


def _activation_policy(*, activate: list[str], ready: fnv1.Ready) -> fnv1.Resource:
    """The composed ManagedResourceActivationPolicy, activating a cloud's managed resource kinds."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "apiextensions.crossplane.io/v1alpha1",
                "kind": "ManagedResourceActivationPolicy",
                "spec": {"activate": activate},
            }
        ),
        ready=ready,
    )


def _gke_cluster(*, credentials: dict | None, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed GKECluster with l4-pool, using credentials unless they're None."""
    spec: dict = {
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
                "gpu": {"acceleratorType": "nvidia-l4", "acceleratorCount": 1},
                "zones": ["us-central1-a"],
            },
        ],
    }
    if credentials is not None:
        spec["credentials"] = credentials
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                "kind": "GKECluster",
                "metadata": {"name": "test-cluster", "namespace": "modelplane-system"},
                "spec": spec,
            }
        ),
        ready=ready,
    )


def _eks_cluster(
    *, zones: list[str], capacity_block: dict | None, fabric: str | None, ready: fnv1.Ready
) -> fnv1.Resource:
    """The composed EKSCluster with l4-pool in zones, setting its capacityBlock and fabric unless they're None."""
    pool: dict = {
        "name": "l4-pool",
        "role": "GPU",
        "instanceType": "g6.xlarge",
        "nodeCount": 2,
        "minNodeCount": None,
        "maxNodeCount": 4,
        "diskSizeGb": 100,
        "gpu": {"acceleratorType": "nvidia-l4"},
        "zones": zones,
    }
    if capacity_block is not None:
        pool["capacityBlock"] = capacity_block
    if fabric is not None:
        pool["fabric"] = fabric
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                "kind": "EKSCluster",
                "metadata": {"name": "test-cluster", "namespace": "modelplane-system"},
                "spec": {"region": "us-west-2", "kubernetesVersion": "1.36", "nodePools": [pool]},
            }
        ),
        ready=ready,
    )


def _vultr_cluster(*, credentials: dict | None, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed VultrCluster with l40s-pool, using credentials unless they're None."""
    spec: dict = {
        "region": "ewr",
        "kubernetesVersion": "v1.36.2+1",
        "nodePools": [
            {
                "name": "l40s-pool",
                "role": "GPU",
                "plan": "vcg-l40s-16c-180g-48vram",
                "nodeCount": 2,
                "maxNodeCount": 4,
                "gpu": {"acceleratorType": "nvidia-l40s"},
            },
        ],
    }
    if credentials is not None:
        spec["credentials"] = credentials
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                "kind": "VultrCluster",
                "metadata": {"name": "test-cluster", "namespace": "modelplane-system"},
                "spec": spec,
            }
        ),
        ready=ready,
    )


def _civo_cluster(*, node_pools: list[dict], credentials: dict | None, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed CivoCluster in LON1 with node_pools, using credentials unless they're None."""
    spec: dict = {"region": "LON1", "nodePools": node_pools}
    if credentials is not None:
        spec["credentials"] = credentials
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                "kind": "CivoCluster",
                "metadata": {"name": "test-cluster", "namespace": "modelplane-system"},
                "spec": spec,
            }
        ),
        ready=ready,
    )


def _cluster_provider_config(*, kubeconfig: str, identity: dict | None) -> fnv1.Resource:
    """The ClusterProviderConfig reaching test-cluster with the kubeconfig Secret, as identity unless it's None."""
    spec: dict = {
        "credentials": {
            "source": "Secret",
            "secretRef": {"namespace": "modelplane-system", "name": kubeconfig, "key": "kubeconfig"},
        },
    }
    if identity is not None:
        spec["identity"] = identity
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "ClusterProviderConfig",
                "metadata": {"name": "test-cluster-cluster-kubeconfig-d0f89"},
                "spec": spec,
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _serving_stack(
    *, cloud: str, secrets: list[dict], client_cas: list[dict] | None, gpu: dict | None, ready: fnv1.Ready
) -> fnv1.Resource:
    """The composed ServingStack, its gateway accepting client_cas and its spec.gpu set to gpu unless they're None."""
    gateway: dict = {"hostname": "gateway-test-cluster-09532.modelplane-system.svc.cluster.local"}
    if client_cas is not None:
        gateway["clientCAs"] = client_cas
    spec: dict = {"cloud": cloud, "gateway": gateway, "stack": "Standard", "secrets": secrets}
    if gpu is not None:
        spec["gpu"] = gpu
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                "kind": "ServingStack",
                "metadata": {"name": "test-cluster-serving-stack-fd00b", "namespace": "modelplane-system"},
                "spec": spec,
            }
        ),
        ready=ready,
    )


def _backend_usage(*, cluster_kind: str) -> fnv1.Resource:
    """The composed Usage holding the cluster_kind cluster XR until the ServingStack is gone."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "of": {
                        "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                        "kind": cluster_kind,
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
    )


def _guard_cluster_usage(*, reason: str) -> fnv1.Resource:
    """The reason-only ClusterUsage the deletion guard composes for test-cluster."""
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
                    "reason": reason,
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _namespace_object(*, team: str, name: str) -> fnv1.Resource:
    """The composed Object that mirrors team's namespace onto the cluster as the Namespace name."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "managementPolicies": ["Observe", "Create", "Update"],
                    "providerConfigRef": {
                        "kind": "ClusterProviderConfig",
                        "name": "test-cluster-cluster-kubeconfig-d0f89",
                    },
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "v1",
                            "kind": "Namespace",
                            "metadata": {"name": name, "labels": {"modelplane.ai/namespace": team}},
                        },
                    },
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


# Every want requires what the function reads, before it composes anything: the
# ModelReplicas and ModelRoutes labelled for test-cluster, across all
# namespaces; every ModelCache, since a cache fans out to many clusters and so
# can't be label-selected to one, leaving the function to filter by
# status.clusters[]; every InferenceGateway, since the cluster gateway accepts
# client certificates from each of their CAs, which is how an InferenceGateway
# proves itself; and the InferenceClass behind each node pool.
#
# A cloud cluster's case observes its ManagedResourceActivationPolicy with every
# kind in status.activated, so the function composes the cluster XR rather than
# waiting for activation, unless the case says otherwise.
#
# gateway-test-cluster-09532.modelplane-system.svc.cluster.local is the internal
# name Modelplane derives for test-cluster's gateway, which
# compose-inference-gateway resolves.
COMPOSE_CASES = [
    ComposeCase(
        name="ExistingCluster",
        reason="An Existing cluster composes a ClusterProviderConfig and a ServingStack from its kubeconfig Secret.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l4",
                            count=1,
                            memory="24Gi",
                            provisioning={
                                "provider": "GKE",
                                "gke": {
                                    "machineType": "g2-standard-48",
                                    "diskSizeGb": 100,
                                    "accelerator": {"type": "nvidia-l4", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
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
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Existing",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        client_cas=None,
                        gpu=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    ComposeCase(
        name="ExistingNonGCPIdentity",
        reason="An Existing cluster threads its non-GCP identity's type into the ClusterProviderConfig and the ServingStack's secrets.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(
                            secretRef=v1alpha1.SecretRef(name="my-kubeconfig"),
                            identitySecretRef=v1alpha1.IdentitySecretRef(
                                name="nebius-creds",
                                key="credentials.json",
                                type="NebiusServiceAccountCredentials",
                            ),
                        ),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l4",
                            count=1,
                            memory="24Gi",
                            provisioning={
                                "provider": "GKE",
                                "gke": {
                                    "machineType": "g2-standard-48",
                                    "diskSizeGb": 100,
                                    "accelerator": {"type": "nvidia-l4", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
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
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig",
                        identity={
                            "type": "NebiusServiceAccountCredentials",
                            "source": "Secret",
                            "secretRef": {
                                "namespace": "modelplane-system",
                                "name": "nebius-creds",
                                "key": "credentials.json",
                            },
                        },
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Existing",
                        secrets=[
                            {"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"},
                            {
                                "type": "NebiusServiceAccountCredentials",
                                "name": "nebius-creds",
                                "key": "credentials.json",
                            },
                        ],
                        client_cas=None,
                        gpu=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    ComposeCase(
        name="GKEFirstPass",
        reason="With no GKECluster observed yet, a GKE cluster composes only the activation policy and the GKECluster.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(source="GKE", gke=v1alpha1.Gke(region="us-central1")),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="l4-pool",
                            className="gpu-l4",
                            nodeCount=2,
                            maxNodeCount=4,
                            zones=["us-central1-a"],
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation_policy(
                        activated=[
                            "projectiammembers.cloudplatform.gcp.m.upbound.io",
                            "projectservices.cloudplatform.gcp.m.upbound.io",
                            "serviceaccounts.cloudplatform.gcp.m.upbound.io",
                            "serviceaccountkeys.cloudplatform.gcp.m.upbound.io",
                            "networks.compute.gcp.m.upbound.io",
                            "subnetworks.compute.gcp.m.upbound.io",
                            "clusters.container.gcp.m.upbound.io",
                            "nodepools.container.gcp.m.upbound.io",
                        ]
                    ),
                },
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l4",
                            count=1,
                            memory="24Gi",
                            provisioning={
                                "provider": "GKE",
                                "gke": {
                                    "machineType": "g2-standard-48",
                                    "diskSizeGb": 100,
                                    "accelerator": {"type": "nvidia-l4", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
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
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "activation": _activation_policy(
                        activate=[
                            "projectiammembers.cloudplatform.gcp.m.upbound.io",
                            "projectservices.cloudplatform.gcp.m.upbound.io",
                            "serviceaccounts.cloudplatform.gcp.m.upbound.io",
                            "serviceaccountkeys.cloudplatform.gcp.m.upbound.io",
                            "networks.compute.gcp.m.upbound.io",
                            "subnetworks.compute.gcp.m.upbound.io",
                            "clusters.container.gcp.m.upbound.io",
                            "nodepools.container.gcp.m.upbound.io",
                        ],
                        ready=fnv1.READY_TRUE,
                    ),
                    "gke-cluster": _gke_cluster(credentials=None, ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Provisioning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForCluster"),
            ],
        ),
    ),
    ComposeCase(
        name="GKECredentials",
        reason="A GKE cluster's credentials pass through to the GKECluster's spec.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="GKE",
                        gke=v1alpha1.Gke(
                            region="us-central1",
                            credentials=v1alpha1.Credentials(type="ProviderConfig", name="my-gcp-account"),
                        ),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="l4-pool",
                            className="gpu-l4",
                            nodeCount=2,
                            maxNodeCount=4,
                            zones=["us-central1-a"],
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation_policy(
                        activated=[
                            "projectiammembers.cloudplatform.gcp.m.upbound.io",
                            "projectservices.cloudplatform.gcp.m.upbound.io",
                            "serviceaccounts.cloudplatform.gcp.m.upbound.io",
                            "serviceaccountkeys.cloudplatform.gcp.m.upbound.io",
                            "networks.compute.gcp.m.upbound.io",
                            "subnetworks.compute.gcp.m.upbound.io",
                            "clusters.container.gcp.m.upbound.io",
                            "nodepools.container.gcp.m.upbound.io",
                        ]
                    ),
                },
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l4",
                            count=1,
                            memory="24Gi",
                            provisioning={
                                "provider": "GKE",
                                "gke": {
                                    "machineType": "g2-standard-48",
                                    "diskSizeGb": 100,
                                    "accelerator": {"type": "nvidia-l4", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
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
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "activation": _activation_policy(
                        activate=[
                            "projectiammembers.cloudplatform.gcp.m.upbound.io",
                            "projectservices.cloudplatform.gcp.m.upbound.io",
                            "serviceaccounts.cloudplatform.gcp.m.upbound.io",
                            "serviceaccountkeys.cloudplatform.gcp.m.upbound.io",
                            "networks.compute.gcp.m.upbound.io",
                            "subnetworks.compute.gcp.m.upbound.io",
                            "clusters.container.gcp.m.upbound.io",
                            "nodepools.container.gcp.m.upbound.io",
                        ],
                        ready=fnv1.READY_TRUE,
                    ),
                    "gke-cluster": _gke_cluster(
                        credentials={"type": "ProviderConfig", "name": "my-gcp-account"}, ready=fnv1.READY_UNSPECIFIED
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Provisioning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForCluster"),
            ],
        ),
    ),
    ComposeCase(
        name="ExistingBackendReady",
        reason="Once its ServingStack is observed Ready with an address, an Existing cluster relays the address and reports its backend healthy.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
                resources={
                    "serving-stack": _observed_serving_stack(gateway={"address": "34.55.100.10"}),
                },
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l4",
                            count=1,
                            memory="24Gi",
                            provisioning={
                                "provider": "GKE",
                                "gke": {
                                    "machineType": "g2-standard-48",
                                    "diskSizeGb": 100,
                                    "accelerator": {"type": "nvidia-l4", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
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
                    cache=None,
                    gateway={"address": "34.55.100.10"},
                ),
                resources={
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Existing",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        client_cas=None,
                        gpu=None,
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_TRUE, reason="BackendHealthy"),
            ],
        ),
    ),
    ComposeCase(
        name="EKSFirstPass",
        reason="With no EKSCluster observed yet, an EKS cluster composes only the activation policy and the EKSCluster.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(source="EKS", eks=v1alpha1.Eks(region="us-west-2")),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="l4-pool",
                            className="gpu-l4-eks",
                            nodeCount=2,
                            maxNodeCount=4,
                            zones=["us-west-2a", "us-west-2b"],
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation_policy(
                        activated=[
                            "eips.ec2.aws.m.upbound.io",
                            "internetgateways.ec2.aws.m.upbound.io",
                            "launchtemplates.ec2.aws.m.upbound.io",
                            "natgateways.ec2.aws.m.upbound.io",
                            "routes.ec2.aws.m.upbound.io",
                            "routetables.ec2.aws.m.upbound.io",
                            "routetableassociations.ec2.aws.m.upbound.io",
                            "securitygroups.ec2.aws.m.upbound.io",
                            "securitygroupegressrules.ec2.aws.m.upbound.io",
                            "securitygroupingressrules.ec2.aws.m.upbound.io",
                            "subnets.ec2.aws.m.upbound.io",
                            "vpcs.ec2.aws.m.upbound.io",
                            "filesystems.efs.aws.m.upbound.io",
                            "mounttargets.efs.aws.m.upbound.io",
                            "addons.eks.aws.m.upbound.io",
                            "clusters.eks.aws.m.upbound.io",
                            "clusterauths.eks.aws.m.upbound.io",
                            "nodegroups.eks.aws.m.upbound.io",
                            "podidentityassociations.eks.aws.m.upbound.io",
                            "policies.iam.aws.m.upbound.io",
                            "roles.iam.aws.m.upbound.io",
                            "rolepolicyattachments.iam.aws.m.upbound.io",
                        ]
                    ),
                },
            ),
            required_resources={
                "class-gpu-l4-eks": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l4-eks",
                            count=1,
                            memory="24Gi",
                            provisioning={
                                "provider": "EKS",
                                "eks": {
                                    "instanceType": "g6.xlarge",
                                    "diskSizeGb": 100,
                                    "accelerator": {"type": "nvidia-l4", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
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
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "activation": _activation_policy(
                        activate=[
                            "eips.ec2.aws.m.upbound.io",
                            "internetgateways.ec2.aws.m.upbound.io",
                            "launchtemplates.ec2.aws.m.upbound.io",
                            "natgateways.ec2.aws.m.upbound.io",
                            "routes.ec2.aws.m.upbound.io",
                            "routetables.ec2.aws.m.upbound.io",
                            "routetableassociations.ec2.aws.m.upbound.io",
                            "securitygroups.ec2.aws.m.upbound.io",
                            "securitygroupegressrules.ec2.aws.m.upbound.io",
                            "securitygroupingressrules.ec2.aws.m.upbound.io",
                            "subnets.ec2.aws.m.upbound.io",
                            "vpcs.ec2.aws.m.upbound.io",
                            "filesystems.efs.aws.m.upbound.io",
                            "mounttargets.efs.aws.m.upbound.io",
                            "addons.eks.aws.m.upbound.io",
                            "clusters.eks.aws.m.upbound.io",
                            "clusterauths.eks.aws.m.upbound.io",
                            "nodegroups.eks.aws.m.upbound.io",
                            "podidentityassociations.eks.aws.m.upbound.io",
                            "policies.iam.aws.m.upbound.io",
                            "roles.iam.aws.m.upbound.io",
                            "rolepolicyattachments.iam.aws.m.upbound.io",
                        ],
                        ready=fnv1.READY_TRUE,
                    ),
                    "eks-cluster": _eks_cluster(
                        zones=["us-west-2a", "us-west-2b"],
                        capacity_block=None,
                        fabric=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4-eks": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4-eks"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Provisioning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForCluster"),
            ],
        ),
    ),
    # The ClusterProviderConfig is built only from the kubeconfig, so it's
    # recreated once the kubeconfig is observed again. It is never emitted with
    # an empty secretRef.
    ComposeCase(
        name="EKSObservedCPC",
        reason="With a ClusterProviderConfig observed but no EKSCluster yet, an EKS cluster leaves the ClusterProviderConfig out of desired state.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(source="EKS", eks=v1alpha1.Eks(region="us-west-2")),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="l4-pool",
                            className="gpu-l4-eks",
                            nodeCount=2,
                            maxNodeCount=4,
                            zones=["us-west-2a", "us-west-2b"],
                        ),
                    ],
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
                                            "name": "test-cluster-kubeconfig-abcde",
                                            "key": "kubeconfig",
                                        },
                                    },
                                },
                            }
                        ),
                    ),
                    "activation": _observed_activation_policy(
                        activated=[
                            "eips.ec2.aws.m.upbound.io",
                            "internetgateways.ec2.aws.m.upbound.io",
                            "launchtemplates.ec2.aws.m.upbound.io",
                            "natgateways.ec2.aws.m.upbound.io",
                            "routes.ec2.aws.m.upbound.io",
                            "routetables.ec2.aws.m.upbound.io",
                            "routetableassociations.ec2.aws.m.upbound.io",
                            "securitygroups.ec2.aws.m.upbound.io",
                            "securitygroupegressrules.ec2.aws.m.upbound.io",
                            "securitygroupingressrules.ec2.aws.m.upbound.io",
                            "subnets.ec2.aws.m.upbound.io",
                            "vpcs.ec2.aws.m.upbound.io",
                            "filesystems.efs.aws.m.upbound.io",
                            "mounttargets.efs.aws.m.upbound.io",
                            "addons.eks.aws.m.upbound.io",
                            "clusters.eks.aws.m.upbound.io",
                            "clusterauths.eks.aws.m.upbound.io",
                            "nodegroups.eks.aws.m.upbound.io",
                            "podidentityassociations.eks.aws.m.upbound.io",
                            "policies.iam.aws.m.upbound.io",
                            "roles.iam.aws.m.upbound.io",
                            "rolepolicyattachments.iam.aws.m.upbound.io",
                        ]
                    ),
                },
            ),
            required_resources={
                "class-gpu-l4-eks": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l4-eks",
                            count=1,
                            memory="24Gi",
                            provisioning={
                                "provider": "EKS",
                                "eks": {
                                    "instanceType": "g6.xlarge",
                                    "diskSizeGb": 100,
                                    "accelerator": {"type": "nvidia-l4", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
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
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "activation": _activation_policy(
                        activate=[
                            "eips.ec2.aws.m.upbound.io",
                            "internetgateways.ec2.aws.m.upbound.io",
                            "launchtemplates.ec2.aws.m.upbound.io",
                            "natgateways.ec2.aws.m.upbound.io",
                            "routes.ec2.aws.m.upbound.io",
                            "routetables.ec2.aws.m.upbound.io",
                            "routetableassociations.ec2.aws.m.upbound.io",
                            "securitygroups.ec2.aws.m.upbound.io",
                            "securitygroupegressrules.ec2.aws.m.upbound.io",
                            "securitygroupingressrules.ec2.aws.m.upbound.io",
                            "subnets.ec2.aws.m.upbound.io",
                            "vpcs.ec2.aws.m.upbound.io",
                            "filesystems.efs.aws.m.upbound.io",
                            "mounttargets.efs.aws.m.upbound.io",
                            "addons.eks.aws.m.upbound.io",
                            "clusters.eks.aws.m.upbound.io",
                            "clusterauths.eks.aws.m.upbound.io",
                            "nodegroups.eks.aws.m.upbound.io",
                            "podidentityassociations.eks.aws.m.upbound.io",
                            "policies.iam.aws.m.upbound.io",
                            "roles.iam.aws.m.upbound.io",
                            "rolepolicyattachments.iam.aws.m.upbound.io",
                        ],
                        ready=fnv1.READY_TRUE,
                    ),
                    "eks-cluster": _eks_cluster(
                        zones=["us-west-2a", "us-west-2b"],
                        capacity_block=None,
                        fabric=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4-eks": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4-eks"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Provisioning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForCluster"),
            ],
        ),
    ),
    # The GKE kubeconfig has no embedded credentials, hence the service account
    # identity on the ClusterProviderConfig.
    ComposeCase(
        name="GKEReady",
        reason="Once its GKECluster is Ready, a GKE cluster composes the ClusterProviderConfig, ServingStack and Usage, and relays its RWX StorageClass.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(source="GKE", gke=v1alpha1.Gke(region="us-central1")),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="l4-pool",
                            className="gpu-l4",
                            nodeCount=2,
                            maxNodeCount=4,
                            zones=["us-central1-a"],
                        ),
                    ],
                ),
                resources={
                    "gke-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "GKECluster",
                                "metadata": {"name": "test-cluster", "namespace": "modelplane-system"},
                                "spec": {
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
                                    "cache": {"storageClassName": "modelplane-rwx"},
                                    "secrets": [
                                        {
                                            "type": "Kubeconfig",
                                            "name": "test-cluster-kubeconfig-abcde",
                                            "key": "kubeconfig",
                                        },
                                        {
                                            "type": "GoogleApplicationCredentials",
                                            "name": "test-cluster-sa-key-fghij",
                                            "key": "credentials.json",
                                        },
                                    ],
                                },
                            }
                        ),
                    ),
                    "activation": _observed_activation_policy(
                        activated=[
                            "projectiammembers.cloudplatform.gcp.m.upbound.io",
                            "projectservices.cloudplatform.gcp.m.upbound.io",
                            "serviceaccounts.cloudplatform.gcp.m.upbound.io",
                            "serviceaccountkeys.cloudplatform.gcp.m.upbound.io",
                            "networks.compute.gcp.m.upbound.io",
                            "subnetworks.compute.gcp.m.upbound.io",
                            "clusters.container.gcp.m.upbound.io",
                            "nodepools.container.gcp.m.upbound.io",
                        ]
                    ),
                },
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l4",
                            count=1,
                            memory="24Gi",
                            provisioning={
                                "provider": "GKE",
                                "gke": {
                                    "machineType": "g2-standard-48",
                                    "diskSizeGb": 100,
                                    "accelerator": {"type": "nvidia-l4", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
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
                    cache={"storageClassName": "modelplane-rwx"},
                    gateway=None,
                ),
                resources={
                    "activation": _activation_policy(
                        activate=[
                            "projectiammembers.cloudplatform.gcp.m.upbound.io",
                            "projectservices.cloudplatform.gcp.m.upbound.io",
                            "serviceaccounts.cloudplatform.gcp.m.upbound.io",
                            "serviceaccountkeys.cloudplatform.gcp.m.upbound.io",
                            "networks.compute.gcp.m.upbound.io",
                            "subnetworks.compute.gcp.m.upbound.io",
                            "clusters.container.gcp.m.upbound.io",
                            "nodepools.container.gcp.m.upbound.io",
                        ],
                        ready=fnv1.READY_TRUE,
                    ),
                    "gke-cluster": _gke_cluster(credentials=None, ready=fnv1.READY_TRUE),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="test-cluster-kubeconfig-abcde",
                        identity={
                            "type": "GoogleApplicationCredentials",
                            "source": "Secret",
                            "secretRef": {
                                "namespace": "modelplane-system",
                                "name": "test-cluster-sa-key-fghij",
                                "key": "credentials.json",
                            },
                        },
                    ),
                    "serving-stack": _serving_stack(
                        cloud="GKE",
                        secrets=[
                            {"type": "Kubeconfig", "name": "test-cluster-kubeconfig-abcde", "key": "kubeconfig"},
                            {
                                "type": "GoogleApplicationCredentials",
                                "name": "test-cluster-sa-key-fghij",
                                "key": "credentials.json",
                            },
                        ],
                        client_cas=None,
                        gpu=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "usage-gke-by-backend": _backend_usage(cluster_kind="GKECluster"),
                },
            ),
            results=[fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="GKE cluster ready, composing backend")],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    ComposeCase(
        name="EKSReady",
        reason="Once its EKSCluster is Ready, an EKS cluster composes the ClusterProviderConfig, ServingStack and Usage, and relays its status.cache.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(source="EKS", eks=v1alpha1.Eks(region="us-west-2")),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="l4-pool",
                            className="gpu-l4-eks",
                            nodeCount=2,
                            maxNodeCount=4,
                            zones=["us-west-2a", "us-west-2b"],
                        ),
                    ],
                ),
                resources={
                    "eks-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "EKSCluster",
                                "metadata": {"name": "test-cluster", "namespace": "modelplane-system"},
                                "spec": {
                                    "region": "us-west-2",
                                    "nodePools": [
                                        {"name": "l4-pool", "role": "GPU", "instanceType": "g6.xlarge", "nodeCount": 2},
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
                                    "cache": {"storageClassName": "modelplane-rwx-efs"},
                                },
                            }
                        ),
                    ),
                    "activation": _observed_activation_policy(
                        activated=[
                            "eips.ec2.aws.m.upbound.io",
                            "internetgateways.ec2.aws.m.upbound.io",
                            "launchtemplates.ec2.aws.m.upbound.io",
                            "natgateways.ec2.aws.m.upbound.io",
                            "routes.ec2.aws.m.upbound.io",
                            "routetables.ec2.aws.m.upbound.io",
                            "routetableassociations.ec2.aws.m.upbound.io",
                            "securitygroups.ec2.aws.m.upbound.io",
                            "securitygroupegressrules.ec2.aws.m.upbound.io",
                            "securitygroupingressrules.ec2.aws.m.upbound.io",
                            "subnets.ec2.aws.m.upbound.io",
                            "vpcs.ec2.aws.m.upbound.io",
                            "filesystems.efs.aws.m.upbound.io",
                            "mounttargets.efs.aws.m.upbound.io",
                            "addons.eks.aws.m.upbound.io",
                            "clusters.eks.aws.m.upbound.io",
                            "clusterauths.eks.aws.m.upbound.io",
                            "nodegroups.eks.aws.m.upbound.io",
                            "podidentityassociations.eks.aws.m.upbound.io",
                            "policies.iam.aws.m.upbound.io",
                            "roles.iam.aws.m.upbound.io",
                            "rolepolicyattachments.iam.aws.m.upbound.io",
                        ]
                    ),
                },
            ),
            required_resources={
                "class-gpu-l4-eks": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l4-eks",
                            count=1,
                            memory="24Gi",
                            provisioning={
                                "provider": "EKS",
                                "eks": {
                                    "instanceType": "g6.xlarge",
                                    "diskSizeGb": 100,
                                    "accelerator": {"type": "nvidia-l4", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
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
                    cache={"storageClassName": "modelplane-rwx-efs"},
                    gateway=None,
                ),
                resources={
                    "activation": _activation_policy(
                        activate=[
                            "eips.ec2.aws.m.upbound.io",
                            "internetgateways.ec2.aws.m.upbound.io",
                            "launchtemplates.ec2.aws.m.upbound.io",
                            "natgateways.ec2.aws.m.upbound.io",
                            "routes.ec2.aws.m.upbound.io",
                            "routetables.ec2.aws.m.upbound.io",
                            "routetableassociations.ec2.aws.m.upbound.io",
                            "securitygroups.ec2.aws.m.upbound.io",
                            "securitygroupegressrules.ec2.aws.m.upbound.io",
                            "securitygroupingressrules.ec2.aws.m.upbound.io",
                            "subnets.ec2.aws.m.upbound.io",
                            "vpcs.ec2.aws.m.upbound.io",
                            "filesystems.efs.aws.m.upbound.io",
                            "mounttargets.efs.aws.m.upbound.io",
                            "addons.eks.aws.m.upbound.io",
                            "clusters.eks.aws.m.upbound.io",
                            "clusterauths.eks.aws.m.upbound.io",
                            "nodegroups.eks.aws.m.upbound.io",
                            "podidentityassociations.eks.aws.m.upbound.io",
                            "policies.iam.aws.m.upbound.io",
                            "roles.iam.aws.m.upbound.io",
                            "rolepolicyattachments.iam.aws.m.upbound.io",
                        ],
                        ready=fnv1.READY_TRUE,
                    ),
                    "eks-cluster": _eks_cluster(
                        zones=["us-west-2a", "us-west-2b"], capacity_block=None, fabric=None, ready=fnv1.READY_TRUE
                    ),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="test-cluster-kubeconfig-abcde", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="EKS",
                        secrets=[{"type": "Kubeconfig", "name": "test-cluster-kubeconfig-abcde", "key": "kubeconfig"}],
                        client_cas=None,
                        gpu=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "usage-eks-by-backend": _backend_usage(cluster_kind="EKSCluster"),
                },
            ),
            results=[fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="EKS cluster ready, composing backend")],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4-eks": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4-eks"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    # compose-eks-cluster turns the pool's capacityBlock into a CAPACITY_BLOCK
    # node group.
    ComposeCase(
        name="EKSCapacityBlock",
        reason="A node pool backed by a Capacity Block passes its reservation ID through to the EKSCluster pool's capacityBlock.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(source="EKS", eks=v1alpha1.Eks(region="us-west-2")),
                    node_pools=[
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
                resources={
                    "activation": _observed_activation_policy(
                        activated=[
                            "eips.ec2.aws.m.upbound.io",
                            "internetgateways.ec2.aws.m.upbound.io",
                            "launchtemplates.ec2.aws.m.upbound.io",
                            "natgateways.ec2.aws.m.upbound.io",
                            "routes.ec2.aws.m.upbound.io",
                            "routetables.ec2.aws.m.upbound.io",
                            "routetableassociations.ec2.aws.m.upbound.io",
                            "securitygroups.ec2.aws.m.upbound.io",
                            "securitygroupegressrules.ec2.aws.m.upbound.io",
                            "securitygroupingressrules.ec2.aws.m.upbound.io",
                            "subnets.ec2.aws.m.upbound.io",
                            "vpcs.ec2.aws.m.upbound.io",
                            "filesystems.efs.aws.m.upbound.io",
                            "mounttargets.efs.aws.m.upbound.io",
                            "addons.eks.aws.m.upbound.io",
                            "clusters.eks.aws.m.upbound.io",
                            "clusterauths.eks.aws.m.upbound.io",
                            "nodegroups.eks.aws.m.upbound.io",
                            "podidentityassociations.eks.aws.m.upbound.io",
                            "policies.iam.aws.m.upbound.io",
                            "roles.iam.aws.m.upbound.io",
                            "rolepolicyattachments.iam.aws.m.upbound.io",
                        ]
                    ),
                },
            ),
            required_resources={
                "class-gpu-l4-eks": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l4-eks",
                            count=1,
                            memory="24Gi",
                            provisioning={
                                "provider": "EKS",
                                "eks": {
                                    "instanceType": "g6.xlarge",
                                    "diskSizeGb": 100,
                                    "accelerator": {"type": "nvidia-l4", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
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
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "activation": _activation_policy(
                        activate=[
                            "eips.ec2.aws.m.upbound.io",
                            "internetgateways.ec2.aws.m.upbound.io",
                            "launchtemplates.ec2.aws.m.upbound.io",
                            "natgateways.ec2.aws.m.upbound.io",
                            "routes.ec2.aws.m.upbound.io",
                            "routetables.ec2.aws.m.upbound.io",
                            "routetableassociations.ec2.aws.m.upbound.io",
                            "securitygroups.ec2.aws.m.upbound.io",
                            "securitygroupegressrules.ec2.aws.m.upbound.io",
                            "securitygroupingressrules.ec2.aws.m.upbound.io",
                            "subnets.ec2.aws.m.upbound.io",
                            "vpcs.ec2.aws.m.upbound.io",
                            "filesystems.efs.aws.m.upbound.io",
                            "mounttargets.efs.aws.m.upbound.io",
                            "addons.eks.aws.m.upbound.io",
                            "clusters.eks.aws.m.upbound.io",
                            "clusterauths.eks.aws.m.upbound.io",
                            "nodegroups.eks.aws.m.upbound.io",
                            "podidentityassociations.eks.aws.m.upbound.io",
                            "policies.iam.aws.m.upbound.io",
                            "roles.iam.aws.m.upbound.io",
                            "rolepolicyattachments.iam.aws.m.upbound.io",
                        ],
                        ready=fnv1.READY_TRUE,
                    ),
                    "eks-cluster": _eks_cluster(
                        zones=["us-west-2a"],
                        capacity_block={"capacityReservationId": "cr-0123456789abcdef0"},
                        fabric=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4-eks": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4-eks"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Provisioning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForCluster"),
            ],
        ),
    ),
    # compose-eks-cluster turns the pool's fabric into EFA launch-template
    # interfaces.
    ComposeCase(
        name="EKSFabricEFA",
        reason="A node pool that opts into the EFA fabric passes fabric.type through to the EKSCluster pool.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(source="EKS", eks=v1alpha1.Eks(region="us-west-2")),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="l4-pool",
                            className="gpu-l4-eks",
                            nodeCount=2,
                            maxNodeCount=4,
                            zones=["us-west-2a"],
                            fabric=v1alpha1.Fabric(type="EFA"),
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation_policy(
                        activated=[
                            "eips.ec2.aws.m.upbound.io",
                            "internetgateways.ec2.aws.m.upbound.io",
                            "launchtemplates.ec2.aws.m.upbound.io",
                            "natgateways.ec2.aws.m.upbound.io",
                            "routes.ec2.aws.m.upbound.io",
                            "routetables.ec2.aws.m.upbound.io",
                            "routetableassociations.ec2.aws.m.upbound.io",
                            "securitygroups.ec2.aws.m.upbound.io",
                            "securitygroupegressrules.ec2.aws.m.upbound.io",
                            "securitygroupingressrules.ec2.aws.m.upbound.io",
                            "subnets.ec2.aws.m.upbound.io",
                            "vpcs.ec2.aws.m.upbound.io",
                            "filesystems.efs.aws.m.upbound.io",
                            "mounttargets.efs.aws.m.upbound.io",
                            "addons.eks.aws.m.upbound.io",
                            "clusters.eks.aws.m.upbound.io",
                            "clusterauths.eks.aws.m.upbound.io",
                            "nodegroups.eks.aws.m.upbound.io",
                            "podidentityassociations.eks.aws.m.upbound.io",
                            "policies.iam.aws.m.upbound.io",
                            "roles.iam.aws.m.upbound.io",
                            "rolepolicyattachments.iam.aws.m.upbound.io",
                        ]
                    ),
                },
            ),
            required_resources={
                "class-gpu-l4-eks": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l4-eks",
                            count=1,
                            memory="24Gi",
                            provisioning={
                                "provider": "EKS",
                                "eks": {
                                    "instanceType": "g6.xlarge",
                                    "diskSizeGb": 100,
                                    "accelerator": {"type": "nvidia-l4", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
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
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "activation": _activation_policy(
                        activate=[
                            "eips.ec2.aws.m.upbound.io",
                            "internetgateways.ec2.aws.m.upbound.io",
                            "launchtemplates.ec2.aws.m.upbound.io",
                            "natgateways.ec2.aws.m.upbound.io",
                            "routes.ec2.aws.m.upbound.io",
                            "routetables.ec2.aws.m.upbound.io",
                            "routetableassociations.ec2.aws.m.upbound.io",
                            "securitygroups.ec2.aws.m.upbound.io",
                            "securitygroupegressrules.ec2.aws.m.upbound.io",
                            "securitygroupingressrules.ec2.aws.m.upbound.io",
                            "subnets.ec2.aws.m.upbound.io",
                            "vpcs.ec2.aws.m.upbound.io",
                            "filesystems.efs.aws.m.upbound.io",
                            "mounttargets.efs.aws.m.upbound.io",
                            "addons.eks.aws.m.upbound.io",
                            "clusters.eks.aws.m.upbound.io",
                            "clusterauths.eks.aws.m.upbound.io",
                            "nodegroups.eks.aws.m.upbound.io",
                            "podidentityassociations.eks.aws.m.upbound.io",
                            "policies.iam.aws.m.upbound.io",
                            "roles.iam.aws.m.upbound.io",
                            "rolepolicyattachments.iam.aws.m.upbound.io",
                        ],
                        ready=fnv1.READY_TRUE,
                    ),
                    "eks-cluster": _eks_cluster(
                        zones=["us-west-2a"], capacity_block=None, fabric="EFA", ready=fnv1.READY_UNSPECIFIED
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4-eks": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4-eks"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Provisioning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForCluster"),
            ],
        ),
    ),
    # minNodeCount stays unset so the pool's autoscaling floor defaults to its
    # node count downstream.
    ComposeCase(
        name="NebiusFirstPass",
        reason="With no NebiusCluster observed yet, a Nebius cluster composes only the activation policy and a NebiusCluster carrying its pool's InfiniBand fabric.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(source="Nebius", nebius=v1alpha1.Nebius()),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="h100-pool",
                            className="gpu-h100-nebius",
                            nodeCount=2,
                            maxNodeCount=4,
                            fabric=v1alpha1.Fabric(
                                type="InfiniBand",
                                infiniband=v1alpha1.Infiniband(fabric="fabric-2"),
                            ),
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation_policy(
                        activated=[
                            "filesystems.compute.nebius.m.upbound.io",
                            "gpuclusters.compute.nebius.m.upbound.io",
                            "clusters.mk8s.nebius.m.upbound.io",
                            "nodegroups.mk8s.nebius.m.upbound.io",
                            "networks.vpc.nebius.m.upbound.io",
                            "subnets.vpc.nebius.m.upbound.io",
                        ]
                    ),
                },
            ),
            required_resources={
                "class-gpu-h100-nebius": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-h100-nebius",
                            count=8,
                            memory="81559Mi",
                            provisioning={
                                "provider": "Nebius",
                                "nebius": {
                                    "platform": "gpu-h100-sxm",
                                    "preset": "8gpu-128vcpu-1600gb",
                                    "diskSizeGb": 200,
                                    "accelerator": {"type": "nvidia-h100", "count": 8},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
                        {
                            "name": "h100-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 8,
                                    "capacity": {"memory": {"value": "81559Mi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "activation": _activation_policy(
                        activate=[
                            "filesystems.compute.nebius.m.upbound.io",
                            "gpuclusters.compute.nebius.m.upbound.io",
                            "clusters.mk8s.nebius.m.upbound.io",
                            "nodegroups.mk8s.nebius.m.upbound.io",
                            "networks.vpc.nebius.m.upbound.io",
                            "subnets.vpc.nebius.m.upbound.io",
                        ],
                        ready=fnv1.READY_TRUE,
                    ),
                    "nebius-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "NebiusCluster",
                                "metadata": {"name": "test-cluster", "namespace": "modelplane-system"},
                                "spec": {
                                    "kubernetesVersion": "1.34",
                                    "nodePools": [
                                        {
                                            "name": "h100-pool",
                                            "role": "GPU",
                                            "platform": "gpu-h100-sxm",
                                            "preset": "8gpu-128vcpu-1600gb",
                                            "diskSizeGb": 200,
                                            "nodeCount": 2,
                                            "maxNodeCount": 4,
                                            "gpu": {"acceleratorType": "nvidia-h100", "driversPreset": "cuda13.0"},
                                            "fabric": {"type": "InfiniBand", "infiniband": {"fabric": "fabric-2"}},
                                        },
                                    ],
                                },
                            }
                        ),
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-h100-nebius": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-h100-nebius"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Provisioning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForCluster"),
            ],
        ),
    ),
    # The mk8s kubeconfig has no embedded credentials, hence the identity. The
    # credentials Secret carries a namespace: it is the Nebius
    # ClusterProviderConfig's Secret, which lives outside modelplane-system.
    ComposeCase(
        name="NebiusReady",
        reason="Once its NebiusCluster is Ready, a Nebius cluster composes a ClusterProviderConfig with the Nebius identity, the ServingStack and the Usage.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(source="Nebius", nebius=v1alpha1.Nebius()),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="h100-pool",
                            className="gpu-h100-nebius",
                            nodeCount=2,
                            maxNodeCount=4,
                            fabric=v1alpha1.Fabric(
                                type="InfiniBand",
                                infiniband=v1alpha1.Infiniband(fabric="fabric-2"),
                            ),
                        ),
                    ],
                ),
                resources={
                    "nebius-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "NebiusCluster",
                                "metadata": {"name": "test-cluster", "namespace": "modelplane-system"},
                                "spec": {
                                    "nodePools": [
                                        {
                                            "name": "h100-pool",
                                            "role": "GPU",
                                            "platform": "gpu-h100-sxm",
                                            "preset": "8gpu-128vcpu-1600gb",
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
                                        {
                                            "type": "NebiusServiceAccountCredentials",
                                            "name": "nebius-credentials",
                                            "key": "credentials.json",
                                            "namespace": "crossplane-system",
                                        },
                                    ],
                                },
                            }
                        ),
                    ),
                    "activation": _observed_activation_policy(
                        activated=[
                            "filesystems.compute.nebius.m.upbound.io",
                            "gpuclusters.compute.nebius.m.upbound.io",
                            "clusters.mk8s.nebius.m.upbound.io",
                            "nodegroups.mk8s.nebius.m.upbound.io",
                            "networks.vpc.nebius.m.upbound.io",
                            "subnets.vpc.nebius.m.upbound.io",
                        ]
                    ),
                },
            ),
            required_resources={
                "class-gpu-h100-nebius": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-h100-nebius",
                            count=8,
                            memory="81559Mi",
                            provisioning={
                                "provider": "Nebius",
                                "nebius": {
                                    "platform": "gpu-h100-sxm",
                                    "preset": "8gpu-128vcpu-1600gb",
                                    "diskSizeGb": 200,
                                    "accelerator": {"type": "nvidia-h100", "count": 8},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
                        {
                            "name": "h100-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 8,
                                    "capacity": {"memory": {"value": "81559Mi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "activation": _activation_policy(
                        activate=[
                            "filesystems.compute.nebius.m.upbound.io",
                            "gpuclusters.compute.nebius.m.upbound.io",
                            "clusters.mk8s.nebius.m.upbound.io",
                            "nodegroups.mk8s.nebius.m.upbound.io",
                            "networks.vpc.nebius.m.upbound.io",
                            "subnets.vpc.nebius.m.upbound.io",
                        ],
                        ready=fnv1.READY_TRUE,
                    ),
                    "nebius-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "NebiusCluster",
                                "metadata": {"name": "test-cluster", "namespace": "modelplane-system"},
                                "spec": {
                                    "kubernetesVersion": "1.34",
                                    "nodePools": [
                                        {
                                            "name": "h100-pool",
                                            "role": "GPU",
                                            "platform": "gpu-h100-sxm",
                                            "preset": "8gpu-128vcpu-1600gb",
                                            "diskSizeGb": 200,
                                            "nodeCount": 2,
                                            "maxNodeCount": 4,
                                            "gpu": {"acceleratorType": "nvidia-h100", "driversPreset": "cuda13.0"},
                                            "fabric": {"type": "InfiniBand", "infiniband": {"fabric": "fabric-2"}},
                                        },
                                    ],
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="test-cluster-kubeconfig-abcde",
                        identity={
                            "type": "NebiusServiceAccountCredentials",
                            "source": "Secret",
                            "secretRef": {
                                "namespace": "crossplane-system",
                                "name": "nebius-credentials",
                                "key": "credentials.json",
                            },
                        },
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Nebius",
                        secrets=[
                            {"type": "Kubeconfig", "name": "test-cluster-kubeconfig-abcde", "key": "kubeconfig"},
                            {
                                "type": "NebiusServiceAccountCredentials",
                                "name": "nebius-credentials",
                                "key": "credentials.json",
                                "namespace": "crossplane-system",
                            },
                        ],
                        client_cas=None,
                        gpu=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "usage-nebius-by-backend": _backend_usage(cluster_kind="NebiusCluster"),
                },
            ),
            results=[fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Nebius cluster ready, composing backend")],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-h100-nebius": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-h100-nebius"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    # The fabric is the plain string - Azure has no user-selectable fabric ID.
    # The pool sets minNodeCount to 1, as an AKS GPU pool must, because the AKS
    # autoscaler can't scale a DRA pool up from zero nodes.
    ComposeCase(
        name="AKSFirstPass",
        reason="With no AKSCluster observed yet, an AKS cluster composes only the activation policy and an AKSCluster carrying its pool's fabric and minNodeCount.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(source="AKS", aks=v1alpha1.Aks(location="westeurope")),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="h100pool",
                            className="gpu-h100-aks",
                            nodeCount=2,
                            minNodeCount=1,
                            maxNodeCount=4,
                            fabric=v1alpha1.Fabric(type="InfiniBand"),
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation_policy(
                        activated=[
                            "kubernetesclusters.containerservice.azure.m.upbound.io",
                            "kubernetesclusternodepools.containerservice.azure.m.upbound.io",
                            "subnets.network.azure.m.upbound.io",
                            "virtualnetworks.network.azure.m.upbound.io",
                            "resourcegroups.azure.m.upbound.io",
                        ]
                    ),
                },
            ),
            required_resources={
                "class-gpu-h100-aks": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-h100-aks",
                            count=8,
                            memory="81559Mi",
                            provisioning={
                                "provider": "AKS",
                                "aks": {
                                    "vmSize": "Standard_ND96isr_H100_v5",
                                    "diskSizeGb": 200,
                                    "accelerator": {"type": "nvidia-h100", "count": 8},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
                        {
                            "name": "h100pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 8,
                                    "capacity": {"memory": {"value": "81559Mi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "activation": _activation_policy(
                        activate=[
                            "kubernetesclusters.containerservice.azure.m.upbound.io",
                            "kubernetesclusternodepools.containerservice.azure.m.upbound.io",
                            "subnets.network.azure.m.upbound.io",
                            "virtualnetworks.network.azure.m.upbound.io",
                            "resourcegroups.azure.m.upbound.io",
                        ],
                        ready=fnv1.READY_TRUE,
                    ),
                    "aks-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "AKSCluster",
                                "metadata": {"name": "test-cluster", "namespace": "modelplane-system"},
                                "spec": {
                                    "location": "westeurope",
                                    "kubernetesVersion": "1.34",
                                    "nodePools": [
                                        {
                                            "name": "h100pool",
                                            "role": "GPU",
                                            "vmSize": "Standard_ND96isr_H100_v5",
                                            "diskSizeGb": 200,
                                            "nodeCount": 2,
                                            "minNodeCount": 1,
                                            "maxNodeCount": 4,
                                            "gpu": {"acceleratorType": "nvidia-h100"},
                                            "fabric": "InfiniBand",
                                        },
                                    ],
                                },
                            }
                        ),
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-h100-aks": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-h100-aks"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Provisioning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForCluster"),
            ],
        ),
    ),
    # The kubeconfig embeds a client certificate, so the ClusterProviderConfig
    # carries no identity (unlike GKE and Nebius).
    ComposeCase(
        name="AKSReady",
        reason="Once its AKSCluster is Ready, an AKS cluster composes a ClusterProviderConfig without identity, the ServingStack and the Usage, and relays its status.cache.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(source="AKS", aks=v1alpha1.Aks(location="westeurope")),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="h100pool",
                            className="gpu-h100-aks",
                            nodeCount=2,
                            minNodeCount=1,
                            maxNodeCount=4,
                            fabric=v1alpha1.Fabric(type="InfiniBand"),
                        ),
                    ],
                ),
                resources={
                    "aks-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "AKSCluster",
                                "metadata": {"name": "test-cluster", "namespace": "modelplane-system"},
                                "spec": {
                                    "location": "westeurope",
                                    "nodePools": [
                                        {
                                            "name": "h100pool",
                                            "role": "GPU",
                                            "vmSize": "Standard_ND96isr_H100_v5",
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
                                    "cache": {"storageClassName": "modelplane-rwx-fs"},
                                },
                            }
                        ),
                    ),
                    "activation": _observed_activation_policy(
                        activated=[
                            "kubernetesclusters.containerservice.azure.m.upbound.io",
                            "kubernetesclusternodepools.containerservice.azure.m.upbound.io",
                            "subnets.network.azure.m.upbound.io",
                            "virtualnetworks.network.azure.m.upbound.io",
                            "resourcegroups.azure.m.upbound.io",
                        ]
                    ),
                },
            ),
            required_resources={
                "class-gpu-h100-aks": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-h100-aks",
                            count=8,
                            memory="81559Mi",
                            provisioning={
                                "provider": "AKS",
                                "aks": {
                                    "vmSize": "Standard_ND96isr_H100_v5",
                                    "diskSizeGb": 200,
                                    "accelerator": {"type": "nvidia-h100", "count": 8},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
                        {
                            "name": "h100pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 8,
                                    "capacity": {"memory": {"value": "81559Mi"}},
                                },
                            ],
                        },
                    ],
                    cache={"storageClassName": "modelplane-rwx-fs"},
                    gateway=None,
                ),
                resources={
                    "activation": _activation_policy(
                        activate=[
                            "kubernetesclusters.containerservice.azure.m.upbound.io",
                            "kubernetesclusternodepools.containerservice.azure.m.upbound.io",
                            "subnets.network.azure.m.upbound.io",
                            "virtualnetworks.network.azure.m.upbound.io",
                            "resourcegroups.azure.m.upbound.io",
                        ],
                        ready=fnv1.READY_TRUE,
                    ),
                    "aks-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "AKSCluster",
                                "metadata": {"name": "test-cluster", "namespace": "modelplane-system"},
                                "spec": {
                                    "location": "westeurope",
                                    "kubernetesVersion": "1.34",
                                    "nodePools": [
                                        {
                                            "name": "h100pool",
                                            "role": "GPU",
                                            "vmSize": "Standard_ND96isr_H100_v5",
                                            "diskSizeGb": 200,
                                            "nodeCount": 2,
                                            "minNodeCount": 1,
                                            "maxNodeCount": 4,
                                            "gpu": {"acceleratorType": "nvidia-h100"},
                                            "fabric": "InfiniBand",
                                        },
                                    ],
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="test-cluster-kubeconfig-abcde", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="AKS",
                        secrets=[{"type": "Kubeconfig", "name": "test-cluster-kubeconfig-abcde", "key": "kubeconfig"}],
                        client_cas=None,
                        gpu=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "usage-aks-by-backend": _backend_usage(cluster_kind="AKSCluster"),
                },
            ),
            results=[fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="AKS cluster ready, composing backend")],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-h100-aks": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-h100-aks"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    # A kind is missing while, for example, a provider is still installing.
    # Leaving the policy not ready keeps the composite from reporting ready.
    # Observing the policy with one kind missing, here
    # nodepools.container.gcp.m.upbound.io, exercises the all-kinds check rather
    # than the policy-absent branch.
    ComposeCase(
        name="KindNotActivated",
        reason="While its activation policy is missing one kind and no GKECluster is observed, a GKE cluster composes only the policy, not ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(source="GKE", gke=v1alpha1.Gke(region="us-central1")),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="l4-pool",
                            className="gpu-l4",
                            nodeCount=2,
                            maxNodeCount=4,
                            zones=["us-central1-a"],
                        ),
                    ],
                ),
                resources={
                    "activation": _observed_activation_policy(
                        activated=[
                            "projectiammembers.cloudplatform.gcp.m.upbound.io",
                            "projectservices.cloudplatform.gcp.m.upbound.io",
                            "serviceaccounts.cloudplatform.gcp.m.upbound.io",
                            "serviceaccountkeys.cloudplatform.gcp.m.upbound.io",
                            "networks.compute.gcp.m.upbound.io",
                            "subnetworks.compute.gcp.m.upbound.io",
                            "clusters.container.gcp.m.upbound.io",
                        ]
                    ),
                },
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l4",
                            count=1,
                            memory="24Gi",
                            provisioning={
                                "provider": "GKE",
                                "gke": {
                                    "machineType": "g2-standard-48",
                                    "diskSizeGb": 100,
                                    "accelerator": {"type": "nvidia-l4", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
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
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "activation": _activation_policy(
                        activate=[
                            "projectiammembers.cloudplatform.gcp.m.upbound.io",
                            "projectservices.cloudplatform.gcp.m.upbound.io",
                            "serviceaccounts.cloudplatform.gcp.m.upbound.io",
                            "serviceaccountkeys.cloudplatform.gcp.m.upbound.io",
                            "networks.compute.gcp.m.upbound.io",
                            "subnetworks.compute.gcp.m.upbound.io",
                            "clusters.container.gcp.m.upbound.io",
                            "nodepools.container.gcp.m.upbound.io",
                        ],
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Provisioning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForCluster"),
            ],
        ),
    ),
    # Once the cluster is observed, the function keeps composing it even when
    # its activation policy isn't observed, so an activation blip never drops a
    # provisioned cluster from desired state.
    ComposeCase(
        name="ActivationBlip",
        reason="With its GKECluster observed but no activation policy, a GKE cluster keeps composing the GKECluster and everything built on it.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(source="GKE", gke=v1alpha1.Gke(region="us-central1")),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="l4-pool",
                            className="gpu-l4",
                            nodeCount=2,
                            maxNodeCount=4,
                            zones=["us-central1-a"],
                        ),
                    ],
                ),
                resources={
                    "gke-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "GKECluster",
                                "metadata": {"name": "test-cluster", "namespace": "modelplane-system"},
                                "spec": {
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
                                    "cache": {"storageClassName": "modelplane-rwx"},
                                    "secrets": [
                                        {
                                            "type": "Kubeconfig",
                                            "name": "test-cluster-kubeconfig-abcde",
                                            "key": "kubeconfig",
                                        },
                                        {
                                            "type": "GoogleApplicationCredentials",
                                            "name": "test-cluster-sa-key-fghij",
                                            "key": "credentials.json",
                                        },
                                    ],
                                },
                            }
                        ),
                    ),
                },
            ),
            required_resources={
                "class-gpu-l4": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l4",
                            count=1,
                            memory="24Gi",
                            provisioning={
                                "provider": "GKE",
                                "gke": {
                                    "machineType": "g2-standard-48",
                                    "diskSizeGb": 100,
                                    "accelerator": {"type": "nvidia-l4", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
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
                    cache={"storageClassName": "modelplane-rwx"},
                    gateway=None,
                ),
                resources={
                    "activation": _activation_policy(
                        activate=[
                            "projectiammembers.cloudplatform.gcp.m.upbound.io",
                            "projectservices.cloudplatform.gcp.m.upbound.io",
                            "serviceaccounts.cloudplatform.gcp.m.upbound.io",
                            "serviceaccountkeys.cloudplatform.gcp.m.upbound.io",
                            "networks.compute.gcp.m.upbound.io",
                            "subnetworks.compute.gcp.m.upbound.io",
                            "clusters.container.gcp.m.upbound.io",
                            "nodepools.container.gcp.m.upbound.io",
                        ],
                        ready=fnv1.READY_TRUE,
                    ),
                    "gke-cluster": _gke_cluster(credentials=None, ready=fnv1.READY_TRUE),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="test-cluster-kubeconfig-abcde",
                        identity={
                            "type": "GoogleApplicationCredentials",
                            "source": "Secret",
                            "secretRef": {
                                "namespace": "modelplane-system",
                                "name": "test-cluster-sa-key-fghij",
                                "key": "credentials.json",
                            },
                        },
                    ),
                    "serving-stack": _serving_stack(
                        cloud="GKE",
                        secrets=[
                            {"type": "Kubeconfig", "name": "test-cluster-kubeconfig-abcde", "key": "kubeconfig"},
                            {
                                "type": "GoogleApplicationCredentials",
                                "name": "test-cluster-sa-key-fghij",
                                "key": "credentials.json",
                            },
                        ],
                        client_cas=None,
                        gpu=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "usage-gke-by-backend": _backend_usage(cluster_kind="GKECluster"),
                },
            ),
            results=[fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="GKE cluster ready, composing backend")],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    # minNodeCount stays unset so the pool's autoscaling floor defaults to its
    # node count downstream.
    ComposeCase(
        name="VultrFirstPass",
        reason="With no VultrCluster observed yet, a Vultr cluster composes only the activation policy and the VultrCluster.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(source="Vultr", vultr=v1alpha1.Vultr(region="ewr")),
                    node_pools=[
                        v1alpha1.NodePool(name="l40s-pool", className="gpu-l40s-vultr", nodeCount=2, maxNodeCount=4),
                    ],
                ),
                resources={
                    "activation": _observed_activation_policy(
                        activated=["kubernetes.vke.vultr.m.upbound.io", "kubernetesnodepools.vke.vultr.m.upbound.io"]
                    ),
                },
            ),
            required_resources={
                "class-gpu-l40s-vultr": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l40s-vultr",
                            count=1,
                            memory="46068Mi",
                            provisioning={
                                "provider": "Vultr",
                                "vultr": {
                                    "plan": "vcg-l40s-16c-180g-48vram",
                                    "accelerator": {"type": "nvidia-l40s", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
                        {
                            "name": "l40s-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "capacity": {"memory": {"value": "46068Mi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "activation": _activation_policy(
                        activate=["kubernetes.vke.vultr.m.upbound.io", "kubernetesnodepools.vke.vultr.m.upbound.io"],
                        ready=fnv1.READY_TRUE,
                    ),
                    "vultr-cluster": _vultr_cluster(credentials=None, ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l40s-vultr": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l40s-vultr"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Provisioning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForCluster"),
            ],
        ),
    ),
    ComposeCase(
        name="VultrCredentials",
        reason="A Vultr cluster's credentials pass through to the VultrCluster's spec.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Vultr",
                        vultr=v1alpha1.Vultr(
                            region="ewr",
                            credentials=v1alpha1.Credentials(type="ProviderConfig", name="my-vultr-account"),
                        ),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l40s-pool", className="gpu-l40s-vultr", nodeCount=2, maxNodeCount=4),
                    ],
                ),
                resources={
                    "activation": _observed_activation_policy(
                        activated=["kubernetes.vke.vultr.m.upbound.io", "kubernetesnodepools.vke.vultr.m.upbound.io"]
                    ),
                },
            ),
            required_resources={
                "class-gpu-l40s-vultr": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l40s-vultr",
                            count=1,
                            memory="46068Mi",
                            provisioning={
                                "provider": "Vultr",
                                "vultr": {
                                    "plan": "vcg-l40s-16c-180g-48vram",
                                    "accelerator": {"type": "nvidia-l40s", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
                        {
                            "name": "l40s-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "capacity": {"memory": {"value": "46068Mi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "activation": _activation_policy(
                        activate=["kubernetes.vke.vultr.m.upbound.io", "kubernetesnodepools.vke.vultr.m.upbound.io"],
                        ready=fnv1.READY_TRUE,
                    ),
                    "vultr-cluster": _vultr_cluster(
                        credentials={"type": "ProviderConfig", "name": "my-vultr-account"}, ready=fnv1.READY_UNSPECIFIED
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l40s-vultr": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l40s-vultr"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Provisioning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForCluster"),
            ],
        ),
    ),
    # The VKE kubeconfig embeds static client certificates, so the
    # ClusterProviderConfig carries no identity (unlike Nebius). VultrCluster
    # reports no cache StorageClass, so status.cache stays unset.
    ComposeCase(
        name="VultrReady",
        reason="Once its VultrCluster is Ready, a Vultr cluster composes a ClusterProviderConfig without identity, the ServingStack and the Usage.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(source="Vultr", vultr=v1alpha1.Vultr(region="ewr")),
                    node_pools=[
                        v1alpha1.NodePool(name="l40s-pool", className="gpu-l40s-vultr", nodeCount=2, maxNodeCount=4),
                    ],
                ),
                resources={
                    "vultr-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "VultrCluster",
                                "metadata": {"name": "test-cluster", "namespace": "modelplane-system"},
                                "spec": {
                                    "region": "ewr",
                                    "nodePools": [
                                        {
                                            "name": "l40s-pool",
                                            "role": "GPU",
                                            "plan": "vcg-l40s-16c-180g-48vram",
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
                                },
                            }
                        ),
                    ),
                    "activation": _observed_activation_policy(
                        activated=["kubernetes.vke.vultr.m.upbound.io", "kubernetesnodepools.vke.vultr.m.upbound.io"]
                    ),
                },
            ),
            required_resources={
                "class-gpu-l40s-vultr": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l40s-vultr",
                            count=1,
                            memory="46068Mi",
                            provisioning={
                                "provider": "Vultr",
                                "vultr": {
                                    "plan": "vcg-l40s-16c-180g-48vram",
                                    "accelerator": {"type": "nvidia-l40s", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
                        {
                            "name": "l40s-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "capacity": {"memory": {"value": "46068Mi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "activation": _activation_policy(
                        activate=["kubernetes.vke.vultr.m.upbound.io", "kubernetesnodepools.vke.vultr.m.upbound.io"],
                        ready=fnv1.READY_TRUE,
                    ),
                    "vultr-cluster": _vultr_cluster(credentials=None, ready=fnv1.READY_TRUE),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="test-cluster-kubeconfig-abcde", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Vultr",
                        secrets=[{"type": "Kubeconfig", "name": "test-cluster-kubeconfig-abcde", "key": "kubeconfig"}],
                        client_cas=None,
                        gpu=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "usage-vultr-by-backend": _backend_usage(cluster_kind="VultrCluster"),
                },
            ),
            results=[fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Vultr cluster ready, composing backend")],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l40s-vultr": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l40s-vultr"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    # minNodeCount stays unset so the pool's autoscaling floor defaults to its
    # node count downstream.
    ComposeCase(
        name="CivoFirstPass",
        reason="With no CivoCluster observed yet, a Civo cluster composes only the activation policy and the CivoCluster.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(source="Civo", civo=v1alpha1.Civo(region="LON1")),
                    node_pools=[
                        v1alpha1.NodePool(name="l40s-pool", className="gpu-l40s-civo", nodeCount=2, maxNodeCount=4),
                    ],
                ),
                resources={
                    "activation": _observed_activation_policy(
                        activated=[
                            "clusters.kubernetes.civo.m.upbound.io",
                            "nodepools.kubernetes.civo.m.upbound.io",
                            "networks.vpc.civo.m.upbound.io",
                            "firewalls.vpc.civo.m.upbound.io",
                        ]
                    ),
                },
            ),
            required_resources={
                "class-gpu-l40s-civo": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l40s-civo",
                            count=1,
                            memory="46068Mi",
                            provisioning={
                                "provider": "Civo",
                                "civo": {
                                    "size": "an.g1.l40s.kube.x1",
                                    "accelerator": {"type": "nvidia-l40s", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
                        {
                            "name": "l40s-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "capacity": {"memory": {"value": "46068Mi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "activation": _activation_policy(
                        activate=[
                            "clusters.kubernetes.civo.m.upbound.io",
                            "nodepools.kubernetes.civo.m.upbound.io",
                            "networks.vpc.civo.m.upbound.io",
                            "firewalls.vpc.civo.m.upbound.io",
                        ],
                        ready=fnv1.READY_TRUE,
                    ),
                    "civo-cluster": _civo_cluster(
                        node_pools=[
                            {
                                "name": "l40s-pool",
                                "role": "GPU",
                                "size": "an.g1.l40s.kube.x1",
                                "nodeCount": 2,
                                "maxNodeCount": 4,
                                "gpu": {"acceleratorType": "nvidia-l40s"},
                            },
                        ],
                        credentials=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l40s-civo": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l40s-civo"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Provisioning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForCluster"),
            ],
        ),
    ),
    ComposeCase(
        name="CivoCredentials",
        reason="A Civo cluster's credentials pass through to the CivoCluster's spec.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Civo",
                        civo=v1alpha1.Civo(
                            region="LON1",
                            credentials=v1alpha1.Credentials(type="ProviderConfig", name="my-civo-account"),
                        ),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l40s-pool", className="gpu-l40s-civo", nodeCount=2, maxNodeCount=4),
                    ],
                ),
                resources={
                    "activation": _observed_activation_policy(
                        activated=[
                            "clusters.kubernetes.civo.m.upbound.io",
                            "nodepools.kubernetes.civo.m.upbound.io",
                            "networks.vpc.civo.m.upbound.io",
                            "firewalls.vpc.civo.m.upbound.io",
                        ]
                    ),
                },
            ),
            required_resources={
                "class-gpu-l40s-civo": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l40s-civo",
                            count=1,
                            memory="46068Mi",
                            provisioning={
                                "provider": "Civo",
                                "civo": {
                                    "size": "an.g1.l40s.kube.x1",
                                    "accelerator": {"type": "nvidia-l40s", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
                        {
                            "name": "l40s-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "capacity": {"memory": {"value": "46068Mi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "activation": _activation_policy(
                        activate=[
                            "clusters.kubernetes.civo.m.upbound.io",
                            "nodepools.kubernetes.civo.m.upbound.io",
                            "networks.vpc.civo.m.upbound.io",
                            "firewalls.vpc.civo.m.upbound.io",
                        ],
                        ready=fnv1.READY_TRUE,
                    ),
                    "civo-cluster": _civo_cluster(
                        node_pools=[
                            {
                                "name": "l40s-pool",
                                "role": "GPU",
                                "size": "an.g1.l40s.kube.x1",
                                "nodeCount": 2,
                                "maxNodeCount": 4,
                                "gpu": {"acceleratorType": "nvidia-l40s"},
                            },
                        ],
                        credentials={"type": "ProviderConfig", "name": "my-civo-account"},
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l40s-civo": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l40s-civo"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Provisioning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForCluster"),
            ],
        ),
    ),
    # The Civo kubeconfig embeds static client certificates, so the
    # ClusterProviderConfig carries no identity (unlike Nebius). CivoCluster
    # reports no cache StorageClass, so status.cache stays unset.
    ComposeCase(
        name="CivoReady",
        reason="Once its CivoCluster is Ready, a Civo cluster composes a ClusterProviderConfig without identity, the ServingStack and the Usage.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(source="Civo", civo=v1alpha1.Civo(region="LON1")),
                    node_pools=[
                        v1alpha1.NodePool(name="l40s-pool", className="gpu-l40s-civo", nodeCount=2, maxNodeCount=4),
                    ],
                ),
                resources={
                    "civo-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "CivoCluster",
                                "metadata": {"name": "test-cluster", "namespace": "modelplane-system"},
                                "spec": {
                                    "region": "LON1",
                                    "nodePools": [
                                        {
                                            "name": "l40s-pool",
                                            "role": "GPU",
                                            "size": "an.g1.l40s.kube.x1",
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
                                },
                            }
                        ),
                    ),
                    "activation": _observed_activation_policy(
                        activated=[
                            "clusters.kubernetes.civo.m.upbound.io",
                            "nodepools.kubernetes.civo.m.upbound.io",
                            "networks.vpc.civo.m.upbound.io",
                            "firewalls.vpc.civo.m.upbound.io",
                        ]
                    ),
                },
            ),
            required_resources={
                "class-gpu-l40s-civo": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l40s-civo",
                            count=1,
                            memory="46068Mi",
                            provisioning={
                                "provider": "Civo",
                                "civo": {
                                    "size": "an.g1.l40s.kube.x1",
                                    "accelerator": {"type": "nvidia-l40s", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
                        {
                            "name": "l40s-pool",
                            "nodes": 4,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "capacity": {"memory": {"value": "46068Mi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "activation": _activation_policy(
                        activate=[
                            "clusters.kubernetes.civo.m.upbound.io",
                            "nodepools.kubernetes.civo.m.upbound.io",
                            "networks.vpc.civo.m.upbound.io",
                            "firewalls.vpc.civo.m.upbound.io",
                        ],
                        ready=fnv1.READY_TRUE,
                    ),
                    "civo-cluster": _civo_cluster(
                        node_pools=[
                            {
                                "name": "l40s-pool",
                                "role": "GPU",
                                "size": "an.g1.l40s.kube.x1",
                                "nodeCount": 2,
                                "maxNodeCount": 4,
                                "gpu": {"acceleratorType": "nvidia-l40s"},
                            },
                        ],
                        credentials=None,
                        ready=fnv1.READY_TRUE,
                    ),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="test-cluster-kubeconfig-abcde", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Civo",
                        secrets=[{"type": "Kubeconfig", "name": "test-cluster-kubeconfig-abcde", "key": "kubeconfig"}],
                        client_cas=None,
                        gpu=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "usage-civo-by-backend": _backend_usage(cluster_kind="CivoCluster"),
                },
            ),
            results=[fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Civo cluster ready, composing backend")],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l40s-civo": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l40s-civo"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    # A lone H100 SXM has NVLink links but no peer, so the serving stack must
    # load that pool's driver with NVreg_NvLinkDisable=1. CivoReady's L40S pool
    # gets no spec.gpu.
    #
    # The request still returns the gpu-l40s-civo class, which no node pool
    # references, and the observed CivoCluster still reports l40s-pool in its
    # spec. The function reads neither.
    ComposeCase(
        name="CivoSingleH100",
        reason="Once its CivoCluster is Ready, a Civo cluster whose pool has one H100 per node disables NVLink for that pool in the ServingStack's spec.gpu.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(source="Civo", civo=v1alpha1.Civo(region="LON1")),
                    node_pools=[
                        v1alpha1.NodePool(name="h100-pool", className="gpu-h100-civo", nodeCount=1),
                    ],
                ),
                resources={
                    "civo-cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                "kind": "CivoCluster",
                                "metadata": {"name": "test-cluster", "namespace": "modelplane-system"},
                                "spec": {
                                    "region": "LON1",
                                    "nodePools": [
                                        {
                                            "name": "l40s-pool",
                                            "role": "GPU",
                                            "size": "an.g1.l40s.kube.x1",
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
                                },
                            }
                        ),
                    ),
                    "activation": _observed_activation_policy(
                        activated=[
                            "clusters.kubernetes.civo.m.upbound.io",
                            "nodepools.kubernetes.civo.m.upbound.io",
                            "networks.vpc.civo.m.upbound.io",
                            "firewalls.vpc.civo.m.upbound.io",
                        ]
                    ),
                },
            ),
            required_resources={
                "class-gpu-l40s-civo": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l40s-civo",
                            count=1,
                            memory="46068Mi",
                            provisioning={
                                "provider": "Civo",
                                "civo": {
                                    "size": "an.g1.l40s.kube.x1",
                                    "accelerator": {"type": "nvidia-l40s", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
                "class-gpu-h100-civo": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-h100-civo",
                            count=1,
                            memory="81559Mi",
                            provisioning={
                                "provider": "Civo",
                                "civo": {
                                    "size": "an.g1.h100.kube.x1",
                                    "accelerator": {"type": "nvidia-h100", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
                        {
                            "name": "h100-pool",
                            "nodes": 1,
                            "devices": [
                                {
                                    "name": "gpu",
                                    "claim": "DRA",
                                    "driver": "gpu.nvidia.com",
                                    "deviceClassName": "gpu.nvidia.com",
                                    "count": 1,
                                    "capacity": {"memory": {"value": "81559Mi"}},
                                },
                            ],
                        },
                    ],
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "activation": _activation_policy(
                        activate=[
                            "clusters.kubernetes.civo.m.upbound.io",
                            "nodepools.kubernetes.civo.m.upbound.io",
                            "networks.vpc.civo.m.upbound.io",
                            "firewalls.vpc.civo.m.upbound.io",
                        ],
                        ready=fnv1.READY_TRUE,
                    ),
                    "civo-cluster": _civo_cluster(
                        node_pools=[
                            {
                                "name": "h100-pool",
                                "role": "GPU",
                                "size": "an.g1.h100.kube.x1",
                                "nodeCount": 1,
                                "gpu": {"acceleratorType": "nvidia-h100"},
                            },
                        ],
                        credentials=None,
                        ready=fnv1.READY_TRUE,
                    ),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="test-cluster-kubeconfig-abcde", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Civo",
                        secrets=[{"type": "Kubeconfig", "name": "test-cluster-kubeconfig-abcde", "key": "kubeconfig"}],
                        client_cas=None,
                        gpu={"pools": [{"name": "h100-pool", "disableNvLink": True}]},
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "usage-civo-by-backend": _backend_usage(cluster_kind="CivoCluster"),
                },
            ),
            results=[fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Civo cluster ready, composing backend")],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-h100-civo": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-h100-civo"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    # The deletion guard cases, ReplicasRoutesAndCaches through ClassUnresolved,
    # observe an existing cluster along with whatever uses it.
    #
    # One reason-only ClusterUsage blocks deletion whatever the count or
    # namespace of its users. The mirrored namespaces are the deduplicated union
    # of team-a (replica), team-b (replica and route), team-c (route) and team-d
    # (cache staging onto this cluster). A cache staging only onto another
    # cluster (team-e) is filtered out by its status.clusters[], proving the
    # namespaces track what actually lands here.
    ComposeCase(
        name="ReplicasRoutesAndCaches",
        reason="ModelReplicas, ModelRoutes and ModelCaches across namespaces compose one ClusterUsage naming each kind, and mirror the namespaces that land here.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "model-replicas": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "modelplane.ai/v1alpha1",
                                    "kind": "ModelReplica",
                                    "metadata": {
                                        "name": "deploy-test-cluster-0",
                                        "namespace": "team-a",
                                        "labels": {"modelplane.ai/cluster": "test-cluster"},
                                    },
                                }
                            )
                        ),
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "modelplane.ai/v1alpha1",
                                    "kind": "ModelReplica",
                                    "metadata": {
                                        "name": "deploy-test-cluster-0",
                                        "namespace": "team-b",
                                        "labels": {"modelplane.ai/cluster": "test-cluster"},
                                    },
                                }
                            )
                        ),
                    ],
                ),
                "model-routes": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "modelplane.ai/v1alpha1",
                                    "kind": "ModelRoute",
                                    "metadata": {
                                        "name": "svc-eu",
                                        "namespace": "team-b",
                                        "labels": {"modelplane.ai/cluster": "test-cluster"},
                                    },
                                }
                            )
                        ),
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "modelplane.ai/v1alpha1",
                                    "kind": "ModelRoute",
                                    "metadata": {
                                        "name": "svc-eu",
                                        "namespace": "team-c",
                                        "labels": {"modelplane.ai/cluster": "test-cluster"},
                                    },
                                }
                            )
                        ),
                    ],
                ),
                "model-caches": fnv1.Resources(
                    items=[
                        _model_cache(name="qwen", namespace="team-d", cluster="test-cluster"),
                        _model_cache(name="kimi", namespace="team-e", cluster="other-cluster"),
                    ],
                ),
                "class-gpu-l4": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l4",
                            count=1,
                            memory="24Gi",
                            provisioning={
                                "provider": "GKE",
                                "gke": {
                                    "machineType": "g2-standard-48",
                                    "diskSizeGb": 100,
                                    "accelerator": {"type": "nvidia-l4", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
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
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "usage-replicas": _guard_cluster_usage(
                        reason="ModelReplicas, ModelRoutes and ModelCaches use this InferenceCluster"
                    ),
                    "namespace-team-a": _namespace_object(team="team-a", name="mp-team-a-bd964"),
                    "namespace-team-b": _namespace_object(team="team-b", name="mp-team-b-6bd62"),
                    "namespace-team-c": _namespace_object(team="team-c", name="mp-team-c-d79d9"),
                    "namespace-team-d": _namespace_object(team="team-d", name="mp-team-d-c2383"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Existing",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        client_cas=None,
                        gpu=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    # A ModelRoute composes its routing Objects through the cluster's
    # ClusterProviderConfig, so it needs the cluster as much as a replica does.
    ComposeCase(
        name="RouteOnCluster",
        reason="A ModelRoute on the cluster, with no replica there, composes the guard and mirrors its team's namespace.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "model-routes": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "modelplane.ai/v1alpha1",
                                    "kind": "ModelRoute",
                                    "metadata": {
                                        "name": "svc-eu",
                                        "namespace": "team-c",
                                        "labels": {"modelplane.ai/cluster": "test-cluster"},
                                    },
                                }
                            )
                        )
                    ]
                ),
                "class-gpu-l4": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l4",
                            count=1,
                            memory="24Gi",
                            provisioning={
                                "provider": "GKE",
                                "gke": {
                                    "machineType": "g2-standard-48",
                                    "diskSizeGb": 100,
                                    "accelerator": {"type": "nvidia-l4", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
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
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "usage-replicas": _guard_cluster_usage(reason="ModelRoutes use this InferenceCluster"),
                    "namespace-team-c": _namespace_object(team="team-c", name="mp-team-c-d79d9"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Existing",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        client_cas=None,
                        gpu=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    # A ModelCache composes its PVC through the cluster's ClusterProviderConfig,
    # so it needs the cluster as much as a replica does.
    ComposeCase(
        name="CacheOnCluster",
        reason="A ModelCache staging onto the cluster, with no replica there, composes the guard and mirrors its team's namespace.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "model-caches": fnv1.Resources(
                    items=[_model_cache(name="qwen", namespace="team-d", cluster="test-cluster")]
                ),
                "class-gpu-l4": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l4",
                            count=1,
                            memory="24Gi",
                            provisioning={
                                "provider": "GKE",
                                "gke": {
                                    "machineType": "g2-standard-48",
                                    "diskSizeGb": 100,
                                    "accelerator": {"type": "nvidia-l4", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
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
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "usage-replicas": _guard_cluster_usage(reason="ModelCaches use this InferenceCluster"),
                    "namespace-team-d": _namespace_object(team="team-d", name="mp-team-d-c2383"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Existing",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        client_cas=None,
                        gpu=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    # An InferenceGateway composes its Gateway and routing Objects through the
    # cluster's ClusterProviderConfig just as a replica does. It is cluster
    # scoped, so it has no namespace to mirror.
    ComposeCase(
        name="GatewayOnCluster",
        reason="An InferenceGateway on the cluster composes the guard but mirrors no namespace.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "gateways": fnv1.Resources(
                    items=[_inference_gateway(name="public", cluster="test-cluster", status=None)]
                ),
                "class-gpu-l4": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l4",
                            count=1,
                            memory="24Gi",
                            provisioning={
                                "provider": "GKE",
                                "gke": {
                                    "machineType": "g2-standard-48",
                                    "diskSizeGb": 100,
                                    "accelerator": {"type": "nvidia-l4", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
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
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "usage-replicas": _guard_cluster_usage(reason="InferenceGateways use this InferenceCluster"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Existing",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        client_cas=None,
                        gpu=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    # This is the teardown transition - the last user is gone, so the function
    # stops composing the guard. The replica and route requirements are empty
    # but present, as Crossplane returns them when a selector matches nothing.
    ComposeCase(
        name="NothingOnCluster",
        reason="With only a ModelCache and an InferenceGateway on another cluster, the cluster composes no ClusterUsage and is deletable.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "model-replicas": fnv1.Resources(),
                "model-routes": fnv1.Resources(),
                "model-caches": fnv1.Resources(
                    items=[_model_cache(name="kimi", namespace="team-e", cluster="other-cluster")]
                ),
                "gateways": fnv1.Resources(
                    items=[_inference_gateway(name="elsewhere", cluster="other-cluster", status=None)]
                ),
                "class-gpu-l4": fnv1.Resources(
                    items=[
                        _inference_class(
                            name="gpu-l4",
                            count=1,
                            memory="24Gi",
                            provisioning={
                                "provider": "GKE",
                                "gke": {
                                    "machineType": "g2-standard-48",
                                    "diskSizeGb": 100,
                                    "accelerator": {"type": "nvidia-l4", "count": 1},
                                },
                            },
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[
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
                    cache=None,
                    gateway=None,
                ),
                resources={
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Existing",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        client_cas=None,
                        gpu=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_FALSE, reason="Installing"),
            ],
        ),
    ),
    # resolve_classes() returns False whenever a referenced InferenceClass isn't
    # observed yet - a routine transient. The guard runs first, so a
    # referencing replica still blocks deletion. This is the case that
    # regresses if the guard is gated behind class resolution or cluster source.
    #
    # Only the guard and namespace are composed, both ready, so the function
    # marks the XR not ready itself. Otherwise it would read ready while it
    # waits for its classes.
    ComposeCase(
        name="ClassUnresolved",
        reason="With its class unresolved and a replica on the cluster, the function composes the guard and the replica's namespace before returning early.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=[
                        v1alpha1.NodePool(name="l4-pool", className="gpu-l4", nodeCount=2, maxNodeCount=4),
                    ],
                ),
            ),
            required_resources={
                "model-replicas": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "modelplane.ai/v1alpha1",
                                    "kind": "ModelReplica",
                                    "metadata": {
                                        "name": "deploy-test-cluster-0",
                                        "namespace": "team-a",
                                        "labels": {"modelplane.ai/cluster": "test-cluster"},
                                    },
                                }
                            )
                        )
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(ready=fnv1.READY_FALSE),
                resources={
                    "usage-replicas": _guard_cluster_usage(reason="ModelReplicas use this InferenceCluster"),
                    "namespace-team-a": _namespace_object(team="team-a", name="mp-team-a-bd964"),
                },
            ),
            results=[fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for InferenceClasses: gpu-l4")],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                    "class-gpu-l4": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1", kind="InferenceClass", match_name="gpu-l4"
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForClasses",
                    message="Waiting for InferenceClasses: gpu-l4",
                ),
            ],
        ),
    ),
    # The hostname gate, which is what keeps a cluster off the schedule until
    # traffic to it is mutually authenticated in both directions. These cases
    # observe an existing cluster's ServingStack ready, with whatever it and the
    # fleet's InferenceGateways have published so far. The gateway name is
    # Modelplane's own, so nothing configures it. An InferenceGateway running on
    # test-cluster also composes the deletion guard.
    #
    # This cluster's CA lets an InferenceGateway tell it reached the right
    # cluster, and an InferenceGateway CA makes the cluster gateway demand a
    # client certificate.
    ComposeCase(
        name="MutuallyAuthenticated",
        reason="With an address, this cluster's CA and an InferenceGateway CA all published, the cluster publishes its gateway hostname.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=None,
                ),
                resources={
                    "serving-stack": _observed_serving_stack(
                        gateway={"address": "34.55.100.10", "caCertificate": "cluster-ca"}
                    ),
                },
            ),
            required_resources={
                "gateways": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            name="fleet-0", cluster="test-cluster", status={"clientCACertificate": "fleet-ca"}
                        )
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[],
                    cache=None,
                    gateway={
                        "address": "34.55.100.10",
                        "caCertificate": "cluster-ca",
                        "hostname": "gateway-test-cluster-09532.modelplane-system.svc.cluster.local",
                    },
                ),
                resources={
                    "usage-replicas": _guard_cluster_usage(reason="InferenceGateways use this InferenceCluster"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Existing",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        client_cas=[{"name": "fleet-0", "certificate": "fleet-ca"}],
                        gpu=None,
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_TRUE, reason="BackendHealthy"),
            ],
        ),
    ),
    # The case that matters: the cluster gateway only demands a client
    # certificate when it has a CA to check against, and with none it serves no
    # Gateway at all. Publishing the hostname anyway would make the cluster
    # schedulable when nothing is listening on it, so every request routed there
    # would be stranded.
    ComposeCase(
        name="NoGatewayCA",
        reason="With no InferenceGateway CA published, the cluster publishes its address and CA but no hostname.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=None,
                ),
                resources={
                    "serving-stack": _observed_serving_stack(
                        gateway={"address": "34.55.100.10", "caCertificate": "cluster-ca"}
                    ),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[], cache=None, gateway={"address": "34.55.100.10", "caCertificate": "cluster-ca"}
                ),
                resources={
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Existing",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        client_cas=None,
                        gpu=None,
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_TRUE, reason="BackendHealthy"),
            ],
        ),
    ),
    # Without this cluster's CA an InferenceGateway can't validate the cluster
    # gateway it reaches, so it would have to fall back to the public trust
    # store.
    ComposeCase(
        name="NoClusterCA",
        reason="Without this cluster's CA, the cluster publishes its address but no hostname.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=None,
                ),
                resources={
                    "serving-stack": _observed_serving_stack(gateway={"address": "34.55.100.10"}),
                },
            ),
            required_resources={
                "gateways": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            name="fleet-0", cluster="test-cluster", status={"clientCACertificate": "fleet-ca"}
                        )
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(gpu_pools=[], cache=None, gateway={"address": "34.55.100.10"}),
                resources={
                    "usage-replicas": _guard_cluster_usage(reason="InferenceGateways use this InferenceCluster"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Existing",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        client_cas=[{"name": "fleet-0", "certificate": "fleet-ca"}],
                        gpu=None,
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_TRUE, reason="BackendHealthy"),
            ],
        ),
    ),
    # A hostname that resolves to nothing strands every request routed to it,
    # and the CA is republished from the same status.
    ComposeCase(
        name="NoAddress",
        reason="Before its ServingStack publishes an address, the cluster publishes no gateway status, not even its CA.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=None,
                ),
                resources={
                    "serving-stack": _observed_serving_stack(gateway={"caCertificate": "cluster-ca"}),
                },
            ),
            required_resources={
                "gateways": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            name="fleet-0", cluster="test-cluster", status={"clientCACertificate": "fleet-ca"}
                        )
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(gpu_pools=[], cache=None, gateway=None),
                resources={
                    "usage-replicas": _guard_cluster_usage(reason="InferenceGateways use this InferenceCluster"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Existing",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        client_cas=[{"name": "fleet-0", "certificate": "fleet-ca"}],
                        gpu=None,
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_TRUE, reason="BackendHealthy"),
            ],
        ),
    ),
    # Any InferenceGateway may forward to this cluster, and these CAs are what
    # switches the cluster gateway's mTLS listener on. One that hasn't published
    # a CA yet is left out rather than holding the others back, and the list is
    # sorted so it doesn't churn.
    ComposeCase(
        name="ManyGatewayCAs",
        reason="The ServingStack accepts the CA of every InferenceGateway that has published one, on any cluster, sorted by name.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_inference_cluster(
                    cluster=v1alpha1.Cluster(
                        source="Existing",
                        existing=v1alpha1.Existing(secretRef=v1alpha1.SecretRef(name="my-kubeconfig")),
                    ),
                    node_pools=None,
                ),
                resources={
                    "serving-stack": _observed_serving_stack(
                        gateway={"address": "34.55.100.10", "caCertificate": "cluster-ca"}
                    ),
                },
            ),
            required_resources={
                "gateways": fnv1.Resources(
                    items=[
                        _inference_gateway(
                            name="fleet-0", cluster="test-cluster", status={"clientCACertificate": "fleet-0-ca"}
                        ),
                        _inference_gateway(
                            name="fleet-1", cluster="test-cluster", status={"clientCACertificate": "fleet-1-ca"}
                        ),
                        _inference_gateway(name="aaa", cluster="elsewhere", status={"clientCACertificate": "aaa-ca"}),
                        _inference_gateway(name="pending", cluster="elsewhere", status={}),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_inference_cluster(
                    gpu_pools=[],
                    cache=None,
                    gateway={
                        "address": "34.55.100.10",
                        "caCertificate": "cluster-ca",
                        "hostname": "gateway-test-cluster-09532.modelplane-system.svc.cluster.local",
                    },
                ),
                resources={
                    "usage-replicas": _guard_cluster_usage(reason="InferenceGateways use this InferenceCluster"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Existing",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        client_cas=[
                            {"name": "aaa", "certificate": "aaa-ca"},
                            {"name": "fleet-0", "certificate": "fleet-0-ca"},
                            {"name": "fleet-1", "certificate": "fleet-1-ca"},
                        ],
                        gpu=None,
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "model-replicas": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelReplica",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-routes": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelRoute",
                        match_labels=fnv1.MatchLabels(labels={"modelplane.ai/cluster": "test-cluster"}),
                    ),
                    "model-caches": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="ModelCache"),
                    "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
                },
            ),
            conditions=[
                fnv1.Condition(type="ClusterReady", status=fnv1.STATUS_CONDITION_TRUE, reason="ClusterRunning"),
                fnv1.Condition(type="BackendReady", status=fnv1.STATUS_CONDITION_TRUE, reason="BackendHealthy"),
            ],
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: ComposeCase) -> None:
    """RunFunction composes the resources an InferenceCluster needs."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason


# The derived gateway hostname doubles as an SNI and a certificate SAN, so two
# clusters must never derive the same one. These cases call _gateway_hostname
# directly, because through RunFunction each cluster name would need a whole
# compose case, renaming every resource the function names after the cluster.
GATEWAY_HOSTNAME_CASES = [
    # A dotted cluster name is a DNS-1123 subdomain, but the first segment of
    # the hostname has to be one DNS-1035 label.
    #
    # With DashedTwin, this pins that eu.example and eu-example hash
    # differently: the hash covers the raw cluster name, before dots become
    # dashes. Sharing one hostname, a cluster's Service would shadow the other's
    # under a certificate it accepts.
    GatewayHostnameCase(
        name="DottedName",
        reason="A dotted cluster name becomes a single DNS label, hashed before its dots become dashes.",
        cluster_name="eu.example",
        want="gateway-eu-example-ad4f6.modelplane-system.svc.cluster.local",
    ),
    GatewayHostnameCase(
        name="DashedTwin",
        reason="A dashed cluster name hashes to a gateway hostname distinct from its dotted twin's.",
        cluster_name="eu-example",
        want="gateway-eu-example-1a2b0.modelplane-system.svc.cluster.local",
    ),
]


@pytest.mark.parametrize("case", GATEWAY_HOSTNAME_CASES, ids=lambda case: case.name)
def test_gateway_hostname(case: GatewayHostnameCase) -> None:
    """_gateway_hostname derives a cluster's gateway hostname."""
    assert fn._gateway_hostname(case.cluster_name) == case.want, case.reason
