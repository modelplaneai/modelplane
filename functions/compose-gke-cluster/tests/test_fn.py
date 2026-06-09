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


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    """Tests for FunctionRunner.RunFunction."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:
        """The function composes GKE cluster infrastructure."""
        cases = [
            Case(
                name="first pass composes infra resources; IAM binding gated",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                v1alpha1.GKECluster(
                                    metadata=metav1.ObjectMeta(
                                        name="test-cluster",
                                        namespace="modelplane-system",
                                    ),
                                    spec=v1alpha1.Spec(
                                        project="my-gcp-project",
                                        region="us-central1",
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
                                ).model_dump(exclude_none=True, mode="json")
                            ),
                        ),
                    ),
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(
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
                                                "type": "GCPServiceAccountKey",
                                                "name": "test-cluster-sa-key-3295c",
                                                "key": "private_key",
                                            },
                                        ],
                                    },
                                }
                            ),
                        ),
                        resources={
                            "network": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "compute.gcp.m.upbound.io/v1beta1",
                                        "kind": "Network",
                                        "spec": {
                                            "forProvider": {
                                                "project": "my-gcp-project",
                                                "autoCreateSubnetworks": False,
                                            },
                                        },
                                    }
                                ),
                            ),
                            "projectservice-filestore": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "cloudplatform.gcp.m.upbound.io/v1beta1",
                                        "kind": "ProjectService",
                                        "spec": {
                                            "forProvider": {
                                                "project": "my-gcp-project",
                                                "service": "file.googleapis.com",
                                                "disableOnDestroy": False,
                                            },
                                        },
                                    }
                                ),
                            ),
                            "subnet": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "compute.gcp.m.upbound.io/v1beta1",
                                        "kind": "Subnetwork",
                                        "spec": {
                                            "forProvider": {
                                                "project": "my-gcp-project",
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
                            ),
                            "cluster": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "container.gcp.m.upbound.io/v1beta1",
                                        "kind": "Cluster",
                                        "spec": {
                                            "forProvider": {
                                                "project": "my-gcp-project",
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
                                            },
                                            "writeConnectionSecretToRef": {
                                                "name": "test-cluster-kubeconfig-55b57",
                                            },
                                        },
                                    }
                                ),
                            ),
                            "nodepool-system": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "container.gcp.m.upbound.io/v1beta1",
                                        "kind": "NodePool",
                                        "spec": {
                                            "forProvider": {
                                                "project": "my-gcp-project",
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
                            ),
                            "nodepool-gpu-pool": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "container.gcp.m.upbound.io/v1beta1",
                                        "kind": "NodePool",
                                        "spec": {
                                            "forProvider": {
                                                "project": "my-gcp-project",
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
                                                    },
                                                },
                                            },
                                        },
                                    }
                                ),
                            ),
                            "service-account": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "cloudplatform.gcp.m.upbound.io/v1beta1",
                                        "kind": "ServiceAccount",
                                        "spec": {
                                            "forProvider": {
                                                "project": "my-gcp-project",
                                                "displayName": "Crossplane GKECluster test-cluster",
                                            },
                                        },
                                    }
                                ),
                            ),
                            "service-account-key": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "cloudplatform.gcp.m.upbound.io/v1beta1",
                                        "kind": "ServiceAccountKey",
                                        "spec": {
                                            "forProvider": {
                                                "serviceAccountIdSelector": {"matchControllerRef": True},
                                            },
                                            "writeConnectionSecretToRef": {
                                                "name": "test-cluster-sa-key-3295c",
                                            },
                                        },
                                    }
                                ),
                            ),
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
                            ),
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
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
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="second pass with observed SA email composes IAM binding and marks ready resources",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                v1alpha1.GKECluster(
                                    metadata=metav1.ObjectMeta(
                                        name="test-cluster",
                                        namespace="modelplane-system",
                                    ),
                                    spec=v1alpha1.Spec(
                                        project="my-gcp-project",
                                        region="us-central1",
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
                                ).model_dump(exclude_none=True, mode="json")
                            ),
                        ),
                        resources={
                            "service-account": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "cloudplatform.gcp.m.upbound.io/v1beta1",
                                        "kind": "ServiceAccount",
                                        "spec": {
                                            "forProvider": {"project": "my-gcp-project"},
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
                                        "spec": {
                                            "forProvider": {
                                                "project": "my-gcp-project",
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
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(
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
                                                "type": "GCPServiceAccountKey",
                                                "name": "test-cluster-sa-key-3295c",
                                                "key": "private_key",
                                            },
                                        ],
                                    },
                                }
                            ),
                        ),
                        resources={
                            "network": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "compute.gcp.m.upbound.io/v1beta1",
                                        "kind": "Network",
                                        "spec": {
                                            "forProvider": {
                                                "project": "my-gcp-project",
                                                "autoCreateSubnetworks": False,
                                            },
                                        },
                                    }
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                            "projectservice-filestore": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "cloudplatform.gcp.m.upbound.io/v1beta1",
                                        "kind": "ProjectService",
                                        "spec": {
                                            "forProvider": {
                                                "project": "my-gcp-project",
                                                "service": "file.googleapis.com",
                                                "disableOnDestroy": False,
                                            },
                                        },
                                    }
                                ),
                            ),
                            "subnet": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "compute.gcp.m.upbound.io/v1beta1",
                                        "kind": "Subnetwork",
                                        "spec": {
                                            "forProvider": {
                                                "project": "my-gcp-project",
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
                            ),
                            "cluster": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "container.gcp.m.upbound.io/v1beta1",
                                        "kind": "Cluster",
                                        "spec": {
                                            "forProvider": {
                                                "project": "my-gcp-project",
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
                                            },
                                            "writeConnectionSecretToRef": {
                                                "name": "test-cluster-kubeconfig-55b57",
                                            },
                                        },
                                    }
                                ),
                            ),
                            "nodepool-system": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "container.gcp.m.upbound.io/v1beta1",
                                        "kind": "NodePool",
                                        "spec": {
                                            "forProvider": {
                                                "project": "my-gcp-project",
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
                            ),
                            "nodepool-gpu-pool": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "container.gcp.m.upbound.io/v1beta1",
                                        "kind": "NodePool",
                                        "spec": {
                                            "forProvider": {
                                                "project": "my-gcp-project",
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
                                                    },
                                                },
                                            },
                                        },
                                    }
                                ),
                            ),
                            "service-account": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "cloudplatform.gcp.m.upbound.io/v1beta1",
                                        "kind": "ServiceAccount",
                                        "spec": {
                                            "forProvider": {
                                                "project": "my-gcp-project",
                                                "displayName": "Crossplane GKECluster test-cluster",
                                            },
                                        },
                                    }
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                            "service-account-key": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "cloudplatform.gcp.m.upbound.io/v1beta1",
                                        "kind": "ServiceAccountKey",
                                        "spec": {
                                            "forProvider": {
                                                "serviceAccountIdSelector": {"matchControllerRef": True},
                                            },
                                            "writeConnectionSecretToRef": {
                                                "name": "test-cluster-sa-key-3295c",
                                            },
                                        },
                                    }
                                ),
                            ),
                            "iam-binding": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        "apiVersion": "cloudplatform.gcp.m.upbound.io/v1beta1",
                                        "kind": "ProjectIAMMember",
                                        "spec": {
                                            "forProvider": {
                                                "project": "my-gcp-project",
                                                "role": "roles/container.admin",
                                                "member": "serviceAccount:test-sa@my-gcp-project.iam.gserviceaccount.com",
                                            },
                                        },
                                    }
                                ),
                            ),
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
                            ),
                            "provider-config-helm": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
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

    async def test_status_reports_observed_network_name(self) -> None:
        """status.network.name surfaces the composed Network's external-name
        (which carries a provider-generated suffix) so network-scoped consumers
        can pin to the real VPC name rather than guess it from the XR name."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        v1alpha1.GKECluster(
                            metadata=metav1.ObjectMeta(name="test-cluster", namespace="modelplane-system"),
                            spec=v1alpha1.Spec(
                                project="my-gcp-project",
                                region="us-central1",
                                nodePools=[
                                    v1alpha1.NodePool(
                                        name="gpu-pool",
                                        role="GPU",
                                        machineType="a2-highgpu-8g",
                                        gpu=v1alpha1.Gpu(acceleratorType="nvidia-tesla-a100", acceleratorCount=8),
                                    ),
                                ],
                            ),
                        ).model_dump(exclude_none=True, mode="json")
                    ),
                ),
                resources={
                    "network": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "compute.gcp.m.upbound.io/v1beta1",
                                "kind": "Network",
                                "metadata": {"annotations": {"crossplane.io/external-name": "test-cluster-abc12"}},
                            }
                        ),
                    ),
                },
            ),
        )
        got = await self.runner.RunFunction(req, None)
        status = json_format.MessageToDict(got.desired.composite.resource).get("status", {})
        self.assertEqual(status.get("network", {}).get("name"), "test-cluster-abc12")
