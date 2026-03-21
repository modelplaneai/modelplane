from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .model.io.upbound.m.gcp.compute.network import v1beta1 as networkv1beta1
from .model.io.upbound.m.gcp.compute.subnetwork import v1beta1 as subnetv1beta1
from .model.io.upbound.m.gcp.container.cluster import v1beta1 as clusterv1beta1
from .model.io.upbound.m.gcp.container.nodepool import v1beta1 as nodepoolv1beta1
from .model.io.upbound.m.gcp.cloudplatform.serviceaccount import v1beta1 as sav1beta1
from .model.io.upbound.m.gcp.cloudplatform.serviceaccountkey import v1beta1 as sakeyv1beta1
from .model.io.upbound.m.gcp.cloudplatform.projectiammember import v1beta1 as iamv1beta1
from .model.io.crossplane.m.kubernetes.providerconfig import v1alpha1 as k8spcv1alpha1
from .model.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.ai.modelplane.infrastructure.gkecluster import v1alpha1


def _has_condition(req: fnv1.RunFunctionRequest, name: str, cond: str) -> bool:
    """Check if an observed composed resource has the given condition True."""
    observed = req.observed.resources.get(name)
    if observed is None:
        return False
    return resource.get_condition(observed.resource, cond).status == "True"


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    xr = v1alpha1.GKECluster(**resource.struct_to_dict(req.observed.composite.resource))
    name = xr.metadata.name
    ns = xr.metadata.namespace
    spec = xr.spec

    networking = spec.networking or v1alpha1.Networking()
    pod_cidr = networking.podCidr or "10.1.0.0/16"
    svc_cidr = networking.serviceCidr or "10.2.0.0/16"
    node_cidr = networking.nodeCidr or "10.0.0.0/24"

    resource.update(
        rsp.desired.resources["network"],
        networkv1beta1.Network(
            spec=networkv1beta1.Spec(
                forProvider=networkv1beta1.ForProvider(
                    project=spec.project,
                    autoCreateSubnetworks=False,
                ),
            ),
        ),
    )

    resource.update(
        rsp.desired.resources["subnet"],
        subnetv1beta1.Subnetwork(
            spec=subnetv1beta1.Spec(
                forProvider=subnetv1beta1.ForProvider(
                    project=spec.project,
                    region=spec.region,
                    networkSelector=subnetv1beta1.NetworkSelector(
                        matchControllerRef=True,
                    ),
                    ipCidrRange=node_cidr,
                    secondaryIpRange=[
                        subnetv1beta1.SecondaryIpRangeItem(
                            rangeName="pods",
                            ipCidrRange=pod_cidr,
                        ),
                        subnetv1beta1.SecondaryIpRangeItem(
                            rangeName="services",
                            ipCidrRange=svc_cidr,
                        ),
                    ],
                ),
            ),
        ),
    )

    kubeconfig_secret_name = f"{name}-kubeconfig"

    resource.update(
        rsp.desired.resources["cluster"],
        clusterv1beta1.Cluster(
            spec=clusterv1beta1.Spec(
                forProvider=clusterv1beta1.ForProvider(
                    project=spec.project,
                    location=spec.region,
                    deletionProtection=False,
                    removeDefaultNodePool=True,
                    initialNodeCount=1,
                    minMasterVersion=spec.kubernetesVersion or "1.35",
                    networkSelector=clusterv1beta1.NetworkSelector(
                        matchControllerRef=True,
                    ),
                    subnetworkSelector=clusterv1beta1.SubnetworkSelector(
                        matchControllerRef=True,
                    ),
                    ipAllocationPolicy=clusterv1beta1.IpAllocationPolicy(
                        clusterSecondaryRangeName="pods",
                        servicesSecondaryRangeName="services",
                    ),
                    releaseChannel=clusterv1beta1.ReleaseChannel(
                        channel="REGULAR",
                    ),
                    workloadIdentityConfig=clusterv1beta1.WorkloadIdentityConfig(
                        workloadPool=f"{spec.project}.svc.id.goog",
                    ),
                ),
                writeConnectionSecretToRef=clusterv1beta1.WriteConnectionSecretToRef(
                    name=kubeconfig_secret_name,
                    namespace=ns,
                ),
            ),
        ),
    )

    for pool in spec.nodePools:
        node_config = nodepoolv1beta1.NodeConfig(
            machineType=pool.machineType,
            diskSizeGb=pool.diskSizeGb or 100,
            imageType="COS_CONTAINERD",
            oauthScopes=[
                "https://www.googleapis.com/auth/cloud-platform",
            ],
        )

        if pool.role == "GPU" and pool.gpu:
            node_config.guestAccelerator = [
                nodepoolv1beta1.GuestAcceleratorItem(
                    type=pool.gpu.acceleratorType,
                    count=pool.gpu.acceleratorCount or 1,
                    gpuDriverInstallationConfig=nodepoolv1beta1.GpuDriverInstallationConfig(
                        gpuDriverVersion="DEFAULT",
                    ),
                ),
            ]
            node_config.labels = {
                "modelplane.ai/gpu": pool.gpu.acceleratorType or "unknown",
                "modelplane.ai/pool": pool.name,
            }
        else:
            node_config.labels = {
                "modelplane.ai/pool": pool.name,
            }

        np = nodepoolv1beta1.NodePool(
            spec=nodepoolv1beta1.Spec(
                forProvider=nodepoolv1beta1.ForProvider(
                    project=spec.project,
                    location=spec.region,
                    clusterSelector=nodepoolv1beta1.ClusterSelector(
                        matchControllerRef=True,
                    ),
                    initialNodeCount=pool.nodeCount or 1,
                    autoscaling=nodepoolv1beta1.Autoscaling(
                        minNodeCount=pool.minNodeCount or 0,
                        maxNodeCount=pool.maxNodeCount or 8,
                    ),
                    nodeConfig=node_config,
                ),
            ),
        )

        if pool.zones:
            np.spec.forProvider.nodeLocations = pool.zones

        resource.update(
            rsp.desired.resources[f"nodepool-{pool.name}"],
            np,
        )

    sa_key_secret_name = f"{name}-sa-key"

    resource.update(
        rsp.desired.resources["service-account"],
        sav1beta1.ServiceAccount(
            spec=sav1beta1.Spec(
                forProvider=sav1beta1.ForProvider(
                    project=spec.project,
                    displayName=f"Crossplane GKECluster {name}",
                ),
            ),
        ),
    )

    resource.update(
        rsp.desired.resources["service-account-key"],
        sakeyv1beta1.ServiceAccountKey(
            spec=sakeyv1beta1.Spec(
                forProvider=sakeyv1beta1.ForProvider(
                    serviceAccountIdSelector=sakeyv1beta1.ServiceAccountIdSelector(
                        matchControllerRef=True,
                    ),
                ),
                writeConnectionSecretToRef=sakeyv1beta1.WriteConnectionSecretToRef(
                    name=sa_key_secret_name,
                    namespace=ns,
                ),
            ),
        ),
    )

    sa_email = None
    observed_sa = req.observed.resources.get("service-account")
    if observed_sa:
        sa_email = (
            resource.struct_to_dict(observed_sa.resource)
            .get("status", {})
            .get("atProvider", {})
            .get("email")
        )

    if sa_email:
        resource.update(
            rsp.desired.resources["iam-binding"],
            iamv1beta1.ProjectIAMMember(
                spec=iamv1beta1.Spec(
                    forProvider=iamv1beta1.ForProvider(
                        project=spec.project,
                        role="roles/container.admin",
                        member=f"serviceAccount:{sa_email}",
                    ),
                ),
            ),
        )

    pc_name = f"{name}-kubeconfig"

    resource.update(
        rsp.desired.resources["provider-config-kubernetes"],
        k8spcv1alpha1.ProviderConfig(
            metadata=metav1.ObjectMeta(name=pc_name),
            spec=k8spcv1alpha1.Spec(
                credentials=k8spcv1alpha1.Credentials(
                    source="Secret",
                    secretRef=k8spcv1alpha1.SecretRef(
                        name=kubeconfig_secret_name,
                        namespace=ns,
                        key="kubeconfig",
                    ),
                ),
                identity=k8spcv1alpha1.Identity(
                    type="GoogleApplicationCredentials",
                    source="Secret",
                    secretRef=k8spcv1alpha1.SecretRef(
                        name=sa_key_secret_name,
                        namespace=ns,
                        key="private_key",
                    ),
                ),
            ),
        ),
    )

    resource.update(
        rsp.desired.resources["provider-config-helm"],
        helmpcv1beta1.ProviderConfig(
            metadata=metav1.ObjectMeta(name=pc_name),
            spec=helmpcv1beta1.Spec(
                credentials=helmpcv1beta1.Credentials(
                    source="Secret",
                    secretRef=helmpcv1beta1.SecretRef(
                        name=kubeconfig_secret_name,
                        namespace=ns,
                        key="kubeconfig",
                    ),
                ),
                identity=helmpcv1beta1.Identity(
                    type="GoogleApplicationCredentials",
                    source="Secret",
                    secretRef=helmpcv1beta1.SecretRef(
                        name=sa_key_secret_name,
                        namespace=ns,
                        key="private_key",
                    ),
                ),
            ),
        ),
    )

    resource.update(rsp.desired.composite, {
        "status": {
            "secrets": [
                {
                    "type": "Kubeconfig",
                    "name": kubeconfig_secret_name,
                    "key": "kubeconfig",
                },
                {
                    "type": "GCPServiceAccountKey",
                    "name": sa_key_secret_name,
                    "key": "private_key",
                },
            ],
        },
    })

    managed_resources = ["network", "subnet", "cluster", "service-account", "service-account-key"]
    managed_resources += [f"nodepool-{pool.name}" for pool in spec.nodePools]
    if sa_email:
        managed_resources.append("iam-binding")

    all_ready = True
    not_ready = []
    for r in managed_resources:
        if _has_condition(req, r, "Ready"):
            rsp.desired.resources[r].ready = fnv1.READY_TRUE
        else:
            all_ready = False
            not_ready.append(r)

    rsp.desired.resources["provider-config-kubernetes"].ready = fnv1.READY_TRUE
    rsp.desired.resources["provider-config-helm"].ready = fnv1.READY_TRUE

    if all_ready:
        rsp.conditions.append(fnv1.Condition(
            type="Ready",
            status=fnv1.STATUS_CONDITION_TRUE,
            reason="Available",
            target=fnv1.TARGET_COMPOSITE_AND_CLAIM,
        ))
    else:
        rsp.conditions.append(fnv1.Condition(
            type="Ready",
            status=fnv1.STATUS_CONDITION_FALSE,
            reason="Creating",
            message=f"Waiting for: {', '.join(not_ready)}",
            target=fnv1.TARGET_COMPOSITE_AND_CLAIM,
        ))
