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

"""Tests for the compose-nebius-cluster function."""

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
from models.ai.modelplane.infrastructure.nebiuscluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-nebius-cluster."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _xr(*, credentials: v1alpha1.Credentials | None, node_pools: list[v1alpha1.NodePool]) -> fnv1.Resource:
    """The observed NebiusCluster XR, with the given credentials and node pools."""
    xr = v1alpha1.NebiusCluster(
        metadata=metav1.ObjectMeta(name="test-cluster", namespace="modelplane-system"),
        spec=v1alpha1.Spec(
            credentials=credentials,
            nodePools=node_pools,
        ),
    )
    return fnv1.Resource(resource=resource.dict_to_struct(xr.model_dump(exclude_none=True, mode="json", by_alias=True)))


def _desired_xr(*, credentials_secret: bool) -> fnv1.Resource:
    """The desired XR's status, which names the credentials Secret only if credentials_secret."""
    secrets = [{"type": "Kubeconfig", "name": "test-cluster-kubeconfig-55b57", "key": "kubeconfig"}]
    if credentials_secret:
        secrets.append(
            {
                "type": "NebiusServiceAccountCredentials",
                "name": "nebius-credentials",
                "key": "credentials.json",
                "namespace": "crossplane-system",
            },
        )
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {"status": {"secrets": secrets, "cache": {"storageClassName": "modelplane-rwx-fs"}}},
        ),
    )


def _nebius_provider_config(*, kind: str, name: str, namespace: str | None) -> fnv1.Resource:
    """The Nebius provider config the XR's credentials name, sourcing them from a Secret."""
    metadata = {"name": name}
    if namespace is not None:
        metadata["namespace"] = namespace
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "nebius.m.upbound.io/v1beta1",
                "kind": kind,
                "metadata": metadata,
                "spec": {
                    "identity": {"type": "ServiceAccount"},
                    "credentials": {
                        "source": "Secret",
                        "secretRef": {
                            "namespace": "crossplane-system",
                            "name": "nebius-credentials",
                            "key": "credentials.json",
                        },
                    },
                    "projectID": "project-e00test",
                },
            }
        ),
    )


def _network(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed VPC network."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "vpc.nebius.m.upbound.io/v1beta1",
                "kind": "Network",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {"name": "test-cluster"},
                },
            }
        ),
        ready=ready,
    )


def _subnet(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed subnet."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "vpc.nebius.m.upbound.io/v1beta1",
                "kind": "Subnet",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "name": "test-cluster",
                        "networkIdSelector": {"matchControllerRef": True},
                        "ipv4PrivatePools": {"useNetworkPools": True},
                    },
                },
            }
        ),
        ready=ready,
    )


def _cluster(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed mk8s cluster."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "mk8s.nebius.m.upbound.io/v1beta1",
                "kind": "Cluster",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "name": "test-cluster",
                        "controlPlane": {
                            "version": "1.34",
                            "subnetIdSelector": {"matchControllerRef": True},
                            "endpoints": {"publicEndpoint": {}},
                        },
                    },
                    "writeConnectionSecretToRef": {"name": "test-cluster-kubeconfig-55b57"},
                },
            }
        ),
        ready=ready,
    )


def _filesystem(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed cache filesystem."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "compute.nebius.m.upbound.io/v1beta1",
                "kind": "Filesystem",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "name": "test-cluster-cache",
                        "type": "NETWORK_SSD",
                        "sizeGibibytes": 1024,
                    },
                },
            }
        ),
        ready=ready,
    )


