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

"""Tests for the compose-eks-cluster function."""

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
from models.ai.modelplane.infrastructure.ekscluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-eks-cluster."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _xr(*, credentials: v1alpha1.Credentials | None, node_pools: list[v1alpha1.NodePool]) -> fnv1.Resource:
    """The observed EKSCluster XR in us-west-2, with the given credentials and node pools."""
    xr = v1alpha1.EKSCluster(
        metadata=metav1.ObjectMeta(name="test-cluster", namespace="modelplane-system"),
        spec=v1alpha1.Spec(
            region="us-west-2",
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
                    # `type` is emitted because the function sets it explicitly
                    # on the Status model, so update_status (exclude_unset)
                    # keeps it rather than dropping it as an unset field.
                    "secrets": [
                        {
                            "type": "Kubeconfig",
                            "name": "test-cluster-kubeconfig-55b57",
                            "key": "kubeconfig",
                        },
                    ],
                    # write_status always publishes the effective RWX
                    # StorageClass name, even before the managed class
                    # materialises on the workload cluster.
                    "cache": {"storageClassName": "modelplane-rwx-efs"},
                },
            }
        ),
    )


def _vpc(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The VPC."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                "kind": "VPC",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "region": "us-west-2",
                        "cidrBlock": "10.0.0.0/16",
                        "enableDnsHostnames": True,
                        "enableDnsSupport": True,
                    },
                },
            }
        ),
        ready=ready,
    )


def _subnet(*, name: str, az: str, cidr: str, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """A public subnet in az."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                "kind": "Subnet",
                "metadata": {
                    "name": name,
                    "labels": {"modelplane.ai/zone": az, "modelplane.ai/subnet-tier": "public"},
                },
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "region": "us-west-2",
                        "availabilityZone": az,
                        "cidrBlock": cidr,
                        "mapPublicIpOnLaunch": True,
                        "tags": {"kubernetes.io/role/elb": "1"},
                        "vpcIdSelector": {"matchControllerRef": True},
                    },
                },
            }
        ),
        ready=ready,
    )


def _private_subnet(
    *, name: str, az: str, cidr: str, cred_kind: str, cred_name: str, ready: fnv1.Ready
) -> fnv1.Resource:
    """A private subnet in az."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                "kind": "Subnet",
                "metadata": {
                    "name": name,
                    "labels": {"modelplane.ai/zone": az, "modelplane.ai/subnet-tier": "private"},
                },
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "region": "us-west-2",
                        "availabilityZone": az,
                        "cidrBlock": cidr,
                        "mapPublicIpOnLaunch": False,
                        "vpcIdSelector": {"matchControllerRef": True},
                    },
                },
            }
        ),
        ready=ready,
    )


def _internet_gateway(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The VPC's internet gateway."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                "kind": "InternetGateway",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "region": "us-west-2",
                        "vpcIdSelector": {"matchControllerRef": True},
                    },
                },
            }
        ),
        ready=ready,
    )


def _route_table(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The public route table."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                "kind": "RouteTable",
                "metadata": {"labels": {"modelplane.ai/subnet-tier": "public"}},
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "region": "us-west-2",
                        "vpcIdSelector": {"matchControllerRef": True},
                    },
                },
            }
        ),
        ready=ready,
    )


def _route_default(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The public route table's default route, through the internet gateway."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                "kind": "Route",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "region": "us-west-2",
                        "destinationCidrBlock": "0.0.0.0/0",
                        "gatewayIdSelector": {"matchControllerRef": True},
                        "routeTableIdSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/subnet-tier": "public"},
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _nat_eip(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The NAT gateway's elastic IP."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                "kind": "EIP",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "region": "us-west-2",
                        "domain": "vpc",
                    },
                },
            }
        ),
        ready=ready,
    )


def _nat_gateway(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The NAT gateway, in the first AZ's public subnet."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                "kind": "NATGateway",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "region": "us-west-2",
                        "allocationIdSelector": {"matchControllerRef": True},
                        "subnetIdSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {
                                "modelplane.ai/zone": "us-west-2a",
                                "modelplane.ai/subnet-tier": "public",
                            },
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _private_route_table(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The private route table."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                "kind": "RouteTable",
                "metadata": {"labels": {"modelplane.ai/subnet-tier": "private"}},
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "region": "us-west-2",
                        "vpcIdSelector": {"matchControllerRef": True},
                    },
                },
            }
        ),
        ready=ready,
    )


def _private_route_default(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The private route table's default route, through the NAT gateway."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                "kind": "Route",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "region": "us-west-2",
                        "destinationCidrBlock": "0.0.0.0/0",
                        "natGatewayIdSelector": {"matchControllerRef": True},
                        "routeTableIdSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/subnet-tier": "private"},
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _route_table_association(*, az: str, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The association of az's public subnet with the public route table."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                "kind": "RouteTableAssociation",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "region": "us-west-2",
                        "routeTableIdSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/subnet-tier": "public"},
                        },
                        "subnetIdSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {
                                "modelplane.ai/zone": az,
                                "modelplane.ai/subnet-tier": "public",
                            },
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _private_route_table_association(*, az: str, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The association of az's private subnet with the private route table."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                "kind": "RouteTableAssociation",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "region": "us-west-2",
                        "routeTableIdSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/subnet-tier": "private"},
                        },
                        "subnetIdSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {
                                "modelplane.ai/zone": az,
                                "modelplane.ai/subnet-tier": "private",
                            },
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _cluster_role(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The IAM role the EKS control plane assumes."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "iam.aws.m.upbound.io/v1beta1",
                "kind": "Role",
                "metadata": {"labels": {"modelplane.ai/iam-role": "cluster"}},
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "assumeRolePolicy": (
                            '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
                            '"Principal":{"Service":"eks.amazonaws.com"},'
                            '"Action":"sts:AssumeRole"}]}'
                        ),
                    },
                },
            }
        ),
        ready=ready,
    )


def _node_role(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The IAM role the nodes assume."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "iam.aws.m.upbound.io/v1beta1",
                "kind": "Role",
                "metadata": {"labels": {"modelplane.ai/iam-role": "node"}},
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "assumeRolePolicy": (
                            '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
                            '"Principal":{"Service":"ec2.amazonaws.com"},'
                            '"Action":"sts:AssumeRole"}]}'
                        ),
                    },
                },
            }
        ),
        ready=ready,
    )


def _pod_identity_role(*, role: str, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """An IAM role a ServiceAccount assumes through EKS Pod Identity."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "iam.aws.m.upbound.io/v1beta1",
                "kind": "Role",
                "metadata": {"labels": {"modelplane.ai/iam-role": role}},
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "assumeRolePolicy": (
                            '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
                            '"Principal":{"Service":"pods.eks.amazonaws.com"},'
                            '"Action":["sts:AssumeRole","sts:TagSession"]}]}'
                        ),
                    },
                },
            }
        ),
        ready=ready,
    )


def _role_policy_attachment(*, role: str, arn: str, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """An attachment of the managed policy arn to the role labelled role."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "iam.aws.m.upbound.io/v1beta1",
                "kind": "RolePolicyAttachment",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "policyArn": arn,
                        "roleSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/iam-role": role},
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _eks_cluster(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed EKS Cluster."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "eks.aws.m.upbound.io/v1beta1",
                "kind": "Cluster",
                "metadata": {"name": "modelplane-system-test-cluster-eks-0865f"},
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "region": "us-west-2",
                        "version": "1.36",
                        "roleArnSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/iam-role": "cluster"},
                        },
                        "accessConfig": {
                            "authenticationMode": "API_AND_CONFIG_MAP",
                            "bootstrapClusterCreatorAdminPermissions": True,
                        },
                        "vpcConfig": {
                            "endpointPrivateAccess": True,
                            "endpointPublicAccess": True,
                            "subnetIdSelector": {"matchControllerRef": True},
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _observed_eks_cluster(*, cluster_security_group_id: str | None, ready_condition: bool) -> fnv1.Resource:
    """The observed EKS Cluster, with its security group id if given, and a True Ready condition only if ready_condition."""
    status: dict = {}
    if cluster_security_group_id is not None:
        status["atProvider"] = {"vpcConfig": {"clusterSecurityGroupId": cluster_security_group_id}}
    if ready_condition:
        status["conditions"] = [
            {
                "type": "Ready",
                "status": "True",
                "reason": "Available",
                "lastTransitionTime": "2024-01-01T00:00:00Z",
            },
        ]
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "eks.aws.m.upbound.io/v1beta1",
                "kind": "Cluster",
                "metadata": {"name": "modelplane-system-test-cluster-eks-0865f"},
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                    "forProvider": {
                        "region": "us-west-2",
                        "version": "1.36",
                        "roleArnSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/iam-role": "cluster"},
                        },
                        "accessConfig": {
                            "authenticationMode": "API_AND_CONFIG_MAP",
                            "bootstrapClusterCreatorAdminPermissions": True,
                        },
                        "vpcConfig": {
                            "endpointPrivateAccess": True,
                            "endpointPublicAccess": True,
                            "subnetIdSelector": {"matchControllerRef": True},
                        },
                    },
                },
                "status": status,
            }
        ),
    )


