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

"""Tests for the compose-upcloud-cluster function."""

import asyncio
import dataclasses
import json
from typing import Any

import pytest
from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format, message
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.infrastructure.upcloudcluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-upcloud-cluster."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _xr(
    *,
    credentials: v1alpha1.Credentials | None,
    autoscaler: v1alpha1.Autoscaler | None,
    node_pools: list[v1alpha1.NodePool],
) -> fnv1.Resource:
    """The observed UpCloudCluster XR in fi-hel2, with the given credentials, autoscaler and node pools."""
    xr = v1alpha1.UpCloudCluster(
        metadata=metav1.ObjectMeta(
            name="test-cluster",
            namespace="modelplane-system",
        ),
        spec=v1alpha1.Spec(
            zone="fi-hel2",
            credentials=credentials,
            autoscaler=autoscaler,
            nodePools=node_pools,
        ),
    )
    return fnv1.Resource(resource=resource.dict_to_struct(xr.model_dump(exclude_none=True, mode="json", by_alias=True)))


def _desired_xr(*, cluster_id: str | None) -> fnv1.Resource:
    """The desired XR, publishing the cluster's kubeconfig Secret and, once
    the cluster is observed, its UpCloud UUID."""
    status: dict[str, Any] = {
        "secrets": [
            {
                "type": "Kubeconfig",
                "name": "test-cluster-kubeconfig-55b57",
                "key": "kubeconfig",
            },
        ],
    }
    if cluster_id is not None:
        status["clusterId"] = cluster_id
    return fnv1.Resource(resource=resource.dict_to_struct({"status": status}))


def _network(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
    """The composed Network, a dedicated SDN network for the cluster."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "network.m.upcloud.com/v1alpha1",
                "kind": "Network",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "name": "test-cluster",
                        "zone": "fi-hel2",
                        "ipNetwork": {
                            "address": "10.0.1.0/24",
                            "dhcp": True,
                            "dhcpDefaultRoute": False,
                            "family": "IPv4",
                        },
                    },
                },
            }
        ),
    )


def _cluster(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed UKS KubernetesCluster."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "uks.m.upcloud.com/v1alpha1",
                "kind": "KubernetesCluster",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "name": "test-cluster",
                        "zone": "fi-hel2",
                        "version": "1.36",
                        "plan": "prod-md-ha",
                        "controlPlaneIpFilter": ["0.0.0.0/0"],
                        "fetchKubeconfig": True,
                        "networkSelector": {"matchControllerRef": True},
                    },
                    "writeConnectionSecretToRef": {"name": "test-cluster-kubeconfig-55b57"},
                },
            }
        ),
        ready=ready,
    )


def _observed_cluster(*, cred_kind: str, cred_name: str, ready: bool) -> fnv1.Resource:
    """The UKS cluster as observed, with its UpCloud UUID, and a Ready condition that's True if ready and False if not."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "uks.m.upcloud.com/v1alpha1",
                "kind": "KubernetesCluster",
                "metadata": {
                    "annotations": {"crossplane.io/external-name": "11111111-2222-3333-4444-555555555555"},
                },
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "name": "test-cluster",
                        "zone": "fi-hel2",
                        "version": "1.36",
                        "plan": "prod-md-ha",
                        "controlPlaneIpFilter": ["0.0.0.0/0"],
                        "fetchKubeconfig": True,
                        "networkSelector": {"matchControllerRef": True},
                    },
                    "writeConnectionSecretToRef": {"name": "test-cluster-kubeconfig-55b57"},
                },
                "status": {
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "True" if ready else "False",
                            "reason": "Available" if ready else "Unavailable",
                            "lastTransitionTime": "2024-01-01T00:00:00Z",
                        },
                    ],
                },
            }
        ),
    )


def _node_pool_system(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
    """The composed KubernetesNodeGroup for the system pool the function injects.

    The external-name annotation names the UKS node group, since the node
    group resource has no name of its own in forProvider.
    """
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "uks.m.upcloud.com/v1alpha1",
                "kind": "KubernetesNodeGroup",
                "metadata": {
                    "annotations": {"crossplane.io/external-name": "system"},
                },
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "plan": "PREMIUM-4xCPU-16GB",
                        "nodeCount": 2,
                        "labels": {"modelplane.ai/pool": "system"},
                        "clusterSelector": {"matchControllerRef": True},
                    },
                },
            }
        ),
    )


