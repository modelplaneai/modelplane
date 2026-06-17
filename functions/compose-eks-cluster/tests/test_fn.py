"""Tests for the compose-eks-cluster function."""

import dataclasses
import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.infrastructure.ekscluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-eks-cluster."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


_KUBECONFIG_SECRET = "test-cluster-kubeconfig-55b57"
_SUBNET_A = "test-cluster-subnet-us-west-2a-952dc"
_SUBNET_B = "test-cluster-subnet-us-west-2b-2b80f"
_SUBNET_C = "test-cluster-subnet-us-west-2c-03273"


def _xr() -> v1alpha1.EKSCluster:
    return v1alpha1.EKSCluster(
        metadata=metav1.ObjectMeta(
            name="test-cluster",
            namespace="modelplane-system",
        ),
        spec=v1alpha1.Spec(
            region="us-west-2",
            nodePools=[
                v1alpha1.NodePool(
                    name="gpu-l4",
                    role="GPU",
                    instanceType="g6.xlarge",
                    nodeCount=1,
                    minNodeCount=0,
                    maxNodeCount=4,
                    gpu=v1alpha1.Gpu(
                        acceleratorType="nvidia-l4",
                    ),
                    zones=["us-west-2a", "us-west-2b"],
                ),
            ],
        ),
    )


# Launch template name is derived the same way the function derives it, so
# the test can't drift from the function's child_name hashing.
_LAUNCH_TEMPLATE_NAME = resource.child_name("test-cluster", "lt-gpu-h200")
_CAPACITY_RESERVATION_ID = "cr-0123456789abcdef0"


def _xr_capacity_block() -> v1alpha1.EKSCluster:
    return v1alpha1.EKSCluster(
        metadata=metav1.ObjectMeta(
            name="test-cluster",
            namespace="modelplane-system",
        ),
        spec=v1alpha1.Spec(
            region="us-west-2",
            nodePools=[
                v1alpha1.NodePool(
                    name="gpu-h200",
                    role="GPU",
                    instanceType="p5en.48xlarge",
                    nodeCount=2,
                    minNodeCount=0,
                    maxNodeCount=2,
                    diskSizeGb=1024,
                    gpu=v1alpha1.Gpu(
                        acceleratorType="nvidia-h200",
                    ),
                    capacityBlock=v1alpha1.CapacityBlock(
                        capacityReservationId=_CAPACITY_RESERVATION_ID,
                    ),
                    zones=["us-west-2a"],
                ),
            ],
        ),
    )


def _launch_template() -> dict:
    return {
        "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
        "kind": "LaunchTemplate",
        "spec": {
            "forProvider": {
                "region": "us-west-2",
                "name": _LAUNCH_TEMPLATE_NAME,
                "instanceType": "p5en.48xlarge",
                "instanceMarketOptions": {"marketType": "capacity-block"},
                "capacityReservationSpecification": {
                    "capacityReservationPreference": "capacity-reservations-only",
                    "capacityReservationTarget": {
                        "capacityReservationId": _CAPACITY_RESERVATION_ID,
                    },
                },
            },
        },
    }