def _cluster_auth(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed ClusterAuth."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "eks.aws.m.upbound.io/v1beta1",
                "kind": "ClusterAuth",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "region": "us-west-2",
                        "clusterNameSelector": {"matchControllerRef": True},
                    },
                    "writeConnectionSecretToRef": {"name": "test-cluster-kubeconfig-55b57"},
                },
            }
        ),
        ready=ready,
    )


def _nodegroup_system(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed system node group every cluster gets."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "eks.aws.m.upbound.io/v1beta1",
                "kind": "NodeGroup",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "managementPolicies": ["Observe", "Create", "Update", "Delete"],
                    "initProvider": {"scalingConfig": {"desiredSize": 1}},
                    "forProvider": {
                        "region": "us-west-2",
                        "amiType": "AL2023_x86_64_STANDARD",
                        "instanceTypes": ["m6i.xlarge"],
                        "clusterNameSelector": {"matchControllerRef": True},
                        "nodeRoleArnSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/iam-role": "node"},
                        },
                        "subnetIdSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/subnet-tier": "private"},
                        },
                        "scalingConfig": {"minSize": 1, "maxSize": 2},
                        "labels": {"modelplane.ai/pool": "system"},
                    },
                },
            }
        ),
        ready=ready,
    )


def _nodegroup_gpu(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed gpu-l4 node group, which needs no launch template."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "eks.aws.m.upbound.io/v1beta1",
                "kind": "NodeGroup",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "managementPolicies": ["Observe", "Create", "Update", "Delete"],
                    "initProvider": {"scalingConfig": {"desiredSize": 1}},
                    "forProvider": {
                        "region": "us-west-2",
                        "amiType": "AL2023_x86_64_NVIDIA",
                        "instanceTypes": ["g6.xlarge"],
                        "diskSize": 100,
                        "clusterNameSelector": {"matchControllerRef": True},
                        "nodeRoleArnSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/iam-role": "node"},
                        },
                        "subnetIdRefs": [
                            {"name": "test-cluster-private-subnet-us-west-2a-6a89f"},
                            {"name": "test-cluster-private-subnet-us-west-2b-b7832"},
                        ],
                        "scalingConfig": {"minSize": 0, "maxSize": 4},
                        "labels": {
                            "modelplane.ai/gpu": "nvidia-l4",
                            "modelplane.ai/pool": "gpu-l4",
                        },
                        "taint": [
                            {
                                "key": "nvidia.com/gpu",
                                "value": "true",
                                "effect": "NO_SCHEDULE",
                            },
                        ],
                    },
                },
            }
        ),
        ready=ready,
    )


def _nodegroup_cpu(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
    """The composed cpu-pool node group, on the standard AMI with the CPU label and taint."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "eks.aws.m.upbound.io/v1beta1",
                "kind": "NodeGroup",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "managementPolicies": ["Observe", "Create", "Update", "Delete"],
                    "initProvider": {"scalingConfig": {"desiredSize": 1}},
                    "forProvider": {
                        "region": "us-west-2",
                        "amiType": "AL2023_x86_64_STANDARD",
                        "instanceTypes": ["m6i.4xlarge"],
                        "diskSize": 100,
                        "clusterNameSelector": {"matchControllerRef": True},
                        "nodeRoleArnSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/iam-role": "node"},
                        },
                        "subnetIdSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/subnet-tier": "private"},
                        },
                        "scalingConfig": {"minSize": 0, "maxSize": 4},
                        "labels": {
                            "modelplane.ai/pool": "cpu-pool",
                            "modelplane.ai/cpu": "true",
                        },
                        "taint": [
                            {
                                "key": "modelplane.ai/cpu",
                                "value": "true",
                                "effect": "NO_SCHEDULE",
                            },
                        ],
                    },
                },
            }
        ),
    )


def _launch_template_efa(*, security_groups: list[str] | None) -> fnv1.Resource:
    """The gpu-h200 EFA launch template, with security_groups on every interface if given."""
    # p5en.48xlarge has 16 network cards; one EFA interface per card.
    interfaces: list[dict] = [
        {"networkCardIndex": 0, "deviceIndex": 0, "interfaceType": "efa"},
        {"networkCardIndex": 1, "deviceIndex": 1, "interfaceType": "efa-only"},
        {"networkCardIndex": 2, "deviceIndex": 1, "interfaceType": "efa-only"},
        {"networkCardIndex": 3, "deviceIndex": 1, "interfaceType": "efa-only"},
        {"networkCardIndex": 4, "deviceIndex": 1, "interfaceType": "efa-only"},
        {"networkCardIndex": 5, "deviceIndex": 1, "interfaceType": "efa-only"},
        {"networkCardIndex": 6, "deviceIndex": 1, "interfaceType": "efa-only"},
        {"networkCardIndex": 7, "deviceIndex": 1, "interfaceType": "efa-only"},
        {"networkCardIndex": 8, "deviceIndex": 1, "interfaceType": "efa-only"},
        {"networkCardIndex": 9, "deviceIndex": 1, "interfaceType": "efa-only"},
        {"networkCardIndex": 10, "deviceIndex": 1, "interfaceType": "efa-only"},
        {"networkCardIndex": 11, "deviceIndex": 1, "interfaceType": "efa-only"},
        {"networkCardIndex": 12, "deviceIndex": 1, "interfaceType": "efa-only"},
        {"networkCardIndex": 13, "deviceIndex": 1, "interfaceType": "efa-only"},
        {"networkCardIndex": 14, "deviceIndex": 1, "interfaceType": "efa-only"},
        {"networkCardIndex": 15, "deviceIndex": 1, "interfaceType": "efa-only"},
    ]
    if security_groups is not None:
        for interface in interfaces:
            interface["securityGroups"] = security_groups
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                "kind": "LaunchTemplate",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                    "forProvider": {
                        "region": "us-west-2",
                        "name": "test-cluster-lt-gpu-h200-83c00",
                        "instanceType": "p5en.48xlarge",
                        "blockDeviceMappings": [
                            {"deviceName": "/dev/xvda", "ebs": {"volumeSize": 1024}},
                        ],
                        "networkInterfaces": interfaces,
                    },
                },
            }
        ),
    )


def _efa_security_group() -> fnv1.Resource:
    """The composed EFA security group."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                "kind": "SecurityGroup",
                "metadata": {
                    "name": "test-cluster-efa-sg-602a9",
                    "labels": {"modelplane.ai/fabric": "EFA"},
                },
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                    "forProvider": {
                        "region": "us-west-2",
                        "name": "test-cluster-efa",
                        "description": "EFA OS-bypass traffic between gang nodes",
                        "vpcIdSelector": {"matchControllerRef": True},
                    },
                },
            }
        ),
    )


def _efa_security_group_ingress() -> fnv1.Resource:
    """The EFA security group's rule admitting all traffic from itself."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                "kind": "SecurityGroupIngressRule",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                    "forProvider": {
                        "region": "us-west-2",
                        "ipProtocol": "-1",
                        "referencedSecurityGroupIdSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/fabric": "EFA"},
                        },
                        "securityGroupIdSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/fabric": "EFA"},
                        },
                    },
                },
            }
        ),
    )


def _efa_security_group_egress() -> fnv1.Resource:
    """The EFA security group's rule allowing all traffic to itself."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                "kind": "SecurityGroupEgressRule",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                    "forProvider": {
                        "region": "us-west-2",
                        "ipProtocol": "-1",
                        "referencedSecurityGroupIdSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/fabric": "EFA"},
                        },
                        "securityGroupIdSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/fabric": "EFA"},
                        },
                    },
                },
            }
        ),
    )


