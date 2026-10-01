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
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _xr(*, credentials: v1alpha1.Credentials | None, node_pool: v1alpha1.NodePool) -> fnv1.Resource:
    """The observed EKSCluster XR in us-west-2, with one node pool."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            v1alpha1.EKSCluster(
                metadata=metav1.ObjectMeta(name="test-cluster", namespace="modelplane-system"),
                spec=v1alpha1.Spec(
                    region="us-west-2",
                    credentials=credentials,
                    nodePools=[node_pool],
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


def _vpc(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _subnet(*, name: str, az: str, cidr: str, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _private_subnet(*, name: str, az: str, cidr: str, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _internet_gateway(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _route_table(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _route_default(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _nat_eip(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _nat_gateway(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _private_route_table(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _private_route_default(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _route_table_association(*, az: str, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _private_route_table_association(*, az: str, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _cluster_role(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _node_role(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _pod_identity_role(*, role: str, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _role_policy_attachment(*, role: str, arn: str, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _eks_cluster(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The desired EKS Cluster."""
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


def _observed_eks_cluster(*, cluster_security_group_id: str | None, ready: bool) -> fnv1.Resource:
    """The observed EKS Cluster, with a Ready condition if ready and its security group id if given."""
    status: dict = {}
    if cluster_security_group_id is not None:
        status["atProvider"] = {"vpcConfig": {"clusterSecurityGroupId": cluster_security_group_id}}
    if ready:
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
    """The desired ClusterAuth."""
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


def _system_node_group(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
    """The system node group every cluster gets."""
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
    )