def _cloud_init_secret() -> fnv1.Resource:
    """The Ready Secret holding the cloud-init user data that mounts the cache filesystem on every node."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": "test-cluster-cloud-init-fd2f2",
                    "namespace": "modelplane-system",
                },
                "type": "Opaque",
                "stringData": {
                    "userData": (
                        "#cloud-config\n"
                        "runcmd:\n"
                        "  - mkdir -p /mnt/data\n"
                        "  - mount -t virtiofs modelplane-cache /mnt/data\n"
                        '  - printf "modelplane-cache /mnt/data virtiofs defaults,nofail 0 2\\n" >> /etc/fstab\n'
                    ),
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _nodegroup_system(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed system node group."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "mk8s.nebius.m.upbound.io/v1beta1",
                "kind": "NodeGroup",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "name": "test-cluster-system",
                        "parentIdSelector": {"matchControllerRef": True},
                        "version": "1.34",
                        "autoscaling": {"minNodeCount": 1, "maxNodeCount": 2},
                        "template": {
                            "resources": {"platform": "cpu-d3", "preset": "4vcpu-16gb"},
                            "bootDisk": {"sizeGibibytes": 100, "type": "NETWORK_SSD"},
                            "networkInterfaces": [
                                {"subnetIdSelector": {"matchControllerRef": True}},
                            ],
                            "filesystems": [
                                {
                                    "attachMode": "READ_WRITE",
                                    "mountTag": "modelplane-cache",
                                    "existingFilesystem": {"idSelector": {"matchControllerRef": True}},
                                },
                            ],
                            "cloudInitUserDataSecretRef": {"name": "test-cluster-cloud-init-fd2f2", "key": "userData"},
                            "metadata": {"labels": {"modelplane.ai/pool": "system"}},
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _nodegroup_gpu(
    *,
    cred_kind: str,
    cred_name: str,
    autoscaling: dict | None,
    fixed_node_count: int | None,
    fabric: str | None,
    ready: fnv1.Ready,
) -> fnv1.Resource:
    """The composed node group for the gpu-h100 pool, on fabric's GPU cluster if any."""
    template: dict[str, Any] = {
        "resources": {"platform": "gpu-h100-sxm", "preset": "8gpu-128vcpu-1600gb"},
        "bootDisk": {"sizeGibibytes": 200, "type": "NETWORK_SSD"},
        "networkInterfaces": [
            {"subnetIdSelector": {"matchControllerRef": True}},
        ],
        "filesystems": [
            {
                "attachMode": "READ_WRITE",
                "mountTag": "modelplane-cache",
                "existingFilesystem": {"idSelector": {"matchControllerRef": True}},
            },
        ],
        "cloudInitUserDataSecretRef": {"name": "test-cluster-cloud-init-fd2f2", "key": "userData"},
        "metadata": {
            "labels": {
                "modelplane.ai/pool": "gpu-h100",
                "modelplane.ai/gpu": "nvidia-h100",
            },
        },
        "gpuSettings": {"driversPreset": "cuda13.0"},
        "taints": [
            {"key": "nvidia.com/gpu", "value": "true", "effect": "NO_SCHEDULE"},
        ],
    }
    if fabric is not None:
        template["gpuCluster"] = {
            "idSelector": {"matchControllerRef": True, "matchLabels": {"modelplane.ai/fabric": fabric}},
        }
    for_provider: dict[str, Any] = {
        "name": "test-cluster-gpu-h100",
        "parentIdSelector": {"matchControllerRef": True},
        "version": "1.34",
        "template": template,
    }
    if autoscaling is not None:
        for_provider["autoscaling"] = autoscaling
    if fixed_node_count is not None:
        for_provider["fixedNodeCount"] = fixed_node_count
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "mk8s.nebius.m.upbound.io/v1beta1",
                "kind": "NodeGroup",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": for_provider,
                },
            }
        ),
        ready=ready,
    )


def _nodegroup_cpu(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed node group for the cpu-pool pool, with the CPU label and taint and no GPU settings."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "mk8s.nebius.m.upbound.io/v1beta1",
                "kind": "NodeGroup",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "name": "test-cluster-cpu-pool",
                        "parentIdSelector": {"matchControllerRef": True},
                        "version": "1.34",
                        "template": {
                            "resources": {"platform": "cpu-d3", "preset": "8vcpu-32gb"},
                            "bootDisk": {"sizeGibibytes": 100, "type": "NETWORK_SSD"},
                            "networkInterfaces": [
                                {"subnetIdSelector": {"matchControllerRef": True}},
                            ],
                            "filesystems": [
                                {
                                    "attachMode": "READ_WRITE",
                                    "mountTag": "modelplane-cache",
                                    "existingFilesystem": {"idSelector": {"matchControllerRef": True}},
                                },
                            ],
                            "cloudInitUserDataSecretRef": {
                                "name": "test-cluster-cloud-init-fd2f2",
                                "key": "userData",
                            },
                            "metadata": {
                                "labels": {
                                    "modelplane.ai/pool": "cpu-pool",
                                    "modelplane.ai/cpu": "true",
                                },
                            },
                            "taints": [
                                {"key": "modelplane.ai/cpu", "value": "true", "effect": "NO_SCHEDULE"},
                            ],
                        },
                        "autoscaling": {"minNodeCount": 1, "maxNodeCount": 4},
                    },
                },
            }
        ),
        ready=ready,
    )


