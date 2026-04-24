"""Compose the control plane routing gateway.

This function installs Envoy Gateway on the control plane cluster via Helm,
creates a GatewayClass and Gateway for unified endpoint routing, and
optionally installs MetalLB for kind/bare-metal clusters. The gateway address
is surfaced in status for compose-model-deployment to use.
"""

from crossplane.function import resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .lib import conditions, helm, metadata
from .lib import resource as libresource
from .model.ai.modelplane.inferencegateway import v1alpha1
from .model.io.crossplane.protection.usage import v1beta1 as usagev1beta1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# Condition types and reasons for the InferenceGateway XR.
CONDITION_TYPE_CONTROLLER_READY = "ControllerReady"

CONDITION_REASON_CONTROLLER_HEALTHY = "ControllerHealthy"
CONDITION_REASON_INSTALLING = "Installing"

# ProviderConfig name for in-cluster Helm releases on the control plane.
_PC_NAME = "modelplane-in-cluster"


class Composer:
    def __init__(self, req, rsp):
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.InferenceGateway(**resource.struct_to_dict(req.observed.composite.resource))

    def compose(self):
        self.compose_provider_config()
        self.compose_metallb()
        self.compose_envoy_gateway()
        self.compose_gateway()
        self.write_status()
        self.derive_conditions()

    def compose_provider_config(self):
        """Namespaced ProviderConfig for provider-helm targeting the control
        plane using the pod's own service account (in-cluster identity).
        Namespaced (not ClusterProviderConfig) so the Usage can protect it."""
        resource.update(
            self.rsp.desired.resources["provider-config-helm"],
            {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "ProviderConfig",
                "metadata": {"name": _PC_NAME, "namespace": metadata.NAMESPACE_SYSTEM},
                "spec": {"credentials": {"source": "InjectedIdentity"}},
            },
        )
        self.rsp.desired.resources["provider-config-helm"].ready = fnv1.READY_TRUE

    def compose_metallb(self):
        """Optional MetalLB for kind/bare-metal clusters that don't have a
        cloud load balancer controller to assign Gateway addresses."""
        eg = self.xr.spec.envoyGateway
        if not (eg and eg.loadBalancer == "MetalLB" and eg.metallb and eg.metallb.addressPool):
            return

        metallb_ns = "metallb-system"

        resource.update(
            self.rsp.desired.resources["namespace-metallb"],
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": metallb_ns},
            },
        )
        self.rsp.desired.resources["namespace-metallb"].ready = fnv1.READY_TRUE

        pc_observed = "provider-config-helm" in self.req.observed.resources
        if pc_observed or "metallb" in self.req.observed.resources:
            resource.update(
                self.rsp.desired.resources["metallb"],
                helm.helm_release(
                    chart="metallb",
                    repo="https://metallb.github.io/metallb",
                    version="0.14.9",
                    namespace=metallb_ns,
                    provider_config=_PC_NAME,
                    labels={metadata.LABEL_KEY_RELEASE: "metallb"},
                    metadata_namespace=metadata.NAMESPACE_SYSTEM,
                ),
            )
            self.compose_pc_usage("metallb")

        # Gate the IPAddressPool and L2Advertisement on MetalLB being ready.
        metallb_ready = conditions.has_condition(self.req, "metallb", "Ready")
        if not (metallb_ready or "metallb-pool" in self.req.observed.resources):
            return

        resource.update(
            self.rsp.desired.resources["metallb-pool"],
            {
                "apiVersion": "metallb.io/v1beta1",
                "kind": "IPAddressPool",
                "metadata": {"name": "modelplane", "namespace": metallb_ns},
                "spec": {"addresses": [eg.metallb.addressPool]},
            },
        )
        self.rsp.desired.resources["metallb-pool"].ready = fnv1.READY_TRUE

        resource.update(
            self.rsp.desired.resources["metallb-l2"],
            {
                "apiVersion": "metallb.io/v1beta1",
                "kind": "L2Advertisement",
                "metadata": {"name": "modelplane", "namespace": metallb_ns},
                "spec": {"ipAddressPools": ["modelplane"]},
            },
        )
        self.rsp.desired.resources["metallb-l2"].ready = fnv1.READY_TRUE

    def compose_envoy_gateway(self):
        """Compose Envoy Gateway. Gated on ProviderConfig being observed."""
        pc_observed = "provider-config-helm" in self.req.observed.resources
        if not (pc_observed or "envoy-gateway" in self.req.observed.resources):
            return

        eg = self.xr.spec.envoyGateway
        resource.update(
            self.rsp.desired.resources["envoy-gateway"],
            helm.helm_release(
                chart="gateway-helm",
                repo="oci://docker.io/envoyproxy",
                version=eg.version if eg else "v1.3.0",
                namespace="envoy-gateway-system",
                provider_config=_PC_NAME,
                values={
                    "config": {
                        "envoyGateway": {
                            "extensionApis": {"enableBackend": True},
                        },
                    },
                },
                labels={metadata.LABEL_KEY_RELEASE: "envoy-gateway"},
                metadata_namespace=metadata.NAMESPACE_SYSTEM,
            ),
        )
        self.compose_pc_usage("envoy-gateway")

    def compose_gateway(self):
        """Compose GatewayClass and Gateway. Gated on Envoy Gateway being
        ready."""
        envoy_gw_ready = conditions.has_condition(self.req, "envoy-gateway", "Ready")

        if envoy_gw_ready or "gateway-class" in self.req.observed.resources:
            resource.update(
                self.rsp.desired.resources["gateway-class"],
                {
                    "apiVersion": "gateway.networking.k8s.io/v1",
                    "kind": "GatewayClass",
                    "metadata": {"name": "envoy"},
                    "spec": {
                        "controllerName": "gateway.envoyproxy.io/gatewayclass-controller",
                    },
                },
            )

        if envoy_gw_ready or "gateway" in self.req.observed.resources:
            gw = self.xr.spec.gateway
            # Protobuf Struct delivers all numbers as float.
            port = int(gw.port) if gw and gw.port else 80

            resource.update(
                self.rsp.desired.resources["gateway"],
                {
                    "apiVersion": "gateway.networking.k8s.io/v1",
                    "kind": "Gateway",
                    "metadata": {
                        "name": metadata.GATEWAY_NAME,
                        "namespace": metadata.NAMESPACE_SYSTEM,
                    },
                    "spec": {
                        "gatewayClassName": "envoy",
                        "listeners": [
                            {
                                "name": "http",
                                "protocol": "HTTP",
                                "port": port,
                                "allowedRoutes": {"namespaces": {"from": "All"}},
                            }
                        ],
                    },
                },
            )

    def write_status(self):
        """Surface the gateway's external address. Only the address — no
        gateway-specific fields. This contract works for any routing backend."""
        status = v1alpha1.Status()

        gw_observed = self.req.observed.resources.get("gateway")
        if gw_observed:
            gw_dict = resource.struct_to_dict(gw_observed.resource)
            addresses = gw_dict.get("status", {}).get("addresses", [])
            if addresses:
                status.address = addresses[0].get("value")

        libresource.update_status(self.rsp.desired.composite, status)

    def derive_conditions(self):
        """Derive readiness for all composed resources and set custom
        conditions."""
        # MetalLB readiness.
        eg = self.xr.spec.envoyGateway
        if (
            eg
            and eg.loadBalancer == "MetalLB"
            and eg.metallb
            and eg.metallb.addressPool
            and conditions.has_condition(self.req, "metallb", "Ready")
        ):
            self.rsp.desired.resources["metallb"].ready = fnv1.READY_TRUE

        # Envoy Gateway readiness.
        envoy_ready = conditions.has_condition(self.req, "envoy-gateway", "Ready")
        if envoy_ready:
            self.rsp.desired.resources["envoy-gateway"].ready = fnv1.READY_TRUE
            # Transition: Envoy Gateway just became ready.
            if "gateway" not in self.req.observed.resources:
                response.normal(self.rsp, "Envoy Gateway ready, composing Gateway")

        # ControllerReady condition.
        conditions.set_condition(
            self.rsp,
            CONDITION_TYPE_CONTROLLER_READY,
            envoy_ready,
            CONDITION_REASON_CONTROLLER_HEALTHY if envoy_ready else CONDITION_REASON_INSTALLING,
        )

        # GatewayClass and Gateway use Accepted (not Ready) — on kind the
        # Gateway won't be Programmed (no LoadBalancer), but Accepted means
        # the controller has scheduled it and it's usable.
        if conditions.has_condition(self.req, "gateway-class", "Accepted"):
            self.rsp.desired.resources["gateway-class"].ready = fnv1.READY_TRUE

        if conditions.has_condition(self.req, "gateway", "Accepted"):
            self.rsp.desired.resources["gateway"].ready = fnv1.READY_TRUE

    def compose_pc_usage(self, release_key):
        """Compose a Usage protecting the ProviderConfig from deletion until
        the given Helm release is gone."""
        resource.update(
            self.rsp.desired.resources[f"usage-pc-by-{release_key}"],
            usagev1beta1.Usage(
                metadata=metav1.ObjectMeta(namespace=metadata.NAMESPACE_SYSTEM),
                spec=usagev1beta1.Spec(
                    of=usagev1beta1.Of(
                        apiVersion="helm.m.crossplane.io/v1beta1",
                        kind="ProviderConfig",
                        resourceRef=usagev1beta1.ResourceRefModel(name=_PC_NAME),
                    ),
                    by=usagev1beta1.By(
                        apiVersion="helm.m.crossplane.io/v1beta1",
                        kind="Release",
                        resourceSelector=usagev1beta1.ResourceSelector(
                            matchControllerRef=True,
                            matchLabels={metadata.LABEL_KEY_RELEASE: release_key},
                        ),
                    ),
                    replayDeletion=True,
                ),
            ),
        )
        self.rsp.desired.resources[f"usage-pc-by-{release_key}"].ready = fnv1.READY_TRUE


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose Envoy Gateway, MetalLB, GatewayClass, and Gateway."""
    Composer(req, rsp).compose()
