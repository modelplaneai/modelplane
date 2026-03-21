from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .model.ai.modelplane.inferencegateway import v1alpha1
from .model.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

_NAMESPACE = "modelplane-system"
_GATEWAY_NAME = "modelplane"


def _has_condition(req: fnv1.RunFunctionRequest, name: str, cond: str) -> bool:
    """Check if an observed composed resource has the given condition True.

    Uses the SDK's get_condition which reads status.conditions from the
    protobuf Struct. Works for both Crossplane conditions (Ready, Synced)
    and Gateway API conditions (Accepted, Programmed).
    """
    observed = req.observed.resources.get(name)
    if observed is None:
        return False
    return resource.get_condition(observed.resource, cond).status == "True"


def _helm_release(
    chart: str,
    repo: str,
    version: str,
    namespace: str,
    provider_config: str,
    values: dict | None = None,
) -> helmv1beta1.Release:
    """Build a Helm Release with metadata.namespace set explicitly.

    Cluster-scoped XRs don't auto-populate the namespace on composed
    namespaced resources, so we set it here.
    """
    release = helmv1beta1.Release(
        metadata=metav1.ObjectMeta(namespace=_NAMESPACE),
        spec=helmv1beta1.Spec(
            providerConfigRef=helmv1beta1.ProviderConfigRef(
                kind="ClusterProviderConfig",
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


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    xr = v1alpha1.InferenceGateway(
        **resource.struct_to_dict(req.observed.composite.resource)
    )

    eg = xr.spec.envoyGateway
    eg_version = eg.version if eg else "v1.3.0"
    gw = xr.spec.gateway
    # Protobuf Struct delivers all numbers as float.
    gw_port = int(gw.port) if gw and gw.port else 80

    pc_name = "modelplane-in-cluster"

    # 1. Compose a ClusterProviderConfig for provider-helm targeting the
    #    control plane (in-cluster identity).
    resource.update(rsp.desired.resources["provider-config-helm"], {
        "apiVersion": "helm.m.crossplane.io/v1beta1",
        "kind": "ClusterProviderConfig",
        "metadata": {"name": pc_name},
        "spec": {
            "credentials": {"source": "InjectedIdentity"},
        },
    })
    rsp.desired.resources["provider-config-helm"].ready = fnv1.READY_TRUE

    # 2. Compose the modelplane-system namespace.
    resource.update(rsp.desired.resources["namespace"], {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": _NAMESPACE},
    })
    rsp.desired.resources["namespace"].ready = fnv1.READY_TRUE

    # 3. If MetalLB is requested, compose it (for kind / bare-metal clusters).
    lb = eg.loadBalancer if eg else None
    address_pool = eg.metallb.addressPool if eg and eg.metallb else ""

    if lb == "MetalLB" and address_pool:
        metallb_ns = "metallb-system"

        resource.update(rsp.desired.resources["namespace-metallb"], {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": metallb_ns},
        })
        rsp.desired.resources["namespace-metallb"].ready = fnv1.READY_TRUE

        metallb_exists = "metallb" in req.observed.resources
        pc_observed = "provider-config-helm" in req.observed.resources
        if pc_observed or metallb_exists:
            resource.update(
                rsp.desired.resources["metallb"],
                _helm_release(
                    chart="metallb",
                    repo="https://metallb.github.io/metallb",
                    version="0.14.9",
                    namespace=metallb_ns,
                    provider_config=pc_name,
                ),
            )

        # Gate the IPAddressPool and L2Advertisement on MetalLB being ready.
        metallb_ready = _has_condition(req, "metallb", "Ready")
        pool_exists = "metallb-pool" in req.observed.resources
        if metallb_ready or pool_exists:
            resource.update(rsp.desired.resources["metallb-pool"], {
                "apiVersion": "metallb.io/v1beta1",
                "kind": "IPAddressPool",
                "metadata": {"name": "modelplane", "namespace": metallb_ns},
                "spec": {"addresses": [address_pool]},
            })
            rsp.desired.resources["metallb-pool"].ready = fnv1.READY_TRUE

            resource.update(rsp.desired.resources["metallb-l2"], {
                "apiVersion": "metallb.io/v1beta1",
                "kind": "L2Advertisement",
                "metadata": {"name": "modelplane", "namespace": metallb_ns},
                "spec": {"ipAddressPools": ["modelplane"]},
            })
            rsp.desired.resources["metallb-l2"].ready = fnv1.READY_TRUE

    # 4. Gate Envoy Gateway on the ProviderConfig being observed.
    pc_observed = "provider-config-helm" in req.observed.resources
    envoy_gw_exists = "envoy-gateway" in req.observed.resources
    if pc_observed or envoy_gw_exists:
        resource.update(
            rsp.desired.resources["envoy-gateway"],
            _helm_release(
                chart="gateway-helm",
                repo="oci://docker.io/envoyproxy",
                version=eg_version or "v1.3.0",
                namespace="envoy-gateway-system",
                provider_config=pc_name,
                values={
                    "config": {
                        "envoyGateway": {
                            "extensionApis": {"enableBackend": True},
                        },
                    },
                },
            ),
        )

    # 5. Gate GatewayClass and Gateway on Envoy Gateway being ready.
    envoy_gw_ready = _has_condition(req, "envoy-gateway", "Ready")
    gw_class_exists = "gateway-class" in req.observed.resources
    gw_exists = "gateway" in req.observed.resources

    if envoy_gw_ready or gw_class_exists:
        resource.update(rsp.desired.resources["gateway-class"], {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "GatewayClass",
            "metadata": {"name": "envoy"},
            "spec": {
                "controllerName": "gateway.envoyproxy.io/gatewayclass-controller",
            },
        })

    if envoy_gw_ready or gw_exists:
        resource.update(rsp.desired.resources["gateway"], {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "Gateway",
            "metadata": {"name": _GATEWAY_NAME, "namespace": _NAMESPACE},
            "spec": {
                "gatewayClassName": "envoy",
                "listeners": [{
                    "name": "http",
                    "protocol": "HTTP",
                    "port": gw_port,
                    "allowedRoutes": {"namespaces": {"from": "All"}},
                }],
            },
        })

    # 6. Read the observed Gateway's status to extract the external address.
    gateway_address = None
    gw_observed = req.observed.resources.get("gateway")
    if gw_observed:
        gw_dict = resource.struct_to_dict(gw_observed.resource)
        addresses = gw_dict.get("status", {}).get("addresses", [])
        if addresses:
            gateway_address = addresses[0].get("value")

    # 7. Write status.
    status: dict = {
        "gateway": {"name": _GATEWAY_NAME, "namespace": _NAMESPACE},
    }
    if gateway_address:
        status["gateway"]["address"] = gateway_address
    resource.update(rsp.desired.composite, {"status": status})

    # 8. Readiness.
    all_ready = True
    not_ready = []

    if lb == "MetalLB" and address_pool:
        if _has_condition(req, "metallb", "Ready"):
            rsp.desired.resources["metallb"].ready = fnv1.READY_TRUE
        else:
            all_ready = False
            not_ready.append("metallb")

    if _has_condition(req, "envoy-gateway", "Ready"):
        rsp.desired.resources["envoy-gateway"].ready = fnv1.READY_TRUE
    else:
        all_ready = False
        not_ready.append("envoy-gateway")

    if _has_condition(req, "gateway-class", "Accepted"):
        rsp.desired.resources["gateway-class"].ready = fnv1.READY_TRUE
    else:
        all_ready = False
        not_ready.append("gateway-class")

    # Gateway: check Accepted (not Programmed). On kind the Gateway won't be
    # Programmed (no LoadBalancer), but Accepted means the controller has
    # scheduled it and it's usable.
    if _has_condition(req, "gateway", "Accepted"):
        rsp.desired.resources["gateway"].ready = fnv1.READY_TRUE
    else:
        all_ready = False
        not_ready.append("gateway")

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
