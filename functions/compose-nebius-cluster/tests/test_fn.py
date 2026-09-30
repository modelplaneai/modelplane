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

import dataclasses
import unittest
from typing import Literal

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.infrastructure.nebiuscluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-nebius-cluster."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


# The Nebius ClusterProviderConfig the function reads the credentials
# Secret off.
_NEBIUS_PROVIDER_CONFIG = {
    "apiVersion": "nebius.m.upbound.io/v1beta1",
    "kind": "ClusterProviderConfig",
    "metadata": {"name": "default"},
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

_PROVIDER_CONFIG_SELECTOR = fnv1.ResourceSelector(
    api_version="nebius.m.upbound.io/v1beta1",
    kind="ClusterProviderConfig",
    match_name="default",
)

# Name of the composed cloud-init Secret. Derived like the function derives
# it - the hash suffix depends only on the parent and child names.
_CLOUD_INIT_SECRET_NAME = resource.child_name("test-cluster", "cloud-init")

# The cloud-init user data mounting the cache filesystem on every node.
_CLOUD_INIT = (
    "#cloud-config\n"
    "runcmd:\n"
    "  - mkdir -p /mnt/data\n"
    "  - mount -t virtiofs modelplane-cache /mnt/data\n"
    '  - printf "modelplane-cache /mnt/data virtiofs defaults,nofail 0 2\\n" >> /etc/fstab\n'
)

# The cache filesystem attachment and cloud-init reference every node group
# template carries.
_TEMPLATE_CACHE_MOUNT = {
    "filesystems": [
        {
            "attachMode": "READ_WRITE",
            "mountTag": "modelplane-cache",
            "existingFilesystem": {"idSelector": {"matchControllerRef": True}},
        },
    ],
    "cloudInitUserDataSecretRef": {"name": _CLOUD_INIT_SECRET_NAME, "key": "userData"},
}


def _xr(
    pools: list[v1alpha1.NodePool],
    credentials: v1alpha1.Credentials | None = None,
    endpoint_access: Literal["PublicAndPrivate", "Private"] | None = None,
) -> dict:
    """A NebiusCluster XR with the given node pools, as a request dict."""
    spec = v1alpha1.Spec(
        nodePools=pools,
        credentials=credentials,
    )
    if endpoint_access is not None:
        spec.endpointAccess = endpoint_access
    return v1alpha1.NebiusCluster(
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
    *,
    with_provider_config: bool = True,
    provider_config_resource: dict | None = None,
    endpoint_access: Literal["PublicAndPrivate", "Private"] | None = None,
) -> fnv1.RunFunctionRequest:
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(
                resource=resource.dict_to_struct(_xr(pools, credentials, endpoint_access)),
            ),
            resources=observed_resources or {},
        ),
    )
    if with_provider_config:
        pc = provider_config_resource if provider_config_resource is not None else _NEBIUS_PROVIDER_CONFIG
        req.required_resources["nebius-provider-config"].items.append(
            fnv1.Resource(resource=resource.dict_to_struct(pc)),
        )
    return req


def _network(cred_kind: str = "ClusterProviderConfig", cred_name: str = "default") -> dict:
    return {
        "apiVersion": "vpc.nebius.m.upbound.io/v1beta1",
        "kind": "Network",
        "spec": {
            "providerConfigRef": {"kind": cred_kind, "name": cred_name},
            "forProvider": {"name": "test-cluster"},
        },
    }


def _subnet(cred_kind: str = "ClusterProviderConfig", cred_name: str = "default") -> dict:
    return {
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


def _cluster(cred_kind: str = "ClusterProviderConfig", cred_name: str = "default") -> dict:
    return {
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


def _private_cluster() -> dict:
    """The cluster golden with endpointAccess Private: the always-present
    private endpoint stands alone, with no public endpoint beside it."""
    cluster = _cluster()
    cluster["spec"]["forProvider"]["controlPlane"]["endpoints"] = {}
    return cluster


def _filesystem(cred_kind: str = "ClusterProviderConfig", cred_name: str = "default") -> dict:
    return {
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


def _cloud_init_secret() -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": _CLOUD_INIT_SECRET_NAME,
            "namespace": "modelplane-system",
        },
        "type": "Opaque",
        "stringData": {"userData": _CLOUD_INIT},
    }


def _csi_release() -> dict:
    return {
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
                    # The node plugin must tolerate the GPU taint so engine
                    # pods on GPU nodes can mount cache PVCs.
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


def _storage_class() -> dict:
    return {
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


def _nodegroup_system(cred_kind: str = "ClusterProviderConfig", cred_name: str = "default") -> dict:
    return {
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
                    **_TEMPLATE_CACHE_MOUNT,
                    "metadata": {"labels": {"modelplane.ai/pool": "system"}},
                },
            },
        },
    }


