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

import dataclasses
import unittest
from typing import Any

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.infrastructure.vultrcluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-vultr-cluster."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


# Name of the cluster's connection secret. Derived like the function derives
# it - the hash suffix depends only on the parent and child names.
_KUBECONFIG_SECRET_NAME = resource.child_name("test-cluster", "kubeconfig")

# The system node pool injected inline into every cluster.
_SYSTEM_POOL = {
    "label": "system",
    "plan": "vc2-6c-16gb",
    "nodeQuantity": 1,
    "autoScaler": True,
    "minNodes": 1,
    "maxNodes": 2,
    "labels": {"modelplane.ai/pool": "system"},
}

# The taint every GPU pool carries.
_GPU_TAINTS = [
    {"key": "nvidia.com/gpu", "value": "true", "effect": "NoSchedule"},
]


def _xr(pools: list[v1alpha1.NodePool]) -> dict:
    """A VultrCluster XR with the given node pools, as a request dict."""
    return v1alpha1.VultrCluster(
        metadata=metav1.ObjectMeta(
            name="test-cluster",
            namespace="modelplane-system",
        ),
        spec=v1alpha1.Spec(
            region="ewr",
            nodePools=pools,
        ),
    ).model_dump(exclude_none=True, mode="json")


def _req(
    pools: list[v1alpha1.NodePool],
    observed_resources: dict[str, fnv1.Resource] | None = None,
) -> fnv1.RunFunctionRequest:
    return fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(resource=resource.dict_to_struct(_xr(pools))),
            resources=observed_resources or {},
        ),
    )


def _cluster(
    cred_kind: str = "ClusterProviderConfig",
    cred_name: str = "default",
) -> dict:
    """A VkeCluster golden with only the system pool."""
    return {
        "apiVersion": "vultr.m.crossplane.io/v1alpha1",
        "kind": "VkeCluster",
        "spec": {
            "providerConfigRef": {"kind": cred_kind, "name": cred_name},
            "forProvider": {
                "label": "test-cluster",
                "region": "ewr",
                "version": "v1.36.1+3",
                "haControlplanes": True,
                "nodePools": [_SYSTEM_POOL],
            },
            "writeConnectionSecretToRef": {"name": _KUBECONFIG_SECRET_NAME},
        },
    }


def _provider_config() -> dict:
    """A provider-kubernetes ProviderConfig golden pointing at the kubeconfig."""
    return {
        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
        "kind": "ProviderConfig",
        "metadata": {
            "name": _KUBECONFIG_SECRET_NAME,
            "namespace": "modelplane-system",
        },
        "spec": {
            "credentials": {
                "source": "Secret",
                "secretRef": {
                    "namespace": "modelplane-system",
                    "name": _KUBECONFIG_SECRET_NAME,
                    "key": "kubeconfig",
                },
            },
        },
    }


def _gpu_observer() -> dict:
    """A provider-kubernetes Object golden that observes the GPU validator DS."""
    return {
        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
        "kind": "Object",
        "metadata": {"namespace": "modelplane-system"},
        "spec": {
            "managementPolicies": ["Observe"],
            "providerConfigRef": {
                "kind": "ProviderConfig",
                "name": _KUBECONFIG_SECRET_NAME,
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


def _helm_provider_config() -> dict:
    """A provider-helm ProviderConfig golden pointing at the kubeconfig."""
    return {
        "apiVersion": "helm.m.crossplane.io/v1beta1",
        "kind": "ProviderConfig",
        "metadata": {
            "name": _KUBECONFIG_SECRET_NAME,
            "namespace": "modelplane-system",
        },
        "spec": {
            "credentials": {
                "source": "Secret",
                "secretRef": {
                    "namespace": "modelplane-system",
                    "name": _KUBECONFIG_SECRET_NAME,
                    "key": "kubeconfig",
                },
            },
        },
    }


def _nfs_installer() -> dict:
    """A provider-kubernetes Object golden installing the NFS client on
    every node, a Longhorn RWX prerequisite VKE's node image lacks."""
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
                        "name": "longhorn-nfs-installation",
                        "namespace": "kube-system",
                        "labels": {"app": "longhorn-nfs-installation"},
                    },
                    "spec": {
                        "selector": {"matchLabels": {"app": "longhorn-nfs-installation"}},
                        "updateStrategy": {"type": "RollingUpdate"},
                        "template": {
                            "metadata": {"labels": {"app": "longhorn-nfs-installation"}},
                            "spec": {
                                "hostNetwork": True,
                                "hostPID": True,
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    },
                                ],
                                "initContainers": [
                                    {
                                        "name": "nfs-installation",
                                        "image": "alpine:3.22",
                                        "command": [
                                            "nsenter",
                                            "--mount=/proc/1/ns/mnt",
                                            "--",
                                            "bash",
                                            "-c",
                                            "apt-get update -qq && apt-get install -y nfs-common && modprobe nfs",
                                        ],
                                        "securityContext": {"privileged": True},
                                    },
                                ],
                                "containers": [
                                    {"name": "sleep", "image": "registry.k8s.io/pause:3.10"},
                                ],
                            },
                        },
                    },
                },
            },
        },
    }


