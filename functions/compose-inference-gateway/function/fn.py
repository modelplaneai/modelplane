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

"""Compose the fleet gateway: the front door for inference requests.

The gateway runs on an InferenceCluster, which already runs Envoy Gateway and
Envoy AI Gateway for its own cluster gateway, so this function composes only
Gateway API and Envoy objects onto that cluster. It installs nothing.

A cluster hosts at most one fleet gateway, because a second would contend for
the same listener. Where two InferenceGateways name one cluster the earlier
name wins and the other reports why rather than fighting over it.

What a request meets here, in order: TLS terminates on the listener; the
caller's key is matched against the Secrets auth selects and resolved to an
identity stamped on the request; the model named in the body picks an
AIGatewayRoute that compose-model-service composed for a ModelService; and that
route's backends, also composed there, translate the request for whichever
endpoint wins. This function owns everything gateway-scoped, and nothing
per-service.
"""

import ipaddress

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.inferencegateway import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# Condition types this function sets on the InferenceGateway.
CONDITION_TYPE_GATEWAY_READY = "GatewayReady"

CONDITION_REASON_GATEWAY_PROGRAMMED = "GatewayProgrammed"
CONDITION_REASON_WAITING_FOR_CLUSTER = "WaitingForCluster"
CONDITION_REASON_WAITING_FOR_GATEWAY = "WaitingForGateway"
CONDITION_REASON_CLUSTER_TAKEN = "ClusterAlreadyHasGateway"
CONDITION_REASON_SECRETS_MISSING = "SecretsMissing"
CONDITION_REASON_AUTH_NOT_ACCEPTED = "CallerAuthNotAccepted"

# Namespace every composed object lands in on the gateway's cluster. The
# ServingStack already creates it there.
REMOTE_NAMESPACE = "modelplane-system"

# The namespace on the control plane holding a gateway's Secrets: caller keys
# and TLS certificates. An InferenceGateway is cluster-scoped, so it has no
# namespace of its own to read them from.
CONTROL_PLANE_NAMESPACE = "modelplane-system"

# Names of the objects composed onto the gateway's cluster. One fleet gateway
# per cluster, so these are fixed rather than derived from the XR's name, which
# keeps them stable if a gateway is renamed.
_GATEWAY_NAME = "fleet-gateway"
_HEALTHZ_NAME = "fleet-gateway-healthz"
_CALLERS_NAME = "fleet-gateway-callers"
_FAILOVER_NAME = "fleet-gateway-failover"

# The GatewayClass the ServingStack installs. Its XRD defaults className to
# this, and a cluster hosting a fleet gateway is one the ServingStack has
# already reconciled, so the class exists.
_GATEWAY_CLASS = "envoy"

# The header the gateway stamps the resolved caller identity onto. Requests to
# an endpoint Modelplane doesn't operate have it removed again, by the
# AIServiceBackend compose-model-service composes for that endpoint.
_CALLER_HEADER = "x-modelplane-caller"

# Where the gateway serves each API. These follow the AI Gateway chart's
# endpointConfig defaults (rootPrefix "/", openai "", anthropic "/anthropic"),
# which the ServingStack leaves alone.
_OPENAI_PREFIX = "/v1"
_ANTHROPIC_PREFIX = "/anthropic/v1"

# The path a geo-DNS record or a fronting edge health checks to decide whether
# this gateway is in rotation.
_HEALTHZ_PATH = "/healthz"

# The cluster gateway serves HTTPS here. The resolving Service and its
# EndpointSlice carry the port so it reads coherently, though Envoy resolves the
# name to an address and connects on the Backend's own port regardless.
_CLUSTER_GATEWAY_PORT = 443

# This gateway's own PKI, issued by cert-manager on its cluster, which the
# serving stack installs there. A composition function runs on every reconcile
# and must be a pure function of its inputs, so it can't generate key material.
#
# A self-signed issuer signs a CA, the CA signs the client certificate the
# gateway presents to a cluster gateway, and the CA's certificate is published
# in status. Every InferenceCluster accepts client certificates from it, which
# is how this gateway proves itself and how anything else is refused. The
# private key never leaves this cluster.
_SELFSIGNED_ISSUER = "fleet-gateway-selfsigned"
_CLIENT_CA_ISSUER = "fleet-gateway-ca"
_CLIENT_CA_SECRET = "fleet-gateway-ca"
_CLIENT_CERT_SECRET = "fleet-gateway-client"

# The trust-manager Bundle republishing the client CA's certificate, and so also
# the ConfigMap it syncs, which is what the control plane reads. See
# compose_client_pki.
_CLIENT_CA_BUNDLE = "fleet-gateway-ca"

# A cert-manager Certificate is Ready once it has issued.
_CERTIFICATE_READY_CEL = (
    "has(object.status) && has(object.status.conditions) && "
    "object.status.conditions.exists(c, c.type == 'Ready' && c.status == 'True')"
)

# A trust-manager Bundle is Synced once it has written its target ConfigMaps.
_BUNDLE_SYNCED_CEL = (
    "has(object.status) && has(object.status.conditions) && "
    "object.status.conditions.exists(c, c.type == 'Synced' && c.status == 'True')"
)

