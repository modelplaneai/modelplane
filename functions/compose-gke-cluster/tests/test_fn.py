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

"""Tests for the compose-gke-cluster function."""

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
from models.ai.modelplane.infrastructure.gkecluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-gke-cluster."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _xr(*, credentials: v1alpha1.Credentials | None) -> fnv1.Resource:
    """The observed GKECluster XR, with the given credentials."""
    xr = v1alpha1.GKECluster(
        metadata=metav1.ObjectMeta(
            name="test-cluster",
            namespace="modelplane-system",
        ),
        spec=v1alpha1.Spec(
            region="us-central1",
            credentials=credentials,
            nodePools=[
                v1alpha1.NodePool(
                    name="gpu-pool",
                    role="GPU",
                    machineType="a2-highgpu-8g",
                    gpu=v1alpha1.Gpu(
                        acceleratorType="nvidia-tesla-a100",
                        acceleratorCount=8,
                    ),
                ),
            ],
        ),
    )
    return fnv1.Resource(resource=resource.dict_to_struct(xr.model_dump(exclude_none=True, mode="json", by_alias=True)))


def _desired_xr() -> fnv1.Resource:
    """The desired XR, publishing its connection Secrets and cache StorageClass."""
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
                        {
                            "type": "GoogleApplicationCredentials",
                            "name": "test-cluster-sa-key-3295c",
                            "key": "private_key",
                        },
                    ],
                    "cache": {"storageClassName": "modelplane-rwx"},
                },
            }
        ),
    )


def _gcp_provider_config(*, kind: str, name: str, namespace: str | None) -> fnv1.Resource:
    """The GCP provider config the function requires, in a namespace if given."""
    metadata = {"name": name}
    if namespace is not None:
        metadata["namespace"] = namespace
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "gcp.m.upbound.io/v1beta1",
                "kind": kind,
                "metadata": metadata,
                "spec": {
                    "projectID": "my-gcp-project",
                    "credentials": {
                        "source": "Secret",
                        "secretRef": {
                            "name": "gcp-credentials",
                            "namespace": "crossplane-system",
                            "key": "credentials",
                        },
                    },
                },
            }
        ),
    )


def _network(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed VPC Network."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "compute.gcp.m.upbound.io/v1beta1",
                "kind": "Network",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "autoCreateSubnetworks": False,
                    },
                },
            }
        ),
        ready=ready,
    )


def _projectservice_filestore(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
    """The composed ProjectService that enables the Filestore API."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "cloudplatform.gcp.m.upbound.io/v1beta1",
                "kind": "ProjectService",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "service": "file.googleapis.com",
                        "disableOnDestroy": False,
                    },
                },
            }
        ),
    )


def _subnet(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
    """The composed Subnetwork, with secondary ranges for pods and services."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "compute.gcp.m.upbound.io/v1beta1",
                "kind": "Subnetwork",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "region": "us-central1",
                        "networkSelector": {"matchControllerRef": True},
                        "ipCidrRange": "10.0.0.0/24",
                        "secondaryIpRange": [
                            {"rangeName": "pods", "ipCidrRange": "10.1.0.0/16"},
                            {"rangeName": "services", "ipCidrRange": "10.2.0.0/16"},
                        ],
                    },
                },
            }
        ),
    )


def _cluster(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
    """The composed GKE Cluster."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "container.gcp.m.upbound.io/v1beta1",
                "kind": "Cluster",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "location": "us-central1",
                        "deletionProtection": False,
                        "removeDefaultNodePool": True,
                        "initialNodeCount": 1,
                        "minMasterVersion": "1.35",
                        "networkSelector": {"matchControllerRef": True},
                        "subnetworkSelector": {"matchControllerRef": True},
                        "ipAllocationPolicy": {
                            "clusterSecondaryRangeName": "pods",
                            "servicesSecondaryRangeName": "services",
                        },
                        "releaseChannel": {"channel": "REGULAR"},
                        "workloadIdentityConfig": {
                            "workloadPool": "my-gcp-project.svc.id.goog",
                        },
                        "addonsConfig": {
                            "gcpFilestoreCsiDriverConfig": {"enabled": True},
                        },
                    },
                    "writeConnectionSecretToRef": {
                        "name": "test-cluster-kubeconfig-55b57",
                    },
                },
            }
        ),
    )


def _nodepool_system(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
    """The system NodePool the function adds to every cluster."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "container.gcp.m.upbound.io/v1beta1",
                "kind": "NodePool",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "location": "us-central1",
                        "clusterSelector": {"matchControllerRef": True},
                        "initialNodeCount": 1,
                        "autoscaling": {"minNodeCount": 1, "maxNodeCount": 2},
                        "nodeConfig": {
                            "machineType": "e2-standard-4",
                            "imageType": "COS_CONTAINERD",
                            "oauthScopes": [
                                "https://www.googleapis.com/auth/cloud-platform",
                            ],
                            "labels": {"modelplane.ai/pool": "system"},
                        },
                    },
                },
            }
        ),
    )