def _node_pool_gpu(
    *,
    management_policies: list[str] | None,
    init_node_count: int | None,
    node_count: int | None,
    ready: fnv1.Ready,
) -> fnv1.Resource:
    """The composed KubernetesNodeGroup for the gpu-l40s pool.

    An autoscaled pool's nodeCount is in initProvider, and its management
    policies leave out LateInitialize, so the cluster autoscaler owns the count
    once the pool exists. A fixed-size pool's nodeCount is in forProvider.
    """
    for_provider: dict[str, Any] = {
        "plan": "GPU-8xCPU-64GB-1xL40S",
        "gpuPlan": {"storageSize": 100, "storageTier": "maxiops"},
        "labels": {
            "modelplane.ai/pool": "gpu-l40s",
            "modelplane.ai/gpu": "nvidia-l40s",
        },
        "clusterSelector": {"matchControllerRef": True},
        "taint": [{"key": "nvidia.com/gpu", "value": "true", "effect": "NoSchedule"}],
    }
    if node_count is not None:
        for_provider["nodeCount"] = node_count
    spec: dict[str, Any] = {
        "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
        "forProvider": for_provider,
    }
    if management_policies is not None:
        spec["managementPolicies"] = management_policies
    if init_node_count is not None:
        spec["initProvider"] = {"nodeCount": init_node_count}
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "uks.m.upcloud.com/v1alpha1",
                "kind": "KubernetesNodeGroup",
                "metadata": {
                    "annotations": {"crossplane.io/external-name": "gpu-l40s"},
                },
                "spec": spec,
            }
        ),
        ready=ready,
    )


def _node_pool_cpu(*, node_count: int, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed KubernetesNodeGroup for the cpu-pool pool, with the CPU label and taint and no GPU labels.

    The Premium plan bundles a fixed disk, so no gpuPlan or
    cloudNativePlan block carries diskSizeGb.
    """
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "uks.m.upcloud.com/v1alpha1",
                "kind": "KubernetesNodeGroup",
                "metadata": {
                    "annotations": {"crossplane.io/external-name": "cpu-pool"},
                },
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                    "forProvider": {
                        "plan": "PREMIUM-8xCPU-32GB",
                        "nodeCount": node_count,
                        "labels": {
                            "modelplane.ai/pool": "cpu-pool",
                            "modelplane.ai/cpu": "true",
                        },
                        "clusterSelector": {"matchControllerRef": True},
                        "taint": [{"key": "modelplane.ai/cpu", "value": "true", "effect": "NoSchedule"}],
                    },
                },
            }
        ),
        ready=ready,
    )


def _provider_config_helm() -> fnv1.Resource:
    """The composed provider-helm ProviderConfig for the cluster, which is always ready."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "ProviderConfig",
                "metadata": {
                    "name": "test-cluster-kubeconfig-55b57",
                    "namespace": "modelplane-system",
                },
                "spec": {
                    "credentials": {
                        "source": "Secret",
                        "secretRef": {
                            "namespace": "modelplane-system",
                            "name": "test-cluster-kubeconfig-55b57",
                            "key": "kubeconfig",
                        },
                    },
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _provider_config_kubernetes() -> fnv1.Resource:
    """The composed provider-kubernetes ProviderConfig for the cluster, which is always ready."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "ProviderConfig",
                "metadata": {
                    "name": "test-cluster-kubeconfig-55b57",
                    "namespace": "modelplane-system",
                },
                "spec": {
                    "credentials": {
                        "source": "Secret",
                        "secretRef": {
                            "namespace": "modelplane-system",
                            "name": "test-cluster-kubeconfig-55b57",
                            "key": "kubeconfig",
                        },
                    },
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _autoscaler_secret() -> fnv1.Resource:
    """The composed Object copying the UpCloud API token to the cluster.

    provider-kubernetes resolves the patchesFrom reference server-side, so the
    token never appears in the Object's manifest on the control plane.
    """
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "managementPolicies": ["Observe", "Create", "Update"],
                    "providerConfigRef": {
                        "kind": "ProviderConfig",
                        "name": "test-cluster-kubeconfig-55b57",
                    },
                    "references": [
                        {
                            "patchesFrom": {
                                "apiVersion": "v1",
                                "kind": "Secret",
                                "name": "upcloud-api",
                                "namespace": "modelplane-system",
                                "fieldPath": "data.token",
                            },
                            "toFieldPath": "data.token",
                        },
                    ],
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "v1",
                            "kind": "Secret",
                            "metadata": {
                                "name": "upcloud-autoscaler",
                                "namespace": "kube-system",
                            },
                            "type": "Opaque",
                        },
                    },
                },
            }
        ),
    )


def _autoscaler_release(*, autoscaling_groups: list[dict]) -> fnv1.Resource:
    """The composed cluster autoscaler Release, scaling the given node groups."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "Release",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "managementPolicies": ["Observe", "Create", "Update"],
                    "providerConfigRef": {
                        "kind": "ProviderConfig",
                        "name": "test-cluster-kubeconfig-55b57",
                    },
                    "forProvider": {
                        "chart": {
                            "name": "cluster-autoscaler",
                            "repository": "https://kubernetes.github.io/autoscaler",
                            "version": "9.57.0",
                        },
                        "namespace": "kube-system",
                        "values": {
                            "cloudProvider": "upcloud",
                            "image": {
                                "repository": "ghcr.io/upcloudltd/autoscaler",
                                "tag": "v1.29.5",
                            },
                            "autoscalingGroups": autoscaling_groups,
                            "extraEnv": {
                                "UPCLOUD_CLUSTER_ID": "11111111-2222-3333-4444-555555555555",
                            },
                            "extraEnvSecrets": {
                                "UPCLOUD_TOKEN": {"name": "upcloud-autoscaler", "key": "token"},
                            },
                        },
                    },
                },
            }
        ),
    )


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


