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

import dataclasses
import unittest
from typing import Literal

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.infrastructure.akscluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-aks-cluster."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


# Names derived like the function derives them - the hash suffix depends only
# on the input names.
_CLUSTER_NAME = resource.child_name("modelplane-system", "test-cluster", "aks")
_KUBECONFIG_SECRET_NAME = resource.child_name("test-cluster", "kubeconfig")


def _xr(
    pools: list[v1alpha1.NodePool],
    credentials: v1alpha1.Credentials | None = None,
    endpoint_access: Literal["Public", "Private"] | None = None,
) -> dict:
    """An AKSCluster XR with the given node pools, as a request dict."""
    spec = v1alpha1.Spec(location="westeurope", nodePools=pools)
    if credentials is not None:
        spec.credentials = credentials
    if endpoint_access is not None:
        spec.endpointAccess = endpoint_access
    return v1alpha1.AKSCluster(
        metadata=metav1.ObjectMeta(
            name="test-cluster",
            namespace="modelplane-system",
        ),
        spec=spec,
    ).model_dump(exclude_none=True, mode="json")


def _req(
    pools: list[v1alpha1.NodePool],
    observed_resources: dict[str, fnv1.Resource] | None = None,
    credentials: v1alpha1.Credentials | None = None,
    endpoint_access: Literal["Public", "Private"] | None = None,
) -> fnv1.RunFunctionRequest:
    return fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(resource=resource.dict_to_struct(_xr(pools, credentials, endpoint_access))),
            resources=observed_resources or {},
        ),
    )


def _resource_group(cred_kind: str = "ClusterProviderConfig", cred_name: str = "default") -> dict:
    return {
        "apiVersion": "azure.m.upbound.io/v1beta1",
        "kind": "ResourceGroup",
        "metadata": {"name": _CLUSTER_NAME},
        "spec": {
            "providerConfigRef": {"kind": cred_kind, "name": cred_name},
            "forProvider": {"location": "westeurope"},
        },
    }