def _gpu_node_group(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
    """The gpu-l4 node group, which needs no launch template."""
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
    """The desired EFA security group."""
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


def _gpu_node_group_efa() -> fnv1.Resource:
    """The gpu-h200 EFA node group, which takes its instance type from the launch template."""
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


def _addon(*, name: str, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _efs_filesystem(*, cred_kind: str, cred_name: str, ready: fnv1.Ready) -> fnv1.Resource:
    """The desired EFS filesystem backing ModelCache."""
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


def _efs_security_group(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _efs_security_group_ingress(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _efs_mount_target(*, subnet_name: str, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _pod_identity_association(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _autoscaler_policy(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _autoscaler_attachment(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _autoscaler_pod_identity(*, cred_kind: str, cred_name: str) -> fnv1.Resource:
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
    )


def _autoscaler_release() -> fnv1.Resource:
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


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


COMPOSE_CASES = [
    Case(
        name="first pass composes infra resources; only the ProviderConfigs are ready",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pool=v1alpha1.NodePool(
                        name="gpu-l4",
                        role="GPU",
                        instanceType="g6.xlarge",
                        nodeCount=1,
                        minNodeCount=0,
                        maxNodeCount=4,
                        gpu=v1alpha1.Gpu(acceleratorType="nvidia-l4"),
                        zones=[v1alpha1.Zone("us-west-2a"), v1alpha1.Zone("us-west-2b")],
                    ),
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "internet-gateway": _internet_gateway(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "nat-eip": _nat_eip(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "nat-gateway": _nat_gateway(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "route-table": _route_table(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "route-default": _route_default(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "private-route-table": _private_route_table(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-role-cluster": _cluster_role(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-role-node": _node_role(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-system": _system_node_group(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "nodegroup-gpu-l4": _gpu_node_group(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "addon-vpc-cni": _addon(name="vpc-cni", cred_kind="ClusterProviderConfig", cred_name="default"),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "addon-coredns": _addon(name="coredns", cred_kind="ClusterProviderConfig", cred_name="default"),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group": _efs_security_group(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # The cluster, its ClusterAuth and the EFS filesystem are observed Ready, so
    # the function marks them ready, alongside the ProviderConfigs it always marks
    # ready. The observed filesystem id lets it compose the StorageClass Object,
    # also ready, against the cluster's own ProviderConfig. The observed cluster
    # lets it compose the autoscaler Helm release, since provider-helm can now
    # reach the cluster. The release isn't observed yet, so it isn't ready.
    Case(
        name="second pass with observed cluster ready marks cluster resources ready",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pool=v1alpha1.NodePool(
                        name="gpu-l4",
                        role="GPU",
                        instanceType="g6.xlarge",
                        nodeCount=1,
                        minNodeCount=0,
                        maxNodeCount=4,
                        gpu=v1alpha1.Gpu(acceleratorType="nvidia-l4"),
                        zones=[v1alpha1.Zone("us-west-2a"), v1alpha1.Zone("us-west-2b")],
                    ),
                ),
                resources={
                    "cluster": _observed_eks_cluster(cluster_security_group_id=None, ready=True),
                    "cluster-auth": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "eks.aws.m.upbound.io/v1beta1",
                                "kind": "ClusterAuth",
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
                                    "forProvider": {
                                        "region": "us-west-2",
                                        "clusterNameSelector": {"matchControllerRef": True},
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
                    "efs-filesystem": fnv1.Resource(
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
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "internet-gateway": _internet_gateway(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "nat-eip": _nat_eip(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "nat-gateway": _nat_gateway(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "route-table": _route_table(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "route-default": _route_default(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "private-route-table": _private_route_table(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-role-cluster": _cluster_role(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-role-node": _node_role(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "nodegroup-system": _system_node_group(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "nodegroup-gpu-l4": _gpu_node_group(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "addon-vpc-cni": _addon(name="vpc-cni", cred_kind="ClusterProviderConfig", cred_name="default"),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "addon-coredns": _addon(name="coredns", cred_kind="ClusterProviderConfig", cred_name="default"),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "efs-security-group": _efs_security_group(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "storage-class-rwx-efs": fnv1.Resource(
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
                    ),
                    "release-cluster-autoscaler": _autoscaler_release(),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # The launch template targets the reservation through the capacity-block
    # market type. The node group sets capacityType CAPACITY_BLOCK and references
    # the launch template. It sets no instanceTypes, because EKS takes the type
    # from the launch template.
    Case(
        name="a Capacity Block pool composes a launch template and a CAPACITY_BLOCK node group",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pool=v1alpha1.NodePool(
                        name="gpu-h200",
                        role="GPU",
                        instanceType="p5en.48xlarge",
                        nodeCount=2,
                        minNodeCount=0,
                        maxNodeCount=2,
                        diskSizeGb=1024,
                        gpu=v1alpha1.Gpu(acceleratorType="nvidia-h200"),
                        capacityBlock=v1alpha1.CapacityBlock(capacityReservationId="cr-0123456789abcdef0"),
                        zones=[v1alpha1.Zone("us-west-2a")],
                    ),
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "internet-gateway": _internet_gateway(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "nat-eip": _nat_eip(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "nat-gateway": _nat_gateway(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "route-table": _route_table(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "route-default": _route_default(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "private-route-table": _private_route_table(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-role-cluster": _cluster_role(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-role-node": _node_role(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-system": _system_node_group(cred_kind="ClusterProviderConfig", cred_name="default"),
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
                    "addon-vpc-cni": _addon(name="vpc-cni", cred_kind="ClusterProviderConfig", cred_name="default"),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "addon-coredns": _addon(name="coredns", cred_kind="ClusterProviderConfig", cred_name="default"),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group": _efs_security_group(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # The node group's launch template carries one EFA interface per network card
    # (card 0 keeps device index 0 for the node's IP traffic, the rest device
    # index 1 for RDMA), the cluster gets an EFA security group with
    # self-referencing all-traffic ingress and egress rules, and the node group
    # references the launch template instead of setting instanceTypes.
    Case(
        name="an EFA pool composes EFA launch template, security group, and rules",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pool=v1alpha1.NodePool(
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
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "internet-gateway": _internet_gateway(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "nat-eip": _nat_eip(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "nat-gateway": _nat_gateway(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "route-table": _route_table(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "route-default": _route_default(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "private-route-table": _private_route_table(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-role-cluster": _cluster_role(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-role-node": _node_role(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-system": _system_node_group(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "launch-template-gpu-h200": _launch_template_efa(security_groups=None),
                    "efa-security-group": _efa_security_group(),
                    "efa-security-group-ingress": _efa_security_group_ingress(),
                    "efa-security-group-egress": _efa_security_group_egress(),
                    "nodegroup-gpu-h200": _gpu_node_group_efa(),
                    "addon-vpc-cni": _addon(name="vpc-cni", cred_kind="ClusterProviderConfig", cred_name="default"),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "addon-coredns": _addon(name="coredns", cred_kind="ClusterProviderConfig", cred_name="default"),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group": _efs_security_group(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # A launch template with networkInterfaces makes its security groups
    # authoritative, so the interfaces must carry both the EFA security group and
    # the EKS cluster security group or the node never joins. Both are set as raw
    # IDs in securityGroups (not securityGroupRefs): the provider's reference
    # resolver no-ops once that field is populated, so a ref mixed with a literal
    # would be dropped. The EFA group's ID comes from its observed external name,
    # the cluster group's from the observed cluster's status, so both appear only
    # once their resources report them. Every interface carries both, EFA first,
    # and none requests a public IP, because the nodes are in private subnets.
    # The cluster is observed, though not yet Ready, so both Helm releases are
    # composed.
    Case(
        name="once both security groups are observed, every EFA interface carries them",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pool=v1alpha1.NodePool(
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
                ),
                resources={
                    "cluster": _observed_eks_cluster(cluster_security_group_id="sg-0cluster", ready=False),
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
                composite=_desired_xr(),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "internet-gateway": _internet_gateway(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "nat-eip": _nat_eip(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "nat-gateway": _nat_gateway(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "route-table": _route_table(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "route-default": _route_default(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "private-route-table": _private_route_table(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-role-cluster": _cluster_role(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-role-node": _node_role(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-system": _system_node_group(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "launch-template-gpu-h200": _launch_template_efa(security_groups=["sg-0efa", "sg-0cluster"]),
                    "efa-security-group": _efa_security_group(),
                    "efa-security-group-ingress": _efa_security_group_ingress(),
                    "efa-security-group-egress": _efa_security_group_egress(),
                    "nodegroup-gpu-h200": _gpu_node_group_efa(),
                    "addon-vpc-cni": _addon(name="vpc-cni", cred_kind="ClusterProviderConfig", cred_name="default"),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "addon-coredns": _addon(name="coredns", cred_kind="ClusterProviderConfig", cred_name="default"),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group": _efs_security_group(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "release-cluster-autoscaler": _autoscaler_release(),
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
        ),
    ),
    # Like the autoscaler, the EFA DRA driver release is gated on the cluster being
    # observed so provider-helm can reach it.
    Case(
        name="an EFA pool installs the EFA DRA driver once the cluster is observed",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pool=v1alpha1.NodePool(
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
                ),
                resources={
                    "cluster": _observed_eks_cluster(cluster_security_group_id=None, ready=True),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "internet-gateway": _internet_gateway(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "nat-eip": _nat_eip(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "nat-gateway": _nat_gateway(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "route-table": _route_table(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "route-default": _route_default(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "private-route-table": _private_route_table(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-role-cluster": _cluster_role(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-role-node": _node_role(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-system": _system_node_group(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "launch-template-gpu-h200": _launch_template_efa(security_groups=None),
                    "efa-security-group": _efa_security_group(),
                    "efa-security-group-ingress": _efa_security_group_ingress(),
                    "efa-security-group-egress": _efa_security_group_egress(),
                    "nodegroup-gpu-h200": _gpu_node_group_efa(),
                    "addon-vpc-cni": _addon(name="vpc-cni", cred_kind="ClusterProviderConfig", cred_name="default"),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "addon-coredns": _addon(name="coredns", cred_kind="ClusterProviderConfig", cred_name="default"),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group": _efs_security_group(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "release-cluster-autoscaler": _autoscaler_release(),
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
        ),
    ),
    Case(
        name="a pool without the EFA fabric installs no EFA DRA driver once the cluster is observed",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=None,
                    node_pool=v1alpha1.NodePool(
                        name="gpu-l4",
                        role="GPU",
                        instanceType="g6.xlarge",
                        nodeCount=1,
                        minNodeCount=0,
                        maxNodeCount=4,
                        gpu=v1alpha1.Gpu(acceleratorType="nvidia-l4"),
                        zones=[v1alpha1.Zone("us-west-2a"), v1alpha1.Zone("us-west-2b")],
                    ),
                ),
                resources={
                    "cluster": _observed_eks_cluster(cluster_security_group_id=None, ready=True),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(),
                resources={
                    "vpc": _vpc(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "internet-gateway": _internet_gateway(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "nat-eip": _nat_eip(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "nat-gateway": _nat_gateway(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "route-table": _route_table(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "route-default": _route_default(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "private-route-table": _private_route_table(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "private-route-default": _private_route_default(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-role-cluster": _cluster_role(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-role-node": _node_role(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_TRUE
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-system": _system_node_group(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "nodegroup-gpu-l4": _gpu_node_group(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "addon-vpc-cni": _addon(name="vpc-cni", cred_kind="ClusterProviderConfig", cred_name="default"),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "addon-coredns": _addon(name="coredns", cred_kind="ClusterProviderConfig", cred_name="default"),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ClusterProviderConfig", cred_name="default", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group": _efs_security_group(cred_kind="ClusterProviderConfig", cred_name="default"),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ClusterProviderConfig",
                        cred_name="default",
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler", cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ClusterProviderConfig", cred_name="default"
                    ),
                    "release-cluster-autoscaler": _autoscaler_release(),
                    "provider-config-kubernetes": _provider_config(api_version="kubernetes.m.crossplane.io/v1alpha1"),
                    "provider-config-helm": _provider_config(api_version="helm.m.crossplane.io/v1beta1"),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    # Every cloud provider MR carries the providerConfigRef that spec.credentials
    # names. The ProviderConfigs reach the cluster through its kubeconfig, so they
    # don't carry the cloud credentials.
    Case(
        name="custom credentials flow through to all cloud MRs",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_xr(
                    credentials=v1alpha1.Credentials(type="ProviderConfig", name="my-aws-account"),
                    node_pool=v1alpha1.NodePool(
                        name="gpu-l4",
                        role="GPU",
                        instanceType="g6.xlarge",
                        nodeCount=1,
                        minNodeCount=0,
                        maxNodeCount=4,
                        gpu=v1alpha1.Gpu(acceleratorType="nvidia-l4"),
                        zones=[v1alpha1.Zone("us-west-2a"), v1alpha1.Zone("us-west-2b")],
                    ),
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_xr(),
                resources={
                    "vpc": _vpc(cred_kind="ProviderConfig", cred_name="my-aws-account"),
                    "subnet-0": _subnet(
                        name="test-cluster-subnet-us-west-2a-952dc",
                        az="us-west-2a",
                        cidr="10.0.0.0/20",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                    ),
                    "subnet-1": _subnet(
                        name="test-cluster-subnet-us-west-2b-2b80f",
                        az="us-west-2b",
                        cidr="10.0.16.0/20",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                    ),
                    "subnet-2": _subnet(
                        name="test-cluster-subnet-us-west-2c-03273",
                        az="us-west-2c",
                        cidr="10.0.32.0/20",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                    ),
                    "private-subnet-0": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2a-6a89f",
                        az="us-west-2a",
                        cidr="10.0.48.0/20",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                    ),
                    "private-subnet-1": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2b-b7832",
                        az="us-west-2b",
                        cidr="10.0.64.0/20",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                    ),
                    "private-subnet-2": _private_subnet(
                        name="test-cluster-private-subnet-us-west-2c-ef57d",
                        az="us-west-2c",
                        cidr="10.0.80.0/20",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                    ),
                    "internet-gateway": _internet_gateway(cred_kind="ProviderConfig", cred_name="my-aws-account"),
                    "nat-eip": _nat_eip(cred_kind="ProviderConfig", cred_name="my-aws-account"),
                    "nat-gateway": _nat_gateway(cred_kind="ProviderConfig", cred_name="my-aws-account"),
                    "route-table": _route_table(cred_kind="ProviderConfig", cred_name="my-aws-account"),
                    "route-default": _route_default(cred_kind="ProviderConfig", cred_name="my-aws-account"),
                    "private-route-table": _private_route_table(cred_kind="ProviderConfig", cred_name="my-aws-account"),
                    "private-route-default": _private_route_default(
                        cred_kind="ProviderConfig", cred_name="my-aws-account"
                    ),
                    "route-table-association-0": _route_table_association(
                        az="us-west-2a", cred_kind="ProviderConfig", cred_name="my-aws-account"
                    ),
                    "route-table-association-1": _route_table_association(
                        az="us-west-2b", cred_kind="ProviderConfig", cred_name="my-aws-account"
                    ),
                    "route-table-association-2": _route_table_association(
                        az="us-west-2c", cred_kind="ProviderConfig", cred_name="my-aws-account"
                    ),
                    "private-route-table-association-0": _private_route_table_association(
                        az="us-west-2a", cred_kind="ProviderConfig", cred_name="my-aws-account"
                    ),
                    "private-route-table-association-1": _private_route_table_association(
                        az="us-west-2b", cred_kind="ProviderConfig", cred_name="my-aws-account"
                    ),
                    "private-route-table-association-2": _private_route_table_association(
                        az="us-west-2c", cred_kind="ProviderConfig", cred_name="my-aws-account"
                    ),
                    "iam-role-cluster": _cluster_role(cred_kind="ProviderConfig", cred_name="my-aws-account"),
                    "iam-attach-cluster-policy": _role_policy_attachment(
                        role="cluster",
                        arn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                    ),
                    "iam-role-node": _node_role(cred_kind="ProviderConfig", cred_name="my-aws-account"),
                    "iam-attach-node-worker": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                    ),
                    "iam-attach-node-cni": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                    ),
                    "iam-attach-node-ecr": _role_policy_attachment(
                        role="node",
                        arn="arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                    ),
                    "cluster": _eks_cluster(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "cluster-auth": _cluster_auth(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "nodegroup-system": _system_node_group(cred_kind="ProviderConfig", cred_name="my-aws-account"),
                    "nodegroup-gpu-l4": _gpu_node_group(cred_kind="ProviderConfig", cred_name="my-aws-account"),
                    "addon-vpc-cni": _addon(name="vpc-cni", cred_kind="ProviderConfig", cred_name="my-aws-account"),
                    "addon-kube-proxy": _addon(
                        name="kube-proxy", cred_kind="ProviderConfig", cred_name="my-aws-account"
                    ),
                    "addon-coredns": _addon(name="coredns", cred_kind="ProviderConfig", cred_name="my-aws-account"),
                    "efs-filesystem": _efs_filesystem(
                        cred_kind="ProviderConfig", cred_name="my-aws-account", ready=fnv1.READY_UNSPECIFIED
                    ),
                    "efs-security-group": _efs_security_group(cred_kind="ProviderConfig", cred_name="my-aws-account"),
                    "efs-security-group-ingress": _efs_security_group_ingress(
                        cred_kind="ProviderConfig", cred_name="my-aws-account"
                    ),
                    "efs-mount-target-0": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2a-6a89f",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                    ),
                    "efs-mount-target-1": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2b-b7832",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                    ),
                    "efs-mount-target-2": _efs_mount_target(
                        subnet_name="test-cluster-private-subnet-us-west-2c-ef57d",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                    ),
                    "iam-role-efs-csi": _pod_identity_role(
                        role="efs-csi", cred_kind="ProviderConfig", cred_name="my-aws-account"
                    ),
                    "iam-attach-efs-csi": _role_policy_attachment(
                        role="efs-csi",
                        arn="arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
                        cred_kind="ProviderConfig",
                        cred_name="my-aws-account",
                    ),
                    "addon-eks-pod-identity-agent": _addon(
                        name="eks-pod-identity-agent", cred_kind="ProviderConfig", cred_name="my-aws-account"
                    ),
                    "pod-identity-efs-csi": _pod_identity_association(
                        cred_kind="ProviderConfig", cred_name="my-aws-account"
                    ),
                    "addon-aws-efs-csi-driver": _addon(
                        name="aws-efs-csi-driver", cred_kind="ProviderConfig", cred_name="my-aws-account"
                    ),
                    "iam-policy-cluster-autoscaler": _autoscaler_policy(
                        cred_kind="ProviderConfig", cred_name="my-aws-account"
                    ),
                    "iam-role-cluster-autoscaler": _pod_identity_role(
                        role="cluster-autoscaler", cred_kind="ProviderConfig", cred_name="my-aws-account"
                    ),
                    "iam-attach-cluster-autoscaler": _autoscaler_attachment(
                        cred_kind="ProviderConfig", cred_name="my-aws-account"
                    ),
                    "pod-identity-cluster-autoscaler": _autoscaler_pod_identity(
                        cred_kind="ProviderConfig", cred_name="my-aws-account"
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
    """RunFunction composes EKS cluster infrastructure."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want)
