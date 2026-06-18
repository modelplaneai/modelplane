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
from models.io.upbound.m.aws.ec2.internetgateway import v1beta1 as igwv1beta1
from models.io.upbound.m.aws.ec2.launchtemplate import v1beta1 as ltv1beta1
from models.io.upbound.m.aws.ec2.route import v1beta1 as routev1beta1
from models.io.upbound.m.aws.ec2.routetable import v1beta1 as rtv1beta1
from models.io.upbound.m.aws.ec2.routetableassociation import v1beta1 as rtav1beta1
from models.io.upbound.m.aws.ec2.securitygroup import v1beta1 as sgv1beta1
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

# Node group management policies that exclude LateInitialize, so the
# desiredSize we seed via initProvider is applied only at creation and then
# left alone. The cluster autoscaler drives the ASG's desired capacity; without
# this Crossplane would keep reverting desiredSize to nodeCount and fight it.
# (initProvider is a beta feature gated on enumerating management policies — the
# default "*" still reconciles forProvider, defeating the purpose.)
_NODE_GROUP_MANAGEMENT = ["Observe", "Create", "Update", "Delete"]

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

# Internal labels written on composed AWS resources so other resources
# can select them. _LABEL_ROLE distinguishes the cluster IAM role from
# the node IAM role. _LABEL_AZ tags each subnet with its Availability
# Zone so NodeGroup subnetIdSelector can pick the right subnets.
_LABEL_ROLE = "modelplane.ai/iam-role"
_LABEL_AZ = "modelplane.ai/zone"

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

# GPU taint applied to GPU node groups so non-GPU pods don't land on
# expensive GPU nodes.
_GPU_TAINT_KEY = "nvidia.com/gpu"
_GPU_TAINT_VALUE = "true"
_GPU_TAINT_EFFECT = "NO_SCHEDULE"

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


def _kubeconfig_secret_name(xr):
    """Derive the kubeconfig secret name from the XR."""
    return resource.child_name(xr.metadata.name, "kubeconfig")


def _cluster_name(xr):
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
    return resource.child_name(xr.metadata.namespace, xr.metadata.name, "eks")


def _subnet_name(xr, az):
    """Derive a stable Crossplane resource name for the subnet in az."""
    return resource.child_name(xr.metadata.name, f"subnet-{az}")


def _az(region, index):
    """Derive an Availability Zone name from a region and an index.

    AWS conventionally names AZs as ``<region><letter>`` where the letter
    starts at ``a`` for the first AZ. The function does not validate
    that the derived AZ actually exists in the region — invalid AZs will
    surface as managed-resource errors at apply time.
    """
    return f"{region}{chr(ord('a') + index)}"


class FunctionRunner(grpcv1.FunctionRunnerService):
    """A FunctionRunner handles gRPC RunFunctionRequests."""

    def __init__(self):
        """Create a new FunctionRunner."""
        self.log = logging.get_logger()

    async def RunFunction(self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext) -> fnv1.RunFunctionResponse:
        """Run the function."""
        log = self.log.bind(tag=req.meta.tag)
        log.info("Running function")

        rsp = response.to(req)
        c = Composer(req, rsp)
        c.compose()
        return rsp


