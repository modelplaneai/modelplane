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

"""Compose a CoreWeave Kubernetes Service (CKS) cluster with node pools.

CKS has no cloud API for node pools. A pool is a NodePool resource inside
the cluster, which this function composes as a provider-kubernetes Object
once the cluster is Ready.

CKS doesn't publish a kubeconfig. Its API endpoint accepts the CoreWeave
API access token the provider uses, and this function renders a
kubeconfig from that endpoint and token.

CKS's autoscaler can't scale a pool up from zero nodes when its pods'
devices bind through DRA, as Modelplane's do. An autoscaled pool that
reaches zero stays there.
"""

import base64
import json
from typing import Any, Literal

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.infrastructure.coreweavecluster import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.crossplane.m.kubernetes.providerconfig import v1alpha1 as k8spcv1alpha1
from models.io.crossplane.protection.usage import v1beta1 as usagev1beta1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from models.io.upbound.m.coreweave.cks.cluster import v1beta1 as clusterv1beta1
from models.io.upbound.m.coreweave.clusterproviderconfig import v1beta1 as cwcpcv1beta1
from models.io.upbound.m.coreweave.networking.vpc import v1beta1 as vpcv1beta1
from models.io.upbound.m.coreweave.providerconfig import v1beta1 as cwpcv1beta1

# The system pool runs controllers such as Envoy Gateway and cert-manager.
# compose-inference-cluster passes only inference pools, and this function
# adds the system pool. CKS nodes are bare metal, which makes one CPU node
# enough.
_SYSTEM_POOL_NAME = "system"
_SYSTEM_POOL_INSTANCE_TYPE = "turin-gp"
_SYSTEM_POOL_NODES = 1

# CoreWeave caps cluster and VPC names at 30 characters.
_NAME_MAX_LEN = 30

# The cluster's pod, service and internal load balancer ranges, from its
# own VPC. They're the sizes CoreWeave recommends.
_POD_CIDR_NAME = "pod-cidr"
_POD_CIDR = "10.0.0.0/13"
_SERVICE_CIDR_NAME = "service-cidr"
_SERVICE_CIDR = "10.16.0.0/22"
_INTERNAL_LB_CIDR_NAME = "internal-lb-cidr"
_INTERNAL_LB_CIDR = "10.32.4.0/22"

# Labels written on CKS node pools' nodes. compose-model-deployment reads
# these labels for GPU scheduling.
_LABEL_GPU = "modelplane.ai/gpu"
_LABEL_POOL = "modelplane.ai/pool"

# CoreWeave's managed GPU Operator doesn't run its device plugin on a node
# with this label set to false. That leaves the DRA driver as the only GPU
# allocator. CKS drops only coreweave.com and coreweave.cloud label keys
# from a NodePool, which lets this one through to the nodes.
_LABEL_DEVICE_PLUGIN_KEY = "nvidia.com/gpu.deploy.device-plugin"
_LABEL_DEVICE_PLUGIN_VALUE = "false"

# Only pods that tolerate this taint run on GPU nodes. CKS's own is_gpu
# taint is PreferNoSchedule, which doesn't stop other pods.
_GPU_TAINT_KEY = "nvidia.com/gpu"
_GPU_TAINT_VALUE = "true"
_GPU_TAINT_EFFECT = "NoSchedule"

# The serving stack's dra-driver-cpu runs on nodes with this label. Engine
# pods tolerate the taint, and other pods don't.
_LABEL_CPU = "modelplane.ai/cpu"
_CPU_TAINT_KEY = "modelplane.ai/cpu"
_CPU_TAINT_VALUE = "true"
_CPU_TAINT_EFFECT = "NoSchedule"

# A NodePool is Ready once CKS validates it. Readiness ignores AtTarget,
# which changes with every autoscaler step and node replacement, because
# the InferenceCluster stops composing the serving stack's ProviderConfig
# while this XR isn't Ready.
_NODE_POOL_READY_CEL = (
    "has(object.status) && has(object.status.conditions)"
    " && object.status.conditions.exists(c, c.type == 'Validated' && c.status == 'True')"
)

