from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .model.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

_NAMESPACE = "modelplane-system"
_GATEWAY_NAME = "modelplane"


def _is_ready(req: fnv1.RunFunctionRequest, name: str) -> bool:
    observed = req.observed.resources.get(name)
    if observed is None:
        return False
    c = resource.get_condition(observed.resource, "Ready")
    return c.status == "True"


def _check_condition(req: fnv1.RunFunctionRequest, name: str, cond_type: str) -> bool:
    """Check if an observed resource has a specific condition set to True."""
    observed = req.observed.resources.get(name)
    if observed is None:
        return False
    d = resource.struct_to_dict(observed.resource)
    for c in d.get("status", {}).get("conditions", []):
        if c.get("type") == cond_type and c.get("status") == "True":
            return True
    return False


def _helm_release(
    chart: str,
    repo: str,
    version: str,
    namespace: str,
    provider_config: str,
    values: dict | None = None,
) -> helmv1beta1.Release:
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
    xr = resource.struct_to_dict(req.observed.composite.resource)
    spec = xr.get("spec", {})

    eg = spec.get("envoyGateway", {})
    eg_version = eg.get("version", "v1.3.0")
    gw_spec = spec.get("gateway", {})
    gw_port = int(gw_spec.get("port", 80))

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
    lb = eg.get("loadBalancer")
    metallb_cfg = eg.get("metallb", {})
    address_pool = metallb_cfg.get("addressPool", "")

    if lb == "MetalLB" and address_pool:
        metallb_ns = "metallb-system"

        resource.update(rsp.desired.resources["namespace-metallb"], {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": metallb_ns},
        })
        rsp.desired.resources["namespace-metallb"].ready = fnv1.READY_TRUE

        metallb_exists = "metallb" in req.observed.resources
        pc_observed_for_metallb = "provider-config-helm" in req.observed.resources
        if pc_observed_for_metallb or metallb_exists:
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
        metallb_ready = _is_ready(req, "metallb")
        pool_exists = "metallb-pool" in req.observed.resources
        if metallb_ready or pool_exists:
            resource.update(rsp.desired.resources["metallb-pool"], {
                "apiVersion": "metallb.io/v1beta1",
                "kind": "IPAddressPool",
                "metadata": {
                    "name": "modelplane",
                    "namespace": metallb_ns,
                },
                "spec": {
                    "addresses": [address_pool],
                },
            })
            rsp.desired.resources["metallb-pool"].ready = fnv1.READY_TRUE

            resource.update(rsp.desired.resources["metallb-l2"], {
                "apiVersion": "metallb.io/v1beta1",
                "kind": "L2Advertisement",
                "metadata": {
                    "name": "modelplane",
                    "namespace": metallb_ns,
                },
                "spec": {
                    "ipAddressPools": ["modelplane"],
                },
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
            ),
        )

    # 5. Gate GatewayClass and Gateway on Envoy Gateway being ready.
    envoy_gw_ready = _is_ready(req, "envoy-gateway")
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
        rsp.desired.resources["gateway-class"].ready = fnv1.READY_TRUE

    if envoy_gw_ready or gw_exists:
        resource.update(rsp.desired.resources["gateway"], {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "Gateway",
            "metadata": {
                "name": _GATEWAY_NAME,
                "namespace": _NAMESPACE,
            },
            "spec": {
                "gatewayClassName": "envoy",
                "listeners": [{
                    "name": "http",
                    "protocol": "HTTP",
                    "port": gw_port,
                    "allowedRoutes": {
                        "namespaces": {"from": "All"},
                    },
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
        "gateway": {
            "name": _GATEWAY_NAME,
            "namespace": _NAMESPACE,
        },
    }
    if gateway_address:
        status["gateway"]["address"] = gateway_address

    resource.update(rsp.desired.composite, {"status": status})

    # 8. Readiness.
    all_ready = True
    not_ready = []

    # MetalLB Helm release: check Ready condition (only if requested).
    if lb == "MetalLB" and address_pool:
        if _is_ready(req, "metallb"):
            rsp.desired.resources["metallb"].ready = fnv1.READY_TRUE
        else:
            all_ready = False
            not_ready.append("metallb")

    # Envoy Gateway Helm release: check Ready condition.
    if _is_ready(req, "envoy-gateway"):
        rsp.desired.resources["envoy-gateway"].ready = fnv1.READY_TRUE
    else:
        all_ready = False
        not_ready.append("envoy-gateway")

    # GatewayClass: check Accepted condition.
    if _check_condition(req, "gateway-class", "Accepted"):
        rsp.desired.resources["gateway-class"].ready = fnv1.READY_TRUE
    else:
        all_ready = False
        not_ready.append("gateway-class")

    # Gateway: check Accepted condition. On kind clusters the Gateway won't
    # be Programmed (no LoadBalancer), but Accepted means the gateway
    # controller has scheduled it and it's usable.
    if _check_condition(req, "gateway", "Accepted"):
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