class Composer:
    def __init__(self, req, rsp):
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.EKSCluster(**resource.struct_to_dict(req.observed.composite.resource))

    def compose(self):
        self.compose_network()
        self.compose_iam()
        self.compose_cluster()
        self.compose_cluster_auth()
        self.compose_node_groups()
        self.compose_addons()
        self.compose_efs()
        self.compose_cluster_autoscaler()
        self.compose_provider_configs()
        self.compose_storage_class()
        self.write_status()
        self.mark_readiness()

    def compose_network(self):
        """Compose the VPC, subnets, and internet routing."""
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

        for i, cidr in enumerate(self._networking().subnetCidrs):
            az = _az(self.xr.spec.region, i)
            cidr_str = cidr.root if hasattr(cidr, "root") else cidr
            resource.update(
                self.rsp.desired.resources[f"subnet-{i}"],
                subnetv1beta1.Subnet(
                    metadata=metav1.ObjectMeta(
                        name=_subnet_name(self.xr, az),
                        labels={_LABEL_AZ: az},
                    ),
                    spec=subnetv1beta1.Spec(
                        forProvider=subnetv1beta1.ForProvider(
                            region=self.xr.spec.region,
                            availabilityZone=az,
                            cidrBlock=cidr_str,
                            mapPublicIpOnLaunch=True,
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

        resource.update(
            self.rsp.desired.resources["route-table"],
            rtv1beta1.RouteTable(
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
                        ),
                    ),
                ),
            ),
        )

        for i in range(len(self._networking().subnetCidrs)):
            az = _az(self.xr.spec.region, i)
            resource.update(
                self.rsp.desired.resources[f"route-table-association-{i}"],
                rtav1beta1.RouteTableAssociation(
                    spec=rtav1beta1.Spec(
                        forProvider=rtav1beta1.ForProvider(
                            region=self.xr.spec.region,
                            routeTableIdSelector=rtav1beta1.RouteTableIdSelector(
                                matchControllerRef=True,
                            ),
                            subnetIdSelector=rtav1beta1.SubnetIdSelector(
                                matchControllerRef=True,
                                matchLabels={_LABEL_AZ: az},
                            ),
                        ),
                    ),
                ),
            )

    def compose_iam(self):
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

    def compose_cluster(self):
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

    def compose_cluster_auth(self):
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

    def compose_node_groups(self):
        """Compose the system and user-declared GPU node groups."""
        self._compose_system_node_group()
        for pool in self.xr.spec.nodePools:
            capacity_block = pool.capacityBlock
            if capacity_block:
                self._compose_launch_template(pool, capacity_block)

            fp = ngv1beta1.ForProvider(
                region=self.xr.spec.region,
                amiType=_AMI_TYPE_GPU if pool.role == "GPU" else _AMI_TYPE_SYSTEM,
                diskSize=pool.diskSizeGb,
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

            if capacity_block:
                # For a Capacity Block node group EKS takes the instance
                # type from the launch template, so the node group must not
                # also set instanceTypes. The launch template carries the
                # instance type and the capacity-block market options.
                fp.capacityType = _CAPACITY_TYPE_CAPACITY_BLOCK
                fp.launchTemplate = ngv1beta1.LaunchTemplate(
                    name=self._launch_template_name(pool),
                    version="$Latest",
                )
            else:
                fp.instanceTypes = [pool.instanceType]

            zone_refs = self._subnet_refs_for_pool(pool)
            if zone_refs:
                fp.subnetIdRefs = zone_refs
            else:
                fp.subnetIdSelector = ngv1beta1.SubnetIdSelector(matchControllerRef=True)

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

    def _launch_template_name(self, pool):
        """Derive the EC2 launch template name for a Capacity Block pool.

        The EKS NodeGroup references the launch template by name, so it
        must be stable and match the launchTemplate.forProvider.name set
        on the composed EC2 LaunchTemplate.
        """
        return resource.child_name(self.xr.metadata.name, f"lt-{pool.name}")

    def _compose_launch_template(self, pool, capacity_block):
        """Compose an EC2 launch template targeting a Capacity Block.

        The launch template carries the instance type and the capacity-block
        market options. EKS launches the node group's instances from this
        template into the reservation. The node group references it by name
        and sets capacityType=CAPACITY_BLOCK.
        """
        resource.update(
            self.rsp.desired.resources[f"launch-template-{pool.name}"],
            ltv1beta1.LaunchTemplate(
                spec=ltv1beta1.Spec(
                    forProvider=ltv1beta1.ForProvider(
                        region=self.xr.spec.region,
                        name=self._launch_template_name(pool),
                        instanceType=pool.instanceType,
                        instanceMarketOptions=ltv1beta1.InstanceMarketOptions(
                            marketType=_MARKET_TYPE_CAPACITY_BLOCK,
                        ),
                        capacityReservationSpecification=ltv1beta1.CapacityReservationSpecification(
                            capacityReservationPreference=_CR_PREFERENCE_ONLY,
                            capacityReservationTarget=ltv1beta1.CapacityReservationTarget(
                                capacityReservationId=capacity_block.capacityReservationId,
                            ),
                        ),
                    ),
                ),
            ),
        )

    def _compose_system_node_group(self):
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

    def _subnet_refs_for_pool(self, pool):
        """Resolve a pool's zones to a list of Crossplane Subnet refs.

        Subnets are composed with deterministic names derived from the
        XR name and the AZ. Pools that pin to specific zones reference
        them by name. Pools without explicit zones return None so the
        NodeGroup's subnetIdSelector picks up all controller-owned
        subnets via matchControllerRef.
        """
        if not pool.zones:
            return None
        return [
            ngv1beta1.SubnetIdRef(name=_subnet_name(self.xr, z.root if hasattr(z, "root") else z)) for z in pool.zones
        ]

    def compose_addons(self):
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

    def compose_efs(self):
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
        # the VPC (the nodes have no single stable SG to reference here).
        resource.update(
            self.rsp.desired.resources["efs-security-group"],
            sgv1beta1.SecurityGroup(
                spec=sgv1beta1.Spec(
                    forProvider=sgv1beta1.ForProvider(
                        region=region,
                        name=f"{self.xr.metadata.name}-efs",
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
                        securityGroupIdSelector=sgrv1beta1.SecurityGroupIdSelector(matchControllerRef=True),
                    ),
                ),
            ),
        )

        # One mount target per node subnet so any AZ's nodes can mount the share.
        for i in range(len(self._networking().subnetCidrs)):
            az = _az(region, i)
            resource.update(
                self.rsp.desired.resources[f"efs-mount-target-{i}"],
                mtv1beta1.MountTarget(
                    spec=mtv1beta1.Spec(
                        forProvider=mtv1beta1.ForProvider(
                            region=region,
                            fileSystemIdSelector=mtv1beta1.FileSystemIdSelector(matchControllerRef=True),
                            subnetIdRef=mtv1beta1.SubnetIdRef(name=_subnet_name(self.xr, az)),
                            securityGroupsSelector=mtv1beta1.SecurityGroupsSelector(matchControllerRef=True),
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

    def compose_storage_class(self):
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
                metadata=metav1.ObjectMeta(namespace=self.xr.metadata.namespace),
                spec=k8sobjv1alpha1.Spec(
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

    def compose_cluster_autoscaler(self):
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
                metadata=metav1.ObjectMeta(namespace=self.xr.metadata.namespace),
                spec=helmv1beta1.Spec(
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

    def compose_provider_configs(self):
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
                            namespace=self.xr.metadata.namespace,
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
                            namespace=self.xr.metadata.namespace,
                            key=_SECRET_KEY_KUBECONFIG,
                        ),
                    ),
                ),
            ),
        )

    def write_status(self):
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

    def _observed_efs_filesystem_id(self):
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

    def mark_readiness(self):
        """Mark composed resources ready based on their observed conditions."""
        managed_resources = [
            "vpc",
            "internet-gateway",
            "route-table",
            "route-default",
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
        for i in range(len(self._networking().subnetCidrs)):
            managed_resources.append(f"subnet-{i}")
            managed_resources.append(f"route-table-association-{i}")
            managed_resources.append(f"efs-mount-target-{i}")
        managed_resources += [f"nodegroup-{p.name}" for p in self.xr.spec.nodePools]
        managed_resources += [f"launch-template-{p.name}" for p in self.xr.spec.nodePools if p.capacityBlock]
        managed_resources += [f"addon-{a}" for a in _ADDONS]
        # The autoscaler Helm release is only composed once the cluster is
        # observed, so only mark it ready when it's actually in desired state —
        # touching it here otherwise would re-add the resource we gated out.
        if "release-cluster-autoscaler" in self.rsp.desired.resources:
            managed_resources.append("release-cluster-autoscaler")

        for r in managed_resources:
            if resource.get_condition(self.req.observed.resources.get(r), "Ready").status == "True":
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE

        self.rsp.desired.resources["provider-config-kubernetes"].ready = fnv1.READY_TRUE
        self.rsp.desired.resources["provider-config-helm"].ready = fnv1.READY_TRUE

    def _networking(self):
        """Return the (defaulted) networking config from the XR."""
        return self.xr.spec.networking or v1alpha1.Networking()
