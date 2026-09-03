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

"""Compose a ModelService's route and backends on every gateway serving it.

A ModelService is one model as a caller sees it: a name that resolves to
whichever of its ModelEndpoints should serve the next request. This function
turns that into an AIGatewayRoute per gateway, plus, per endpoint, the objects
that gateway needs in order to reach it and translate the request for it.

Which gateways serve a service is the gateway's choice, not the service's: an
InferenceGateway's serviceSelector matches the service's labels, and an absent
selector matches every service. So this function reads every InferenceGateway
and works out which of them select it, rather than the service naming gateways.
That is what makes residency fall out of labels instead of needing a feature:
label a service for a region and only that region's gateways serve it.

The objects are composed per service rather than per endpoint. Two ModelServices
selecting one endpoint each compose their own copies, which costs some
duplicated config and avoids two composites owning one object. It also keeps a
credential from being propagated to a gateway that serves neither service.
"""

import math

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.inferencegateway import v1alpha1 as igv1alpha1
from models.ai.modelplane.modelendpoint import v1alpha1 as mev1alpha1
from models.ai.modelplane.modelservice import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

from function import names

# Condition types this function sets on the ModelService.
CONDITION_TYPE_ROUTING_READY = "RoutingReady"

CONDITION_REASON_ROUTES_ACCEPTED = "RoutesAccepted"
CONDITION_REASON_WAITING_FOR_RESOURCES = "WaitingForResources"
CONDITION_REASON_NO_GATEWAY = "NoGatewayServesThisService"
CONDITION_REASON_NO_ENDPOINTS = "NoReadyEndpoints"
CONDITION_REASON_WAITING_FOR_ROUTES = "WaitingForRoutes"

# The namespace on a gateway's cluster that every composed object lands in. The
# ServingStack already creates it there.
REMOTE_NAMESPACE = "modelplane-system"

# The Gateway compose-inference-gateway composes on each gateway's cluster.
_GATEWAY_NAME = "fleet-gateway"

# The header the AI Gateway's ext-proc puts the request body's model into,
# before the routing decision, so a route can match on it.
_MODEL_HEADER = "x-ai-eg-model"

# The header the fleet gateway stamps the caller's identity onto. Removed again
# for a backend Modelplane doesn't operate, so a third-party provider isn't told
# which tenant is calling. The usage record reads the caller from request
# metadata, which this doesn't disturb.
_CALLER_HEADER = "x-modelplane-caller"

# The Secret compose-inference-gateway has cert-manager issue for the fleet
# gateway's client certificate, in the same namespace on the same cluster.
_CLIENT_CERT_SECRET = "fleet-gateway-client"

# Envoy AI Gateway's per-backendRef weight limit, inherited from Gateway API.
_MAX_WEIGHT = 1000000

# An AIGatewayRoute reports acceptance as a top-level condition.
_ROUTE_ACCEPTED_CEL = (
    "has(object.status) && has(object.status.conditions) && "
    "object.status.conditions.exists(c, c.type == 'Accepted' && c.status == 'True')"
)

# How long the gateway waits for a whole response, and for the first byte of
# one. streamIdleTimeout is what lets a backend that hangs before the first
# token reset and fall over to the next priority; past the first byte the
# tokens are already sent, so it truncates instead.
_REQUEST_TIMEOUT = "300s"
_STREAM_IDLE_TIMEOUT = "60s"

# Token counts to capture per request. Declaring them is also what makes the
# gateway ask a backend for usage on a streamed response, which otherwise
# reports none at all.
_LLM_REQUEST_COSTS = [
    {"metadataKey": "llm_input_token", "type": "InputToken"},
    {"metadataKey": "llm_output_token", "type": "OutputToken"},
    {"metadataKey": "llm_total_token", "type": "TotalToken"},
]


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


# Set by compose-model-deployment on every ModelEndpoint it composes, naming
# the cluster the replica landed on. Its presence is what marks an endpoint as
# one Modelplane operates.
_LABEL_CLUSTER = "modelplane.ai/cluster"