# Until this XR is first Ready, the system pool is Ready once it has a
# node. CKS validates a pool long before its nodes boot, and the serving
# stack installs as soon as this XR is Ready. Its Helm releases would time
# out with no node to run on, and provider-helm doesn't retry a failed
# install. After that the pool always uses _NODE_POOL_READY_CEL, even if
# the XR later goes un-Ready, because replacing its one node can leave it
# empty for a while.
_SYSTEM_POOL_READY_CEL = "has(object.status) && has(object.status.currentNodes) && object.status.currentNodes >= 1"

# Management policies for an autoscaled pool's two Objects. CKS's
# autoscaler sets the NodePool's targetNodes, and CKS rejects a NodePool
# without it. One Object would either reset targetNodes on every poll or
# remove it by leaving it out. provider-kubernetes server-side applies each
# Object under its own field manager. The first Object creates and deletes the
# NodePool with a starting targetNodes. The second updates every other
# field, and leaving targetNodes out doesn't remove a field it never set.
_ManagementPolicy = Literal["Observe", "Create", "Update", "Delete", "LateInitialize", "*"]
_AUTOSCALED_POOL_MANAGEMENT: list[_ManagementPolicy] = ["Observe", "Create", "Delete"]
_AUTOSCALED_POOL_SETTINGS_MANAGEMENT: list[_ManagementPolicy] = ["Observe", "Update"]

# Suffix of an autoscaled pool's second Object's resource name. Pool names
# can't contain a dot, which rules out a collision with another pool's.
_SETTINGS_SUFFIX = ".settings"

# Secret type written to XR status. compose-inference-cluster reads this to
# wire the kubeconfig into a ClusterProviderConfig.
_SECRET_TYPE_KUBECONFIG = "Kubeconfig"

# Key within the composed kubeconfig Secret.
_SECRET_KEY_KUBECONFIG = "kubeconfig"

# Key within the CoreWeave credentials Secret's JSON that holds the API
# access token, as provider-coreweave reads it.
_CREDENTIALS_KEY_TOKEN = "token"

# CKS's ReadWriteMany StorageClass, and the copy ModelCache uses. CKS's
# class retains a volume after its claim goes, which would leave a deleted
# ModelCache's weights on billed storage.
_CKS_STORAGE_CLASS = "shared-vast"
_CACHE_STORAGE_CLASS = "modelplane-rwx-fs"

# The fields of shared-vast the copy takes. Not its metadata, which marks
# shared-vast as the cluster's default class.
_STORAGE_CLASS_FIELDS = ("provisioner", "parameters", "mountOptions", "allowVolumeExpansion", "volumeBindingMode")


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


def _kubeconfig_secret_name(xr: v1alpha1.CoreWeaveCluster) -> str:
    """Derive the kubeconfig secret name from the XR."""
    return resource.child_name(_name(xr.metadata), "kubeconfig")


def _cluster_name(xr: v1alpha1.CoreWeaveCluster) -> str:
    """The CKS cluster's name, also used for its VPC.

    The name includes the XR's namespace and a hash. CoreWeave rejects a
    second VPC with the same name in a zone, and XRs in different namespaces
    can share a name. The XR's name leads, which makes it what survives
    truncation and what the CoreWeave console shows. A name longer than
    CoreWeave's 30 characters loses characters before its hash, which keeps
    it unique.
    """
    name = resource.child_name(_name(xr.metadata), _namespace(xr.metadata))
    if len(name) <= _NAME_MAX_LEN:
        return name
    prefix, suffix = name.rsplit("-", 1)
    return f"{prefix[: _NAME_MAX_LEN - len(suffix) - 1].rstrip('-')}-{suffix}"