def _virtual_network(cred_kind: str = "ClusterProviderConfig", cred_name: str = "default") -> dict:
    return {
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


def _subnet(cred_kind: str = "ClusterProviderConfig", cred_name: str = "default") -> dict:
    return {
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


def _cluster(cred_kind: str = "ClusterProviderConfig", cred_name: str = "default") -> dict:
    return {
        "apiVersion": "containerservice.azure.m.upbound.io/v1beta1",
        "kind": "KubernetesCluster",
        "metadata": {"name": _CLUSTER_NAME},
        "spec": {
            "providerConfigRef": {"kind": cred_kind, "name": cred_name},
            "forProvider": {
                "location": "westeurope",
                "kubernetesVersion": "1.34",
                "dnsPrefix": _CLUSTER_NAME,
                "nodeResourceGroup": f"{_CLUSTER_NAME}-nodes",
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
            "writeConnectionSecretToRef": {"name": _KUBECONFIG_SECRET_NAME},
        },
    }


def _private_cluster() -> dict:
    """The cluster golden with endpointAccess Private applied."""
    cluster = _cluster()
    cluster["spec"]["forProvider"]["privateClusterEnabled"] = True
    cluster["spec"]["forProvider"]["privateClusterPublicFqdnEnabled"] = True
    cluster["spec"]["forProvider"]["privateDnsZoneId"] = "None"
    return cluster


def _cluster_with_user_default_pool() -> dict:
    """The cluster golden with the declared System pool as its default pool."""
    cluster = _cluster()
    cluster["spec"]["forProvider"]["defaultNodePool"] = {
        "name": "syspool",
        "vmSize": "Standard_D8s_v5",
        "autoScalingEnabled": True,
        "minCount": 1,
        "maxCount": 3,
        "osDiskSizeGb": 100,
        "temporaryNameForRotation": "syspooltmp",
        "nodeLabels": {"modelplane.ai/pool": "syspool"},
        "vnetSubnetIdSelector": {"matchControllerRef": True},
    }
    return cluster


def _nodepool_system_agent() -> dict:
    """A second declared System pool, composed as a System-mode agent pool."""
    return {
        "apiVersion": "containerservice.azure.m.upbound.io/v1beta1",
        "kind": "KubernetesClusterNodePool",
        "metadata": {"annotations": {"crossplane.io/external-name": "syspool2"}},
        "spec": {
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
            "managementPolicies": ["Observe", "Create", "Update", "Delete"],
            "initProvider": {"nodeCount": 1},
            "forProvider": {
                "kubernetesClusterIdSelector": {"matchControllerRef": True},
                "vnetSubnetIdSelector": {"matchControllerRef": True},
                "mode": "System",
                "vmSize": "Standard_D8s_v5",
                "osDiskSizeGb": 100,
                "orchestratorVersion": "1.34",
                "autoScalingEnabled": True,
                "minCount": 1,
                "maxCount": 2,
                "nodeLabels": {"modelplane.ai/pool": "syspool2"},
            },
        },
    }


def _nodepool_gpu(
    cred_kind: str = "ClusterProviderConfig",
    cred_name: str = "default",
    **for_provider_extra: object,
) -> dict:
    """A GPU node pool golden, merged with extra forProvider fields."""
    return {
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
                **for_provider_extra,
            },
        },
    }


def _network_operator_release() -> dict:
    return {
        "apiVersion": "helm.m.crossplane.io/v1beta1",
        "kind": "Release",
        "metadata": {"namespace": "modelplane-system"},
        "spec": {
            "managementPolicies": ["Observe", "Create", "Update"],
            "providerConfigRef": {
                "kind": "ProviderConfig",
                "name": _KUBECONFIG_SECRET_NAME,
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
                    # The driver and device plugin must tolerate the GPU taint
                    # to run on the InfiniBand nodes.
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


def _storage_class() -> dict:
    return {
        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
        "kind": "Object",
        "metadata": {"namespace": "modelplane-system"},
        "spec": {
            "managementPolicies": ["Observe", "Create", "Update"],
            "providerConfigRef": {
                "kind": "ProviderConfig",
                "name": _KUBECONFIG_SECRET_NAME,
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


def _provider_config(api_version: str, kind: str) -> dict:
    return {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": {"name": _KUBECONFIG_SECRET_NAME},
        "spec": {
            "credentials": {
                "source": "Secret",
                "secretRef": {
                    "name": _KUBECONFIG_SECRET_NAME,
                    "namespace": "modelplane-system",
                    "key": "kubeconfig",
                },
            },
        },
    }


def _status() -> dict:
    return {
        "status": {
            "secrets": [
                {
                    "type": "Kubeconfig",
                    "name": _KUBECONFIG_SECRET_NAME,
                    "key": "kubeconfig",
                },
            ],
            "cache": {"storageClassName": "modelplane-rwx-fs"},
        },
    }


def _observed_ready(desired: dict) -> fnv1.Resource:
    """An observed variant of a desired resource with a Ready=True condition."""
    observed = {
        **desired,
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
    return fnv1.Resource(resource=resource.dict_to_struct(observed))


_GPU_POOL = v1alpha1.NodePool(
    name="gpuh100",
    role="GPU",
    vmSize="Standard_ND96isr_H100_v5",
    diskSizeGb=200,
    nodeCount=1,
    minNodeCount=1,
    maxNodeCount=4,
    gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
)

_GPU_POOL_INFINIBAND = v1alpha1.NodePool(
    name="gpuh100",
    role="GPU",
    vmSize="Standard_ND96isr_H100_v5",
    diskSizeGb=200,
    nodeCount=1,
    minNodeCount=1,
    maxNodeCount=4,
    gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
    fabric="InfiniBand",
)

_SYSTEM_POOL = v1alpha1.NodePool(
    name="syspool",
    role="System",
    vmSize="Standard_D8s_v5",
    nodeCount=1,
    minNodeCount=1,
    maxNodeCount=3,
)

_SYSTEM_POOL_2 = v1alpha1.NodePool(
    name="syspool2",
    role="System",
    vmSize="Standard_D8s_v5",
    nodeCount=1,
    minNodeCount=1,
    maxNodeCount=2,
)


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    """Tests for FunctionRunner.RunFunction."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:
        """The function composes AKS cluster infrastructure."""
        cases = [
            Case(
                name="first pass composes infra; gated resources wait for the cluster",
                req=_req([_GPU_POOL]),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "resource-group": fnv1.Resource(resource=resource.dict_to_struct(_resource_group())),
                            "virtual-network": fnv1.Resource(resource=resource.dict_to_struct(_virtual_network())),
                            "subnet": fnv1.Resource(resource=resource.dict_to_struct(_subnet())),
                            "cluster": fnv1.Resource(resource=resource.dict_to_struct(_cluster())),
                            # The StorageClass isn't composed yet: the cluster
                            # isn't observed, so the ProviderConfigs can't
                            # reach it.
                            "nodepool-gpuh100": fnv1.Resource(resource=resource.dict_to_struct(_nodepool_gpu())),
                            "provider-config-kubernetes": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _provider_config("kubernetes.m.crossplane.io/v1alpha1", "ProviderConfig"),
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _provider_config("helm.m.crossplane.io/v1beta1", "ProviderConfig"),
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="zones pass through to the node pool",
                req=_req(
                    [
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
                    ]
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "resource-group": fnv1.Resource(resource=resource.dict_to_struct(_resource_group())),
                            "virtual-network": fnv1.Resource(resource=resource.dict_to_struct(_virtual_network())),
                            "subnet": fnv1.Resource(resource=resource.dict_to_struct(_subnet())),
                            "cluster": fnv1.Resource(resource=resource.dict_to_struct(_cluster())),
                            "nodepool-gpuh100": fnv1.Resource(
                                resource=resource.dict_to_struct(_nodepool_gpu(zones=["1"])),
                            ),
                            "provider-config-kubernetes": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _provider_config("kubernetes.m.crossplane.io/v1alpha1", "ProviderConfig"),
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _provider_config("helm.m.crossplane.io/v1beta1", "ProviderConfig"),
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="InfiniBand pool composes the network operator once the cluster is observed",
                req=_req(
                    [_GPU_POOL_INFINIBAND],
                    observed_resources={
                        "cluster": _observed_ready(_cluster()),
                    },
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "resource-group": fnv1.Resource(resource=resource.dict_to_struct(_resource_group())),
                            "virtual-network": fnv1.Resource(resource=resource.dict_to_struct(_virtual_network())),
                            "subnet": fnv1.Resource(resource=resource.dict_to_struct(_subnet())),
                            "cluster": fnv1.Resource(
                                resource=resource.dict_to_struct(_cluster()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "nodepool-gpuh100": fnv1.Resource(resource=resource.dict_to_struct(_nodepool_gpu())),
                            "release-network-operator": fnv1.Resource(
                                resource=resource.dict_to_struct(_network_operator_release()),
                            ),
                            "storage-class-rwx-fs": fnv1.Resource(
                                resource=resource.dict_to_struct(_storage_class()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "provider-config-kubernetes": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _provider_config("kubernetes.m.crossplane.io/v1alpha1", "ProviderConfig"),
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _provider_config("helm.m.crossplane.io/v1beta1", "ProviderConfig"),
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="InfiniBand pool before the cluster is observed gates the network operator",
                req=_req([_GPU_POOL_INFINIBAND]),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "resource-group": fnv1.Resource(resource=resource.dict_to_struct(_resource_group())),
                            "virtual-network": fnv1.Resource(resource=resource.dict_to_struct(_virtual_network())),
                            "subnet": fnv1.Resource(resource=resource.dict_to_struct(_subnet())),
                            "cluster": fnv1.Resource(resource=resource.dict_to_struct(_cluster())),
                            "nodepool-gpuh100": fnv1.Resource(resource=resource.dict_to_struct(_nodepool_gpu())),
                            "provider-config-kubernetes": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _provider_config("kubernetes.m.crossplane.io/v1alpha1", "ProviderConfig"),
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _provider_config("helm.m.crossplane.io/v1beta1", "ProviderConfig"),
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="custom credentials flow through to all cloud MRs",
                req=_req(
                    [_GPU_POOL],
                    credentials=v1alpha1.Credentials(
                        type="ProviderConfig",
                        name="my-azure-account",
                    ),
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "resource-group": fnv1.Resource(
                                resource=resource.dict_to_struct(_resource_group("ProviderConfig", "my-azure-account")),
                            ),
                            "virtual-network": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _virtual_network("ProviderConfig", "my-azure-account")
                                ),
                            ),
                            "subnet": fnv1.Resource(
                                resource=resource.dict_to_struct(_subnet("ProviderConfig", "my-azure-account")),
                            ),
                            "cluster": fnv1.Resource(
                                resource=resource.dict_to_struct(_cluster("ProviderConfig", "my-azure-account")),
                            ),
                            "nodepool-gpuh100": fnv1.Resource(
                                resource=resource.dict_to_struct(_nodepool_gpu("ProviderConfig", "my-azure-account")),
                            ),
                            "provider-config-kubernetes": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _provider_config("kubernetes.m.crossplane.io/v1alpha1", "ProviderConfig"),
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _provider_config("helm.m.crossplane.io/v1beta1", "ProviderConfig"),
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="marks managed resources ready from observed conditions",
                req=_req(
                    [_GPU_POOL],
                    observed_resources={
                        "resource-group": _observed_ready(_resource_group()),
                        "virtual-network": _observed_ready(_virtual_network()),
                        "subnet": _observed_ready(_subnet()),
                        "cluster": _observed_ready(_cluster()),
                        "nodepool-gpuh100": _observed_ready(_nodepool_gpu()),
                    },
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "resource-group": fnv1.Resource(
                                resource=resource.dict_to_struct(_resource_group()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "virtual-network": fnv1.Resource(
                                resource=resource.dict_to_struct(_virtual_network()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "subnet": fnv1.Resource(
                                resource=resource.dict_to_struct(_subnet()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "cluster": fnv1.Resource(
                                resource=resource.dict_to_struct(_cluster()),
                                ready=fnv1.READY_TRUE,
                            ),
                            # The cluster is observed, so the StorageClass is
                            # composed too.
                            "storage-class-rwx-fs": fnv1.Resource(
                                resource=resource.dict_to_struct(_storage_class()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "nodepool-gpuh100": fnv1.Resource(
                                resource=resource.dict_to_struct(_nodepool_gpu()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "provider-config-kubernetes": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _provider_config("kubernetes.m.crossplane.io/v1alpha1", "ProviderConfig"),
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _provider_config("helm.m.crossplane.io/v1beta1", "ProviderConfig"),
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="endpointAccess Private composes a private cluster",
                req=_req([_GPU_POOL], endpoint_access="Private"),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "resource-group": fnv1.Resource(resource=resource.dict_to_struct(_resource_group())),
                            "virtual-network": fnv1.Resource(resource=resource.dict_to_struct(_virtual_network())),
                            "subnet": fnv1.Resource(resource=resource.dict_to_struct(_subnet())),
                            "cluster": fnv1.Resource(resource=resource.dict_to_struct(_private_cluster())),
                            "nodepool-gpuh100": fnv1.Resource(resource=resource.dict_to_struct(_nodepool_gpu())),
                            "provider-config-kubernetes": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _provider_config("kubernetes.m.crossplane.io/v1alpha1", "ProviderConfig"),
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _provider_config("helm.m.crossplane.io/v1beta1", "ProviderConfig"),
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="a declared System pool becomes the cluster's default node pool",
                req=_req([_SYSTEM_POOL, _GPU_POOL]),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "resource-group": fnv1.Resource(resource=resource.dict_to_struct(_resource_group())),
                            "virtual-network": fnv1.Resource(resource=resource.dict_to_struct(_virtual_network())),
                            "subnet": fnv1.Resource(resource=resource.dict_to_struct(_subnet())),
                            "cluster": fnv1.Resource(
                                resource=resource.dict_to_struct(_cluster_with_user_default_pool()),
                            ),
                            # The System pool is the cluster's inline default
                            # pool, so only the GPU pool composes an agent
                            # pool.
                            "nodepool-gpuh100": fnv1.Resource(resource=resource.dict_to_struct(_nodepool_gpu())),
                            "provider-config-kubernetes": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _provider_config("kubernetes.m.crossplane.io/v1alpha1", "ProviderConfig"),
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _provider_config("helm.m.crossplane.io/v1beta1", "ProviderConfig"),
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="a second System pool composes as a System-mode agent pool",
                req=_req([_SYSTEM_POOL, _SYSTEM_POOL_2, _GPU_POOL]),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "resource-group": fnv1.Resource(resource=resource.dict_to_struct(_resource_group())),
                            "virtual-network": fnv1.Resource(resource=resource.dict_to_struct(_virtual_network())),
                            "subnet": fnv1.Resource(resource=resource.dict_to_struct(_subnet())),
                            "cluster": fnv1.Resource(
                                resource=resource.dict_to_struct(_cluster_with_user_default_pool()),
                            ),
                            "nodepool-syspool2": fnv1.Resource(
                                resource=resource.dict_to_struct(_nodepool_system_agent()),
                            ),
                            "nodepool-gpuh100": fnv1.Resource(resource=resource.dict_to_struct(_nodepool_gpu())),
                            "provider-config-kubernetes": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _provider_config("kubernetes.m.crossplane.io/v1alpha1", "ProviderConfig"),
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _provider_config("helm.m.crossplane.io/v1beta1", "ProviderConfig"),
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                ),
            ),
        ]

        for case in cases:
            with self.subTest(case.name):
                got = await self.runner.RunFunction(case.req, None)
                self.assertEqual(
                    json_format.MessageToDict(case.want),
                    json_format.MessageToDict(got),
                    "-want, +got",
                )
