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
from typing import Literal

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
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _xr(
    *,
    credentials: v1alpha1.Credentials | None,
    fabric: Literal["None", "InfiniBand"],
    zones: list[v1alpha1.Zone] | None,
) -> fnv1.Resource:
    """The observed AKSCluster XR, with one gpuh100 pool."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            v1alpha1.AKSCluster(
                metadata=metav1.ObjectMeta(name="test-cluster", namespace="modelplane-system"),
                spec=v1alpha1.Spec(
                    location="westeurope",
                    credentials=credentials,
                    nodePools=[
                        v1alpha1.NodePool(
                            name="gpuh100",
                            role="GPU",
                            vmSize="Standard_ND96isr_H100_v5",
                            diskSizeGb=200,
                            nodeCount=1,
                            minNodeCount=1,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                            fabric=fabric,
                            zones=zones,
                        ),
                    ],
                ),
            ).model_dump(exclude_none=True, mode="json", by_alias=True)
        ),
    )


def _desired_xr() -> fnv1.Resource:
    """The desired XR, publishing its kubeconfig Secret and cache StorageClass."""
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
                    "cache": {"storageClassName": "modelplane-rwx-fs"},
                },
            }
        ),
    )


def _resource_group(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The desired ResourceGroup."""
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
    """The desired VirtualNetwork."""
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
    """The desired Subnet."""
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
    """The desired KubernetesCluster."""
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
    """The desired gpuh100 node pool, in zones if given."""
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
            },
        },
    }
    if zones is not None:
        nodepool["spec"]["forProvider"]["zones"] = zones
    return fnv1.Resource(resource=resource.dict_to_struct(nodepool), ready=ready)


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


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


COMPOSE_CASES = [
    # The StorageClass isn't composed yet: the cluster isn't observed, so the
    # ProviderConfigs can't reach it.
    Case(
        name="first pass composes infra; gated resources wait for the cluster",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(credentials=None, fabric="None", zones=None),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(),
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
        ),
    ),
    Case(
        name="zones pass through to the node pool",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(credentials=None, fabric="None", zones=[v1alpha1.Zone("1")]),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(),
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
        ),
    ),
    Case(
        name="InfiniBand pool composes the network operator once the cluster is observed",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(credentials=None, fabric="InfiniBand", zones=None),
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
                composite=_desired_xr(),
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
        ),
    ),
    Case(
        name="InfiniBand pool before the cluster is observed gates the network operator",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(credentials=None, fabric="InfiniBand", zones=None),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(),
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
        ),
    ),
    # The ProviderConfigs reach the cluster through its kubeconfig, not the
    # cloud credentials.
    Case(
        name="custom credentials flow through to all cloud MRs",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=v1alpha1.Credentials(type="ProviderConfig", name="my-azure-account"),
                    fabric="None",
                    zones=None,
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(),
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
        ),
    ),
    # The cluster is observed, so the StorageClass is composed too.
    Case(
        name="marks managed resources ready from observed conditions",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(credentials=None, fabric="None", zones=None),
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
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction composes AKS cluster infrastructure."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want)
