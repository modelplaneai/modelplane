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

"""Tests for the compose-aks-cluster function."""

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
from models.ai.modelplane.infrastructure.akscluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-aks-cluster."""

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


def _xr(*, credentials: v1alpha1.Credentials | None, node_pools: list[v1alpha1.NodePool]) -> fnv1.Resource:
    """The observed AKSCluster XR, with the given credentials and node pools."""
    xr = v1alpha1.AKSCluster(
        metadata=metav1.ObjectMeta(name="test-cluster", namespace="modelplane-system"),
        spec=v1alpha1.Spec(
            location="westeurope",
            credentials=credentials,
            nodePools=node_pools,
        ),
    )
    return fnv1.Resource(resource=resource.dict_to_struct(xr.model_dump(exclude_none=True, mode="json", by_alias=True)))


def _desired_xr(*, node_pools: list[dict]) -> fnv1.Resource:
    """The desired XR, publishing node_pools' states, its kubeconfig Secret and cache StorageClass."""
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
                    "cache": {"storageClassName": "modelplane-rwx-fs"},
                },
            }
        ),
    )


def _resource_group(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed ResourceGroup."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "azure.m.upbound.io/v1beta1",
                "kind": "ResourceGroup",
                "metadata": {"name": "modelplane-system-test-cluster-aks-1173e"},
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {"location": "westeurope"},
                },
            }
        ),
        ready=ready,
    )


def _virtual_network(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed VirtualNetwork."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "network.azure.m.upbound.io/v1beta1",
                "kind": "VirtualNetwork",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "location": "westeurope",
                        "addressSpace": ["10.0.0.0/16"],
                        "resourceGroupNameSelector": {"matchControllerRef": True},
                    },
                },
            }
        ),
        ready=ready,
    )


def _subnet(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed Subnet."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "network.azure.m.upbound.io/v1beta1",
                "kind": "Subnet",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "addressPrefixes": ["10.0.0.0/20"],
                        "resourceGroupNameSelector": {"matchControllerRef": True},
                        "virtualNetworkNameSelector": {"matchControllerRef": True},
                    },
                },
            }
        ),
        ready=ready,
    )


def _cluster(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed KubernetesCluster."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "containerservice.azure.m.upbound.io/v1beta1",
                "kind": "KubernetesCluster",
                "metadata": {"name": "modelplane-system-test-cluster-aks-1173e"},
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "location": "westeurope",
                        "kubernetesVersion": "1.34",
                        "dnsPrefix": "modelplane-system-test-cluster-aks-1173e",
                        "nodeResourceGroup": "modelplane-system-test-cluster-aks-1173e-nodes",
                        "resourceGroupNameSelector": {"matchControllerRef": True},
                        "identity": {"type": "SystemAssigned"},
                        "defaultNodePool": {
                            "name": "system",
                            "vmSize": "Standard_D4s_v5",
                            "autoScalingEnabled": True,
                            "minCount": 1,
                            "maxCount": 2,
                            "osDiskSizeGb": 100,
                            "temporaryNameForRotation": "systemtmp",
                            "nodeLabels": {"modelplane.ai/pool": "system"},
                            "upgradeSettings": {"maxSurge": "10%"},
                            "vnetSubnetIdSelector": {"matchControllerRef": True},
                        },
                        "networkProfile": {
                            "networkPlugin": "azure",
                            "networkPluginMode": "overlay",
                            "podCidr": "10.244.0.0/16",
                            "serviceCidr": "10.96.0.0/16",
                            "dnsServiceIp": "10.96.0.10",
                        },
                    },
                    "writeConnectionSecretToRef": {"name": "test-cluster-kubeconfig-55b57"},
                },
            }
        ),
        ready=ready,
    )