def _nodegroup_gpu_efa() -> fnv1.Resource:
    """The composed gpu-h200 EFA node group, which takes its instance type from the launch template."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "eks.aws.m.upbound.io/v1beta1",
                "kind": "NodeGroup",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                    "managementPolicies": ["Observe", "Create", "Update", "Delete"],
                    "initProvider": {"scalingConfig": {"desiredSize": 2}},
                    "forProvider": {
                        "region": "us-west-2",
                        "amiType": "AL2023_x86_64_NVIDIA",
                        "clusterNameSelector": {"matchControllerRef": True},
                        "nodeRoleArnSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/iam-role": "node"},
                        },
                        "launchTemplate": {
                            "name": "test-cluster-lt-gpu-h200-83c00",
                            "version": "$Latest",
                        },
                        "subnetIdRefs": [{"name": "test-cluster-private-subnet-us-west-2a-6a89f"}],
                        "scalingConfig": {"minSize": 0, "maxSize": 2},
                        "labels": {
                            "modelplane.ai/gpu": "nvidia-h200",
                            "modelplane.ai/pool": "gpu-h200",
                        },
                        "taint": [
                            {
                                "key": "nvidia.com/gpu",
                                "value": "true",
                                "effect": "NO_SCHEDULE",
                            },
                        ],
                    },
                },
            }
        ),
    )


def _addon(*, name: str, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The EKS addon called name."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "eks.aws.m.upbound.io/v1beta1",
                "kind": "Addon",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "region": "us-west-2",
                        "addonName": name,
                        "clusterNameSelector": {"matchControllerRef": True},
                    },
                },
            }
        ),
        ready=ready,
    )


def _efs_filesystem(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The composed EFS filesystem backing ModelCache."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "efs.aws.m.upbound.io/v1beta1",
                "kind": "FileSystem",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {"region": "us-west-2", "throughputMode": "elastic", "encrypted": True},
                },
            }
        ),
        ready=ready,
    )


def _efs_security_group(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The security group on the EFS mount targets."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                "kind": "SecurityGroup",
                "metadata": {"labels": {"modelplane.ai/sg-role": "efs"}},
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "region": "us-west-2",
                        "name": "test-cluster-efs",
                        "description": "NFS access to the ModelCache EFS mount targets",
                        "vpcIdSelector": {"matchControllerRef": True},
                    },
                },
            }
        ),
        ready=ready,
    )


def _efs_security_group_ingress(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The EFS security group's rule admitting NFS from the VPC."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                "kind": "SecurityGroupIngressRule",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "region": "us-west-2",
                        "ipProtocol": "tcp",
                        "fromPort": 2049,
                        "toPort": 2049,
                        "cidrIpv4": "10.0.0.0/16",
                        "securityGroupIdSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/sg-role": "efs"},
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _efs_mount_target(*, subnet_name: str, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """An EFS mount target in the named subnet."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "efs.aws.m.upbound.io/v1beta1",
                "kind": "MountTarget",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "region": "us-west-2",
                        "fileSystemIdSelector": {"matchControllerRef": True},
                        "subnetIdRef": {"name": subnet_name},
                        "securityGroupsSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/sg-role": "efs"},
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _pod_identity_association(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The Pod Identity association for the EFS CSI controller."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "eks.aws.m.upbound.io/v1beta1",
                "kind": "PodIdentityAssociation",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "region": "us-west-2",
                        "namespace": "kube-system",
                        "serviceAccount": "efs-csi-controller-sa",
                        "clusterNameSelector": {"matchControllerRef": True},
                        "roleArnSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/iam-role": "efs-csi"},
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _autoscaler_policy(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The IAM policy the cluster autoscaler needs."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "iam.aws.m.upbound.io/v1beta1",
                "kind": "Policy",
                "metadata": {"labels": {"modelplane.ai/iam-role": "cluster-autoscaler"}},
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "policy": (
                            '{"Version":"2012-10-17","Statement":['
                            '{"Effect":"Allow","Action":['
                            '"autoscaling:DescribeAutoScalingGroups",'
                            '"autoscaling:DescribeAutoScalingInstances",'
                            '"autoscaling:DescribeLaunchConfigurations",'
                            '"autoscaling:DescribeScalingActivities",'
                            '"ec2:DescribeImages",'
                            '"ec2:DescribeInstanceTypes",'
                            '"ec2:DescribeLaunchTemplateVersions",'
                            '"ec2:GetInstanceTypesFromInstanceRequirements",'
                            '"eks:DescribeNodegroup"'
                            '],"Resource":["*"]},'
                            '{"Effect":"Allow","Action":['
                            '"autoscaling:SetDesiredCapacity",'
                            '"autoscaling:TerminateInstanceInAutoScalingGroup"'
                            '],"Resource":["*"]}]}'
                        ),
                    },
                },
            }
        ),
        ready=ready,
    )


def _autoscaler_attachment(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The attachment of the autoscaler's policy to its role."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "iam.aws.m.upbound.io/v1beta1",
                "kind": "RolePolicyAttachment",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "policyArnSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/iam-role": "cluster-autoscaler"},
                        },
                        "roleSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/iam-role": "cluster-autoscaler"},
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _autoscaler_pod_identity(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The Pod Identity association for the cluster autoscaler."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "eks.aws.m.upbound.io/v1beta1",
                "kind": "PodIdentityAssociation",
                "spec": {
                    "providerConfigRef": {"kind": cred_kind, "name": cred_name},
                    "forProvider": {
                        "region": "us-west-2",
                        "namespace": "kube-system",
                        "serviceAccount": "cluster-autoscaler",
                        "clusterNameSelector": {"matchControllerRef": True},
                        "roleArnSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {"modelplane.ai/iam-role": "cluster-autoscaler"},
                        },
                    },
                },
            }
        ),
        ready=ready,
    )


def _autoscaler_release(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The cluster autoscaler's Helm release."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "Release",
                "metadata": {"namespace": "modelplane-system"},
                "spec": {
                    "managementPolicies": ["Observe", "Create", "Update"],
                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-cluster-kubeconfig-55b57"},
                    "forProvider": {
                        "chart": {
                            "name": "cluster-autoscaler",
                            "repository": "https://kubernetes.github.io/autoscaler",
                            "version": "9.57.0",
                        },
                        "namespace": "kube-system",
                        "values": {
                            "cloudProvider": "aws",
                            "awsRegion": "us-west-2",
                            "autoDiscovery": {"clusterName": "modelplane-system-test-cluster-eks-0865f"},
                            "rbac": {"serviceAccount": {"name": "cluster-autoscaler"}},
                            "extraArgs": {"balance-similar-node-groups": True},
                        },
                    },
                },
            }
        ),
        ready=ready,
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


def _observed_resource(*, ready: bool) -> fnv1.Resource:
    """An observed managed resource, Ready or not."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "status": {
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "True" if ready else "False",
                            "reason": "Available" if ready else "Creating",
                            "lastTransitionTime": "2024-01-01T00:00:00Z",
                        },
                    ],
                },
            }
        ),
    )


def _observed_efs_filesystem(*, ready: bool) -> fnv1.Resource:
    """The observed EFS FileSystem, whose external name is the id the StorageClass pins to."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "efs.aws.m.upbound.io/v1beta1",
                "kind": "FileSystem",
                "metadata": {"annotations": {"crossplane.io/external-name": "fs-0abc123"}},
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                    "forProvider": {
                        "region": "us-west-2",
                        "throughputMode": "elastic",
                        "encrypted": True,
                    },
                },
                "status": {
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "True" if ready else "False",
                            "reason": "Available" if ready else "Creating",
                            "lastTransitionTime": "2024-01-01T00:00:00Z",
                        },
                    ],
                },
            }
        ),
    )


