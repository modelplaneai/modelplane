"""Compose the control plane routing gateway.

This function installs Traefik Proxy on the control plane cluster via Helm,
creates a GatewayClass and Gateway for unified endpoint routing, and
optionally installs MetalLB for kind/bare-metal clusters. The gateway address
is surfaced in status for compose-model-deployment to use.

Traefik is used (instead of e.g. Envoy Gateway) because it supports
per-backendRef URLRewrite filters. This is a Gateway API Extended feature
that allows each backend in a weighted traffic split to have its own path
rewrite, which Modelplane needs to route across endpoints with different
path conventions (e.g. a self-hosted model at /v1/ alongside Groq at
/openai/v1/). Envoy Gateway does not support this — see
envoyproxy/gateway#7099.
"""

import grpc
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.inferencegateway import v1alpha1
from models.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from models.io.crossplane.protection.usage import v1beta1 as usagev1beta1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# Condition types and reasons for the InferenceGateway XR.
CONDITION_TYPE_CONTROLLER_READY = "ControllerReady"

CONDITION_REASON_CONTROLLER_HEALTHY = "ControllerHealthy"
CONDITION_REASON_INSTALLING = "Installing"

# ProviderConfig name for in-cluster Helm releases on the control plane.
_PC_NAME = "modelplane-in-cluster"

# The modelplane-system namespace. Used for Helm release metadata,
# Usage resources, and the Gateway resource.
_NAMESPACE_SYSTEM = "modelplane-system"

# The control plane gateway name. Used as the Gateway resource name
# and the MetalLB IP pool / L2Advertisement name.
_GATEWAY_NAME = "modelplane"

# Label key for Helm releases, used in Usage selectors to protect
# ProviderConfigs from premature deletion.
_LABEL_RELEASE = "modelplane.ai/release"

# Traefik Helm chart coordinates.
_TRAEFIK_CHART = "traefik"
_TRAEFIK_REPO = "https://traefik.github.io/charts"
_TRAEFIK_NAMESPACE = "traefik-system"
_TRAEFIK_SERVICE_NAME = "traefik"

# Traefik's GatewayClass controllerName and the GatewayClass name we
# compose for it.
_TRAEFIK_GATEWAY_CLASS = "traefik"
_TRAEFIK_CONTROLLER_NAME = "traefik.io/gateway-controller"

# Traefik's default "web" entryPoint listens on this port internally.
# The Gateway listener port must match the entryPoint's internal port,
# not the Service's exposed port. The Helm chart exposes the same
# entryPoint at port 80 on the Service by default.
_TRAEFIK_WEB_ENTRYPOINT_PORT = 8000


