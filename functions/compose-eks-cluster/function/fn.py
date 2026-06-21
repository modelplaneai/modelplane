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

"""Compose an EKS cluster with networking, node groups, and IAM roles.

This function provisions the AWS infrastructure for an inference environment:
a VPC with one subnet per AZ, an EKS cluster with system and GPU node groups,
IAM roles for the control plane and nodes, and ProviderConfigs for
provider-kubernetes and provider-helm to reach the cluster.

The kubeconfig written by the EKS ClusterAuth managed resource contains a
static bearer token that the AWS provider refreshes every refreshPeriod
(default 10 minutes) using its own ProviderConfig credentials. The AWS
principal that creates the cluster is granted cluster-admin via
bootstrapClusterCreatorAdminPermissions, so the same credentials are
authorised on the cluster. Downstream consumers only need the kubeconfig;
no per-cluster AWS identity has to be wired into provider-kubernetes.
"""

from typing import Literal

import grpc
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.infrastructure.ekscluster import v1alpha1
from models.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1
from models.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.crossplane.m.kubernetes.providerconfig import v1alpha1 as k8spcv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from models.io.upbound.m.aws.ec2.eip import v1beta1 as eipv1beta1
from models.io.upbound.m.aws.ec2.internetgateway import v1beta1 as igwv1beta1
from models.io.upbound.m.aws.ec2.launchtemplate import v1beta1 as ltv1beta1
from models.io.upbound.m.aws.ec2.natgateway import v1beta1 as natv1beta1
from models.io.upbound.m.aws.ec2.route import v1beta1 as routev1beta1
from models.io.upbound.m.aws.ec2.routetable import v1beta1 as rtv1beta1
from models.io.upbound.m.aws.ec2.routetableassociation import v1beta1 as rtav1beta1
from models.io.upbound.m.aws.ec2.securitygroup import v1beta1 as sgv1beta1
from models.io.upbound.m.aws.ec2.securitygroupegressrule import v1beta1 as sgev1beta1
from models.io.upbound.m.aws.ec2.securitygroupingressrule import v1beta1 as sgrv1beta1
from models.io.upbound.m.aws.ec2.subnet import v1beta1 as subnetv1beta1
from models.io.upbound.m.aws.ec2.vpc import v1beta1 as vpcv1beta1
from models.io.upbound.m.aws.efs.filesystem import v1beta1 as fsv1beta1
from models.io.upbound.m.aws.efs.mounttarget import v1beta1 as mtv1beta1
from models.io.upbound.m.aws.eks.addon import v1beta1 as addonv1beta1
from models.io.upbound.m.aws.eks.cluster import v1beta1 as clusterv1beta1
from models.io.upbound.m.aws.eks.clusterauth import v1beta1 as clusterauthv1beta1
from models.io.upbound.m.aws.eks.nodegroup import v1beta1 as ngv1beta1
from models.io.upbound.m.aws.eks.podidentityassociation import v1beta1 as piav1beta1
from models.io.upbound.m.aws.iam.policy import v1beta1 as policyv1beta1
from models.io.upbound.m.aws.iam.role import v1beta1 as rolev1beta1
from models.io.upbound.m.aws.iam.rolepolicyattachment import v1beta1 as rpav1beta1


def _name(meta: metav1.ObjectMeta | None) -> str:
    """The object's name, always set on resources read from the API server."""
    if meta is None or meta.name is None:
        raise ValueError("metadata.name is unexpectedly absent")
    return meta.name


def _namespace(meta: metav1.ObjectMeta | None) -> str:
    """The object's namespace, always set on namespaced resources read from the API server."""
    if meta is None or meta.namespace is None:
        raise ValueError("metadata.namespace is unexpectedly absent")
    return meta.namespace


# Node group management policies that exclude LateInitialize, so the
# desiredSize we seed via initProvider is applied only at creation and then
# left alone. The cluster autoscaler drives the ASG's desired capacity; without
# this Crossplane would keep reverting desiredSize to nodeCount and fight it.
# (initProvider is a beta feature gated on enumerating management policies — the
# default "*" still reconciles forProvider, defeating the purpose.)
_ManagementPolicy = Literal["Observe", "Create", "Update", "Delete", "LateInitialize", "*"]
_NODE_GROUP_MANAGEMENT: list[_ManagementPolicy] = ["Observe", "Create", "Update", "Delete"]

# Management policies that exclude Delete, used for resources installed on the
# workload cluster (the RWX StorageClass Object, the autoscaler and EFA DRA
# Helm Releases). These exist only to configure the cluster and are only ever
# deleted because the whole EKSCluster - and the cluster itself - is being torn
# down. Deleting them then means asking provider-helm / provider-kubernetes to
# reach a cluster whose kubeconfig Secret has already been deleted, which wedges
# their finalizers and hangs the composite. Orphaning them sidesteps that: the
# in-cluster resources die with the cluster. Crossplane names composed resources
# deterministically from the owner XR's UID and the composition resource name,
# so if one of these MRs is ever deleted out of band the recomposed MR takes the
# same name and provider-helm / provider-kubernetes adopt the existing release
# or object rather than erroring.
_ORPHAN_MANAGEMENT: list[_ManagementPolicy] = ["Observe", "Create", "Update"]

# System node group injected into every EKS cluster to host control-plane
# components (Envoy Gateway, KEDA, KServe controller, etc.). Not part of
# the user-facing API — compose-inference-cluster only passes GPU groups.
_SYSTEM_POOL_NAME = "system"
_SYSTEM_POOL_INSTANCE_TYPE = "m6i.xlarge"
_SYSTEM_POOL_NODE_COUNT = 1
_SYSTEM_POOL_MIN_NODE_COUNT = 1
_SYSTEM_POOL_MAX_NODE_COUNT = 2

# Labels written on EKS node groups. compose-model-deployment reads
# these labels for GPU scheduling.
_LABEL_GPU = "modelplane.ai/gpu"
_LABEL_POOL = "modelplane.ai/pool"
# Tags the EFA security group so its self-referencing ingress/egress rules and
# the launch template's EFA interfaces can select it - distinct from the EFS
# security group, which also matches the controller ref.
_LABEL_FABRIC = "modelplane.ai/fabric"

# Distinguishes the controller's security groups from each other. A cluster has
# the EFS security group, and an EFA pool adds the EFA one; a bare
# matchControllerRef selector can't tell them apart, so each rule filters on the
# role of the group it targets.
_LABEL_SG_ROLE = "modelplane.ai/sg-role"
_SG_ROLE_EFS = "efs"

# Internal labels written on composed AWS resources so other resources
# can select them. _LABEL_ROLE distinguishes the cluster IAM role from
# the node IAM role. _LABEL_AZ tags each subnet with its Availability
# Zone so NodeGroup subnetIdSelector can pick the right subnets.
_LABEL_ROLE = "modelplane.ai/iam-role"
_LABEL_AZ = "modelplane.ai/zone"

# Tags a subnet with its network tier. Public subnets (IGW route) host the NAT
# gateway and load balancers; private subnets (NAT route) host the nodes. Node
# groups select private subnets by this label; the public route table and NAT
# gateway select public ones.
_LABEL_TIER = "modelplane.ai/subnet-tier"
_TIER_PUBLIC = "public"
_TIER_PRIVATE = "private"

# EKS discovers which subnets to place internet-facing load balancers in by this
# tag. Nodes run in private subnets, so the serving gateway's ELB would have no
# public subnet to land in without tagging the public ones.
_TAG_ELB_ROLE = "kubernetes.io/role/elb"

_ROLE_CLUSTER = "cluster"
_ROLE_NODE = "node"

# Secret type written to XR status. compose-inference-cluster reads
# this to wire the kubeconfig into a ClusterProviderConfig.
_SECRET_TYPE_KUBECONFIG = "Kubeconfig"
_SECRET_KEY_KUBECONFIG = "kubeconfig"

# AMI types for EKS-optimised AMIs. AL2023 NVIDIA includes the NVIDIA
# driver and container toolkit pre-installed. AL2023 standard is the
# default Amazon Linux 2023 AMI for non-GPU workloads.
_AMI_TYPE_SYSTEM = "AL2023_x86_64_STANDARD"
_AMI_TYPE_GPU = "AL2023_x86_64_NVIDIA"

# Root EBS device name on the AL2023 EKS-optimised AMIs. A launch-template node
# group sizes its root volume through a block device mapping on this device,
# since EKS won't accept the node group's diskSize when a template is set.
_ROOT_DEVICE_NAME = "/dev/xvda"