def _longhorn_release() -> dict:
    """A helm Release golden installing Longhorn."""
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
                    "name": "longhorn",
                    "repository": "https://charts.longhorn.io",
                    "version": "1.12.0",
                },
                "namespace": "longhorn-system",
                "values": {
                    "persistence": {"defaultClass": False},
                    "global": {
                        "tolerations": [
                            {
                                "key": "nvidia.com/gpu",
                                "operator": "Exists",
                                "effect": "NoSchedule",
                            },
                        ],
                    },
                    "defaultSettings": {
                        "taintToleration": "nvidia.com/gpu:NoSchedule",
                        "replicaSoftAntiAffinity": "true",
                    },
                },
            },
        },
    }


def _storage_class() -> dict:
    """A provider-kubernetes Object golden for the Longhorn RWX StorageClass."""
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
                    "metadata": {"name": "modelplane-rwx-longhorn"},
                    "provisioner": "driver.longhorn.io",
                    "allowVolumeExpansion": True,
                    "parameters": {
                        "dataEngine": "v1",
                        "numberOfReplicas": "2",
                        "staleReplicaTimeout": "30",
                        "nfsOptions": "vers=4.2,noresvport,softerr,timeo=600,retrans=5",
                    },
                },
            },
        },
    }


def _node_pool(
    label: str,
    plan: str,
    node_quantity: int,
    labels: dict,
    taints: list | None = None,
    *,
    auto_scaler: bool = False,
    min_nodes: int | None = None,
    max_nodes: int | None = None,
    cred_kind: str = "ClusterProviderConfig",
    cred_name: str = "default",
) -> dict:
    """A VkeNodePool golden."""
    fp: dict[str, Any] = {
        "label": label,
        "plan": plan,
        "nodeQuantity": node_quantity,
        "labels": labels,
        "vkeClusterIDSelector": {"matchControllerRef": True},
    }
    if taints:
        fp["taints"] = taints
    if auto_scaler:
        fp["autoScaler"] = True
        fp["minNodes"] = min_nodes
        fp["maxNodes"] = max_nodes
    return {
        "apiVersion": "vultr.m.crossplane.io/v1alpha1",
        "kind": "VkeNodePool",
        "spec": {
            "providerConfigRef": {"kind": cred_kind, "name": cred_name},
            "forProvider": fp,
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
            "cache": {"storageClassName": "modelplane-rwx-longhorn"},
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


def _observed_unready(desired: dict) -> fnv1.Resource:
    """An observed variant of a desired resource with a Ready=False condition."""
    observed = {
        **desired,
        "status": {
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "reason": "Unavailable",
                    "lastTransitionTime": "2024-01-01T00:00:00Z",
                },
            ],
        },
    }
    return fnv1.Resource(resource=resource.dict_to_struct(observed))


_GPU_POOL = v1alpha1.NodePool(
    name="gpu-l40s",
    role="GPU",
    plan="vcg-l40s-16c-180g-48vram",
    maxNodeCount=4,
    gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
)