def _nodepool_gpu(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
    """The NodePool for the XR's gpu-pool."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "container.gcp.m.upbound.io/v1beta1",
                "kind": "NodePool",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "location": "us-central1",
                        "clusterSelector": {"matchControllerRef": True},
                        "initialNodeCount": 1,
                        "autoscaling": {"minNodeCount": 0, "maxNodeCount": 8},
                        "nodeConfig": {
                            "machineType": "a2-highgpu-8g",
                            "diskSizeGb": 100,
                            "imageType": "COS_CONTAINERD",
                            "oauthScopes": [
                                "https://www.googleapis.com/auth/cloud-platform",
                            ],
                            "guestAccelerator": [
                                {
                                    "type": "nvidia-tesla-a100",
                                    "count": 8,
                                    "gpuDriverInstallationConfig": {
                                        "gpuDriverVersion": "DEFAULT",
                                    },
                                },
                            ],
                            "labels": {
                                "modelplane.ai/gpu": "nvidia-tesla-a100",
                                "modelplane.ai/pool": "gpu-pool",
                                "cloud.google.com/gke-nvidia-gpu-dra-driver": "true",
                            },
                        },
                    },
                },
            }
        ),
    )


def _service_account(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed GCP ServiceAccount."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "cloudplatform.gcp.m.upbound.io/v1beta1",
                "kind": "ServiceAccount",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "displayName": "Crossplane GKECluster test-cluster",
                    },
                },
            }
        ),
        ready=ready,
    )


def _service_account_key(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
    """The composed ServiceAccountKey."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "cloudplatform.gcp.m.upbound.io/v1beta1",
                "kind": "ServiceAccountKey",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "serviceAccountIdSelector": {"matchControllerRef": True},
                    },
                    "writeConnectionSecretToRef": {
                        "name": "test-cluster-sa-key-3295c",
                    },
                },
            }
        ),
    )