def _helm_release(
    chart: str,
    repo: str,
    version: str,
    namespace: str,
    provider_config: str,
    values: dict | None = None,
    labels: dict | None = None,
    metadata_namespace: str | None = None,
) -> helmv1beta1.Release:
    """Build a Helm Release targeting a remote (or local) cluster.

    Args:
        chart: The Helm chart name.
        repo: The chart repository URL.
        version: The chart version.
        namespace: The namespace to install the chart into on the target cluster.
        provider_config: Name of the ProviderConfig to use.
        values: Optional Helm values dict.
        labels: Optional labels for the Release metadata.
        metadata_namespace: Optional namespace for the Release resource itself.
            Set this explicitly when composing from a cluster-scoped XR, since
            cluster-scoped XRs don't auto-populate namespace on composed
            namespaced resources.
    """
    md = None
    if labels or metadata_namespace:
        md = metav1.ObjectMeta(namespace=metadata_namespace, labels=labels)

    release = helmv1beta1.Release(
        metadata=md,
        spec=helmv1beta1.Spec(
            providerConfigRef=helmv1beta1.ProviderConfigRef(
                kind="ProviderConfig",
                name=provider_config,
            ),
            forProvider=helmv1beta1.ForProvider(
                chart=helmv1beta1.Chart(
                    name=chart,
                    repository=repo,
                    version=version,
                ),
                namespace=namespace,
            ),
        ),
    )
    if values:
        release.spec.forProvider.values = values
    return release


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
        self.xr = v1alpha1.InferenceGateway(**resource.struct_to_dict(req.observed.composite.resource))

    def compose(self):
        self.compose_provider_config()
        self.compose_metallb()
        self.compose_traefik()
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
                "metadata": {"name": _PC_NAME, "namespace": _NAMESPACE_SYSTEM},
                "spec": {"credentials": {"source": "InjectedIdentity"}},
            },
        )
        self.rsp.desired.resources["provider-config-helm"].ready = fnv1.READY_TRUE

    def compose_metallb(self):
        """Optional MetalLB for kind/bare-metal clusters that don't have a
        cloud load balancer controller to assign Gateway addresses."""
        t = self.xr.spec.traefik
        if not (t and t.loadBalancer == "MetalLB" and t.metallb and t.metallb.addressPool):
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
                _helm_release(
                    chart="metallb",
                    repo="https://metallb.github.io/metallb",
                    version="0.14.9",
                    namespace=metallb_ns,
                    provider_config=_PC_NAME,
                    labels={_LABEL_RELEASE: "metallb"},
                    metadata_namespace=_NAMESPACE_SYSTEM,
                ),
            )
            self.compose_pc_usage("metallb")

        # Gate the IPAddressPool and L2Advertisement on MetalLB being ready.
        metallb_ready = resource.get_condition(self.req.observed.resources.get("metallb"), "Ready").status == "True"
        if not (metallb_ready or "metallb-pool" in self.req.observed.resources):
            return

        resource.update(
            self.rsp.desired.resources["metallb-pool"],
            {
                "apiVersion": "metallb.io/v1beta1",
                "kind": "IPAddressPool",
                "metadata": {"name": _GATEWAY_NAME, "namespace": metallb_ns},
                "spec": {"addresses": [t.metallb.addressPool]},
            },
        )
        self.rsp.desired.resources["metallb-pool"].ready = fnv1.READY_TRUE

        resource.update(
            self.rsp.desired.resources["metallb-l2"],
            {
                "apiVersion": "metallb.io/v1beta1",
                "kind": "L2Advertisement",
                "metadata": {"name": _GATEWAY_NAME, "namespace": metallb_ns},
                "spec": {"ipAddressPools": [_GATEWAY_NAME]},
            },
        )
        self.rsp.desired.resources["metallb-l2"].ready = fnv1.READY_TRUE

    def compose_traefik(self):
        """Compose Traefik Proxy. Gated on ProviderConfig being observed."""
        pc_observed = "provider-config-helm" in self.req.observed.resources
        if not (pc_observed or "traefik" in self.req.observed.resources):
            return

        resource.update(
            self.rsp.desired.resources["traefik"],
            _helm_release(
                chart=_TRAEFIK_CHART,
                repo=_TRAEFIK_REPO,
                version=self.xr.spec.traefik.version,
                namespace=_TRAEFIK_NAMESPACE,
                provider_config=_PC_NAME,
                values={
                    "providers": {
                        "kubernetesGateway": {
                            "enabled": True,
                            "statusAddress": {
                                "service": {
                                    "namespace": _TRAEFIK_NAMESPACE,
                                    "name": _TRAEFIK_SERVICE_NAME,
                                },
                            },
                        },
                        "kubernetesIngress": {"enabled": False},
                    },
                    # Give the Traefik Service a predictable name so
                    # statusAddress.service can reference it. The default
                    # name includes Crossplane's generated release name.
                    "service": {"nameOverride": _TRAEFIK_SERVICE_NAME},
                    # Disable Traefik's built-in GatewayClass and Gateway
                    # creation. Crossplane composes them so they appear in
                    # observed resources and we can read status.addresses.
                    "gateway": {"enabled": False},
                },
                labels={_LABEL_RELEASE: "traefik"},
                metadata_namespace=_NAMESPACE_SYSTEM,
            ),
        )
        self.compose_pc_usage("traefik")

    def compose_gateway(self):
        """Compose GatewayClass and Gateway. Gated on Traefik being ready."""
        traefik_ready = resource.get_condition(self.req.observed.resources.get("traefik"), "Ready").status == "True"

        if traefik_ready or "gateway-class" in self.req.observed.resources:
            resource.update(
                self.rsp.desired.resources["gateway-class"],
                {
                    "apiVersion": "gateway.networking.k8s.io/v1",
                    "kind": "GatewayClass",
                    "metadata": {"name": _TRAEFIK_GATEWAY_CLASS},
                    "spec": {
                        "controllerName": _TRAEFIK_CONTROLLER_NAME,
                    },
                },
            )

        if traefik_ready or "gateway" in self.req.observed.resources:
            # The Gateway listener port must match Traefik's "web"
            # entryPoint internal port, not the Service's exposed port.
            resource.update(
                self.rsp.desired.resources["gateway"],
                {
                    "apiVersion": "gateway.networking.k8s.io/v1",
                    "kind": "Gateway",
                    "metadata": {
                        "name": _GATEWAY_NAME,
                        "namespace": _NAMESPACE_SYSTEM,
                    },
                    "spec": {
                        "gatewayClassName": _TRAEFIK_GATEWAY_CLASS,
                        "listeners": [
                            {
                                "name": "web",
                                "protocol": "HTTP",
                                "port": _TRAEFIK_WEB_ENTRYPOINT_PORT,
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

        resource.update_status(self.rsp.desired.composite, status)

    def derive_conditions(self):
        """Derive readiness for all composed resources and set custom
        conditions."""
        # MetalLB readiness.
        t = self.xr.spec.traefik
        if (
            t
            and t.loadBalancer == "MetalLB"
            and t.metallb
            and t.metallb.addressPool
            and resource.get_condition(self.req.observed.resources.get("metallb"), "Ready").status == "True"
        ):
            self.rsp.desired.resources["metallb"].ready = fnv1.READY_TRUE

        # Traefik readiness.
        traefik_ready = resource.get_condition(self.req.observed.resources.get("traefik"), "Ready").status == "True"
        if traefik_ready:
            self.rsp.desired.resources["traefik"].ready = fnv1.READY_TRUE
            # Transition: Traefik just became ready.
            if "gateway" not in self.req.observed.resources:
                response.normal(self.rsp, "Traefik ready, composing Gateway")

        # ControllerReady condition.
        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_CONTROLLER_READY,
                status="True" if traefik_ready else "False",
                reason=CONDITION_REASON_CONTROLLER_HEALTHY if traefik_ready else CONDITION_REASON_INSTALLING,
            ),
        )

        # GatewayClass and Gateway use Accepted (not Ready) — on kind the
        # Gateway won't be Programmed (no LoadBalancer), but Accepted means
        # the controller has scheduled it and it's usable.
        if resource.get_condition(self.req.observed.resources.get("gateway-class"), "Accepted").status == "True":
            self.rsp.desired.resources["gateway-class"].ready = fnv1.READY_TRUE

        if resource.get_condition(self.req.observed.resources.get("gateway"), "Accepted").status == "True":
            self.rsp.desired.resources["gateway"].ready = fnv1.READY_TRUE

    def compose_pc_usage(self, release_key):
        """Compose a Usage protecting the ProviderConfig from deletion until
        the given Helm release is gone."""
        resource.update(
            self.rsp.desired.resources[f"usage-pc-by-{release_key}"],
            usagev1beta1.Usage(
                metadata=metav1.ObjectMeta(namespace=_NAMESPACE_SYSTEM),
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
                            matchLabels={_LABEL_RELEASE: release_key},
                        ),
                    ),
                    replayDeletion=True,
                ),
            ),
        )
        self.rsp.desired.resources[f"usage-pc-by-{release_key}"].ready = fnv1.READY_TRUE
