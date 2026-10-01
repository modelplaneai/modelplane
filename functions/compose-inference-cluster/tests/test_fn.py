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
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


@dataclasses.dataclass
class GatewayHostnameCase:
    """A test case for _gateway_hostname."""

    name: str
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
    *, cloud: str, secrets: list[dict], client_cas: list[dict] | None, ready: fnv1.Ready
) -> fnv1.Resource:
    """The composed ServingStack, its gateway accepting client_cas unless they're None."""
    gateway: dict = {"hostname": "gateway-test-cluster-09532.modelplane-system.svc.cluster.local"}
    if client_cas is not None:
        gateway["clientCAs"] = client_cas
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                "kind": "ServingStack",
                "metadata": {"name": "test-cluster-serving-stack-fd00b", "namespace": "modelplane-system"},
                "spec": {"cloud": cloud, "gateway": gateway, "stack": "Standard", "secrets": secrets},
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


def _guard_clusterusage(*, reason: str) -> fnv1.Resource:
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
        name="existing cluster with secrets composes backend and CPC",
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
    # A non-GCP identity threads the declared identity type into the CPC, and
    # into an extra ServingStack identity secret of the same type.
    ComposeCase(
        name="existing cluster with a non-GCP identity threads the identity type",
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
    # The first pass: no GKECluster observed yet, and the classes resolved.
    ComposeCase(
        name="GKE cluster first pass composes only the policy and the GKECluster XR",
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
        name="GKE credentials pass through to GKECluster spec",
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
    # The second pass: the ServingStack is observed ready, with a gateway
    # address.
    ComposeCase(
        name="existing cluster second pass with backend ready",
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
    # The first pass: no EKSCluster observed yet, and the classes resolved.
    ComposeCase(
        name="EKS cluster first pass composes only the policy and the EKSCluster XR",
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
    # The EKSCluster isn't observed yet, so neither is its kubeconfig, but a
    # ClusterProviderConfig is observed from a prior reconcile. The CPC is built
    # only from the kubeconfig, so without one it's left out of desired state
    # this reconcile, and recreated once the kubeconfig is observed again. It is
    # never emitted with an empty secretRef.
    ComposeCase(
        name="EKS cluster not ready omits the observed CPC",
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
    # The GKECluster is observed ready with its secrets, so the function
    # composes the CPC with the GKE service account identity, the ServingStack
    # with both secrets, and the Usage that blocks GKECluster deletion until the
    # ServingStack is gone. It relays the GKECluster's RWX StorageClass up to
    # status.cache.
    ComposeCase(
        name="GKE cluster ready composes CPC, backend and usage, and relays its RWX StorageClass",
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
    # The kubeconfig is observed on the EKSCluster status. The function wires
    # the ClusterProviderConfig, composes the ServingStack backend, and emits
    # the Usage that blocks EKSCluster deletion until the ServingStack is gone.
    # It marks the EKSCluster ready and relays its status.cache up to the
    # InferenceCluster's status.cache.
    ComposeCase(
        name="EKS cluster ready composes ServingStack and Usage",
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
    # The first pass with a node pool backed by a Capacity Block. The
    # reservation ID flows through to the EKSCluster node pool's capacityBlock,
    # which compose-eks-cluster turns into a CAPACITY_BLOCK node group.
    ComposeCase(
        name="EKS node pool with a Capacity Block sets capacityBlock on the EKSCluster pool",
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
    # The first pass with a node pool that opts into the EFA fabric. fabric.type
    # flows through to the EKSCluster node pool, which compose-eks-cluster turns
    # into EFA launch-template interfaces.
    ComposeCase(
        name="EKS node pool with fabric EFA sets fabric on the EKSCluster pool",
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
    # The first pass composes only the activation policy and the NebiusCluster
    # XR. The pool's InfiniBand fabric flows through to the NebiusCluster pool's
    # fabric, and minNodeCount stays unset so the pool's autoscaling floor
    # defaults to its node count downstream.
    ComposeCase(
        name="Nebius cluster first pass composes only the policy and the NebiusCluster XR",
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
    # The kubeconfig and service account credentials are observed on the
    # NebiusCluster status. The function wires the ClusterProviderConfig with
    # the Nebius identity (the mk8s kubeconfig has no embedded credentials),
    # composes the ServingStack backend with both secrets, and emits the Usage
    # that blocks NebiusCluster deletion until the ServingStack is gone. The
    # credentials Secret carries a namespace: it is the Nebius
    # ClusterProviderConfig's Secret, which lives outside modelplane-system.
    ComposeCase(
        name="Nebius cluster ready composes CPC with Nebius identity, ServingStack, and Usage",
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
    # The first pass composes only the activation policy and the AKSCluster XR.
    # The pool's InfiniBand fabric flows through to the AKSCluster pool as the
    # plain fabric string - Azure has no user-selectable fabric ID. The pool
    # sets minNodeCount to 1, as an AKS GPU pool must, because the AKS
    # autoscaler can't scale a DRA pool up from zero nodes.
    ComposeCase(
        name="AKS cluster first pass composes only the policy and the AKSCluster XR",
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
    # The kubeconfig is observed on the AKSCluster status. It embeds a client
    # certificate, so the ClusterProviderConfig carries no identity (unlike
    # GKE and Nebius). The function composes the ServingStack backend and the
    # Usage that blocks AKSCluster deletion until the ServingStack is gone, and
    # relays the AKSCluster's status.cache up to status.cache.
    ComposeCase(
        name="AKS cluster ready composes CPC without identity, ServingStack, and Usage",
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
    # While the policy is missing even one of the kinds from status.activated
    # (e.g. a provider still installing), and with no cluster observed, the
    # function composes only the activation policy, not the cluster XR. It
    # doesn't mark the policy ready, so the composite doesn't report ready.
    # Observing the policy with a kind missing, here
    # nodepools.container.gcp.m.upbound.io, exercises the all-kinds check rather
    # than the policy-absent branch.
    ComposeCase(
        name="cloud cluster not activated composes only the policy",
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
    # the policy momentarily stops reporting the kinds active, so an activation
    # blip never drops a provisioned cluster from desired state. Here the
    # policy isn't observed at all.
    ComposeCase(
        name="observed cluster keeps composing through an activation blip",
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
    # The first pass composes only the activation policy and the VultrCluster
    # XR. minNodeCount stays unset so the pool's autoscaling floor defaults to
    # its node count downstream.
    ComposeCase(
        name="Vultr cluster first pass composes only the policy and the VultrCluster XR",
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
    # Vultr credentials pass through to the VultrCluster spec, mirroring the GKE
    # passthrough.
    ComposeCase(
        name="Vultr credentials pass through to VultrCluster spec",
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
    # The kubeconfig is observed on the VultrCluster status. The VKE kubeconfig
    # embeds static client certificates, so the ClusterProviderConfig carries no
    # identity (unlike Nebius). The function composes the ServingStack backend
    # with the kubeconfig and emits the Usage that blocks VultrCluster deletion
    # until the ServingStack is gone. VultrCluster reports no cache
    # StorageClass, so status.cache stays unset.
    ComposeCase(
        name="Vultr cluster ready composes CPC without identity, ServingStack, and Usage",
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
    # The deletion guard cases up to the early return observe an existing
    # cluster along with whatever uses it.
    #
    # ModelReplicas, ModelRoutes and ModelCaches across several namespaces
    # compose a single reason-only ClusterUsage blocking the InferenceCluster's
    # deletion, whatever their count or namespace, and mirror the deduplicated
    # union of their namespaces: team-a (replica), team-b (replica and route),
    # team-c (route), team-d (cache staging onto this cluster). A cache staging
    # only onto another cluster (team-e) is filtered out by its
    # status.clusters[], proving the namespaces track what actually lands here.
    # The guard's reason names every kind in use.
    ComposeCase(
        name="ModelReplicas, ModelRoutes and ModelCaches compose the guard and the mirrored namespaces",
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
                    "usage-replicas": _guard_clusterusage(
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
    # ClusterProviderConfig, so it blocks deletion without any replica there,
    # and its team's namespace is mirrored.
    ComposeCase(
        name="a ModelRoute on the cluster composes the guard",
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
                    "usage-replicas": _guard_clusterusage(reason="ModelRoutes use this InferenceCluster"),
                    "namespace-team-c": _namespace_object(team="team-c", name="mp-team-c-d79d9"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Existing",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        client_cas=None,
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
    # so it blocks deletion without any replica there, and its team's namespace
    # is mirrored.
    ComposeCase(
        name="a ModelCache staging onto the cluster composes the guard",
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
                    "usage-replicas": _guard_clusterusage(reason="ModelCaches use this InferenceCluster"),
                    "namespace-team-d": _namespace_object(team="team-d", name="mp-team-d-c2383"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Existing",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        client_cas=None,
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
    # cluster's ClusterProviderConfig just as a replica does, so it blocks
    # deletion too. It is cluster scoped, so it mirrors no namespace.
    ComposeCase(
        name="an InferenceGateway on the cluster composes the guard",
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
                    "usage-replicas": _guard_clusterusage(reason="InferenceGateways use this InferenceCluster"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Existing",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        client_cas=None,
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
    # Every guard requirement resolves to nothing on this cluster, so there's
    # no ClusterUsage. This is the teardown transition - the last user is gone,
    # so the function stops composing the guard and the cluster becomes
    # deletable. A cache staging onto another cluster and a gateway running on
    # another cluster don't hold it. The replica and route requirements are
    # empty but present, as Crossplane returns them when a selector matches
    # nothing.
    ComposeCase(
        name="nothing on the cluster leaves it deletable",
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
    # The guard is composed even when compose() returns early. resolve_classes()
    # returns False whenever a referenced InferenceClass isn't observed yet - a
    # routine transient. Here the class requirement is declared but not
    # fulfilled. The function returns before composing the cluster, but the
    # guard runs first, so a referencing replica still blocks deletion. This is
    # the case that regresses if the guard is gated behind class resolution or
    # cluster source.
    #
    # Only the guard and namespace are composed, both ready, so the function
    # marks the XR not ready itself. Otherwise it would read ready while it
    # waits for its classes.
    ComposeCase(
        name="guard is composed even when compose returns early",
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
                    "usage-replicas": _guard_clusterusage(reason="ModelReplicas use this InferenceCluster"),
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
    # An address to reach, this cluster's CA so an InferenceGateway can tell it
    # reached the right cluster, and an InferenceGateway CA so the cluster
    # gateway demands a client certificate.
    ComposeCase(
        name="hostname is published once both directions are authenticated",
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
                    "usage-replicas": _guard_clusterusage(reason="InferenceGateways use this InferenceCluster"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Existing",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        client_cas=[{"name": "fleet-0", "certificate": "fleet-ca"}],
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
        name="no hostname without an InferenceGateway CA",
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
        name="no hostname without this cluster's CA",
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
                    "usage-replicas": _guard_clusterusage(reason="InferenceGateways use this InferenceCluster"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Existing",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        client_cas=[{"name": "fleet-0", "certificate": "fleet-ca"}],
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
        name="no gateway status before an address",
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
                    "usage-replicas": _guard_clusterusage(reason="InferenceGateways use this InferenceCluster"),
                    "cluster-provider-config-kubernetes": _cluster_provider_config(
                        kubeconfig="my-kubeconfig", identity=None
                    ),
                    "serving-stack": _serving_stack(
                        cloud="Existing",
                        secrets=[{"type": "Kubeconfig", "name": "my-kubeconfig", "key": "kubeconfig"}],
                        client_cas=[{"name": "fleet-0", "certificate": "fleet-ca"}],
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
    # Any InferenceGateway may forward to this cluster, so its gateway accepts
    # every published CA, whichever cluster the InferenceGateway runs on. These
    # CAs are what switches the cluster gateway's mTLS listener on. One that
    # hasn't published a CA yet is left out rather than holding the others
    # back, and the list is sorted so it doesn't churn.
    ComposeCase(
        name="ServingStack accepts every InferenceGateway CA",
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
                    "usage-replicas": _guard_clusterusage(reason="InferenceGateways use this InferenceCluster"),
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
    assert _to_dict(got) == _to_dict(case.want)


# The derived gateway hostname doubles as an SNI and a certificate SAN, so two
# clusters must never derive the same one. These cases call _gateway_hostname
# directly, because through RunFunction each cluster name would need a whole
# compose case, renaming every resource the function names after the cluster.
GATEWAY_HOSTNAME_CASES = [
    # A dotted cluster name is a DNS-1123 subdomain, but the first segment of
    # the hostname has to be one DNS-1035 label.
    GatewayHostnameCase(
        name="dots become a single DNS label",
        cluster_name="eu.example",
        want="gateway-eu-example-ad4f6.modelplane-system.svc.cluster.local",
    ),
    # With the case above, this pins that eu.example and eu-example hash
    # differently: the hash covers the raw cluster name, before dots become
    # dashes. Sharing one hostname, a cluster's Service would shadow the other's
    # under a certificate it accepts.
    GatewayHostnameCase(
        name="the dashed twin of a dotted name gets its own hash",
        cluster_name="eu-example",
        want="gateway-eu-example-1a2b0.modelplane-system.svc.cluster.local",
    ),
]


@pytest.mark.parametrize("case", GATEWAY_HOSTNAME_CASES, ids=lambda case: case.name)
def test_gateway_hostname(case: GatewayHostnameCase) -> None:
    """_gateway_hostname derives a cluster's gateway hostname."""
    assert fn._gateway_hostname(case.cluster_name) == case.want
