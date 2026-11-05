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

"""Tests for the compose-coreweave-cluster function."""

import asyncio
import dataclasses
import datetime
import json
from typing import Any

import pytest
from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format, message
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.infrastructure.coreweavecluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# The kubeconfig the function renders for the observed cluster's endpoint and
# the token in _credentials_secret.
_KUBECONFIG = """{
  "apiVersion": "v1",
  "kind": "Config",
  "clusters": [
    {
      "name": "cks",
      "cluster": {
        "server": "https://abc123-abcd1234.k8s.us-east-04a.coreweave.com"
      }
    }
  ],
  "users": [
    {
      "name": "cks",
      "user": {
        "token": "CW-SECRET-test"
      }
    }
  ],
  "contexts": [
    {
      "name": "cks",
      "context": {
        "cluster": "cks",
        "user": "cks"
      }
    }
  ],
  "current-context": "cks"
}"""


@dataclasses.dataclass
class Case:
    """A test case for compose-coreweave-cluster."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _xr(*, credentials: v1alpha1.Credentials | None, node_pools: list[v1alpha1.NodePool], ready: bool) -> fnv1.Resource:
    """The observed CoreWeaveCluster XR, with the given credentials and node pools, and a Ready condition if ready."""
    xr = v1alpha1.CoreWeaveCluster(
        metadata=metav1.ObjectMeta(
            name="test-cluster",
            namespace="modelplane-system",
        ),
        spec=v1alpha1.Spec(
            zone="US-EAST-04A",
            kubernetesVersion="1.37",
            credentials=credentials,
            nodePools=node_pools,
        ),
    )
    if ready:
        xr.status = v1alpha1.Status(
            conditions=[
                v1alpha1.Condition(
                    type="Ready",
                    status="True",
                    reason="Available",
                    lastTransitionTime=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC),
                ),
            ],
        )
    return fnv1.Resource(resource=resource.dict_to_struct(xr.model_dump(exclude_none=True, mode="json", by_alias=True)))


def _desired_xr(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The desired XR, publishing the kubeconfig Secret and the cache StorageClass."""
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
        ready=ready,
    )


def _vpc(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed VPC."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "networking.coreweave.m.upbound.io/v1beta1",
                "kind": "VPC",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "name": "test-cluster-modelplane-21669",
                        "zone": "US-EAST-04A",
                        "vpcPrefixes": [
                            {"name": "pod-cidr", "value": "10.0.0.0/13"},
                            {"name": "service-cidr", "value": "10.16.0.0/22"},
                            {"name": "internal-lb-cidr", "value": "10.32.4.0/22"},
                        ],
                    },
                },
            }
        ),
        ready=ready,
    )


def _cluster(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed CKS cluster."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "cks.coreweave.m.upbound.io/v1beta1",
                "kind": "Cluster",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "name": "test-cluster-modelplane-21669",
                        "zone": "US-EAST-04A",
                        "version": "v1.37",
                        "public": True,
                        "vpcIdSelector": {"matchControllerRef": True},
                        "podCidrName": "pod-cidr",
                        "serviceCidrName": "service-cidr",
                        "internalLbCidrNames": ["internal-lb-cidr"],
                    },
                },
            }
        ),
        ready=ready,
    )


def _observed_cluster(*, ready: bool) -> fnv1.Resource:
    """The CKS cluster as observed, reporting its API server endpoint, with a Ready condition that's True if ready and False if not."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "cks.coreweave.m.upbound.io/v1beta1",
                "kind": "Cluster",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                    "forProvider": {
                        "name": "test-cluster-modelplane-21669",
                        "zone": "US-EAST-04A",
                        "version": "v1.37",
                        "public": True,
                        "vpcIdSelector": {"matchControllerRef": True},
                        "podCidrName": "pod-cidr",
                        "serviceCidrName": "service-cidr",
                        "internalLbCidrNames": ["internal-lb-cidr"],
                    },
                },
                "status": {
                    "atProvider": {
                        "id": "8a5f1c9e-0000-0000-0000-000000000000",
                        "apiServerEndpoint": "abc123-abcd1234.k8s.us-east-04a.coreweave.com",
                    },
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


def _cluster_provider_config() -> fnv1.Resource:
    """The CoreWeave ClusterProviderConfig named default, reading the token from crossplane-system/coreweave-creds."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "coreweave.m.upbound.io/v1beta1",
                "kind": "ClusterProviderConfig",
                "metadata": {"name": "default"},
                "spec": {
                    "credentials": {
                        "source": "Secret",
                        "secretRef": {
                            "namespace": "crossplane-system",
                            "name": "coreweave-creds",
                            "key": "credentials",
                        },
                    },
                },
            }
        ),
    )