_GPU_POOL_GOLDEN = _node_pool(
    label="gpu-l40s",
    plan="vcg-l40s-16c-180g-48vram",
    node_quantity=1,
    labels={"modelplane.ai/pool": "gpu-l40s", "modelplane.ai/gpu": "nvidia-l40s"},
    taints=_GPU_TAINTS,
    auto_scaler=True,
    min_nodes=1,
    max_nodes=4,
)


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    """Tests for FunctionRunner.RunFunction."""

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:
        """The function composes VKE cluster infrastructure."""
        cases = [
            Case(
                name="cluster composed first; node pools withheld until cluster Ready",
                req=_req([_GPU_POOL]),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "cluster": fnv1.Resource(
                                resource=resource.dict_to_struct(_cluster()),
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="node pools and GPU observer composed once cluster is Ready; autoscaling from maxNodeCount",
                req=_req(
                    [_GPU_POOL],
                    observed_resources={
                        "cluster": _observed_ready(_cluster()),
                    },
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "cluster": fnv1.Resource(
                                resource=resource.dict_to_struct(_cluster()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "node-pool-gpu-l40s": fnv1.Resource(
                                resource=resource.dict_to_struct(_GPU_POOL_GOLDEN),
                            ),
                            "provider-config-kubernetes": fnv1.Resource(
                                resource=resource.dict_to_struct(_provider_config()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "gpu-observer": fnv1.Resource(
                                resource=resource.dict_to_struct(_gpu_observer()),
                            ),
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(_helm_provider_config()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "nfs-installer": fnv1.Resource(
                                resource=resource.dict_to_struct(_nfs_installer()),
                            ),
                            "release-longhorn": fnv1.Resource(
                                resource=resource.dict_to_struct(_longhorn_release()),
                            ),
                            "storage-class-rwx-longhorn": fnv1.Resource(
                                resource=resource.dict_to_struct(_storage_class()),
                                ready=fnv1.READY_TRUE,
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="dependents kept when the cluster Ready condition transiently regresses",
                req=_req(
                    [_GPU_POOL],
                    observed_resources={
                        "cluster": _observed_unready(_cluster()),
                        "provider-config-kubernetes": _observed_ready(_provider_config()),
                    },
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "cluster": fnv1.Resource(
                                resource=resource.dict_to_struct(_cluster()),
                            ),
                            "node-pool-gpu-l40s": fnv1.Resource(
                                resource=resource.dict_to_struct(_GPU_POOL_GOLDEN),
                            ),
                            "provider-config-kubernetes": fnv1.Resource(
                                resource=resource.dict_to_struct(_provider_config()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "gpu-observer": fnv1.Resource(
                                resource=resource.dict_to_struct(_gpu_observer()),
                            ),
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(_helm_provider_config()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "nfs-installer": fnv1.Resource(
                                resource=resource.dict_to_struct(_nfs_installer()),
                            ),
                            "release-longhorn": fnv1.Resource(
                                resource=resource.dict_to_struct(_longhorn_release()),
                            ),
                            "storage-class-rwx-longhorn": fnv1.Resource(
                                resource=resource.dict_to_struct(_storage_class()),
                                ready=fnv1.READY_TRUE,
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="observed node pool alone keeps dependents composed",
                req=_req(
                    [_GPU_POOL],
                    observed_resources={
                        "cluster": _observed_unready(_cluster()),
                        "node-pool-gpu-l40s": _observed_ready(_GPU_POOL_GOLDEN),
                    },
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "cluster": fnv1.Resource(
                                resource=resource.dict_to_struct(_cluster()),
                            ),
                            "node-pool-gpu-l40s": fnv1.Resource(
                                resource=resource.dict_to_struct(_GPU_POOL_GOLDEN),
                                ready=fnv1.READY_TRUE,
                            ),
                            "provider-config-kubernetes": fnv1.Resource(
                                resource=resource.dict_to_struct(_provider_config()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "gpu-observer": fnv1.Resource(
                                resource=resource.dict_to_struct(_gpu_observer()),
                            ),
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(_helm_provider_config()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "nfs-installer": fnv1.Resource(
                                resource=resource.dict_to_struct(_nfs_installer()),
                            ),
                            "release-longhorn": fnv1.Resource(
                                resource=resource.dict_to_struct(_longhorn_release()),
                            ),
                            "storage-class-rwx-longhorn": fnv1.Resource(
                                resource=resource.dict_to_struct(_storage_class()),
                                ready=fnv1.READY_TRUE,
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="fixed-size GPU pool",
                req=_req(
                    [
                        v1alpha1.NodePool(
                            name="gpu-l40s",
                            role="GPU",
                            plan="vcg-l40s-16c-180g-48vram",
                            nodeCount=2,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l40s"),
                        ),
                    ],
                    observed_resources={
                        "cluster": _observed_ready(_cluster()),
                    },
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "cluster": fnv1.Resource(
                                resource=resource.dict_to_struct(_cluster()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "node-pool-gpu-l40s": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _node_pool(
                                        label="gpu-l40s",
                                        plan="vcg-l40s-16c-180g-48vram",
                                        node_quantity=2,
                                        labels={
                                            "modelplane.ai/pool": "gpu-l40s",
                                            "modelplane.ai/gpu": "nvidia-l40s",
                                        },
                                        taints=_GPU_TAINTS,
                                    ),
                                ),
                            ),
                            "provider-config-kubernetes": fnv1.Resource(
                                resource=resource.dict_to_struct(_provider_config()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "gpu-observer": fnv1.Resource(
                                resource=resource.dict_to_struct(_gpu_observer()),
                            ),
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(_helm_provider_config()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "nfs-installer": fnv1.Resource(
                                resource=resource.dict_to_struct(_nfs_installer()),
                            ),
                            "release-longhorn": fnv1.Resource(
                                resource=resource.dict_to_struct(_longhorn_release()),
                            ),
                            "storage-class-rwx-longhorn": fnv1.Resource(
                                resource=resource.dict_to_struct(_storage_class()),
                                ready=fnv1.READY_TRUE,
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="minNodeCount sets the autoscaler floor; System pool carries no taint",
                req=_req(
                    [
                        v1alpha1.NodePool(
                            name="workers",
                            role="System",
                            plan="vc2-6c-16gb",
                            nodeCount=2,
                            minNodeCount=2,
                            maxNodeCount=5,
                        ),
                    ],
                    observed_resources={
                        "cluster": _observed_ready(_cluster()),
                    },
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "cluster": fnv1.Resource(
                                resource=resource.dict_to_struct(_cluster()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "node-pool-workers": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _node_pool(
                                        label="workers",
                                        plan="vc2-6c-16gb",
                                        node_quantity=2,
                                        labels={"modelplane.ai/pool": "workers"},
                                        auto_scaler=True,
                                        min_nodes=2,
                                        max_nodes=5,
                                    ),
                                ),
                            ),
                            "provider-config-kubernetes": fnv1.Resource(
                                resource=resource.dict_to_struct(_provider_config()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "gpu-observer": fnv1.Resource(
                                resource=resource.dict_to_struct(_gpu_observer()),
                            ),
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(_helm_provider_config()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "nfs-installer": fnv1.Resource(
                                resource=resource.dict_to_struct(_nfs_installer()),
                            ),
                            "release-longhorn": fnv1.Resource(
                                resource=resource.dict_to_struct(_longhorn_release()),
                            ),
                            "storage-class-rwx-longhorn": fnv1.Resource(
                                resource=resource.dict_to_struct(_storage_class()),
                                ready=fnv1.READY_TRUE,
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="VultrCluster Ready only once gpu-observer, NFS installer and Longhorn are Ready",
                req=_req(
                    [_GPU_POOL],
                    observed_resources={
                        "cluster": _observed_ready(_cluster()),
                        "node-pool-gpu-l40s": _observed_ready(_GPU_POOL_GOLDEN),
                        "gpu-observer": _observed_ready(_gpu_observer()),
                        "nfs-installer": _observed_ready(_nfs_installer()),
                        "release-longhorn": _observed_ready(_longhorn_release()),
                    },
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "cluster": fnv1.Resource(
                                resource=resource.dict_to_struct(_cluster()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "node-pool-gpu-l40s": fnv1.Resource(
                                resource=resource.dict_to_struct(_GPU_POOL_GOLDEN),
                                ready=fnv1.READY_TRUE,
                            ),
                            "provider-config-kubernetes": fnv1.Resource(
                                resource=resource.dict_to_struct(_provider_config()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "gpu-observer": fnv1.Resource(
                                resource=resource.dict_to_struct(_gpu_observer()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(_helm_provider_config()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "nfs-installer": fnv1.Resource(
                                resource=resource.dict_to_struct(_nfs_installer()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "release-longhorn": fnv1.Resource(
                                resource=resource.dict_to_struct(_longhorn_release()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "storage-class-rwx-longhorn": fnv1.Resource(
                                resource=resource.dict_to_struct(_storage_class()),
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