def _nodepool_gpu(*, cred_kind: str, cred_name: str, zones: list[str] | None, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed gpuh100 node pool, in zones if given."""
    nodepool = {
        "apiVersion": "containerservice.azure.m.upbound.io/v1beta1",
        "kind": "KubernetesClusterNodePool",
        "metadata": {"annotations": {"crossplane.io/external-name": "gpuh100"}},
        "spec": {
            "providerConfigRef": {"kind": cred_kind, "name": cred_name},
            "managementPolicies": ["Observe", "Create", "Update", "Delete"],
            "initProvider": {"nodeCount": 1},
            "forProvider": {
                "kubernetesClusterIdSelector": {"matchControllerRef": True},
                "vnetSubnetIdSelector": {"matchControllerRef": True},
                "mode": "User",
                "vmSize": "Standard_ND96isr_H100_v5",
                "osDiskSizeGb": 200,
                "orchestratorVersion": "1.34",
                "autoScalingEnabled": True,
                "minCount": 1,
                "maxCount": 4,
                "gpuDriver": "Install",
                "nodeLabels": {
                    "modelplane.ai/gpu": "nvidia-h100",
                    "modelplane.ai/pool": "gpuh100",
                },
                "nodeTaints": ["nvidia.com/gpu=true:NoSchedule"],
                "upgradeSettings": {"maxSurge": "10%"},
            },
        },
    }
    if zones is not None:
        nodepool["spec"]["forProvider"]["zones"] = zones
    return fnv1.Resource(resource=resource.dict_to_struct(nodepool), ready=ready)


def _nodepool_cpu(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
    """The composed cpupool node pool, with the CPU label and taint and no GPU driver."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "containerservice.azure.m.upbound.io/v1beta1",
                "kind": "KubernetesClusterNodePool",
                "metadata": {"annotations": {"crossplane.io/external-name": "cpupool"}},
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "managementPolicies": ["Observe", "Create", "Update", "Delete"],
                    "initProvider": {"nodeCount": 1},
                    "forProvider": {
                        "kubernetesClusterIdSelector": {"matchControllerRef": True},
                        "vnetSubnetIdSelector": {"matchControllerRef": True},
                        "mode": "User",
                        "vmSize": "Standard_D8s_v5",
                        "osDiskSizeGb": 100,
                        "orchestratorVersion": "1.34",
                        "autoScalingEnabled": True,
                        "minCount": 0,
                        "maxCount": 4,
                        "nodeLabels": {
                            "modelplane.ai/pool": "cpupool",
                            "modelplane.ai/cpu": "true",
                        },
                        "nodeTaints": ["modelplane.ai/cpu=true:NoSchedule"],
                        "upgradeSettings": {"maxSurge": "10%"},
                    },
                },
            }
        ),
    )


