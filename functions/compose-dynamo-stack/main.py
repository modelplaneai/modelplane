"""Install Dynamo and supporting components on a remote cluster.

This function composes the NVIDIA Dynamo inference framework on a Kubernetes
cluster: cert-manager, Envoy Gateway, the dynamo-platform Helm chart (operator,
etcd, NATS), and a Gateway for inference traffic routing.

The structure mirrors compose-kserve-stack. Both stack functions install the
same gateway infrastructure (cert-manager, Envoy Gateway, Gateway/GatewayClass)
and differ only in the inference backend they install.
"""

from pathlib import Path

from crossplane.function import resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .lib import conditions, helm, k8s, metadata, secrets
from .lib import resource as libresource
from .model.ai.modelplane.infrastructure.dynamostack import v1alpha1
from .model.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1
from .model.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from .model.io.crossplane.m.kubernetes.providerconfig import (
    v1alpha1 as k8spcv1alpha1,
)
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


def _pc_name(xr):
    """Derive the ProviderConfig name from the XR."""
    return f"{xr.metadata.name}-cluster"


class Composer:
    def __init__(self, req, rsp):
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.DynamoStack(**resource.struct_to_dict(req.observed.composite.resource))

    def compose(self):
        if not self.compose_provider_configs():
            return
        self.compose_usages()
        self.compose_cert_manager()
        self.compose_envoy_gateway()
        self.compose_dynamo_platform()
        self.compose_gateway()
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

    def compose_dynamo_platform(self):
        """Compose the dynamo-platform Helm chart. Gated on ProviderConfigs
        being observed AND cert-manager being ready. The Dynamo operator may
        create Certificate resources that require cert-manager."""
        pc_observed = self.provider_configs_observed()
        cert_manager_ready = conditions.has_condition(self.req, "cert-manager", "Ready")
        gate = pc_observed and cert_manager_ready

        if not (gate or "dynamo-platform" in self.req.observed.resources):
            return

        v = self.xr.spec.versions or v1alpha1.Versions()
        resource.update(
            self.rsp.desired.resources["dynamo-platform"],
            helm.helm_release(
                chart="dynamo-platform",
                # NGC doesn't publish a standard Helm repo index. The chart is
                # downloaded directly by URL: the Helm provider resolves
                # <repo>/charts/<chart>-<version>.tgz from this base URL.
                repo="https://helm.ngc.nvidia.com/nvidia/ai-dynamo",
                version=v.dynamo,
                namespace="dynamo-system",
                provider_config=_pc_name(self.xr),
            ),
        )

        # Transition: cert-manager is ready, Dynamo not yet composed.
        if not cert_manager_ready:
            return
        if conditions.has_condition(self.req, "dynamo-platform", "Ready"):
            return
        if "dynamo-platform" in self.req.observed.resources:
            return
        response.normal(self.rsp, "cert-manager ready, composing Dynamo platform")

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
                            "name": "dynamo-ingress-gateway",
                            "namespace": "dynamo-system",
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
        """Mark composed resources as ready."""
        always_ready = [
            "provider-config-kubernetes",
            "provider-config-helm",
        ]
        for r in always_ready:
            if r in self.rsp.desired.resources:
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE

        condition_ready = [
            "cert-manager",
            "envoy-gateway",
            "dynamo-platform",
            "gateway-class",
            "gateway",
        ]
        for r in condition_ready:
            if r in self.rsp.desired.resources and conditions.has_condition(self.req, r, "Ready"):
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE

    def provider_configs_observed(self):
        """Check if both ProviderConfigs have been persisted by Crossplane."""
        return (
            "provider-config-helm" in self.req.observed.resources
            and "provider-config-kubernetes" in self.req.observed.resources
        )


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose the Dynamo inference stack on a remote cluster."""
    Composer(req, rsp).compose()