# Capacity Block backing. Large GPU instances (e.g. p5en.48xlarge) are
# rarely available on demand; AWS allocates them via Capacity Blocks for
# ML. A node group backed by a Capacity Block uses the CAPACITY_BLOCK
# capacity type and a launch template that targets the reservation: it
# sets the instance market type to "capacity-block" and points at the
# reservation ID. EKS requires the instance type to come from the launch
# template (not the node group's instanceTypes) for capacity-block groups.
_CAPACITY_TYPE_CAPACITY_BLOCK = "CAPACITY_BLOCK"
_MARKET_TYPE_CAPACITY_BLOCK = "capacity-block"
_CR_PREFERENCE_ONLY = "capacity-reservations-only"

# Elastic Fabric Adapter. A pool with fabric: EFA attaches EFA network
# interfaces to each node so cross-node NCCL runs over GPUDirect RDMA
# rather than TCP. The interfaces are configured in the node group's
# launch template: the primary (network card 0) stays a normal interface
# for IP traffic, and the remaining network cards each carry one efa
# interface. EFA OS-bypass requires the interfaces to sit in a security
# group that allows all traffic to and from itself, so we compose a
# dedicated EFA security group with self-referencing ingress and egress.
_FABRIC_EFA = "EFA"
# Card 0 is EFA-with-ENA: it carries the node's IP traffic and EFA. The
# secondary cards are efa-only - dedicated RDMA with no IP, which nodeadm leaves
# unmanaged. Marking the secondaries plain "efa" makes nodeadm try to manage
# them as IP interfaces and its primary-ENI-only setup times out, so the node
# never joins.
_INTERFACE_TYPE_EFA = "efa"
_INTERFACE_TYPE_EFA_ONLY = "efa-only"

# EFA network cards per instance type. An instance gets the most fabric
# bandwidth when every network card carries an EFA interface, so the launch
# template configures one interface per card. The count is instance-type
# specific; this table covers the EFA-capable GPU types Modelplane targets.
# A type that's absent but still sets fabric: EFA falls back to a single EFA
# interface (_EFA_CARDS_DEFAULT) - correct, just not maximal bandwidth.
_EFA_NETWORK_CARDS = {
    "p5.48xlarge": 32,
    "p5e.48xlarge": 32,
    "p5en.48xlarge": 16,
    "p4d.24xlarge": 4,
    "p4de.24xlarge": 4,
}
_EFA_CARDS_DEFAULT = 1

# EFA DRA driver (DRANET). When any pool uses the EFA fabric we install it as a
# Helm release on the cluster, the same way the autoscaler is installed. It runs
# as a DaemonSet that discovers each node's EFA interfaces, publishes them as DRA
# ResourceSlices, and registers the efa.networking.k8s.aws DeviceClass a
# multi-node gang's ResourceClaims request alongside their GPUs. EKS defaults to
# a Kubernetes version where DRANET is supported; we use it rather than the EFA
# device plugin so EFA allocation matches the DRA model the GPU driver uses.
_EFA_DRA_DRIVER_NAMESPACE = "kube-system"
_EFA_DRA_DRIVER_CHART_REPO = "https://aws.github.io/eks-charts"
_EFA_DRA_DRIVER_CHART_NAME = "aws-dranet"
_EFA_DRA_DRIVER_CHART_VERSION = "1.0.0"

# GPU taint applied to GPU node groups so non-GPU pods don't land on
# expensive GPU nodes.
_GPU_TAINT_KEY = "nvidia.com/gpu"
_GPU_TAINT_VALUE = "true"
_GPU_TAINT_EFFECT = "NO_SCHEDULE"

# Kubernetes-API taint effect (as a toleration value, not the EC2 NO_SCHEDULE
# form). The EFA DRA driver's DaemonSet must tolerate the GPU taint to run on
# the GPU nodes, like the NVIDIA GPU DRA driver does.
_GPU_TAINT_EFFECT_K8S = "NoSchedule"

# IAM policies attached to the cluster and node roles. These are
# AWS-managed policies; their ARNs are stable.
_POLICY_CLUSTER = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
_POLICY_NODE_WORKER = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
_POLICY_NODE_CNI = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
_POLICY_NODE_ECR = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
_POLICY_EFS_CSI = "arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy"

# Trust policies for the cluster and node roles. The cluster role is
# assumed by the EKS service; the node role is assumed by EC2 instances
# that join the cluster.
_ASSUME_ROLE_CLUSTER = (
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
    '"Principal":{"Service":"eks.amazonaws.com"},'
    '"Action":"sts:AssumeRole"}]}'
)
_ASSUME_ROLE_NODE = (
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
    '"Principal":{"Service":"ec2.amazonaws.com"},'
    '"Action":"sts:AssumeRole"}]}'
)
# Trust policy for the EFS CSI driver role, assumed through EKS Pod Identity.
_ASSUME_ROLE_POD_IDENTITY = (
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
    '"Principal":{"Service":"pods.eks.amazonaws.com"},'
    '"Action":["sts:AssumeRole","sts:TagSession"]}]}'
)

# The EFS CSI controller runs as efs-csi-controller-sa in kube-system; Pod
# Identity binds its IAM role (_ROLE_EFS_CSI) to that ServiceAccount.
_ROLE_EFS_CSI = "efs-csi"
_EFS_CSI_NAMESPACE = "kube-system"
_EFS_CSI_SERVICE_ACCOUNT = "efs-csi-controller-sa"

# Cluster autoscaler. EKS managed node groups carry a scalingConfig but nothing
# scales within it on their own — unlike GKE, whose control plane autoscales.
# We install the Kubernetes cluster autoscaler (DRA rules out Karpenter and EKS
# Auto Mode) so a pool's maxNodeCount is reachable headroom, matching GKE. The
# autoscaler runs as cluster-autoscaler in kube-system; Pod Identity binds its
# IAM role (_ROLE_CLUSTER_AUTOSCALER) to that ServiceAccount, and it discovers
# node groups by the tags EKS puts on their ASGs.
_ROLE_CLUSTER_AUTOSCALER = "cluster-autoscaler"
_AUTOSCALER_NAMESPACE = "kube-system"
_AUTOSCALER_SERVICE_ACCOUNT = "cluster-autoscaler"
_AUTOSCALER_CHART_REPO = "https://kubernetes.github.io/autoscaler"
_AUTOSCALER_CHART_NAME = "cluster-autoscaler"
_AUTOSCALER_CHART_VERSION = "9.57.0"

# IAM permissions the autoscaler needs to discover node groups and adjust their
# ASGs' desired capacity. The Full Cluster Autoscaler Features policy from
# https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/cloudprovider/aws/README.md.
_POLICY_CLUSTER_AUTOSCALER = (
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
)

# EKS Addons installed on every cluster. The vpc-cni addon provides pod
# networking, kube-proxy programs node iptables, and coredns provides
# in-cluster DNS. All three are required for a functional cluster.
_ADDONS = ("vpc-cni", "kube-proxy", "coredns")

# Annotation the provider sets on a managed resource with its external name
# (the cloud-assigned id). Read from the EFS FileSystem to learn its id.
_ANNOTATION_EXTERNAL_NAME = "crossplane.io/external-name"

# Name of the RWX StorageClass Modelplane composes for ModelCache when the
# user doesn't bring their own. EFS dynamic provisioning creates an access
# point per PVC inside the auto-provisioned filesystem.
_MANAGED_STORAGE_CLASS = "modelplane-rwx-efs"


def _kubeconfig_secret_name(xr: v1alpha1.EKSCluster) -> str:
    """Derive the kubeconfig secret name from the XR."""
    return resource.child_name(_name(xr.metadata), "kubeconfig")


def _cluster_name(xr: v1alpha1.EKSCluster) -> str:
    """The EKS cluster's name in AWS.

    Pinned to a deterministic, compose-time-known name (rather than left to a
    provider-generated external-name) because the cluster autoscaler discovers
    node groups by the ``k8s.io/cluster-autoscaler/<cluster-name>`` tag EKS puts
    on their ASGs, so its autoDiscovery.clusterName has to match this exactly.

    The name is derived from the XR's namespace and name. EKSCluster is
    namespaced but an EKS cluster name is account- and region-global, so the XR
    name alone would let two clusters in different namespaces collide on one AWS
    cluster. child_name folds both in and appends a hash for uniqueness.
    """
    return resource.child_name(_namespace(xr.metadata), _name(xr.metadata), "eks")