def _gpu_node_group_capacity_block() -> dict:
    return {
        "apiVersion": "eks.aws.m.upbound.io/v1beta1",
        "kind": "NodeGroup",
        "spec": {
            "forProvider": {
                "region": "us-west-2",
                "amiType": "AL2023_x86_64_NVIDIA",
                "diskSize": 1024,
                "clusterNameSelector": {"matchControllerRef": True},
                "nodeRoleArnSelector": {
                    "matchControllerRef": True,
                    "matchLabels": {"modelplane.ai/iam-role": "node"},
                },
                "capacityType": "CAPACITY_BLOCK",
                "launchTemplate": {
                    "name": _LAUNCH_TEMPLATE_NAME,
                    "version": "$Latest",
                },
                "subnetIdRefs": [{"name": _SUBNET_A}],
                "scalingConfig": {"desiredSize": 2, "minSize": 0, "maxSize": 2},
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


def _ready_condition() -> dict:
    return {
        "type": "Ready",
        "status": "True",
        "reason": "Available",
        "lastTransitionTime": "2024-01-01T00:00:00Z",
    }


def _vpc() -> dict:
    return {
        "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
        "kind": "VPC",
        "spec": {
            "forProvider": {
                "region": "us-west-2",
                "cidrBlock": "10.0.0.0/16",
                "enableDnsHostnames": True,
                "enableDnsSupport": True,
            },
        },
    }


def _subnet(name: str, az: str, cidr: str) -> dict:
    return {
        "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
        "kind": "Subnet",
        "metadata": {"name": name, "labels": {"modelplane.ai/zone": az}},
        "spec": {
            "forProvider": {
                "region": "us-west-2",
                "availabilityZone": az,
                "cidrBlock": cidr,
                "mapPublicIpOnLaunch": True,
                "vpcIdSelector": {"matchControllerRef": True},
            },
        },
    }


def _internet_gateway() -> dict:
    return {
        "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
        "kind": "InternetGateway",
        "spec": {
            "forProvider": {
                "region": "us-west-2",
                "vpcIdSelector": {"matchControllerRef": True},
            },
        },
    }


def _route_table() -> dict:
    return {
        "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
        "kind": "RouteTable",
        "spec": {
            "forProvider": {
                "region": "us-west-2",
                "vpcIdSelector": {"matchControllerRef": True},
            },
        },
    }


def _route_default() -> dict:
    return {
        "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
        "kind": "Route",
        "spec": {
            "forProvider": {
                "region": "us-west-2",
                "destinationCidrBlock": "0.0.0.0/0",
                "gatewayIdSelector": {"matchControllerRef": True},
                "routeTableIdSelector": {"matchControllerRef": True},
            },
        },
    }


def _route_table_association(az: str) -> dict:
    return {
        "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
        "kind": "RouteTableAssociation",
        "spec": {
            "forProvider": {
                "region": "us-west-2",
                "routeTableIdSelector": {"matchControllerRef": True},
                "subnetIdSelector": {
                    "matchControllerRef": True,
                    "matchLabels": {"modelplane.ai/zone": az},
                },
            },
        },
    }


def _role(role: str, assume_policy: str) -> dict:
    return {
        "apiVersion": "iam.aws.m.upbound.io/v1beta1",
        "kind": "Role",
        "metadata": {"labels": {"modelplane.ai/iam-role": role}},
        "spec": {"forProvider": {"assumeRolePolicy": assume_policy}},
    }


def _role_policy_attachment(role: str, arn: str) -> dict:
    return {
        "apiVersion": "iam.aws.m.upbound.io/v1beta1",
        "kind": "RolePolicyAttachment",
        "spec": {
            "forProvider": {
                "policyArn": arn,
                "roleSelector": {
                    "matchControllerRef": True,
                    "matchLabels": {"modelplane.ai/iam-role": role},
                },
            },
        },
    }


_ASSUME_CLUSTER = (
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
    '"Principal":{"Service":"eks.amazonaws.com"},'
    '"Action":"sts:AssumeRole"}]}'
)
_ASSUME_NODE = (
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
    '"Principal":{"Service":"ec2.amazonaws.com"},'
    '"Action":"sts:AssumeRole"}]}'
)
_ASSUME_POD_IDENTITY = (
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
    '"Principal":{"Service":"pods.eks.amazonaws.com"},'
    '"Action":["sts:AssumeRole","sts:TagSession"]}]}'
)
_POLICY_EFS_CSI = "arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy"


def _eks_cluster() -> dict:
    return {
        "apiVersion": "eks.aws.m.upbound.io/v1beta1",
        "kind": "Cluster",
        "spec": {
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


def _cluster_auth() -> dict:
    return {
        "apiVersion": "eks.aws.m.upbound.io/v1beta1",
        "kind": "ClusterAuth",
        "spec": {
            "forProvider": {
                "region": "us-west-2",
                "clusterNameSelector": {"matchControllerRef": True},
            },
            "writeConnectionSecretToRef": {"name": _KUBECONFIG_SECRET},
        },
    }


def _system_node_group() -> dict:
    return {
        "apiVersion": "eks.aws.m.upbound.io/v1beta1",
        "kind": "NodeGroup",
        "spec": {
            "forProvider": {
                "region": "us-west-2",
                "amiType": "AL2023_x86_64_STANDARD",
                "instanceTypes": ["m6i.xlarge"],
                "clusterNameSelector": {"matchControllerRef": True},
                "nodeRoleArnSelector": {
                    "matchControllerRef": True,
                    "matchLabels": {"modelplane.ai/iam-role": "node"},
                },
                "subnetIdSelector": {"matchControllerRef": True},
                "scalingConfig": {"desiredSize": 1, "minSize": 1, "maxSize": 2},
                "labels": {"modelplane.ai/pool": "system"},
            },
        },
    }


def _gpu_node_group() -> dict:
    return {
        "apiVersion": "eks.aws.m.upbound.io/v1beta1",
        "kind": "NodeGroup",
        "spec": {
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
                "subnetIdRefs": [{"name": _SUBNET_A}, {"name": _SUBNET_B}],
                "scalingConfig": {"desiredSize": 1, "minSize": 0, "maxSize": 4},
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


def _addon(name: str) -> dict:
    return {
        "apiVersion": "eks.aws.m.upbound.io/v1beta1",
        "kind": "Addon",
        "spec": {
            "forProvider": {
                "region": "us-west-2",
                "addonName": name,
                "clusterNameSelector": {"matchControllerRef": True},
            },
        },
    }


def _efs_filesystem() -> dict:
    return {
        "apiVersion": "efs.aws.m.upbound.io/v1beta1",
        "kind": "FileSystem",
        "spec": {"forProvider": {"region": "us-west-2", "throughputMode": "elastic", "encrypted": True}},
    }


def _efs_security_group() -> dict:
    return {
        "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
        "kind": "SecurityGroup",
        "spec": {
            "forProvider": {
                "region": "us-west-2",
                "name": "test-cluster-efs",
                "description": "NFS access to the ModelCache EFS mount targets",
                "vpcIdSelector": {"matchControllerRef": True},
            },
        },
    }


def _efs_security_group_ingress() -> dict:
    return {
        "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
        "kind": "SecurityGroupIngressRule",
        "spec": {
            "forProvider": {
                "region": "us-west-2",
                "ipProtocol": "tcp",
                "fromPort": 2049,
                "toPort": 2049,
                "cidrIpv4": "10.0.0.0/16",
                "securityGroupIdSelector": {"matchControllerRef": True},
            },
        },
    }


def _efs_mount_target(subnet_name: str) -> dict:
    return {
        "apiVersion": "efs.aws.m.upbound.io/v1beta1",
        "kind": "MountTarget",
        "spec": {
            "forProvider": {
                "region": "us-west-2",
                "fileSystemIdSelector": {"matchControllerRef": True},
                "subnetIdRef": {"name": subnet_name},
                "securityGroupsSelector": {"matchControllerRef": True},
            },
        },
    }


def _pod_identity_association() -> dict:
    return {
        "apiVersion": "eks.aws.m.upbound.io/v1beta1",
        "kind": "PodIdentityAssociation",
        "spec": {
            "forProvider": {
                "region": "us-west-2",
                "namespace": "kube-system",
                "serviceAccount": "efs-csi-controller-sa",
                "clusterNameSelector": {"matchControllerRef": True},
                "roleArnSelector": {"matchControllerRef": True, "matchLabels": {"modelplane.ai/iam-role": "efs-csi"}},
            },
        },
    }


def _storage_class_object(filesystem_id: str) -> dict:
    return {
        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
        "kind": "Object",
        "metadata": {"namespace": "modelplane-system"},
        "spec": {
            "providerConfigRef": {
                "kind": "ProviderConfig",
                "name": _KUBECONFIG_SECRET,
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
                        "fileSystemId": filesystem_id,
                        "directoryPerms": "700",
                    },
                    "volumeBindingMode": "Immediate",
                },
            },
        },
    }


def _provider_config(api_version: str) -> dict:
    return {
        "apiVersion": api_version,
        "kind": "ProviderConfig",
        "metadata": {"name": _KUBECONFIG_SECRET},
        "spec": {
            "credentials": {
                "source": "Secret",
                "secretRef": {
                    "name": _KUBECONFIG_SECRET,
                    "namespace": "modelplane-system",
                    "key": "kubeconfig",
                },
            },
        },
    }


def _expected_status() -> dict:
    # `type` is emitted because the function sets it explicitly on the Status
    # model, so update_status (exclude_unset) keeps it rather than dropping it
    # as an unset field.
    status = {
        "secrets": [
            {
                "type": "Kubeconfig",
                "name": _KUBECONFIG_SECRET,
                "key": "kubeconfig",
            },
        ],
        # write_status always publishes the effective RWX StorageClass name,
        # even before the managed class materialises on the workload cluster.
        "cache": {"storageClassName": "modelplane-rwx-efs"},
    }
    return {"status": status}


def _expected_resources() -> dict:
    return {
        "vpc": fnv1.Resource(resource=resource.dict_to_struct(_vpc())),
        "subnet-0": fnv1.Resource(
            resource=resource.dict_to_struct(_subnet(_SUBNET_A, "us-west-2a", "10.0.0.0/20")),
        ),
        "subnet-1": fnv1.Resource(
            resource=resource.dict_to_struct(_subnet(_SUBNET_B, "us-west-2b", "10.0.16.0/20")),
        ),
        "subnet-2": fnv1.Resource(
            resource=resource.dict_to_struct(_subnet(_SUBNET_C, "us-west-2c", "10.0.32.0/20")),
        ),
        "internet-gateway": fnv1.Resource(resource=resource.dict_to_struct(_internet_gateway())),
        "route-table": fnv1.Resource(resource=resource.dict_to_struct(_route_table())),
        "route-default": fnv1.Resource(resource=resource.dict_to_struct(_route_default())),
        "route-table-association-0": fnv1.Resource(
            resource=resource.dict_to_struct(_route_table_association("us-west-2a")),
        ),
        "route-table-association-1": fnv1.Resource(
            resource=resource.dict_to_struct(_route_table_association("us-west-2b")),
        ),
        "route-table-association-2": fnv1.Resource(
            resource=resource.dict_to_struct(_route_table_association("us-west-2c")),
        ),
        "iam-role-cluster": fnv1.Resource(
            resource=resource.dict_to_struct(_role("cluster", _ASSUME_CLUSTER)),
        ),
        "iam-attach-cluster-policy": fnv1.Resource(
            resource=resource.dict_to_struct(
                _role_policy_attachment("cluster", "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"),
            ),
        ),
        "iam-role-node": fnv1.Resource(resource=resource.dict_to_struct(_role("node", _ASSUME_NODE))),
        "iam-attach-node-worker": fnv1.Resource(
            resource=resource.dict_to_struct(
                _role_policy_attachment("node", "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"),
            ),
        ),
        "iam-attach-node-cni": fnv1.Resource(
            resource=resource.dict_to_struct(
                _role_policy_attachment("node", "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"),
            ),
        ),
        "iam-attach-node-ecr": fnv1.Resource(
            resource=resource.dict_to_struct(
                _role_policy_attachment("node", "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"),
            ),
        ),
        "cluster": fnv1.Resource(resource=resource.dict_to_struct(_eks_cluster())),
        "cluster-auth": fnv1.Resource(resource=resource.dict_to_struct(_cluster_auth())),
        "nodegroup-system": fnv1.Resource(resource=resource.dict_to_struct(_system_node_group())),
        "nodegroup-gpu-l4": fnv1.Resource(resource=resource.dict_to_struct(_gpu_node_group())),
        "addon-vpc-cni": fnv1.Resource(resource=resource.dict_to_struct(_addon("vpc-cni"))),
        "addon-kube-proxy": fnv1.Resource(resource=resource.dict_to_struct(_addon("kube-proxy"))),
        "addon-coredns": fnv1.Resource(resource=resource.dict_to_struct(_addon("coredns"))),
        "efs-filesystem": fnv1.Resource(resource=resource.dict_to_struct(_efs_filesystem())),
        "efs-security-group": fnv1.Resource(resource=resource.dict_to_struct(_efs_security_group())),
        "efs-security-group-ingress": fnv1.Resource(resource=resource.dict_to_struct(_efs_security_group_ingress())),
        "efs-mount-target-0": fnv1.Resource(resource=resource.dict_to_struct(_efs_mount_target(_SUBNET_A))),
        "efs-mount-target-1": fnv1.Resource(resource=resource.dict_to_struct(_efs_mount_target(_SUBNET_B))),
        "efs-mount-target-2": fnv1.Resource(resource=resource.dict_to_struct(_efs_mount_target(_SUBNET_C))),
        "iam-role-efs-csi": fnv1.Resource(resource=resource.dict_to_struct(_role("efs-csi", _ASSUME_POD_IDENTITY))),
        "iam-attach-efs-csi": fnv1.Resource(
            resource=resource.dict_to_struct(_role_policy_attachment("efs-csi", _POLICY_EFS_CSI)),
        ),
        "addon-eks-pod-identity-agent": fnv1.Resource(
            resource=resource.dict_to_struct(_addon("eks-pod-identity-agent")),
        ),
        "pod-identity-efs-csi": fnv1.Resource(resource=resource.dict_to_struct(_pod_identity_association())),
        "addon-aws-efs-csi-driver": fnv1.Resource(resource=resource.dict_to_struct(_addon("aws-efs-csi-driver"))),
        "provider-config-kubernetes": fnv1.Resource(
            resource=resource.dict_to_struct(_provider_config("kubernetes.m.crossplane.io/v1alpha1")),
            ready=fnv1.READY_TRUE,
        ),
        "provider-config-helm": fnv1.Resource(
            resource=resource.dict_to_struct(_provider_config("helm.m.crossplane.io/v1beta1")),
            ready=fnv1.READY_TRUE,
        ),
    }


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    """Tests for FunctionRunner.RunFunction."""

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:
        """The function composes EKS cluster infrastructure."""
        # Second pass: cluster and cluster-auth observed Ready, function flips
        # those two desired resources ready while still emitting everything.
        ready_resources = _expected_resources()
        ready_resources["cluster"] = fnv1.Resource(
            resource=ready_resources["cluster"].resource,
            ready=fnv1.READY_TRUE,
        )
        ready_resources["cluster-auth"] = fnv1.Resource(
            resource=ready_resources["cluster-auth"].resource,
            ready=fnv1.READY_TRUE,
        )
        ready_resources["efs-filesystem"] = fnv1.Resource(
            resource=ready_resources["efs-filesystem"].resource,
            ready=fnv1.READY_TRUE,
        )
        # Once the EFS filesystem id is observed, the managed StorageClass Object
        # is composed (and marked ready) against the cluster's own ProviderConfig.
        ready_resources["storage-class-rwx-efs"] = fnv1.Resource(
            resource=resource.dict_to_struct(_storage_class_object("fs-0abc123")),
            ready=fnv1.READY_TRUE,
        )

        cases = [
            Case(
                name="first pass composes infra resources; none ready",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                _xr().model_dump(exclude_none=True, mode="json"),
                            ),
                        ),
                    ),
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(_expected_status()),
                        ),
                        resources=_expected_resources(),
                    ),
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="second pass with observed cluster ready marks cluster resources ready",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                _xr().model_dump(exclude_none=True, mode="json"),
                            ),
                        ),
                        resources={
                            "cluster": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        **_eks_cluster(),
                                        "status": {"conditions": [_ready_condition()]},
                                    },
                                ),
                            ),
                            "cluster-auth": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        **_cluster_auth(),
                                        "status": {"conditions": [_ready_condition()]},
                                    },
                                ),
                            ),
                            "efs-filesystem": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    {
                                        **_efs_filesystem(),
                                        "metadata": {"annotations": {"crossplane.io/external-name": "fs-0abc123"}},
                                        "status": {"conditions": [_ready_condition()]},
                                    },
                                ),
                            ),
                        },
                    ),
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(_expected_status()),
                        ),
                        resources=ready_resources,
                    ),
                    context=structpb.Struct(),
                ),
            ),
        ]

        for case in cases:
            with self.subTest(name=case.name):
                got = await self.runner.RunFunction(case.req, None)
                self.assertEqual(
                    json_format.MessageToDict(case.want),
                    json_format.MessageToDict(got),
                )

    async def test_compose_capacity_block(self) -> None:
        """A Capacity Block pool composes a launch template and a CAPACITY_BLOCK node group.

        The GPU node group must not set instanceTypes (EKS takes the type
        from the launch template), must set capacityType=CAPACITY_BLOCK, and
        must reference the launch template. The launch template targets the
        reservation via the capacity-block market type.
        """
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        _xr_capacity_block().model_dump(exclude_none=True, mode="json"),
                    ),
                ),
            ),
        )

        got = await self.runner.RunFunction(req, None)
        resources = got.desired.resources

        # The launch template is composed and targets the reservation.
        self.assertIn("launch-template-gpu-h200", resources)
        self.assertEqual(
            _launch_template(),
            resource.struct_to_dict(resources["launch-template-gpu-h200"].resource),
        )

        # The GPU node group uses CAPACITY_BLOCK + the launch template and
        # carries no instanceTypes.
        self.assertEqual(
            _gpu_node_group_capacity_block(),
            resource.struct_to_dict(resources["nodegroup-gpu-h200"].resource),
        )


if __name__ == "__main__":
    unittest.main()