def _provider_config(*, api_version: str) -> fnv1.Resource:
    """A Ready ProviderConfig targeting the cluster as the Nebius service account."""
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
                    "identity": {
                        "type": "NebiusServiceAccountCredentials",
                        "source": "Secret",
                        "secretRef": {
                            "name": "nebius-credentials",
                            "namespace": "crossplane-system",
                            "key": "credentials.json",
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


# Every case is a NebiusCluster named test-cluster in modelplane-system. Every
# response requires the Nebius provider config the XR's credentials name, which
# is the ClusterProviderConfig named default unless the XR says otherwise. The
# function reads the credentials Secret off it.
COMPOSE_CASES = [
    # The pool sets maxNodeCount but not minNodeCount, and its nodeCount is 1,
    # so this case can't tell an autoscaling floor taken from nodeCount from one
    # fixed at 1. The CSI driver release and StorageClass aren't composed yet:
    # the cluster isn't observed, so the ProviderConfigs can't reach it.
    Case(
        name="FirstPass",
        reason="With nothing observed, a NebiusCluster composes its Nebius infrastructure and ProviderConfigs, autoscaling the GPU pool from one node to maxNodeCount.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h100",
                            role="GPU",
                            platform="gpu-h100-sxm",
                            preset="8gpu-128vcpu-1600gb",
                            diskSizeGb=200,
                            nodeCount=1,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                        ),
                    ],
                ),
            ),
            required_resources={
                "nebius-provider-config": fnv1.Resources(
                    items=[_nebius_provider_config(kind="ClusterProviderConfig", name="default", namespace=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(credentials_secret=True),
                resources={
                    "network": _network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "subnet": _subnet(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "filesystem": _filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cloud-init": _cloud_init_secret(),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-gpu-h100": _nodegroup_gpu(
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        autoscaling={"minNodeCount": 1, "maxNodeCount": 4},
                        fixed_node_count=None,
                        fabric=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "nebius-provider-config": fnv1.ResourceSelector(
                        api_version="nebius.m.upbound.io/v1beta1",
                        kind="ClusterProviderConfig",
                        match_name="default",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="FirstPassCPUPool",
        reason="A CPU-role node group composes with the modelplane.ai/pool and modelplane.ai/cpu labels and the CPU taint, and no GPU settings.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="cpu-pool",
                            role="CPU",
                            platform="cpu-d3",
                            preset="8vcpu-32gb",
                            nodeCount=1,
                            maxNodeCount=4,
                        ),
                    ],
                ),
            ),
            required_resources={
                "nebius-provider-config": fnv1.Resources(
                    items=[_nebius_provider_config(kind="ClusterProviderConfig", name="default", namespace=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(credentials_secret=True),
                resources={
                    "network": _network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "subnet": _subnet(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "filesystem": _filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cloud-init": _cloud_init_secret(),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-cpu-pool": _nodegroup_cpu(
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "nebius-provider-config": fnv1.ResourceSelector(
                        api_version="nebius.m.upbound.io/v1beta1",
                        kind="ClusterProviderConfig",
                        match_name="default",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="ProviderConfigNotFetched",
        reason="Before the Nebius provider config is fetched, and with no ProviderConfig observed, a NebiusCluster composes its infrastructure but not the ProviderConfigs, and reports it's waiting.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h100",
                            role="GPU",
                            platform="gpu-h100-sxm",
                            preset="8gpu-128vcpu-1600gb",
                            diskSizeGb=200,
                            nodeCount=1,
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
                composite=_desired_xr(credentials_secret=False),
                resources={
                    "network": _network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "subnet": _subnet(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "filesystem": _filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cloud-init": _cloud_init_secret(),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-gpu-h100": _nodegroup_gpu(
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        autoscaling={"minNodeCount": 1, "maxNodeCount": 4},
                        fixed_node_count=None,
                        fabric=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Waiting for Nebius ClusterProviderConfig default",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "nebius-provider-config": fnv1.ResourceSelector(
                        api_version="nebius.m.upbound.io/v1beta1",
                        kind="ClusterProviderConfig",
                        match_name="default",
                    ),
                },
            ),
        ),
    ),
    # Keeping the observed credentials stops the function tearing the
    # ProviderConfigs out of desired state. Here the config hasn't been fetched
    # yet, and the function falls back the same way when it's gone.
    Case(
        name="ObservedCredentials",
        reason="With the Nebius provider config not yet fetched but a composed ProviderConfig observed, a NebiusCluster keeps that ProviderConfig's credentials and still composes the ProviderConfigs.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h100",
                            role="GPU",
                            platform="gpu-h100-sxm",
                            preset="8gpu-128vcpu-1600gb",
                            diskSizeGb=200,
                            nodeCount=1,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                        ),
                    ],
                ),
                resources={
                    "provider-config-kubernetes": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
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
                                    "identity": {
                                        "type": "NebiusServiceAccountCredentials",
                                        "source": "Secret",
                                        "secretRef": {
                                            "name": "nebius-credentials",
                                            "namespace": "crossplane-system",
                                            "key": "credentials.json",
                                        },
                                    },
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
                composite=_desired_xr(credentials_secret=True),
                resources={
                    "network": _network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "subnet": _subnet(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "filesystem": _filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cloud-init": _cloud_init_secret(),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-gpu-h100": _nodegroup_gpu(
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        autoscaling={"minNodeCount": 1, "maxNodeCount": 4},
                        fixed_node_count=None,
                        fabric=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Nebius ClusterProviderConfig default not found; keeping the "
                    "credentials the composed ProviderConfig already carries",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "nebius-provider-config": fnv1.ResourceSelector(
                        api_version="nebius.m.upbound.io/v1beta1",
                        kind="ClusterProviderConfig",
                        match_name="default",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="FixedInfiniBandPool",
        reason="A NebiusCluster with a fixed-size pool on InfiniBand fabric-2 composes a GpuCluster for the fabric and a node group of fixedNodeCount 2 on it.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h100",
                            role="GPU",
                            platform="gpu-h100-sxm",
                            preset="8gpu-128vcpu-1600gb",
                            diskSizeGb=200,
                            nodeCount=2,
                            fabric=v1alpha1.Fabric(
                                type="InfiniBand",
                                infiniband=v1alpha1.Infiniband(fabric="fabric-2"),
                            ),
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                        ),
                    ],
                ),
            ),
            required_resources={
                "nebius-provider-config": fnv1.Resources(
                    items=[_nebius_provider_config(kind="ClusterProviderConfig", name="default", namespace=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(credentials_secret=True),
                resources={
                    "network": _network(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "subnet": _subnet(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "filesystem": _filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cloud-init": _cloud_init_secret(),
                    "gpu-cluster-fabric-2": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "compute.nebius.m.upbound.io/v1beta1",
                                "kind": "GpuCluster",
                                "metadata": {"labels": {"modelplane.ai/fabric": "fabric-2"}},
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "forProvider": {
                                        "name": "test-cluster-fabric-2",
                                        "infinibandFabric": "fabric-2",
                                    },
                                },
                            }
                        ),
                    ),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-gpu-h100": _nodegroup_gpu(
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        autoscaling=None,
                        fixed_node_count=2,
                        fabric="fabric-2",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "nebius-provider-config": fnv1.ResourceSelector(
                        api_version="nebius.m.upbound.io/v1beta1",
                        kind="ClusterProviderConfig",
                        match_name="default",
                    ),
                },
            ),
        ),
    ),
    # The release tolerates the GPU taint so its node plugin runs on the GPU
    # nodes, where engine pods mount cache PVCs.
    Case(
        name="ResourcesReady",
        reason="With the cluster, its other Nebius resources and the CSI driver release observed Ready, a NebiusCluster marks them ready and composes the release and StorageClass.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h100",
                            role="GPU",
                            platform="gpu-h100-sxm",
                            preset="8gpu-128vcpu-1600gb",
                            diskSizeGb=200,
                            nodeCount=1,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                        ),
                    ],
                ),
                resources={
                    "network": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "vpc.nebius.m.upbound.io/v1beta1",
                                "kind": "Network",
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "forProvider": {"name": "test-cluster"},
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
                                "apiVersion": "vpc.nebius.m.upbound.io/v1beta1",
                                "kind": "Subnet",
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "forProvider": {
                                        "name": "test-cluster",
                                        "networkIdSelector": {"matchControllerRef": True},
                                        "ipv4PrivatePools": {"useNetworkPools": True},
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
                                "apiVersion": "mk8s.nebius.m.upbound.io/v1beta1",
                                "kind": "Cluster",
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "forProvider": {
                                        "name": "test-cluster",
                                        "controlPlane": {
                                            "version": "1.34",
                                            "subnetIdSelector": {"matchControllerRef": True},
                                            "endpoints": {"publicEndpoint": {}},
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
                    "filesystem": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "compute.nebius.m.upbound.io/v1beta1",
                                "kind": "Filesystem",
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "forProvider": {
                                        "name": "test-cluster-cache",
                                        "type": "NETWORK_SSD",
                                        "sizeGibibytes": 1024,
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
                    "release-csi-mounted-fs-path": fnv1.Resource(
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
                                            "name": "csi-mounted-fs-path",
                                            "repository": "oci://cr.eu-north1.nebius.cloud/mk8s/helm",
                                            "version": "0.1.6",
                                        },
                                        "namespace": "kube-system",
                                        "values": {
                                            "dataDir": "/mnt/data/csi-mounted-fs-path-data/",
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
                    "nodegroup-system": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "mk8s.nebius.m.upbound.io/v1beta1",
                                "kind": "NodeGroup",
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "forProvider": {
                                        "name": "test-cluster-system",
                                        "parentIdSelector": {"matchControllerRef": True},
                                        "version": "1.34",
                                        "autoscaling": {"minNodeCount": 1, "maxNodeCount": 2},
                                        "template": {
                                            "resources": {"platform": "cpu-d3", "preset": "4vcpu-16gb"},
                                            "bootDisk": {"sizeGibibytes": 100, "type": "NETWORK_SSD"},
                                            "networkInterfaces": [
                                                {"subnetIdSelector": {"matchControllerRef": True}},
                                            ],
                                            "filesystems": [
                                                {
                                                    "attachMode": "READ_WRITE",
                                                    "mountTag": "modelplane-cache",
                                                    "existingFilesystem": {"idSelector": {"matchControllerRef": True}},
                                                },
                                            ],
                                            "cloudInitUserDataSecretRef": {
                                                "name": "test-cluster-cloud-init-fd2f2",
                                                "key": "userData",
                                            },
                                            "metadata": {"labels": {"modelplane.ai/pool": "system"}},
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
                    "nodegroup-gpu-h100": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "mk8s.nebius.m.upbound.io/v1beta1",
                                "kind": "NodeGroup",
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "forProvider": {
                                        "name": "test-cluster-gpu-h100",
                                        "parentIdSelector": {"matchControllerRef": True},
                                        "version": "1.34",
                                        "template": {
                                            "resources": {"platform": "gpu-h100-sxm", "preset": "8gpu-128vcpu-1600gb"},
                                            "bootDisk": {"sizeGibibytes": 200, "type": "NETWORK_SSD"},
                                            "networkInterfaces": [
                                                {"subnetIdSelector": {"matchControllerRef": True}},
                                            ],
                                            "filesystems": [
                                                {
                                                    "attachMode": "READ_WRITE",
                                                    "mountTag": "modelplane-cache",
                                                    "existingFilesystem": {"idSelector": {"matchControllerRef": True}},
                                                },
                                            ],
                                            "cloudInitUserDataSecretRef": {
                                                "name": "test-cluster-cloud-init-fd2f2",
                                                "key": "userData",
                                            },
                                            "metadata": {
                                                "labels": {
                                                    "modelplane.ai/pool": "gpu-h100",
                                                    "modelplane.ai/gpu": "nvidia-h100",
                                                },
                                            },
                                            "gpuSettings": {"driversPreset": "cuda13.0"},
                                            "taints": [
                                                {"key": "nvidia.com/gpu", "value": "true", "effect": "NO_SCHEDULE"},
                                            ],
                                        },
                                        "autoscaling": {"minNodeCount": 1, "maxNodeCount": 4},
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
            required_resources={
                "nebius-provider-config": fnv1.Resources(
                    items=[_nebius_provider_config(kind="ClusterProviderConfig", name="default", namespace=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(credentials_secret=True),
                resources={
                    "network": _network(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "subnet": _subnet(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "filesystem": _filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "cloud-init": _cloud_init_secret(),
                    "release-csi-mounted-fs-path": fnv1.Resource(
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
                                            "name": "csi-mounted-fs-path",
                                            "repository": "oci://cr.eu-north1.nebius.cloud/mk8s/helm",
                                            "version": "0.1.6",
                                        },
                                        "namespace": "kube-system",
                                        "values": {
                                            "dataDir": "/mnt/data/csi-mounted-fs-path-data/",
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
                            }
                        ),
                        ready=fnv1.READY_TRUE,
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
                                            "provisioner": "mounted-fs-path.csi.nebius.ai",
                                            "volumeBindingMode": "WaitForFirstConsumer",
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "nodegroup-gpu-h100": _nodegroup_gpu(
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        autoscaling={"minNodeCount": 1, "maxNodeCount": 4},
                        fixed_node_count=None,
                        fabric=None,
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "nebius-provider-config": fnv1.ResourceSelector(
                        api_version="nebius.m.upbound.io/v1beta1",
                        kind="ClusterProviderConfig",
                        match_name="default",
                    ),
                },
            ),
        ),
    ),
    # The XR names a namespaced ProviderConfig, so the function requires it from
    # the XR's own namespace. The ProviderConfig returned here sits in
    # crossplane-system, which Crossplane wouldn't return for that selector.
    # Status names crossplane-system as the credentials Secret's namespace. That
    # is both the ProviderConfig's namespace and the one its secretRef sets, so
    # this case can't show which of them the function reads.
    Case(
        name="CustomCredentials",
        reason="The ProviderConfig a NebiusCluster's spec.credentials names becomes every Nebius managed resource's providerConfigRef.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=v1alpha1.Credentials(type="ProviderConfig", name="my-nebius-account"),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h100",
                            role="GPU",
                            platform="gpu-h100-sxm",
                            preset="8gpu-128vcpu-1600gb",
                            diskSizeGb=200,
                            nodeCount=1,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                        ),
                    ],
                ),
            ),
            required_resources={
                "nebius-provider-config": fnv1.Resources(
                    items=[
                        _nebius_provider_config(
                            kind="ProviderConfig", name="my-nebius-account", namespace="crossplane-system"
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(credentials_secret=True),
                resources={
                    "network": _network(
                        cred_kind="ProviderConfig", cred_name="my-nebius-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "subnet": _subnet(
                        cred_kind="ProviderConfig", cred_name="my-nebius-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster": _cluster(
                        cred_kind="ProviderConfig", cred_name="my-nebius-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "filesystem": _filesystem(
                        cred_kind="ProviderConfig", cred_name="my-nebius-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cloud-init": _cloud_init_secret(),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ProviderConfig", cred_name="my-nebius-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-gpu-h100": _nodegroup_gpu(
                        cred_kind="ProviderConfig",
                        cred_name="my-nebius-account",
                        autoscaling={"minNodeCount": 1, "maxNodeCount": 4},
                        fixed_node_count=None,
                        fabric=None,
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "nebius-provider-config": fnv1.ResourceSelector(
                        api_version="nebius.m.upbound.io/v1beta1",
                        kind="ProviderConfig",
                        match_name="my-nebius-account",
                        namespace="modelplane-system",
                    ),
                },
            ),
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction composes a NebiusCluster's network, mk8s cluster, node groups and ProviderConfigs."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
