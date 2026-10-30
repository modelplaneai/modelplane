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

"""Tests for the compose-vultr-cluster function."""

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
from models.ai.modelplane.infrastructure.vultrcluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-vultr-cluster."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


@dataclasses.dataclass
class Derived:
    """What derive_conditions reports: each node pool's state, and the conditions it sets."""

    node_pools: list[dict]
    conditions: list[fnv1.Condition]


@dataclasses.dataclass
class DeriveConditionsCase:
    """A test case for Composer.derive_conditions."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    desired: fnv1.State
    want: Derived


def _xr(*, node_pools: list[v1alpha1.NodePool]) -> fnv1.Resource:
    """The observed VultrCluster XR, with the given node pools."""
    xr = v1alpha1.VultrCluster(
        metadata=metav1.ObjectMeta(
            name="test-cluster",
            namespace="modelplane-system",
        ),
        spec=v1alpha1.Spec(
            region="ewr",
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


def _cluster(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed VKE cluster."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "vke.vultr.m.upbound.io/v1beta1",
                "kind": "Kubernetes",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                    "forProvider": {
                        "label": "test-cluster",
                        "region": "ewr",
                        "version": "v1.36.2+1",
                        "haControlplanes": True,
                        # The system node pool the function adds inline to every cluster.
                        "nodePools": {
                            "label": "system",
                            "plan": "vc2-6c-16gb",
                            "nodeQuantity": 1,
                            "autoScaler": True,
                            "minNodes": 1,
                            "maxNodes": 2,
                            "labels": [{"key": "modelplane.ai/pool", "value": "system"}],
                        },
                    },
                    "writeConnectionSecretToRef": {"name": "test-cluster-kubeconfig-55b57"},
                },
            }
        ),
        ready=ready,
    )


def _observed_cluster(*, ready: bool) -> fnv1.Resource:
    """The VKE cluster as observed, with a Ready condition that's True if ready and False if not."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "vke.vultr.m.upbound.io/v1beta1",
                "kind": "Kubernetes",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                    "forProvider": {
                        "label": "test-cluster",
                        "region": "ewr",
                        "version": "v1.36.2+1",
                        "haControlplanes": True,
                        "nodePools": {
                            "label": "system",
                            "plan": "vc2-6c-16gb",
                            "nodeQuantity": 1,
                            "autoScaler": True,
                            "minNodes": 1,
                            "maxNodes": 2,
                            "labels": [{"key": "modelplane.ai/pool", "value": "system"}],
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


def _node_pool_gpu(*, node_quantity: int, autoscaling: dict | None, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed KubernetesNodePool for the gpu-l40s pool, with autoscaling if given."""
    for_provider: dict[str, Any] = {
        "label": "gpu-l40s",
        "plan": "vcg-l40s-16c-180g-48vram",
        "nodeQuantity": node_quantity,
        "labels": [
            {"key": "modelplane.ai/pool", "value": "gpu-l40s"},
            {"key": "modelplane.ai/gpu", "value": "nvidia-l40s"},
            {"key": "nvidia.com/gpu.deploy.device-plugin", "value": "false"},
        ],
        "clusterIdSelector": {"matchControllerRef": True},
        "taints": [{"key": "nvidia.com/gpu", "value": "true", "effect": "NoSchedule"}],
    }
    if autoscaling is not None:
        for_provider.update(autoscaling)
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "vke.vultr.m.upbound.io/v1beta1",
                "kind": "KubernetesNodePool",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                    "forProvider": for_provider,
                },
            }
        ),
        ready=ready,
    )


def _node_pool_cpu(*, node_quantity: int, autoscaling: dict | None, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed KubernetesNodePool for the cpu-pool pool, with the CPU label and taint and no GPU labels."""
    for_provider: dict[str, Any] = {
        "label": "cpu-pool",
        "plan": "vc2-6c-16gb",
        "nodeQuantity": node_quantity,
        "labels": [
            {"key": "modelplane.ai/pool", "value": "cpu-pool"},
            {"key": "modelplane.ai/cpu", "value": "true"},
        ],
        "clusterIdSelector": {"matchControllerRef": True},
        "taints": [{"key": "modelplane.ai/cpu", "value": "true", "effect": "NoSchedule"}],
    }
    if autoscaling is not None:
        for_provider.update(autoscaling)
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "vke.vultr.m.upbound.io/v1beta1",
                "kind": "KubernetesNodePool",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                    "forProvider": for_provider,
                },
            }
        ),
        ready=ready,
    )


