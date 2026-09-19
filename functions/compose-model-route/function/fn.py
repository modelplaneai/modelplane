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

"""Compose a ModelService's routing objects on one gateway's cluster.

A ModelRoute is one ModelService pinned to one InferenceGateway. The
ModelService composes a ModelRoute per gateway that serves it, carrying the
service's endpoint selectors, and this function renders the AIGatewayRoute
matching the service's model name, plus per endpoint the Backend, credential and
policy the gateway needs to reach it, onto that gateway's cluster.

The endpoint resolution and weight distribution are the same for every gateway,
so each ModelRoute redoes it from the same ModelEndpoints rather than the
ModelService pre-resolving and pinning a backend list. That keeps input
resolution in one place, and keeps the ModelRoute self-contained: given only the
gateway it's pinned to and the selectors it carries, it resolves the gateway's
cluster, the endpoints, their credentials and CAs, and renders.
"""

import math

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.inferencegateway import v1alpha1 as igv1alpha1
from models.ai.modelplane.modelendpoint import v1alpha1 as mev1alpha1
from models.ai.modelplane.modelroute import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# Condition types this function sets on the ModelRoute.
CONDITION_TYPE_ROUTING_READY = "RoutingReady"

CONDITION_REASON_ROUTE_ACCEPTED = "RouteAccepted"
CONDITION_REASON_WAITING_FOR_RESOURCES = "WaitingForResources"
CONDITION_REASON_WAITING_FOR_GATEWAY = "WaitingForGateway"
CONDITION_REASON_WAITING_FOR_CLUSTER = "WaitingForCluster"
CONDITION_REASON_NO_ENDPOINTS = "NoReadyEndpoints"
CONDITION_REASON_WAITING_FOR_ROUTE = "WaitingForRoute"

# Objects land in a namespace mirroring the ModelRoute's own, so a service in
# namespace `ml-team` on the control plane composes into child_name("mp",
# "ml-team") on the gateway's cluster and can't collide with another team's. The
# prefix keeps a team namespace named `default` or `kube-system` off the
# cluster's own. The name and label are a cross-function contract with
# compose-inference-gateway, whose Gateway selects routes by the label, and
# compose-model-replica.
_NS_PREFIX = "mp"
_NS_LABEL = "modelplane.ai/namespace"

# The Gateway compose-inference-gateway composes on each gateway's cluster, and
# the namespace it lives in. The route lands in the team's namespace and attaches
# across to the gateway, which its listeners allow by the label.
_GATEWAY_NAME = "inference-gateway"
_GATEWAY_NAMESPACE = "modelplane-system"

# The gateway's client-CA ClusterIssuer, composed by compose-inference-gateway.
_CLIENT_CA_ISSUER = "inference-gateway-ca"

# The Gateway listeners compose-inference-gateway names. A gateway that
# terminates TLS serves inference on the HTTPS listener alone, redirecting :80 to
# it, so a route binds to that listener to keep credentials off the clear :80.
# Without TLS there's only the HTTP listener to bind to.
_LISTENER_HTTP = "http"
_LISTENER_HTTPS = "https"

# The header the AI Gateway's ext-proc puts the request body's model into,
# before the routing decision, so a route can match on it.
_MODEL_HEADER = "x-ai-eg-model"

# The header an InferenceGateway stamps the caller's identity onto. Removed again
# for a backend Modelplane doesn't operate, so a third-party provider isn't told
# which tenant is calling.
_CALLER_HEADER = "x-modelplane-caller"

# The client certificate a backend presents to a cluster gateway.
_CLIENT_CERT_SECRET = "inference-gateway-client"

# The ModelEndpoint api.schema whose backends take their key in x-api-key.
_SCHEMA_ANTHROPIC = "Anthropic"

# Envoy AI Gateway's per-backendRef weight limit, inherited from Gateway API.
_MAX_WEIGHT = 1000000

# An AIGatewayRoute reports acceptance as a top-level condition.
_ROUTE_ACCEPTED_CEL = (
    "has(object.status) && has(object.status.conditions) && "
    "object.status.conditions.exists(c, c.type == 'Accepted' && c.status == 'True')"
)

# A cert-manager Certificate is Ready once it has issued.
_CERTIFICATE_READY_CEL = (
    "has(object.status) && has(object.status.conditions) && "
    "object.status.conditions.exists(c, c.type == 'Ready' && c.status == 'True')"
)