def _nodegroup_system_user(cred_kind: str = "ClusterProviderConfig", cred_name: str = "default") -> dict:
    """A declared System pool's node group, replacing the injected default."""
    return {
        "apiVersion": "mk8s.nebius.m.upbound.io/v1beta1",
        "kind": "NodeGroup",
        "spec": {
            "providerConfigRef": {"kind": cred_kind, "name": cred_name},
            "forProvider": {
                "name": "test-cluster-sys",
                "parentIdSelector": {"matchControllerRef": True},
                "version": "1.34",
                "autoscaling": {"minNodeCount": 1, "maxNodeCount": 3},
                "template": {
                    "resources": {"platform": "cpu-d3", "preset": "8vcpu-32gb"},
                    "bootDisk": {"sizeGibibytes": 100, "type": "NETWORK_SSD"},
                    "networkInterfaces": [
                        {"subnetIdSelector": {"matchControllerRef": True}},
                    ],
                    **_TEMPLATE_CACHE_MOUNT,
                    "metadata": {"labels": {"modelplane.ai/pool": "sys"}},
                },
            },
        },
    }


def _nodegroup_gpu(
    template_extra: dict,
    cred_kind: str = "ClusterProviderConfig",
    cred_name: str = "default",
    **for_provider_extra: object,
) -> dict:
    """A GPU node group golden with the standard template, merged with the
    given scaling config and extra template fields."""
    template = {
        "resources": {"platform": "gpu-h100-sxm", "preset": "8gpu-128vcpu-1600gb"},
        "bootDisk": {"sizeGibibytes": 200, "type": "NETWORK_SSD"},
        "networkInterfaces": [
            {"subnetIdSelector": {"matchControllerRef": True}},
        ],
        **_TEMPLATE_CACHE_MOUNT,
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
    template.update(template_extra)
    return {
        "apiVersion": "mk8s.nebius.m.upbound.io/v1beta1",
        "kind": "NodeGroup",
        "spec": {
            "providerConfigRef": {"kind": cred_kind, "name": cred_name},
            "forProvider": {
                "name": "test-cluster-gpu-h100",
                "parentIdSelector": {"matchControllerRef": True},
                "version": "1.34",
                "template": template,
                **for_provider_extra,
            },
        },
    }


def _provider_config(api_version: str, kind: str) -> dict:
    return {
        "apiVersion": api_version,
        "kind": kind,
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


def _status(*, with_credentials: bool = True) -> dict:
    secrets: list[dict] = [
        {
            "type": "Kubeconfig",
            "name": "test-cluster-kubeconfig-55b57",
            "key": "kubeconfig",
        },
    ]
    if with_credentials:
        secrets.append(
            {
                "type": "NebiusServiceAccountCredentials",
                "name": "nebius-credentials",
                "key": "credentials.json",
                "namespace": "crossplane-system",
            },
        )
    return {
        "status": {
            "secrets": secrets,
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
    name="gpu-h100",
    role="GPU",
    platform="gpu-h100-sxm",
    preset="8gpu-128vcpu-1600gb",
    diskSizeGb=200,
    maxNodeCount=4,
    gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
)

_SYSTEM_POOL_USER = v1alpha1.NodePool(
    name="sys",
    role="System",
    platform="cpu-d3",
    preset="8vcpu-32gb",
    minNodeCount=1,
    maxNodeCount=3,
)


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    """Tests for FunctionRunner.RunFunction."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:
        """The function composes Nebius mk8s cluster infrastructure."""
        cases = [
            Case(
                name="first pass composes infra resources; autoscaling from maxNodeCount",
                req=_req([_GPU_POOL]),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "network": fnv1.Resource(resource=resource.dict_to_struct(_network())),
                            "subnet": fnv1.Resource(resource=resource.dict_to_struct(_subnet())),
                            "cluster": fnv1.Resource(resource=resource.dict_to_struct(_cluster())),
                            "filesystem": fnv1.Resource(resource=resource.dict_to_struct(_filesystem())),
                            "cloud-init": fnv1.Resource(
                                resource=resource.dict_to_struct(_cloud_init_secret()),
                                ready=fnv1.READY_TRUE,
                            ),
                            # The CSI driver release and StorageClass aren't
                            # composed yet: the cluster isn't observed, so the
                            # ProviderConfigs can't reach it.
                            "nodegroup-system": fnv1.Resource(resource=resource.dict_to_struct(_nodegroup_system())),
                            # nodeCount defaults to 1 and minNodeCount is
                            # unset, so autoscaling starts at the node count.
                            "nodegroup-gpu-h100": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _nodegroup_gpu({}, autoscaling={"minNodeCount": 1, "maxNodeCount": 4}),
                                ),
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
                name="provider config not yet fetched gates provider configs, not infra",
                req=_req([_GPU_POOL], with_provider_config=False),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(_status(with_credentials=False)),
                        ),
                        resources={
                            "network": fnv1.Resource(resource=resource.dict_to_struct(_network())),
                            "subnet": fnv1.Resource(resource=resource.dict_to_struct(_subnet())),
                            "cluster": fnv1.Resource(resource=resource.dict_to_struct(_cluster())),
                            "filesystem": fnv1.Resource(resource=resource.dict_to_struct(_filesystem())),
                            "cloud-init": fnv1.Resource(
                                resource=resource.dict_to_struct(_cloud_init_secret()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "nodegroup-system": fnv1.Resource(resource=resource.dict_to_struct(_nodegroup_system())),
                            "nodegroup-gpu-h100": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _nodegroup_gpu({}, autoscaling={"minNodeCount": 1, "maxNodeCount": 4}),
                                ),
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
                ),
            ),
            Case(
                name="deleted provider config keeps credentials from the observed ProviderConfig",
                req=_req(
                    [_GPU_POOL],
                    observed_resources={
                        "provider-config-kubernetes": fnv1.Resource(
                            resource=resource.dict_to_struct(
                                _provider_config("kubernetes.m.crossplane.io/v1alpha1", "ProviderConfig"),
                            ),
                        ),
                    },
                    with_provider_config=False,
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "network": fnv1.Resource(resource=resource.dict_to_struct(_network())),
                            "subnet": fnv1.Resource(resource=resource.dict_to_struct(_subnet())),
                            "cluster": fnv1.Resource(resource=resource.dict_to_struct(_cluster())),
                            "filesystem": fnv1.Resource(resource=resource.dict_to_struct(_filesystem())),
                            "cloud-init": fnv1.Resource(
                                resource=resource.dict_to_struct(_cloud_init_secret()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "nodegroup-system": fnv1.Resource(resource=resource.dict_to_struct(_nodegroup_system())),
                            "nodegroup-gpu-h100": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _nodegroup_gpu({}, autoscaling={"minNodeCount": 1, "maxNodeCount": 4}),
                                ),
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
                    results=[
                        fnv1.Result(
                            severity=fnv1.SEVERITY_NORMAL,
                            message="Nebius ClusterProviderConfig default not found; keeping the "
                            "credentials the composed ProviderConfig already carries",
                        ),
                    ],
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="fixed-size fabric pool composes a GPU cluster and fixedNodeCount",
                req=_req(
                    [
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
                    ]
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "network": fnv1.Resource(resource=resource.dict_to_struct(_network())),
                            "subnet": fnv1.Resource(resource=resource.dict_to_struct(_subnet())),
                            "cluster": fnv1.Resource(resource=resource.dict_to_struct(_cluster())),
                            "filesystem": fnv1.Resource(resource=resource.dict_to_struct(_filesystem())),
                            "cloud-init": fnv1.Resource(
                                resource=resource.dict_to_struct(_cloud_init_secret()),
                                ready=fnv1.READY_TRUE,
                            ),
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
                            "nodegroup-system": fnv1.Resource(resource=resource.dict_to_struct(_nodegroup_system())),
                            "nodegroup-gpu-h100": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _nodegroup_gpu(
                                        {
                                            "gpuCluster": {
                                                "idSelector": {
                                                    "matchControllerRef": True,
                                                    "matchLabels": {"modelplane.ai/fabric": "fabric-2"},
                                                },
                                            },
                                        },
                                        fixedNodeCount=2,
                                    ),
                                ),
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
                        "network": _observed_ready(_network()),
                        "subnet": _observed_ready(_subnet()),
                        "cluster": _observed_ready(_cluster()),
                        "filesystem": _observed_ready(_filesystem()),
                        "release-csi-mounted-fs-path": _observed_ready(_csi_release()),
                        "nodegroup-system": _observed_ready(_nodegroup_system()),
                        "nodegroup-gpu-h100": _observed_ready(
                            _nodegroup_gpu({}, autoscaling={"minNodeCount": 1, "maxNodeCount": 4}),
                        ),
                    },
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "network": fnv1.Resource(
                                resource=resource.dict_to_struct(_network()),
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
                            "filesystem": fnv1.Resource(
                                resource=resource.dict_to_struct(_filesystem()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "cloud-init": fnv1.Resource(
                                resource=resource.dict_to_struct(_cloud_init_secret()),
                                ready=fnv1.READY_TRUE,
                            ),
                            # The cluster is observed, so the CSI driver
                            # release and StorageClass are composed too.
                            "release-csi-mounted-fs-path": fnv1.Resource(
                                resource=resource.dict_to_struct(_csi_release()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "storage-class-rwx-fs": fnv1.Resource(
                                resource=resource.dict_to_struct(_storage_class()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "nodegroup-system": fnv1.Resource(
                                resource=resource.dict_to_struct(_nodegroup_system()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "nodegroup-gpu-h100": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _nodegroup_gpu({}, autoscaling={"minNodeCount": 1, "maxNodeCount": 4}),
                                ),
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
                name="endpointAccess Private composes no public endpoint",
                req=_req([_GPU_POOL], endpoint_access="Private"),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "network": fnv1.Resource(resource=resource.dict_to_struct(_network())),
                            "subnet": fnv1.Resource(resource=resource.dict_to_struct(_subnet())),
                            "cluster": fnv1.Resource(resource=resource.dict_to_struct(_private_cluster())),
                            "filesystem": fnv1.Resource(resource=resource.dict_to_struct(_filesystem())),
                            "cloud-init": fnv1.Resource(
                                resource=resource.dict_to_struct(_cloud_init_secret()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "nodegroup-system": fnv1.Resource(resource=resource.dict_to_struct(_nodegroup_system())),
                            "nodegroup-gpu-h100": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _nodegroup_gpu({}, autoscaling={"minNodeCount": 1, "maxNodeCount": 4}),
                                ),
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
                name="a declared System pool replaces the injected default",
                req=_req([_SYSTEM_POOL_USER, _GPU_POOL]),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "network": fnv1.Resource(resource=resource.dict_to_struct(_network())),
                            "subnet": fnv1.Resource(resource=resource.dict_to_struct(_subnet())),
                            "cluster": fnv1.Resource(resource=resource.dict_to_struct(_cluster())),
                            "filesystem": fnv1.Resource(resource=resource.dict_to_struct(_filesystem())),
                            "cloud-init": fnv1.Resource(
                                resource=resource.dict_to_struct(_cloud_init_secret()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "nodegroup-sys": fnv1.Resource(
                                resource=resource.dict_to_struct(_nodegroup_system_user()),
                            ),
                            "nodegroup-gpu-h100": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _nodegroup_gpu({}, autoscaling={"minNodeCount": 1, "maxNodeCount": 4}),
                                ),
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
                name="custom credentials flow through to all cloud MRs",
                req=_req(
                    [_GPU_POOL],
                    credentials=v1alpha1.Credentials(type="ProviderConfig", name="my-nebius-account"),
                    provider_config_resource={
                        "apiVersion": "nebius.m.upbound.io/v1beta1",
                        "kind": "ProviderConfig",
                        "metadata": {"name": "my-nebius-account", "namespace": "crossplane-system"},
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
                    },
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "network": fnv1.Resource(
                                resource=resource.dict_to_struct(_network("ProviderConfig", "my-nebius-account")),
                            ),
                            "subnet": fnv1.Resource(
                                resource=resource.dict_to_struct(_subnet("ProviderConfig", "my-nebius-account")),
                            ),
                            "cluster": fnv1.Resource(
                                resource=resource.dict_to_struct(_cluster("ProviderConfig", "my-nebius-account")),
                            ),
                            "filesystem": fnv1.Resource(
                                resource=resource.dict_to_struct(_filesystem("ProviderConfig", "my-nebius-account")),
                            ),
                            "cloud-init": fnv1.Resource(
                                resource=resource.dict_to_struct(_cloud_init_secret()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "nodegroup-system": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _nodegroup_system("ProviderConfig", "my-nebius-account"),
                                ),
                            ),
                            "nodegroup-gpu-h100": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _nodegroup_gpu(
                                        {},
                                        "ProviderConfig",
                                        "my-nebius-account",
                                        autoscaling={"minNodeCount": 1, "maxNodeCount": 4},
                                    ),
                                ),
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
        ]

        # Every compose path declares the provider config requirement; the
        # selector kind and name vary by credentials.
        custom_creds_selector = fnv1.ResourceSelector(
            api_version="nebius.m.upbound.io/v1beta1",
            kind="ProviderConfig",
            match_name="my-nebius-account",
            namespace="modelplane-system",
        )
        for case in cases[:-1]:
            case.want.requirements.resources["nebius-provider-config"].CopyFrom(_PROVIDER_CONFIG_SELECTOR)
        cases[-1].want.requirements.resources["nebius-provider-config"].CopyFrom(custom_creds_selector)

        for case in cases:
            with self.subTest(case.name):
                got = await self.runner.RunFunction(case.req, None)
                self.assertEqual(
                    json_format.MessageToDict(case.want),
                    json_format.MessageToDict(got),
                    "-want, +got",
                )
