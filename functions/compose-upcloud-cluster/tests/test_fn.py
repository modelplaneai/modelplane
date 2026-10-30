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


def _desired_xr(*, node_pools: list[dict]) -> fnv1.Resource:
    """The desired XR, publishing node_pools' states and the cluster's kubeconfig Secret."""
    status: dict[str, Any] = {
        "nodePools": node_pools,
        "secrets": [
            {
                "type": "Kubeconfig",
                "name": "test-cluster-kubeconfig-55b57",
                "key": "kubeconfig",
            },
        ],
    }
    return fnv1.Resource(resource=resource.dict_to_struct({"status": status}))


def _network(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
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
        ready=ready,
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


def _node_pool_system(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
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
        ready=ready,
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
                "metadata": {
                    "namespace": "modelplane-system",
                    "annotations": {"crossplane.io/external-name": "mp-cluster-autoscaler"},
                },
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


def _observed_managed_resource(*, ready: bool, failure: str | None) -> fnv1.Resource:
    """An observed managed resource whose Ready condition is True if ready and False if not, and whose
    LastAsyncOperation condition is False with failure as its message unless failure is None."""
    conditions = [
        {
            "type": "Ready",
            "status": "True" if ready else "False",
            "reason": "Available" if ready else "Creating",
            "lastTransitionTime": "2024-01-01T00:00:00Z",
        },
    ]
    if failure is not None:
        conditions.append(
            {
                "type": "LastAsyncOperation",
                "status": "False",
                "reason": "AsyncCreateFailure",
                "message": failure,
                "lastTransitionTime": "2024-01-01T00:00:00Z",
            }
        )
    return fnv1.Resource(resource=resource.dict_to_struct({"status": {"conditions": conditions}}))


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
                composite=_desired_xr(node_pools=[{"name": "gpu-l40s", "state": "Pending"}]),
                resources={
                    "network": _network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="0 of 2 resources ready",
                ),
                fnv1.Condition(
                    type="NodePoolsReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Pending",
                    message="Waiting for ClusterReady",
                ),
            ],
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
                composite=_desired_xr(node_pools=[{"name": "gpu-l40s", "state": "Provisioning"}]),
                resources={
                    "network": _network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-system": _node_pool_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
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
                        autoscaling_groups=[
                            {"name": "system", "minSize": 2, "maxSize": 2},
                            {"name": "gpu-l40s", "minSize": 1, "maxSize": 4},
                        ],
                    ),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="3 of 7 resources ready",
                ),
                fnv1.Condition(
                    type="NodePoolsReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Pending",
                    message="Waiting for ClusterReady",
                ),
            ],
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
                composite=_desired_xr(node_pools=[{"name": "gpu-l40s", "state": "Provisioning"}]),
                resources={
                    "network": _network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "node-pool-system": _node_pool_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
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
                        autoscaling_groups=[
                            {"name": "system", "minSize": 2, "maxSize": 2},
                            {"name": "gpu-l40s", "minSize": 1, "maxSize": 4},
                        ],
                    ),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="2 of 7 resources ready",
                ),
                fnv1.Condition(
                    type="NodePoolsReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Pending",
                    message="Waiting for ClusterReady",
                ),
            ],
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
                composite=_desired_xr(node_pools=[{"name": "gpu-l40s", "state": "Provisioning"}]),
                resources={
                    "network": _network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-system": _node_pool_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
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
                        autoscaling_groups=[
                            {"name": "system", "minSize": 2, "maxSize": 2},
                            {"name": "gpu-l40s", "minSize": 2, "maxSize": 4},
                        ],
                    ),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="3 of 7 resources ready",
                ),
                fnv1.Condition(
                    type="NodePoolsReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Pending",
                    message="Waiting for ClusterReady",
                ),
            ],
        ),
    ),
    # The fork lists every node group in the cluster and scales any group
    # missing from the autoscaler's list between one node and the cluster
    # plan's limit, so a fixed pool must appear pinned at its node count
    # whenever another pool autoscales.
    Case(
        name="FixedPoolPinned",
        reason="A fixed-size pool beside an autoscaled one is listed in the autoscaler's groups pinned at its node count, keeping the autoscaler off it.",
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
                            nodeCount=1,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
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
                composite=_desired_xr(
                    node_pools=[
                        {"name": "gpu-l40s", "state": "Provisioning"},
                        {"name": "cpu-pool", "state": "Provisioning"},
                    ]
                ),
                resources={
                    "network": _network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-system": _node_pool_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=["Observe", "Create", "Update", "Delete"],
                        init_node_count=1,
                        node_count=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "node-pool-cpu-pool": _node_pool_cpu(
                        node_count=2,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-helm": _provider_config_helm(),
                    "provider-config-kubernetes": _provider_config_kubernetes(),
                    "autoscaler-secret": _autoscaler_secret(),
                    "release-cluster-autoscaler": _autoscaler_release(
                        autoscaling_groups=[
                            {"name": "system", "minSize": 2, "maxSize": 2},
                            {"name": "gpu-l40s", "minSize": 1, "maxSize": 4},
                            {"name": "cpu-pool", "minSize": 2, "maxSize": 2},
                        ],
                    ),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="3 of 7 resources ready",
                ),
                fnv1.Condition(
                    type="NodePoolsReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Pending",
                    message="Waiting for ClusterReady",
                ),
            ],
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
                composite=_desired_xr(node_pools=[{"name": "gpu-l40s", "state": "Provisioning"}]),
                resources={
                    "network": _network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-system": _node_pool_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=None,
                        init_node_count=None,
                        node_count=2,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="1 of 3 resources ready",
                ),
                fnv1.Condition(
                    type="NodePoolsReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Pending",
                    message="Waiting for ClusterReady",
                ),
            ],
        ),
    ),
    # The node groups are the only sentinel on a cluster without an
    # autoscaler: nothing composes a ProviderConfig there, so an observed
    # node group alone must keep the Ready-gated block composed when the
    # cluster's Ready condition regresses - dropping the groups would
    # deprovision their nodes.
    Case(
        name="ReadyRegressedNoAutoscaler",
        reason="With the cluster's Ready condition regressed to False, no autoscaler and a node group observed, an UpCloudCluster keeps the node groups composed.",
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
                    "cluster": _observed_cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=False),
                    "node-pool-system": _node_pool_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "gpu-l40s", "state": "Provisioning"}]),
                resources={
                    "network": _network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "node-pool-system": _node_pool_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=None,
                        init_node_count=None,
                        node_count=2,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="0 of 3 resources ready",
                ),
                fnv1.Condition(
                    type="NodePoolsReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Pending",
                    message="Waiting for ClusterReady",
                ),
            ],
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
                composite=_desired_xr(node_pools=[{"name": "cpu-pool", "state": "Provisioning"}]),
                resources={
                    "network": _network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-system": _node_pool_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "node-pool-cpu-pool": _node_pool_cpu(
                        node_count=2,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="1 of 3 resources ready",
                ),
                fnv1.Condition(
                    type="NodePoolsReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Pending",
                    message="Waiting for ClusterReady",
                ),
            ],
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
                composite=_desired_xr(node_pools=[{"name": "gpu-l40s", "state": "Provisioning"}]),
                resources={
                    "network": _network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-system": _node_pool_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
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
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="3 of 6 resources ready",
                ),
                fnv1.Condition(
                    type="NodePoolsReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Pending",
                    message="Waiting for ClusterReady",
                ),
            ],
        ),
    ),
    Case(
        name="CustomCredentials",
        reason="An UpCloudCluster naming its own ProviderConfig gets it on every UpCloud resource, and its System-role pool gets no GPU label or taint, counts under ClusterReady, and is left out of status.nodePools.",
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
                composite=_desired_xr(node_pools=[]),
                resources={
                    "network": _network(cred_kind="ProviderConfig", cred_name="team-a", ready=fnv1.READY_UNSPECIFIED),
                    "cluster": _cluster(cred_kind="ProviderConfig", cred_name="team-a", ready=fnv1.READY_TRUE),
                    "node-pool-system": _node_pool_system(
                        cred_kind="ProviderConfig", cred_name="team-a", ready=fnv1.READY_UNSPECIFIED
                    ),
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
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="1 of 4 resources ready",
                ),
                fnv1.Condition(
                    type="NodePoolsReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Pending",
                    message="Waiting for ClusterReady",
                ),
            ],
        ),
    ),
    Case(
        name="PoolsProvisioning",
        reason="With every cluster resource observed Ready and the cpu-pool node group unready, ClusterReady is True, NodePoolsReady is Provisioning, and only gpu-l40s is Ready.",
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
                        v1alpha1.NodePool(name="cpu-pool", role="CPU", plan="PREMIUM-8xCPU-32GB", nodeCount=2),
                    ],
                ),
                resources={
                    "network": _observed_managed_resource(ready=True, failure=None),
                    "cluster": _observed_cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=True),
                    "node-pool-system": _observed_managed_resource(ready=True, failure=None),
                    "node-pool-gpu-l40s": _observed_managed_resource(ready=True, failure=None),
                    "node-pool-cpu-pool": _observed_managed_resource(ready=False, failure=None),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(
                    node_pools=[{"name": "gpu-l40s", "state": "Ready"}, {"name": "cpu-pool", "state": "Provisioning"}]
                ),
                resources={
                    "network": _network(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-system": _node_pool_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=None,
                        init_node_count=None,
                        node_count=2,
                        ready=fnv1.READY_TRUE,
                    ),
                    "node-pool-cpu-pool": _node_pool_cpu(node_count=2, ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Available",
                ),
                fnv1.Condition(
                    type="NodePoolsReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="1 of 2 node pools ready",
                ),
            ],
        ),
    ),
    Case(
        name="AllReady",
        reason="With every resource observed Ready, ClusterReady and NodePoolsReady are True and every pool is Ready.",
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
                        v1alpha1.NodePool(name="cpu-pool", role="CPU", plan="PREMIUM-8xCPU-32GB", nodeCount=2),
                    ],
                ),
                resources={
                    "network": _observed_managed_resource(ready=True, failure=None),
                    "cluster": _observed_cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=True),
                    "node-pool-system": _observed_managed_resource(ready=True, failure=None),
                    "node-pool-gpu-l40s": _observed_managed_resource(ready=True, failure=None),
                    "node-pool-cpu-pool": _observed_managed_resource(ready=True, failure=None),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(
                    node_pools=[{"name": "gpu-l40s", "state": "Ready"}, {"name": "cpu-pool", "state": "Ready"}]
                ),
                resources={
                    "network": _network(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-system": _node_pool_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=None,
                        init_node_count=None,
                        node_count=2,
                        ready=fnv1.READY_TRUE,
                    ),
                    "node-pool-cpu-pool": _node_pool_cpu(node_count=2, ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Available",
                ),
                fnv1.Condition(
                    type="NodePoolsReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Available",
                ),
            ],
        ),
    ),
    Case(
        name="PoolFailed",
        reason="With the provider reporting the gpu-l40s node group's LastAsyncOperation False, that pool is Failed with the provider's error prefixed by the resource name, and NodePoolsReady is Failed naming it.",
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
                        v1alpha1.NodePool(name="cpu-pool", role="CPU", plan="PREMIUM-8xCPU-32GB", nodeCount=2),
                    ],
                ),
                resources={
                    "network": _observed_managed_resource(ready=True, failure=None),
                    "cluster": _observed_cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=True),
                    "node-pool-system": _observed_managed_resource(ready=True, failure=None),
                    "node-pool-gpu-l40s": _observed_managed_resource(
                        ready=False, failure="creating node group: quota exceeded"
                    ),
                    "node-pool-cpu-pool": _observed_managed_resource(ready=True, failure=None),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(
                    node_pools=[
                        {
                            "name": "gpu-l40s",
                            "state": "Failed",
                            "message": "node-pool-gpu-l40s: creating node group: quota exceeded",
                        },
                        {"name": "cpu-pool", "state": "Ready"},
                    ]
                ),
                resources={
                    "network": _network(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-system": _node_pool_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=None,
                        init_node_count=None,
                        node_count=2,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "node-pool-cpu-pool": _node_pool_cpu(node_count=2, ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Available",
                ),
                fnv1.Condition(
                    type="NodePoolsReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Failed",
                    message="1 of 2 node pools failed: gpu-l40s",
                ),
            ],
        ),
    ),
    Case(
        name="ClusterFailed",
        reason="With the provider reporting the network's and cluster's LastAsyncOperation False, ClusterReady is Failed with the first failed resource's error and a count of the rest, NodePoolsReady is Pending on it, and each pool is Pending because its node group isn't composed.",
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
                        v1alpha1.NodePool(name="cpu-pool", role="CPU", plan="PREMIUM-8xCPU-32GB", nodeCount=2),
                    ],
                ),
                resources={
                    "network": _observed_managed_resource(ready=False, failure="creating network: zone unavailable"),
                    "cluster": _observed_managed_resource(ready=False, failure="creating cluster: quota exceeded"),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(
                    node_pools=[{"name": "gpu-l40s", "state": "Pending"}, {"name": "cpu-pool", "state": "Pending"}]
                ),
                resources={
                    "network": _network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Failed",
                    message="cluster: creating cluster: quota exceeded (and 1 more)",
                ),
                fnv1.Condition(
                    type="NodePoolsReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Pending",
                    message="Waiting for ClusterReady",
                ),
            ],
        ),
    ),
    Case(
        name="ClusterRegressedPoolsReady",
        reason="With the cluster observed unready after its pools came up, ClusterReady is Provisioning and NodePoolsReady reports the pools' own state, True, rather than Pending on ClusterReady.",
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
                        v1alpha1.NodePool(name="cpu-pool", role="CPU", plan="PREMIUM-8xCPU-32GB", nodeCount=2),
                    ],
                ),
                resources={
                    "network": _observed_managed_resource(ready=True, failure=None),
                    "cluster": _observed_cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=False),
                    "node-pool-system": _observed_managed_resource(ready=True, failure=None),
                    "node-pool-gpu-l40s": _observed_managed_resource(ready=True, failure=None),
                    "node-pool-cpu-pool": _observed_managed_resource(ready=True, failure=None),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(
                    node_pools=[{"name": "gpu-l40s", "state": "Ready"}, {"name": "cpu-pool", "state": "Ready"}]
                ),
                resources={
                    "network": _network(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "cluster": _cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "node-pool-system": _node_pool_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=None,
                        init_node_count=None,
                        node_count=2,
                        ready=fnv1.READY_TRUE,
                    ),
                    "node-pool-cpu-pool": _node_pool_cpu(node_count=2, ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="2 of 3 resources ready",
                ),
                fnv1.Condition(
                    type="NodePoolsReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Available",
                ),
            ],
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction composes an UpCloud cluster's infrastructure."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