# Token counts to capture per request. Declaring them is also what makes the
# gateway ask a backend for usage on a streamed response, which otherwise
# reports none at all.
_LLM_REQUEST_COSTS = [
    {"metadataKey": "llm_input_token", "type": "InputToken"},
    {"metadataKey": "llm_output_token", "type": "OutputToken"},
    {"metadataKey": "llm_total_token", "type": "TotalToken"},
]

# Set by compose-model-deployment on every ModelEndpoint it composes, naming
# the cluster the replica landed on. Its presence is what marks an endpoint as
# one Modelplane operates.
_LABEL_CLUSTER = "modelplane.ai/cluster"


def _name(meta: metav1.ObjectMeta | None) -> str:
    if meta is None or meta.name is None:
        raise ValueError("metadata.name is unexpectedly absent")
    return meta.name


def _namespace(meta: metav1.ObjectMeta | None) -> str:
    if meta is None or meta.namespace is None:
        raise ValueError("metadata.namespace is unexpectedly absent")
    return meta.namespace


def _labels(meta: metav1.ObjectMeta | None) -> dict[str, str]:
    return dict(meta.labels) if meta and meta.labels else {}


def _composed_by_modelplane(ep: mev1alpha1.ModelEndpoint) -> bool:
    """Whether Modelplane composed this endpoint, and so operates it.

    Decided by the cluster label compose-model-deployment stamps on a composed
    endpoint. A hand-written endpoint carrying it is claiming to be ours, and
    will be treated as ours.
    """
    return _LABEL_CLUSTER in _labels(ep.metadata)


def _api_key_secret(ep: mev1alpha1.ModelEndpoint) -> mev1alpha1.SecretRef | None:
    """The Secret holding an endpoint's API key, if it authenticates with one."""
    cred = ep.spec.credential
    return cred.apiKey.secretRef if cred and cred.apiKey else None


def _endpoint_ready(ep: mev1alpha1.ModelEndpoint) -> bool:
    """Whether a ModelEndpoint reports EndpointReady=True.

    An endpoint that doesn't is left out of the route, so a missing credential
    keeps traffic away rather than failing the requests that reach it.
    """
    for c in ep.status.conditions if ep.status and ep.status.conditions else []:
        if c.type == "EndpointReady":
            return c.status == "True"
    return False