def _kubeconfig(endpoint: str, token: str) -> str:
    """A kubeconfig for the cluster's API endpoint.

    It omits certificate-authority-data. The endpoint's certificate is
    publicly trusted, and CoreWeave's docs warn that setting
    certificate-authority-data can break the connection. It's JSON, which is
    valid YAML, to avoid a YAML dependency.
    """
    return json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [{"name": "cks", "cluster": {"server": f"https://{endpoint}"}}],
            "users": [{"name": "cks", "user": {"token": token}}],
            "contexts": [{"name": "cks", "context": {"cluster": "cks", "user": "cks"}}],
            "current-context": "cks",
        },
        indent=2,
    )


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
        self.xr = v1alpha1.CoreWeaveCluster(**resource.struct_to_dict(req.observed.composite.resource))

    def _cred_kind(self) -> str:
        creds = self.xr.spec.credentials
        return creds.type if creds and creds.type else "ClusterProviderConfig"

    def _cred_name(self) -> str:
        creds = self.xr.spec.credentials
        return creds.name if creds and creds.name else "default"

    def compose(self) -> None:
        self.compose_vpc()
        self.compose_cluster()
        self.compose_vpc_usage()
        self.compose_kubeconfig(self.resolve_token())
        if self._dependents_ready() or self._dependents_observed():
            self.compose_provider_config()
            self.compose_node_pools()
            self.compose_cache_storage_class()
            self.compose_usages()
        else:
            # The VPC and cluster can be Ready before the kubeconfig and node
            # pools exist.
            self.rsp.desired.composite.ready = fnv1.READY_FALSE
        self.write_status()
        self.mark_readiness()

    def _dependents_ready(self) -> bool:
        """Whether the cluster is Ready and its kubeconfig Secret exists."""
        cluster_ready = resource.get_condition(self.req.observed.resources.get("cluster"), "Ready").status == "True"
        return cluster_ready and "kubeconfig" in self.req.observed.resources

    def _dependents_observed(self) -> bool:
        """Whether an earlier reconcile composed the dependents.

        Dropping them when the cluster's Ready condition briefly regresses
        would delete the NodePools and their nodes.
        """
        observed = self.req.observed.resources
        return "provider-config-kubernetes" in observed or any(name.startswith("node-pool-") for name in observed)

    def compose_vpc(self) -> None:
        """Compose the cluster's VPC."""
        resource.update(
            self.rsp.desired.resources["vpc"],
            vpcv1beta1.VPC(
                spec=vpcv1beta1.Spec(
                    providerConfigRef=vpcv1beta1.ProviderConfigRef(
                        kind=self._cred_kind(),
                        name=self._cred_name(),
                    ),
                    forProvider=vpcv1beta1.ForProvider(
                        name=_cluster_name(self.xr),
                        zone=self.xr.spec.zone,
                        vpcPrefixes=[
                            vpcv1beta1.VpcPrefix(name=_POD_CIDR_NAME, value=_POD_CIDR),
                            vpcv1beta1.VpcPrefix(name=_SERVICE_CIDR_NAME, value=_SERVICE_CIDR),
                            vpcv1beta1.VpcPrefix(name=_INTERNAL_LB_CIDR_NAME, value=_INTERNAL_LB_CIDR),
                        ],
                    ),
                ),
            ),
        )

    def compose_cluster(self) -> None:
        """Compose the CKS cluster. Its API server must be public, because only
        the public endpoint accepts CoreWeave API access tokens."""
        resource.update(
            self.rsp.desired.resources["cluster"],
            clusterv1beta1.Cluster(
                spec=clusterv1beta1.Spec(
                    providerConfigRef=clusterv1beta1.ProviderConfigRef(
                        kind=self._cred_kind(),
                        name=self._cred_name(),
                    ),
                    forProvider=clusterv1beta1.ForProvider(
                        name=_cluster_name(self.xr),
                        zone=self.xr.spec.zone,
                        version=f"v{self.xr.spec.kubernetesVersion}",
                        public=True,
                        vpcIdSelector=clusterv1beta1.VpcIdSelector(matchControllerRef=True),
                        podCidrName=_POD_CIDR_NAME,
                        serviceCidrName=_SERVICE_CIDR_NAME,
                        internalLbCidrNames=[_INTERNAL_LB_CIDR_NAME],
                    ),
                ),
            ),
        )

    def compose_vpc_usage(self) -> None:
        """Block deleting the VPC until the cluster is gone. CoreWeave's API
        answers a delete of a VPC a cluster still uses with NotFound, and
        provider-coreweave treats that as success, which leaks the VPC."""
        resource.update(
            self.rsp.desired.resources["usage-vpc-by-cluster"],
            usagev1beta1.Usage(
                metadata=metav1.ObjectMeta(namespace=_namespace(self.xr.metadata)),
                spec=usagev1beta1.Spec(
                    of=usagev1beta1.Of(
                        apiVersion="networking.coreweave.m.upbound.io/v1beta1",
                        kind="VPC",
                        resourceSelector=usagev1beta1.ResourceSelectorModel(matchControllerRef=True),
                    ),
                    by=usagev1beta1.By(
                        apiVersion="cks.coreweave.m.upbound.io/v1beta1",
                        kind="Cluster",
                        resourceSelector=usagev1beta1.ResourceSelector(matchControllerRef=True),
                    ),
                    replayDeletion=True,
                ),
            ),
        )

    def resolve_token(self) -> str | None:
        """The CoreWeave API access token from the CoreWeave ProviderConfig's
        Secret, or None if the function can't get it."""
        cred_kind = self._cred_kind()
        cred_name = self._cred_name()
        response.require_resources(
            self.rsp,
            name="coreweave-provider-config",
            api_version="coreweave.m.upbound.io/v1beta1",
            kind=cred_kind,
            match_name=cred_name,
            namespace=_namespace(self.xr.metadata) if cred_kind == "ProviderConfig" else None,
        )
        d = request.get_required_resource(self.req, "coreweave-provider-config")
        if d is None:
            response.normal(self.rsp, f"Waiting for CoreWeave {cred_kind} {cred_name}")
            return None

        # A namespaced ProviderConfig's secretRef has no namespace. The Secret
        # is in the ProviderConfig's.
        if cred_kind == "ClusterProviderConfig":
            cpc = cwcpcv1beta1.ClusterProviderConfig.model_validate(d)
            source = cpc.spec.credentials.source
            ref = cpc.spec.credentials.secretRef
            secret_namespace = ref.namespace if ref else None
        else:
            pc = cwpcv1beta1.ProviderConfig.model_validate(d)
            source = pc.spec.credentials.source
            ref = pc.spec.credentials.secretRef
            secret_namespace = _namespace(pc.metadata)
        if source != "Secret" or ref is None:
            response.warning(
                self.rsp,
                f"CoreWeave {cred_kind} {cred_name} doesn't source credentials from a Secret; "
                "can't render a kubeconfig without its API access token",
            )
            return None

        response.require_resources(
            self.rsp,
            name="coreweave-credentials",
            api_version="v1",
            kind="Secret",
            match_name=ref.name,
            namespace=secret_namespace,
        )
        secret = request.get_required_resource(self.req, "coreweave-credentials")
        if secret is None:
            response.normal(self.rsp, f"Waiting for CoreWeave credentials Secret {secret_namespace}/{ref.name}")
            return None
        # Invalid base64, UTF-8 and JSON all raise ValueError.
        try:
            creds = json.loads(base64.b64decode(secret.get("data", {}).get(ref.key, "")))
        except ValueError:
            creds = None
        token = creds.get(_CREDENTIALS_KEY_TOKEN) if isinstance(creds, dict) else None
        if not isinstance(token, str) or not token:
            response.warning(
                self.rsp,
                f"CoreWeave credentials Secret {secret_namespace}/{ref.name} key {ref.key} "
                f"isn't a JSON object with a {_CREDENTIALS_KEY_TOKEN} field",
            )
            return None
        return token

    def compose_kubeconfig(self, token: str | None) -> None:
        """Compose the kubeconfig Secret once the cluster reports its endpoint
        and the function has the token. Until then an existing Secret keeps
        its observed data. Dropping it from desired state would delete it
        and cut every consumer off from the cluster."""
        endpoint = self._observed_api_server_endpoint()
        if endpoint and token:
            secret: dict[str, Any] = {"stringData": {_SECRET_KEY_KUBECONFIG: _kubeconfig(endpoint, token)}}
        elif "kubeconfig" in self.req.observed.resources:
            observed = resource.struct_to_dict(self.req.observed.resources["kubeconfig"].resource)
            secret = {"data": observed.get("data", {})}
        else:
            return
        resource.update(
            self.rsp.desired.resources["kubeconfig"],
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": _kubeconfig_secret_name(self.xr),
                    "namespace": _namespace(self.xr.metadata),
                },
                "type": "Opaque",
                **secret,
            },
        )

    def _observed_api_server_endpoint(self) -> str | None:
        """The cluster's API server endpoint, None until CKS reports it."""
        observed = self.req.observed.resources.get("cluster")
        if observed is None:
            return None
        cluster = clusterv1beta1.Cluster.model_validate(resource.struct_to_dict(observed.resource))
        if cluster.status and cluster.status.atProvider and cluster.status.atProvider.apiServerEndpoint:
            return cluster.status.atProvider.apiServerEndpoint
        return None

    def compose_provider_config(self) -> None:
        """Compose the provider-kubernetes ProviderConfig the cluster's
        Objects use."""
        kubeconfig_name = _kubeconfig_secret_name(self.xr)
        resource.update(
            self.rsp.desired.resources["provider-config-kubernetes"],
            k8spcv1alpha1.ProviderConfig(
                metadata=metav1.ObjectMeta(
                    name=kubeconfig_name,
                    namespace=_namespace(self.xr.metadata),
                ),
                spec=k8spcv1alpha1.Spec(
                    credentials=k8spcv1alpha1.Credentials(
                        source="Secret",
                        secretRef=k8spcv1alpha1.SecretRef(
                            namespace=_namespace(self.xr.metadata),
                            name=kubeconfig_name,
                            key=_SECRET_KEY_KUBECONFIG,
                        ),
                    ),
                ),
            ),
        )

    def compose_node_pools(self) -> None:
        """Compose a NodePool Object for the system pool and each
        user-defined pool, and a second Object for each autoscaled pool."""
        ready_cel = _SYSTEM_POOL_READY_CEL
        if resource.get_condition(self.req.observed.composite.resource, "Ready").status == "True":
            ready_cel = _NODE_POOL_READY_CEL
        observed = self.req.observed.resources.get(f"node-pool-{_SYSTEM_POOL_NAME}")
        if observed is not None:
            obj = k8sobjv1alpha1.Object.model_validate(resource.struct_to_dict(observed.resource))
            if obj.spec.readiness is not None and obj.spec.readiness.celQuery == _NODE_POOL_READY_CEL:
                ready_cel = _NODE_POOL_READY_CEL
        self._compose_node_pool(
            key=f"node-pool-{_SYSTEM_POOL_NAME}",
            name=_SYSTEM_POOL_NAME,
            management_policies=None,
            ready_cel=ready_cel,
            spec={
                "instanceType": _SYSTEM_POOL_INSTANCE_TYPE,
                "targetNodes": _SYSTEM_POOL_NODES,
                "autoscaling": False,
                "nodeLabels": {_LABEL_POOL: _SYSTEM_POOL_NAME},
            },
        )
        for pool in self.xr.spec.nodePools:
            spec = self._node_pool_spec(pool)
            autoscaled = pool.maxNodeCount is not None
            self._compose_node_pool(
                key=f"node-pool-{pool.name}",
                name=pool.name,
                management_policies=_AUTOSCALED_POOL_MANAGEMENT if autoscaled else None,
                ready_cel=_NODE_POOL_READY_CEL,
                spec=spec,
            )
            if not autoscaled:
                continue
            self._compose_node_pool(
                key=f"node-pool-{pool.name}{_SETTINGS_SUFFIX}",
                name=pool.name,
                management_policies=_AUTOSCALED_POOL_SETTINGS_MANAGEMENT,
                ready_cel=None,
                spec={k: v for k, v in spec.items() if k != "targetNodes"},
            )

    def _node_pool_spec(self, pool: v1alpha1.NodePool) -> dict[str, Any]:
        """Map an XR node pool to a CKS NodePool's spec."""
        labels = {_LABEL_POOL: pool.name}
        if pool.role == "GPU" and pool.gpu:
            labels[_LABEL_GPU] = pool.gpu.acceleratorType
            labels[_LABEL_DEVICE_PLUGIN_KEY] = _LABEL_DEVICE_PLUGIN_VALUE
            taint = {"key": _GPU_TAINT_KEY, "value": _GPU_TAINT_VALUE, "effect": _GPU_TAINT_EFFECT}
        else:
            labels[_LABEL_CPU] = "true"
            taint = {"key": _CPU_TAINT_KEY, "value": _CPU_TAINT_VALUE, "effect": _CPU_TAINT_EFFECT}

        spec: dict[str, Any] = {
            "instanceType": pool.instanceType,
            "targetNodes": pool.nodeCount,
            "autoscaling": False,
            "nodeLabels": labels,
            "nodeTaints": [taint],
        }
        # An autoscaled pool's nodeCount only sets its starting targetNodes
        # (see _AUTOSCALED_POOL_MANAGEMENT). The pool starts with at least one
        # node, because CKS's autoscaler must have seen one of a pool's nodes
        # before it can scale the pool up. minNodes defaults to the starting
        # count, because a pool whose pods bind devices through DRA can't
        # scale up from zero.
        if pool.maxNodeCount is not None:
            spec["targetNodes"] = pool.nodeCount or 1
            spec["autoscaling"] = True
            spec["minNodes"] = pool.minNodeCount if pool.minNodeCount is not None else spec["targetNodes"]
            spec["maxNodes"] = pool.maxNodeCount
        return spec

    def _compose_node_pool(
        self,
        *,
        key: str,
        name: str,
        management_policies: list[_ManagementPolicy] | None,
        ready_cel: str | None,
        spec: dict[str, Any],
    ) -> None:
        """Compose an Object for a cluster-scoped NodePool."""
        obj = k8sobjv1alpha1.Object(
            metadata=metav1.ObjectMeta(namespace=_namespace(self.xr.metadata)),
            spec=k8sobjv1alpha1.Spec(
                providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                    kind="ProviderConfig",
                    name=_kubeconfig_secret_name(self.xr),
                ),
                forProvider=k8sobjv1alpha1.ForProvider(
                    manifest={
                        "apiVersion": "compute.coreweave.com/v1alpha1",
                        "kind": "NodePool",
                        "metadata": {"name": name},
                        "spec": spec,
                    },
                ),
            ),
        )
        if ready_cel is not None:
            obj.spec.readiness = k8sobjv1alpha1.Readiness(policy="DeriveFromCelQuery", celQuery=ready_cel)
        if management_policies is not None:
            obj.spec.managementPolicies = management_policies
        resource.update(self.rsp.desired.resources[key], obj)

    def compose_cache_storage_class(self) -> None:
        """Observe CKS's shared-vast StorageClass, and compose a copy of it
        whose reclaimPolicy is Delete. It copies the observed class because
        the class's parameters specify the org's storage tenant and the
        zone."""
        resource.update(
            self.rsp.desired.resources["storage-class-cks"],
            k8sobjv1alpha1.Object(
                metadata=metav1.ObjectMeta(namespace=_namespace(self.xr.metadata)),
                spec=k8sobjv1alpha1.Spec(
                    managementPolicies=["Observe"],
                    providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                        kind="ProviderConfig",
                        name=_kubeconfig_secret_name(self.xr),
                    ),
                    forProvider=k8sobjv1alpha1.ForProvider(
                        manifest={
                            "apiVersion": "storage.k8s.io/v1",
                            "kind": "StorageClass",
                            "metadata": {"name": _CKS_STORAGE_CLASS},
                        },
                    ),
                ),
            ),
        )

        observed = self.req.observed.resources.get("storage-class-cks")
        if observed is None:
            return
        obj = k8sobjv1alpha1.Object.model_validate(resource.struct_to_dict(observed.resource))
        if obj.status is None or obj.status.atProvider is None or obj.status.atProvider.manifest is None:
            return
        cks = obj.status.atProvider.manifest

        manifest = {
            "apiVersion": "storage.k8s.io/v1",
            "kind": "StorageClass",
            "metadata": {"name": _CACHE_STORAGE_CLASS},
            **{f: cks[f] for f in _STORAGE_CLASS_FIELDS if f in cks},
            "reclaimPolicy": "Delete",
        }
        resource.update(
            self.rsp.desired.resources["storage-class-rwx-fs"],
            k8sobjv1alpha1.Object(
                metadata=metav1.ObjectMeta(namespace=_namespace(self.xr.metadata)),
                spec=k8sobjv1alpha1.Spec(
                    providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                        kind="ProviderConfig",
                        name=_kubeconfig_secret_name(self.xr),
                    ),
                    forProvider=k8sobjv1alpha1.ForProvider(manifest=manifest),
                ),
            ),
        )

    def compose_usages(self) -> None:
        """Block deleting the kubeconfig Secret and the cluster until the
        provider-kubernetes ProviderConfig is gone. compose-usages blocks
        deleting the ProviderConfig until every Object is gone, so
        provider-kubernetes can still reach the cluster to delete the
        NodePools."""
        by = usagev1beta1.By(
            apiVersion="kubernetes.m.crossplane.io/v1alpha1",
            kind="ProviderConfig",
            resourceRef=usagev1beta1.ResourceRef(name=_kubeconfig_secret_name(self.xr)),
        )
        resource.update(
            self.rsp.desired.resources["usage-kubeconfig-by-provider-config"],
            usagev1beta1.Usage(
                metadata=metav1.ObjectMeta(namespace=_namespace(self.xr.metadata)),
                spec=usagev1beta1.Spec(
                    of=usagev1beta1.Of(
                        apiVersion="v1",
                        kind="Secret",
                        resourceRef=usagev1beta1.ResourceRefModel(name=_kubeconfig_secret_name(self.xr)),
                    ),
                    by=by,
                    replayDeletion=True,
                ),
            ),
        )
        resource.update(
            self.rsp.desired.resources["usage-cluster-by-provider-config"],
            usagev1beta1.Usage(
                metadata=metav1.ObjectMeta(namespace=_namespace(self.xr.metadata)),
                spec=usagev1beta1.Spec(
                    of=usagev1beta1.Of(
                        apiVersion="cks.coreweave.m.upbound.io/v1beta1",
                        kind="Cluster",
                        resourceSelector=usagev1beta1.ResourceSelectorModel(matchControllerRef=True),
                    ),
                    by=by,
                    replayDeletion=True,
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
            cache=v1alpha1.Cache(storageClassName=_CACHE_STORAGE_CLASS),
        )
        resource.update_status(self.rsp.desired.composite, status)

    def mark_readiness(self) -> None:
        """Mark the VPC, the cluster and each pool's first NodePool Object
        ready when its observed Ready condition is True, and everything else
        ready unconditionally. An autoscaled pool's first Object reports the
        pool's readiness."""
        always_ready = (
            "storage-class-cks",
            "storage-class-rwx-fs",
            "usage-vpc-by-cluster",
            "kubeconfig",
            "provider-config-kubernetes",
            "usage-kubeconfig-by-provider-config",
            "usage-cluster-by-provider-config",
        )
        for r in self.rsp.desired.resources:
            if r in always_ready or r.endswith(_SETTINGS_SUFFIX):
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE
                continue
            if resource.get_condition(self.req.observed.resources.get(r), "Ready").status == "True":
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE
