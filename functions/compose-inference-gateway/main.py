"""Compose the control plane routing gateway.

This function installs Envoy Gateway on the control plane cluster via Helm,
creates a GatewayClass and Gateway for unified endpoint routing, and
optionally installs MetalLB for kind/bare-metal clusters. The gateway address
is surfaced in status for compose-model-deployment to use.
"""

from crossplane.function import resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .lib import conditions
from .lib import helm
from .lib import metadata
from .lib import resource as libresource
from .model.ai.modelplane.inferencegateway import v1alpha1

# Condition types and reasons for the InferenceGateway XR.
CONDITION_TYPE_CONTROLLER_READY = "ControllerReady"

CONDITION_REASON_CONTROLLER_HEALTHY = "ControllerHealthy"
CONDITION_REASON_INSTALLING = "Installing"


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    """Compose Envoy Gateway, MetalLB, GatewayClass, and Gateway."""
    xr = v1alpha1.InferenceGateway(
        **resource.struct_to_dict(req.observed.composite.resource)
    )

    eg = xr.spec.envoyGateway
    eg_version = eg.version if eg else "v1.3.0"
    gw = xr.spec.gateway
    # Protobuf Struct delivers all numbers as float.
    gw_port = int(gw.port) if gw and gw.port else 80

    pc_name = "modelplane-in-cluster"

    # Namespaced ProviderConfig for provider-helm targeting the control plane
    # using the pod's own service account (in-cluster identity). Namespaced
    # (not ClusterProviderConfig) so the Usage can protect it — cross-scope
    # Usages aren't supported.
    resource.update(
        rsp.desired.resources["provider-config-helm"],
        {
            "apiVersion": "helm.m.crossplane.io/v1beta1",
            "kind": "ProviderConfig",
            "metadata": {"name": pc_name, "namespace": metadata.NAMESPACE_SYSTEM},
            "spec": {"credentials": {"source": "InjectedIdentity"}},
        },
    )
    rsp.desired.resources["provider-config-helm"].ready = fnv1.READY_TRUE

    def _pc_usage(release_key: str) -> None:
        """Compose a Usage protecting the ProviderConfig from deletion until
        the given Helm release is gone. One Usage per release is needed
        because matchControllerRef only matches a single resource."""
        resource.update(
            rsp.desired.resources[f"usage-pc-by-{release_key}"],
            {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "metadata": {"namespace": metadata.NAMESPACE_SYSTEM},
                "spec": {
                    "of": {
                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                        "kind": "ProviderConfig",
                        "resourceRef": {"name": pc_name},
                    },
                    "by": {
                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                        "kind": "Release",
                        "resourceSelector": {
                            "matchControllerRef": True,
                            "matchLabels": {metadata.LABEL_KEY_RELEASE: release_key},
                        },
                    },
                    "replayDeletion": True,
                },
            },
        )
        rsp.desired.resources[f"usage-pc-by-{release_key}"].ready = fnv1.READY_TRUE

    # Optional MetalLB for kind/bare-metal clusters that don't have a cloud
    # load balancer controller to assign Gateway addresses.
    lb = eg.loadBalancer if eg else None
    address_pool = eg.metallb.addressPool if eg and eg.metallb else ""

    if lb == "MetalLB" and address_pool:
        metallb_ns = "metallb-system"

        resource.update(
            rsp.desired.resources["namespace-metallb"],
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": metallb_ns},
            },
        )
        rsp.desired.resources["namespace-metallb"].ready = fnv1.READY_TRUE

        metallb_exists = "metallb" in req.observed.resources
        pc_observed = "provider-config-helm" in req.observed.resources
        if pc_observed or metallb_exists:
            resource.update(
                rsp.desired.resources["metallb"],
                helm.helm_release(
                    chart="metallb",
                    repo="https://metallb.github.io/metallb",
                    version="0.14.9",
                    namespace=metallb_ns,
                    provider_config=pc_name,
                    labels={metadata.LABEL_KEY_RELEASE: "metallb"},
                    metadata_namespace=metadata.NAMESPACE_SYSTEM,
                ),
            )
            _pc_usage("metallb")

        # Gate the IPAddressPool and L2Advertisement on MetalLB being ready.
        metallb_ready = conditions.has_condition(req, "metallb", "Ready")
        pool_exists = "metallb-pool" in req.observed.resources
        if metallb_ready or pool_exists:
            resource.update(
                rsp.desired.resources["metallb-pool"],
                {
                    "apiVersion": "metallb.io/v1beta1",
                    "kind": "IPAddressPool",
                    "metadata": {"name": "modelplane", "namespace": metallb_ns},
                    "spec": {"addresses": [address_pool]},
                },
            )
            rsp.desired.resources["metallb-pool"].ready = fnv1.READY_TRUE

            resource.update(
                rsp.desired.resources["metallb-l2"],
                {
                    "apiVersion": "metallb.io/v1beta1",
                    "kind": "L2Advertisement",
                    "metadata": {"name": "modelplane", "namespace": metallb_ns},
                    "spec": {"ipAddressPools": ["modelplane"]},
                },
            )
            rsp.desired.resources["metallb-l2"].ready = fnv1.READY_TRUE

    # Gate Envoy Gateway on the ProviderConfig being observed.
    pc_observed = "provider-config-helm" in req.observed.resources
    envoy_gw_exists = "envoy-gateway" in req.observed.resources
    if pc_observed or envoy_gw_exists:
        resource.update(
            rsp.desired.resources["envoy-gateway"],
            helm.helm_release(
                chart="gateway-helm",
                repo="oci://docker.io/envoyproxy",
                version=eg_version,
                namespace="envoy-gateway-system",
                provider_config=pc_name,
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
        _pc_usage("envoy-gateway")

    # Gate GatewayClass and Gateway on Envoy Gateway being ready.
    envoy_gw_ready = conditions.has_condition(req, "envoy-gateway", "Ready")
    gw_class_exists = "gateway-class" in req.observed.resources
    gw_exists = "gateway" in req.observed.resources

    if envoy_gw_ready or gw_class_exists:
        resource.update(
            rsp.desired.resources["gateway-class"],
            {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "GatewayClass",
                "metadata": {"name": "envoy"},
                "spec": {
                    "controllerName": "gateway.envoyproxy.io/gatewayclass-controller",
                },
            },
        )

    if envoy_gw_ready or gw_exists:
        resource.update(
            rsp.desired.resources["gateway"],
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
                            "port": gw_port,
                            "allowedRoutes": {"namespaces": {"from": "All"}},
                        }
                    ],
                },
            },
        )

    # Read the observed Gateway's status to extract the external address.
    gateway_address = None
    gw_observed = req.observed.resources.get("gateway")
    if gw_observed:
        gw_dict = resource.struct_to_dict(gw_observed.resource)
        addresses = gw_dict.get("status", {}).get("addresses", [])
        if addresses:
            gateway_address = addresses[0].get("value")

    # Write status. Only the address — no gateway-specific fields. This
    # contract works for any routing backend (Envoy Gateway, LiteLLM, etc.).
    status = v1alpha1.Status()
    if gateway_address:
        status.address = gateway_address
    libresource.update_status(rsp.desired.composite, status)

    # Track per-resource readiness. Crossplane derives the XR's Ready
    # condition automatically from composed resource readiness.
    # GatewayClass and Gateway use Accepted (not Ready) — on kind the
    # Gateway won't be Programmed (no LoadBalancer), but Accepted means
    # the controller has scheduled it and it's usable.
    if lb == "MetalLB" and address_pool:
        if conditions.has_condition(req, "metallb", "Ready"):
            rsp.desired.resources["metallb"].ready = fnv1.READY_TRUE

    envoy_ready = conditions.has_condition(req, "envoy-gateway", "Ready")
    if envoy_ready:
        rsp.desired.resources["envoy-gateway"].ready = fnv1.READY_TRUE
        # Transition: Envoy Gateway just became ready (Gateway not yet observed).
        if not gw_exists:
            response.normal(rsp, "Envoy Gateway ready, composing Gateway")

    # ControllerReady: the gateway controller is running.
    conditions.set_condition(
        rsp,
        CONDITION_TYPE_CONTROLLER_READY,
        envoy_ready,
        CONDITION_REASON_CONTROLLER_HEALTHY
        if envoy_ready
        else CONDITION_REASON_INSTALLING,
    )

    if conditions.has_condition(req, "gateway-class", "Accepted"):
        rsp.desired.resources["gateway-class"].ready = fnv1.READY_TRUE

    if conditions.has_condition(req, "gateway", "Accepted"):
        rsp.desired.resources["gateway"].ready = fnv1.READY_TRUE