def _distribute_weights(
    entries: list[tuple[int, list[mev1alpha1.ModelEndpoint]]],
) -> list[tuple[mev1alpha1.ModelEndpoint, int]]:
    """Turn per-entry weights into per-backendRef weights within one priority.

    A weight is written per selector entry but applied per backend, so each
    entry's weight is spread across the endpoints it matched, preserving the
    ratio between entries: an entry weighted 90 next to one weighted 10 keeps
    90% of the traffic however many endpoints each matched.

    Every entry's weight is first scaled by a common factor so it is at least
    its endpoint count, because a backend weighted 0 is not merely
    deprioritised, it is dropped from the load assignment entirely. The scaled
    weights are then reduced by their greatest common divisor, and clamped to
    the per-backendRef maximum so even an extreme ratio yields a route the API
    server accepts.

    Called once per priority, because weights only compete within a tier.
    """
    live = [(weight, eps) for weight, eps in entries if eps]
    if not live:
        return []

    # Each endpoint gets (entry weight * scale) // (endpoint count) plus a share
    # of the remainder. Unscaled, an entry whose weight is below its endpoint
    # count would floor some endpoints to 0, so scale must be at least
    # ceil(endpoint count / entry weight) for every entry. One common factor
    # leaves the ratios between entries unchanged.
    scale = 1
    for weight, eps in live:
        scale = max(scale, math.ceil(len(eps) / weight))

    weighted: list[tuple[mev1alpha1.ModelEndpoint, int]] = []
    for weight, eps in live:
        base, remainder = divmod(weight * scale, len(eps))
        for idx, ep in enumerate(eps):
            weighted.append((ep, base + (1 if idx < remainder else 0)))

    weights = [w for _, w in weighted]
    divisor = math.gcd(*weights)
    highest = max(weights) // divisor
    if highest <= _MAX_WEIGHT:
        return [(ep, w // divisor) for ep, w in weighted]

    # An extreme but in-bounds ratio can still exceed the limit once reduced: a
    # max-weight entry beside a tiny one spread over several endpoints scales
    # past it. Rescale so the largest weight lands on the limit, keeping every
    # endpoint at 1 or more, and trading a little precision for a valid route.
    return [(ep, max(1, round(w / divisor / highest * _MAX_WEIGHT))) for ep, w in weighted]


def _k8s_object(provider_config: str, manifest: dict, *, ready_when: str | None = None) -> k8sobjv1alpha1.Object:
    """Wrap a manifest in a provider-kubernetes Object for the gateway's cluster."""
    readiness = (
        k8sobjv1alpha1.Readiness(policy="DeriveFromCelQuery", celQuery=ready_when)
        if ready_when is not None
        else k8sobjv1alpha1.Readiness(policy="SuccessfulCreate")
    )
    return k8sobjv1alpha1.Object(
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
        self.xr = v1alpha1.ModelRoute(**resource.struct_to_dict(req.observed.composite.resource))
        # The namespace on the gateway's cluster this route's objects land in,
        # mirroring the ModelRoute's own so teams can't collide.
        self.namespace = resource.child_name(_NS_PREFIX, _namespace(self.xr.metadata))
        # This ModelRoute's name (one per service and gateway). Per-endpoint
        # objects are named from it so each route owns its own.
        self.route_name = _name(self.xr.metadata)
        self.provider_config = ""
        self.address: str | None = None
        self.inference_listener = _LISTENER_HTTP
        # Endpoints per priority, as (entry weight, endpoints) so weights can be
        # distributed within a tier.
        self.tiers: dict[int, list[tuple[int, list[mev1alpha1.ModelEndpoint]]]] = {}
        self.credentials: dict[str, dict] = {}
        # CA certificate per InferenceCluster, for validating its gateway.
        self.cluster_cas: dict[str, str] = {}
        self.total = 0
        self.ready_count = 0

    def compose(self) -> None:
        if not self.resolve_inputs():
            self.write_status()
            return
        self.compose_namespace()
        self.compose_backends()
        self.compose_route()
        self.write_status()
        self.mark_ready()
        self.derive_conditions()

    def mark_ready(self) -> None:
        """Mark each composed resource ready once its observed counterpart is.

        The composition pipeline has no auto-ready function, so a composed
        resource is Ready only when this function says so.
        """
        for key, res in self.rsp.desired.resources.items():
            if resource.get_condition(self.req.observed.resources.get(key), "Ready").status == "True":
                res.ready = fnv1.READY_TRUE

    def resolve_inputs(self) -> bool:
        """Resolve the pinned gateway, its cluster, and the endpoints behind it.

        Returns False, having set conditions, when there's nothing to compose.
        """
        response.require_resources(
            self.rsp,
            name="gateway",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceGateway",
            match_name=self.xr.spec.gatewayName,
        )
        response.require_resources(
            self.rsp,
            name="clusters",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceCluster",
        )
        ns = _namespace(self.xr.metadata)
        for entry in self.xr.spec.endpoints:
            response.require_resources(
                self.rsp,
                name=f"endpoints-{entry.name}",
                api_version="modelplane.ai/v1alpha1",
                kind="ModelEndpoint",
                namespace=ns,
                match_labels=dict(entry.selector.matchLabels),
            )

        keys = ["gateway", "clusters"] + [f"endpoints-{entry.name}" for entry in self.xr.spec.endpoints]
        if any(k not in self.req.required_resources for k in keys):
            self.not_ready(CONDITION_REASON_WAITING_FOR_RESOURCES, "Waiting for the gateway and endpoints to resolve")
            return False

        if not self.resolve_gateway():
            return False
        self.resolve_cluster_cas()
        self.resolve_endpoints()

        if not self.resolve_credentials():
            return False

        # After resolving, not inside it: an endpoint with no credential needs
        # no Secret but may still be missing its cluster's CA, and resolving
        # returns early when nothing has a credential at all.
        self.drop_unusable_endpoints()
        # One check, after dropping rather than also before it, because dropping
        # only ever removes endpoints. A route with no backendRefs is worse than
        # no route: a caller gets a reply that isn't an error.
        if not any(eps for entries in self.tiers.values() for _, eps in entries):
            self.not_ready(
                CONDITION_REASON_NO_ENDPOINTS,
                f"None of the {self.total} selected ModelEndpoints is ready to carry traffic",
            )
            return False
        return True

    def resolve_gateway(self) -> bool:
        """Resolve the gateway this route is pinned to, and its cluster's
        ProviderConfig.

        The gateway's client PKI has to have issued before backends are composed:
        every composed endpoint's backend names the gateway's client certificate
        Secret, issued from that CA, and Envoy Gateway fails a backend closed when
        the Secret naming its certificate is missing.
        """
        gateways = request.get_required_resources(self.req, "gateway")
        if not gateways:
            self.not_ready(
                CONDITION_REASON_WAITING_FOR_GATEWAY,
                f"InferenceGateway {self.xr.spec.gatewayName} does not exist",
            )
            return False
        gw = igv1alpha1.InferenceGateway.model_validate(gateways[0])

        if not (gw.status and gw.status.clientCACertificate):
            self.not_ready(
                CONDITION_REASON_WAITING_FOR_GATEWAY,
                f"InferenceGateway {self.xr.spec.gatewayName} has not published its client CA",
            )
            return False

        pc = None
        for c in request.get_required_resources(self.req, "clusters"):
            cluster = icv1alpha1.InferenceCluster.model_validate(c)
            if _name(cluster.metadata) != gw.spec.clusterName:
                continue
            if cluster.status and cluster.status.providerConfigRef and cluster.status.providerConfigRef.name:
                pc = cluster.status.providerConfigRef.name
        if pc is None:
            self.not_ready(
                CONDITION_REASON_WAITING_FOR_CLUSTER,
                f"InferenceCluster {gw.spec.clusterName} has not published a providerConfigRef",
            )
            return False

        self.provider_config = pc
        self.address = gw.status.address
        self.inference_listener = _LISTENER_HTTPS if gw.spec.tls else _LISTENER_HTTP
        return True

    def resolve_cluster_cas(self) -> None:
        """Index each cluster's gateway CA certificate by cluster name, for
        validating a composed endpoint's cluster gateway."""
        for c in request.get_required_resources(self.req, "clusters"):
            cluster = icv1alpha1.InferenceCluster.model_validate(c)
            if cluster.status and cluster.status.gateway and cluster.status.gateway.caCertificate:
                self.cluster_cas[_name(cluster.metadata)] = cluster.status.gateway.caCertificate

    def resolve_endpoints(self) -> None:
        """Group ready endpoints by the priority of the entry that selected them.

        An endpoint matched by more than one entry belongs to the first that
        matched it, so a canary entry and a catch-all entry can't both weight
        the same endpoint.
        """
        seen: set[str] = set()
        for entry in self.xr.spec.endpoints:
            matched: list[mev1alpha1.ModelEndpoint] = []
            for d in request.get_required_resources(self.req, f"endpoints-{entry.name}"):
                ep = mev1alpha1.ModelEndpoint.model_validate(d)
                key = f"{_namespace(ep.metadata)}/{_name(ep.metadata)}"
                if key in seen:
                    continue
                seen.add(key)
                self.total += 1
                if not _endpoint_ready(ep):
                    continue
                self.ready_count += 1
                matched.append(ep)
            # By name, so the weight remainder (the +1s _distribute_weights hands
            # to the first endpoints of a tier) lands the same way every reconcile
            # rather than following the API server's unspecified list order, which
            # would churn the composed weights.
            matched.sort(key=lambda ep: f"{_namespace(ep.metadata)}/{_name(ep.metadata)}")
            priority = entry.priority if entry.priority is not None else 0
            weight = entry.weight if entry.weight is not None else 1
            self.tiers.setdefault(priority, []).append((weight, matched))

    def credential_ready(self, ep: mev1alpha1.ModelEndpoint) -> bool:
        """Whether this endpoint's credential resolved to a usable Secret.

        The endpoint's own EndpointReady is supposed to keep an unusable one out
        of the route, but it's written by another XR on an independent reconcile
        loop. In the window between a Secret being deleted and that XR noticing,
        this function sees a ready endpoint and an unresolved credential. Reading
        the dict unguarded there raises, which fails the whole composition and
        withdraws the route, over one endpoint of possibly many.
        """
        ref = _api_key_secret(ep)
        if ref is None:
            return True
        secret = self.credentials.get(_name(ep.metadata))
        if secret is None:
            return False
        return (ref.key or "apiKey") in secret.get("data", {})

    def resolve_credentials(self) -> bool:
        """Require the Secret behind each ready endpoint's credential.

        The endpoints only become known once their requirements resolve, so
        these are requested on a later pass than the endpoints themselves. Until
        they resolve nothing is composed, because composing a route whose
        backends have no credential would send unauthenticated requests to a
        provider.
        """
        wanted: dict[str, str] = {}
        for entries in self.tiers.values():
            for _, eps in entries:
                for ep in eps:
                    ref = _api_key_secret(ep)
                    if ref:
                        wanted[_name(ep.metadata)] = ref.name
        if not wanted:
            return True

        ns = _namespace(self.xr.metadata)
        for endpoint, secret in sorted(wanted.items()):
            response.require_resources(
                self.rsp,
                name=f"credential-{endpoint}",
                api_version="v1",
                kind="Secret",
                namespace=ns,
                match_name=secret,
            )
        for endpoint in sorted(wanted):
            key = f"credential-{endpoint}"
            if key not in self.req.required_resources:
                self.not_ready(
                    CONDITION_REASON_WAITING_FOR_RESOURCES,
                    "Waiting for endpoint credential Secrets to resolve",
                )
                return False
            found = request.get_required_resources(self.req, key)
            if found:
                self.credentials[endpoint] = found[0]

        return True

    def cluster_ca_ready(self, ep: mev1alpha1.ModelEndpoint) -> bool:
        """Whether this endpoint's cluster has published the CA the backend has
        to pin.

        Only composed endpoints pin one. A cluster publishes the hostname their
        origin is built from only once it has published its CA, so normally both
        are present, but the two come from another XR's status on an independent
        loop and a cluster withdraws its status when its gateway address goes
        away. Composing the backend anyway would reference a ConfigMap nothing
        composes, and Envoy Gateway fails that route closed.
        """
        cluster = _labels(ep.metadata).get(_LABEL_CLUSTER, "")
        if not cluster:
            return True
        return cluster in self.cluster_cas

    def drop_unusable_endpoints(self) -> None:
        """Leave out any endpoint this can't compose a working backend for,
        rather than composing one that can't carry a request.

        That means a credential that didn't resolve to a usable Secret, or a
        cluster that hasn't published the CA the backend pins. An endpoint's own
        EndpointReady says much the same, but it's written by another XR on an
        independent loop, so in the window between a Secret or a cluster status
        going away and that XR noticing, this one sees a ready endpoint and
        neither. Dropping only that endpoint keeps the rest of the route serving;
        raising here would withdraw the route entirely.
        """
        no_credential: list[str] = []
        no_ca: list[str] = []
        for entries in self.tiers.values():
            for _, eps in entries:
                for ep in list(eps):
                    if not self.credential_ready(ep):
                        no_credential.append(_name(ep.metadata))
                    elif not self.cluster_ca_ready(ep):
                        no_ca.append(_name(ep.metadata))
                    else:
                        continue
                    eps.remove(ep)
                    self.ready_count -= 1
        if no_credential:
            response.warning(
                self.rsp,
                "Endpoints left out of the route, their credential Secret missing or missing its key: "
                + ", ".join(sorted(no_credential)),
            )
        if no_ca:
            response.warning(
                self.rsp,
                "Endpoints left out of the route, their cluster has published no gateway CA: "
                + ", ".join(sorted(no_ca)),
            )

    def compose_backends(self) -> None:
        """Per endpoint: how to reach it, what it speaks, and its credential.

        Plus, once per cluster rather than per endpoint, the CA certificate the
        gateway validates that cluster's gateway against.
        """
        clusters: set[str] = set()
        for entries in self.tiers.values():
            for _, eps in entries:
                for ep in eps:
                    self.compose_backend(ep)
                    cluster = _labels(ep.metadata).get(_LABEL_CLUSTER, "")
                    if cluster:
                        clusters.add(cluster)
        for cluster in sorted(clusters):
            self.compose_cluster_ca(cluster)
        # Only a Modelplane endpoint does mTLS to a cluster gateway, so the client
        # certificate is composed only when one is present.
        if clusters:
            self.compose_client_cert()

    def compose_namespace(self) -> None:
        """Compose the namespace this route's objects land in.

        provider-kubernetes doesn't create a target namespace, so this does,
        mirroring the ModelRoute's own. Every route in the namespace composes it
        identically and none deletes it (the management policies omit Delete), so
        one route's removal can't take the namespace from the others, nor leave it
        Terminating.
        """
        obj = _k8s_object(
            self.provider_config,
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": self.namespace, "labels": {_NS_LABEL: _namespace(self.xr.metadata)}},
            },
        )
        obj.spec.managementPolicies = ["Observe", "Create", "Update"]
        resource.update(self.rsp.desired.resources["namespace"], obj)

    def compose_client_cert(self) -> None:
        """Issue the client certificate this namespace's backends present.

        A Modelplane backend does mTLS to a cluster gateway with a certificate
        issued from the gateway's CA. Envoy Gateway reads a Backend's
        clientCertificateRef only from the Backend's own namespace, so it's issued
        here from the gateway's CA ClusterIssuer rather than shared from
        modelplane-system. Like the namespace it's never deleted, so removing one
        route doesn't drop the certificate the namespace's other backends present.
        """
        obj = _k8s_object(
            self.provider_config,
            {
                "apiVersion": "cert-manager.io/v1",
                "kind": "Certificate",
                "metadata": {"name": _CLIENT_CERT_SECRET, "namespace": self.namespace},
                "spec": {
                    "secretName": _CLIENT_CERT_SECRET,
                    "commonName": f"inference-gateway-{self.xr.spec.gatewayName}"[:64],
                    "usages": ["client auth", "digital signature", "key encipherment"],
                    "duration": "2160h",
                    "renewBefore": "720h",
                    "privateKey": {"algorithm": "ECDSA", "size": 256, "rotationPolicy": "Always"},
                    "issuerRef": {"name": _CLIENT_CA_ISSUER, "kind": "ClusterIssuer", "group": "cert-manager.io"},
                },
            },
            ready_when=_CERTIFICATE_READY_CEL,
        )
        obj.spec.managementPolicies = ["Observe", "Create", "Update"]
        resource.update(self.rsp.desired.resources["client-certificate"], obj)

    def compose_cluster_ca(self, cluster: str) -> None:
        """Copy one cluster gateway's CA certificate to the gateway's cluster.

        A ConfigMap because a CA certificate is public, and because Envoy Gateway
        reads a Backend's caCertificateRefs from one. Keyed and named by the
        cluster, so several routes reaching the same cluster converge on identical
        content rather than fighting over it.
        """
        resource.update(
            self.rsp.desired.resources[f"cluster-ca-{cluster}"],
            _k8s_object(
                self.provider_config,
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": resource.child_name("cluster-ca", cluster), "namespace": self.namespace},
                    "data": {"ca.crt": self.cluster_cas[cluster]},
                },
            ),
        )

    def compose_backend(self, ep: mev1alpha1.ModelEndpoint) -> None:
        ep_name = _name(ep.metadata)
        # Named from the route and the endpoint, so each route owns its own
        # backend objects: deleting one route can't withdraw an object another
        # route still serves. Keyed in desired state by the endpoint alone (unique
        # within a route), the composed object by both.
        # NOTE(negz): child_name joins its parts, so two distinct (route,
        # endpoint) pairs could in theory collide within a namespace. Both parts
        # are already hashed names, so it can't happen by accident - only a
        # malicious tenant brute-forcing the 5-char hash could force it, which the
        # soft namespace boundary doesn't try to defend against.
        name = resource.child_name(self.route_name, ep_name)
        scheme, _, host = ep.spec.origin.partition("://")
        hostname, _, port = host.partition(":")
        tls = scheme == "https"
        number = int(port) if port else (443 if tls else 80)

        # Addressed by hostname, never by address. Envoy Gateway emits a single
        # STRICT_DNS cluster for a route whose backends are all hostnames, which
        # is what carries the per-priority localities failover needs. An address
        # makes it an EDS cluster instead, where the per-endpoint metadata
        # naming the chosen backend is never stamped, so the model rewrite, the
        # host rewrite and the credential all silently stop applying while
        # traffic keeps flowing. The ModelEndpoint XRD rejects an address, so
        # this is a hostname.
        spec: dict = {"endpoints": [{"fqdn": {"hostname": hostname, "port": number}}]}
        if tls:
            # A Modelplane-composed endpoint is a cluster gateway, whose
            # certificate is signed by its own cluster's CA, and which requires
            # a client certificate in return.
            #
            # Anything else is a public endpoint, validated against the system
            # trust store.
            cluster = _labels(ep.metadata).get(_LABEL_CLUSTER, "")
            if cluster:
                # drop_unusable_endpoints has already left out any composed
                # endpoint whose cluster hasn't published a CA, so there is one
                # to pin and a ConfigMap composed to hold it.
                spec["tls"] = {
                    "caCertificateRefs": [
                        {"kind": "ConfigMap", "group": "", "name": resource.child_name("cluster-ca", cluster)}
                    ],
                    "sni": hostname,
                    "clientCertificateRef": {"kind": "Secret", "group": "", "name": _CLIENT_CERT_SECRET},
                }
            else:
                spec["tls"] = {"wellKnownCACertificates": "System", "sni": hostname}
        backend: dict = {
            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
            "kind": "Backend",
            "metadata": {"name": name, "namespace": self.namespace},
            "spec": spec,
        }
        resource.update(self.rsp.desired.resources[f"backend-{ep_name}"], _k8s_object(self.provider_config, backend))

        api = ep.spec.api
        schema: dict = {"name": api.schema_ if api and api.schema_ else "OpenAI"}
        prefix = api.prefix if api and api.prefix else "/v1"
        schema["prefix"] = prefix
        service_backend: dict = {
            "apiVersion": "aigateway.envoyproxy.io/v1beta1",
            "kind": "AIServiceBackend",
            "metadata": {"name": name, "namespace": self.namespace},
            "spec": {
                "schema": schema,
                "backendRef": {"group": "gateway.envoyproxy.io", "kind": "Backend", "name": name},
            },
        }
        # A backend Modelplane doesn't operate isn't told which tenant is
        # calling. Our own endpoints keep the header, because the cluster gateway
        # and the engine behind it are ours.
        if not _composed_by_modelplane(ep):
            service_backend["spec"]["headerMutation"] = {"remove": [_CALLER_HEADER]}
        resource.update(
            self.rsp.desired.resources[f"aibackend-{ep_name}"],
            _k8s_object(self.provider_config, service_backend),
        )

        ref = _api_key_secret(ep)
        if ref is None:
            return
        secret = self.credentials.get(ep_name)
        secret_name = resource.child_name(self.route_name, ep_name, "credential")
        key = ref.key or "apiKey"
        # The AI Gateway reads the credential from a fixed key, so a Secret
        # using another name is republished under the expected one rather than
        # forcing the key onto whoever writes the Secret.
        data = secret.get("data", {}) if secret else {}
        resource.update(
            self.rsp.desired.resources[f"credential-{ep_name}"],
            _k8s_object(
                self.provider_config,
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": secret_name, "namespace": self.namespace},
                    "type": "Opaque",
                    "data": {"apiKey": data[key]},
                },
            ),
        )
        # The header a key travels in depends on the API. Anthropic's reads
        # x-api-key, which AnthropicAPIKey sets. APIKey sends a bearer token in
        # Authorization, which OpenAI and the providers compatible with it read.
        if schema["name"] == _SCHEMA_ANTHROPIC:
            auth = {"type": "AnthropicAPIKey", "anthropicAPIKey": {"secretRef": {"name": secret_name}}}
        else:
            auth = {"type": "APIKey", "apiKey": {"secretRef": {"name": secret_name}}}
        resource.update(
            self.rsp.desired.resources[f"credpolicy-{ep_name}"],
            _k8s_object(
                self.provider_config,
                {
                    "apiVersion": "aigateway.envoyproxy.io/v1beta1",
                    "kind": "BackendSecurityPolicy",
                    "metadata": {"name": name, "namespace": self.namespace},
                    "spec": {
                        **auth,
                        "targetRefs": [
                            {
                                "group": "aigateway.envoyproxy.io",
                                "kind": "AIServiceBackend",
                                "name": name,
                            }
                        ],
                    },
                },
            ),
        )

    def compose_route(self) -> None:
        """The AIGatewayRoute matching this service's model name.

        One rule, matching the model header exactly, because only exact matches
        appear in the gateway's /v1/models.

        Every ready endpoint is a backendRef carrying its own weight, priority
        and upstream model name, so the request that wins is translated for
        whichever backend served it.
        """
        # A ModelService's priorities are an ordering, and Envoy's are levels it
        # walks from 0 upwards, so they're renumbered to 0..N-1 over the tiers
        # that actually have a ready endpoint. Passing them through would leave
        # gaps: a user may write 0 and 5, and a tier whose endpoints are all
        # unready drops out entirely, which during a deployment roll can leave a
        # route whose only tier is priority 1 with no priority 0 at all.
        ns = _namespace(self.xr.metadata)
        svc = self.xr.spec.serviceName
        distributed = {p: _distribute_weights(self.tiers[p]) for p in sorted(self.tiers)}
        populated = [p for p in sorted(self.tiers) if distributed[p]]
        refs: list[dict] = []
        for level, priority in enumerate(populated):
            for ep, weight in distributed[priority]:
                ref: dict = {
                    "name": resource.child_name(self.route_name, _name(ep.metadata)),
                    "weight": weight,
                    "priority": level,
                }
                if ep.spec.model:
                    ref["modelNameOverride"] = ep.spec.model
                refs.append(ref)

        resource.update(
            self.rsp.desired.resources["route"],
            _k8s_object(
                self.provider_config,
                {
                    "apiVersion": "aigateway.envoyproxy.io/v1beta1",
                    "kind": "AIGatewayRoute",
                    "metadata": {"name": svc, "namespace": self.namespace},
                    "spec": {
                        "parentRefs": [
                            {
                                "group": "gateway.networking.k8s.io",
                                "kind": "Gateway",
                                "name": _GATEWAY_NAME,
                                "namespace": _GATEWAY_NAMESPACE,
                                "sectionName": self.inference_listener,
                            }
                        ],
                        "rules": [
                            {
                                "matches": [
                                    {
                                        "headers": [
                                            {
                                                "type": "Exact",
                                                "name": _MODEL_HEADER,
                                                "value": f"{ns}/{svc}",
                                            }
                                        ]
                                    }
                                ],
                                "backendRefs": refs,
                                # AI Gateway defaults an unset request timeout to
                                # 60s, so it's always set. streamIdleTimeout
                                # becomes Envoy's per-attempt idle timeout, which
                                # resets a silent backend and retries the request.
                                "timeouts": {"request": self.xr.spec.timeouts.request},
                                "streamIdleTimeout": self.xr.spec.timeouts.idle,
                                # /v1/models reports this as each model's owner,
                                # which otherwise reads "Envoy AI Gateway".
                                "modelsOwnedBy": ns,
                            }
                        ],
                        "llmRequestCosts": _LLM_REQUEST_COSTS,
                    },
                },
                # Readiness tracks the route being accepted, not merely written.
                # A route Envoy AI Gateway rejects, for a missing AIServiceBackend
                # or a rule it won't take, would otherwise leave the route
                # reporting RoutingReady while no caller can reach it.
                ready_when=_ROUTE_ACCEPTED_CEL,
            ),
        )

    def write_status(self) -> None:
        """Publish the model callers name, the gateway's address, and counts."""
        status = v1alpha1.Status(
            model=f"{_namespace(self.xr.metadata)}/{self.xr.spec.serviceName}",
            endpoints=v1alpha1.Endpoints(total=self.total, ready=self.ready_count),
        )
        if self.address:
            status.address = self.address
        resource.update_status(self.rsp.desired.composite, status)

    def not_ready(self, reason: str, message: str) -> None:
        # Mark the composite not-ready explicitly. A pass that composes nothing,
        # or whose route hasn't been accepted, would otherwise aggregate to a
        # trivially-ready XR, and compose-model-service reads this ModelRoute's
        # Ready to decide the service is serving.
        self.rsp.desired.composite.ready = fnv1.READY_FALSE
        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_ROUTING_READY,
                status="False",
                reason=reason,
                message=message,
            ),
        )
        response.normal(self.rsp, message)

    def derive_conditions(self) -> None:
        """RoutingReady once the composed route has been accepted."""
        if resource.get_condition(self.req.observed.resources.get("route"), "Ready").status != "True":
            self.not_ready(
                CONDITION_REASON_WAITING_FOR_ROUTE,
                f"Waiting for the route on gateway {self.xr.spec.gatewayName} to be accepted",
            )
            return
        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_ROUTING_READY,
                status="True",
                reason=CONDITION_REASON_ROUTE_ACCEPTED,
            ),
        )