def _credentials_secret(*, credentials: str) -> fnv1.Resource:
    """The CoreWeave credentials Secret, with the given base64 data under its credentials key."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "coreweave-creds", "namespace": "crossplane-system"},
                "data": {"credentials": credentials},
            }
        ),
    )


def _kubeconfig_secret() -> fnv1.Resource:
    """The composed kubeconfig Secret, rendered from the observed endpoint and token, which is always ready."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": "test-cluster-kubeconfig-55b57",
                    "namespace": "modelplane-system",
                },
                "type": "Opaque",
                "stringData": {"kubeconfig": _KUBECONFIG},
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _observed_kubeconfig_secret() -> fnv1.Resource:
    """The kubeconfig Secret as observed, its data base64 encoded by the API server."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": "test-cluster-kubeconfig-55b57",
                    "namespace": "modelplane-system",
                },
                "type": "Opaque",
                "data": {"kubeconfig": "eyJraW5kIjogIkNvbmZpZyJ9"},
            }
        ),
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


def _node_pool(
    *, name: str, management_policies: list[str] | None, cel_query: str, spec: dict[str, Any], ready: fnv1.Ready
) -> fnv1.Resource:
    """A composed NodePool Object, with the given NodePool name, readiness query and spec, and management policies unless they're None."""
    object_spec: dict[str, Any] = {
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": "test-cluster-kubeconfig-55b57",
        },
        "readiness": {
            "policy": "DeriveFromCelQuery",
            "celQuery": cel_query,
        },
        "forProvider": {
            "manifest": {
                "apiVersion": "compute.coreweave.com/v1alpha1",
                "kind": "NodePool",
                "metadata": {"name": name},
                "spec": spec,
            },
        },
    }
    if management_policies is not None:
        object_spec["managementPolicies"] = management_policies
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": object_spec,
            }
        ),
        ready=ready,
    )


def _node_pool_settings(*, name: str, spec: dict[str, Any]) -> fnv1.Resource:
    """The composed Object keeping an autoscaled pool's NodePool current, with the given NodePool name and spec."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "managementPolicies": ["Observe", "Update"],
                    "providerConfigRef": {
                        "kind": "ProviderConfig",
                        "name": "test-cluster-kubeconfig-55b57",
                    },
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "compute.coreweave.com/v1alpha1",
                            "kind": "NodePool",
                            "metadata": {"name": name},
                            "spec": spec,
                        },
                    },
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _observed_node_pool(*, name: str, cel_query: str, spec: dict[str, Any]) -> fnv1.Resource:
    """An observed, Ready NodePool Object, with the given NodePool name, readiness query and spec."""
    manifest = {
        "apiVersion": "compute.coreweave.com/v1alpha1",
        "kind": "NodePool",
        "metadata": {"name": name},
        "spec": spec,
    }
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "providerConfigRef": {
                        "kind": "ProviderConfig",
                        "name": "test-cluster-kubeconfig-55b57",
                    },
                    "readiness": {
                        "policy": "DeriveFromCelQuery",
                        "celQuery": cel_query,
                    },
                    "forProvider": {"manifest": manifest},
                },
                "status": {
                    "atProvider": {"manifest": manifest},
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
    )


def _system_node_pool(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed NodePool Object for the system pool every cluster gets."""
    return _node_pool(
        name="system",
        management_policies=None,
        cel_query="has(object.status) && has(object.status.currentNodes) && object.status.currentNodes >= 1",
        spec={
            "instanceType": "turin-gp",
            "targetNodes": 1,
            "autoscaling": False,
            "nodeLabels": {"modelplane.ai/pool": "system"},
        },
        ready=ready,
    )