def _provider_config() -> fnv1.Resource:
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


def _gpu_observer(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed Object observing the GPU validator DaemonSet."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "managementPolicies": ["Observe"],
                    "providerConfigRef": {
                        "kind": "ProviderConfig",
                        "name": "test-cluster-kubeconfig-55b57",
                    },
                    "readiness": {
                        "policy": "DeriveFromCelQuery",
                        "celQuery": (
                            "has(object.status.numberReady)"
                            " && object.status.desiredNumberScheduled >= 1"
                            " && object.status.numberReady == object.status.desiredNumberScheduled"
                        ),
                    },
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "apps/v1",
                            "kind": "DaemonSet",
                            "metadata": {
                                "name": "nvidia-operator-validator",
                                "namespace": "gpu-operator",
                            },
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _observed_failed(*, message: str) -> fnv1.Resource:
    """An observed managed resource whose provider reports its last operation failed with message."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "status": {
                    "conditions": [
                        {
                            "type": "LastAsyncOperation",
                            "status": "False",
                            "reason": "AsyncCreateFailure",
                            "message": message,
                            "lastTransitionTime": "2024-01-01T00:00:00Z",
                        },
                    ],
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
        reason="With nothing observed, a VultrCluster composes only the cluster, holding back its node pools, ProviderConfig and GPU observer until it's Ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            plan="vcg-l40s-16c-180g-48vram",
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
                    "cluster": _cluster(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="0 of 1 resources ready",
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
        reason="With the cluster observed Ready, a VultrCluster composes the node pool, ProviderConfig and GPU observer, autoscaling the pool from one node to maxNodeCount.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            plan="vcg-l40s-16c-180g-48vram",
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(ready=True),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "gpu-l40s", "state": "Provisioning"}]),
                resources={
                    "cluster": _cluster(ready=fnv1.READY_TRUE),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        node_quantity=1,
                        autoscaling={"autoScaler": True, "minNodes": 1, "maxNodes": 4},
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-kubernetes": _provider_config(),
                    "gpu-observer": _gpu_observer(ready=fnv1.READY_UNSPECIFIED),
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
                    message="0 of 1 node pools ready",
                ),
            ],
        ),
    ),
    Case(
        name="ClusterReadyCPUPool",
        reason=(
            "A CPU-role pool composes with the modelplane.ai/pool and modelplane.ai/cpu labels and the CPU taint, "
            "no GPU or device-plugin labels, and no GPU observer - Vultr pre-installs the GPU operator only on "
            "clusters with GPU pools, so there is no validator DaemonSet to observe."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    node_pools=[
                        v1alpha1.NodePool(
                            name="cpu-pool",
                            role="CPU",
                            plan="vc2-6c-16gb",
                            maxNodeCount=4,
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(ready=True),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "cpu-pool", "state": "Provisioning"}]),
                resources={
                    "cluster": _cluster(ready=fnv1.READY_TRUE),
                    "node-pool-cpu-pool": _node_pool_cpu(
                        node_quantity=1,
                        autoscaling={"autoScaler": True, "minNodes": 1, "maxNodes": 4},
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-kubernetes": _provider_config(),
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
                    message="0 of 1 node pools ready",
                ),
            ],
        ),
    ),
    Case(
        name="ClusterReadyRegressed",
        reason="With the cluster's Ready condition regressed to False but its ProviderConfig observed, a VultrCluster keeps the node pool, ProviderConfig and GPU observer composed.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            plan="vcg-l40s-16c-180g-48vram",
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(ready=False),
                    "provider-config-kubernetes": fnv1.Resource(
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
                    "cluster": _cluster(ready=fnv1.READY_UNSPECIFIED),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        node_quantity=1,
                        autoscaling={"autoScaler": True, "minNodes": 1, "maxNodes": 4},
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-kubernetes": _provider_config(),
                    "gpu-observer": _gpu_observer(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="1 of 2 resources ready",
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
        name="OnlyNodePoolObserved",
        reason="With the cluster not Ready and only the node pool observed, a VultrCluster keeps the node pool, ProviderConfig and GPU observer composed, and marks the Ready node pool ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            plan="vcg-l40s-16c-180g-48vram",
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(ready=False),
                    "node-pool-gpu-l40s": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "vke.vultr.m.upbound.io/v1beta1",
                                "kind": "KubernetesNodePool",
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "forProvider": {
                                        "label": "gpu-l40s",
                                        "plan": "vcg-l40s-16c-180g-48vram",
                                        "nodeQuantity": 1,
                                        "labels": [
                                            {"key": "modelplane.ai/pool", "value": "gpu-l40s"},
                                            {"key": "modelplane.ai/gpu", "value": "nvidia-l40s"},
                                            {"key": "nvidia.com/gpu.deploy.device-plugin", "value": "false"},
                                        ],
                                        "clusterIdSelector": {"matchControllerRef": True},
                                        "taints": [{"key": "nvidia.com/gpu", "value": "true", "effect": "NoSchedule"}],
                                        "autoScaler": True,
                                        "minNodes": 1,
                                        "maxNodes": 4,
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
                    "cluster": _cluster(ready=fnv1.READY_UNSPECIFIED),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        node_quantity=1,
                        autoscaling={"autoScaler": True, "minNodes": 1, "maxNodes": 4},
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-kubernetes": _provider_config(),
                    "gpu-observer": _gpu_observer(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="1 of 2 resources ready",
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
        reason="A VultrCluster with a GPU pool that sets nodeCount but no maxNodeCount composes a node pool of that size with no autoscaler.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            plan="vcg-l40s-16c-180g-48vram",
                            nodeCount=2,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(ready=True),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "gpu-l40s", "state": "Provisioning"}]),
                resources={
                    "cluster": _cluster(ready=fnv1.READY_TRUE),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        node_quantity=2,
                        autoscaling=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-kubernetes": _provider_config(),
                    "gpu-observer": _gpu_observer(ready=fnv1.READY_UNSPECIFIED),
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
                    message="0 of 1 node pools ready",
                ),
            ],
        ),
    ),
    # The pool's nodeCount and minNodeCount are both 2, so this case can't tell
    # which of them sets the autoscaler's floor.
    Case(
        name="SystemRolePool",
        reason=(
            "A VultrCluster's System-role pool autoscales from a floor of 2 nodes to maxNodeCount, and gets "
            "neither GPU labels nor a taint - and with no GPU pool there is no GPU observer to compose."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    node_pools=[
                        v1alpha1.NodePool(
                            name="workers",
                            role="System",
                            plan="vc2-6c-16gb",
                            nodeCount=2,
                            minNodeCount=2,
                            maxNodeCount=5,
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(ready=True),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "workers", "state": "Provisioning"}]),
                resources={
                    "cluster": _cluster(ready=fnv1.READY_TRUE),
                    "node-pool-workers": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "vke.vultr.m.upbound.io/v1beta1",
                                "kind": "KubernetesNodePool",
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "forProvider": {
                                        "label": "workers",
                                        "plan": "vc2-6c-16gb",
                                        "nodeQuantity": 2,
                                        "labels": [{"key": "modelplane.ai/pool", "value": "workers"}],
                                        "clusterIdSelector": {"matchControllerRef": True},
                                        "autoScaler": True,
                                        "minNodes": 2,
                                        "maxNodes": 5,
                                    },
                                },
                            }
                        ),
                    ),
                    "provider-config-kubernetes": _provider_config(),
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
                    message="0 of 1 node pools ready",
                ),
            ],
        ),
    ),
    Case(
        name="GPUObserverReady",
        reason="With the GPU observer observed Ready alongside the cluster and node pool, a VultrCluster marks every composed resource ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            plan="vcg-l40s-16c-180g-48vram",
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(ready=True),
                    "node-pool-gpu-l40s": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "vke.vultr.m.upbound.io/v1beta1",
                                "kind": "KubernetesNodePool",
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "forProvider": {
                                        "label": "gpu-l40s",
                                        "plan": "vcg-l40s-16c-180g-48vram",
                                        "nodeQuantity": 1,
                                        "labels": [
                                            {"key": "modelplane.ai/pool", "value": "gpu-l40s"},
                                            {"key": "modelplane.ai/gpu", "value": "nvidia-l40s"},
                                            {"key": "nvidia.com/gpu.deploy.device-plugin", "value": "false"},
                                        ],
                                        "clusterIdSelector": {"matchControllerRef": True},
                                        "taints": [{"key": "nvidia.com/gpu", "value": "true", "effect": "NoSchedule"}],
                                        "autoScaler": True,
                                        "minNodes": 1,
                                        "maxNodes": 4,
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
                    "gpu-observer": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "managementPolicies": ["Observe"],
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-cluster-kubeconfig-55b57",
                                    },
                                    "readiness": {
                                        "policy": "DeriveFromCelQuery",
                                        "celQuery": (
                                            "has(object.status.numberReady)"
                                            " && object.status.desiredNumberScheduled >= 1"
                                            " && object.status.numberReady == object.status.desiredNumberScheduled"
                                        ),
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "apps/v1",
                                            "kind": "DaemonSet",
                                            "metadata": {
                                                "name": "nvidia-operator-validator",
                                                "namespace": "gpu-operator",
                                            },
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
                composite=_desired_xr(node_pools=[{"name": "gpu-l40s", "state": "Ready"}]),
                resources={
                    "cluster": _cluster(ready=fnv1.READY_TRUE),
                    "node-pool-gpu-l40s": _node_pool_gpu(
                        node_quantity=1,
                        autoscaling={"autoScaler": True, "minNodes": 1, "maxNodes": 4},
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-kubernetes": _provider_config(),
                    "gpu-observer": _gpu_observer(ready=fnv1.READY_TRUE),
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
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction composes VKE cluster infrastructure."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason


# Each case's XR has two pools: gpu-l40s, built by its KubernetesNodePool and the
# gpu-observer, and cpu-pool, built by its KubernetesNodePool. desired is what
# the function composed, with the readiness mark_readiness set.
DERIVE_CONDITIONS_CASES = [
    DeriveConditionsCase(
        name="ClusterProvisioning",
        reason="With the cluster unready, ClusterReady counts how many cluster resources are ready and NodePoolsReady waits on it while each pool provisions.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            plan="vcg-l40s-16c-180g-48vram",
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                        v1alpha1.NodePool(name="cpu-pool", role="CPU", plan="vc2-6c-16gb"),
                    ],
                ),
            ),
        ),
        desired=fnv1.State(
            resources={
                "cluster": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
                "provider-config-kubernetes": fnv1.Resource(ready=fnv1.READY_TRUE),
                "node-pool-gpu-l40s": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
                "node-pool-cpu-pool": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
                "gpu-observer": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
            },
        ),
        want=Derived(
            node_pools=[{"name": "gpu-l40s", "state": "Provisioning"}, {"name": "cpu-pool", "state": "Provisioning"}],
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="1 of 2 resources ready",
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
    DeriveConditionsCase(
        name="PoolsProvisioning",
        reason="With the cluster and both node pools ready but the gpu-observer unready, the GPU pool is Provisioning and only cpu-pool is Ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            plan="vcg-l40s-16c-180g-48vram",
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                        v1alpha1.NodePool(name="cpu-pool", role="CPU", plan="vc2-6c-16gb"),
                    ],
                ),
            ),
        ),
        desired=fnv1.State(
            resources={
                "cluster": fnv1.Resource(ready=fnv1.READY_TRUE),
                "provider-config-kubernetes": fnv1.Resource(ready=fnv1.READY_TRUE),
                "node-pool-gpu-l40s": fnv1.Resource(ready=fnv1.READY_TRUE),
                "node-pool-cpu-pool": fnv1.Resource(ready=fnv1.READY_TRUE),
                "gpu-observer": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
            },
        ),
        want=Derived(
            node_pools=[{"name": "gpu-l40s", "state": "Provisioning"}, {"name": "cpu-pool", "state": "Ready"}],
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
    DeriveConditionsCase(
        name="PoolPending",
        reason="With only the cluster composed, neither pool's resources are, so both pools are Pending.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            plan="vcg-l40s-16c-180g-48vram",
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                        v1alpha1.NodePool(name="cpu-pool", role="CPU", plan="vc2-6c-16gb"),
                    ],
                ),
            ),
        ),
        desired=fnv1.State(
            resources={
                "cluster": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
            },
        ),
        want=Derived(
            node_pools=[{"name": "gpu-l40s", "state": "Pending"}, {"name": "cpu-pool", "state": "Pending"}],
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="0 of 1 resources ready",
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
    DeriveConditionsCase(
        name="AllReady",
        reason="With every resource ready, ClusterReady and NodePoolsReady are True and every pool is Ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            plan="vcg-l40s-16c-180g-48vram",
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                        v1alpha1.NodePool(name="cpu-pool", role="CPU", plan="vc2-6c-16gb"),
                    ],
                ),
            ),
        ),
        desired=fnv1.State(
            resources={
                "cluster": fnv1.Resource(ready=fnv1.READY_TRUE),
                "provider-config-kubernetes": fnv1.Resource(ready=fnv1.READY_TRUE),
                "node-pool-gpu-l40s": fnv1.Resource(ready=fnv1.READY_TRUE),
                "node-pool-cpu-pool": fnv1.Resource(ready=fnv1.READY_TRUE),
                "gpu-observer": fnv1.Resource(ready=fnv1.READY_TRUE),
            },
        ),
        want=Derived(
            node_pools=[{"name": "gpu-l40s", "state": "Ready"}, {"name": "cpu-pool", "state": "Ready"}],
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
    DeriveConditionsCase(
        name="PoolFailed",
        reason="With the provider reporting a pool's KubernetesNodePool create failed, the pool is Failed with the provider's error, even while the cluster provisions.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            plan="vcg-l40s-16c-180g-48vram",
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                        v1alpha1.NodePool(name="cpu-pool", role="CPU", plan="vc2-6c-16gb"),
                    ],
                ),
                resources={
                    "node-pool-cpu-pool": _observed_failed(
                        message="async create failed: Plan is not available in the selected region"
                    ),
                },
            ),
        ),
        desired=fnv1.State(
            resources={
                "cluster": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
                "provider-config-kubernetes": fnv1.Resource(ready=fnv1.READY_TRUE),
                "node-pool-gpu-l40s": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
                "node-pool-cpu-pool": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
                "gpu-observer": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
            },
        ),
        want=Derived(
            node_pools=[
                {"name": "gpu-l40s", "state": "Provisioning"},
                {
                    "name": "cpu-pool",
                    "state": "Failed",
                    "message": "node-pool-cpu-pool: async create failed: Plan is not available in the selected region",
                },
            ],
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="1 of 2 resources ready",
                ),
                fnv1.Condition(
                    type="NodePoolsReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Failed",
                    message="1 of 2 node pools failed: cpu-pool",
                ),
            ],
        ),
    ),
    DeriveConditionsCase(
        name="ClusterFailed",
        reason="With the provider reporting the cluster's create failed, ClusterReady is Failed with its error and NodePoolsReady waits on it.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            plan="vcg-l40s-16c-180g-48vram",
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                        v1alpha1.NodePool(name="cpu-pool", role="CPU", plan="vc2-6c-16gb"),
                    ],
                ),
                resources={
                    "cluster": _observed_failed(message="async create failed: Region is not available"),
                },
            ),
        ),
        desired=fnv1.State(
            resources={
                "cluster": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
            },
        ),
        want=Derived(
            node_pools=[{"name": "gpu-l40s", "state": "Pending"}, {"name": "cpu-pool", "state": "Pending"}],
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Failed",
                    message="cluster: async create failed: Region is not available",
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
]


@pytest.mark.parametrize("case", DERIVE_CONDITIONS_CASES, ids=lambda case: case.name)
def test_derive_conditions(case: DeriveConditionsCase) -> None:
    """derive_conditions reports which provisioning stage the cluster is in, and each node pool's state."""
    rsp = fnv1.RunFunctionResponse(desired=case.desired)
    node_pools = fn.Composer(case.req, rsp).derive_conditions()
    got = {
        "nodePools": [p.model_dump(exclude_none=True) for p in node_pools],
        "conditions": [_to_dict(c) for c in rsp.conditions],
    }
    want = {"nodePools": case.want.node_pools, "conditions": [_to_dict(c) for c in case.want.conditions]}
    assert got == want, case.reason