# A Gateway is ready once it has an address to hand out.
_GATEWAY_READY_CEL = "has(object.status) && has(object.status.addresses) && object.status.addresses.size() > 0"

# A Gateway API policy reports acceptance per attachment, under
# status.ancestors[].conditions rather than status.conditions.
_POLICY_ACCEPTED_CEL = (
    "has(object.status) && has(object.status.ancestors) && "
    "object.status.ancestors.exists(a, has(a.conditions) && "
    "a.conditions.exists(c, c.type == 'Accepted' && c.status == 'True'))"
)

# Kubernetes injects ndots:5 into every pod, which tells the resolver to try each
# search domain before a name with fewer than five dots. Every hostname this
# gateway resolves has fewer: a provider like api.together.xyz has two, a cluster
# gateway's name three or four. So each lookup first issues one query per search
# domain, and any that a cluster's upstream resolver answers slowly or not at all
# stalls the whole resolution. Envoy then warms the cluster with no endpoints and
# answers 503, having logged only DNS timeouts.
#
# ndots:1 makes these names absolute, so the search domains are skipped. It costs
# the ability to reach a bare single-label name, which no backend uses.
#
# Envoy Gateway has no field for a pod's dnsConfig, so this goes through its
# deployment patch.
_NDOTS_PATCH = {"spec": {"template": {"spec": {"dnsConfig": {"options": [{"name": "ndots", "value": "1"}]}}}}}

# The metadata namespace the AI Gateway's ext-proc writes per-request values to.
_AI_METADATA = "io.envoy.ai_gateway"


def _md(key: str) -> str:
    """An access log command operator reading one AI Gateway metadata key."""
    return f"%DYNAMIC_METADATA({_AI_METADATA}:{key})%"


# One usage record per request. This is the only place a token count and the
# tenant that incurred it are visible together: engine metrics are per-model
# with no caller dimension, and a provider's are not ours to read.
#
# The token counts come from metadata rather than the response body because the
# ext-proc has already parsed them, including from a streamed response's final
# usage frame, which it asks the backend for on our behalf.
#
# The caller comes from metadata for a different reason. The header carrying it
# is removed before a request reaches a backend Modelplane doesn't operate, so
# as not to disclose a tenant to a third-party provider. Reading the header here
# would drop the caller from exactly the records that price provider spend.
_USAGE_RECORD = {
    "caller": _md("caller"),
    "service": "%REQ(X-AI-EG-MODEL)%",
    # The AIServiceBackend that served, as "<namespace>/<name>". That is the
    # per-service copy of a ModelEndpoint rather than the endpoint itself, so
    # it reads as "<remote ns>/<service ns>-<service>-<endpoint>". The
    # ModelEndpoint's own identity isn't available to the gateway: it has no
    # notion of one. Joining a record back to a ModelEndpoint therefore means
    # matching on this and the service, and a name long enough to have been
    # hashed can only be matched by recomputing it.
    "endpoint": _md("ai_service_backend_name"),
    "served_model": _md("model_name_override"),
    "response_model": _md("response_model"),
    "input_tokens": _md("llm_input_token"),
    "output_tokens": _md("llm_output_token"),
    "total_tokens": _md("llm_total_token"),
    "status": "%RESPONSE_CODE%",
    "duration_ms": "%DURATION%",
    "start_time": "%START_TIME%",
}


def _name(md) -> str:  # noqa: ANN001  # generated ObjectMeta models vary by kind
    """The name of a resource, from its generated ObjectMeta."""
    return md.name if md and md.name else ""


def _ip_version(address: str) -> int | None:
    """The IP version of an address, or None if it isn't an IP literal.

    A cluster gateway's address is either an IP literal or a load balancer's own
    DNS name, and the two are resolved differently. Parsing rather than matching,
    because IPv6 is not a thing to write a regex for.
    """
    try:
        return ipaddress.ip_address(address).version
    except ValueError:
        return None


def _wrap(provider_config: str, manifest: dict, *, cel_query: str | None = None) -> k8sobjv1alpha1.Object:
    """Wrap a manifest in a provider-kubernetes Object for the gateway's cluster.

    The Object's own namespace is set explicitly because an InferenceGateway is
    cluster-scoped, and Crossplane only defaults a composed namespaced
    resource's namespace from a namespaced composite. Left unset, every reconcile
    fails with "an empty namespace may not be set when a resource name is
    provided" before composing anything.

    Readiness defaults to SuccessfulCreate, which is right for the policies and
    Secrets that have no runtime status worth waiting on. The Gateway passes a
    cel_query so its readiness reflects having been programmed.
    """
    readiness = (
        k8sobjv1alpha1.Readiness(policy="DeriveFromCelQuery", celQuery=cel_query)
        if cel_query is not None
        else k8sobjv1alpha1.Readiness(policy="SuccessfulCreate")
    )
    return k8sobjv1alpha1.Object(
        metadata=metav1.ObjectMeta(namespace=CONTROL_PLANE_NAMESPACE),
        spec=k8sobjv1alpha1.Spec(
            providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                kind="ClusterProviderConfig",
                name=provider_config,
            ),
            readiness=readiness,
            forProvider=k8sobjv1alpha1.ForProvider(manifest=manifest),
        ),
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
        Composer(req, rsp).compose()
        return rsp