def _usage_vpc() -> fnv1.Resource:
    """The composed Usage holding the VPC until the CKS cluster is gone."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "of": {
                        "apiVersion": "networking.coreweave.m.upbound.io/v1beta1",
                        "kind": "VPC",
                        "resourceSelector": {"matchControllerRef": True},
                    },
                    "by": {
                        "apiVersion": "cks.coreweave.m.upbound.io/v1beta1",
                        "kind": "Cluster",
                        "resourceSelector": {"matchControllerRef": True},
                    },
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _usage_kubeconfig() -> fnv1.Resource:
    """The composed Usage holding the kubeconfig Secret until the provider-kubernetes ProviderConfig is gone."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "of": {
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "resourceRef": {"name": "test-cluster-kubeconfig-55b57"},
                    },
                    "by": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "ProviderConfig",
                        "resourceRef": {"name": "test-cluster-kubeconfig-55b57"},
                    },
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _usage_cluster() -> fnv1.Resource:
    """The composed Usage holding the CKS cluster until the provider-kubernetes ProviderConfig is gone."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "of": {
                        "apiVersion": "cks.coreweave.m.upbound.io/v1beta1",
                        "kind": "Cluster",
                        "resourceSelector": {"matchControllerRef": True},
                    },
                    "by": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "ProviderConfig",
                        "resourceRef": {"name": "test-cluster-kubeconfig-55b57"},
                    },
                    "replayDeletion": True,
                },
            }
        ),
        ready=fnv1.READY_TRUE,
    )


def _storage_class_observer() -> fnv1.Resource:
    """The composed Object observing CKS's shared-vast StorageClass, which is always ready."""
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
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "storage.k8s.io/v1",
                            "kind": "StorageClass",
                            "metadata": {"name": "shared-vast"},
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
    Case(
        name="FirstPass",
        reason="With nothing observed or fetched, a CoreWeaveCluster composes the VPC and cluster and requires its ClusterProviderConfig.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    ready=False,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h100",
                            role="GPU",
                            instanceType="gd-8xh100ib-i128",
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                        ),
                    ],
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(ready=fnv1.READY_FALSE),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED),
                    "cluster": _cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "usage-vpc-by-cluster": _usage_vpc(),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Waiting for CoreWeave ClusterProviderConfig default",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "coreweave-provider-config": fnv1.ResourceSelector(
                        api_version="coreweave.m.upbound.io/v1beta1",
                        kind="ClusterProviderConfig",
                        match_name="default",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="CustomCredentials",
        reason=(
            "A CoreWeaveCluster naming a namespaced ProviderConfig uses it for the VPC and cluster, and requires its "
            "credentials Secret from the ProviderConfig's namespace."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=v1alpha1.Credentials(type="ProviderConfig", name="team"),
                    ready=False,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h100",
                            role="GPU",
                            instanceType="gd-8xh100ib-i128",
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                        ),
                    ],
                ),
            ),
            required_resources={
                "coreweave-provider-config": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "coreweave.m.upbound.io/v1beta1",
                                    "kind": "ProviderConfig",
                                    "metadata": {"name": "team", "namespace": "modelplane-system"},
                                    "spec": {
                                        "credentials": {
                                            "source": "Secret",
                                            "secretRef": {"name": "team-creds", "key": "credentials"},
                                        },
                                    },
                                }
                            ),
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(ready=fnv1.READY_FALSE),
                resources={
                    "vpc": _vpc(cred_kind="ProviderConfig", cred_name="team", ready=fnv1.READY_UNSPECIFIED),
                    "cluster": _cluster(cred_kind="ProviderConfig", cred_name="team", ready=fnv1.READY_UNSPECIFIED),
                    "usage-vpc-by-cluster": _usage_vpc(),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Waiting for CoreWeave credentials Secret modelplane-system/team-creds",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "coreweave-provider-config": fnv1.ResourceSelector(
                        api_version="coreweave.m.upbound.io/v1beta1",
                        kind="ProviderConfig",
                        match_name="team",
                        namespace="modelplane-system",
                    ),
                    "coreweave-credentials": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="team-creds",
                        namespace="modelplane-system",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="ClusterReady",
        reason=(
            "With the cluster observed Ready and the token fetched, a CoreWeaveCluster composes the kubeconfig "
            "Secret, holding back the node pools until the Secret exists."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    ready=False,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h100",
                            role="GPU",
                            instanceType="gd-8xh100ib-i128",
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(ready=True),
                },
            ),
            required_resources={
                "coreweave-provider-config": fnv1.Resources(items=[_cluster_provider_config()]),
                "coreweave-credentials": fnv1.Resources(
                    items=[_credentials_secret(credentials="eyJ0b2tlbiI6ICJDVy1TRUNSRVQtdGVzdCJ9")],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(ready=fnv1.READY_FALSE),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "usage-vpc-by-cluster": _usage_vpc(),
                    "kubeconfig": _kubeconfig_secret(),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "coreweave-provider-config": fnv1.ResourceSelector(
                        api_version="coreweave.m.upbound.io/v1beta1",
                        kind="ClusterProviderConfig",
                        match_name="default",
                    ),
                    "coreweave-credentials": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="coreweave-creds",
                        namespace="crossplane-system",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="KubeconfigObserved",
        reason=(
            "With the cluster Ready and its kubeconfig Secret observed, a CoreWeaveCluster composes the ProviderConfig, "
            "a fixed-size GPU NodePool beside the system NodePool, and the Usages that order their teardown."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    ready=False,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h100",
                            role="GPU",
                            instanceType="gd-8xh100ib-i128",
                            nodeCount=2,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(ready=True),
                    "kubeconfig": _observed_kubeconfig_secret(),
                },
            ),
            required_resources={
                "coreweave-provider-config": fnv1.Resources(items=[_cluster_provider_config()]),
                "coreweave-credentials": fnv1.Resources(
                    items=[_credentials_secret(credentials="eyJ0b2tlbiI6ICJDVy1TRUNSRVQtdGVzdCJ9")],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(ready=fnv1.READY_UNSPECIFIED),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "usage-vpc-by-cluster": _usage_vpc(),
                    "kubeconfig": _kubeconfig_secret(),
                    "provider-config-kubernetes": _provider_config(),
                    "node-pool-system": _system_node_pool(ready=fnv1.READY_UNSPECIFIED),
                    "node-pool-gpu-h100": _node_pool(
                        name="gpu-h100",
                        management_policies=None,
                        cel_query=(
                            "has(object.status) && has(object.status.conditions)"
                            " && object.status.conditions.exists(c, c.type == 'Validated' && c.status == 'True')"
                        ),
                        spec={
                            "instanceType": "gd-8xh100ib-i128",
                            "targetNodes": 2,
                            "autoscaling": False,
                            "nodeLabels": {
                                "modelplane.ai/pool": "gpu-h100",
                                "modelplane.ai/gpu": "nvidia-h100",
                                "nvidia.com/gpu.deploy.device-plugin": "false",
                            },
                            "nodeTaints": [{"key": "nvidia.com/gpu", "value": "true", "effect": "NoSchedule"}],
                        },
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "usage-kubeconfig-by-provider-config": _usage_kubeconfig(),
                    "usage-cluster-by-provider-config": _usage_cluster(),
                    "storage-class-cks": _storage_class_observer(),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "coreweave-provider-config": fnv1.ResourceSelector(
                        api_version="coreweave.m.upbound.io/v1beta1",
                        kind="ClusterProviderConfig",
                        match_name="default",
                    ),
                    "coreweave-credentials": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="coreweave-creds",
                        namespace="crossplane-system",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="ReadySwitchesSystemPool",
        reason=(
            "Once a CoreWeaveCluster is Ready, its system pool's NodePool Object is Ready once CKS validates the pool, "
            "like any other pool, so replacing the pool's one node doesn't make the XR un-Ready."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    ready=True,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h100",
                            role="GPU",
                            instanceType="gd-8xh100ib-i128",
                            nodeCount=2,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(ready=True),
                    "kubeconfig": _observed_kubeconfig_secret(),
                    "node-pool-system": _observed_node_pool(
                        name="system",
                        cel_query="has(object.status) && has(object.status.currentNodes) && object.status.currentNodes >= 1",
                        spec={"instanceType": "turin-gp", "targetNodes": 1},
                    ),
                    "node-pool-gpu-h100": _observed_node_pool(
                        name="gpu-h100",
                        cel_query=(
                            "has(object.status) && has(object.status.conditions)"
                            " && object.status.conditions.exists(c, c.type == 'Validated' && c.status == 'True')"
                        ),
                        spec={"instanceType": "gd-8xh100ib-i128", "targetNodes": 2},
                    ),
                },
            ),
            required_resources={
                "coreweave-provider-config": fnv1.Resources(items=[_cluster_provider_config()]),
                "coreweave-credentials": fnv1.Resources(
                    items=[_credentials_secret(credentials="eyJ0b2tlbiI6ICJDVy1TRUNSRVQtdGVzdCJ9")],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(ready=fnv1.READY_UNSPECIFIED),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "usage-vpc-by-cluster": _usage_vpc(),
                    "kubeconfig": _kubeconfig_secret(),
                    "provider-config-kubernetes": _provider_config(),
                    "node-pool-system": _node_pool(
                        name="system",
                        management_policies=None,
                        cel_query=(
                            "has(object.status) && has(object.status.conditions)"
                            " && object.status.conditions.exists(c, c.type == 'Validated' && c.status == 'True')"
                        ),
                        spec={
                            "instanceType": "turin-gp",
                            "targetNodes": 1,
                            "autoscaling": False,
                            "nodeLabels": {"modelplane.ai/pool": "system"},
                        },
                        ready=fnv1.READY_TRUE,
                    ),
                    "node-pool-gpu-h100": _node_pool(
                        name="gpu-h100",
                        management_policies=None,
                        cel_query=(
                            "has(object.status) && has(object.status.conditions)"
                            " && object.status.conditions.exists(c, c.type == 'Validated' && c.status == 'True')"
                        ),
                        spec={
                            "instanceType": "gd-8xh100ib-i128",
                            "targetNodes": 2,
                            "autoscaling": False,
                            "nodeLabels": {
                                "modelplane.ai/pool": "gpu-h100",
                                "modelplane.ai/gpu": "nvidia-h100",
                                "nvidia.com/gpu.deploy.device-plugin": "false",
                            },
                            "nodeTaints": [{"key": "nvidia.com/gpu", "value": "true", "effect": "NoSchedule"}],
                        },
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-kubeconfig-by-provider-config": _usage_kubeconfig(),
                    "usage-cluster-by-provider-config": _usage_cluster(),
                    "storage-class-cks": _storage_class_observer(),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "coreweave-provider-config": fnv1.ResourceSelector(
                        api_version="coreweave.m.upbound.io/v1beta1",
                        kind="ClusterProviderConfig",
                        match_name="default",
                    ),
                    "coreweave-credentials": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="coreweave-creds",
                        namespace="crossplane-system",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="SystemPoolLatched",
        reason=(
            "A system pool whose NodePool Object is already Ready once validated stays that way while the "
            "CoreWeaveCluster is un-Ready, here because a new pool is waiting for CKS to validate it."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    ready=False,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h100",
                            role="GPU",
                            instanceType="gd-8xh100ib-i128",
                            nodeCount=2,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(ready=True),
                    "kubeconfig": _observed_kubeconfig_secret(),
                    "node-pool-system": _observed_node_pool(
                        name="system",
                        cel_query=(
                            "has(object.status) && has(object.status.conditions)"
                            " && object.status.conditions.exists(c, c.type == 'Validated' && c.status == 'True')"
                        ),
                        spec={"instanceType": "turin-gp", "targetNodes": 1},
                    ),
                },
            ),
            required_resources={
                "coreweave-provider-config": fnv1.Resources(items=[_cluster_provider_config()]),
                "coreweave-credentials": fnv1.Resources(
                    items=[_credentials_secret(credentials="eyJ0b2tlbiI6ICJDVy1TRUNSRVQtdGVzdCJ9")],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(ready=fnv1.READY_UNSPECIFIED),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "usage-vpc-by-cluster": _usage_vpc(),
                    "kubeconfig": _kubeconfig_secret(),
                    "provider-config-kubernetes": _provider_config(),
                    "node-pool-system": _node_pool(
                        name="system",
                        management_policies=None,
                        cel_query=(
                            "has(object.status) && has(object.status.conditions)"
                            " && object.status.conditions.exists(c, c.type == 'Validated' && c.status == 'True')"
                        ),
                        spec={
                            "instanceType": "turin-gp",
                            "targetNodes": 1,
                            "autoscaling": False,
                            "nodeLabels": {"modelplane.ai/pool": "system"},
                        },
                        ready=fnv1.READY_TRUE,
                    ),
                    "node-pool-gpu-h100": _node_pool(
                        name="gpu-h100",
                        management_policies=None,
                        cel_query=(
                            "has(object.status) && has(object.status.conditions)"
                            " && object.status.conditions.exists(c, c.type == 'Validated' && c.status == 'True')"
                        ),
                        spec={
                            "instanceType": "gd-8xh100ib-i128",
                            "targetNodes": 2,
                            "autoscaling": False,
                            "nodeLabels": {
                                "modelplane.ai/pool": "gpu-h100",
                                "modelplane.ai/gpu": "nvidia-h100",
                                "nvidia.com/gpu.deploy.device-plugin": "false",
                            },
                            "nodeTaints": [{"key": "nvidia.com/gpu", "value": "true", "effect": "NoSchedule"}],
                        },
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "usage-kubeconfig-by-provider-config": _usage_kubeconfig(),
                    "usage-cluster-by-provider-config": _usage_cluster(),
                    "storage-class-cks": _storage_class_observer(),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "coreweave-provider-config": fnv1.ResourceSelector(
                        api_version="coreweave.m.upbound.io/v1beta1",
                        kind="ClusterProviderConfig",
                        match_name="default",
                    ),
                    "coreweave-credentials": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="coreweave-creds",
                        namespace="crossplane-system",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="CacheStorageClass",
        reason=(
            "Once CKS's shared-vast StorageClass is observed, a CoreWeaveCluster composes modelplane-rwx-fs, a copy "
            "of it that deletes a volume when its claim is deleted."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    ready=False,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h100",
                            role="GPU",
                            instanceType="gd-8xh100ib-i128",
                            nodeCount=2,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(ready=True),
                    "kubeconfig": _observed_kubeconfig_secret(),
                    "storage-class-cks": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "spec": {
                                    "managementPolicies": ["Observe"],
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "storage.k8s.io/v1",
                                            "kind": "StorageClass",
                                            "metadata": {"name": "shared-vast"},
                                        },
                                    },
                                },
                                # The class as CKS creates it, with its tenant
                                # and zone specific values replaced.
                                "status": {
                                    "atProvider": {
                                        "manifest": {
                                            "apiVersion": "storage.k8s.io/v1",
                                            "kind": "StorageClass",
                                            "metadata": {
                                                "name": "shared-vast",
                                                "annotations": {"storageclass.kubernetes.io/is-default-class": "true"},
                                            },
                                            "provisioner": "csi.vastdata.com",
                                            "parameters": {
                                                "root_export": "/k8s",
                                                "view_policy": "nosquash-tenant",
                                                "vip_pool_fqdn": "all-tenants.storage-backend01.us-east-04.int.coreweave.cloud",
                                            },
                                            "mountOptions": ["vers=3", "nconnect=32"],
                                            "allowVolumeExpansion": True,
                                            "reclaimPolicy": "Retain",
                                            "volumeBindingMode": "Immediate",
                                        },
                                    },
                                },
                            }
                        ),
                    ),
                },
            ),
            required_resources={
                "coreweave-provider-config": fnv1.Resources(items=[_cluster_provider_config()]),
                "coreweave-credentials": fnv1.Resources(
                    items=[_credentials_secret(credentials="eyJ0b2tlbiI6ICJDVy1TRUNSRVQtdGVzdCJ9")],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(ready=fnv1.READY_UNSPECIFIED),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "usage-vpc-by-cluster": _usage_vpc(),
                    "kubeconfig": _kubeconfig_secret(),
                    "provider-config-kubernetes": _provider_config(),
                    "node-pool-system": _system_node_pool(ready=fnv1.READY_UNSPECIFIED),
                    "node-pool-gpu-h100": _node_pool(
                        name="gpu-h100",
                        management_policies=None,
                        cel_query=(
                            "has(object.status) && has(object.status.conditions)"
                            " && object.status.conditions.exists(c, c.type == 'Validated' && c.status == 'True')"
                        ),
                        spec={
                            "instanceType": "gd-8xh100ib-i128",
                            "targetNodes": 2,
                            "autoscaling": False,
                            "nodeLabels": {
                                "modelplane.ai/pool": "gpu-h100",
                                "modelplane.ai/gpu": "nvidia-h100",
                                "nvidia.com/gpu.deploy.device-plugin": "false",
                            },
                            "nodeTaints": [{"key": "nvidia.com/gpu", "value": "true", "effect": "NoSchedule"}],
                        },
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "usage-kubeconfig-by-provider-config": _usage_kubeconfig(),
                    "usage-cluster-by-provider-config": _usage_cluster(),
                    "storage-class-cks": _storage_class_observer(),
                    "storage-class-rwx-fs": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"namespace": "modelplane-system"},
                                "spec": {
                                    "providerConfigRef": {
                                        "kind": "ProviderConfig",
                                        "name": "test-cluster-kubeconfig-55b57",
                                    },
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "storage.k8s.io/v1",
                                            "kind": "StorageClass",
                                            "metadata": {"name": "modelplane-rwx-fs"},
                                            "provisioner": "csi.vastdata.com",
                                            "parameters": {
                                                "root_export": "/k8s",
                                                "view_policy": "nosquash-tenant",
                                                "vip_pool_fqdn": "all-tenants.storage-backend01.us-east-04.int.coreweave.cloud",
                                            },
                                            "mountOptions": ["vers=3", "nconnect=32"],
                                            "allowVolumeExpansion": True,
                                            "volumeBindingMode": "Immediate",
                                            "reclaimPolicy": "Delete",
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "coreweave-provider-config": fnv1.ResourceSelector(
                        api_version="coreweave.m.upbound.io/v1beta1",
                        kind="ClusterProviderConfig",
                        match_name="default",
                    ),
                    "coreweave-credentials": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="coreweave-creds",
                        namespace="crossplane-system",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="CPUPool",
        reason=(
            "A CPU-role pool composes a NodePool with the modelplane.ai/pool and modelplane.ai/cpu labels and the "
            "CPU taint, and no GPU or device-plugin labels."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    ready=False,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="cpu-pool",
                            role="CPU",
                            instanceType="turin-gp",
                            nodeCount=1,
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(ready=True),
                    "kubeconfig": _observed_kubeconfig_secret(),
                },
            ),
            required_resources={
                "coreweave-provider-config": fnv1.Resources(items=[_cluster_provider_config()]),
                "coreweave-credentials": fnv1.Resources(
                    items=[_credentials_secret(credentials="eyJ0b2tlbiI6ICJDVy1TRUNSRVQtdGVzdCJ9")],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(ready=fnv1.READY_UNSPECIFIED),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "usage-vpc-by-cluster": _usage_vpc(),
                    "kubeconfig": _kubeconfig_secret(),
                    "provider-config-kubernetes": _provider_config(),
                    "node-pool-system": _system_node_pool(ready=fnv1.READY_UNSPECIFIED),
                    "node-pool-cpu-pool": _node_pool(
                        name="cpu-pool",
                        management_policies=None,
                        cel_query=(
                            "has(object.status) && has(object.status.conditions)"
                            " && object.status.conditions.exists(c, c.type == 'Validated' && c.status == 'True')"
                        ),
                        spec={
                            "instanceType": "turin-gp",
                            "targetNodes": 1,
                            "autoscaling": False,
                            "nodeLabels": {"modelplane.ai/pool": "cpu-pool", "modelplane.ai/cpu": "true"},
                            "nodeTaints": [{"key": "modelplane.ai/cpu", "value": "true", "effect": "NoSchedule"}],
                        },
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "usage-kubeconfig-by-provider-config": _usage_kubeconfig(),
                    "usage-cluster-by-provider-config": _usage_cluster(),
                    "storage-class-cks": _storage_class_observer(),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "coreweave-provider-config": fnv1.ResourceSelector(
                        api_version="coreweave.m.upbound.io/v1beta1",
                        kind="ClusterProviderConfig",
                        match_name="default",
                    ),
                    "coreweave-credentials": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="coreweave-creds",
                        namespace="crossplane-system",
                    ),
                },
            ),
        ),
    ),
    # The autoscaler has moved the observed NodePool's targetNodes to 3. The
    # function still composes its seed of one node, which the first Object's
    # management policies never apply to an existing NodePool.
    Case(
        name="AutoscaledPoolReady",
        reason=(
            "An autoscaled pool that asks for no nodes is seeded with one and keeps one, its NodePool Object can't update the "
            "targetNodes CKS's autoscaler owns, a second Object without targetNodes keeps the rest of the pool "
            "current, and both are marked ready with the rest of the cluster."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    ready=False,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h100",
                            role="GPU",
                            instanceType="gd-8xh100ib-i128",
                            nodeCount=0,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                        ),
                    ],
                ),
                resources={
                    "vpc": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "networking.coreweave.m.upbound.io/v1beta1",
                                "kind": "VPC",
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
                    "cluster": _observed_cluster(ready=True),
                    "kubeconfig": _observed_kubeconfig_secret(),
                    "node-pool-system": _observed_node_pool(
                        name="system",
                        cel_query="has(object.status) && has(object.status.currentNodes) && object.status.currentNodes >= 1",
                        spec={"instanceType": "turin-gp", "targetNodes": 1},
                    ),
                    "node-pool-gpu-h100": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "spec": {
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "compute.coreweave.com/v1alpha1",
                                            "kind": "NodePool",
                                            "metadata": {"name": "gpu-h100"},
                                            "spec": {
                                                "instanceType": "gd-8xh100ib-i128",
                                                "autoscaling": True,
                                                "minNodes": 1,
                                                "maxNodes": 4,
                                                "targetNodes": 1,
                                            },
                                        },
                                    },
                                },
                                "status": {
                                    "atProvider": {
                                        "manifest": {
                                            "apiVersion": "compute.coreweave.com/v1alpha1",
                                            "kind": "NodePool",
                                            "metadata": {"name": "gpu-h100"},
                                            "spec": {
                                                "instanceType": "gd-8xh100ib-i128",
                                                "autoscaling": True,
                                                "minNodes": 1,
                                                "maxNodes": 4,
                                                "targetNodes": 3,
                                            },
                                        },
                                    },
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
                "coreweave-provider-config": fnv1.Resources(items=[_cluster_provider_config()]),
                "coreweave-credentials": fnv1.Resources(
                    items=[_credentials_secret(credentials="eyJ0b2tlbiI6ICJDVy1TRUNSRVQtdGVzdCJ9")],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(ready=fnv1.READY_UNSPECIFIED),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "usage-vpc-by-cluster": _usage_vpc(),
                    "kubeconfig": _kubeconfig_secret(),
                    "provider-config-kubernetes": _provider_config(),
                    "node-pool-system": _system_node_pool(ready=fnv1.READY_TRUE),
                    "node-pool-gpu-h100": _node_pool(
                        name="gpu-h100",
                        management_policies=["Observe", "Create", "Delete"],
                        cel_query=(
                            "has(object.status) && has(object.status.conditions)"
                            " && object.status.conditions.exists(c, c.type == 'Validated' && c.status == 'True')"
                        ),
                        spec={
                            "instanceType": "gd-8xh100ib-i128",
                            "targetNodes": 1,
                            "autoscaling": True,
                            "minNodes": 1,
                            "maxNodes": 4,
                            "nodeLabels": {
                                "modelplane.ai/pool": "gpu-h100",
                                "modelplane.ai/gpu": "nvidia-h100",
                                "nvidia.com/gpu.deploy.device-plugin": "false",
                            },
                            "nodeTaints": [{"key": "nvidia.com/gpu", "value": "true", "effect": "NoSchedule"}],
                        },
                        ready=fnv1.READY_TRUE,
                    ),
                    "node-pool-gpu-h100.settings": _node_pool_settings(
                        name="gpu-h100",
                        spec={
                            "instanceType": "gd-8xh100ib-i128",
                            "autoscaling": True,
                            "minNodes": 1,
                            "maxNodes": 4,
                            "nodeLabels": {
                                "modelplane.ai/pool": "gpu-h100",
                                "modelplane.ai/gpu": "nvidia-h100",
                                "nvidia.com/gpu.deploy.device-plugin": "false",
                            },
                            "nodeTaints": [{"key": "nvidia.com/gpu", "value": "true", "effect": "NoSchedule"}],
                        },
                    ),
                    "usage-kubeconfig-by-provider-config": _usage_kubeconfig(),
                    "usage-cluster-by-provider-config": _usage_cluster(),
                    "storage-class-cks": _storage_class_observer(),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "coreweave-provider-config": fnv1.ResourceSelector(
                        api_version="coreweave.m.upbound.io/v1beta1",
                        kind="ClusterProviderConfig",
                        match_name="default",
                    ),
                    "coreweave-credentials": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="coreweave-creds",
                        namespace="crossplane-system",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="ClusterReadyRegressed",
        reason=(
            "With the cluster's Ready condition regressed to False but its ProviderConfig observed, a "
            "CoreWeaveCluster keeps the NodePools composed, seeding the new autoscaled pool from nodeCount with the "
            "minNodeCount it asks for."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    ready=False,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h100",
                            role="GPU",
                            instanceType="gd-8xh100ib-i128",
                            nodeCount=2,
                            minNodeCount=1,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(ready=False),
                    "kubeconfig": _observed_kubeconfig_secret(),
                    "provider-config-kubernetes": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "ProviderConfig",
                                "metadata": {
                                    "name": "test-cluster-kubeconfig-55b57",
                                    "namespace": "modelplane-system",
                                },
                            }
                        ),
                    ),
                },
            ),
            required_resources={
                "coreweave-provider-config": fnv1.Resources(items=[_cluster_provider_config()]),
                "coreweave-credentials": fnv1.Resources(
                    items=[_credentials_secret(credentials="eyJ0b2tlbiI6ICJDVy1TRUNSRVQtdGVzdCJ9")],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(ready=fnv1.READY_UNSPECIFIED),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED),
                    "cluster": _cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "usage-vpc-by-cluster": _usage_vpc(),
                    "kubeconfig": _kubeconfig_secret(),
                    "provider-config-kubernetes": _provider_config(),
                    "node-pool-system": _system_node_pool(ready=fnv1.READY_UNSPECIFIED),
                    "node-pool-gpu-h100": _node_pool(
                        name="gpu-h100",
                        management_policies=["Observe", "Create", "Delete"],
                        cel_query=(
                            "has(object.status) && has(object.status.conditions)"
                            " && object.status.conditions.exists(c, c.type == 'Validated' && c.status == 'True')"
                        ),
                        spec={
                            "instanceType": "gd-8xh100ib-i128",
                            "targetNodes": 2,
                            "autoscaling": True,
                            "minNodes": 1,
                            "maxNodes": 4,
                            "nodeLabels": {
                                "modelplane.ai/pool": "gpu-h100",
                                "modelplane.ai/gpu": "nvidia-h100",
                                "nvidia.com/gpu.deploy.device-plugin": "false",
                            },
                            "nodeTaints": [{"key": "nvidia.com/gpu", "value": "true", "effect": "NoSchedule"}],
                        },
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "node-pool-gpu-h100.settings": _node_pool_settings(
                        name="gpu-h100",
                        spec={
                            "instanceType": "gd-8xh100ib-i128",
                            "autoscaling": True,
                            "minNodes": 1,
                            "maxNodes": 4,
                            "nodeLabels": {
                                "modelplane.ai/pool": "gpu-h100",
                                "modelplane.ai/gpu": "nvidia-h100",
                                "nvidia.com/gpu.deploy.device-plugin": "false",
                            },
                            "nodeTaints": [{"key": "nvidia.com/gpu", "value": "true", "effect": "NoSchedule"}],
                        },
                    ),
                    "usage-kubeconfig-by-provider-config": _usage_kubeconfig(),
                    "usage-cluster-by-provider-config": _usage_cluster(),
                    "storage-class-cks": _storage_class_observer(),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "coreweave-provider-config": fnv1.ResourceSelector(
                        api_version="coreweave.m.upbound.io/v1beta1",
                        kind="ClusterProviderConfig",
                        match_name="default",
                    ),
                    "coreweave-credentials": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="coreweave-creds",
                        namespace="crossplane-system",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="CredentialsMissing",
        reason=(
            "With the ClusterProviderConfig gone, a CoreWeaveCluster keeps the observed kubeconfig Secret's data "
            "rather than deleting the Secret."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    ready=False,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h100",
                            role="GPU",
                            instanceType="gd-8xh100ib-i128",
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(ready=False),
                    "kubeconfig": _observed_kubeconfig_secret(),
                },
            ),
            required_resources={
                "coreweave-provider-config": fnv1.Resources(),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(ready=fnv1.READY_FALSE),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED),
                    "cluster": _cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "usage-vpc-by-cluster": _usage_vpc(),
                    "kubeconfig": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "v1",
                                "kind": "Secret",
                                "metadata": {
                                    "name": "test-cluster-kubeconfig-55b57",
                                    "namespace": "modelplane-system",
                                },
                                "type": "Opaque",
                                "data": {"kubeconfig": "eyJraW5kIjogIkNvbmZpZyJ9"},
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Waiting for CoreWeave ClusterProviderConfig default",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "coreweave-provider-config": fnv1.ResourceSelector(
                        api_version="coreweave.m.upbound.io/v1beta1",
                        kind="ClusterProviderConfig",
                        match_name="default",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="CredentialsNotJSON",
        reason="A credentials Secret that isn't the JSON provider-coreweave reads warns, and no kubeconfig is composed.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    ready=False,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h100",
                            role="GPU",
                            instanceType="gd-8xh100ib-i128",
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(ready=True),
                },
            ),
            required_resources={
                "coreweave-provider-config": fnv1.Resources(items=[_cluster_provider_config()]),
                "coreweave-credentials": fnv1.Resources(items=[_credentials_secret(credentials="bm90IGpzb24=")]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(ready=fnv1.READY_FALSE),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "usage-vpc-by-cluster": _usage_vpc(),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message=(
                        "CoreWeave credentials Secret crossplane-system/coreweave-creds key credentials "
                        "isn't a JSON object with a token field"
                    ),
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "coreweave-provider-config": fnv1.ResourceSelector(
                        api_version="coreweave.m.upbound.io/v1beta1",
                        kind="ClusterProviderConfig",
                        match_name="default",
                    ),
                    "coreweave-credentials": fnv1.ResourceSelector(
                        api_version="v1",
                        kind="Secret",
                        match_name="coreweave-creds",
                        namespace="crossplane-system",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="CredentialsNotFromSecret",
        reason="A ClusterProviderConfig that doesn't read its credentials from a Secret warns, and no kubeconfig is composed.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    ready=False,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h100",
                            role="GPU",
                            instanceType="gd-8xh100ib-i128",
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_cluster(ready=True),
                },
            ),
            required_resources={
                "coreweave-provider-config": fnv1.Resources(
                    items=[
                        fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "coreweave.m.upbound.io/v1beta1",
                                    "kind": "ClusterProviderConfig",
                                    "metadata": {"name": "default"},
                                    "spec": {
                                        "credentials": {
                                            "source": "Environment",
                                            "env": {"name": "COREWEAVE_TOKEN"},
                                        },
                                    },
                                }
                            ),
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(ready=fnv1.READY_FALSE),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "usage-vpc-by-cluster": _usage_vpc(),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message=(
                        "CoreWeave ClusterProviderConfig default doesn't source credentials from a Secret; "
                        "can't render a kubeconfig without its API access token"
                    ),
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "coreweave-provider-config": fnv1.ResourceSelector(
                        api_version="coreweave.m.upbound.io/v1beta1",
                        kind="ClusterProviderConfig",
                        match_name="default",
                    ),
                },
            ),
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction composes CKS cluster infrastructure."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
