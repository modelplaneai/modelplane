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

"""Tests for the compose-civo-cluster function."""

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
from models.ai.modelplane.infrastructure.civocluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-civo-cluster."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _xr(*, credentials: v1alpha1.Credentials | None, node_pools: list[v1alpha1.NodePool]) -> fnv1.Resource:
    """The observed CivoCluster XR in LON1, with the given credentials and node pools."""
    xr = v1alpha1.CivoCluster(
        metadata=metav1.ObjectMeta(
            name="test-cluster",
            namespace="modelplane-system",
        ),
        spec=v1alpha1.Spec(
            region="LON1",
            credentials=credentials,
            nodePools=node_pools,
        ),
    )
    return fnv1.Resource(resource=resource.dict_to_struct(xr.model_dump(exclude_none=True, mode="json", by_alias=True)))


def _desired_xr(*, node_pools: list[dict]) -> fnv1.Resource:
    """The desired XR, publishing node_pools' states and the cluster's kubeconfig Secret."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "status": {
                    "nodePools": node_pools,
                    "secrets": [
                        {
                            "type": "Kubeconfig",
                            "name": "test-cluster-kubeconfig-55b57",
                            "key": "kubeconfig",
                        },
                    ],
                },
            }
        ),
    )


def _network(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed Network."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "vpc.civo.m.upbound.io/v1beta1",
                "kind": "Network",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "label": "test-cluster",
                        "region": "LON1",
                    },
                },
            }
        ),
        ready=ready,
    )


def _firewall(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed Firewall, with Civo's default rules."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "vpc.civo.m.upbound.io/v1beta1",
                "kind": "Firewall",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "name": "test-cluster",
                        "region": "LON1",
                        "createDefaultRules": True,
                        "networkIdSelector": {"matchControllerRef": True},
                    },
                },
            }
        ),
        ready=ready,
    )


def _cluster(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed Civo Cluster."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.civo.m.upbound.io/v1beta1",
                "kind": "Cluster",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "name": "test-cluster",
                        "region": "LON1",
                        "cni": "cilium",
                        "applications": "-traefik2-nodeport",
                        "writeKubeconfig": True,
                        "networkIdSelector": {"matchControllerRef": True},
                        "firewallIdSelector": {"matchControllerRef": True},
                        # The system node pool the function adds inline to every cluster.
                        "pools": {
                            "label": "system",
                            "size": "g4p.kube.small",
                            "nodeCount": 2,
                            "labels": {"modelplane.ai/pool": "system"},
                        },
                    },
                    "writeConnectionSecretToRef": {"name": "test-cluster-kubeconfig-55b57"},
                },
            }
        ),
        ready=ready,
    )


def _observed_cluster(*, cred_kind: str, cred_name: str, ready: bool) -> fnv1.Resource:
    """The Civo Cluster as observed, with its Civo ID, and a Ready condition that's True if ready and False if not."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.civo.m.upbound.io/v1beta1",
                "kind": "Cluster",
                "metadata": {
                    "annotations": {"crossplane.io/external-name": "11111111-2222-3333-4444-555555555555"},
                },
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "name": "test-cluster",
                        "region": "LON1",
                        "cni": "cilium",
                        "applications": "-traefik2-nodeport",
                        "writeKubeconfig": True,
                        "networkIdSelector": {"matchControllerRef": True},
                        "firewallIdSelector": {"matchControllerRef": True},
                        "pools": {
                            "label": "system",
                            "size": "g4p.kube.small",
                            "nodeCount": 2,
                            "labels": {"modelplane.ai/pool": "system"},
                        },
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


def _node_pool_gpu(
    *,
    management_policies: list[str] | None,
    init_node_count: int | None,
    node_count: int | None,
    ready: fnv1.Ready,
) -> fnv1.Resource:
    """The composed NodePool for the gpu-l40s pool.

    An autoscaled pool's nodeCount is in initProvider, and its management
    policies leave out LateInitialize, so the cluster autoscaler owns the count
    once the pool exists. A fixed-size pool's nodeCount is in forProvider.
    """
    for_provider: dict[str, Any] = {
        "label": "gpu-l40s",
        "size": "an.g1.l40s.kube.x1",
        "region": "LON1",
        "labels": {
            "modelplane.ai/pool": "gpu-l40s",
            "modelplane.ai/gpu": "nvidia-l40s",
        },
        "clusterIdSelector": {"matchControllerRef": True},
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
                "apiVersion": "kubernetes.civo.m.upbound.io/v1beta1",
                "kind": "NodePool",
                "spec": spec,
            }
        ),
        ready=ready,
    )


