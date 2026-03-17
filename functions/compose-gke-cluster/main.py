from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .model.io.upbound.gcp.compute.network import v1beta1 as networkv1beta1
from .model.io.upbound.gcp.compute.subnetwork import v1beta2 as subnetv1beta2
from .model.io.upbound.gcp.container.cluster import v1beta2 as clusterv1beta2
from .model.io.upbound.gcp.container.nodepool import v1beta2 as nodepoolv1beta2
from .model.ai.modelplane.infrastructure.gkecluster import v1alpha1


def _is_ready(req: fnv1.RunFunctionRequest, name: str) -> bool:
    observed = req.observed.resources.get(name)
    if observed is None:
        return False
    c = resource.get_condition(observed.resource, "Ready")
    return c.status == "True"


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    xr = v1alpha1.GKECluster(**resource.struct_to_dict(req.observed.composite.resource))
    name = xr.metadata.name
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
        subnetv1beta2.Subnetwork(
            spec=subnetv1beta2.Spec(
                forProvider=subnetv1beta2.ForProvider(
                    project=spec.project,
                    region=spec.region,
                    networkSelector=subnetv1beta2.NetworkSelector(
                        matchControllerRef=True,
                    ),
                    ipCidrRange=node_cidr,
                    secondaryIpRange=[
                        subnetv1beta2.SecondaryIpRangeItem(
                            rangeName="pods",
                            ipCidrRange=pod_cidr,
                        ),
                        subnetv1beta2.SecondaryIpRangeItem(
                            rangeName="services",
                            ipCidrRange=svc_cidr,
                        ),
                    ],
                ),
            ),
        ),
    )

    resource.update(
        rsp.desired.resources["cluster"],
        clusterv1beta2.Cluster(
            spec=clusterv1beta2.Spec(
                forProvider=clusterv1beta2.ForProvider(
                    project=spec.project,
                    location=spec.region,
                    deletionProtection=False,
                    removeDefaultNodePool=True,
                    initialNodeCount=1,
                    minMasterVersion=spec.kubernetesVersion or "1.35",
                    networkSelector=clusterv1beta2.NetworkSelector(
                        matchControllerRef=True,
                    ),
                    subnetworkSelector=clusterv1beta2.SubnetworkSelector(
                        matchControllerRef=True,
                    ),
                    ipAllocationPolicy=clusterv1beta2.IpAllocationPolicy(
                        clusterSecondaryRangeName="pods",
                        servicesSecondaryRangeName="services",
                    ),
                    releaseChannel=clusterv1beta2.ReleaseChannel(
                        channel="REGULAR",
                    ),
                    workloadIdentityConfig=clusterv1beta2.WorkloadIdentityConfig(
                        workloadPool=f"{spec.project}.svc.id.goog",
                    ),
                    masterAuth=clusterv1beta2.MasterAuth(
                        clientCertificateConfig=clusterv1beta2.ClientCertificateConfig(
                            issueClientCertificate=True,
                        ),
                    ),
                ),
                writeConnectionSecretToRef=clusterv1beta2.WriteConnectionSecretToRef(
                    name=f"{name}-kubeconfig",
                    namespace="crossplane-system",
                ),
            ),
        ),
    )

    for pool in spec.nodePools:
        node_config = nodepoolv1beta2.NodeConfig(
            machineType=pool.machineType,
            diskSizeGb=pool.diskSizeGb or 100,
            imageType="COS_CONTAINERD",
            oauthScopes=[
                "https://www.googleapis.com/auth/cloud-platform",
            ],
        )

        if pool.role == "GPU" and pool.gpu:
            node_config.guestAccelerator = [
                nodepoolv1beta2.GuestAcceleratorItem(
                    type=pool.gpu.acceleratorType,
                    count=pool.gpu.acceleratorCount or 1,
                    gpuDriverInstallationConfig=nodepoolv1beta2.GpuDriverInstallationConfig(
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

        np = nodepoolv1beta2.NodePool(
            spec=nodepoolv1beta2.Spec(
                forProvider=nodepoolv1beta2.ForProvider(
                    project=spec.project,
                    location=spec.region,
                    clusterSelector=nodepoolv1beta2.ClusterSelector(
                        matchControllerRef=True,
                    ),
                    initialNodeCount=pool.nodeCount or 1,
                    autoscaling=nodepoolv1beta2.Autoscaling(
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

    pc_name = f"{name}-kubeconfig"

    resource.update(
        rsp.desired.resources["provider-config-kubernetes"],
        {
            "apiVersion": "kubernetes.crossplane.io/v1alpha1",
            "kind": "ProviderConfig",
            "metadata": {"name": pc_name},
            "spec": {
                "credentials": {
                    "source": "Secret",
                    "secretRef": {
                        "name": f"{name}-kubeconfig",
                        "namespace": "crossplane-system",
                        "key": "kubeconfig",
                    },
                },
            },
        },
    )

    resource.update(
        rsp.desired.resources["provider-config-helm"],
        {
            "apiVersion": "helm.crossplane.io/v1beta1",
            "kind": "ProviderConfig",
            "metadata": {"name": pc_name},
            "spec": {
                "credentials": {
                    "source": "Secret",
                    "secretRef": {
                        "name": f"{name}-kubeconfig",
                        "namespace": "crossplane-system",
                        "key": "kubeconfig",
                    },
                },
            },
        },
    )

    resource.update(rsp.desired.composite, {
        "status": {
            "providerConfigRef": {"name": pc_name},
        },
    })

    managed_resources = ["network", "subnet", "cluster"]
    managed_resources += [f"nodepool-{pool.name}" for pool in spec.nodePools]

    all_ready = True
    not_ready = []
    for r in managed_resources:
        if _is_ready(req, r):
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
