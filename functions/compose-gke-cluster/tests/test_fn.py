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

import dataclasses
import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.infrastructure.gkecluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-gke-cluster."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


_DEFAULT_CRED_KIND = "ClusterProviderConfig"
_DEFAULT_CRED_NAME = "default"

_GCP_PROVIDER_CONFIG = {
    "apiVersion": "gcp.m.upbound.io/v1beta1",
    "kind": "ClusterProviderConfig",
    "metadata": {"name": "default"},
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

_GCP_PROVIDER_CONFIG_SELECTOR = fnv1.ResourceSelector(
    api_version="gcp.m.upbound.io/v1beta1",
    kind="ClusterProviderConfig",
    match_name="default",
)


def _gke_xr(credentials: v1alpha1.Credentials | None = None) -> v1alpha1.GKECluster:
    return v1alpha1.GKECluster(
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


def _network(cred_kind: str = _DEFAULT_CRED_KIND, cred_name: str = _DEFAULT_CRED_NAME) -> dict:
    return {
        "apiVersion": "compute.gcp.m.upbound.io/v1beta1",
        "kind": "Network",
        "spec": {
            "providerConfigRef": {"kind": cred_kind, "name": cred_name},
            "forProvider": {
                "autoCreateSubnetworks": False,
            },
        },
    }


def _projectservice_filestore(cred_kind: str = _DEFAULT_CRED_KIND, cred_name: str = _DEFAULT_CRED_NAME) -> dict:
    return {
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


def _subnet(cred_kind: str = _DEFAULT_CRED_KIND, cred_name: str = _DEFAULT_CRED_NAME) -> dict:
    return {
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


def _cluster(cred_kind: str = _DEFAULT_CRED_KIND, cred_name: str = _DEFAULT_CRED_NAME) -> dict:
    return {
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


def _nodepool_system(cred_kind: str = _DEFAULT_CRED_KIND, cred_name: str = _DEFAULT_CRED_NAME) -> dict:
    return {
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


def _nodepool_gpu(cred_kind: str = _DEFAULT_CRED_KIND, cred_name: str = _DEFAULT_CRED_NAME) -> dict:
    return {
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


def _service_account(cred_kind: str = _DEFAULT_CRED_KIND, cred_name: str = _DEFAULT_CRED_NAME) -> dict:
    return {
        "apiVersion": "cloudplatform.gcp.m.upbound.io/v1beta1",
        "kind": "ServiceAccount",
        "spec": {
            "providerConfigRef": {"kind": cred_kind, "name": cred_name},
            "forProvider": {
                "displayName": "Crossplane GKECluster test-cluster",
            },
        },
    }


def _service_account_key(cred_kind: str = _DEFAULT_CRED_KIND, cred_name: str = _DEFAULT_CRED_NAME) -> dict:
    return {
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


def _iam_binding(
    sa_email: str = "test-sa@my-gcp-project.iam.gserviceaccount.com",
    cred_kind: str = _DEFAULT_CRED_KIND,
    cred_name: str = _DEFAULT_CRED_NAME,
) -> dict:
    return {
        "apiVersion": "cloudplatform.gcp.m.upbound.io/v1beta1",
        "kind": "ProjectIAMMember",
        "spec": {
            "providerConfigRef": {"kind": cred_kind, "name": cred_name},
            "forProvider": {
                "role": "roles/container.admin",
                "member": f"serviceAccount:{sa_email}",
                "project": "my-gcp-project",
            },
        },
    }


def _provider_config_kubernetes() -> dict:
    return {
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


def _provider_config_helm() -> dict:
    return {
        "apiVersion": "helm.m.crossplane.io/v1beta1",
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


def _storage_class_rwx(network_name: str) -> dict:
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
                    "metadata": {"name": "modelplane-rwx"},
                    "provisioner": "filestore.csi.storage.gke.io",
                    "parameters": {
                        "tier": "enterprise",
                        "network": network_name,
                    },
                    "volumeBindingMode": "Immediate",
                    "allowVolumeExpansion": True,
                },
            },
        },
    }


def _expected_status() -> dict:
    return {
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


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    """Tests for FunctionRunner.RunFunction."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:
        """The function composes GKE cluster infrastructure."""
        req1 = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        _gke_xr().model_dump(exclude_none=True, mode="json"),
                    ),
                ),
            ),
        )
        req1.required_resources["gcp-provider-config"].items.append(
            fnv1.Resource(resource=resource.dict_to_struct(_GCP_PROVIDER_CONFIG))
        )

        want1 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(_expected_status()),
                ),
                resources={
                    "network": fnv1.Resource(
                        resource=resource.dict_to_struct(_network()),
                    ),
                    "projectservice-filestore": fnv1.Resource(
                        resource=resource.dict_to_struct(_projectservice_filestore()),
                    ),
                    "subnet": fnv1.Resource(
                        resource=resource.dict_to_struct(_subnet()),
                    ),
                    "cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(_cluster()),
                    ),
                    "nodepool-system": fnv1.Resource(
                        resource=resource.dict_to_struct(_nodepool_system()),
                    ),
                    "nodepool-gpu-pool": fnv1.Resource(
                        resource=resource.dict_to_struct(_nodepool_gpu()),
                    ),
                    "service-account": fnv1.Resource(
                        resource=resource.dict_to_struct(_service_account()),
                    ),
                    "service-account-key": fnv1.Resource(
                        resource=resource.dict_to_struct(_service_account_key()),
                    ),
                    "provider-config-kubernetes": fnv1.Resource(
                        resource=resource.dict_to_struct(_provider_config_kubernetes()),
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-helm": fnv1.Resource(
                        resource=resource.dict_to_struct(_provider_config_helm()),
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
        )
        want1.requirements.resources["gcp-provider-config"].CopyFrom(_GCP_PROVIDER_CONFIG_SELECTOR)

        req2 = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        _gke_xr().model_dump(exclude_none=True, mode="json"),
                    ),
                ),
                resources={
                    "service-account": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "cloudplatform.gcp.m.upbound.io/v1beta1",
                                "kind": "ServiceAccount",
                                "spec": {
                                    "forProvider": {},
                                },
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
                                # The external-name annotation carries the
                                # provider-generated VPC name, which the
                                # function pins the Filestore StorageClass to.
                                "metadata": {
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
        )
        req2.required_resources["gcp-provider-config"].items.append(
            fnv1.Resource(resource=resource.dict_to_struct(_GCP_PROVIDER_CONFIG))
        )

        want2 = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(_expected_status()),
                ),
                resources={
                    "network": fnv1.Resource(
                        resource=resource.dict_to_struct(_network()),
                        ready=fnv1.READY_TRUE,
                    ),
                    "projectservice-filestore": fnv1.Resource(
                        resource=resource.dict_to_struct(_projectservice_filestore()),
                    ),
                    # With the network name known, the managed Filestore
                    # StorageClass is composed against the cluster's own
                    # provider-kubernetes ProviderConfig, pinned to the
                    # observed VPC. StorageClass has no Ready condition,
                    # so readiness is SuccessfulCreate. It's orphaned (no
                    # Delete policy) so it dies with the cluster instead of
                    # wedging on a deleted kubeconfig Secret during teardown.
                    "storage-class-rwx": fnv1.Resource(
                        resource=resource.dict_to_struct(_storage_class_rwx("test-cluster-abc12")),
                        ready=fnv1.READY_TRUE,
                    ),
                    "subnet": fnv1.Resource(
                        resource=resource.dict_to_struct(_subnet()),
                    ),
                    "cluster": fnv1.Resource(
                        resource=resource.dict_to_struct(_cluster()),
                    ),
                    "nodepool-system": fnv1.Resource(
                        resource=resource.dict_to_struct(_nodepool_system()),
                    ),
                    "nodepool-gpu-pool": fnv1.Resource(
                        resource=resource.dict_to_struct(_nodepool_gpu()),
                    ),
                    "service-account": fnv1.Resource(
                        resource=resource.dict_to_struct(_service_account()),
                        ready=fnv1.READY_TRUE,
                    ),
                    "service-account-key": fnv1.Resource(
                        resource=resource.dict_to_struct(_service_account_key()),
                    ),
                    "iam-binding": fnv1.Resource(
                        resource=resource.dict_to_struct(_iam_binding()),
                    ),
                    "provider-config-kubernetes": fnv1.Resource(
                        resource=resource.dict_to_struct(_provider_config_kubernetes()),
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-helm": fnv1.Resource(
                        resource=resource.dict_to_struct(_provider_config_helm()),
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
        )
        want2.requirements.resources["gcp-provider-config"].CopyFrom(_GCP_PROVIDER_CONFIG_SELECTOR)

        cases = [
            Case(name="first pass composes infra resources; IAM binding gated", req=req1, want=want1),
            Case(
                name="second pass with observed SA email composes IAM binding and marks ready resources",
                req=req2,
                want=want2,
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

    async def test_custom_credentials(self) -> None:
        """Custom credentials flow through to all cloud MRs.

        When spec.credentials is set with a custom type and name, every cloud
        provider MR (Network, ProjectService, Subnetwork, Cluster, NodePools,
        ServiceAccount, ServiceAccountKey, IAM binding) carries the corresponding
        providerConfigRef. The kubeconfig-based resources (provider-config-kubernetes,
        provider-config-helm, storage-class-rwx) are unaffected.
        """
        ck = "ProviderConfig"
        cn = "my-gcp-account"
        creds = v1alpha1.Credentials(type=ck, name=cn)
        custom_pc = {
            "apiVersion": "gcp.m.upbound.io/v1beta1",
            "kind": "ProviderConfig",
            "metadata": {"name": cn, "namespace": "crossplane-system"},
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

        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        _gke_xr(credentials=creds).model_dump(exclude_none=True, mode="json"),
                    ),
                ),
                resources={
                    "service-account": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "cloudplatform.gcp.m.upbound.io/v1beta1",
                                "kind": "ServiceAccount",
                                "spec": {
                                    "forProvider": {},
                                },
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
        )
        req.required_resources["gcp-provider-config"].items.append(
            fnv1.Resource(resource=resource.dict_to_struct(custom_pc))
        )

        got = await self.runner.RunFunction(req, None)
        rs = got.desired.resources

        cloud_checks = {
            "network": _network(ck, cn),
            "projectservice-filestore": _projectservice_filestore(ck, cn),
            "subnet": _subnet(ck, cn),
            "cluster": _cluster(ck, cn),
            "nodepool-system": _nodepool_system(ck, cn),
            "nodepool-gpu-pool": _nodepool_gpu(ck, cn),
            "service-account": _service_account(ck, cn),
            "service-account-key": _service_account_key(ck, cn),
            "iam-binding": _iam_binding("test-sa@my-gcp-project.iam.gserviceaccount.com", ck, cn),
        }

        for key, want in cloud_checks.items():
            with self.subTest(resource=key):
                self.assertIn(key, rs, f"resource {key!r} not found in desired")
                got_dict = resource.struct_to_dict(rs[key].resource)
                self.assertEqual(want, got_dict, f"resource {key!r} mismatch")

        # kubeconfig-based resources must NOT carry the cloud providerConfigRef
        for key in ("provider-config-kubernetes", "provider-config-helm"):
            got_dict = resource.struct_to_dict(rs[key].resource)
            self.assertNotIn(
                "providerConfigRef",
                got_dict.get("spec", {}),
                f"{key} should not have providerConfigRef",
            )

        custom_selector = fnv1.ResourceSelector(
            api_version="gcp.m.upbound.io/v1beta1",
            kind="ProviderConfig",
            match_name=cn,
            namespace="modelplane-system",
        )
        self.assertEqual(
            custom_selector,
            got.requirements.resources["gcp-provider-config"],
        )