def _node_pool_cpu(
    *,
    management_policies: list[str] | None,
    init_node_count: int | None,
    node_count: int | None,
    ready: fnv1.Ready,
) -> fnv1.Resource:
    """The composed NodePool for the cpu-pool pool, with the CPU label and taint and no GPU label."""
    for_provider: dict[str, Any] = {
        "label": "cpu-pool",
        "size": "g4s.kube.large",
        "region": "LON1",
        "labels": {
            "modelplane.ai/pool": "cpu-pool",
            "modelplane.ai/cpu": "true",
        },
        "clusterIdSelector": {"matchControllerRef": True},
        "taint": [{"key": "modelplane.ai/cpu", "value": "true", "effect": "NoSchedule"}],
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
                "apiVersion": "kubernetes.civo.m.upbound.io/v1beta1",
                "kind": "NodePool",
                "spec": spec,
            }
        ),
        ready=ready,
    )


def _provider_config() -> fnv1.Resource:
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


def _autoscaler_release(*, autoscaling_groups: list[dict], ready: fnv1.Ready) -> fnv1.Resource:
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
                            "cloudProvider": "civo",
                            "autoscalingGroups": autoscaling_groups,
                            "secretKeyRefNameOverride": "civo-api-access",
                        },
                    },
                },
            }
        ),
        ready=ready,
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
        reason="With nothing observed, a CivoCluster composes the network, firewall and cluster, and withholds the node pools, ProviderConfig and autoscaler until the cluster is Ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            size="an.g1.l40s.kube.x1",
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
                    "firewall": _firewall(
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
        name="ClusterReady",
        reason="With the cluster observed Ready, a CivoCluster composes the node pools, ProviderConfig and autoscaler, which has no node groups until a pool's Civo ID is observed.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            size="an.g1.l40s.kube.x1",
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
                    "firewall": _firewall(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=["Observe", "Create", "Update", "Delete"],
                        init_node_count=1,
                        node_count=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-helm": _provider_config(),
                    "release-cluster-autoscaler": _autoscaler_release(
                        autoscaling_groups=[], ready=fnv1.READY_UNSPECIFIED
                    ),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="2 of 5 resources ready",
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
        reason="A CPU-role pool composes with the modelplane.ai/pool and modelplane.ai/cpu labels and the CPU taint, and no GPU label.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="cpu-pool",
                            role="CPU",
                            size="g4s.kube.large",
                            maxNodeCount=4,
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
                    "firewall": _firewall(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-cpu-pool": _node_pool_cpu(
                        management_policies=["Observe", "Create", "Update", "Delete"],
                        init_node_count=1,
                        node_count=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-helm": _provider_config(),
                    "release-cluster-autoscaler": _autoscaler_release(
                        autoscaling_groups=[], ready=fnv1.READY_UNSPECIFIED
                    ),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="2 of 5 resources ready",
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
    # The autoscaler addresses node groups by the pool ID the provider writes
    # to the NodePool's external-name annotation once the pool exists. The pool
    # leaves nodeCount at its default of 1, so this case can't tell a floor
    # taken from nodeCount from one fixed at 1.
    Case(
        name="PoolIDObserved",
        reason="With the GPU pool observed Ready under its Civo ID, a CivoCluster marks the pool ready, reports NodePoolsReady True from it while ClusterReady is Provisioning, and has the autoscaler scale that ID from one node to its maxNodeCount.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            size="an.g1.l40s.kube.x1",
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=True),
                    "node-pool-gpu-l40s": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.civo.m.upbound.io/v1beta1",
                                "kind": "NodePool",
                                "metadata": {
                                    "annotations": {
                                        "crossplane.io/external-name": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                                    },
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "managementPolicies": ["Observe", "Create", "Update", "Delete"],
                                    "initProvider": {"nodeCount": 1},
                                    "forProvider": {
                                        "label": "gpu-l40s",
                                        "size": "an.g1.l40s.kube.x1",
                                        "region": "LON1",
                                        "labels": {
                                            "modelplane.ai/pool": "gpu-l40s",
                                            "modelplane.ai/gpu": "nvidia-l40s",
                                        },
                                        "clusterIdSelector": {"matchControllerRef": True},
                                        "taint": [{"key": "nvidia.com/gpu", "value": "true", "effect": "NoSchedule"}],
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
                composite=_desired_xr(node_pools=[{"name": "gpu-l40s", "state": "Ready"}]),
                resources={
                    "network": _network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "firewall": _firewall(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=["Observe", "Create", "Update", "Delete"],
                        init_node_count=1,
                        node_count=None,
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-helm": _provider_config(),
                    "release-cluster-autoscaler": _autoscaler_release(
                        autoscaling_groups=[
                            {"name": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "minSize": 1, "maxSize": 4}
                        ],
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
                    message="2 of 5 resources ready",
                ),
                fnv1.Condition(
                    type="NodePoolsReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Available",
                ),
            ],
        ),
    ),
    # The observed ProviderConfig shows the dependents were composed before.
    # Dropping them would delete the NodePools, and deprovision their nodes.
    Case(
        name="ClusterReadyRegressed",
        reason="With the cluster's Ready condition regressed to False but its ProviderConfig observed, a CivoCluster keeps the node pools, ProviderConfig and autoscaler composed.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            size="an.g1.l40s.kube.x1",
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
                    "firewall": _firewall(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=["Observe", "Create", "Update", "Delete"],
                        init_node_count=1,
                        node_count=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-helm": _provider_config(),
                    "release-cluster-autoscaler": _autoscaler_release(
                        autoscaling_groups=[], ready=fnv1.READY_UNSPECIFIED
                    ),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="1 of 5 resources ready",
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
        name="FixedSizePool",
        reason="A CivoCluster with a GPU pool that sets nodeCount but no maxNodeCount puts that nodeCount in forProvider under the default management policies, and still composes the autoscaler with no node groups.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            size="an.g1.l40s.kube.x1",
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
                    "firewall": _firewall(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=None,
                        init_node_count=None,
                        node_count=2,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-helm": _provider_config(),
                    "release-cluster-autoscaler": _autoscaler_release(
                        autoscaling_groups=[], ready=fnv1.READY_UNSPECIFIED
                    ),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="2 of 5 resources ready",
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
    # The helm ProviderConfig and the Release reach the cluster through its
    # kubeconfig, so they don't carry the Civo credentials.
    Case(
        name="CustomCredentials",
        reason="A CivoCluster naming its own ProviderConfig gets it on every Civo resource, and its System-role pool gets no GPU label or taint, counts under ClusterReady, and is left out of status.nodePools.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=v1alpha1.Credentials(type="ProviderConfig", name="team-a"),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="workers",
                            role="System",
                            size="g4p.kube.small",
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
                    "firewall": _firewall(cred_kind="ProviderConfig", cred_name="team-a", ready=fnv1.READY_UNSPECIFIED),
                    "cluster": _cluster(cred_kind="ProviderConfig", cred_name="team-a", ready=fnv1.READY_TRUE),
                    "node-pool-workers": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.civo.m.upbound.io/v1beta1",
                                "kind": "NodePool",
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "team-a"},
                                    "forProvider": {
                                        "label": "workers",
                                        "size": "g4p.kube.small",
                                        "region": "LON1",
                                        "labels": {"modelplane.ai/pool": "workers"},
                                        "clusterIdSelector": {"matchControllerRef": True},
                                        "nodeCount": 2,
                                    },
                                },
                            }
                        ),
                    ),
                    "provider-config-helm": _provider_config(),
                    "release-cluster-autoscaler": _autoscaler_release(
                        autoscaling_groups=[], ready=fnv1.READY_UNSPECIFIED
                    ),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="2 of 6 resources ready",
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
        name="ClusterProvisioning",
        reason="With the firewall and cluster observed unready, ClusterReady counts the 1 of 3 resources ready, NodePoolsReady is Pending on it, and each pool is Pending because its NodePool isn't composed.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            size="an.g1.l40s.kube.x1",
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                        v1alpha1.NodePool(name="cpu-pool", role="CPU", size="g4s.kube.large", nodeCount=1),
                    ],
                ),
                resources={
                    "network": _observed_managed_resource(ready=True, failure=None),
                    "firewall": _observed_managed_resource(ready=False, failure=None),
                    "cluster": _observed_cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=False),
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
                    "network": _network(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "firewall": _firewall(
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
    Case(
        name="PoolsProvisioning",
        reason="With every cluster resource observed Ready and the cpu-pool NodePool unready, ClusterReady is True, NodePoolsReady is Provisioning, and only gpu-l40s is Ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            size="an.g1.l40s.kube.x1",
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                        v1alpha1.NodePool(name="cpu-pool", role="CPU", size="g4s.kube.large", nodeCount=1),
                    ],
                ),
                resources={
                    "network": _observed_managed_resource(ready=True, failure=None),
                    "firewall": _observed_managed_resource(ready=True, failure=None),
                    "cluster": _observed_cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=True),
                    "provider-config-helm": _observed_managed_resource(ready=True, failure=None),
                    "release-cluster-autoscaler": _observed_managed_resource(ready=True, failure=None),
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
                    "firewall": _firewall(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=["Observe", "Create", "Update", "Delete"],
                        init_node_count=1,
                        node_count=None,
                        ready=fnv1.READY_TRUE,
                    ),
                    "node-pool-cpu-pool": _node_pool_cpu(
                        management_policies=None,
                        init_node_count=None,
                        node_count=1,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-helm": _provider_config(),
                    "release-cluster-autoscaler": _autoscaler_release(autoscaling_groups=[], ready=fnv1.READY_TRUE),
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
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            size="an.g1.l40s.kube.x1",
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                        v1alpha1.NodePool(name="cpu-pool", role="CPU", size="g4s.kube.large", nodeCount=1),
                    ],
                ),
                resources={
                    "network": _observed_managed_resource(ready=True, failure=None),
                    "firewall": _observed_managed_resource(ready=True, failure=None),
                    "cluster": _observed_cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=True),
                    "provider-config-helm": _observed_managed_resource(ready=True, failure=None),
                    "release-cluster-autoscaler": _observed_managed_resource(ready=True, failure=None),
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
                    "firewall": _firewall(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=["Observe", "Create", "Update", "Delete"],
                        init_node_count=1,
                        node_count=None,
                        ready=fnv1.READY_TRUE,
                    ),
                    "node-pool-cpu-pool": _node_pool_cpu(
                        management_policies=None,
                        init_node_count=None,
                        node_count=1,
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-helm": _provider_config(),
                    "release-cluster-autoscaler": _autoscaler_release(autoscaling_groups=[], ready=fnv1.READY_TRUE),
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
        reason="With the provider reporting the gpu-l40s NodePool's LastAsyncOperation False, that pool is Failed with the provider's error prefixed by the resource name, and NodePoolsReady is Failed naming it.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            size="an.g1.l40s.kube.x1",
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                        v1alpha1.NodePool(name="cpu-pool", role="CPU", size="g4s.kube.large", nodeCount=1),
                    ],
                ),
                resources={
                    "network": _observed_managed_resource(ready=True, failure=None),
                    "firewall": _observed_managed_resource(ready=True, failure=None),
                    "cluster": _observed_cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=True),
                    "provider-config-helm": _observed_managed_resource(ready=True, failure=None),
                    "release-cluster-autoscaler": _observed_managed_resource(ready=True, failure=None),
                    "node-pool-gpu-l40s": _observed_managed_resource(
                        ready=False, failure="async create failed: insufficient an.g1.l40s.kube.x1 capacity in LON1"
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
                            "message": "node-pool-gpu-l40s: async create failed: insufficient an.g1.l40s.kube.x1 capacity in LON1",
                        },
                        {"name": "cpu-pool", "state": "Ready"},
                    ]
                ),
                resources={
                    "network": _network(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "firewall": _firewall(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=["Observe", "Create", "Update", "Delete"],
                        init_node_count=1,
                        node_count=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "node-pool-cpu-pool": _node_pool_cpu(
                        management_policies=None,
                        init_node_count=None,
                        node_count=1,
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-helm": _provider_config(),
                    "release-cluster-autoscaler": _autoscaler_release(autoscaling_groups=[], ready=fnv1.READY_TRUE),
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
        reason="With the provider reporting the firewall's and cluster's LastAsyncOperation False, ClusterReady is Failed with the first failed resource's error and a count of the rest, and NodePoolsReady is Pending on it.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            size="an.g1.l40s.kube.x1",
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                        v1alpha1.NodePool(name="cpu-pool", role="CPU", size="g4s.kube.large", nodeCount=1),
                    ],
                ),
                resources={
                    "network": _observed_managed_resource(ready=True, failure=None),
                    "firewall": _observed_managed_resource(
                        ready=False, failure="async create failed: firewall name test-cluster is already in use"
                    ),
                    "cluster": _observed_managed_resource(
                        ready=False, failure="async create failed: kubernetes version 1.20.0-k3s1 is not supported"
                    ),
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
                    "network": _network(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "firewall": _firewall(
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
                    message="cluster: async create failed: kubernetes version 1.20.0-k3s1 is not supported (and 1 more)",
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
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            size="an.g1.l40s.kube.x1",
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                        v1alpha1.NodePool(name="cpu-pool", role="CPU", size="g4s.kube.large", nodeCount=1),
                    ],
                ),
                resources={
                    "network": _observed_managed_resource(ready=True, failure=None),
                    "firewall": _observed_managed_resource(ready=True, failure=None),
                    "cluster": _observed_cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=False),
                    "provider-config-helm": _observed_managed_resource(ready=True, failure=None),
                    "release-cluster-autoscaler": _observed_managed_resource(ready=True, failure=None),
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
                    "firewall": _firewall(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "cluster": _cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=["Observe", "Create", "Update", "Delete"],
                        init_node_count=1,
                        node_count=None,
                        ready=fnv1.READY_TRUE,
                    ),
                    "node-pool-cpu-pool": _node_pool_cpu(
                        management_policies=None,
                        init_node_count=None,
                        node_count=1,
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-helm": _provider_config(),
                    "release-cluster-autoscaler": _autoscaler_release(autoscaling_groups=[], ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="4 of 5 resources ready",
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
    """RunFunction composes a Civo cluster's infrastructure."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