def _storage_class_rwx_efs() -> fnv1.Resource:
    """The composed EFS StorageClass, pinned to the observed FileSystem's id."""
    return fnv1.Resource(
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
                            "metadata": {"name": "modelplane-rwx-efs"},
                            "provisioner": "efs.csi.aws.com",
                            "parameters": {
                                "provisioningMode": "efs-ap",
                                "fileSystemId": "fs-0abc123",
                                "directoryPerms": "700",
                            },
                            "volumeBindingMode": "Immediate",
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
    Case(
        name="FirstPass",
        reason="With nothing observed, an EKSCluster composes its AWS infrastructure and ProviderConfigs, and marks only the ProviderConfigs ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l4",
                            role="GPU",
                            instanceType="g6.xlarge",
                            nodeCount=1,
                            minNodeCount=0,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l4"),
                            zones=[v1alpha1.Zone("us-west-2a"), v1alpha1.Zone("us-west-2b")],
                        ),
                    ],
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "gpu-l4", "state": "Provisioning"}]),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "internet-gateway": _internet_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nat-eip": _nat_eip(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nat-gateway": _nat_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-table": _route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-default": _route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "private-route-table": _private_route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-cluster": _cluster_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-node": _node_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-gpu-l4": _nodegroup_gpu(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "addon-vpc-cni": _addon(
                        name="vpc-cni",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-coredns": _addon(
                        name="coredns",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group": _efs_security_group(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
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
                    message="2 of 49 resources ready",
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
        reason="A CPU-role node pool composes its node group on the standard AMI with the modelplane.ai/pool and modelplane.ai/cpu labels and the CPU taint.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="cpu-pool",
                            role="CPU",
                            instanceType="m6i.4xlarge",
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
                composite=_desired_xr(node_pools=[{"name": "cpu-pool", "state": "Provisioning"}]),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "internet-gateway": _internet_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nat-eip": _nat_eip(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nat-gateway": _nat_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-table": _route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-default": _route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "private-route-table": _private_route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-cluster": _cluster_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-node": _node_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-cpu-pool": _nodegroup_cpu(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "addon-vpc-cni": _addon(
                        name="vpc-cni",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-coredns": _addon(
                        name="coredns",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group": _efs_security_group(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
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
                    message="2 of 49 resources ready",
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
    # The observed filesystem id lets the function compose the StorageClass
    # Object, which is ready, against the cluster's own ProviderConfig. The
    # observed cluster lets it compose the autoscaler Helm release, since
    # provider-helm can now reach the cluster. The release isn't observed yet, so
    # it isn't ready.
    Case(
        name="ResourcesReady",
        reason="With the cluster, ClusterAuth and EFS filesystem observed Ready, an EKSCluster marks them ready and composes the StorageClass and autoscaler release.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l4",
                            role="GPU",
                            instanceType="g6.xlarge",
                            nodeCount=1,
                            minNodeCount=0,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l4"),
                            zones=[v1alpha1.Zone("us-west-2a"), v1alpha1.Zone("us-west-2b")],
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_eks_cluster(cluster_security_group_id=None, ready_condition=True),
                    "cluster-auth": _observed_resource(ready=True),
                    "efs-filesystem": _observed_efs_filesystem(ready=True),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "gpu-l4", "state": "Provisioning"}]),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "internet-gateway": _internet_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nat-eip": _nat_eip(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nat-gateway": _nat_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-table": _route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-default": _route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "private-route-table": _private_route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-cluster": _cluster_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-node": _node_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-gpu-l4": _nodegroup_gpu(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "addon-vpc-cni": _addon(
                        name="vpc-cni",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-coredns": _addon(
                        name="coredns",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-security-group": _efs_security_group(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "storage-class-rwx-efs": _storage_class_rwx_efs(),
                    "release-cluster-autoscaler": _autoscaler_release(ready=fnv1.READY_UNSPECIFIED),
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
                    message="6 of 51 resources ready",
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
    # The launch template targets the reservation through the capacity-block
    # market type. The node group sets no instanceTypes, because EKS takes the
    # type from the launch template.
    Case(
        name="CapacityBlock",
        reason="An EKSCluster with a Capacity Block pool composes a launch template targeting its reservation and a CAPACITY_BLOCK node group that uses the template.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h200",
                            role="GPU",
                            instanceType="p5en.48xlarge",
                            nodeCount=2,
                            minNodeCount=0,
                            maxNodeCount=2,
                            diskSizeGb=1024,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h200"),
                            capacityReservation=v1alpha1.CapacityReservation(
                                type="CapacityBlock", id="cr-0123456789abcdef0"
                            ),
                            zones=[v1alpha1.Zone("us-west-2a")],
                        ),
                    ],
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "gpu-h200", "state": "Provisioning"}]),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "internet-gateway": _internet_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nat-eip": _nat_eip(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nat-gateway": _nat_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-table": _route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-default": _route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "private-route-table": _private_route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-cluster": _cluster_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-node": _node_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "launch-template-gpu-h200": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                                "kind": "LaunchTemplate",
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "forProvider": {
                                        "region": "us-west-2",
                                        "name": "test-cluster-lt-gpu-h200-83c00",
                                        "instanceType": "p5en.48xlarge",
                                        "blockDeviceMappings": [
                                            {"deviceName": "/dev/xvda", "ebs": {"volumeSize": 1024}},
                                        ],
                                        "instanceMarketOptions": {"marketType": "capacity-block"},
                                        "capacityReservationSpecification": {
                                            "capacityReservationPreference": "capacity-reservations-only",
                                            "capacityReservationTarget": {
                                                "capacityReservationId": "cr-0123456789abcdef0",
                                            },
                                        },
                                    },
                                },
                            }
                        ),
                    ),
                    "nodegroup-gpu-h200": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "eks.aws.m.upbound.io/v1beta1",
                                "kind": "NodeGroup",
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "managementPolicies": ["Observe", "Create", "Update", "Delete"],
                                    "initProvider": {"scalingConfig": {"desiredSize": 2}},
                                    "forProvider": {
                                        "region": "us-west-2",
                                        "amiType": "AL2023_x86_64_NVIDIA",
                                        "clusterNameSelector": {"matchControllerRef": True},
                                        "nodeRoleArnSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/iam-role": "node"},
                                        },
                                        "capacityType": "CAPACITY_BLOCK",
                                        "launchTemplate": {
                                            "name": "test-cluster-lt-gpu-h200-83c00",
                                            "version": "$Latest",
                                        },
                                        "subnetIdRefs": [{"name": "test-cluster-private-subnet-us-west-2a-6a89f"}],
                                        "scalingConfig": {"minSize": 0, "maxSize": 2},
                                        "labels": {
                                            "modelplane.ai/gpu": "nvidia-h200",
                                            "modelplane.ai/pool": "gpu-h200",
                                        },
                                        "taint": [
                                            {
                                                "key": "nvidia.com/gpu",
                                                "value": "true",
                                                "effect": "NO_SCHEDULE",
                                            },
                                        ],
                                    },
                                },
                            }
                        ),
                    ),
                    "addon-vpc-cni": _addon(
                        name="vpc-cni",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-coredns": _addon(
                        name="coredns",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group": _efs_security_group(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
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
                    message="2 of 49 resources ready",
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
    # An On-Demand Capacity Reservation needs no market type, and its node group
    # keeps the default on-demand capacity type.
    Case(
        name="OnDemandCapacityReservation",
        reason="An EKSCluster with an On-Demand Capacity Reservation pool composes a launch template targeting its reservation, with no market type, and an on-demand node group that uses the template.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h200",
                            role="GPU",
                            instanceType="p5en.48xlarge",
                            nodeCount=2,
                            minNodeCount=0,
                            maxNodeCount=2,
                            diskSizeGb=1024,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h200"),
                            capacityReservation=v1alpha1.CapacityReservation(
                                type="OnDemand", id="cr-0123456789abcdef0"
                            ),
                            zones=[v1alpha1.Zone("us-west-2a")],
                        ),
                    ],
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "gpu-h200", "state": "Provisioning"}]),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "internet-gateway": _internet_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nat-eip": _nat_eip(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nat-gateway": _nat_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-table": _route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-default": _route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "private-route-table": _private_route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-cluster": _cluster_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-node": _node_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "launch-template-gpu-h200": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                                "kind": "LaunchTemplate",
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "forProvider": {
                                        "region": "us-west-2",
                                        "name": "test-cluster-lt-gpu-h200-83c00",
                                        "instanceType": "p5en.48xlarge",
                                        "blockDeviceMappings": [
                                            {"deviceName": "/dev/xvda", "ebs": {"volumeSize": 1024}},
                                        ],
                                        "capacityReservationSpecification": {
                                            "capacityReservationPreference": "capacity-reservations-only",
                                            "capacityReservationTarget": {
                                                "capacityReservationId": "cr-0123456789abcdef0",
                                            },
                                        },
                                    },
                                },
                            }
                        ),
                    ),
                    "nodegroup-gpu-h200": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "eks.aws.m.upbound.io/v1beta1",
                                "kind": "NodeGroup",
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "managementPolicies": ["Observe", "Create", "Update", "Delete"],
                                    "initProvider": {"scalingConfig": {"desiredSize": 2}},
                                    "forProvider": {
                                        "region": "us-west-2",
                                        "amiType": "AL2023_x86_64_NVIDIA",
                                        "clusterNameSelector": {"matchControllerRef": True},
                                        "nodeRoleArnSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/iam-role": "node"},
                                        },
                                        "launchTemplate": {
                                            "name": "test-cluster-lt-gpu-h200-83c00",
                                            "version": "$Latest",
                                        },
                                        "subnetIdRefs": [{"name": "test-cluster-private-subnet-us-west-2a-6a89f"}],
                                        "scalingConfig": {"minSize": 0, "maxSize": 2},
                                        "labels": {
                                            "modelplane.ai/gpu": "nvidia-h200",
                                            "modelplane.ai/pool": "gpu-h200",
                                        },
                                        "taint": [
                                            {
                                                "key": "nvidia.com/gpu",
                                                "value": "true",
                                                "effect": "NO_SCHEDULE",
                                            },
                                        ],
                                    },
                                },
                            }
                        ),
                    ),
                    "addon-vpc-cni": _addon(
                        name="vpc-cni",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-coredns": _addon(
                        name="coredns",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group": _efs_security_group(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
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
                    message="2 of 49 resources ready",
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
    # The node group's launch template carries one EFA interface per network card
    # (card 0 keeps device index 0 for the node's IP traffic, the rest device
    # index 1 for RDMA), the cluster gets an EFA security group with
    # self-referencing all-traffic ingress and egress rules, and the node group
    # references the launch template instead of setting instanceTypes.
    Case(
        name="EFAFirstPass",
        reason="With nothing observed, an EKSCluster with an EFA pool composes an EFA launch template, still without security groups, and an EFA security group with its rules.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h200",
                            role="GPU",
                            instanceType="p5en.48xlarge",
                            nodeCount=2,
                            minNodeCount=0,
                            maxNodeCount=2,
                            diskSizeGb=1024,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h200"),
                            fabric="EFA",
                            zones=[v1alpha1.Zone("us-west-2a")],
                        ),
                    ],
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "gpu-h200", "state": "Provisioning"}]),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "internet-gateway": _internet_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nat-eip": _nat_eip(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nat-gateway": _nat_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-table": _route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-default": _route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "private-route-table": _private_route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-cluster": _cluster_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-node": _node_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "launch-template-gpu-h200": _launch_template_efa(security_groups=None),
                    "efa-security-group": _efa_security_group(),
                    "efa-security-group-ingress": _efa_security_group_ingress(),
                    "efa-security-group-egress": _efa_security_group_egress(),
                    "nodegroup-gpu-h200": _nodegroup_gpu_efa(),
                    "addon-vpc-cni": _addon(
                        name="vpc-cni",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-coredns": _addon(
                        name="coredns",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group": _efs_security_group(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
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
                    message="2 of 52 resources ready",
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
    # Both security group IDs are observed, the EFA group's in its external name
    # and the cluster group's in the cluster's status, so every interface carries
    # both, EFA first, and none asks for a public IP. The cluster is observed but
    # not Ready, so both Helm releases are composed and the cluster isn't marked
    # ready.
    Case(
        name="SecurityGroupsObserved",
        reason="With the EFA security group and the cluster observed, an EKSCluster puts both their security group ids on every EFA interface in the launch template.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h200",
                            role="GPU",
                            instanceType="p5en.48xlarge",
                            nodeCount=2,
                            minNodeCount=0,
                            maxNodeCount=2,
                            diskSizeGb=1024,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h200"),
                            fabric="EFA",
                            zones=[v1alpha1.Zone("us-west-2a")],
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_eks_cluster(cluster_security_group_id="sg-0cluster", ready_condition=False),
                    "efa-security-group": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                                "kind": "SecurityGroup",
                                "metadata": {
                                    "name": "test-cluster-efa-sg-602a9",
                                    "labels": {"modelplane.ai/fabric": "EFA"},
                                    "annotations": {"crossplane.io/external-name": "sg-0efa"},
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "forProvider": {
                                        "region": "us-west-2",
                                        "name": "test-cluster-efa",
                                        "description": "EFA OS-bypass traffic between gang nodes",
                                        "vpcIdSelector": {"matchControllerRef": True},
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
                composite=_desired_xr(node_pools=[{"name": "gpu-h200", "state": "Provisioning"}]),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "internet-gateway": _internet_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nat-eip": _nat_eip(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nat-gateway": _nat_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-table": _route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-default": _route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "private-route-table": _private_route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-cluster": _cluster_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-node": _node_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "launch-template-gpu-h200": _launch_template_efa(security_groups=["sg-0efa", "sg-0cluster"]),
                    "efa-security-group": _efa_security_group(),
                    "efa-security-group-ingress": _efa_security_group_ingress(),
                    "efa-security-group-egress": _efa_security_group_egress(),
                    "nodegroup-gpu-h200": _nodegroup_gpu_efa(),
                    "addon-vpc-cni": _addon(
                        name="vpc-cni",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-coredns": _addon(
                        name="coredns",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group": _efs_security_group(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "release-cluster-autoscaler": _autoscaler_release(ready=fnv1.READY_UNSPECIFIED),
                    "release-efa-dra-driver": fnv1.Resource(
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
                                            "name": "aws-dranet",
                                            "repository": "https://aws.github.io/eks-charts",
                                            "version": "1.0.0",
                                        },
                                        "namespace": "kube-system",
                                        "values": {
                                            "tolerations": [
                                                {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                                            ],
                                        },
                                    },
                                },
                            }
                        )
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
                    message="2 of 54 resources ready",
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
    # Like the autoscaler, the EFA DRA driver release is gated on the cluster being
    # observed so provider-helm can reach it.
    Case(
        name="EFAClusterObserved",
        reason="With the cluster observed, an EKSCluster with an EFA pool composes the EFA DRA driver release alongside the autoscaler release.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h200",
                            role="GPU",
                            instanceType="p5en.48xlarge",
                            nodeCount=2,
                            minNodeCount=0,
                            maxNodeCount=2,
                            diskSizeGb=1024,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h200"),
                            fabric="EFA",
                            zones=[v1alpha1.Zone("us-west-2a")],
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_eks_cluster(cluster_security_group_id=None, ready_condition=True),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "gpu-h200", "state": "Provisioning"}]),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "internet-gateway": _internet_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nat-eip": _nat_eip(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nat-gateway": _nat_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-table": _route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-default": _route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "private-route-table": _private_route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-cluster": _cluster_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-node": _node_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "launch-template-gpu-h200": _launch_template_efa(security_groups=None),
                    "efa-security-group": _efa_security_group(),
                    "efa-security-group-ingress": _efa_security_group_ingress(),
                    "efa-security-group-egress": _efa_security_group_egress(),
                    "nodegroup-gpu-h200": _nodegroup_gpu_efa(),
                    "addon-vpc-cni": _addon(
                        name="vpc-cni",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-coredns": _addon(
                        name="coredns",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group": _efs_security_group(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "release-cluster-autoscaler": _autoscaler_release(ready=fnv1.READY_UNSPECIFIED),
                    "release-efa-dra-driver": fnv1.Resource(
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
                                            "name": "aws-dranet",
                                            "repository": "https://aws.github.io/eks-charts",
                                            "version": "1.0.0",
                                        },
                                        "namespace": "kube-system",
                                        "values": {
                                            "tolerations": [
                                                {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
                                            ],
                                        },
                                    },
                                },
                            }
                        )
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
                    message="3 of 54 resources ready",
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
        name="NoFabricClusterObserved",
        reason="With the cluster observed, an EKSCluster whose pool has no fabric composes the autoscaler release but no EFA DRA driver release.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l4",
                            role="GPU",
                            instanceType="g6.xlarge",
                            nodeCount=1,
                            minNodeCount=0,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l4"),
                            zones=[v1alpha1.Zone("us-west-2a"), v1alpha1.Zone("us-west-2b")],
                        ),
                    ],
                ),
                resources={
                    "cluster": _observed_eks_cluster(cluster_security_group_id=None, ready_condition=True),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "gpu-l4", "state": "Provisioning"}]),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "internet-gateway": _internet_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nat-eip": _nat_eip(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nat-gateway": _nat_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-table": _route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-default": _route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "private-route-table": _private_route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-cluster": _cluster_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-node": _node_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-gpu-l4": _nodegroup_gpu(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "addon-vpc-cni": _addon(
                        name="vpc-cni",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-coredns": _addon(
                        name="coredns",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group": _efs_security_group(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "release-cluster-autoscaler": _autoscaler_release(ready=fnv1.READY_UNSPECIFIED),
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
                    message="3 of 50 resources ready",
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
    # The ProviderConfigs reach the cluster through its kubeconfig, so they don't
    # carry the cloud credentials.
    Case(
        name="CustomCredentials",
        reason="The ProviderConfig an EKSCluster's spec.credentials names becomes every AWS managed resource's providerConfigRef.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=v1alpha1.Credentials(type="ProviderConfig", name="my-aws-account"),
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l4",
                            role="GPU",
                            instanceType="g6.xlarge",
                            nodeCount=1,
                            minNodeCount=0,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l4"),
                            zones=[v1alpha1.Zone("us-west-2a"), v1alpha1.Zone("us-west-2b")],
                        ),
                    ],
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "gpu-l4", "state": "Provisioning"}]),
                resources={
                    "vpc": _vpc(cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "internet-gateway": _internet_gateway(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nat-eip": _nat_eip(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nat-gateway": _nat_gateway(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-table": _route_table(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-default": _route_default(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "private-route-table": _private_route_table(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "private-route-default": _private_route_default(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-cluster": _cluster_role(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-node": _node_role(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-gpu-l4": _nodegroup_gpu(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "addon-vpc-cni": _addon(
                        name="vpc-cni",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-coredns": _addon(
                        name="coredns",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group": _efs_security_group(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
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
                    message="2 of 49 resources ready",
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
        name="PoolsProvisioning",
        reason="With every cluster resource observed Ready and the gpu-l4 node group unready, ClusterReady is Available, NodePoolsReady is Provisioning and gpu-l4 is Provisioning.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l4",
                            role="GPU",
                            instanceType="g6.xlarge",
                            nodeCount=1,
                            minNodeCount=0,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l4"),
                            zones=[v1alpha1.Zone("us-west-2a"), v1alpha1.Zone("us-west-2b")],
                        ),
                    ],
                ),
                resources={
                    "vpc": _observed_resource(ready=True),
                    "subnet-0": _observed_resource(ready=True),
                    "subnet-1": _observed_resource(ready=True),
                    "subnet-2": _observed_resource(ready=True),
                    "private-subnet-0": _observed_resource(ready=True),
                    "private-subnet-1": _observed_resource(ready=True),
                    "private-subnet-2": _observed_resource(ready=True),
                    "internet-gateway": _observed_resource(ready=True),
                    "nat-eip": _observed_resource(ready=True),
                    "nat-gateway": _observed_resource(ready=True),
                    "route-table": _observed_resource(ready=True),
                    "route-default": _observed_resource(ready=True),
                    "private-route-table": _observed_resource(ready=True),
                    "private-route-default": _observed_resource(ready=True),
                    "route-table-association-0": _observed_resource(ready=True),
                    "route-table-association-1": _observed_resource(ready=True),
                    "route-table-association-2": _observed_resource(ready=True),
                    "private-route-table-association-0": _observed_resource(ready=True),
                    "private-route-table-association-1": _observed_resource(ready=True),
                    "private-route-table-association-2": _observed_resource(ready=True),
                    "iam-role-cluster": _observed_resource(ready=True),
                    "iam-attach-cluster-policy": _observed_resource(ready=True),
                    "iam-role-node": _observed_resource(ready=True),
                    "iam-attach-node-worker": _observed_resource(ready=True),
                    "iam-attach-node-cni": _observed_resource(ready=True),
                    "iam-attach-node-ecr": _observed_resource(ready=True),
                    "cluster": _observed_eks_cluster(cluster_security_group_id=None, ready_condition=True),
                    "cluster-auth": _observed_resource(ready=True),
                    "nodegroup-system": _observed_resource(ready=True),
                    "addon-vpc-cni": _observed_resource(ready=True),
                    "addon-kube-proxy": _observed_resource(ready=True),
                    "addon-coredns": _observed_resource(ready=True),
                    "efs-filesystem": _observed_efs_filesystem(ready=True),
                    "efs-security-group": _observed_resource(ready=True),
                    "efs-security-group-ingress": _observed_resource(ready=True),
                    "efs-mount-target-0": _observed_resource(ready=True),
                    "efs-mount-target-1": _observed_resource(ready=True),
                    "efs-mount-target-2": _observed_resource(ready=True),
                    "iam-role-efs-csi": _observed_resource(ready=True),
                    "iam-attach-efs-csi": _observed_resource(ready=True),
                    "addon-eks-pod-identity-agent": _observed_resource(ready=True),
                    "pod-identity-efs-csi": _observed_resource(ready=True),
                    "addon-aws-efs-csi-driver": _observed_resource(ready=True),
                    "iam-policy-cluster-autoscaler": _observed_resource(ready=True),
                    "iam-role-cluster-autoscaler": _observed_resource(ready=True),
                    "iam-attach-cluster-autoscaler": _observed_resource(ready=True),
                    "pod-identity-cluster-autoscaler": _observed_resource(ready=True),
                    "release-cluster-autoscaler": _observed_resource(ready=True),
                    "nodegroup-gpu-l4": _observed_resource(ready=False),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "gpu-l4", "state": "Provisioning"}]),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "internet-gateway": _internet_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "nat-eip": _nat_eip(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "nat-gateway": _nat_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table": _route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-default": _route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table": _private_route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-role-cluster": _cluster_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-role-node": _node_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "nodegroup-gpu-l4": _nodegroup_gpu(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "addon-vpc-cni": _addon(
                        name="vpc-cni", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "addon-coredns": _addon(
                        name="coredns", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-security-group": _efs_security_group(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "storage-class-rwx-efs": _storage_class_rwx_efs(),
                    "release-cluster-autoscaler": _autoscaler_release(ready=fnv1.READY_TRUE),
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
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Provisioning",
                    message="0 of 1 node pools ready",
                ),
            ],
        ),
    ),
    Case(
        name="AllReady",
        reason="With every composed resource observed Ready, ClusterReady and NodePoolsReady are Available and gpu-l4 is Ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l4",
                            role="GPU",
                            instanceType="g6.xlarge",
                            nodeCount=1,
                            minNodeCount=0,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l4"),
                            zones=[v1alpha1.Zone("us-west-2a"), v1alpha1.Zone("us-west-2b")],
                        ),
                    ],
                ),
                resources={
                    "vpc": _observed_resource(ready=True),
                    "subnet-0": _observed_resource(ready=True),
                    "subnet-1": _observed_resource(ready=True),
                    "subnet-2": _observed_resource(ready=True),
                    "private-subnet-0": _observed_resource(ready=True),
                    "private-subnet-1": _observed_resource(ready=True),
                    "private-subnet-2": _observed_resource(ready=True),
                    "internet-gateway": _observed_resource(ready=True),
                    "nat-eip": _observed_resource(ready=True),
                    "nat-gateway": _observed_resource(ready=True),
                    "route-table": _observed_resource(ready=True),
                    "route-default": _observed_resource(ready=True),
                    "private-route-table": _observed_resource(ready=True),
                    "private-route-default": _observed_resource(ready=True),
                    "route-table-association-0": _observed_resource(ready=True),
                    "route-table-association-1": _observed_resource(ready=True),
                    "route-table-association-2": _observed_resource(ready=True),
                    "private-route-table-association-0": _observed_resource(ready=True),
                    "private-route-table-association-1": _observed_resource(ready=True),
                    "private-route-table-association-2": _observed_resource(ready=True),
                    "iam-role-cluster": _observed_resource(ready=True),
                    "iam-attach-cluster-policy": _observed_resource(ready=True),
                    "iam-role-node": _observed_resource(ready=True),
                    "iam-attach-node-worker": _observed_resource(ready=True),
                    "iam-attach-node-cni": _observed_resource(ready=True),
                    "iam-attach-node-ecr": _observed_resource(ready=True),
                    "cluster": _observed_eks_cluster(cluster_security_group_id=None, ready_condition=True),
                    "cluster-auth": _observed_resource(ready=True),
                    "nodegroup-system": _observed_resource(ready=True),
                    "addon-vpc-cni": _observed_resource(ready=True),
                    "addon-kube-proxy": _observed_resource(ready=True),
                    "addon-coredns": _observed_resource(ready=True),
                    "efs-filesystem": _observed_efs_filesystem(ready=True),
                    "efs-security-group": _observed_resource(ready=True),
                    "efs-security-group-ingress": _observed_resource(ready=True),
                    "efs-mount-target-0": _observed_resource(ready=True),
                    "efs-mount-target-1": _observed_resource(ready=True),
                    "efs-mount-target-2": _observed_resource(ready=True),
                    "iam-role-efs-csi": _observed_resource(ready=True),
                    "iam-attach-efs-csi": _observed_resource(ready=True),
                    "addon-eks-pod-identity-agent": _observed_resource(ready=True),
                    "pod-identity-efs-csi": _observed_resource(ready=True),
                    "addon-aws-efs-csi-driver": _observed_resource(ready=True),
                    "iam-policy-cluster-autoscaler": _observed_resource(ready=True),
                    "iam-role-cluster-autoscaler": _observed_resource(ready=True),
                    "iam-attach-cluster-autoscaler": _observed_resource(ready=True),
                    "pod-identity-cluster-autoscaler": _observed_resource(ready=True),
                    "release-cluster-autoscaler": _observed_resource(ready=True),
                    "nodegroup-gpu-l4": _observed_resource(ready=True),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "gpu-l4", "state": "Ready"}]),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "internet-gateway": _internet_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "nat-eip": _nat_eip(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "nat-gateway": _nat_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table": _route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-default": _route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table": _private_route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-role-cluster": _cluster_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-role-node": _node_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "nodegroup-gpu-l4": _nodegroup_gpu(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "addon-vpc-cni": _addon(
                        name="vpc-cni", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "addon-coredns": _addon(
                        name="coredns", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-security-group": _efs_security_group(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "storage-class-rwx-efs": _storage_class_rwx_efs(),
                    "release-cluster-autoscaler": _autoscaler_release(ready=fnv1.READY_TRUE),
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
    Case(
        name="PoolFailed",
        reason="With the gpu-l4 node group's LastAsyncOperation False, gpu-l4 is Failed with the provider's error prefixed by the resource name, and NodePoolsReady is Failed naming the pool.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l4",
                            role="GPU",
                            instanceType="g6.xlarge",
                            nodeCount=1,
                            minNodeCount=0,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l4"),
                            zones=[v1alpha1.Zone("us-west-2a"), v1alpha1.Zone("us-west-2b")],
                        ),
                    ],
                ),
                resources={
                    "vpc": _observed_resource(ready=True),
                    "subnet-0": _observed_resource(ready=True),
                    "subnet-1": _observed_resource(ready=True),
                    "subnet-2": _observed_resource(ready=True),
                    "private-subnet-0": _observed_resource(ready=True),
                    "private-subnet-1": _observed_resource(ready=True),
                    "private-subnet-2": _observed_resource(ready=True),
                    "internet-gateway": _observed_resource(ready=True),
                    "nat-eip": _observed_resource(ready=True),
                    "nat-gateway": _observed_resource(ready=True),
                    "route-table": _observed_resource(ready=True),
                    "route-default": _observed_resource(ready=True),
                    "private-route-table": _observed_resource(ready=True),
                    "private-route-default": _observed_resource(ready=True),
                    "route-table-association-0": _observed_resource(ready=True),
                    "route-table-association-1": _observed_resource(ready=True),
                    "route-table-association-2": _observed_resource(ready=True),
                    "private-route-table-association-0": _observed_resource(ready=True),
                    "private-route-table-association-1": _observed_resource(ready=True),
                    "private-route-table-association-2": _observed_resource(ready=True),
                    "iam-role-cluster": _observed_resource(ready=True),
                    "iam-attach-cluster-policy": _observed_resource(ready=True),
                    "iam-role-node": _observed_resource(ready=True),
                    "iam-attach-node-worker": _observed_resource(ready=True),
                    "iam-attach-node-cni": _observed_resource(ready=True),
                    "iam-attach-node-ecr": _observed_resource(ready=True),
                    "cluster": _observed_eks_cluster(cluster_security_group_id=None, ready_condition=True),
                    "cluster-auth": _observed_resource(ready=True),
                    "nodegroup-system": _observed_resource(ready=True),
                    "addon-vpc-cni": _observed_resource(ready=True),
                    "addon-kube-proxy": _observed_resource(ready=True),
                    "addon-coredns": _observed_resource(ready=True),
                    "efs-filesystem": _observed_efs_filesystem(ready=True),
                    "efs-security-group": _observed_resource(ready=True),
                    "efs-security-group-ingress": _observed_resource(ready=True),
                    "efs-mount-target-0": _observed_resource(ready=True),
                    "efs-mount-target-1": _observed_resource(ready=True),
                    "efs-mount-target-2": _observed_resource(ready=True),
                    "iam-role-efs-csi": _observed_resource(ready=True),
                    "iam-attach-efs-csi": _observed_resource(ready=True),
                    "addon-eks-pod-identity-agent": _observed_resource(ready=True),
                    "pod-identity-efs-csi": _observed_resource(ready=True),
                    "addon-aws-efs-csi-driver": _observed_resource(ready=True),
                    "iam-policy-cluster-autoscaler": _observed_resource(ready=True),
                    "iam-role-cluster-autoscaler": _observed_resource(ready=True),
                    "iam-attach-cluster-autoscaler": _observed_resource(ready=True),
                    "pod-identity-cluster-autoscaler": _observed_resource(ready=True),
                    "release-cluster-autoscaler": _observed_resource(ready=True),
                    "nodegroup-gpu-l4": _observed_failed(
                        message="async create failed: waiting for EKS Node Group (test-cluster:gpu-l4) create: unexpected state CREATE_FAILED, AsgInstanceLaunchFailures: InsufficientInstanceCapacity"
                    ),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(
                    node_pools=[
                        {
                            "name": "gpu-l4",
                            "state": "Failed",
                            "message": "nodegroup-gpu-l4: async create failed: waiting for EKS Node Group (test-cluster:gpu-l4) create: unexpected state CREATE_FAILED, AsgInstanceLaunchFailures: InsufficientInstanceCapacity",
                        }
                    ]
                ),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "internet-gateway": _internet_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "nat-eip": _nat_eip(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "nat-gateway": _nat_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table": _route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-default": _route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table": _private_route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-role-cluster": _cluster_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-role-node": _node_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "nodegroup-gpu-l4": _nodegroup_gpu(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "addon-vpc-cni": _addon(
                        name="vpc-cni", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "addon-coredns": _addon(
                        name="coredns", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-security-group": _efs_security_group(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "storage-class-rwx-efs": _storage_class_rwx_efs(),
                    "release-cluster-autoscaler": _autoscaler_release(ready=fnv1.READY_TRUE),
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
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Failed",
                    message="1 of 1 node pools failed: gpu-l4",
                ),
            ],
        ),
    ),
    Case(
        name="LaunchTemplateFailed",
        reason="With a Capacity Block pool's launch template's LastAsyncOperation False, the launch template counts toward gpu-h200, so gpu-h200 is Failed with its error and NodePoolsReady is Failed while ClusterReady is Available.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-h200",
                            role="GPU",
                            instanceType="p5en.48xlarge",
                            nodeCount=2,
                            minNodeCount=0,
                            maxNodeCount=2,
                            diskSizeGb=1024,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h200"),
                            capacityReservation=v1alpha1.CapacityReservation(
                                type="CapacityBlock", id="cr-0123456789abcdef0"
                            ),
                            zones=[v1alpha1.Zone("us-west-2a")],
                        ),
                    ],
                ),
                resources={
                    "vpc": _observed_resource(ready=True),
                    "subnet-0": _observed_resource(ready=True),
                    "subnet-1": _observed_resource(ready=True),
                    "subnet-2": _observed_resource(ready=True),
                    "private-subnet-0": _observed_resource(ready=True),
                    "private-subnet-1": _observed_resource(ready=True),
                    "private-subnet-2": _observed_resource(ready=True),
                    "internet-gateway": _observed_resource(ready=True),
                    "nat-eip": _observed_resource(ready=True),
                    "nat-gateway": _observed_resource(ready=True),
                    "route-table": _observed_resource(ready=True),
                    "route-default": _observed_resource(ready=True),
                    "private-route-table": _observed_resource(ready=True),
                    "private-route-default": _observed_resource(ready=True),
                    "route-table-association-0": _observed_resource(ready=True),
                    "route-table-association-1": _observed_resource(ready=True),
                    "route-table-association-2": _observed_resource(ready=True),
                    "private-route-table-association-0": _observed_resource(ready=True),
                    "private-route-table-association-1": _observed_resource(ready=True),
                    "private-route-table-association-2": _observed_resource(ready=True),
                    "iam-role-cluster": _observed_resource(ready=True),
                    "iam-attach-cluster-policy": _observed_resource(ready=True),
                    "iam-role-node": _observed_resource(ready=True),
                    "iam-attach-node-worker": _observed_resource(ready=True),
                    "iam-attach-node-cni": _observed_resource(ready=True),
                    "iam-attach-node-ecr": _observed_resource(ready=True),
                    "cluster": _observed_eks_cluster(cluster_security_group_id=None, ready_condition=True),
                    "cluster-auth": _observed_resource(ready=True),
                    "nodegroup-system": _observed_resource(ready=True),
                    "addon-vpc-cni": _observed_resource(ready=True),
                    "addon-kube-proxy": _observed_resource(ready=True),
                    "addon-coredns": _observed_resource(ready=True),
                    "efs-filesystem": _observed_efs_filesystem(ready=True),
                    "efs-security-group": _observed_resource(ready=True),
                    "efs-security-group-ingress": _observed_resource(ready=True),
                    "efs-mount-target-0": _observed_resource(ready=True),
                    "efs-mount-target-1": _observed_resource(ready=True),
                    "efs-mount-target-2": _observed_resource(ready=True),
                    "iam-role-efs-csi": _observed_resource(ready=True),
                    "iam-attach-efs-csi": _observed_resource(ready=True),
                    "addon-eks-pod-identity-agent": _observed_resource(ready=True),
                    "pod-identity-efs-csi": _observed_resource(ready=True),
                    "addon-aws-efs-csi-driver": _observed_resource(ready=True),
                    "iam-policy-cluster-autoscaler": _observed_resource(ready=True),
                    "iam-role-cluster-autoscaler": _observed_resource(ready=True),
                    "iam-attach-cluster-autoscaler": _observed_resource(ready=True),
                    "pod-identity-cluster-autoscaler": _observed_resource(ready=True),
                    "release-cluster-autoscaler": _observed_resource(ready=True),
                    "launch-template-gpu-h200": _observed_failed(
                        message="async create failed: creating EC2 Launch Template (test-cluster-gpu-h200): operation error EC2: CreateLaunchTemplate, InvalidCapacityReservationId.NotFound"
                    ),
                    "nodegroup-gpu-h200": _observed_resource(ready=False),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(
                    node_pools=[
                        {
                            "name": "gpu-h200",
                            "state": "Failed",
                            "message": "launch-template-gpu-h200: async create failed: creating EC2 Launch Template (test-cluster-gpu-h200): operation error EC2: CreateLaunchTemplate, InvalidCapacityReservationId.NotFound",
                        }
                    ]
                ),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "internet-gateway": _internet_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "nat-eip": _nat_eip(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "nat-gateway": _nat_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table": _route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-default": _route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table": _private_route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-role-cluster": _cluster_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-role-node": _node_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "launch-template-gpu-h200": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
                                "kind": "LaunchTemplate",
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "forProvider": {
                                        "region": "us-west-2",
                                        "name": "test-cluster-lt-gpu-h200-83c00",
                                        "instanceType": "p5en.48xlarge",
                                        "blockDeviceMappings": [
                                            {"deviceName": "/dev/xvda", "ebs": {"volumeSize": 1024}},
                                        ],
                                        "instanceMarketOptions": {"marketType": "capacity-block"},
                                        "capacityReservationSpecification": {
                                            "capacityReservationPreference": "capacity-reservations-only",
                                            "capacityReservationTarget": {
                                                "capacityReservationId": "cr-0123456789abcdef0",
                                            },
                                        },
                                    },
                                },
                            }
                        ),
                    ),
                    "nodegroup-gpu-h200": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "eks.aws.m.upbound.io/v1beta1",
                                "kind": "NodeGroup",
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "managementPolicies": ["Observe", "Create", "Update", "Delete"],
                                    "initProvider": {"scalingConfig": {"desiredSize": 2}},
                                    "forProvider": {
                                        "region": "us-west-2",
                                        "amiType": "AL2023_x86_64_NVIDIA",
                                        "clusterNameSelector": {"matchControllerRef": True},
                                        "nodeRoleArnSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/iam-role": "node"},
                                        },
                                        "capacityType": "CAPACITY_BLOCK",
                                        "launchTemplate": {
                                            "name": "test-cluster-lt-gpu-h200-83c00",
                                            "version": "$Latest",
                                        },
                                        "subnetIdRefs": [{"name": "test-cluster-private-subnet-us-west-2a-6a89f"}],
                                        "scalingConfig": {"minSize": 0, "maxSize": 2},
                                        "labels": {
                                            "modelplane.ai/gpu": "nvidia-h200",
                                            "modelplane.ai/pool": "gpu-h200",
                                        },
                                        "taint": [
                                            {
                                                "key": "nvidia.com/gpu",
                                                "value": "true",
                                                "effect": "NO_SCHEDULE",
                                            },
                                        ],
                                    },
                                },
                            }
                        ),
                    ),
                    "addon-vpc-cni": _addon(
                        name="vpc-cni", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "addon-coredns": _addon(
                        name="coredns", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-security-group": _efs_security_group(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "storage-class-rwx-efs": _storage_class_rwx_efs(),
                    "release-cluster-autoscaler": _autoscaler_release(ready=fnv1.READY_TRUE),
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
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Failed",
                    message="1 of 1 node pools failed: gpu-h200",
                ),
            ],
        ),
    ),
    Case(
        name="ClusterFailed",
        reason="With the NAT gateway's LastAsyncOperation False, ClusterReady is Failed with the provider's error prefixed by the resource name, and NodePoolsReady is Pending on it while no pool is Ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l4",
                            role="GPU",
                            instanceType="g6.xlarge",
                            nodeCount=1,
                            minNodeCount=0,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l4"),
                            zones=[v1alpha1.Zone("us-west-2a"), v1alpha1.Zone("us-west-2b")],
                        ),
                    ],
                ),
                resources={
                    "vpc": _observed_resource(ready=True),
                    "subnet-0": _observed_resource(ready=True),
                    "subnet-1": _observed_resource(ready=True),
                    "subnet-2": _observed_resource(ready=True),
                    "private-subnet-0": _observed_resource(ready=True),
                    "private-subnet-1": _observed_resource(ready=True),
                    "private-subnet-2": _observed_resource(ready=True),
                    "internet-gateway": _observed_resource(ready=True),
                    "nat-eip": _observed_resource(ready=True),
                    "nat-gateway": _observed_failed(
                        message="async create failed: creating EC2 NAT Gateway: operation error EC2: CreateNatGateway, NatGatewayLimitExceeded"
                    ),
                    "route-table": _observed_resource(ready=True),
                    "route-default": _observed_resource(ready=True),
                    "private-route-table": _observed_resource(ready=True),
                    "private-route-default": _observed_resource(ready=True),
                    "route-table-association-0": _observed_resource(ready=True),
                    "route-table-association-1": _observed_resource(ready=True),
                    "route-table-association-2": _observed_resource(ready=True),
                    "private-route-table-association-0": _observed_resource(ready=True),
                    "private-route-table-association-1": _observed_resource(ready=True),
                    "private-route-table-association-2": _observed_resource(ready=True),
                    "iam-role-cluster": _observed_resource(ready=True),
                    "iam-attach-cluster-policy": _observed_resource(ready=True),
                    "iam-role-node": _observed_resource(ready=True),
                    "iam-attach-node-worker": _observed_resource(ready=True),
                    "iam-attach-node-cni": _observed_resource(ready=True),
                    "iam-attach-node-ecr": _observed_resource(ready=True),
                    "cluster": _observed_eks_cluster(cluster_security_group_id=None, ready_condition=True),
                    "cluster-auth": _observed_resource(ready=True),
                    "nodegroup-system": _observed_resource(ready=True),
                    "addon-vpc-cni": _observed_resource(ready=True),
                    "addon-kube-proxy": _observed_resource(ready=True),
                    "addon-coredns": _observed_resource(ready=True),
                    "efs-filesystem": _observed_efs_filesystem(ready=True),
                    "efs-security-group": _observed_resource(ready=True),
                    "efs-security-group-ingress": _observed_resource(ready=True),
                    "efs-mount-target-0": _observed_resource(ready=True),
                    "efs-mount-target-1": _observed_resource(ready=True),
                    "efs-mount-target-2": _observed_resource(ready=True),
                    "iam-role-efs-csi": _observed_resource(ready=True),
                    "iam-attach-efs-csi": _observed_resource(ready=True),
                    "addon-eks-pod-identity-agent": _observed_resource(ready=True),
                    "pod-identity-efs-csi": _observed_resource(ready=True),
                    "addon-aws-efs-csi-driver": _observed_resource(ready=True),
                    "iam-policy-cluster-autoscaler": _observed_resource(ready=True),
                    "iam-role-cluster-autoscaler": _observed_resource(ready=True),
                    "iam-attach-cluster-autoscaler": _observed_resource(ready=True),
                    "pod-identity-cluster-autoscaler": _observed_resource(ready=True),
                    "release-cluster-autoscaler": _observed_resource(ready=True),
                    "nodegroup-gpu-l4": _observed_resource(ready=False),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "gpu-l4", "state": "Provisioning"}]),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "internet-gateway": _internet_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "nat-eip": _nat_eip(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "nat-gateway": _nat_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "route-table": _route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-default": _route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table": _private_route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-role-cluster": _cluster_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-role-node": _node_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "nodegroup-gpu-l4": _nodegroup_gpu(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "addon-vpc-cni": _addon(
                        name="vpc-cni", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "addon-coredns": _addon(
                        name="coredns", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-security-group": _efs_security_group(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "storage-class-rwx-efs": _storage_class_rwx_efs(),
                    "release-cluster-autoscaler": _autoscaler_release(ready=fnv1.READY_TRUE),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                },
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Failed",
                    message="nat-gateway: async create failed: creating EC2 NAT Gateway: operation error EC2: CreateNatGateway, NatGatewayLimitExceeded",
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
        name="ClusterUnreadyPoolReady",
        reason="With the coredns addon unready and the gpu-l4 node group Ready, ClusterReady is Provisioning and NodePoolsReady reports gpu-l4's own state, Available, rather than Pending.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pools=[
                        v1alpha1.NodePool(
                            name="gpu-l4",
                            role="GPU",
                            instanceType="g6.xlarge",
                            nodeCount=1,
                            minNodeCount=0,
                            maxNodeCount=4,
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-l4"),
                            zones=[v1alpha1.Zone("us-west-2a"), v1alpha1.Zone("us-west-2b")],
                        ),
                    ],
                ),
                resources={
                    "vpc": _observed_resource(ready=True),
                    "subnet-0": _observed_resource(ready=True),
                    "subnet-1": _observed_resource(ready=True),
                    "subnet-2": _observed_resource(ready=True),
                    "private-subnet-0": _observed_resource(ready=True),
                    "private-subnet-1": _observed_resource(ready=True),
                    "private-subnet-2": _observed_resource(ready=True),
                    "internet-gateway": _observed_resource(ready=True),
                    "nat-eip": _observed_resource(ready=True),
                    "nat-gateway": _observed_resource(ready=True),
                    "route-table": _observed_resource(ready=True),
                    "route-default": _observed_resource(ready=True),
                    "private-route-table": _observed_resource(ready=True),
                    "private-route-default": _observed_resource(ready=True),
                    "route-table-association-0": _observed_resource(ready=True),
                    "route-table-association-1": _observed_resource(ready=True),
                    "route-table-association-2": _observed_resource(ready=True),
                    "private-route-table-association-0": _observed_resource(ready=True),
                    "private-route-table-association-1": _observed_resource(ready=True),
                    "private-route-table-association-2": _observed_resource(ready=True),
                    "iam-role-cluster": _observed_resource(ready=True),
                    "iam-attach-cluster-policy": _observed_resource(ready=True),
                    "iam-role-node": _observed_resource(ready=True),
                    "iam-attach-node-worker": _observed_resource(ready=True),
                    "iam-attach-node-cni": _observed_resource(ready=True),
                    "iam-attach-node-ecr": _observed_resource(ready=True),
                    "cluster": _observed_eks_cluster(cluster_security_group_id=None, ready_condition=True),
                    "cluster-auth": _observed_resource(ready=True),
                    "nodegroup-system": _observed_resource(ready=True),
                    "addon-vpc-cni": _observed_resource(ready=True),
                    "addon-kube-proxy": _observed_resource(ready=True),
                    "addon-coredns": _observed_resource(ready=False),
                    "efs-filesystem": _observed_efs_filesystem(ready=True),
                    "efs-security-group": _observed_resource(ready=True),
                    "efs-security-group-ingress": _observed_resource(ready=True),
                    "efs-mount-target-0": _observed_resource(ready=True),
                    "efs-mount-target-1": _observed_resource(ready=True),
                    "efs-mount-target-2": _observed_resource(ready=True),
                    "iam-role-efs-csi": _observed_resource(ready=True),
                    "iam-attach-efs-csi": _observed_resource(ready=True),
                    "addon-eks-pod-identity-agent": _observed_resource(ready=True),
                    "pod-identity-efs-csi": _observed_resource(ready=True),
                    "addon-aws-efs-csi-driver": _observed_resource(ready=True),
                    "iam-policy-cluster-autoscaler": _observed_resource(ready=True),
                    "iam-role-cluster-autoscaler": _observed_resource(ready=True),
                    "iam-attach-cluster-autoscaler": _observed_resource(ready=True),
                    "pod-identity-cluster-autoscaler": _observed_resource(ready=True),
                    "release-cluster-autoscaler": _observed_resource(ready=True),
                    "nodegroup-gpu-l4": _observed_resource(ready=True),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(node_pools=[{"name": "gpu-l4", "state": "Ready"}]),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "internet-gateway": _internet_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "nat-eip": _nat_eip(cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE),
                    "nat-gateway": _nat_gateway(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table": _route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-default": _route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table": _private_route_table(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-role-cluster": _cluster_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-role-node": _node_role(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "nodegroup-system": _nodegroup_system(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "nodegroup-gpu-l4": _nodegroup_gpu(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "addon-vpc-cni": _addon(
                        name="vpc-cni", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "addon-coredns": _addon(
                        name="coredns",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_UNSPECIFIED,
                    ),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-security-group": _efs_security_group(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi", cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                        ready=fnv1.READY_TRUE,
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "storage-class-rwx-efs": _storage_class_rwx_efs(),
                    "release-cluster-autoscaler": _autoscaler_release(ready=fnv1.READY_TRUE),
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
                    message="50 of 51 resources ready",
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
    """RunFunction composes EKS cluster infrastructure."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