def _composed_by_modelplane(ep: mev1alpha1.ModelEndpoint) -> bool:
    """Whether Modelplane composed this endpoint, and so operates it.

    Decided by the cluster label compose-model-deployment stamps on a composed
    endpoint. A hand-written endpoint carrying it is claiming to be ours, and
    will be treated as ours.
    """
    return _LABEL_CLUSTER in _labels(ep.metadata)


def _endpoint_ready(d: dict) -> bool:
    """Whether a ModelEndpoint reports EndpointReady=True.

    An endpoint that doesn't is left out of the route, so a missing credential
    keeps traffic away rather than failing the requests that reach it.
    """
    for c in d.get("status", {}).get("conditions", []):
        if c.get("type") == "EndpointReady":
            return c.get("status") == "True"
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

    # An extreme ratio can still exceed the limit once reduced. Rescale so the
    # largest weight lands on it, keeping every endpoint at 1 or more. Trades a
    # little precision for a valid route, in a case no realistic config reaches.
    return [(ep, max(1, round(w / divisor / highest * _MAX_WEIGHT))) for ep, w in weighted]


def _wrap(provider_config: str, manifest: dict, *, cel_query: str | None = None) -> k8sobjv1alpha1.Object:
    """Wrap a manifest in a provider-kubernetes Object for a gateway's cluster."""
    readiness = (
        k8sobjv1alpha1.Readiness(policy="DeriveFromCelQuery", celQuery=cel_query)
        if cel_query is not None
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


class ServingGateway:
    """An InferenceGateway serving this service, and how to reach its cluster."""

    def __init__(self, xr: igv1alpha1.InferenceGateway, provider_config: str) -> None:
        self.xr = xr
        self.name = _name(xr.metadata)
        self.provider_config = provider_config

    def serves(self, labels: dict[str, str]) -> bool:
        """Whether this gateway's serviceSelector matches a service's labels.

        An absent selector serves every service, which is the default and what
        a single-gateway Modelplane wants.
        """
        sel = self.xr.spec.serviceSelector
        if sel is None:
            return True
        return all(labels.get(k) == v for k, v in sel.matchLabels.items())


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
        self.xr = v1alpha1.ModelService(**resource.struct_to_dict(req.observed.composite.resource))
        self.ns = _namespace(self.xr.metadata)
        self.svc = _name(self.xr.metadata)
        self.gateways: list[ServingGateway] = []
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
        self.compose_routes()
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
        """Resolve the gateways serving this service and the endpoints behind it.

        Returns False, having set conditions, when there's nothing to compose.
        """
        response.require_resources(
            self.rsp,
            name="gateways",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceGateway",
        )
        response.require_resources(
            self.rsp,
            name="clusters",
            api_version="modelplane.ai/v1alpha1",
            kind="InferenceCluster",
        )
        for i, entry in enumerate(self.xr.spec.endpoints):
            response.require_resources(
                self.rsp,
                name=f"endpoints-{i}",
                api_version="modelplane.ai/v1alpha1",
                kind="ModelEndpoint",
                namespace=self.ns,
                match_labels=dict(entry.selector.matchLabels),
            )

        keys = ["gateways", "clusters"] + [f"endpoints-{i}" for i in range(len(self.xr.spec.endpoints))]
        if any(k not in self.req.required_resources for k in keys):
            self.not_ready(CONDITION_REASON_WAITING_FOR_RESOURCES, "Waiting for gateways and endpoints to resolve")
            return False

        self.resolve_gateways()
        self.resolve_endpoints()

        if not self.gateways:
            self.not_ready(
                CONDITION_REASON_NO_GATEWAY,
                "No InferenceGateway's serviceSelector matches this service's labels, so no caller can reach it",
            )
            return False
        if not self.resolve_credentials():
            return False

        # After resolving, not inside it: an endpoint with no credentialRef needs
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

    def resolve_gateways(self) -> None:
        """The gateways whose serviceSelector matches this service's labels, and
        which have a cluster we can compose onto."""
        pcs: dict[str, str] = {}
        for c in request.get_required_resources(self.req, "clusters"):
            cluster = icv1alpha1.InferenceCluster.model_validate(c)
            if cluster.status and cluster.status.providerConfigRef and cluster.status.providerConfigRef.name:
                pcs[_name(cluster.metadata)] = cluster.status.providerConfigRef.name
            if cluster.status and cluster.status.gateway and cluster.status.gateway.caCertificate:
                self.cluster_cas[_name(cluster.metadata)] = cluster.status.gateway.caCertificate

        labels = _labels(self.xr.metadata)
        for g in request.get_required_resources(self.req, "gateways"):
            gw = igv1alpha1.InferenceGateway.model_validate(g)
            pc = pcs.get(gw.spec.clusterName)
            if pc is None:
                # The gateway's cluster hasn't published a ProviderConfig yet.
                # compose-inference-gateway reports that on the gateway; there's
                # nothing useful this service can add.
                continue
            if not (gw.status and gw.status.clientCACertificate):
                # Its client PKI hasn't issued. Every composed endpoint's backend
                # names this gateway's client certificate Secret, which is issued
                # from the same CA and so doesn't exist on its cluster yet, and
                # Envoy Gateway fails a backend closed when the Secret naming its
                # certificate is missing. A cluster becomes schedulable once any
                # gateway has published, so this one can be behind.
                continue
            candidate = ServingGateway(gw, pc)
            if candidate.serves(labels):
                self.gateways.append(candidate)
        self.gateways.sort(key=lambda g: g.name)

    def resolve_endpoints(self) -> None:
        """Group ready endpoints by the priority of the entry that selected them.

        An endpoint matched by more than one entry belongs to the first that
        matched it, so a canary entry and a catch-all entry can't both weight
        the same endpoint.
        """
        seen: set[str] = set()
        for i, entry in enumerate(self.xr.spec.endpoints):
            matched: list[mev1alpha1.ModelEndpoint] = []
            for d in request.get_required_resources(self.req, f"endpoints-{i}"):
                ep = mev1alpha1.ModelEndpoint.model_validate(d)
                key = f"{_namespace(ep.metadata)}/{_name(ep.metadata)}"
                if key in seen:
                    continue
                seen.add(key)
                self.total += 1
                if not _endpoint_ready(d):
                    continue
                self.ready_count += 1
                matched.append(ep)
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
        withdraws the route from every gateway serving the service, over one
        endpoint of possibly many.
        """
        ref = ep.spec.credentialRef
        if ref is None:
            return True
        secret = self.credentials.get(_name(ep.metadata))
        if secret is None:
            return False
        return (ref.key or "apiKey") in secret.get("data", {})

    def resolve_credentials(self) -> bool:
        """Require the Secret behind each ready endpoint's credentialRef.

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
                    if ep.spec.credentialRef:
                        wanted[_name(ep.metadata)] = ep.spec.credentialRef.name
        if not wanted:
            return True

        for endpoint, secret in sorted(wanted.items()):
            response.require_resources(
                self.rsp,
                name=f"credential-{endpoint}",
                api_version="v1",
                kind="Secret",
                namespace=self.ns,
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
        neither. Dropping only that endpoint keeps the rest of the service
        serving; raising here would withdraw the route from every gateway.
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

    def compose_routes(self) -> None:
        """One route per gateway, plus each gateway's copy of the backends."""
        for gw in self.gateways:
            self.compose_backends(gw)
            self.compose_route(gw)

    def compose_backends(self, gw: ServingGateway) -> None:
        """Per endpoint: how to reach it, what it speaks, and its credential.

        Plus, once per cluster rather than per endpoint, the CA certificate the
        gateway validates that cluster's gateway against.
        """
        clusters: set[str] = set()
        for entries in self.tiers.values():
            for _, eps in entries:
                for ep in eps:
                    self.compose_backend(gw, ep)
                    cluster = _labels(ep.metadata).get(_LABEL_CLUSTER, "")
                    if cluster:
                        clusters.add(cluster)
        for cluster in sorted(clusters):
            self.compose_cluster_ca(gw, cluster)

    def compose_cluster_ca(self, gw: ServingGateway, cluster: str) -> None:
        """Copy one cluster gateway's CA certificate to a gateway's cluster.

        A ConfigMap because a CA certificate is public, and because Envoy Gateway
        reads a Backend's caCertificateRefs from one. Keyed and named by the
        cluster, so several ModelServices reaching the same cluster converge on
        identical content rather than fighting over it.
        """
        resource.update(
            self.rsp.desired.resources[f"cluster-ca-{gw.name}-{cluster}"],
            _wrap(
                gw.provider_config,
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": names.cluster_ca(cluster), "namespace": REMOTE_NAMESPACE},
                    "data": {"ca.crt": self.cluster_cas[cluster]},
                },
            ),
        )

    def compose_backend(self, gw: ServingGateway, ep: mev1alpha1.ModelEndpoint) -> None:
        ep_name = _name(ep.metadata)
        name = names.backend(self.ns, self.svc, ep_name)
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
            # certificate is signed by its own cluster's CA rather than a public
            # one, and which requires a client certificate in return. That pair
            # is what makes a fleet gateway the only thing able to reach the
            # engines behind it.
            #
            # Anything else is a public endpoint, validated against the system
            # trust store. Presenting a client certificate to a provider would
            # be meaningless, and pinning our own CA would reject them.
            cluster = _labels(ep.metadata).get(_LABEL_CLUSTER, "")
            if cluster:
                # drop_unusable_endpoints has already left out any composed
                # endpoint whose cluster hasn't published a CA, so there is one
                # to pin and a ConfigMap composed to hold it. Falling back to the
                # public trust store here instead would leave the backend unable
                # to complete a handshake, presenting no client certificate to a
                # gateway that requires one, while the endpoint and the route
                # both reported ready.
                spec["tls"] = {
                    "caCertificateRefs": [{"kind": "ConfigMap", "group": "", "name": names.cluster_ca(cluster)}],
                    "sni": hostname,
                    "clientCertificateRef": {"kind": "Secret", "group": "", "name": _CLIENT_CERT_SECRET},
                }
            else:
                spec["tls"] = {"wellKnownCACertificates": "System", "sni": hostname}
        backend: dict = {
            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
            "kind": "Backend",
            "metadata": {"name": name, "namespace": REMOTE_NAMESPACE},
            "spec": spec,
        }
        resource.update(self.rsp.desired.resources[f"backend-{gw.name}-{ep_name}"], _wrap(gw.provider_config, backend))

        api = ep.spec.api
        schema: dict = {"name": api.schema_ if api and api.schema_ else "OpenAI"}
        prefix = api.prefix if api and api.prefix else "/v1"
        schema["prefix"] = prefix
        service_backend: dict = {
            "apiVersion": "aigateway.envoyproxy.io/v1beta1",
            "kind": "AIServiceBackend",
            "metadata": {"name": name, "namespace": REMOTE_NAMESPACE},
            "spec": {
                "schema": schema,
                "backendRef": {"group": "gateway.envoyproxy.io", "kind": "Backend", "name": name},
            },
        }
        # A backend Modelplane doesn't operate isn't told which tenant is
        # calling. Our own endpoints keep the header, because the cluster gateway
        # and the engine behind it are ours.
        #
        # Whether we operate it is decided by whether we composed it, not by
        # whether it carries a credential: a third party can need no key, or
        # authenticate by client certificate, and inferring from credentialRef
        # would disclose the caller to it.
        if not _composed_by_modelplane(ep):
            service_backend["spec"]["headerMutation"] = {"remove": [_CALLER_HEADER]}
        resource.update(
            self.rsp.desired.resources[f"aibackend-{gw.name}-{ep_name}"],
            _wrap(gw.provider_config, service_backend),
        )

        if not ep.spec.credentialRef:
            return
        secret = self.credentials.get(ep_name)
        secret_name = names.credential(self.ns, self.svc, ep_name)
        key = ep.spec.credentialRef.key or "apiKey"
        # The AI Gateway reads the credential from a fixed key, so a Secret
        # using another name is republished under the expected one rather than
        # forcing the key onto whoever writes the Secret.
        data = secret.get("data", {}) if secret else {}
        resource.update(
            self.rsp.desired.resources[f"credential-{gw.name}-{ep_name}"],
            _wrap(
                gw.provider_config,
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": secret_name, "namespace": REMOTE_NAMESPACE},
                    "type": "Opaque",
                    "data": {"apiKey": data[key]},
                },
            ),
        )
        resource.update(
            self.rsp.desired.resources[f"credpolicy-{gw.name}-{ep_name}"],
            _wrap(
                gw.provider_config,
                {
                    "apiVersion": "aigateway.envoyproxy.io/v1beta1",
                    "kind": "BackendSecurityPolicy",
                    "metadata": {"name": name, "namespace": REMOTE_NAMESPACE},
                    "spec": {
                        "type": "APIKey",
                        "apiKey": {"secretRef": {"name": secret_name}},
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

    def compose_route(self, gw: ServingGateway) -> None:
        """The AIGatewayRoute matching this service's model name.

        One rule, matching the model header exactly. Exact rather than a regex
        because only exact matches appear in the gateway's /v1/models, and a
        service a caller can't discover is a service they can't use.

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
        populated = [p for p in sorted(self.tiers) if _distribute_weights(self.tiers[p])]
        refs: list[dict] = []
        for level, priority in enumerate(populated):
            for ep, weight in _distribute_weights(self.tiers[priority]):
                ref: dict = {
                    "name": names.backend(self.ns, self.svc, _name(ep.metadata)),
                    "weight": weight,
                    "priority": level,
                }
                if ep.spec.model:
                    ref["modelNameOverride"] = ep.spec.model
                refs.append(ref)

        resource.update(
            self.rsp.desired.resources[f"route-{gw.name}"],
            _wrap(
                gw.provider_config,
                {
                    "apiVersion": "aigateway.envoyproxy.io/v1beta1",
                    "kind": "AIGatewayRoute",
                    "metadata": {"name": names.route(self.ns, self.svc), "namespace": REMOTE_NAMESPACE},
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
                                "matches": [
                                    {
                                        "headers": [
                                            {
                                                "type": "Exact",
                                                "name": _MODEL_HEADER,
                                                "value": names.model(self.ns, self.svc),
                                            }
                                        ]
                                    }
                                ],
                                "backendRefs": refs,
                                "timeouts": {"request": _REQUEST_TIMEOUT},
                                "streamIdleTimeout": _STREAM_IDLE_TIMEOUT,
                            }
                        ],
                        "llmRequestCosts": _LLM_REQUEST_COSTS,
                    },
                },
                # Readiness tracks the route being accepted, not merely written.
                # A route Envoy AI Gateway rejects, for a missing
                # AIServiceBackend or a rule it won't take, would otherwise leave
                # the service reporting RoutingReady while no caller can reach it.
                cel_query=_ROUTE_ACCEPTED_CEL,
            ),
        )

    def write_status(self) -> None:
        """Publish the model callers name, the gateways serving it, and counts."""
        status = v1alpha1.Status(
            model=names.model(self.ns, self.svc),
            endpoints=v1alpha1.Endpoints(total=self.total, ready=self.ready_count),
        )
        served = []
        for gw in self.gateways:
            entry = v1alpha1.Gateway(name=gw.name)
            if gw.xr.spec.hostname:
                entry.hostname = gw.xr.spec.hostname
            if gw.xr.status and gw.xr.status.address:
                entry.address = gw.xr.status.address
            served.append(entry)
        if served:
            status.gateways = served
        resource.update_status(self.rsp.desired.composite, status)

    def not_ready(self, reason: str, message: str) -> None:
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
        """RoutingReady once every composed route has been applied.

        Every gateway, not any: a service reachable through some of the gateways
        that should serve it is a residency or capacity problem worth surfacing,
        not a success.
        """
        pending = [
            gw.name
            for gw in self.gateways
            if resource.get_condition(self.req.observed.resources.get(f"route-{gw.name}"), "Ready").status != "True"
        ]
        if pending:
            self.not_ready(
                CONDITION_REASON_WAITING_FOR_ROUTES,
                f"Waiting for routes on gateways: {', '.join(pending)}",
            )
            return
        response.set_conditions(
            self.rsp,
            resource.Condition(
                typ=CONDITION_TYPE_ROUTING_READY,
                status="True",
                reason=CONDITION_REASON_ROUTES_ACCEPTED,
            ),
        )
