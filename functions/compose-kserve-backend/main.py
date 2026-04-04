"""Install KServe and supporting components on a remote cluster.

This function composes the full KServe inference backend on a GKE cluster:
cert-manager, Envoy Gateway, LeaderWorkerSet, KServe CRDs and controller,
inference extension CRDs, and a Gateway. Resources are composed as Helm
releases and provider-kubernetes Objects, all targeting the remote cluster
via ProviderConfigs.

Usage resources protect ProviderConfigs from premature deletion during
teardown, ensuring Helm releases can uninstall before losing connectivity.
"""

import json
from pathlib import Path

from crossplane.function import resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .lib import conditions, helm, k8s, metadata, secrets
from .lib import resource as libresource
from .model.ai.modelplane.infrastructure.kservebackend import v1alpha1
from .model.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1
from .model.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from .model.io.crossplane.m.kubernetes.providerconfig import (
    v1alpha1 as k8spcv1alpha1,
)
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

_HERE = Path(__file__).parent

# Gateway API Inference Extension CRDs (InferenceModel, InferencePool).
# Not part of any Helm chart — applied as raw provider-kubernetes Objects.
_INFERENCE_EXTENSION_CRDS = json.loads((_HERE / "inference_extension_crds.json").read_text())

# KServe storage initializer config override. Enables modelcar support
# for model caching, which the default KServe config doesn't include.
_STORAGE_INITIALIZER_CONFIG = json.dumps(
    {
        "image": "kserve/storage-initializer:latest",
        "memoryRequest": "100Mi",
        "memoryLimit": "4Gi",
        "cpuRequest": "100m",
        "cpuLimit": "1",
        "caBundleConfigMapName": "",
        "caBundleVolumeMountPath": "/etc/ssl/custom-certs",
        "enableModelcar": True,
        "cpuModelcar": "10m",
        "memoryModelcar": "15Mi",
        "uidModelcar": 1010,
    }
)

# Kustomize patch applied via Helm's patchesFrom to override the
# inferenceservice-config ConfigMap with the storage initializer config.
_KUSTOMIZE_STORAGE_PATCH = json.dumps(
    {
        "patches": [
            {
                "patch": json.dumps(
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": "inferenceservice-config"},
                        "data": {"storageInitializer": _STORAGE_INITIALIZER_CONFIG},
                    }
                ),
                "target": {
                    "kind": "ConfigMap",
                    "name": "inferenceservice-config",
                },
            }
        ],
    }
)


def _pc_name(xr):
    """Derive the ProviderConfig name from the XR."""
    return f"{xr.metadata.name}-cluster"