def _subnet_name(xr: v1alpha1.EKSCluster, az: str) -> str:
    """Derive a stable Crossplane resource name for the public subnet in az."""
    return resource.child_name(_name(xr.metadata), f"subnet-{az}")


def _private_subnet_name(xr: v1alpha1.EKSCluster, az: str) -> str:
    """Derive a stable Crossplane resource name for the private subnet in az."""
    return resource.child_name(_name(xr.metadata), f"private-subnet-{az}")


def _private_cidr(public_cidr: v1alpha1.SubnetCidr) -> str:
    """Derive a private subnet CIDR from its AZ's public one.

    The VPC is a /16 split into /20s. The public subnets take the low /20s
    (10.0.0.0/20, .16, .32); the private subnets mirror them in the high half
    by adding 48 to the third octet (10.0.48.0/20, .64, .80), so the two tiers
    never overlap and each private subnet shares its AZ with a public one.
    """
    cidr_str = public_cidr.root if hasattr(public_cidr, "root") else public_cidr
    network, mask = cidr_str.split("/")
    octets = network.split(".")
    octets[2] = str(int(octets[2]) + 48)
    return f"{'.'.join(octets)}/{mask}"


def _az(region: str, index: int) -> str:
    """Derive an Availability Zone name from a region and an index.

    AWS conventionally names AZs as ``<region><letter>`` where the letter
    starts at ``a`` for the first AZ. The function does not validate
    that the derived AZ actually exists in the region — invalid AZs will
    surface as managed-resource errors at apply time.
    """
    return f"{region}{chr(ord('a') + index)}"


class FunctionRunner(grpcv1.FunctionRunnerServiceServicer):
    """A FunctionRunner handles gRPC RunFunctionRequests."""

    def __init__(self) -> None:
        """Create a new FunctionRunner."""
        self.log = logging.get_logger()

    async def RunFunction(
        self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext | None
    ) -> fnv1.RunFunctionResponse:  # ty: ignore[invalid-method-override]  # the generated grpc servicer base is untyped
        """Run the function."""
        log = self.log.bind(tag=req.meta.tag)
        log.info("Running function")

        rsp = response.to(req)
        c = Composer(req, rsp)
        c.compose()
        return rsp


class Composer:
    def __init__(self, req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse) -> None:
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.EKSCluster(**resource.struct_to_dict(req.observed.composite.resource))

    def compose(self) -> None:
        self.compose_network()
        self.compose_iam()
        self.compose_cluster()
        self.compose_cluster_auth()
        self.compose_node_groups()
        self.compose_addons()
        self.compose_efs()
        self.compose_cluster_autoscaler()
        self.compose_efa_dra_driver()
        self.compose_provider_configs()
        self.compose_storage_class()
        self.write_status()
        self.mark_readiness()

    def compose_network(self) -> None:
        """Compose the VPC, its two subnet tiers, and internet routing.

        The VPC has a public and a private subnet per AZ. Public subnets route
        to the internet gateway and host the NAT gateway and the serving
        gateway's load balancer; they're tagged for ELB discovery. Private
        subnets route outbound through the NAT gateway and host the nodes, which
        have no public IP. This is the standard EKS topology: a node group that
        defines its own network interfaces (for EFA) can't be assigned a public
        IP, so nodes must reach the internet through NAT, while inbound traffic
        still arrives through a load balancer in the public subnets.
        """
        resource.update(
            self.rsp.desired.resources["vpc"],
            vpcv1beta1.VPC(
                spec=vpcv1beta1.Spec(
                    forProvider=vpcv1beta1.ForProvider(
                        region=self.xr.spec.region,
                        cidrBlock=self._networking().vpcCidr,
                        enableDnsHostnames=True,
                        enableDnsSupport=True,
                    ),
                ),
            ),
        )

        for i, cidr in enumerate(self._networking().subnetCidrs or []):
            az = _az(self.xr.spec.region, i)
            cidr_str = cidr.root if hasattr(cidr, "root") else cidr
            # Public subnet: IGW route, auto-assigned public IPs, ELB-tagged.
            resource.update(
                self.rsp.desired.resources[f"subnet-{i}"],
                subnetv1beta1.Subnet(
                    metadata=metav1.ObjectMeta(
                        name=_subnet_name(self.xr, az),
                        labels={_LABEL_AZ: az, _LABEL_TIER: _TIER_PUBLIC},
                    ),
                    spec=subnetv1beta1.Spec(
                        forProvider=subnetv1beta1.ForProvider(
                            region=self.xr.spec.region,
                            availabilityZone=az,
                            cidrBlock=cidr_str,
                            mapPublicIpOnLaunch=True,
                            tags={_TAG_ELB_ROLE: "1"},
                            vpcIdSelector=subnetv1beta1.VpcIdSelector(
                                matchControllerRef=True,
                            ),
                        ),
                    ),
                ),
            )
            # Private subnet: NAT route, no public IPs, hosts the nodes.
            resource.update(
                self.rsp.desired.resources[f"private-subnet-{i}"],
                subnetv1beta1.Subnet(
                    metadata=metav1.ObjectMeta(
                        name=_private_subnet_name(self.xr, az),
                        labels={_LABEL_AZ: az, _LABEL_TIER: _TIER_PRIVATE},
                    ),
                    spec=subnetv1beta1.Spec(
                        forProvider=subnetv1beta1.ForProvider(
                            region=self.xr.spec.region,
                            availabilityZone=az,
                            cidrBlock=_private_cidr(cidr),
                            mapPublicIpOnLaunch=False,
                            vpcIdSelector=subnetv1beta1.VpcIdSelector(
                                matchControllerRef=True,
                            ),
                        ),
                    ),
                ),
            )

        resource.update(
            self.rsp.desired.resources["internet-gateway"],
            igwv1beta1.InternetGateway(
                spec=igwv1beta1.Spec(
                    forProvider=igwv1beta1.ForProvider(
                        region=self.xr.spec.region,
                        vpcIdSelector=igwv1beta1.VpcIdSelector(
                            matchControllerRef=True,
                        ),
                    ),
                ),
            ),
        )

        # A single NAT gateway in the first public subnet gives the private
        # subnets outbound internet. One NAT (not one per AZ) keeps cost down;
        # cross-AZ NAT traffic is acceptable for this workload.
        resource.update(
            self.rsp.desired.resources["nat-eip"],
            eipv1beta1.EIP(
                spec=eipv1beta1.Spec(
                    forProvider=eipv1beta1.ForProvider(
                        region=self.xr.spec.region,
                        domain="vpc",
                    ),
                ),
            ),
        )
        nat_az = _az(self.xr.spec.region, 0)
        resource.update(
            self.rsp.desired.resources["nat-gateway"],
            natv1beta1.NATGateway(
                spec=natv1beta1.Spec(
                    forProvider=natv1beta1.ForProvider(
                        region=self.xr.spec.region,
                        allocationIdSelector=natv1beta1.AllocationIdSelector(
                            matchControllerRef=True,
                        ),
                        subnetIdSelector=natv1beta1.SubnetIdSelector(
                            matchControllerRef=True,
                            matchLabels={_LABEL_AZ: nat_az, _LABEL_TIER: _TIER_PUBLIC},
                        ),
                    ),
                ),
            ),
        )

        # Public route table: 0.0.0.0/0 -> IGW, associated with public subnets.
        resource.update(
            self.rsp.desired.resources["route-table"],
            rtv1beta1.RouteTable(
                metadata=metav1.ObjectMeta(labels={_LABEL_TIER: _TIER_PUBLIC}),
                spec=rtv1beta1.Spec(
                    forProvider=rtv1beta1.ForProvider(
                        region=self.xr.spec.region,
                        vpcIdSelector=rtv1beta1.VpcIdSelector(
                            matchControllerRef=True,
                        ),
                    ),
                ),
            ),
        )
        resource.update(
            self.rsp.desired.resources["route-default"],
            routev1beta1.Route(
                spec=routev1beta1.Spec(
                    forProvider=routev1beta1.ForProvider(
                        region=self.xr.spec.region,
                        destinationCidrBlock="0.0.0.0/0",
                        gatewayIdSelector=routev1beta1.GatewayIdSelector(
                            matchControllerRef=True,
                        ),
                        routeTableIdSelector=routev1beta1.RouteTableIdSelector(
                            matchControllerRef=True,
                            matchLabels={_LABEL_TIER: _TIER_PUBLIC},
                        ),
                    ),
                ),
            ),
        )

        # Private route table: 0.0.0.0/0 -> NAT, associated with private subnets.
        resource.update(
            self.rsp.desired.resources["private-route-table"],
            rtv1beta1.RouteTable(
                metadata=metav1.ObjectMeta(labels={_LABEL_TIER: _TIER_PRIVATE}),
                spec=rtv1beta1.Spec(
                    forProvider=rtv1beta1.ForProvider(
                        region=self.xr.spec.region,
                        vpcIdSelector=rtv1beta1.VpcIdSelector(
                            matchControllerRef=True,
                        ),
                    ),
                ),
            ),
        )
        resource.update(
            self.rsp.desired.resources["private-route-default"],
            routev1beta1.Route(
                spec=routev1beta1.Spec(
                    forProvider=routev1beta1.ForProvider(
                        region=self.xr.spec.region,
                        destinationCidrBlock="0.0.0.0/0",
                        natGatewayIdSelector=routev1beta1.NatGatewayIdSelector(
                            matchControllerRef=True,
                        ),
                        routeTableIdSelector=routev1beta1.RouteTableIdSelector(
                            matchControllerRef=True,
                            matchLabels={_LABEL_TIER: _TIER_PRIVATE},
                        ),
                    ),
                ),
            ),
        )

        for i in range(len(self._networking().subnetCidrs or [])):
            az = _az(self.xr.spec.region, i)
            resource.update(
                self.rsp.desired.resources[f"route-table-association-{i}"],
                rtav1beta1.RouteTableAssociation(
                    spec=rtav1beta1.Spec(
                        forProvider=rtav1beta1.ForProvider(
                            region=self.xr.spec.region,
                            routeTableIdSelector=rtav1beta1.RouteTableIdSelector(
                                matchControllerRef=True,
                                matchLabels={_LABEL_TIER: _TIER_PUBLIC},
                            ),
                            subnetIdSelector=rtav1beta1.SubnetIdSelector(
                                matchControllerRef=True,
                                matchLabels={_LABEL_AZ: az, _LABEL_TIER: _TIER_PUBLIC},
                            ),
                        ),
                    ),
                ),
            )
            resource.update(
                self.rsp.desired.resources[f"private-route-table-association-{i}"],
                rtav1beta1.RouteTableAssociation(
                    spec=rtav1beta1.Spec(
                        forProvider=rtav1beta1.ForProvider(
                            region=self.xr.spec.region,
                            routeTableIdSelector=rtav1beta1.RouteTableIdSelector(
                                matchControllerRef=True,
                                matchLabels={_LABEL_TIER: _TIER_PRIVATE},
                            ),
                            subnetIdSelector=rtav1beta1.SubnetIdSelector(
                                matchControllerRef=True,
                                matchLabels={_LABEL_AZ: az, _LABEL_TIER: _TIER_PRIVATE},
                            ),
                        ),
                    ),
                ),
            )

    def compose_iam(self) -> None:
        """Compose the cluster and node IAM roles."""
        resource.update(
            self.rsp.desired.resources["iam-role-cluster"],
            rolev1beta1.Role(
                metadata=metav1.ObjectMeta(labels={_LABEL_ROLE: _ROLE_CLUSTER}),
                spec=rolev1beta1.Spec(
                    forProvider=rolev1beta1.ForProvider(
                        assumeRolePolicy=_ASSUME_ROLE_CLUSTER,
                    ),
                ),
            ),
        )

        resource.update(
            self.rsp.desired.resources["iam-attach-cluster-policy"],
            rpav1beta1.RolePolicyAttachment(
                spec=rpav1beta1.Spec(
                    forProvider=rpav1beta1.ForProvider(
                        policyArn=_POLICY_CLUSTER,
                        roleSelector=rpav1beta1.RoleSelector(
                            matchControllerRef=True,
                            matchLabels={_LABEL_ROLE: _ROLE_CLUSTER},
                        ),
                    ),
                ),
            ),
        )

        resource.update(
            self.rsp.desired.resources["iam-role-node"],
            rolev1beta1.Role(
                metadata=metav1.ObjectMeta(labels={_LABEL_ROLE: _ROLE_NODE}),
                spec=rolev1beta1.Spec(
                    forProvider=rolev1beta1.ForProvider(
                        assumeRolePolicy=_ASSUME_ROLE_NODE,
                    ),
                ),
            ),
        )

        for key, arn in (
            ("iam-attach-node-worker", _POLICY_NODE_WORKER),
            ("iam-attach-node-cni", _POLICY_NODE_CNI),
            ("iam-attach-node-ecr", _POLICY_NODE_ECR),
        ):
            resource.update(
                self.rsp.desired.resources[key],
                rpav1beta1.RolePolicyAttachment(
                    spec=rpav1beta1.Spec(
                        forProvider=rpav1beta1.ForProvider(
                            policyArn=arn,
                            roleSelector=rpav1beta1.RoleSelector(
                                matchControllerRef=True,
                                matchLabels={_LABEL_ROLE: _ROLE_NODE},
                            ),
                        ),
                    ),
                ),
            )

    def compose_cluster(self) -> None:
        """Compose the EKS cluster."""
        resource.update(
            self.rsp.desired.resources["cluster"],
            clusterv1beta1.Cluster(
                metadata=metav1.ObjectMeta(name=_cluster_name(self.xr)),
                spec=clusterv1beta1.Spec(
                    forProvider=clusterv1beta1.ForProvider(
                        region=self.xr.spec.region,
                        version=self.xr.spec.kubernetesVersion,
                        roleArnSelector=clusterv1beta1.RoleArnSelector(
                            matchControllerRef=True,
                            matchLabels={_LABEL_ROLE: _ROLE_CLUSTER},
                        ),
                        accessConfig=clusterv1beta1.AccessConfig(
                            authenticationMode="API_AND_CONFIG_MAP",
                            bootstrapClusterCreatorAdminPermissions=True,
                        ),
                        vpcConfig=clusterv1beta1.VpcConfig(
                            endpointPrivateAccess=True,
                            endpointPublicAccess=True,
                            subnetIdSelector=clusterv1beta1.SubnetIdSelector(
                                matchControllerRef=True,
                            ),
                        ),
                    ),
                ),
            ),
        )

    def compose_cluster_auth(self) -> None:
        """Compose the ClusterAuth resource that writes a kubeconfig.

        ClusterAuth uses the AWS provider's own credentials to mint an
        EKS authentication token, and writes a kubeconfig Secret with
        that token embedded. It refreshes the token every refreshPeriod
        (default 10m) before it expires.
        """
        resource.update(
            self.rsp.desired.resources["cluster-auth"],
            clusterauthv1beta1.ClusterAuth(
                spec=clusterauthv1beta1.Spec(
                    forProvider=clusterauthv1beta1.ForProvider(
                        region=self.xr.spec.region,
                        clusterNameSelector=clusterauthv1beta1.ClusterNameSelector(
                            matchControllerRef=True,
                        ),
                    ),
                    writeConnectionSecretToRef=clusterauthv1beta1.WriteConnectionSecretToRef(
                        name=_kubeconfig_secret_name(self.xr),
                    ),
                ),
            ),
        )

    def compose_node_groups(self) -> None:
        """Compose the system and user-declared GPU node groups."""
        self._compose_system_node_group()
        for pool in self.xr.spec.nodePools:
            capacity_block = pool.capacityBlock
            efa = pool.fabric == _FABRIC_EFA
            # A launch template is needed to carry the capacity-block market
            # options, the EFA interfaces, or both. EKS takes the instance type
            # from the launch template whenever one is set, so the node group
            # must not also set instanceTypes in that case.
            uses_launch_template = bool(capacity_block) or efa
            if uses_launch_template:
                self._compose_launch_template(pool, capacity_block, efa=efa)
            if efa:
                self._compose_efa_security_group()

            fp = ngv1beta1.ForProvider(
                region=self.xr.spec.region,
                amiType=_AMI_TYPE_GPU if pool.role == "GPU" else _AMI_TYPE_SYSTEM,
                clusterNameSelector=ngv1beta1.ClusterNameSelector(
                    matchControllerRef=True,
                ),
                nodeRoleArnSelector=ngv1beta1.NodeRoleArnSelector(
                    matchControllerRef=True,
                    matchLabels={_LABEL_ROLE: _ROLE_NODE},
                ),
                # min/max are ours to enforce; desiredSize is seeded via
                # initProvider below so the autoscaler can move it freely.
                scalingConfig=ngv1beta1.ScalingConfig(
                    minSize=pool.minNodeCount,
                    maxSize=pool.maxNodeCount,
                ),
                labels={_LABEL_POOL: pool.name},
            )

            if uses_launch_template:
                # EKS takes the instance type from the launch template, so the
                # node group must not also set instanceTypes. The template
                # carries the instance type, the disk size, any capacity-block
                # market options, and any EFA interfaces. EKS rejects a node
                # group that sets diskSize alongside a launch template, so the
                # disk size lives only in the template in this case.
                fp.launchTemplate = ngv1beta1.LaunchTemplate(
                    name=self._launch_template_name(pool),
                    version="$Latest",
                )
                if capacity_block:
                    fp.capacityType = _CAPACITY_TYPE_CAPACITY_BLOCK
            else:
                fp.instanceTypes = [pool.instanceType]
                fp.diskSize = pool.diskSizeGb

            zone_refs = self._subnet_refs_for_pool(pool)
            if zone_refs:
                fp.subnetIdRefs = zone_refs
            else:
                fp.subnetIdSelector = ngv1beta1.SubnetIdSelector(
                    matchControllerRef=True,
                    matchLabels={_LABEL_TIER: _TIER_PRIVATE},
                )

            if pool.role == "GPU" and pool.gpu:
                fp.labels = {
                    _LABEL_GPU: pool.gpu.acceleratorType,
                    _LABEL_POOL: pool.name,
                }
                fp.taint = [
                    ngv1beta1.TaintItem(
                        key=_GPU_TAINT_KEY,
                        value=_GPU_TAINT_VALUE,
                        effect=_GPU_TAINT_EFFECT,
                    ),
                ]

            resource.update(
                self.rsp.desired.resources[f"nodegroup-{pool.name}"],
                ngv1beta1.NodeGroup(
                    spec=ngv1beta1.Spec(
                        managementPolicies=_NODE_GROUP_MANAGEMENT,
                        initProvider=ngv1beta1.InitProvider(
                            scalingConfig=ngv1beta1.ScalingConfig(desiredSize=pool.nodeCount),
                        ),
                        forProvider=fp,
                    ),
                ),
            )

    def _launch_template_name(self, pool: v1alpha1.NodePool) -> str:
        """Derive the EC2 launch template name for a Capacity Block pool.

        The EKS NodeGroup references the launch template by name, so it
        must be stable and match the launchTemplate.forProvider.name set
        on the composed EC2 LaunchTemplate.
        """
        return resource.child_name(_name(self.xr.metadata), f"lt-{pool.name}")

    def _compose_launch_template(
        self, pool: v1alpha1.NodePool, capacity_block: v1alpha1.CapacityBlock | None, *, efa: bool
    ) -> None:
        """Compose an EC2 launch template for a node group.

        EKS launches the node group's instances from this template. It carries
        the instance type and, depending on the pool, the capacity-block market
        options (so instances come from the reservation) and/or EFA network
        interfaces (so nodes get GPUDirect RDMA). The node group references it
        by name; for a capacity-block pool it also sets capacityType.

        The template must not set subnets - EKS rejects a launch template with
        subnet configuration for managed node groups and takes the subnets from
        the node group API instead. It must carry the disk size, because EKS
        rejects a node group that sets diskSize alongside a launch template and
        requires the size to come from the template instead.
        """
        fp = ltv1beta1.ForProvider(
            region=self.xr.spec.region,
            name=self._launch_template_name(pool),
            instanceType=pool.instanceType,
            blockDeviceMappings=[
                ltv1beta1.BlockDeviceMapping(
                    deviceName=_ROOT_DEVICE_NAME,
                    ebs=ltv1beta1.Ebs(volumeSize=pool.diskSizeGb),
                ),
            ],
        )

        if capacity_block:
            fp.instanceMarketOptions = ltv1beta1.InstanceMarketOptions(
                marketType=_MARKET_TYPE_CAPACITY_BLOCK,
            )
            fp.capacityReservationSpecification = ltv1beta1.CapacityReservationSpecification(
                capacityReservationPreference=_CR_PREFERENCE_ONLY,
                capacityReservationTarget=ltv1beta1.CapacityReservationTarget(
                    capacityReservationId=capacity_block.capacityReservationId,
                ),
            )

        if efa:
            fp.networkInterfaces = self._efa_network_interfaces(pool)

        resource.update(
            self.rsp.desired.resources[f"launch-template-{pool.name}"],
            ltv1beta1.LaunchTemplate(spec=ltv1beta1.Spec(forProvider=fp)),
        )

    def _efa_network_interfaces(self, pool: v1alpha1.NodePool) -> list[ltv1beta1.NetworkInterface]:
        """Build the launch template's EFA network interfaces for a pool.

        Every network card carries an EFA interface for maximum fabric
        bandwidth: card 0 is interfaceType efa (EFA-with-ENA, the primary that
        also carries IP traffic), cards 1..N are efa-only (dedicated RDMA, no
        IP, which nodeadm leaves unmanaged). Card 0 takes device index 0, the
        rest device index 1 on their own network card. No interface requests a
        public IP - EKS rejects associate_public_ip_address with multiple
        interfaces, and the nodes are in private subnets reaching the internet
        through the NAT gateway.

        Every interface carries both the EFA security group (for the
        self-referencing all-traffic rules EFA OS-bypass needs) and the cluster
        security group (for node <-> control plane traffic). Defining
        networkInterfaces makes these authoritative; EKS no longer attaches the
        cluster security group itself, so a missing cluster SG leaves the node
        unable to reach the API server.

        Both groups are set as raw IDs in securityGroups, not as
        securityGroupRefs. The provider resolves securityGroupRefs into the same
        securityGroups field, but its multi-reference resolver is a no-op once
        that field is already populated (it caches resolved values). Mixing a
        ref for the EFA group with a literal cluster-group ID would make the
        resolver skip the ref, dropping the EFA group from every interface the
        moment the cluster reports its SG. So we resolve the EFA group's own ID
        ourselves from its observed external name and set both as literals. Both
        IDs are absent on the first reconcile (the EFA SG isn't created and the
        cluster hasn't reported its SG yet); securityGroups stays unset until
        each lands, so the template gains them over the next reconciles.

        The card count is instance-type specific.
        """
        cards = _EFA_NETWORK_CARDS.get(pool.instanceType, _EFA_CARDS_DEFAULT)
        observed_sgs = (self._observed_efa_security_group_id(), self._observed_cluster_security_group_id())
        security_groups = [sg for sg in observed_sgs if sg]
        interfaces = []
        for card in range(cards):
            ni = ltv1beta1.NetworkInterface(
                networkCardIndex=card,
                deviceIndex=0 if card == 0 else 1,
                interfaceType=_INTERFACE_TYPE_EFA if card == 0 else _INTERFACE_TYPE_EFA_ONLY,
            )
            if security_groups:
                ni.securityGroups = security_groups
            interfaces.append(ni)
        return interfaces

    def _observed_efa_security_group_id(self) -> str | None:
        """The composed EFA security group's ID, from its observed MR's
        external-name annotation (the sg-xxxx ID the provider sets once it
        exists). None before the group is created, so the launch template is
        composed without it on the first reconcile and gains it once observed.
        """
        observed = self.req.observed.resources.get("efa-security-group")
        if not observed:
            return None
        sg = sgv1beta1.SecurityGroup.model_validate(resource.struct_to_dict(observed.resource))
        if not sg.metadata or not sg.metadata.annotations:
            return None
        return sg.metadata.annotations.get(_ANNOTATION_EXTERNAL_NAME)

    def _observed_cluster_security_group_id(self) -> str | None:
        """The EKS-managed cluster security group ID, from the observed cluster.

        EKS creates this group and reports it on the cluster's status; it's
        absent until the cluster exists. Returns None before then, so the launch
        template is composed without it on the first reconcile and gains it once
        the cluster reports it.
        """
        observed = self.req.observed.resources.get("cluster")
        if not observed:
            return None
        cluster = clusterv1beta1.Cluster.model_validate(resource.struct_to_dict(observed.resource))
        if not cluster.status or not cluster.status.atProvider or not cluster.status.atProvider.vpcConfig:
            return None
        return cluster.status.atProvider.vpcConfig.clusterSecurityGroupId

    def _efa_security_group_resource_name(self) -> str:
        """Object (metadata.name) of the shared EFA security group."""
        return resource.child_name(_name(self.xr.metadata), "efa-sg")

    def _compose_efa_security_group(self) -> None:
        """Compose the EFA security group and its self-referencing rules.

        EFA's OS-bypass transport requires every EFA interface to sit in a
        security group that allows all traffic to and from itself. One group
        serves every EFA pool in the cluster; the function composes it once even
        if several pools set fabric: EFA (resource.update is idempotent by key).
        """
        region = self.xr.spec.region
        resource.update(
            self.rsp.desired.resources["efa-security-group"],
            sgv1beta1.SecurityGroup(
                metadata=metav1.ObjectMeta(
                    name=self._efa_security_group_resource_name(),
                    labels={_LABEL_FABRIC: _FABRIC_EFA},
                ),
                spec=sgv1beta1.Spec(
                    forProvider=sgv1beta1.ForProvider(
                        region=region,
                        name=f"{_name(self.xr.metadata)}-efa",
                        description="EFA OS-bypass traffic between gang nodes",
                        vpcIdSelector=sgv1beta1.VpcIdSelector(matchControllerRef=True),
                    ),
                ),
            ),
        )
        resource.update(
            self.rsp.desired.resources["efa-security-group-ingress"],
            sgrv1beta1.SecurityGroupIngressRule(
                spec=sgrv1beta1.Spec(
                    forProvider=sgrv1beta1.ForProvider(
                        region=region,
                        ipProtocol="-1",
                        referencedSecurityGroupIdSelector=sgrv1beta1.ReferencedSecurityGroupIdSelector(
                            matchControllerRef=True,
                            matchLabels={_LABEL_FABRIC: _FABRIC_EFA},
                        ),
                        securityGroupIdSelector=sgrv1beta1.SecurityGroupIdSelector(
                            matchControllerRef=True,
                            matchLabels={_LABEL_FABRIC: _FABRIC_EFA},
                        ),
                    ),
                ),
            ),
        )
        resource.update(
            self.rsp.desired.resources["efa-security-group-egress"],
            sgev1beta1.SecurityGroupEgressRule(
                spec=sgev1beta1.Spec(
                    forProvider=sgev1beta1.ForProvider(
                        region=region,
                        ipProtocol="-1",
                        referencedSecurityGroupIdSelector=sgev1beta1.ReferencedSecurityGroupIdSelector(
                            matchControllerRef=True,
                            matchLabels={_LABEL_FABRIC: _FABRIC_EFA},
                        ),
                        securityGroupIdSelector=sgev1beta1.SecurityGroupIdSelector(
                            matchControllerRef=True,
                            matchLabels={_LABEL_FABRIC: _FABRIC_EFA},
                        ),
                    ),
                ),
            ),
        )

    def _compose_system_node_group(self) -> None:
        """Compose the system node group for control-plane components."""
        resource.update(
            self.rsp.desired.resources[f"nodegroup-{_SYSTEM_POOL_NAME}"],
            ngv1beta1.NodeGroup(
                spec=ngv1beta1.Spec(
                    managementPolicies=_NODE_GROUP_MANAGEMENT,
                    initProvider=ngv1beta1.InitProvider(
                        scalingConfig=ngv1beta1.ScalingConfig(desiredSize=_SYSTEM_POOL_NODE_COUNT),
                    ),
                    forProvider=ngv1beta1.ForProvider(
                        region=self.xr.spec.region,
                        amiType=_AMI_TYPE_SYSTEM,
                        instanceTypes=[_SYSTEM_POOL_INSTANCE_TYPE],
                        clusterNameSelector=ngv1beta1.ClusterNameSelector(
                            matchControllerRef=True,
                        ),
                        nodeRoleArnSelector=ngv1beta1.NodeRoleArnSelector(
                            matchControllerRef=True,
                            matchLabels={_LABEL_ROLE: _ROLE_NODE},
                        ),
                        subnetIdSelector=ngv1beta1.SubnetIdSelector(
                            matchControllerRef=True,
                            matchLabels={_LABEL_TIER: _TIER_PRIVATE},
                        ),
                        # min/max are ours to enforce; desiredSize is seeded via
                        # initProvider so the autoscaler can move it freely.
                        scalingConfig=ngv1beta1.ScalingConfig(
                            minSize=_SYSTEM_POOL_MIN_NODE_COUNT,
                            maxSize=_SYSTEM_POOL_MAX_NODE_COUNT,
                        ),
                        labels={_LABEL_POOL: _SYSTEM_POOL_NAME},
                    ),
                ),
            ),
        )

    def _subnet_refs_for_pool(self, pool: v1alpha1.NodePool) -> list[ngv1beta1.SubnetIdRef] | None:
        """Resolve a pool's zones to a list of private Crossplane Subnet refs.

        Nodes run in the private subnets (NAT egress, no public IP), so a pool
        references its AZs' private subnets by name. Pools without explicit
        zones return None so the NodeGroup's subnetIdSelector picks up the
        private subnets by tier label.
        """
        if not pool.zones:
            return None
        return [
            ngv1beta1.SubnetIdRef(name=_private_subnet_name(self.xr, z.root if hasattr(z, "root") else z))
            for z in pool.zones
        ]

    def compose_addons(self) -> None:
        for name in _ADDONS:
            resource.update(
                self.rsp.desired.resources[f"addon-{name}"],
                addonv1beta1.Addon(
                    spec=addonv1beta1.Spec(
                        forProvider=addonv1beta1.ForProvider(
                            region=self.xr.spec.region,
                            addonName=name,
                            clusterNameSelector=addonv1beta1.ClusterNameSelector(
                                matchControllerRef=True,
                            ),
                        ),
                    ),
                ),
            )

    def compose_efs(self) -> None:
        """Provision EFS RWX storage for ModelCache: an Elastic-throughput
        filesystem, a mount target per node subnet, an NFS security group, and
        the EFS CSI driver. The driver's IAM role is bound through Pod Identity
        (the eks-pod-identity-agent addon plus an association), so no OIDC
        provider is needed. compose_storage_class pins the modelplane-rwx-efs
        StorageClass to this filesystem's id."""
        region = self.xr.spec.region

        resource.update(
            self.rsp.desired.resources["efs-filesystem"],
            fsv1beta1.FileSystem(
                spec=fsv1beta1.Spec(
                    forProvider=fsv1beta1.ForProvider(
                        region=region,
                        throughputMode="elastic",
                        encrypted=True,
                    ),
                ),
            ),
        )

        # An NFS security group on the mount targets, reachable from any node in
        # the VPC (the nodes have no single stable SG to reference here). It
        # carries _LABEL_SG_ROLE so its ingress rule and the mount targets
        # select it specifically: an EFA pool composes a second controller-owned
        # SecurityGroup, and a bare matchControllerRef selector would match
        # either one.
        resource.update(
            self.rsp.desired.resources["efs-security-group"],
            sgv1beta1.SecurityGroup(
                metadata=metav1.ObjectMeta(labels={_LABEL_SG_ROLE: _SG_ROLE_EFS}),
                spec=sgv1beta1.Spec(
                    forProvider=sgv1beta1.ForProvider(
                        region=region,
                        name=f"{_name(self.xr.metadata)}-efs",
                        description="NFS access to the ModelCache EFS mount targets",
                        vpcIdSelector=sgv1beta1.VpcIdSelector(matchControllerRef=True),
                    ),
                ),
            ),
        )
        resource.update(
            self.rsp.desired.resources["efs-security-group-ingress"],
            sgrv1beta1.SecurityGroupIngressRule(
                spec=sgrv1beta1.Spec(
                    forProvider=sgrv1beta1.ForProvider(
                        region=region,
                        ipProtocol="tcp",
                        fromPort=2049,
                        toPort=2049,
                        cidrIpv4=self._networking().vpcCidr,
                        securityGroupIdSelector=sgrv1beta1.SecurityGroupIdSelector(
                            matchControllerRef=True,
                            matchLabels={_LABEL_SG_ROLE: _SG_ROLE_EFS},
                        ),
                    ),
                ),
            ),
        )

        # One mount target per node subnet so any AZ's nodes can mount the share.
        for i in range(len(self._networking().subnetCidrs or [])):
            az = _az(region, i)
            resource.update(
                self.rsp.desired.resources[f"efs-mount-target-{i}"],
                mtv1beta1.MountTarget(
                    spec=mtv1beta1.Spec(
                        forProvider=mtv1beta1.ForProvider(
                            region=region,
                            fileSystemIdSelector=mtv1beta1.FileSystemIdSelector(matchControllerRef=True),
                            # Mount targets live in the private subnets, where the
                            # nodes that mount them run.
                            subnetIdRef=mtv1beta1.SubnetIdRef(name=_private_subnet_name(self.xr, az)),
                            securityGroupsSelector=mtv1beta1.SecurityGroupsSelector(
                                matchControllerRef=True,
                                matchLabels={_LABEL_SG_ROLE: _SG_ROLE_EFS},
                            ),
                        ),
                    ),
                ),
            )

        # IAM role for the CSI driver, attached to the AWS-managed policy.
        resource.update(
            self.rsp.desired.resources["iam-role-efs-csi"],
            rolev1beta1.Role(
                metadata=metav1.ObjectMeta(labels={_LABEL_ROLE: _ROLE_EFS_CSI}),
                spec=rolev1beta1.Spec(
                    forProvider=rolev1beta1.ForProvider(assumeRolePolicy=_ASSUME_ROLE_POD_IDENTITY),
                ),
            ),
        )
        resource.update(
            self.rsp.desired.resources["iam-attach-efs-csi"],
            rpav1beta1.RolePolicyAttachment(
                spec=rpav1beta1.Spec(
                    forProvider=rpav1beta1.ForProvider(
                        policyArn=_POLICY_EFS_CSI,
                        roleSelector=rpav1beta1.RoleSelector(
                            matchControllerRef=True,
                            matchLabels={_LABEL_ROLE: _ROLE_EFS_CSI},
                        ),
                    ),
                ),
            ),
        )

        # Pod Identity: the agent addon, then an association binding the CSI
        # driver's ServiceAccount to the role above.
        resource.update(
            self.rsp.desired.resources["addon-eks-pod-identity-agent"],
            addonv1beta1.Addon(
                spec=addonv1beta1.Spec(
                    forProvider=addonv1beta1.ForProvider(
                        region=region,
                        addonName="eks-pod-identity-agent",
                        clusterNameSelector=addonv1beta1.ClusterNameSelector(matchControllerRef=True),
                    ),
                ),
            ),
        )
        resource.update(
            self.rsp.desired.resources["pod-identity-efs-csi"],
            piav1beta1.PodIdentityAssociation(
                spec=piav1beta1.Spec(
                    forProvider=piav1beta1.ForProvider(
                        region=region,
                        namespace=_EFS_CSI_NAMESPACE,
                        serviceAccount=_EFS_CSI_SERVICE_ACCOUNT,
                        clusterNameSelector=piav1beta1.ClusterNameSelector(matchControllerRef=True),
                        roleArnSelector=piav1beta1.RoleArnSelector(
                            matchControllerRef=True,
                            matchLabels={_LABEL_ROLE: _ROLE_EFS_CSI},
                        ),
                    ),
                ),
            ),
        )

        resource.update(
            self.rsp.desired.resources["addon-aws-efs-csi-driver"],
            addonv1beta1.Addon(
                spec=addonv1beta1.Spec(
                    forProvider=addonv1beta1.ForProvider(
                        region=region,
                        addonName="aws-efs-csi-driver",
                        clusterNameSelector=addonv1beta1.ClusterNameSelector(matchControllerRef=True),
                    ),
                ),
            ),
        )

    def compose_storage_class(self) -> None:
        """Compose the EFS RWX StorageClass on the workload cluster. Gated on
        the filesystem id: the StorageClass pins to it, and the id is known
        only once the FileSystem is observed. The Object is applied through the
        cluster's own provider-kubernetes ProviderConfig. StorageClass has no
        Ready condition, so use SuccessfulCreate (DeriveFromObject would
        hang)."""
        filesystem_id = self._observed_efs_filesystem_id()
        if not filesystem_id:
            return
        manifest = {
            "apiVersion": "storage.k8s.io/v1",
            "kind": "StorageClass",
            "metadata": {"name": _MANAGED_STORAGE_CLASS},
            "provisioner": "efs.csi.aws.com",
            "parameters": {"provisioningMode": "efs-ap", "fileSystemId": filesystem_id, "directoryPerms": "700"},
            "volumeBindingMode": "Immediate",
        }
        resource.update(
            self.rsp.desired.resources["storage-class-rwx-efs"],
            k8sobjv1alpha1.Object(
                metadata=metav1.ObjectMeta(namespace=_namespace(self.xr.metadata)),
                spec=k8sobjv1alpha1.Spec(
                    managementPolicies=_ORPHAN_MANAGEMENT,
                    providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                        kind="ProviderConfig",
                        name=_kubeconfig_secret_name(self.xr),
                    ),
                    readiness=k8sobjv1alpha1.Readiness(policy="SuccessfulCreate"),
                    forProvider=k8sobjv1alpha1.ForProvider(manifest=manifest),
                ),
            ),
        )
        self.rsp.desired.resources["storage-class-rwx-efs"].ready = fnv1.READY_TRUE

    def compose_cluster_autoscaler(self) -> None:
        """Provision the Kubernetes cluster autoscaler so GPU pools scale within
        their min/max, the way GKE's built-in autoscaler does. DRA rules out
        Karpenter and EKS Auto Mode, so we install the autoscaler ourselves: a
        custom IAM policy and role bound to its ServiceAccount through Pod
        Identity (the eks-pod-identity-agent addon, composed for EFS, is reused),
        and the cluster-autoscaler Helm chart on the cluster's own helm
        ProviderConfig. The autoscaler auto-discovers node groups by the ASG tags
        EKS applies (k8s.io/cluster-autoscaler/enabled and .../<cluster-name>)."""
        region = self.xr.spec.region

        # A custom IAM policy: the autoscaler needs ASG and EC2/EKS describe and
        # scale permissions that no AWS-managed policy grants.
        resource.update(
            self.rsp.desired.resources["iam-policy-cluster-autoscaler"],
            policyv1beta1.Policy(
                metadata=metav1.ObjectMeta(labels={_LABEL_ROLE: _ROLE_CLUSTER_AUTOSCALER}),
                spec=policyv1beta1.Spec(
                    forProvider=policyv1beta1.ForProvider(policy=_POLICY_CLUSTER_AUTOSCALER),
                ),
            ),
        )

        # The role the autoscaler assumes, and the attachment of the policy
        # above to it. Both selected by label, matching the EFS CSI pattern.
        resource.update(
            self.rsp.desired.resources["iam-role-cluster-autoscaler"],
            rolev1beta1.Role(
                metadata=metav1.ObjectMeta(labels={_LABEL_ROLE: _ROLE_CLUSTER_AUTOSCALER}),
                spec=rolev1beta1.Spec(
                    forProvider=rolev1beta1.ForProvider(assumeRolePolicy=_ASSUME_ROLE_POD_IDENTITY),
                ),
            ),
        )
        resource.update(
            self.rsp.desired.resources["iam-attach-cluster-autoscaler"],
            rpav1beta1.RolePolicyAttachment(
                spec=rpav1beta1.Spec(
                    forProvider=rpav1beta1.ForProvider(
                        policyArnSelector=rpav1beta1.PolicyArnSelector(
                            matchControllerRef=True,
                            matchLabels={_LABEL_ROLE: _ROLE_CLUSTER_AUTOSCALER},
                        ),
                        roleSelector=rpav1beta1.RoleSelector(
                            matchControllerRef=True,
                            matchLabels={_LABEL_ROLE: _ROLE_CLUSTER_AUTOSCALER},
                        ),
                    ),
                ),
            ),
        )

        # Pod Identity binds the role to the autoscaler's ServiceAccount. The
        # eks-pod-identity-agent addon is composed by compose_efs.
        resource.update(
            self.rsp.desired.resources["pod-identity-cluster-autoscaler"],
            piav1beta1.PodIdentityAssociation(
                spec=piav1beta1.Spec(
                    forProvider=piav1beta1.ForProvider(
                        region=region,
                        namespace=_AUTOSCALER_NAMESPACE,
                        serviceAccount=_AUTOSCALER_SERVICE_ACCOUNT,
                        clusterNameSelector=piav1beta1.ClusterNameSelector(matchControllerRef=True),
                        roleArnSelector=piav1beta1.RoleArnSelector(
                            matchControllerRef=True,
                            matchLabels={_LABEL_ROLE: _ROLE_CLUSTER_AUTOSCALER},
                        ),
                    ),
                ),
            ),
        )

        # The Helm release runs on the cluster's own helm ProviderConfig. Gate
        # it on the cluster being observed: until it exists, the ProviderConfig
        # can't reach a cluster and the release would just error.
        cluster_observed = "cluster" in self.req.observed.resources
        release_exists = "release-cluster-autoscaler" in self.req.observed.resources
        if not (cluster_observed or release_exists):
            return

        resource.update(
            self.rsp.desired.resources["release-cluster-autoscaler"],
            helmv1beta1.Release(
                metadata=metav1.ObjectMeta(namespace=_namespace(self.xr.metadata)),
                spec=helmv1beta1.Spec(
                    managementPolicies=_ORPHAN_MANAGEMENT,
                    providerConfigRef=helmv1beta1.ProviderConfigRef(
                        kind="ProviderConfig",
                        name=_kubeconfig_secret_name(self.xr),
                    ),
                    forProvider=helmv1beta1.ForProvider(
                        chart=helmv1beta1.Chart(
                            name=_AUTOSCALER_CHART_NAME,
                            repository=_AUTOSCALER_CHART_REPO,
                            version=_AUTOSCALER_CHART_VERSION,
                        ),
                        namespace=_AUTOSCALER_NAMESPACE,
                        values={
                            "cloudProvider": "aws",
                            "awsRegion": region,
                            "autoDiscovery": {"clusterName": _cluster_name(self.xr)},
                            "rbac": {"serviceAccount": {"name": _AUTOSCALER_SERVICE_ACCOUNT}},
                            # GPU pools span AZs (one ASG per AZ); balance them so
                            # gang-scheduled replicas don't pile onto one zone.
                            "extraArgs": {"balance-similar-node-groups": True},
                        },
                    ),
                ),
            ),
        )

    def compose_efa_dra_driver(self) -> None:
        """Compose the EFA DRA driver (DRANET), only when a pool uses the EFA
        fabric. Installed as a Helm release on the cluster's own helm
        ProviderConfig, like the autoscaler, and gated the same way: until the
        cluster is observed the ProviderConfig can't reach it and the release
        would just error.
        """
        if not any(p.fabric == _FABRIC_EFA for p in self.xr.spec.nodePools):
            return

        cluster_observed = "cluster" in self.req.observed.resources
        release_exists = "release-efa-dra-driver" in self.req.observed.resources
        if not (cluster_observed or release_exists):
            return

        resource.update(
            self.rsp.desired.resources["release-efa-dra-driver"],
            helmv1beta1.Release(
                metadata=metav1.ObjectMeta(namespace=_namespace(self.xr.metadata)),
                spec=helmv1beta1.Spec(
                    managementPolicies=_ORPHAN_MANAGEMENT,
                    providerConfigRef=helmv1beta1.ProviderConfigRef(
                        kind="ProviderConfig",
                        name=_kubeconfig_secret_name(self.xr),
                    ),
                    forProvider=helmv1beta1.ForProvider(
                        chart=helmv1beta1.Chart(
                            name=_EFA_DRA_DRIVER_CHART_NAME,
                            repository=_EFA_DRA_DRIVER_CHART_REPO,
                            version=_EFA_DRA_DRIVER_CHART_VERSION,
                        ),
                        namespace=_EFA_DRA_DRIVER_NAMESPACE,
                        # The driver's DaemonSet must run on the GPU nodes, which
                        # carry the nvidia.com/gpu taint. The chart tolerates only
                        # CriticalAddonsOnly by default, so without this it never
                        # schedules and no EFA ResourceSlices are published.
                        values={
                            "tolerations": [
                                {
                                    "key": _GPU_TAINT_KEY,
                                    "operator": "Exists",
                                    "effect": _GPU_TAINT_EFFECT_K8S,
                                },
                            ],
                        },
                    ),
                ),
            ),
        )

    def compose_provider_configs(self) -> None:
        kubeconfig_secret = _kubeconfig_secret_name(self.xr)
        resource.update(
            self.rsp.desired.resources["provider-config-kubernetes"],
            k8spcv1alpha1.ProviderConfig(
                metadata=metav1.ObjectMeta(name=kubeconfig_secret),
                spec=k8spcv1alpha1.Spec(
                    credentials=k8spcv1alpha1.Credentials(
                        source="Secret",
                        secretRef=k8spcv1alpha1.SecretRef(
                            name=kubeconfig_secret,
                            namespace=_namespace(self.xr.metadata),
                            key=_SECRET_KEY_KUBECONFIG,
                        ),
                    ),
                ),
            ),
        )

        resource.update(
            self.rsp.desired.resources["provider-config-helm"],
            helmpcv1beta1.ProviderConfig(
                metadata=metav1.ObjectMeta(name=kubeconfig_secret),
                spec=helmpcv1beta1.Spec(
                    credentials=helmpcv1beta1.Credentials(
                        source="Secret",
                        secretRef=helmpcv1beta1.SecretRef(
                            name=kubeconfig_secret,
                            namespace=_namespace(self.xr.metadata),
                            key=_SECRET_KEY_KUBECONFIG,
                        ),
                    ),
                ),
            ),
        )

    def write_status(self) -> None:
        status = v1alpha1.Status(
            secrets=[
                v1alpha1.Secret(
                    type=_SECRET_TYPE_KUBECONFIG,
                    name=_kubeconfig_secret_name(self.xr),
                    key=_SECRET_KEY_KUBECONFIG,
                ),
            ],
            # The RWX StorageClass Modelplane composes for ModelCache.
            # Published immediately so ModelCache can target it; the class may
            # still be materialising on the workload cluster.
            cache=v1alpha1.Cache(storageClassName=_MANAGED_STORAGE_CLASS),
        )
        resource.update_status(self.rsp.desired.composite, status)

    def _observed_efs_filesystem_id(self) -> str | None:
        """The composed EFS filesystem's id, from the observed FileSystem MR's
        external-name annotation (set by the provider once it exists). None on
        early reconciles before the filesystem is created."""
        observed = self.req.observed.resources.get("efs-filesystem")
        if not observed:
            return None
        fs = fsv1beta1.FileSystem.model_validate(resource.struct_to_dict(observed.resource))
        if not fs.metadata or not fs.metadata.annotations:
            return None
        return fs.metadata.annotations.get(_ANNOTATION_EXTERNAL_NAME)

    def mark_readiness(self) -> None:
        """Mark composed resources ready based on their observed conditions."""
        managed_resources = [
            "vpc",
            "internet-gateway",
            "route-table",
            "route-default",
            "nat-eip",
            "nat-gateway",
            "private-route-table",
            "private-route-default",
            "iam-role-cluster",
            "iam-attach-cluster-policy",
            "iam-role-node",
            "iam-attach-node-worker",
            "iam-attach-node-cni",
            "iam-attach-node-ecr",
            "cluster",
            "cluster-auth",
            f"nodegroup-{_SYSTEM_POOL_NAME}",
            "efs-filesystem",
            "efs-security-group",
            "efs-security-group-ingress",
            "iam-role-efs-csi",
            "iam-attach-efs-csi",
            "addon-eks-pod-identity-agent",
            "pod-identity-efs-csi",
            "addon-aws-efs-csi-driver",
            "iam-policy-cluster-autoscaler",
            "iam-role-cluster-autoscaler",
            "iam-attach-cluster-autoscaler",
            "pod-identity-cluster-autoscaler",
        ]
        for i in range(len(self._networking().subnetCidrs or [])):
            managed_resources.append(f"subnet-{i}")
            managed_resources.append(f"private-subnet-{i}")
            managed_resources.append(f"route-table-association-{i}")
            managed_resources.append(f"private-route-table-association-{i}")
            managed_resources.append(f"efs-mount-target-{i}")
        managed_resources += [f"nodegroup-{p.name}" for p in self.xr.spec.nodePools]
        managed_resources += [
            f"launch-template-{p.name}" for p in self.xr.spec.nodePools if p.capacityBlock or p.fabric == _FABRIC_EFA
        ]
        if any(p.fabric == _FABRIC_EFA for p in self.xr.spec.nodePools):
            managed_resources += [
                "efa-security-group",
                "efa-security-group-ingress",
                "efa-security-group-egress",
            ]
        managed_resources += [f"addon-{a}" for a in _ADDONS]
        # The autoscaler and EFA driver Helm releases are only composed once the
        # cluster is observed, so only mark them ready when they're actually in
        # desired state — touching them here otherwise would re-add a resource we
        # gated out.
        if "release-cluster-autoscaler" in self.rsp.desired.resources:
            managed_resources.append("release-cluster-autoscaler")
        if "release-efa-dra-driver" in self.rsp.desired.resources:
            managed_resources.append("release-efa-dra-driver")

        for r in managed_resources:
            if resource.get_condition(self.req.observed.resources.get(r), "Ready").status == "True":
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE

        self.rsp.desired.resources["provider-config-kubernetes"].ready = fnv1.READY_TRUE
        self.rsp.desired.resources["provider-config-helm"].ready = fnv1.READY_TRUE

    def _networking(self) -> v1alpha1.Networking:
        """Return the (defaulted) networking config from the XR."""
        return self.xr.spec.networking or v1alpha1.Networking()