COMPOSE_CASES = [
    Case(
        name="FirstPass",
        reason="With nothing observed, an UpCloudCluster composes the network and cluster, and withholds the node groups, ProviderConfigs, autoscaler Secret and autoscaler until the cluster is Ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    autoscaler=v1alpha1.Autoscaler(secretRef=v1alpha1.SecretRef(name="upcloud-api")),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            plan="GPU-8xCPU-64GB-1xL40S",
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                    ],
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(cluster_id=None),
                resources={
                    "network": _network(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "cluster": _cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    Case(
        name="ClusterReady",
        reason="With the cluster observed Ready under its UpCloud UUID, an UpCloudCluster composes the node groups, ProviderConfigs, autoscaler token Secret and the autoscaler, scaling the GPU pool from one node to its maxNodeCount.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    autoscaler=v1alpha1.Autoscaler(secretRef=v1alpha1.SecretRef(name="upcloud-api")),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            plan="GPU-8xCPU-64GB-1xL40S",
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=True),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(cluster_id="11111111-2222-3333-4444-555555555555"),
                resources={
                    "network": _network(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-system": _node_pool_system(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=["Observe", "Create", "Update", "Delete"],
                        init_node_count=1,
                        node_count=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-helm": _provider_config_helm(),
                    "provider-config-kubernetes": _provider_config_kubernetes(),
                    "autoscaler-secret": _autoscaler_secret(),
                    "release-cluster-autoscaler": _autoscaler_release(
                        autoscaling_groups=[{"name": "gpu-l40s", "minSize": 1, "maxSize": 4}],
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # The observed ProviderConfig shows the dependents were composed before.
    # Dropping them would delete the KubernetesNodeGroups, and deprovision
    # their nodes.
    Case(
        name="ClusterReadyRegressed",
        reason="With the cluster's Ready condition regressed to False but its ProviderConfig observed, an UpCloudCluster keeps the node groups, ProviderConfigs, autoscaler Secret and autoscaler composed.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    autoscaler=v1alpha1.Autoscaler(secretRef=v1alpha1.SecretRef(name="upcloud-api")),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            plan="GPU-8xCPU-64GB-1xL40S",
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=False),
                    "provider-config-helm": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "ProviderConfig",
                                "metadata": {
                                    "name": "test-cluster-kubeconfig-55b57",
                                    "namespace": "modelplane-system",
                                },
                                "spec": {
                                    "credentials": {
                                        "source": "Secret",
                                        "secretRef": {
                                            "namespace": "modelplane-system",
                                            "name": "test-cluster-kubeconfig-55b57",
                                            "key": "kubeconfig",
                                        },
                                    },
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
                                },
                            }
                        ),
                    ),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(cluster_id="11111111-2222-3333-4444-555555555555"),
                resources={
                    "network": _network(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "cluster": _cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "node-pool-system": _node_pool_system(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=["Observe", "Create", "Update", "Delete"],
                        init_node_count=1,
                        node_count=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-helm": _provider_config_helm(),
                    "provider-config-kubernetes": _provider_config_kubernetes(),
                    "autoscaler-secret": _autoscaler_secret(),
                    "release-cluster-autoscaler": _autoscaler_release(
                        autoscaling_groups=[{"name": "gpu-l40s", "minSize": 1, "maxSize": 4}],
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    Case(
        name="MinNodeCount",
        reason="An autoscaled pool with minNodeCount set scales between that floor and maxNodeCount, and seeds the pool with nodeCount nodes.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    autoscaler=v1alpha1.Autoscaler(secretRef=v1alpha1.SecretRef(name="upcloud-api")),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            plan="GPU-8xCPU-64GB-1xL40S",
                            nodeCount=2,
                            minNodeCount=2,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=True),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(cluster_id="11111111-2222-3333-4444-555555555555"),
                resources={
                    "network": _network(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-system": _node_pool_system(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=["Observe", "Create", "Update", "Delete"],
                        init_node_count=2,
                        node_count=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-helm": _provider_config_helm(),
                    "provider-config-kubernetes": _provider_config_kubernetes(),
                    "autoscaler-secret": _autoscaler_secret(),
                    "release-cluster-autoscaler": _autoscaler_release(
                        autoscaling_groups=[{"name": "gpu-l40s", "minSize": 2, "maxSize": 4}],
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # Without spec.autoscaler nothing needs to reach into the cluster, so no
    # ProviderConfigs, token Secret or autoscaler Release are composed.
    Case(
        name="FixedSizePool",
        reason="An UpCloudCluster with a fixed-size GPU pool and no autoscaler credentials composes only the node groups, with nodeCount in forProvider under the default management policies.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    autoscaler=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            plan="GPU-8xCPU-64GB-1xL40S",
                            nodeCount=2,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=True),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(cluster_id="11111111-2222-3333-4444-555555555555"),
                resources={
                    "network": _network(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-system": _node_pool_system(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=None,
                        init_node_count=None,
                        node_count=2,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    Case(
        name="ClusterReadyCPUPool",
        reason=(
            "A CPU-role pool composes with the modelplane.ai/pool and modelplane.ai/cpu labels and the CPU taint, "
            "no GPU label, and no plan storage block - Premium plans bundle a fixed disk."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    autoscaler=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="cpu-pool",
                            role="CPU",
                            plan="PREMIUM-8xCPU-32GB",
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=True),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(cluster_id="11111111-2222-3333-4444-555555555555"),
                resources={
                    "network": _network(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-system": _node_pool_system(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "node-pool-cpu-pool": _node_pool_cpu(
                        node_count=2,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # The release excludes Delete, so dropping it from desired state when
    # spec.autoscaler goes away would orphan a running autoscaler that keeps
    # scaling its old node groups. The token Secret Object is dropped: its
    # source reference left with spec.autoscaler.
    Case(
        name="AutoscalerRemoved",
        reason="With spec.autoscaler removed after the release was composed, an UpCloudCluster keeps the ProviderConfigs and the release composed with an empty node group list, and drops the token Secret copy.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    autoscaler=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            plan="GPU-8xCPU-64GB-1xL40S",
                            nodeCount=2,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=True),
                    "release-cluster-autoscaler": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {"namespace": "modelplane-system"},
                            }
                        ),
                    ),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(cluster_id="11111111-2222-3333-4444-555555555555"),
                resources={
                    "network": _network(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-system": _node_pool_system(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=None,
                        init_node_count=None,
                        node_count=2,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-helm": _provider_config_helm(),
                    "provider-config-kubernetes": _provider_config_kubernetes(),
                    "release-cluster-autoscaler": _autoscaler_release(autoscaling_groups=[]),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    Case(
        name="CustomCredentials",
        reason="An UpCloudCluster naming its own ProviderConfig gets it on every UpCloud resource, and its System-role pool gets no GPU label or taint.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=v1alpha1.Credentials(type="ProviderConfig", name="team-a"),
                    autoscaler=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="workers",
                            role="System",
                            plan="PREMIUM-8xCPU-16GB",
                            nodeCount=2,
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(cred_kind="ProviderConfig", cred_name="team-a", ready=True),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(cluster_id="11111111-2222-3333-4444-555555555555"),
                resources={
                    "network": _network(cred_kind="ProviderConfig", cred_name="team-a"),
                    "cluster": _cluster(cred_kind="ProviderConfig", cred_name="team-a", ready=fnv1.READY_TRUE),
                    "node-pool-system": _node_pool_system(cred_kind="ProviderConfig", cred_name="team-a"),
                    "node-pool-workers": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "uks.m.upcloud.com/v1alpha1",
                                "kind": "KubernetesNodeGroup",
                                "metadata": {
                                    "annotations": {"crossplane.io/external-name": "workers"},
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "team-a"},
                                    "forProvider": {
                                        "plan": "PREMIUM-8xCPU-16GB",
                                        "nodeCount": 2,
                                        "labels": {"modelplane.ai/pool": "workers"},
                                        "clusterSelector": {"matchControllerRef": True},
                                    },
                                },
                            }
                        ),
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction composes an UpCloud cluster's infrastructure."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