class Composer:
    def __init__(self, req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse) -> None:
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.InferenceGateway(**resource.struct_to_dict(req.observed.composite.resource))
        self.cluster: icv1alpha1.InferenceCluster | None = None
        self.caller_secrets: list[dict] = []
        self.tls_secrets: list[dict] = []

    def compose(self) -> None:
        if not self.resolve_inputs():
            return
        self.compose_secrets()
        self.compose_envoy_proxy()
        self.compose_gateway()
        self.compose_client_pki()
        self.compose_cluster_names()
        self.compose_caller_auth()
        self.compose_failover_policy()
        self.compose_healthz()
        self.write_status()
        self.mark_ready()
        self.derive_conditions()

    def mark_ready(self) -> None:
        """Mark each composed resource ready once its observed counterpart is.

        Nothing else does this. The composition pipeline has no auto-ready
        function, so a desired resource's readiness is whatever the function
        says, and a function that says nothing leaves the XR permanently
        not-Ready however healthy everything under it is.
        """
        for key, res in self.rsp.desired.resources.items():
            if resource.get_condition(self.req.observed.resources.get(key), "Ready").status == "True":
                res.ready = fnv1.READY_TRUE

    def resolve_inputs(self) -> bool:
        """Require the gateway's cluster, its Secrets, and the other gateways.

        Returns False, having set conditions explaining why, when the gateway
        can't be composed yet.
        """
        response.require_resources(
            self.rsp,
            name="cluster",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceCluster",
            match_name=self.xr.spec.clusterName,
        )
        # Every InferenceGateway, to settle which one owns this cluster.
        response.require_resources(
            self.rsp,
            name="gateways",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceGateway",
        )
        # Every InferenceCluster, to resolve each cluster gateway's name to its
        # address on this gateway's cluster (see compose_cluster_names).
        response.require_resources(
            self.rsp,
            name="clusters",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceCluster",
        )
        if self.xr.spec.auth:
            response.require_resources(
                self.rsp,
                name="caller-secrets",
                api_version="v1",
                kind="Secret",
                namespace=CONTROL_PLANE_NAMESPACE,
                match_labels=dict(self.xr.spec.auth.secretSelector.matchLabels),
            )
        for i, ref in enumerate(self.xr.spec.tls.certificateRefs if self.xr.spec.tls else []):
            response.require_resources(
                self.rsp,
                name=f"tls-secret-{i}",
                api_version="v1",
                kind="Secret",
                namespace=CONTROL_PLANE_NAMESPACE,
                match_name=ref.name,
            )

        # A requirement key is absent until it resolves, which is how the SDK
        # distinguishes unresolved from resolved-empty.
        if "cluster" not in self.req.required_resources or "gateways" not in self.req.required_resources:
            self.not_ready(
                CONDITION_REASON_WAITING_FOR_CLUSTER,
                "Waiting for the gateway's cluster and the other gateways to resolve",
            )
            return False

        clusters = request.get_required_resources(self.req, "cluster")
        if not clusters:
            self.not_ready(
                CONDITION_REASON_WAITING_FOR_CLUSTER,
                f"InferenceCluster {self.xr.spec.clusterName} does not exist",
            )
            return False
        self.cluster = icv1alpha1.InferenceCluster.model_validate(clusters[0])

        if not self.owns_cluster():
            return False

        if not (
            self.cluster.status and self.cluster.status.providerConfigRef and self.cluster.status.providerConfigRef.name
        ):
            self.not_ready(
                CONDITION_REASON_WAITING_FOR_CLUSTER,
                f"InferenceCluster {self.xr.spec.clusterName} has not published a providerConfigRef",
            )
            return False

        return self.resolve_secrets()

    def owns_cluster(self) -> bool:
        """Whether this gateway is the one that runs on its cluster.

        Two gateways on one cluster would contend for the same listener, so the
        incumbent wins: whichever already has an address keeps it. A gateway
        created later reports why rather than taking the cluster over, because
        taking it over would delete the winner's Gateway and bring its load
        balancer back on a different address, which is the thing a gateway is
        never allowed to do to its callers.

        With no incumbent, the oldest wins, and the lowest name breaks a tie in
        creation time. Both are stable across reconciles and identical in every
        gateway's function, so nobody flaps.
        """
        mine = _name(self.xr.metadata)
        rivals: list[tuple[int, str, str]] = []
        for g in request.get_required_resources(self.req, "gateways"):
            gw = v1alpha1.InferenceGateway.model_validate(g)
            if gw.spec.clusterName != self.xr.spec.clusterName:
                continue
            serving = bool(gw.status and gw.status.address)
            # creationTimestamp is a RootModel wrapping a datetime, so age
            # comes off the datetime it holds. An unset one sorts oldest,
            # which only happens before the API server has stamped it.
            stamp = gw.metadata.creationTimestamp if gw.metadata else None
            created = stamp.root.isoformat() if stamp else ""
            # Sorts incumbents first, then by age, then by name.
            rivals.append((0 if serving else 1, created, _name(gw.metadata)))
        rivals.sort()
        if rivals and rivals[0][2] != mine:
            self.not_ready(
                CONDITION_REASON_CLUSTER_TAKEN,
                f"InferenceCluster {self.xr.spec.clusterName} already hosts InferenceGateway {rivals[0][2]}",
            )
            return False
        return True

    def resolve_secrets(self) -> bool:
        """Resolve the caller-key and TLS Secrets this gateway propagates."""
        if self.xr.spec.auth:
            if "caller-secrets" not in self.req.required_resources:
                self.not_ready(CONDITION_REASON_SECRETS_MISSING, "Waiting for caller key Secrets to resolve")
                return False
            self.caller_secrets = request.get_required_resources(self.req, "caller-secrets")
            if not self.caller_secrets:
                # Composing auth that selects nothing would accept no caller at
                # all, which looks identical to a broken key from outside.
                self.not_ready(
                    CONDITION_REASON_SECRETS_MISSING,
                    "spec.auth.secretSelector matches no Secret, so no caller could authenticate",
                )
                return False

        for i, ref in enumerate(self.xr.spec.tls.certificateRefs if self.xr.spec.tls else []):
            key = f"tls-secret-{i}"
            if key not in self.req.required_resources:
                self.not_ready(CONDITION_REASON_SECRETS_MISSING, f"Waiting for TLS Secret {ref.name} to resolve")
                return False
            found = request.get_required_resources(self.req, key)
            if not found:
                self.not_ready(CONDITION_REASON_SECRETS_MISSING, f"TLS Secret {ref.name} does not exist")
                return False
            self.tls_secrets.append(found[0])

        return True

    @property
    def pc(self) -> str:
        """The ClusterProviderConfig targeting the gateway's cluster."""
        assert self.cluster and self.cluster.status and self.cluster.status.providerConfigRef
        return self.cluster.status.providerConfigRef.name or ""

    def compose_secrets(self) -> None:
        """Copy the gateway's Secrets to its cluster.

        The data is copied verbatim, base64 and all, so re-encoding can't
        corrupt a value. Caller keys land under a prefixed name; each certificate
        keeps the name the XR referenced it by, since the Gateway's
        certificateRefs name them.

        Keyed by the source Secret's name, never by its position in the selector's
        results. Those come back in API server order, so keying by index means
        deleting one Secret repoints every later Object at a different Secret and
        drops the last one. Deleting an Object deletes the remote object it
        manages, so a Secret the SecurityPolicy still names can vanish, and Envoy
        Gateway fails an unresolvable credential ref closed with a 500 on every
        route of the gateway.
        """
        for secret in self.caller_secrets:
            src = secret.get("metadata", {}).get("name", "")
            resource.update(
                self.rsp.desired.resources[f"caller-secret-{src}"],
                _wrap(
                    self.pc,
                    {
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "metadata": {"name": f"callers-{src}", "namespace": REMOTE_NAMESPACE},
                        "type": "Opaque",
                        "data": secret.get("data", {}),
                    },
                ),
            )
        for secret in self.tls_secrets:
            src = secret.get("metadata", {}).get("name", "")
            resource.update(
                self.rsp.desired.resources[f"tls-secret-{src}"],
                _wrap(
                    self.pc,
                    {
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "metadata": {"name": src, "namespace": REMOTE_NAMESPACE},
                        "type": "kubernetes.io/tls",
                        "data": secret.get("data", {}),
                    },
                ),
            )

    def compose_envoy_proxy(self) -> None:
        """An EnvoyProxy carrying this gateway's usage-record access log.

        Attached to the Gateway rather than the GatewayClass, because the class
        is shared with the cluster gateway, whose per-pod routing has nothing to
        log here.

        Every token field reads request metadata rather than a header or the
        response body. The AI Gateway's ext-proc writes the counts there, having
        also asked the backend for usage on streamed responses, which otherwise
        report none. The caller is read from metadata for a different reason:
        the header carrying it is removed before the request reaches a
        third-party backend, so a log reading the header would drop the caller
        from exactly the records that attribute provider spend.
        """
        resource.update(
            self.rsp.desired.resources["envoy-proxy"],
            _wrap(
                self.pc,
                {
                    "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                    "kind": "EnvoyProxy",
                    "metadata": {"name": _GATEWAY_NAME, "namespace": REMOTE_NAMESPACE},
                    "spec": {
                        "provider": {
                            "type": "Kubernetes",
                            "kubernetes": {
                                "envoyService": {"externalTrafficPolicy": "Cluster"},
                                "envoyDeployment": {"patch": {"type": "StrategicMerge", "value": _NDOTS_PATCH}},
                            },
                        },
                        "telemetry": {
                            "accessLog": {
                                "settings": [
                                    {
                                        "format": {
                                            "type": "JSON",
                                            "json": _USAGE_RECORD,
                                        },
                                        "sinks": [{"type": "File", "file": {"path": "/dev/stdout"}}],
                                    }
                                ]
                            }
                        },
                    },
                },
            ),
        )

    def compose_gateway(self) -> None:
        """The Gateway callers connect to.

        Always an HTTP listener, so a gateway with neither hostname nor
        certificate still answers, which is the getting-started shape and the
        shape behind someone else's edge. An HTTPS listener joins it when the XR
        carries TLS.

        The HTTP listener deliberately carries no hostname even when the XR has
        one. A listener hostname is matched against the request's Host, so
        setting it would 404 anything addressed by IP, and status.address is
        exactly what a geo-DNS record or fronting edge health checks at /healthz.
        The HTTPS listener does need one, to choose a certificate.

        Routes are accepted only from this namespace. Every route Modelplane
        composes lands here, and accepting them from anywhere would let anyone
        who can create an HTTPRoute on this cluster attach to the authenticated
        front door, overriding its SecurityPolicy the way /healthz does.
        """
        listeners: list[dict] = [
            {
                "name": "http",
                "protocol": "HTTP",
                "port": 80,
                "allowedRoutes": {"namespaces": {"from": "Same"}},
            }
        ]
        if self.xr.spec.tls:
            listeners.append(
                {
                    "name": "https",
                    "protocol": "HTTPS",
                    "port": 443,
                    "hostname": self.xr.spec.hostname,
                    "tls": {
                        "mode": "Terminate",
                        "certificateRefs": [{"name": r.name} for r in self.xr.spec.tls.certificateRefs],
                    },
                    "allowedRoutes": {"namespaces": {"from": "Same"}},
                }
            )

        resource.update(
            self.rsp.desired.resources["gateway"],
            _wrap(
                self.pc,
                {
                    "apiVersion": "gateway.networking.k8s.io/v1",
                    "kind": "Gateway",
                    "metadata": {"name": _GATEWAY_NAME, "namespace": REMOTE_NAMESPACE},
                    "spec": {
                        "gatewayClassName": _GATEWAY_CLASS,
                        "infrastructure": {
                            "parametersRef": {
                                "group": "gateway.envoyproxy.io",
                                "kind": "EnvoyProxy",
                                "name": _GATEWAY_NAME,
                            }
                        },
                        "listeners": listeners,
                    },
                },
                cel_query=_GATEWAY_READY_CEL,
            ),
        )

    def compose_client_pki(self) -> None:
        """Compose the certificate this gateway presents to a cluster gateway.

        A cluster gateway refuses a request that arrives without one, so this is
        what lets the fleet gateway reach the engines behind it and stops
        anything else. cert-manager on this gateway's cluster does the issuing.

        The certificate's subject is this gateway's name. Nothing matches on it:
        a cluster gateway checks the signing CA, not the subject, because what it
        needs to know is that a fleet gateway is calling rather than which one.
        """
        gateway = _name(self.xr.metadata)
        objects: list[tuple[str, dict, str | None]] = [
            (
                "client-selfsigned-issuer",
                {
                    "apiVersion": "cert-manager.io/v1",
                    "kind": "Issuer",
                    "metadata": {"name": _SELFSIGNED_ISSUER, "namespace": REMOTE_NAMESPACE},
                    "spec": {"selfSigned": {}},
                },
                None,
            ),
            (
                "client-ca-certificate",
                {
                    "apiVersion": "cert-manager.io/v1",
                    "kind": "Certificate",
                    "metadata": {"name": _CLIENT_CA_ISSUER, "namespace": REMOTE_NAMESPACE},
                    "spec": {
                        "isCA": True,
                        # Bounded to the 64-byte X.509 commonName limit; the
                        # gateway name is a cluster-scoped resource name. The CN
                        # is cosmetic, since a cluster gateway trusts this CA by
                        # its certificate rather than its name.
                        "commonName": f"modelplane fleet gateway CA {gateway}"[:64],
                        "secretName": _CLIENT_CA_SECRET,
                        "duration": "87600h",
                        "renewBefore": "8760h",
                        "privateKey": {"algorithm": "ECDSA", "size": 256},
                        "issuerRef": {"name": _SELFSIGNED_ISSUER, "kind": "Issuer", "group": "cert-manager.io"},
                    },
                },
                _CERTIFICATE_READY_CEL,
            ),
            (
                "client-ca-issuer",
                {
                    "apiVersion": "cert-manager.io/v1",
                    "kind": "Issuer",
                    "metadata": {"name": _CLIENT_CA_ISSUER, "namespace": REMOTE_NAMESPACE},
                    "spec": {"ca": {"secretName": _CLIENT_CA_SECRET}},
                },
                None,
            ),
            (
                "client-certificate",
                {
                    "apiVersion": "cert-manager.io/v1",
                    "kind": "Certificate",
                    "metadata": {"name": _CLIENT_CERT_SECRET, "namespace": REMOTE_NAMESPACE},
                    "spec": {
                        "secretName": _CLIENT_CERT_SECRET,
                        "commonName": f"fleet-gateway-{gateway}"[:64],
                        "usages": ["client auth", "digital signature", "key encipherment"],
                        "duration": "2160h",
                        "renewBefore": "720h",
                        "privateKey": {"algorithm": "ECDSA", "size": 256, "rotationPolicy": "Always"},
                        "issuerRef": {"name": _CLIENT_CA_ISSUER, "kind": "Issuer", "group": "cert-manager.io"},
                    },
                },
                _CERTIFICATE_READY_CEL,
            ),
            # Republish the CA certificate on its own, so the control plane can
            # read it without reading the private key next to it. A cluster
            # gateway needs this certificate to know a fleet gateway is calling,
            # and the only route to it is through this gateway's status.
            #
            # cert-manager writes ca.crt and tls.key into one Secret. Observing
            # that Secret would mean provider-kubernetes copying the whole thing
            # into the Object's status, private key included, where anyone who
            # can get objects could read it and mint a client certificate every
            # cluster trusts. A Bundle takes one named key from a Secret and
            # writes it to a ConfigMap, so the key is read once, in-cluster, by a
            # controller already entitled to it.
            (
                "client-ca-bundle",
                {
                    "apiVersion": "trust.cert-manager.io/v1alpha1",
                    "kind": "Bundle",
                    # Cluster-scoped, and it names the ConfigMap it syncs.
                    "metadata": {"name": _CLIENT_CA_BUNDLE},
                    "spec": {
                        "sources": [{"secret": {"name": _CLIENT_CA_SECRET, "key": "ca.crt"}}],
                        "target": {
                            "configMap": {"key": "ca.crt"},
                            # A target syncs to every namespace by default. Only
                            # modelplane-system reads it.
                            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": REMOTE_NAMESPACE}},
                        },
                    },
                },
                _BUNDLE_SYNCED_CEL,
            ),
            # Observed, not managed: trust-manager owns this ConfigMap, and
            # status only needs to read the CA certificate back out of it.
            (
                "client-ca-configmap",
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": _CLIENT_CA_BUNDLE, "namespace": REMOTE_NAMESPACE},
                },
                None,
            ),
        ]
        for key, manifest, cel in objects:
            obj = _wrap(self.pc, manifest, cel_query=cel)
            if key == "client-ca-configmap":
                obj.spec.managementPolicies = ["Observe"]
            resource.update(self.rsp.desired.resources[key], obj)

    def observed_client_ca(self) -> str | None:
        """This gateway's client CA certificate, read off the ConfigMap
        trust-manager syncs. A ConfigMap holds it as plain text, so unlike a
        Secret there is nothing to decode."""
        obj = self.req.observed.resources.get("client-ca-configmap")
        if obj is None:
            return None
        d = resource.struct_to_dict(obj.resource)
        data = d.get("status", {}).get("atProvider", {}).get("manifest", {}).get("data", {})
        return data.get("ca.crt") or None

    def compose_cluster_names(self) -> None:
        """Resolve each cluster gateway's internal name to its address, here.

        A ModelService's backends address a cluster gateway by the name
        compose-inference-cluster derives, carried on ModelEndpoint.spec.origin,
        and Envoy resolves that name itself. So this gateway's cluster needs a
        Service of that name pointing at the cluster gateway's address, for every
        cluster this gateway might route to, its own included when that cluster
        serves models too. A platform publishes no DNS for any of them.

        A cluster that hasn't published both an address and its name has no
        gateway to reach yet, so it gets no Service.
        """
        if "clusters" not in self.req.required_resources:
            return
        for c in request.get_required_resources(self.req, "clusters"):
            cluster = icv1alpha1.InferenceCluster.model_validate(c)
            gw = cluster.status.gateway if cluster.status else None
            if not (gw and gw.address and gw.hostname):
                continue
            self.compose_cluster_name(gw.hostname, gw.address)

    def compose_cluster_name(self, hostname: str, address: str) -> None:
        """Compose the Service that resolves one cluster gateway's name.

        The Service's name is the hostname's first label, so its cluster-DNS name
        is the whole hostname; the derivation lives in compose-inference-cluster
        and this only splits the label back off. An address that is an IP is
        served by a headless Service and an EndpointSlice carrying it; a hostname,
        which is how a cloud load balancer names itself, by an ExternalName
        Service. The resolvable name is identical either way, so the Backend that
        points at it, the SNI, and the certificate SAN never branch on this.
        """
        label = hostname.split(".", 1)[0]
        version = _ip_version(address)
        if version is None:
            resource.update(
                self.rsp.desired.resources[f"cluster-name-{label}"],
                _wrap(
                    self.pc,
                    {
                        "apiVersion": "v1",
                        "kind": "Service",
                        "metadata": {"name": label, "namespace": REMOTE_NAMESPACE},
                        "spec": {"type": "ExternalName", "externalName": address},
                    },
                ),
            )
            return
        # Selectorless and headless: cluster DNS answers with the EndpointSlice's
        # address directly, so Envoy connects to the load balancer rather than
        # hairpinning through a ClusterIP.
        resource.update(
            self.rsp.desired.resources[f"cluster-name-{label}"],
            _wrap(
                self.pc,
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": label, "namespace": REMOTE_NAMESPACE},
                    "spec": {"clusterIP": "None", "ports": [{"name": "https", "port": _CLUSTER_GATEWAY_PORT}]},
                },
            ),
        )
        resource.update(
            self.rsp.desired.resources[f"cluster-name-slice-{label}"],
            _wrap(
                self.pc,
                {
                    "apiVersion": "discovery.k8s.io/v1",
                    "kind": "EndpointSlice",
                    "metadata": {
                        "name": label,
                        "namespace": REMOTE_NAMESPACE,
                        "labels": {"kubernetes.io/service-name": label},
                    },
                    "addressType": f"IPv{version}",
                    "ports": [{"name": "https", "port": _CLUSTER_GATEWAY_PORT}],
                    "endpoints": [{"addresses": [address], "conditions": {"ready": True}}],
                },
            ),
        )

    def compose_caller_auth(self) -> None:
        """A SecurityPolicy authenticating callers against the selected Secrets.

        Each key in a Secret is one caller: the entry's name is the identity,
        which the policy resolves and forwards as a header, and the value is the
        key, which it strips so it travels no further. The filter also accepts a
        key sent as "Bearer <key>", which is how an OpenAI client sends it.

        Composed only when the XR asks for auth. Without it the gateway
        authenticates nobody, which is a deliberate shape for running behind
        something that already has.
        """
        if not self.xr.spec.auth:
            return
        refs = [{"name": f"callers-{s.get('metadata', {}).get('name', '')}"} for s in self.caller_secrets]
        resource.update(
            self.rsp.desired.resources["caller-auth"],
            _wrap(
                self.pc,
                {
                    "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                    "kind": "SecurityPolicy",
                    "metadata": {"name": _CALLERS_NAME, "namespace": REMOTE_NAMESPACE},
                    "spec": {
                        "targetRefs": [
                            {
                                "group": "gateway.networking.k8s.io",
                                "kind": "Gateway",
                                "name": _GATEWAY_NAME,
                            }
                        ],
                        "apiKeyAuth": {
                            "credentialRefs": refs,
                            "extractFrom": [{"headers": ["Authorization"]}],
                            "forwardClientIDHeader": _CALLER_HEADER,
                            "sanitize": True,
                        },
                    },
                },
                # Readiness tracks the policy being Accepted, not merely applied.
                # Envoy Gateway rejects the whole policy when two selected
                # Secrets share a key value, and an unresolvable credential ref
                # fails closed with a 500 on every route. Both would otherwise
                # leave the gateway reporting Ready while refusing every caller.
                cel_query=_POLICY_ACCEPTED_CEL,
            ),
        )

    def compose_failover_policy(self) -> None:
        """A BackendTrafficPolicy that makes a ModelService's priorities mean
        something, and ejects an endpoint that keeps failing.

        A ModelService's priority is stamped as an endpoint locality priority,
        which on its own changes nothing: Envoy only tries a lower priority when
        a retry predicate tells it to, and numAttemptsPerPriority is what
        installs one. Without this policy every endpoint in a service shares
        traffic regardless of priority, so failover silently doesn't happen.

        It targets the Gateway rather than each route because a
        BackendTrafficPolicy can only target a Gateway or a route, never a
        backend, so per-endpoint tuning isn't available either way, and one
        policy per gateway beats one per ModelService.

        Retrying is bounded by the first byte reaching the caller. Past that the
        tokens are sent and a retry would duplicate them, so a backend dying
        mid-stream truncates the response rather than failing over.
        """
        resource.update(
            self.rsp.desired.resources["failover-policy"],
            _wrap(
                self.pc,
                {
                    "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                    "kind": "BackendTrafficPolicy",
                    "metadata": {"name": _FAILOVER_NAME, "namespace": REMOTE_NAMESPACE},
                    "spec": {
                        "targetRefs": [
                            {
                                "group": "gateway.networking.k8s.io",
                                "kind": "Gateway",
                                "name": _GATEWAY_NAME,
                            }
                        ],
                        "retry": {
                            "numAttemptsPerPriority": 1,
                            "numRetries": 3,
                            "retryOn": {
                                # retriable-status-codes has to be among the
                                # triggers for httpStatusCodes to do anything.
                                # Envoy only consults retriable_status_codes when
                                # retry_on names it, and Envoy Gateway replaces
                                # retry_on wholesale with whatever triggers says,
                                # so listing a status code without this trigger
                                # is silently inert. A provider answering 503,
                                # which is the case this exists for, would not be
                                # retried and would not fail over.
                                "triggers": [
                                    "connect-failure",
                                    "refused-stream",
                                    "reset",
                                    "retriable-status-codes",
                                ],
                                "httpStatusCodes": [503],
                            },
                        },
                        "healthCheck": {
                            "passive": {
                                "baseEjectionTime": "30s",
                                "consecutive5XxErrors": 5,
                                "interval": "5s",
                                "maxEjectionPercent": 100,
                            },
                            # A sibling of passive, not a field inside it. Nested
                            # wrongly the API server prunes it, the policy still
                            # applies, and panic mode silently stays at its
                            # default.
                            #
                            # That default is 50%: once that share of a cluster's
                            # endpoints is unhealthy Envoy ignores health and
                            # spreads traffic over all of them, ejected ones
                            # included. Every endpoint of a ModelService shares
                            # one cluster, so ejecting a whole priority tier
                            # usually crosses it, and failover would stop working
                            # in exactly the case it exists for. Disabled,
                            # because a request is better refused than sent
                            # somewhere known dead.
                            "panicThreshold": 0,
                        },
                    },
                },
                cel_query=_POLICY_ACCEPTED_CEL,
            ),
        )

    def compose_healthz(self) -> None:
        """A /healthz returning 200 while the gateway is live and able to route.

        This is the target a geo-DNS record or a fronting edge checks to decide
        whether this address is in rotation, so it must answer without a
        credential. A Gateway-level SecurityPolicy covers every route on the
        listener, including this one, so the route carries its own policy to
        override it. Allow-all on this route only; inference still authenticates.
        """
        resource.update(
            self.rsp.desired.resources["healthz-filter"],
            _wrap(
                self.pc,
                {
                    "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                    "kind": "HTTPRouteFilter",
                    "metadata": {"name": _HEALTHZ_NAME, "namespace": REMOTE_NAMESPACE},
                    "spec": {
                        "directResponse": {
                            "statusCode": 200,
                            "contentType": "application/json",
                            "body": {"type": "Inline", "inline": '{"status":"ok"}'},
                        }
                    },
                },
            ),
        )
        resource.update(
            self.rsp.desired.resources["healthz-route"],
            _wrap(
                self.pc,
                {
                    "apiVersion": "gateway.networking.k8s.io/v1",
                    "kind": "HTTPRoute",
                    "metadata": {"name": _HEALTHZ_NAME, "namespace": REMOTE_NAMESPACE},
                    "spec": {
                        "parentRefs": [
                            {
                                "group": "gateway.networking.k8s.io",
                                "kind": "Gateway",
                                "name": _GATEWAY_NAME,
                            }
                        ],
                        "rules": [
                            {
                                "matches": [{"path": {"type": "Exact", "value": _HEALTHZ_PATH}}],
                                "filters": [
                                    {
                                        "type": "ExtensionRef",
                                        "extensionRef": {
                                            "group": "gateway.envoyproxy.io",
                                            "kind": "HTTPRouteFilter",
                                            "name": _HEALTHZ_NAME,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                },
            ),
        )
        if not self.xr.spec.auth:
            return
        resource.update(
            self.rsp.desired.resources["healthz-auth"],
            _wrap(
                self.pc,
                {
                    "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                    "kind": "SecurityPolicy",
                    "metadata": {"name": f"{_HEALTHZ_NAME}-open", "namespace": REMOTE_NAMESPACE},
                    "spec": {
                        "targetRefs": [
                            {
                                "group": "gateway.networking.k8s.io",
                                "kind": "HTTPRoute",
                                "name": _HEALTHZ_NAME,
                            }
                        ],
                        "authorization": {"defaultAction": "Allow"},
                    },
                },
                cel_query=_POLICY_ACCEPTED_CEL,
            ),
        )

    def observed_gateway_address(self) -> str | None:
        """The gateway's address, read back from the composed remote Gateway."""
        obj = self.req.observed.resources.get("gateway")
        if obj is None:
            return None
        d = resource.struct_to_dict(obj.resource)
        addresses = d.get("status", {}).get("atProvider", {}).get("manifest", {}).get("status", {}).get("addresses", [])
        return addresses[0].get("value") if addresses else None

    def write_status(self) -> None:
        """Publish the gateway's address and the URLs callers use.

        The endpoints are built from the hostname when there is one, so what
        status reports is what a caller can actually put in an SDK's base_url,
        and from the address otherwise.
        """
        address = self.observed_gateway_address()
        status = v1alpha1.Status()
        if address:
            status.address = address
        base = None
        if self.xr.spec.hostname:
            base = f"https://{self.xr.spec.hostname}" if self.xr.spec.tls else f"http://{self.xr.spec.hostname}"
        elif address:
            base = f"http://{address}"
        ca = self.observed_client_ca()
        if ca:
            status.clientCACertificate = ca
        if base:
            status.endpoints = v1alpha1.Endpoints(
                openAI=f"{base}{_OPENAI_PREFIX}",
                anthropic=f"{base}{_ANTHROPIC_PREFIX}",
            )
        resource.update_status(self.rsp.desired.composite, status)

    def not_ready(self, reason: str, message: str) -> None:
        """Report that the gateway isn't ready, and why."""
        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_GATEWAY_READY,
                status="False",
                reason=reason,
                message=message,
            ),
        )
        response.normal(self.rsp, message)

    def derive_conditions(self) -> None:
        """GatewayReady tracks the Gateway being programmed and, where the XR
        asks for auth, its caller policy being accepted.

        Both, because a gateway whose policy was rejected answers every request
        with a 500 while its Gateway is perfectly healthy. Reporting Ready then
        would say the front door works when nothing can get through it.
        """
        waiting = [
            key
            for key in (["gateway", "caller-auth"] if self.xr.spec.auth else ["gateway"])
            if resource.get_condition(self.req.observed.resources.get(key), "Ready").status != "True"
        ]
        if not waiting:
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_GATEWAY_READY,
                    status="True",
                    reason=CONDITION_REASON_GATEWAY_PROGRAMMED,
                ),
            )
            return
        if waiting == ["caller-auth"]:
            self.not_ready(
                CONDITION_REASON_AUTH_NOT_ACCEPTED,
                "The gateway's caller authentication policy has not been accepted, so every request is refused. "
                "Two selected Secrets sharing a key value will do this.",
            )
            return
        self.not_ready(
            CONDITION_REASON_WAITING_FOR_GATEWAY,
            f"Waiting for the Gateway on cluster {self.xr.spec.clusterName} to be programmed",
        )
