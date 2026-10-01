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
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


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


def _gpu_node_pool(*, node_quantity: int, autoscaling: dict | None, ready: fnv1.Ready) -> fnv1.Resource:
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


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


COMPOSE_CASES = [
    Case(
        name="cluster composed first; node pools withheld until cluster Ready",
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
                composite=_desired_xr(),
                resources={
                    "cluster": _cluster(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    Case(
        name="node pools and GPU observer composed once cluster is Ready; autoscaling from maxNodeCount",
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
                composite=_desired_xr(),
                resources={
                    "cluster": _cluster(ready=fnv1.READY_TRUE),
                    "node-pool-gpu-l40s": _gpu_node_pool(
                        node_quantity=1,
                        autoscaling={"autoScaler": True, "minNodes": 1, "maxNodes": 4},
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-kubernetes": _provider_config(),
                    "gpu-observer": _gpu_observer(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    Case(
        name="dependents kept when the cluster Ready condition transiently regresses",
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
                composite=_desired_xr(),
                resources={
                    "cluster": _cluster(ready=fnv1.READY_UNSPECIFIED),
                    "node-pool-gpu-l40s": _gpu_node_pool(
                        node_quantity=1,
                        autoscaling={"autoScaler": True, "minNodes": 1, "maxNodes": 4},
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-kubernetes": _provider_config(),
                    "gpu-observer": _gpu_observer(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    Case(
        name="observed node pool alone keeps dependents composed",
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
                composite=_desired_xr(),
                resources={
                    "cluster": _cluster(ready=fnv1.READY_UNSPECIFIED),
                    "node-pool-gpu-l40s": _gpu_node_pool(
                        node_quantity=1,
                        autoscaling={"autoScaler": True, "minNodes": 1, "maxNodes": 4},
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-kubernetes": _provider_config(),
                    "gpu-observer": _gpu_observer(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    Case(
        name="fixed-size GPU pool",
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
                composite=_desired_xr(),
                resources={
                    "cluster": _cluster(ready=fnv1.READY_TRUE),
                    "node-pool-gpu-l40s": _gpu_node_pool(
                        node_quantity=2,
                        autoscaling=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-kubernetes": _provider_config(),
                    "gpu-observer": _gpu_observer(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    Case(
        name="minNodeCount sets the autoscaler floor; System pool carries no taint",
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
                composite=_desired_xr(),
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
                    "gpu-observer": _gpu_observer(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    Case(
        name="VultrCluster Ready only once the gpu-observer is Ready",
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
                composite=_desired_xr(),
                resources={
                    "cluster": _cluster(ready=fnv1.READY_TRUE),
                    "node-pool-gpu-l40s": _gpu_node_pool(
                        node_quantity=1,
                        autoscaling={"autoScaler": True, "minNodes": 1, "maxNodes": 4},
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-kubernetes": _provider_config(),
                    "gpu-observer": _gpu_observer(ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction composes VKE cluster infrastructure."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want)
