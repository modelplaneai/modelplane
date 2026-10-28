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


@dataclasses.dataclass
class ReleaseNameCase:
    """A test case for _release_name."""

    name: str
    reason: str
    installed: str | None
    fixed: str
    want: str


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


def _desired_xr() -> fnv1.Resource:
    """The desired XR, publishing the cluster's kubeconfig Secret."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "status": {
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


def _network(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _firewall(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
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


def _autoscaler_release(*, release_name: str, autoscaling_groups: list[dict]) -> fnv1.Resource:
    """The composed cluster autoscaler Release, installed as release_name and scaling the given node groups."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "Release",
                "metadata": {
                    "namespace": "modelplane-system",
                    "annotations": {"crossplane.io/external-name": release_name},
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
                            "cloudProvider": "civo",
                            "autoscalingGroups": autoscaling_groups,
                            "secretKeyRefNameOverride": "civo-api-access",
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
                composite=_desired_xr(),
                resources={
                    "network": _network(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "firewall": _firewall(cred_kind="ClusterProviderConfig", cred_name="default"),
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
                composite=_desired_xr(),
                resources={
                    "network": _network(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "firewall": _firewall(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=["Observe", "Create", "Update", "Delete"],
                        init_node_count=1,
                        node_count=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-helm": _provider_config(),
                    "release-cluster-autoscaler": _autoscaler_release(
                        release_name="mp-cluster-autoscaler", autoscaling_groups=[]
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # provider-helm installs a Release under its external name, which defaults
    # to its metadata.name. Renaming a Release installed that way would install
    # the chart a second time.
    Case(
        name="GeneratedNameKept",
        reason="An autoscaler Release installed before its name was fixed keeps the release name it was installed under.",
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
                    "release-cluster-autoscaler": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "name": "test-cluster-3f9a1c2b7d4e",
                                    "namespace": "modelplane-system",
                                    "annotations": {"crossplane.io/external-name": "test-cluster-3f9a1c2b7d4e"},
                                },
                                "spec": {
                                    "forProvider": {
                                        "chart": {
                                            "name": "cluster-autoscaler",
                                            "repository": "https://kubernetes.github.io/autoscaler",
                                            "version": "9.57.0",
                                        },
                                        "namespace": "kube-system",
                                    },
                                },
                            }
                        )
                    ),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(),
                resources={
                    "network": _network(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "firewall": _firewall(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=["Observe", "Create", "Update", "Delete"],
                        init_node_count=1,
                        node_count=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-helm": _provider_config(),
                    "release-cluster-autoscaler": _autoscaler_release(
                        release_name="test-cluster-3f9a1c2b7d4e", autoscaling_groups=[]
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # The autoscaler addresses node groups by the pool ID the provider writes
    # to the NodePool's external-name annotation once the pool exists. The pool
    # leaves nodeCount at its default of 1, so this case can't tell a floor
    # taken from nodeCount from one fixed at 1.
    Case(
        name="PoolIDObserved",
        reason="With the GPU pool observed Ready under its Civo ID, a CivoCluster marks the pool ready and has the autoscaler scale that ID from one node to its maxNodeCount.",
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
                composite=_desired_xr(),
                resources={
                    "network": _network(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "firewall": _firewall(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=["Observe", "Create", "Update", "Delete"],
                        init_node_count=1,
                        node_count=None,
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-helm": _provider_config(),
                    "release-cluster-autoscaler": _autoscaler_release(
                        release_name="mp-cluster-autoscaler",
                        autoscaling_groups=[
                            {"name": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "minSize": 1, "maxSize": 4}
                        ],
                    ),
                },
            ),
            context=structpb.Struct(),
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
                composite=_desired_xr(),
                resources={
                    "network": _network(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "firewall": _firewall(cred_kind="ClusterProviderConfig", cred_name="default"),
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
                        release_name="mp-cluster-autoscaler", autoscaling_groups=[]
                    ),
                },
            ),
            context=structpb.Struct(),
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
                composite=_desired_xr(),
                resources={
                    "network": _network(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "firewall": _firewall(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        management_policies=None,
                        init_node_count=None,
                        node_count=2,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-helm": _provider_config(),
                    "release-cluster-autoscaler": _autoscaler_release(
                        release_name="mp-cluster-autoscaler", autoscaling_groups=[]
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # The helm ProviderConfig and the Release reach the cluster through its
    # kubeconfig, so they don't carry the Civo credentials.
    Case(
        name="CustomCredentials",
        reason="A CivoCluster naming its own ProviderConfig gets it on every Civo resource, and its System-role pool gets no GPU label or taint.",
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
                composite=_desired_xr(),
                resources={
                    "network": _network(cred_kind="ProviderConfig", cred_name="team-a"),
                    "firewall": _firewall(cred_kind="ProviderConfig", cred_name="team-a"),
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
                        release_name="mp-cluster-autoscaler", autoscaling_groups=[]
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction composes a Civo cluster's infrastructure."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason


# These cases call _release_name directly, because through RunFunction each
# installed name would need a whole compose case.
RELEASE_NAME_CASES = [
    ReleaseNameCase(
        name="NotInstalled",
        reason="A Release with no observed external name gets the fixed name.",
        installed=None,
        fixed="mp-cluster-autoscaler",
        want="mp-cluster-autoscaler",
    ),
    ReleaseNameCase(
        name="InstalledNameKept",
        reason="A Release installed under its generated name keeps it, so the chart isn't installed a second time.",
        installed="test-cluster-3f9a1c2b7d4e",
        fixed="mp-cluster-autoscaler",
        want="test-cluster-3f9a1c2b7d4e",
    ),
    ReleaseNameCase(
        name="NameAtLimit",
        reason="A Release whose name is exactly Helm's 53-character limit keeps it.",
        installed="cluster-orchestration-orchestrat-cluster-1a2b3c4d5e6f",
        fixed="mp-cluster-autoscaler",
        want="cluster-orchestration-orchestrat-cluster-1a2b3c4d5e6f",
    ),
    # Helm rejects the install of a release whose name is too long, so a Release
    # named that way has nothing installed under its name to keep.
    ReleaseNameCase(
        name="NameTooLong",
        reason="A Release whose generated name is longer than Helm allows gets the fixed name.",
        installed="cluster-orchestration-orchestrating-cluster-1a2b3c4d5e6f",
        fixed="mp-cluster-autoscaler",
        want="mp-cluster-autoscaler",
    ),
]


@pytest.mark.parametrize("case", RELEASE_NAME_CASES, ids=lambda case: case.name)
def test_release_name(case: ReleaseNameCase) -> None:
    """_release_name picks the Helm release name for a Release."""
    assert fn._release_name(case.installed, case.fixed) == case.want, case.reason