def _provider_config(*, api_version: str) -> fnv1.Resource:
    """The provider-kubernetes or provider-helm ProviderConfig for the composed cluster, which is always ready."""
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
                        "type": "GoogleApplicationCredentials",
                        "source": "Secret",
                        "secretRef": {
                            "name": "test-cluster-sa-key-3295c",
                            "namespace": "modelplane-system",
                            "key": "private_key",
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
        name="first pass composes infra resources; IAM binding gated",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_xr(credentials=None)),
            required_resources={
                "gcp-provider-config": fnv1.Resources(
                    items=[_gcp_provider_config(kind="ClusterProviderConfig", name="default", namespace=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(),
                resources={
                    "network": _network(
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "projectservice-filestore": _projectservice_filestore(
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "subnet": _subnet(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "nodepool-system": _nodepool_system(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "nodepool-gpu-pool": _nodepool_gpu(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "service-account": _service_account(
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "service-account-key": _service_account_key(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gcp-provider-config": fnv1.ResourceSelector(
                        api_version="gcp.m.upbound.io/v1beta1",
                        kind="ClusterProviderConfig",
                        match_name="default",
                    ),
                },
            ),
        ),
    ),
    # The ProviderConfig resolved to nothing and no cluster is observed to
    # take the project from, so nothing can be composed. The XR is marked
    # not ready rather than left to aggregate to trivially ready.
    Case(
        name="a missing ProviderConfig composes nothing and isn't ready",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_xr(credentials=None)),
            required_resources={"gcp-provider-config": fnv1.Resources()},
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=fnv1.Resource(ready=fnv1.READY_FALSE)),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Waiting for GCP ClusterProviderConfig default",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gcp-provider-config": fnv1.ResourceSelector(
                        api_version="gcp.m.upbound.io/v1beta1",
                        kind="ClusterProviderConfig",
                        match_name="default",
                    ),
                },
            ),
        ),
    ),
    Case(
        name="second pass with observed SA email composes IAM binding and marks ready resources",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(credentials=None),
                resources={
                    "service-account": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "cloudplatform.gcp.m.upbound.io/v1beta1",
                                "kind": "ServiceAccount",
                                "spec": {"forProvider": {}},
                                "status": {
                                    "atProvider": {
                                        "email": "test-sa@my-gcp-project.iam.gserviceaccount.com",
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
                    "network": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "compute.gcp.m.upbound.io/v1beta1",
                                "kind": "Network",
                                "metadata": {
                                    # The external-name annotation carries the
                                    # provider-generated VPC name, which the
                                    # function pins the Filestore StorageClass to.
                                    "annotations": {"crossplane.io/external-name": "test-cluster-abc12"},
                                },
                                "spec": {
                                    "forProvider": {
                                        "autoCreateSubnetworks": False,
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
                "gcp-provider-config": fnv1.Resources(
                    items=[_gcp_provider_config(kind="ClusterProviderConfig", name="default", namespace=None)],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(),
                resources={
                    "network": _network(
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "projectservice-filestore": _projectservice_filestore(
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    # Composed once the observed Network's external name gives
                    # the VPC to pin Filestore to. A StorageClass has no Ready
                    # condition, hence SuccessfulCreate, and the Object omits
                    # Delete so it dies with the cluster rather than wedging
                    # teardown on the deleted kubeconfig Secret.
                    "storage-class-rwx": fnv1.Resource(
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
                                            "metadata": {"name": "modelplane-rwx"},
                                            "provisioner": "filestore.csi.storage.gke.io",
                                            "parameters": {
                                                "tier": "enterprise",
                                                "network": "test-cluster-abc12",
                                            },
                                            "volumeBindingMode": "Immediate",
                                            "allowVolumeExpansion": True,
                                        },
                                    },
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "subnet": _subnet(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "cluster": _cluster(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "nodepool-system": _nodepool_system(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "nodepool-gpu-pool": _nodepool_gpu(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "service-account": _service_account(
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "service-account-key": _service_account_key(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "iam-binding": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "cloudplatform.gcp.m.upbound.io/v1beta1",
                                "kind": "ProjectIAMMember",
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "forProvider": {
                                        "role": "roles/container.admin",
                                        "member": "serviceAccount:test-sa@my-gcp-project.iam.gserviceaccount.com",
                                        "project": "my-gcp-project",
                                    },
                                },
                            }
                        ),
                    ),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "gcp-provider-config": fnv1.ResourceSelector(
                        api_version="gcp.m.upbound.io/v1beta1",
                        kind="ClusterProviderConfig",
                        match_name="default",
                    ),
                },
            ),
        ),
    ),
    # Custom credentials set every cloud MR's providerConfigRef. The
    # kubeconfig-based ProviderConfigs carry none, so they're unaffected.
    Case(
        name="custom credentials flow through to all cloud MRs",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(credentials=v1alpha1.Credentials(type="ProviderConfig", name="my-gcp-account")),
                resources={
                    "service-account": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "cloudplatform.gcp.m.upbound.io/v1beta1",
                                "kind": "ServiceAccount",
                                "spec": {"forProvider": {}},
                                "status": {
                                    "atProvider": {
                                        "email": "test-sa@my-gcp-project.iam.gserviceaccount.com",
                                    },
                                },
                            }
                        ),
                    ),
                },
            ),
            required_resources={
                "gcp-provider-config": fnv1.Resources(
                    items=[
                        _gcp_provider_config(
                            kind="ProviderConfig",
                            name="my-gcp-account",
                            namespace="crossplane-system",
                        ),
                    ],
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(),
                resources={
                    "network": _network(
                        cred_kind="ProviderConfig",
                        cred_name="my-gcp-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "projectservice-filestore": _projectservice_filestore(
                        cred_kind="ProviderConfig",
                        cred_name="my-gcp-account",
                    ),
                    "subnet": _subnet(cred_kind="ProviderConfig", cred_name="my-gcp-account"),
                    "cluster": _cluster(cred_kind="ProviderConfig", cred_name="my-gcp-account"),
                    "nodepool-system": _nodepool_system(cred_kind="ProviderConfig", cred_name="my-gcp-account"),
                    "nodepool-gpu-pool": _nodepool_gpu(cred_kind="ProviderConfig", cred_name="my-gcp-account"),
                    "service-account": _service_account(
                        cred_kind="ProviderConfig",
                        cred_name="my-gcp-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "service-account-key": _service_account_key(cred_kind="ProviderConfig", cred_name="my-gcp-account"),
                    "iam-binding": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "cloudplatform.gcp.m.upbound.io/v1beta1",
                                "kind": "ProjectIAMMember",
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "my-gcp-account"},
                                    "forProvider": {
                                        "role": "roles/container.admin",
                                        "member": "serviceAccount:test-sa@my-gcp-project.iam.gserviceaccount.com",
                                        "project": "my-gcp-project",
                                    },
                                },
                            }
                        ),
                    ),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                },
            ),
            context=structpb.Struct(),
            # A ProviderConfig, unlike a ClusterProviderConfig, is namespaced,
            # so the function requires it from the XR's namespace. Crossplane
            # wouldn't return the request's crossplane-system one for this
            # selector, but the function doesn't check its namespace.
            requirements=fnv1.Requirements(
                resources={
                    "gcp-provider-config": fnv1.ResourceSelector(
                        api_version="gcp.m.upbound.io/v1beta1",
                        kind="ProviderConfig",
                        match_name="my-gcp-account",
                        namespace="modelplane-system",
                    ),
                },
            ),
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction composes GKE cluster infrastructure."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want)