class Composer:
    def __init__(self, req, rsp):
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.KServeBackend(**resource.struct_to_dict(req.observed.composite.resource))

    def compose(self):
        if not self.compose_provider_configs():
            return
        self.compose_usages()
        self.compose_cert_manager()
        self.compose_envoy_gateway()
        self.compose_leader_worker_set()
        self.compose_inference_ext_crds()
        self.compose_gateway()
        self.compose_kserve()
        self.compose_storage_patch()
        self.write_status()
        self.mark_readiness()

    def compose_provider_configs(self):
        """Build ProviderConfigs from the XR's secrets. Returns False if the
        kubeconfig secret is missing."""
        xr_secrets = self.xr.spec.secrets or []

        kubeconfig_secret = next((s for s in xr_secrets if s.type == secrets.SECRET_TYPE_KUBECONFIG), None)
        if not kubeconfig_secret:
            response.warning(self.rsp, "spec.secrets must include a Kubeconfig entry")
            return False

        # The kubeconfig provides the cluster endpoint and CA cert. If a
        # cloud-specific credential secret is present, it's layered on as an
        # identity block so the provider authenticates via the cloud's IAM
        # instead of relying on whatever auth is baked into the kubeconfig.
        k8s_pc_spec = k8spcv1alpha1.Spec(
            credentials=k8spcv1alpha1.Credentials(
                source="Secret",
                secretRef=k8spcv1alpha1.SecretRef(
                    name=kubeconfig_secret.name,
                    namespace=self.xr.metadata.namespace,
                    key=kubeconfig_secret.key,
                ),
            ),
        )
        helm_pc_spec = helmpcv1beta1.Spec(
            credentials=helmpcv1beta1.Credentials(
                source="Secret",
                secretRef=helmpcv1beta1.SecretRef(
                    name=kubeconfig_secret.name,
                    namespace=self.xr.metadata.namespace,
                    key=kubeconfig_secret.key,
                ),
            ),
        )

        gcp_secret = next(
            (s for s in xr_secrets if s.type == secrets.SECRET_TYPE_GCP_SA_KEY),
            None,
        )
        if gcp_secret:
            k8s_pc_spec.identity = k8spcv1alpha1.Identity(
                type="GoogleApplicationCredentials",
                source="Secret",
                secretRef=k8spcv1alpha1.SecretRef(
                    name=gcp_secret.name,
                    namespace=self.xr.metadata.namespace,
                    key=gcp_secret.key,
                ),
            )
            helm_pc_spec.identity = helmpcv1beta1.Identity(
                type="GoogleApplicationCredentials",
                source="Secret",
                secretRef=helmpcv1beta1.SecretRef(
                    name=gcp_secret.name,
                    namespace=self.xr.metadata.namespace,
                    key=gcp_secret.key,
                ),
            )

        resource.update(
            self.rsp.desired.resources["provider-config-kubernetes"],
            k8spcv1alpha1.ProviderConfig(
                metadata=metav1.ObjectMeta(name=_pc_name(self.xr)),
                spec=k8s_pc_spec,
            ),
        )

        resource.update(
            self.rsp.desired.resources["provider-config-helm"],
            helmpcv1beta1.ProviderConfig(
                metadata=metav1.ObjectMeta(name=_pc_name(self.xr)),
                spec=helm_pc_spec,
            ),
        )

        return True

    def compose_usages(self):
        """Compose Usages for deletion ordering.

        Two concerns: ProviderConfigs must outlive the resources that reference
        them, and the Envoy Gateway controller must outlive the Gateway and
        GatewayClass resources it manages (they have finalizers the controller
        must process).

        The deletion chain is:
          Gateway Object → GatewayClass Object → envoy-gateway Release
          All Releases → helm ProviderConfig
          All Objects → k8s ProviderConfig
        """
        pc = _pc_name(self.xr)

        # Helm ProviderConfig protected by all Releases.
        resource.update(
            self.rsp.desired.resources["usage-helm-pc"],
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "spec": {
                    "of": {
                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                        "kind": "ProviderConfig",
                        "resourceRef": {"name": pc},
                    },
                    "by": {
                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                        "kind": "Release",
                        "resourceSelector": {"matchControllerRef": True},
                    },
                    "replayDeletion": True,
                },
            },
        )
        self.rsp.desired.resources["usage-helm-pc"].ready = fnv1.READY_TRUE

        # K8s ProviderConfig protected by all Objects.
        resource.update(
            self.rsp.desired.resources["usage-k8s-pc"],
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "spec": {
                    "of": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "ProviderConfig",
                        "resourceRef": {"name": pc},
                    },
                    "by": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "resourceSelector": {"matchControllerRef": True},
                    },
                    "replayDeletion": True,
                },
            },
        )
        self.rsp.desired.resources["usage-k8s-pc"].ready = fnv1.READY_TRUE

        # GatewayClass Object protected by Gateway Object. The GatewayClass
        # has a gateway-exists-finalizer that the EG controller won't remove
        # while Gateways reference it.
        resource.update(
            self.rsp.desired.resources["usage-gateway-class-by-gateway"],
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "spec": {
                    "of": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {metadata.LABEL_KEY_RESOURCE: "gateway-class"},
                        },
                    },
                    "by": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {metadata.LABEL_KEY_RESOURCE: "gateway"},
                        },
                    },
                    "replayDeletion": True,
                },
            },
        )
        self.rsp.desired.resources["usage-gateway-class-by-gateway"].ready = fnv1.READY_TRUE

        # Envoy Gateway Release protected by GatewayClass Object. The EG
        # controller must be running to process the GatewayClass's
        # gateway-exists-finalizer during deletion.
        resource.update(
            self.rsp.desired.resources["usage-envoy-gw-by-gateway-class"],
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "spec": {
                    "of": {
                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                        "kind": "Release",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {metadata.LABEL_KEY_RESOURCE: "envoy-gateway"},
                        },
                    },
                    "by": {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {metadata.LABEL_KEY_RESOURCE: "gateway-class"},
                        },
                    },
                    "replayDeletion": True,
                },
            },
        )
        self.rsp.desired.resources["usage-envoy-gw-by-gateway-class"].ready = fnv1.READY_TRUE

    def compose_cert_manager(self):
        """Compose cert-manager. Gated on ProviderConfigs being observed."""
        pc_observed = self.provider_configs_observed()
        if not (pc_observed or "cert-manager" in self.req.observed.resources):
            return

        v = self.xr.spec.versions or v1alpha1.Versions()
        resource.update(
            self.rsp.desired.resources["cert-manager"],
            helm.helm_release(
                chart="cert-manager",
                repo="https://charts.jetstack.io",
                version=v.certManager,
                namespace="cert-manager",
                provider_config=_pc_name(self.xr),
                values={"crds": {"enabled": True, "keep": False}},
            ),
        )

    def compose_envoy_gateway(self):
        """Compose Envoy Gateway. Gated on ProviderConfigs being observed."""
        pc_observed = self.provider_configs_observed()
        if not (pc_observed or "envoy-gateway" in self.req.observed.resources):
            return

        v = self.xr.spec.versions or v1alpha1.Versions()
        resource.update(
            self.rsp.desired.resources["envoy-gateway"],
            helm.helm_release(
                chart="gateway-helm",
                repo="oci://docker.io/envoyproxy",
                version=v.envoyGateway,
                namespace="envoy-gateway-system",
                provider_config=_pc_name(self.xr),
                labels={metadata.LABEL_KEY_RESOURCE: "envoy-gateway"},
                values={
                    "config": {
                        "envoyGateway": {
                            "extensionApis": {"enableBackend": True},
                        },
                    },
                },
            ),
        )

    def compose_leader_worker_set(self):
        """Compose LeaderWorkerSet. Gated on ProviderConfigs being observed."""
        pc_observed = self.provider_configs_observed()
        if not (pc_observed or "leader-worker-set" in self.req.observed.resources):
            return

        v = self.xr.spec.versions or v1alpha1.Versions()
        resource.update(
            self.rsp.desired.resources["leader-worker-set"],
            helm.helm_release(
                chart="lws",
                repo="oci://registry.k8s.io/lws/charts",
                version=v.leaderWorkerSet,
                namespace="lws-system",
                provider_config=_pc_name(self.xr),
            ),
        )

    def compose_inference_ext_crds(self):
        """Compose inference extension CRDs. Gated on ProviderConfigs being
        observed."""
        pc_observed = self.provider_configs_observed()

        for crd in _INFERENCE_EXTENSION_CRDS:
            crd_name = crd["metadata"]["name"]
            short = crd_name.split(".")[0]
            key = f"inference-ext-crd-{short}"

            if not (pc_observed or key in self.req.observed.resources):
                continue

            resource.update(
                self.rsp.desired.resources[key],
                k8s.k8s_object(_pc_name(self.xr), crd),
            )

    def compose_gateway(self):
        """Compose the GatewayClass and Gateway on the remote cluster. Gated on
        ProviderConfigs being observed."""
        pc_observed = self.provider_configs_observed()
        pc = _pc_name(self.xr)

        gw = self.xr.spec.gateway or v1alpha1.Gateway()

        if gw.listeners:
            listeners = [{"name": ln.name, "protocol": ln.protocol, "port": ln.port} for ln in gw.listeners]
        else:
            listeners = [{"name": "http", "protocol": "HTTP", "port": 80}]

        if pc_observed or "gateway-class" in self.req.observed.resources:
            resource.update(
                self.rsp.desired.resources["gateway-class"],
                k8s.k8s_object(
                    pc,
                    {
                        "apiVersion": "gateway.networking.k8s.io/v1",
                        "kind": "GatewayClass",
                        "metadata": {"name": gw.className},
                        "spec": {
                            "controllerName": "gateway.envoyproxy.io/gatewayclass-controller",
                        },
                    },
                    metadata=metav1.ObjectMeta(labels={metadata.LABEL_KEY_RESOURCE: "gateway-class"}),
                ),
            )

        if pc_observed or "gateway" in self.req.observed.resources:
            resource.update(
                self.rsp.desired.resources["gateway"],
                k8s.k8s_object(
                    pc,
                    {
                        "apiVersion": "gateway.networking.k8s.io/v1",
                        "kind": "Gateway",
                        "metadata": {
                            "name": "kserve-ingress-gateway",
                            "namespace": "kserve",
                        },
                        "spec": {
                            "gatewayClassName": gw.className,
                            "listeners": [
                                {
                                    **ln,
                                    "allowedRoutes": {"namespaces": {"from": "All"}},
                                }
                                for ln in listeners
                            ],
                        },
                    },
                    metadata=metav1.ObjectMeta(labels={metadata.LABEL_KEY_RESOURCE: "gateway"}),
                ),
            )

    def compose_kserve(self):
        """Compose KServe CRDs and controller. Gated on ProviderConfigs being
        observed AND cert-manager being ready. The kserve chart creates
        Certificate and Issuer resources, and the kserve controller registers a
        validating webhook that Helm calls during install. Both fail if
        cert-manager isn't fully up."""
        pc_observed = self.provider_configs_observed()
        cert_manager_ready = conditions.has_condition(self.req, "cert-manager", "Ready")
        gate = pc_observed and cert_manager_ready

        v = self.xr.spec.versions or v1alpha1.Versions()
        pc = _pc_name(self.xr)

        if gate or "kserve-crds" in self.req.observed.resources:
            resource.update(
                self.rsp.desired.resources["kserve-crds"],
                helm.helm_release(
                    chart="kserve-llmisvc-crd",
                    repo="oci://ghcr.io/kserve/charts",
                    version=v.kserve,
                    namespace="kserve",
                    provider_config=pc,
                ),
            )

        if gate or "kserve-controller" in self.req.observed.resources:
            patch_cm_name = f"{self.xr.metadata.name}-storage-patch"
            kserve_release = helm.helm_release(
                chart="kserve-llmisvc-resources",
                repo="oci://ghcr.io/kserve/charts",
                version=v.kserve,
                namespace="kserve",
                provider_config=pc,
            )
            kserve_release.spec.forProvider.patchesFrom = [
                helmv1beta1.PatchesFromItem(
                    configMapKeyRef=helmv1beta1.ConfigMapKeyRef(
                        name=patch_cm_name,
                        namespace=self.xr.metadata.namespace,
                        key="patches",
                    ),
                ),
            ]
            resource.update(self.rsp.desired.resources["kserve-controller"], kserve_release)

        # Transition: cert-manager is ready, KServe not yet composed.
        if not cert_manager_ready:
            return
        if conditions.has_condition(self.req, "kserve-controller", "Ready"):
            return
        if "kserve-controller" in self.req.observed.resources:
            return
        response.normal(self.rsp, "cert-manager ready, composing KServe")

    def compose_storage_patch(self):
        """Compose the storage initializer config patch ConfigMap. This is a
        local resource (not remote), so it's not gated."""
        resource.update(
            self.rsp.desired.resources["kserve-storage-patch"],
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": f"{self.xr.metadata.name}-storage-patch",
                    "namespace": self.xr.metadata.namespace,
                },
                "data": {
                    "patches": _KUSTOMIZE_STORAGE_PATCH,
                },
            },
        )

    def write_status(self):
        """Extract the gateway address from the observed Gateway Object and
        write it to the XR's status."""
        gateway_address = None
        gateway_observed = self.req.observed.resources.get("gateway")
        if gateway_observed:
            gw_dict = resource.struct_to_dict(gateway_observed.resource)
            addresses = (
                gw_dict.get("status", {})
                .get("atProvider", {})
                .get("manifest", {})
                .get("status", {})
                .get("addresses", [])
            )
            if addresses:
                gateway_address = addresses[0].get("value")

        status = v1alpha1.Status()
        if gateway_address:
            status.gateway = v1alpha1.GatewayModel(address=gateway_address)
        libresource.update_status(self.rsp.desired.composite, status)

    def mark_readiness(self):
        """Mark composed resources as ready. Resources that don't need external
        readiness tracking are always marked ready. Others are marked ready when
        their observed condition is True."""
        # These resources don't have meaningful readiness signals — mark them
        # ready unconditionally so they don't block the XR.
        always_ready = [
            "provider-config-kubernetes",
            "provider-config-helm",
            "kserve-storage-patch",
        ]
        for r in always_ready:
            if r in self.rsp.desired.resources:
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE

        condition_ready = [
            "cert-manager",
            "envoy-gateway",
            "leader-worker-set",
            "kserve-crds",
            "kserve-controller",
            "inference-ext-crd-inferencemodels",
            "inference-ext-crd-inferencepools",
            "gateway-class",
            "gateway",
        ]
        for r in condition_ready:
            if r in self.rsp.desired.resources and conditions.has_condition(self.req, r, "Ready"):
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE

    def provider_configs_observed(self):
        """Check if both ProviderConfigs have been persisted by Crossplane from
        a previous reconcile. Resources targeting the remote cluster are gated
        on this to avoid transient 'ProviderConfig not found' errors on first
        creation."""
        return (
            "provider-config-helm" in self.req.observed.resources
            and "provider-config-kubernetes" in self.req.observed.resources
        )


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose the KServe inference backend on a remote cluster."""
    Composer(req, rsp).compose()