def _provider_config(*, api_version: str) -> fnv1.Resource:
    """A Ready ProviderConfig that reaches the cluster through its kubeconfig Secret."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": api_version,
                "kind": "ProviderConfig",
                "metadata": {"name": "test-cluster-kubeconfig-55b57"},
                "spec": {
                    "credentials": {
                        "source": "Secret",
                        "secretRef": {
                            "name": "test-cluster-kubeconfig-55b57",
                            "namespace": "modelplane-system",
                            "key": "kubeconfig",
                        },
                    },
                },
            }
        ),
        ready=fnv1.READY_TRUE,
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
    # The StorageClass isn't composed yet: the cluster isn't observed, so the
    # ProviderConfigs can't reach it.
    Case(
        name="FirstPass",
        reason="With nothing observed, an AKSCluster composes its Azure infrastructure and ProviderConfigs, but not the StorageClass.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpuh100",
                            role="GPU",
                            vmSize="Standard_ND96isr_H100_v5",
                            diskSizeGb=200,
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                        ),
                    ],
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "gpuh100", "state": "Provisioning"}]),
                resources={
                    "resource-group": _resource_group(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "virtual-network": _virtual_network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "subnet": _subnet(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodepool-gpuh100": _nodepool_gpu(
                        cred_kind="ClusterProviderConfig", cred_name="default", zones=None, ready=fnv1.READY_UNSPECIFIED
                    ),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
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
        name="FirstPassCPUPool",
        reason="A CPU-role node pool composes with the modelplane.ai/pool and modelplane.ai/cpu labels and the CPU taint, and no GPU driver.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="cpupool",
                            role="CPU",
                            vmSize="Standard_D8s_v5",
                            nodeCount=1,
                            minNodeCount=0,
                            maxNodeCount=4,
                        ),
                    ],
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "cpupool", "state": "Provisioning"}]),
                resources={
                    "resource-group": _resource_group(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "virtual-network": _virtual_network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "subnet": _subnet(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodepool-cpupool": _nodepool_cpu(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
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
        name="NodePoolZones",
        reason="An AKSCluster passes a pool's zones through to its KubernetesClusterNodePool.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpuh100",
                            role="GPU",
                            vmSize="Standard_ND96isr_H100_v5",
                            diskSizeGb=200,
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                            zones=[v1alpha1.Zone("1")],
                        ),
                    ],
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "gpuh100", "state": "Provisioning"}]),
                resources={
                    "resource-group": _resource_group(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "virtual-network": _virtual_network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "subnet": _subnet(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodepool-gpuh100": _nodepool_gpu(
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        zones=["1"],
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
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
        name="InfiniBandFirstPass",
        reason="With nothing observed, an AKSCluster with an InfiniBand pool doesn't compose the network operator release yet.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpuh100",
                            role="GPU",
                            vmSize="Standard_ND96isr_H100_v5",
                            diskSizeGb=200,
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                            fabric="InfiniBand",
                        ),
                    ],
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "gpuh100", "state": "Provisioning"}]),
                resources={
                    "resource-group": _resource_group(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "virtual-network": _virtual_network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "subnet": _subnet(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodepool-gpuh100": _nodepool_gpu(
                        cred_kind="ClusterProviderConfig", cred_name="default", zones=None, ready=fnv1.READY_UNSPECIFIED
                    ),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
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
        name="InfiniBandClusterObserved",
        reason="With the cluster observed, an AKSCluster with an InfiniBand pool composes the network operator release alongside the StorageClass.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpuh100",
                            role="GPU",
                            vmSize="Standard_ND96isr_H100_v5",
                            diskSizeGb=200,
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                            fabric="InfiniBand",
                        ),
                    ],
                ),
                resources={
                    "cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "containerservice.azure.m.upbound.io/v1beta1",
                                "kind": "KubernetesCluster",
                                "metadata": {"name": "modelplane-system-test-cluster-aks-1173e"},
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "forProvider": {
                                        "location": "westeurope",
                                        "kubernetesVersion": "1.34",
                                        "dnsPrefix": "modelplane-system-test-cluster-aks-1173e",
                                        "nodeResourceGroup": "modelplane-system-test-cluster-aks-1173e-nodes",
                                        "resourceGroupNameSelector": {"matchControllerRef": True},
                                        "identity": {"type": "SystemAssigned"},
                                        "defaultNodePool": {
                                            "name": "system",
                                            "vmSize": "Standard_D4s_v5",
                                            "autoScalingEnabled": True,
                                            "minCount": 1,
                                            "maxCount": 2,
                                            "osDiskSizeGb": 100,
                                            "temporaryNameForRotation": "systemtmp",
                                            "nodeLabels": {"modelplane.ai/pool": "system"},
                                            "upgradeSettings": {"maxSurge": "10%"},
                                            "vnetSubnetIdSelector": {"matchControllerRef": True},
                                        },
                                        "networkProfile": {
                                            "networkPlugin": "azure",
                                            "networkPluginMode": "overlay",
                                            "podCidr": "10.244.0.0/16",
                                            "serviceCidr": "10.96.0.0/16",
                                            "dnsServiceIp": "10.96.0.10",
                                        },
                                    },
                                    "writeConnectionSecretToRef": {"name": "test-cluster-kubeconfig-55b57"},
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
                composite=_desired_xr(node_pools=[{"name": "gpuh100", "state": "Provisioning"}]),
                resources={
                    "resource-group": _resource_group(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "virtual-network": _virtual_network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "subnet": _subnet(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "nodepool-gpuh100": _nodepool_gpu(
                        cred_kind="ClusterProviderConfig", cred_name="default", zones=None, ready=fnv1.READY_UNSPECIFIED
                    ),
                    "release-network-operator": fnv1.Resource(
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
                                            "name": "network-operator",
                                            "repository": "https://helm.ngc.nvidia.com/nvidia",
                                            "version": "26.4.0",
                                        },
                                        "namespace": "network-operator",
                                        "values": {
                                            "deployCR": True,
                                            "ofedDriver": {"deploy": True},
                                            "rdmaSharedDevicePlugin": {"deploy": True},
                                            # The driver and device plugin must
                                            # tolerate the GPU taint to run on
                                            # the InfiniBand nodes.
                                            "daemonsets": {
                                                "tolerations": [
                                                    {
                                                        "key": "nvidia.com/gpu",
                                                        "operator": "Exists",
                                                        "effect": "NoSchedule",
                                                    },
                                                ],
                                            },
                                        },
                                    },
                                },
                            }
                        ),
                    ),
                    "storage-class-rwx-fs": fnv1.Resource(
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
                                    "readiness": {"policy": "SuccessfulCreate"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "storage.k8s.io/v1",
                                            "kind": "StorageClass",
                                            "metadata": {"name": "modelplane-rwx-fs"},
                                            "provisioner": "file.csi.azure.com",
                                            "parameters": {"skuName": "Premium_LRS"},
                                            "mountOptions": [
                                                "dir_mode=0777",
                                                "file_mode=0777",
                                                "uid=0",
                                                "gid=0",
                                                "mfsymlinks",
                                                "cache=strict",
                                                "actimeo=30",
                                                "nosharesock",
                                            ],
                                            "reclaimPolicy": "Delete",
                                            "allowVolumeExpansion": True,
                                            "volumeBindingMode": "WaitForFirstConsumer",
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="4 of 8 resources ready",
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
    # The ProviderConfigs reach the cluster through its kubeconfig, not the
    # cloud credentials.
    Case(
        name="CustomCredentials",
        reason="The ProviderConfig an AKSCluster's spec.credentials names becomes every Azure managed resource's providerConfigRef.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=v1alpha1.Credentials(type="ProviderConfig", name="my-azure-account"),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpuh100",
                            role="GPU",
                            vmSize="Standard_ND96isr_H100_v5",
                            diskSizeGb=200,
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                        ),
                    ],
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "gpuh100", "state": "Provisioning"}]),
                resources={
                    "resource-group": _resource_group(
                        cred_kind="ProviderConfig", cred_name="my-azure-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "virtual-network": _virtual_network(
                        cred_kind="ProviderConfig", cred_name="my-azure-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "subnet": _subnet(
                        cred_kind="ProviderConfig", cred_name="my-azure-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(
                        cred_kind="ProviderConfig", cred_name="my-azure-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodepool-gpuh100": _nodepool_gpu(
                        cred_kind="ProviderConfig",
                        cred_name="my-azure-account",
                        zones=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
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
        name="ResourcesReady",
        reason="With the cluster and its other Azure resources observed Ready, an AKSCluster marks them ready and composes the StorageClass.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpuh100",
                            role="GPU",
                            vmSize="Standard_ND96isr_H100_v5",
                            diskSizeGb=200,
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                        ),
                    ],
                ),
                resources={
                    "resource-group": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "azure.m.upbound.io/v1beta1",
                                "kind": "ResourceGroup",
                                "metadata": {"name": "modelplane-system-test-cluster-aks-1173e"},
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "forProvider": {"location": "westeurope"},
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
                    "virtual-network": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "network.azure.m.upbound.io/v1beta1",
                                "kind": "VirtualNetwork",
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "forProvider": {
                                        "location": "westeurope",
                                        "addressSpace": ["10.0.0.0/16"],
                                        "resourceGroupNameSelector": {"matchControllerRef": True},
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
                    "subnet": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "network.azure.m.upbound.io/v1beta1",
                                "kind": "Subnet",
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "forProvider": {
                                        "addressPrefixes": ["10.0.0.0/20"],
                                        "resourceGroupNameSelector": {"matchControllerRef": True},
                                        "virtualNetworkNameSelector": {"matchControllerRef": True},
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
                    "cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "containerservice.azure.m.upbound.io/v1beta1",
                                "kind": "KubernetesCluster",
                                "metadata": {"name": "modelplane-system-test-cluster-aks-1173e"},
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "forProvider": {
                                        "location": "westeurope",
                                        "kubernetesVersion": "1.34",
                                        "dnsPrefix": "modelplane-system-test-cluster-aks-1173e",
                                        "nodeResourceGroup": "modelplane-system-test-cluster-aks-1173e-nodes",
                                        "resourceGroupNameSelector": {"matchControllerRef": True},
                                        "identity": {"type": "SystemAssigned"},
                                        "defaultNodePool": {
                                            "name": "system",
                                            "vmSize": "Standard_D4s_v5",
                                            "autoScalingEnabled": True,
                                            "minCount": 1,
                                            "maxCount": 2,
                                            "osDiskSizeGb": 100,
                                            "temporaryNameForRotation": "systemtmp",
                                            "nodeLabels": {"modelplane.ai/pool": "system"},
                                            "upgradeSettings": {"maxSurge": "10%"},
                                            "vnetSubnetIdSelector": {"matchControllerRef": True},
                                        },
                                        "networkProfile": {
                                            "networkPlugin": "azure",
                                            "networkPluginMode": "overlay",
                                            "podCidr": "10.244.0.0/16",
                                            "serviceCidr": "10.96.0.0/16",
                                            "dnsServiceIp": "10.96.0.10",
                                        },
                                    },
                                    "writeConnectionSecretToRef": {"name": "test-cluster-kubeconfig-55b57"},
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
                    "nodepool-gpuh100": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "containerservice.azure.m.upbound.io/v1beta1",
                                "kind": "KubernetesClusterNodePool",
                                "metadata": {"annotations": {"crossplane.io/external-name": "gpuh100"}},
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "managementPolicies": ["Observe", "Create", "Update", "Delete"],
                                    "initProvider": {"nodeCount": 1},
                                    "forProvider": {
                                        "kubernetesClusterIdSelector": {"matchControllerRef": True},
                                        "vnetSubnetIdSelector": {"matchControllerRef": True},
                                        "mode": "User",
                                        "vmSize": "Standard_ND96isr_H100_v5",
                                        "osDiskSizeGb": 200,
                                        "orchestratorVersion": "1.34",
                                        "autoScalingEnabled": True,
                                        "minCount": 1,
                                        "maxCount": 4,
                                        "gpuDriver": "Install",
                                        "nodeLabels": {
                                            "modelplane.ai/gpu": "nvidia-h100",
                                            "modelplane.ai/pool": "gpuh100",
                                        },
                                        "nodeTaints": ["nvidia.com/gpu=true:NoSchedule"],
                                        "upgradeSettings": {"maxSurge": "10%"},
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
                composite=_desired_xr(node_pools=[{"name": "gpuh100", "state": "Ready"}]),
                resources={
                    "resource-group": _resource_group(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "virtual-network": _virtual_network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "subnet": _subnet(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "storage-class-rwx-fs": fnv1.Resource(
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
                                    "readiness": {"policy": "SuccessfulCreate"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "storage.k8s.io/v1",
                                            "kind": "StorageClass",
                                            "metadata": {"name": "modelplane-rwx-fs"},
                                            "provisioner": "file.csi.azure.com",
                                            "parameters": {"skuName": "Premium_LRS"},
                                            "mountOptions": [
                                                "dir_mode=0777",
                                                "file_mode=0777",
                                                "uid=0",
                                                "gid=0",
                                                "mfsymlinks",
                                                "cache=strict",
                                                "actimeo=30",
                                                "nosharesock",
                                            ],
                                            "reclaimPolicy": "Delete",
                                            "allowVolumeExpansion": True,
                                            "volumeBindingMode": "WaitForFirstConsumer",
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "nodepool-gpuh100": _nodepool_gpu(
                        cred_kind="ClusterProviderConfig", cred_name="default", zones=None, ready=fnv1.READY_TRUE
                    ),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
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
    """RunFunction composes AKS cluster infrastructure."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason


def _named_xr(*, name: str) -> v1alpha1.AKSCluster:
    """The observed AKSCluster XR, with the given name."""
    return v1alpha1.AKSCluster(
        metadata=metav1.ObjectMeta(name=name, namespace="modelplane-system"),
        spec=v1alpha1.Spec(
            location="westeurope",
            nodePools=[
                v1alpha1.NodePool(
                    name="gpuh100",
                    role="GPU",
                    vmSize="Standard_ND96isr_H100_v5",
                    nodeCount=1,
                    minNodeCount=0,
                    maxNodeCount=1,
                ),
            ],
        ),
    )


@pytest.mark.parametrize(
    ("name", "want"),
    [
        # The derived name is exactly 54 characters: used as is.
        ("mpiiiiiiiiiiiiiiiiiiiiiiii", "modelplane-system-mpiiiiiiiiiiiiiiiiiiiiiiii-aks-383fa"),
        # The derived name is 55 characters: it loses a character before its
        # hash suffix to fit.
        ("mpiiiiiiiiiiiiiiiiiiiiiiiii", "modelplane-system-mpiiiiiiiiiiiiiiiiiiiiiiiii-ak-0d844"),
        # The cut lands on a hyphen: the trailing hyphen is stripped, so the
        # prefix still ends alphanumeric.
        (
            "mpi-" + "i" * 25,
            "modelplane-system-mpi-iiiiiiiiiiiiiiiiiiiiiiiii-90184",
        ),
    ],
)
def test_dns_prefix(name: str, want: str) -> None:
    """_dns_prefix uses the cluster name as the DNS prefix when it fits Azure's 54 character limit, and cuts it to fit when it doesn't."""
    got = fn._dns_prefix(_named_xr(name=name))
    assert got == want


def test_dns_prefix_wired() -> None:
    """A cluster whose name exceeds Azure's 54 character DNS prefix limit gets the cut prefix, not its full name."""
    xr = _named_xr(name="mpiiiiiiiiiiiiiiiiiiiiiiiii")
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(
                resource=resource.dict_to_struct(xr.model_dump(exclude_none=True, mode="json", by_alias=True))
            )
        ),
    )
    got = asyncio.run(fn.FunctionRunner().RunFunction(req, None))
    cluster = resource.struct_to_dict(got.desired.resources["cluster"].resource)
    assert cluster["spec"]["forProvider"]["dnsPrefix"] == "modelplane-system-mpiiiiiiiiiiiiiiiiiiiiiiiii-ak-0d844"


# Each case's XR has two pools, each built by its node pool: gpua10, and
# gpuh100, an InfiniBand pool whose network operator release counts toward the
# cluster. desired is what the function composed, with the readiness
# mark_readiness set.
DERIVE_CONDITIONS_CASES = [
    DeriveConditionsCase(
        name="ClusterProvisioning",
        reason="With cluster resources unready, ClusterReady counts how many are ready and NodePoolsReady waits on it while each pool provisions.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(name="gpua10", role="GPU", vmSize="Standard_NV36ads_A10_v5", nodeCount=1),
                        v1alpha1.NodePool(
                            name="gpuh100",
                            role="GPU",
                            vmSize="Standard_ND96isr_H100_v5",
                            nodeCount=1,
                            fabric="InfiniBand",
                        ),
                    ],
                ),
            ),
        ),
        desired=fnv1.State(
            resources={
                "resource-group": fnv1.Resource(ready=fnv1.READY_TRUE),
                "virtual-network": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
                "subnet": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
                "cluster": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
                "release-network-operator": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
                "provider-config-kubernetes": fnv1.Resource(ready=fnv1.READY_TRUE),
                "provider-config-helm": fnv1.Resource(ready=fnv1.READY_TRUE),
                "nodepool-gpua10": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
                "nodepool-gpuh100": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
            },
        ),
        want=Derived(
            node_pools=[{"name": "gpua10", "state": "Provisioning"}, {"name": "gpuh100", "state": "Provisioning"}],
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
    DeriveConditionsCase(
        name="PoolsProvisioning",
        reason="With every cluster resource ready and the gpuh100 pool's node pool unready, only gpua10 is Ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(name="gpua10", role="GPU", vmSize="Standard_NV36ads_A10_v5", nodeCount=1),
                        v1alpha1.NodePool(
                            name="gpuh100",
                            role="GPU",
                            vmSize="Standard_ND96isr_H100_v5",
                            nodeCount=1,
                            fabric="InfiniBand",
                        ),
                    ],
                ),
            ),
        ),
        desired=fnv1.State(
            resources={
                "resource-group": fnv1.Resource(ready=fnv1.READY_TRUE),
                "virtual-network": fnv1.Resource(ready=fnv1.READY_TRUE),
                "subnet": fnv1.Resource(ready=fnv1.READY_TRUE),
                "cluster": fnv1.Resource(ready=fnv1.READY_TRUE),
                "release-network-operator": fnv1.Resource(ready=fnv1.READY_TRUE),
                "provider-config-kubernetes": fnv1.Resource(ready=fnv1.READY_TRUE),
                "provider-config-helm": fnv1.Resource(ready=fnv1.READY_TRUE),
                "storage-class-rwx-fs": fnv1.Resource(ready=fnv1.READY_TRUE),
                "nodepool-gpua10": fnv1.Resource(ready=fnv1.READY_TRUE),
                "nodepool-gpuh100": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
            },
        ),
        want=Derived(
            node_pools=[{"name": "gpua10", "state": "Ready"}, {"name": "gpuh100", "state": "Provisioning"}],
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
        name="AllReady",
        reason="With every resource ready, ClusterReady and NodePoolsReady are True and every pool is Ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(name="gpua10", role="GPU", vmSize="Standard_NV36ads_A10_v5", nodeCount=1),
                        v1alpha1.NodePool(
                            name="gpuh100",
                            role="GPU",
                            vmSize="Standard_ND96isr_H100_v5",
                            nodeCount=1,
                            fabric="InfiniBand",
                        ),
                    ],
                ),
            ),
        ),
        desired=fnv1.State(
            resources={
                "resource-group": fnv1.Resource(ready=fnv1.READY_TRUE),
                "virtual-network": fnv1.Resource(ready=fnv1.READY_TRUE),
                "subnet": fnv1.Resource(ready=fnv1.READY_TRUE),
                "cluster": fnv1.Resource(ready=fnv1.READY_TRUE),
                "release-network-operator": fnv1.Resource(ready=fnv1.READY_TRUE),
                "provider-config-kubernetes": fnv1.Resource(ready=fnv1.READY_TRUE),
                "provider-config-helm": fnv1.Resource(ready=fnv1.READY_TRUE),
                "storage-class-rwx-fs": fnv1.Resource(ready=fnv1.READY_TRUE),
                "nodepool-gpua10": fnv1.Resource(ready=fnv1.READY_TRUE),
                "nodepool-gpuh100": fnv1.Resource(ready=fnv1.READY_TRUE),
            },
        ),
        want=Derived(
            node_pools=[{"name": "gpua10", "state": "Ready"}, {"name": "gpuh100", "state": "Ready"}],
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
        reason="With the provider reporting a pool's node pool create failed, the pool is Failed with the provider's error, even while the cluster provisions.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(name="gpua10", role="GPU", vmSize="Standard_NV36ads_A10_v5", nodeCount=1),
                        v1alpha1.NodePool(
                            name="gpuh100",
                            role="GPU",
                            vmSize="Standard_ND96isr_H100_v5",
                            nodeCount=1,
                            fabric="InfiniBand",
                        ),
                    ],
                ),
                resources={
                    "nodepool-gpuh100": _observed_failed(message="async create failed: SkuNotAvailable"),
                },
            ),
        ),
        desired=fnv1.State(
            resources={
                "resource-group": fnv1.Resource(ready=fnv1.READY_TRUE),
                "cluster": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
                "nodepool-gpua10": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
                "nodepool-gpuh100": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
            },
        ),
        want=Derived(
            node_pools=[
                {"name": "gpua10", "state": "Provisioning"},
                {
                    "name": "gpuh100",
                    "state": "Failed",
                    "message": "nodepool-gpuh100: async create failed: SkuNotAvailable",
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
                    message="1 of 2 node pools failed: gpuh100",
                ),
            ],
        ),
    ),
    DeriveConditionsCase(
        name="ClusterFailed",
        reason="With the provider reporting two cluster resources failed, ClusterReady is Failed with the first one's error.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(name="gpua10", role="GPU", vmSize="Standard_NV36ads_A10_v5", nodeCount=1),
                        v1alpha1.NodePool(
                            name="gpuh100",
                            role="GPU",
                            vmSize="Standard_ND96isr_H100_v5",
                            nodeCount=1,
                            fabric="InfiniBand",
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_failed(message="async create failed: K8sVersionNotSupported"),
                    "subnet": _observed_failed(message="async create failed: NetcfgInvalidSubnet"),
                },
            ),
        ),
        desired=fnv1.State(
            resources={
                "resource-group": fnv1.Resource(ready=fnv1.READY_TRUE),
                "subnet": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
                "cluster": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
                "nodepool-gpua10": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
                "nodepool-gpuh100": fnv1.Resource(ready=fnv1.READY_UNSPECIFIED),
            },
        ),
        want=Derived(
            node_pools=[{"name": "gpua10", "state": "Provisioning"}, {"name": "gpuh100", "state": "Provisioning"}],
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Failed",
                    message="cluster: async create failed: K8sVersionNotSupported (and 1 more)",
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
