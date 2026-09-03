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

"""Tests for the compose-inference-gateway function."""

import base64
import dataclasses
import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.inferencegateway import v1alpha1

_PC = "gw-eu-cluster-kubeconfig"
_CLUSTER = "gw-eu"
_ADDRESS = "34.56.129.3"


@dataclasses.dataclass
class Case:
    """A test case for compose-inference-gateway."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _xr(**spec) -> dict:  # noqa: ANN003
    """The InferenceGateway XR, built from the generated model so a field the
    XRD doesn't define can't creep into a test."""
    xr = v1alpha1.InferenceGateway(
        apiVersion="modelplane.ai/v1alpha1",
        kind="InferenceGateway",
        metadata={"name": "eu"},
        spec=v1alpha1.Spec(clusterName=_CLUSTER, **spec),
    )
    return xr.model_dump(exclude_none=True, mode="json", by_alias=True)


def _cluster(*, provider_config: str | None = _PC) -> dict:
    """An observed InferenceCluster, optionally without a providerConfigRef.

    A registered cluster with no GPU pools, which is what a region with callers
    but no accelerators looks like, and the least a gateway needs.
    """
    status: dict = {}
    if provider_config:
        status["providerConfigRef"] = {"name": provider_config}
    return {
        "apiVersion": "modelplane.ai/v1alpha1",
        "kind": "InferenceCluster",
        "metadata": {"name": _CLUSTER},
        "spec": {
            "cluster": {
                "source": "Existing",
                "existing": {"secretRef": {"name": f"{_CLUSTER}-kubeconfig", "key": "kubeconfig"}},
            }
        },
        "status": status,
    }


def _cluster_with_gateway(name: str, *, address: str, hostname: str) -> dict:
    """An observed InferenceCluster whose gateway has published an address and
    the internal name Modelplane derived for it."""
    return {
        "apiVersion": "modelplane.ai/v1alpha1",
        "kind": "InferenceCluster",
        "metadata": {"name": name},
        "spec": {
            "cluster": {
                "source": "Existing",
                "existing": {"secretRef": {"name": f"{name}-kubeconfig", "key": "kubeconfig"}},
            }
        },
        "status": {"gateway": {"address": address, "hostname": hostname}},
    }


def _gateway_xr(name: str, cluster: str) -> dict:
    """Another InferenceGateway, for the one-per-cluster contest."""
    return {
        "apiVersion": "modelplane.ai/v1alpha1",
        "kind": "InferenceGateway",
        "metadata": {"name": name},
        "spec": {"clusterName": cluster},
    }


def _secret(name: str, data: dict[str, str]) -> dict:
    """A control-plane Secret, with values base64 encoded as the API server
    stores them, since the function copies data verbatim."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": fn.CONTROL_PLANE_NAMESPACE},
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()},
    }


def _required(**resources) -> dict:  # noqa: ANN003
    """Build the request's required_resources map."""
    return {
        name: fnv1.Resources(items=[fnv1.Resource(resource=resource.dict_to_struct(r)) for r in items])
        for name, items in resources.items()
    }


def _requirements(*, auth: bool = False, tls: int = 0) -> fnv1.Requirements:
    """The requirements the function always emits, in the order it emits them."""
    reqs = {
        "cluster": fnv1.ResourceSelector(
            api_version="modelplane.ai/v1alpha1", kind="InferenceCluster", match_name=_CLUSTER
        ),
        "gateways": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"),
        "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
    }
    if auth:
        reqs["caller-secrets"] = fnv1.ResourceSelector(
            api_version="v1",
            kind="Secret",
            namespace=fn.CONTROL_PLANE_NAMESPACE,
            match_labels=fnv1.MatchLabels(labels={"modelplane.ai/inference-keys": "true"}),
        )
    for i in range(tls):
        reqs[f"tls-secret-{i}"] = fnv1.ResourceSelector(
            api_version="v1", kind="Secret", namespace=fn.CONTROL_PLANE_NAMESPACE, match_name=f"eu-tls-{i}"
        )
    return fnv1.Requirements(resources=reqs)


def _observed_gateway(address: str | None, *, ready: bool) -> fnv1.Resource:
    """The composed Gateway Object as observed, optionally with an address.

    lastTransitionTime is fixed so the input is deterministic.
    """
    manifest: dict = {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "Gateway",
        "metadata": {"name": fn._GATEWAY_NAME, "namespace": fn.REMOTE_NAMESPACE},
    }
    if address:
        manifest["status"] = {"addresses": [{"type": "IPAddress", "value": address}]}
    status: dict = {"atProvider": {"manifest": manifest}}
    if ready:
        status["conditions"] = [
            {
                "type": "Ready",
                "status": "True",
                "reason": "Available",
                "lastTransitionTime": "2026-06-08T00:00:00Z",
            }
        ]
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "status": status,
            }
        )
    )


def _observed_accepted() -> fnv1.Resource:
    """A composed policy Object as observed once accepted.

    Its readiness comes from a CEL query on the policy's own Accepted condition,
    so an Object that merely applied isn't enough.
    """
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "status": {
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "True",
                            "reason": "Available",
                            "lastTransitionTime": "2026-06-08T00:00:00Z",
                        }
                    ]
                },
            }
        )
    )


def _not_ready(reason: str, message: str, requirements: fnv1.Requirements) -> fnv1.RunFunctionResponse:
    """The whole response for a pass that composes nothing: no desired
    resources, one GatewayReady=False condition, and the reason as a result."""
    return fnv1.RunFunctionResponse(
        meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
        desired=fnv1.State(),
        context=structpb.Struct(),
        requirements=requirements,
        conditions=[
            fnv1.Condition(
                type=fn.CONDITION_TYPE_GATEWAY_READY,
                status=fnv1.STATUS_CONDITION_FALSE,
                reason=reason,
                message=message,
            )
        ],
        results=[fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message=message)],
    )


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_gates(self) -> None:
        """Passes where the gateway can't be composed compose nothing, and say
        why. Asserting the whole response proves nothing is composed against a
        cluster we can't reach, rather than a subset being applied."""
        cases = [
            Case(
                name="unresolved requirements compose nothing",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_xr()))),
                ),
                want=_not_ready(
                    fn.CONDITION_REASON_WAITING_FOR_CLUSTER,
                    "Waiting for the gateway's cluster and the other gateways to resolve",
                    _requirements(),
                ),
            ),
            Case(
                name="a named cluster that does not exist",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_xr()))),
                    required_resources=_required(cluster=[], gateways=[_gateway_xr("eu", _CLUSTER)]),
                ),
                want=_not_ready(
                    fn.CONDITION_REASON_WAITING_FOR_CLUSTER,
                    f"InferenceCluster {_CLUSTER} does not exist",
                    _requirements(),
                ),
            ),
            Case(
                name="a cluster that already hosts a lower-named gateway",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_xr()))),
                    required_resources=_required(
                        cluster=[_cluster()],
                        gateways=[_gateway_xr("eu", _CLUSTER), _gateway_xr("aaa", _CLUSTER)],
                    ),
                ),
                want=_not_ready(
                    fn.CONDITION_REASON_CLUSTER_TAKEN,
                    f"InferenceCluster {_CLUSTER} already hosts InferenceGateway aaa",
                    _requirements(),
                ),
            ),
            Case(
                name="a cluster with no providerConfigRef yet",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_xr()))),
                    required_resources=_required(
                        cluster=[_cluster(provider_config=None)], gateways=[_gateway_xr("eu", _CLUSTER)]
                    ),
                ),
                want=_not_ready(
                    fn.CONDITION_REASON_WAITING_FOR_CLUSTER,
                    f"InferenceCluster {_CLUSTER} has not published a providerConfigRef",
                    _requirements(),
                ),
            ),
            Case(
                name="auth selecting no Secret would authenticate nobody",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                _xr(
                                    auth=v1alpha1.Auth(
                                        secretSelector=v1alpha1.SecretSelector(
                                            matchLabels={"modelplane.ai/inference-keys": "true"}
                                        )
                                    )
                                )
                            )
                        )
                    ),
                    required_resources=_required(
                        cluster=[_cluster()], gateways=[_gateway_xr("eu", _CLUSTER)], **{"caller-secrets": []}
                    ),
                ),
                want=_not_ready(
                    fn.CONDITION_REASON_SECRETS_MISSING,
                    "spec.auth.secretSelector matches no Secret, so no caller could authenticate",
                    _requirements(auth=True),
                ),
            ),
        ]
        for case in cases:
            with self.subTest(case.name):
                got = await self.runner.RunFunction(case.req, None)
                self.assertEqual(
                    json_format.MessageToDict(case.want),
                    json_format.MessageToDict(got),
                    "-want, +got",
                )

    async def test_minimal_gateway(self) -> None:
        """A gateway with no hostname, TLS or auth: the getting-started shape.

        Composes the gateway objects and no auth policies, and reports no
        endpoints until the Gateway has an address.
        """
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_xr()))),
            required_resources=_required(cluster=[_cluster()], gateways=[_gateway_xr("eu", _CLUSTER)]),
        )
        got = await self.runner.RunFunction(req, None)

        self.assertEqual(
            sorted(got.desired.resources),
            sorted(
                [
                    # The client certificate this gateway presents to a cluster
                    # gateway, which refuses a request that arrives without one.
                    "client-ca-certificate",
                    "client-ca-issuer",
                    "client-ca-bundle",
                    "client-ca-configmap",
                    "client-certificate",
                    "client-selfsigned-issuer",
                    "envoy-proxy",
                    "failover-policy",
                    "gateway",
                    "healthz-filter",
                    "healthz-route",
                ]
            ),
            "composes the gateway objects and its client PKI, and no caller auth",
        )
        for key, res in got.desired.resources.items():
            d = resource.struct_to_dict(res.resource)
            self.assertEqual(d["kind"], "Object", f"{key} targets the gateway's cluster")
            self.assertEqual(
                d["spec"]["providerConfigRef"],
                {"kind": "ClusterProviderConfig", "name": _PC},
                f"{key} uses the cluster's ClusterProviderConfig",
            )
            # An InferenceGateway is cluster-scoped, and Crossplane only
            # defaults a composed namespaced resource's namespace from a
            # namespaced composite. Without this every reconcile fails with
            # "an empty namespace may not be set when a resource name is
            # provided" and nothing is composed at all.
            self.assertEqual(
                d["metadata"]["namespace"],
                fn.CONTROL_PLANE_NAMESPACE,
                f"{key} sets its own namespace, which a cluster-scoped XR must",
            )
            manifest = d["spec"]["forProvider"]["manifest"]
            if manifest["kind"] == "Bundle":
                # A Bundle is cluster-scoped, so it has no namespace of its own.
                # It picks the namespace it syncs its ConfigMap to by selector.
                self.assertNotIn(
                    "namespace",
                    manifest["metadata"],
                    f"{key} is cluster-scoped, so it sets no namespace",
                )
                self.assertEqual(
                    manifest["spec"]["target"]["namespaceSelector"],
                    {"matchLabels": {"kubernetes.io/metadata.name": fn.REMOTE_NAMESPACE}},
                    f"{key} syncs only to the remote namespace",
                )
                continue
            self.assertEqual(
                manifest["metadata"]["namespace"],
                fn.REMOTE_NAMESPACE,
                f"{key} lands in the remote namespace",
            )

        # numAttemptsPerPriority is what installs Envoy's previous_priorities
        # retry predicate. Without it a ModelService's priorities are stamped on
        # the endpoints and ignored, so every endpoint shares traffic and
        # failover never happens. Nothing in status would show it.
        failover = resource.struct_to_dict(got.desired.resources["failover-policy"].resource)
        self.assertEqual(
            failover["spec"]["forProvider"]["manifest"]["spec"]["retry"],
            {
                "numAttemptsPerPriority": 1,
                "numRetries": 3,
                "retryOn": {
                    # retriable-status-codes has to be present for the status
                    # code below to do anything: Envoy Gateway replaces retry_on
                    # wholesale with this list, and Envoy only consults
                    # retriable_status_codes when retry_on names it. Without it a
                    # provider answering 503 is never retried, which is the case
                    # failover exists for.
                    "triggers": [
                        "connect-failure",
                        "refused-stream",
                        "reset",
                        "retriable-status-codes",
                    ],
                    "httpStatusCodes": [503],
                },
            },
        )
        # Panic mode defaults to 50%, above which Envoy ignores health and
        # spreads traffic over every endpoint including the ejected ones. Every
        # endpoint of a ModelService shares one cluster, so ejecting a whole
        # priority tier usually crosses it and failover stops working.
        #
        # Asserted on the whole healthCheck, because panicThreshold is a sibling
        # of passive rather than a field inside it, and nested wrongly the API
        # server prunes it while the policy still applies. Reaching for it at a
        # path that doesn't exist is how the wrong nesting survived review.
        self.assertEqual(
            failover["spec"]["forProvider"]["manifest"]["spec"]["healthCheck"],
            {
                "passive": {
                    "baseEjectionTime": "30s",
                    "consecutive5XxErrors": 5,
                    "interval": "5s",
                    "maxEjectionPercent": 100,
                },
                "panicThreshold": 0,
            },
        )
        self.assertEqual(
            failover["spec"]["forProvider"]["manifest"]["spec"]["targetRefs"],
            [{"group": "gateway.networking.k8s.io", "kind": "Gateway", "name": fn._GATEWAY_NAME}],
            "targets the Gateway, so it covers every ModelService's route",
        )

        # The token fields must read request metadata, not the response body or
        # a header. The caller header is stripped before a third-party backend
        # sees it, so a log reading the header loses the caller on exactly the
        # records that attribute provider spend.
        log = resource.struct_to_dict(got.desired.resources["envoy-proxy"].resource)
        fields = log["spec"]["forProvider"]["manifest"]["spec"]["telemetry"]["accessLog"]["settings"][0]["format"][
            "json"
        ]
        # Without ndots:1 every backend hostname is resolved against each of the
        # pod's search domains first, since they all have fewer than five dots.
        # A cluster whose upstream resolver is slow then stalls resolution, and
        # Envoy answers 503 with nothing but DNS timeouts to show for it.
        self.assertEqual(
            log["spec"]["forProvider"]["manifest"]["spec"]["provider"]["kubernetes"]["envoyDeployment"]["patch"],
            {"type": "StrategicMerge", "value": fn._NDOTS_PATCH},
        )
        self.assertEqual(
            fn._NDOTS_PATCH["spec"]["template"]["spec"]["dnsConfig"]["options"],
            [{"name": "ndots", "value": "1"}],
        )

        self.assertEqual(fields["caller"], "%DYNAMIC_METADATA(io.envoy.ai_gateway:caller)%")
        self.assertEqual(fields["input_tokens"], "%DYNAMIC_METADATA(io.envoy.ai_gateway:llm_input_token)%")
        self.assertEqual(fields["output_tokens"], "%DYNAMIC_METADATA(io.envoy.ai_gateway:llm_output_token)%")

        gw = resource.struct_to_dict(got.desired.resources["gateway"].resource)
        manifest = gw["spec"]["forProvider"]["manifest"]
        self.assertEqual(
            manifest["spec"]["listeners"],
            [{"name": "http", "protocol": "HTTP", "port": 80, "allowedRoutes": {"namespaces": {"from": "Same"}}}],
            "one HTTP listener, no hostname, accepting only this namespace's routes",
        )
        self.assertEqual(
            manifest["spec"]["infrastructure"]["parametersRef"],
            {"group": "gateway.envoyproxy.io", "kind": "EnvoyProxy", "name": fn._GATEWAY_NAME},
            "its own EnvoyProxy, not the GatewayClass's",
        )
        self.assertEqual(
            resource.struct_to_dict(got.desired.composite.resource).get("status"),
            {},
            "nothing to report until the Gateway has an address",
        )

    async def test_full_gateway(self) -> None:
        """A gateway with a hostname, TLS and auth, whose Gateway has an address.

        Checks the things a caller depends on: the HTTPS listener, the Secrets
        copied to the cluster, the caller policy naming them, /healthz exempted
        from that policy, and the endpoints status reporting HTTPS URLs.
        """
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        _xr(
                            hostname="eu.example.com",
                            tls=v1alpha1.Tls(certificateRefs=[v1alpha1.CertificateRef(name="eu-tls-0")]),
                            auth=v1alpha1.Auth(
                                secretSelector=v1alpha1.SecretSelector(
                                    matchLabels={"modelplane.ai/inference-keys": "true"}
                                )
                            ),
                        )
                    )
                ),
                resources={
                    "gateway": _observed_gateway(_ADDRESS, ready=True),
                    "caller-auth": _observed_accepted(),
                },
            ),
            required_resources=_required(
                cluster=[_cluster()],
                gateways=[_gateway_xr("eu", _CLUSTER)],
                **{
                    "caller-secrets": [_secret("ml-team-keys", {"ml-team-assistant": "sk-mp-a1b2c3"})],
                    "tls-secret-0": [_secret("eu-tls-0", {"tls.crt": "cert", "tls.key": "key"})],
                },
            ),
        )
        got = await self.runner.RunFunction(req, None)

        self.assertEqual(
            sorted(got.desired.resources),
            [
                "caller-auth",
                "caller-secret-ml-team-keys",
                "client-ca-bundle",
                "client-ca-certificate",
                "client-ca-configmap",
                "client-ca-issuer",
                "client-certificate",
                "client-selfsigned-issuer",
                "envoy-proxy",
                "failover-policy",
                "gateway",
                "healthz-auth",
                "healthz-filter",
                "healthz-route",
                "tls-secret-eu-tls-0",
            ],
        )

        def manifest(key: str) -> dict:
            return resource.struct_to_dict(got.desired.resources[key].resource)["spec"]["forProvider"]["manifest"]

        self.assertEqual(
            manifest("gateway")["spec"]["listeners"][1],
            {
                "name": "https",
                "protocol": "HTTPS",
                "port": 443,
                "hostname": "eu.example.com",
                "tls": {"mode": "Terminate", "certificateRefs": [{"name": "eu-tls-0"}]},
                "allowedRoutes": {"namespaces": {"from": "Same"}},
            },
        )
        # The HTTP listener stays hostname-less even here. A listener hostname is
        # matched against the request Host, so setting it 404s anything addressed
        # by IP, which is what /healthz on status.address is.
        self.assertNotIn("hostname", manifest("gateway")["spec"]["listeners"][0])
        self.assertEqual(
            manifest("tls-secret-eu-tls-0"),
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "eu-tls-0", "namespace": fn.REMOTE_NAMESPACE},
                "type": "kubernetes.io/tls",
                "data": {
                    "tls.crt": base64.b64encode(b"cert").decode(),
                    "tls.key": base64.b64encode(b"key").decode(),
                },
            },
            "the certificate is copied verbatim, keeping the name the Gateway refers to it by",
        )
        self.assertEqual(
            manifest("caller-auth")["spec"]["apiKeyAuth"],
            {
                "credentialRefs": [{"name": "callers-ml-team-keys"}],
                "extractFrom": [{"headers": ["Authorization"]}],
                "forwardClientIDHeader": fn._CALLER_HEADER,
                "sanitize": True,
            },
        )
        self.assertEqual(
            manifest("healthz-auth")["spec"],
            {
                "targetRefs": [{"group": "gateway.networking.k8s.io", "kind": "HTTPRoute", "name": fn._HEALTHZ_NAME}],
                "authorization": {"defaultAction": "Allow"},
            },
            "/healthz overrides the Gateway-level policy so a health check needs no credential",
        )
        self.assertEqual(
            resource.struct_to_dict(got.desired.composite.resource)["status"],
            {
                "address": _ADDRESS,
                "endpoints": {
                    "openAI": "https://eu.example.com/v1",
                    "anthropic": "https://eu.example.com/anthropic/v1",
                },
            },
        )
        self.assertEqual(
            list(got.conditions),
            [
                fnv1.Condition(
                    type=fn.CONDITION_TYPE_GATEWAY_READY,
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason=fn.CONDITION_REASON_GATEWAY_PROGRAMMED,
                )
            ],
        )

    async def test_endpoints_fall_back_to_the_address(self) -> None:
        """Without a hostname the endpoints use the address over plain HTTP, so
        what status reports is always something a caller can actually use."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(_xr())),
                resources={"gateway": _observed_gateway(_ADDRESS, ready=False)},
            ),
            required_resources=_required(cluster=[_cluster()], gateways=[_gateway_xr("eu", _CLUSTER)]),
        )
        got = await self.runner.RunFunction(req, None)
        self.assertEqual(
            resource.struct_to_dict(got.desired.composite.resource)["status"],
            {
                "address": _ADDRESS,
                "endpoints": {
                    "openAI": f"http://{_ADDRESS}/v1",
                    "anthropic": f"http://{_ADDRESS}/anthropic/v1",
                },
            },
        )
        self.assertEqual(
            next(iter(got.conditions)).reason,
            fn.CONDITION_REASON_WAITING_FOR_GATEWAY,
            "an address alone isn't readiness; the Gateway must be programmed",
        )

    async def test_resolves_each_cluster_gateway_name(self) -> None:
        """A Service per cluster gateway, resolving its name to its address here.

        A ModelService's backends address a cluster gateway by the name
        compose-inference-cluster derived, and this gateway's Envoy resolves it,
        so its cluster needs a Service of that name. An IP is served by a
        headless Service and an EndpointSlice; a hostname, which is how a cloud
        load balancer names itself, by an ExternalName Service. A cluster that
        hasn't published both an address and a name gets neither.
        """
        ipv4 = "prod-ipv4-gateway-aaaaa.modelplane-system.svc.cluster.local"
        ipv6 = "prod-ipv6-gateway-bbbbb.modelplane-system.svc.cluster.local"
        dns = "prod-dns-gateway-ccccc.modelplane-system.svc.cluster.local"
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_xr()))),
            required_resources=_required(
                cluster=[_cluster()],
                gateways=[_gateway_xr("eu", _CLUSTER)],
                clusters=[
                    _cluster(),  # this gateway's own cluster, no gateway published yet
                    _cluster_with_gateway("prod-ipv4", address="203.0.113.7", hostname=ipv4),
                    _cluster_with_gateway("prod-ipv6", address="2001:db8::1", hostname=ipv6),
                    _cluster_with_gateway("prod-dns", address="lb-x.elb.amazonaws.com", hostname=dns),
                ],
            ),
        )
        got = await self.runner.RunFunction(req, None)

        resolvers = {
            key: resource.struct_to_dict(res.resource)
            for key, res in got.desired.resources.items()
            if key.startswith("cluster-name")
        }
        for key, obj in resolvers.items():
            self.assertEqual(
                obj["spec"]["providerConfigRef"],
                {"kind": "ClusterProviderConfig", "name": _PC},
                f"{key} is composed against this gateway's own cluster",
            )
        manifests = {key: obj["spec"]["forProvider"]["manifest"] for key, obj in resolvers.items()}
        self.assertEqual(
            manifests,
            {
                "cluster-name-prod-ipv4-gateway-aaaaa": {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": "prod-ipv4-gateway-aaaaa", "namespace": fn.REMOTE_NAMESPACE},
                    "spec": {"clusterIP": "None", "ports": [{"name": "https", "port": 443}]},
                },
                "cluster-name-slice-prod-ipv4-gateway-aaaaa": {
                    "apiVersion": "discovery.k8s.io/v1",
                    "kind": "EndpointSlice",
                    "metadata": {
                        "name": "prod-ipv4-gateway-aaaaa",
                        "namespace": fn.REMOTE_NAMESPACE,
                        "labels": {"kubernetes.io/service-name": "prod-ipv4-gateway-aaaaa"},
                    },
                    "addressType": "IPv4",
                    "ports": [{"name": "https", "port": 443}],
                    "endpoints": [{"addresses": ["203.0.113.7"], "conditions": {"ready": True}}],
                },
                "cluster-name-prod-ipv6-gateway-bbbbb": {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": "prod-ipv6-gateway-bbbbb", "namespace": fn.REMOTE_NAMESPACE},
                    "spec": {"clusterIP": "None", "ports": [{"name": "https", "port": 443}]},
                },
                "cluster-name-slice-prod-ipv6-gateway-bbbbb": {
                    "apiVersion": "discovery.k8s.io/v1",
                    "kind": "EndpointSlice",
                    "metadata": {
                        "name": "prod-ipv6-gateway-bbbbb",
                        "namespace": fn.REMOTE_NAMESPACE,
                        "labels": {"kubernetes.io/service-name": "prod-ipv6-gateway-bbbbb"},
                    },
                    "addressType": "IPv6",
                    "ports": [{"name": "https", "port": 443}],
                    "endpoints": [{"addresses": ["2001:db8::1"], "conditions": {"ready": True}}],
                },
                "cluster-name-prod-dns-gateway-ccccc": {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": "prod-dns-gateway-ccccc", "namespace": fn.REMOTE_NAMESPACE},
                    "spec": {"type": "ExternalName", "externalName": "lb-x.elb.amazonaws.com"},
                },
            },
            "IP clusters get a headless Service + EndpointSlice, the hostname cluster an ExternalName, "
            "and the own cluster with nothing published gets neither",
        )

    async def test_a_rejected_caller_policy_is_not_ready(self) -> None:
        """A gateway whose caller policy was rejected refuses every request with
        a 500 while its Gateway is perfectly healthy. Envoy Gateway rejects the
        policy when two selected Secrets share a key value, so this is reachable
        by writing two Secrets, and reporting Ready would say the front door
        works when nothing can get through it."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        _xr(
                            auth=v1alpha1.Auth(
                                secretSelector=v1alpha1.SecretSelector(
                                    matchLabels={"modelplane.ai/inference-keys": "true"}
                                )
                            )
                        )
                    )
                ),
                # The Gateway is programmed; the policy is not accepted.
                resources={"gateway": _observed_gateway(_ADDRESS, ready=True)},
            ),
            required_resources=_required(
                cluster=[_cluster()],
                gateways=[_gateway_xr("eu", _CLUSTER)],
                **{"caller-secrets": [_secret("ml-team-keys", {"a": "sk-1"})]},
            ),
        )
        got = await self.runner.RunFunction(req, None)
        cond = next(iter(got.conditions))
        self.assertEqual(cond.status, fnv1.STATUS_CONDITION_FALSE)
        self.assertEqual(cond.reason, fn.CONDITION_REASON_AUTH_NOT_ACCEPTED)

    async def test_the_incumbent_keeps_its_cluster(self) -> None:
        """A gateway created later must not take a cluster off one already
        serving traffic. Doing so would delete the incumbent's Gateway and bring
        its load balancer back on a different address, which is the one thing a
        gateway may never do to its callers, and lowest-name-wins would have."""
        # "aaa" sorts before "zzz" but "zzz" already has an address.
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {
                            "apiVersion": "modelplane.ai/v1alpha1",
                            "kind": "InferenceGateway",
                            "metadata": {"name": "aaa"},
                            "spec": {"clusterName": _CLUSTER},
                        }
                    )
                )
            ),
            required_resources=_required(
                cluster=[_cluster()],
                gateways=[
                    _gateway_xr("aaa", _CLUSTER),
                    {**_gateway_xr("zzz", _CLUSTER), "status": {"address": _ADDRESS}},
                ],
            ),
        )
        got = await self.runner.RunFunction(req, None)
        self.assertEqual(len(got.desired.resources), 0, "the newcomer composes nothing")
        cond = next(iter(got.conditions))
        self.assertEqual(cond.reason, fn.CONDITION_REASON_CLUSTER_TAKEN)
        self.assertIn("zzz", cond.message)

    async def test_no_composed_object_observes_a_secret(self) -> None:
        """No composed Object reads a Secret, which is what keeps this gateway's
        client CA private key off the control plane.

        provider-kubernetes copies an observed object's whole manifest into the
        Object's status, and its --sanitize-secrets flag defaults to false, so
        observing a Secret publishes every key in it to anyone who can get
        objects. This CA signs the certificate every cluster gateway in the fleet
        accepts, so leaking its key means anyone can reach any engine.

        Asserted over everything composed rather than over the PKI, because the
        cost of reintroducing this anywhere is the same.

        Observing is the case that matters here. The Secrets this function
        *writes* also end up in status, because provider-kubernetes reports what
        it observes of what it manages, so this alone doesn't keep their contents
        off the control plane. Those hold caller keys and serving certificates
        that came from control-plane Secrets to begin with, so the exposure is a
        wider audience for data already present rather than data that would
        otherwise never be there, and prerequisites.yaml runs
        provider-kubernetes with --sanitize-secrets to redact it. A CA private
        key is different in kind: it is generated on the workload cluster and
        observing it is the only way it could ever reach the control plane.
        """
        # Auth and TLS both on, so the Secret-copying path is exercised: without
        # them this function composes no Secret at all and the assertion holds
        # vacuously.
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        _xr(
                            hostname="gw.example.org",
                            tls={"certificateRefs": [{"name": "eu-tls-0"}]},
                            auth={"secretSelector": {"matchLabels": {"team": "ml"}}},
                        )
                    )
                ),
            ),
            required_resources=_required(
                cluster=[_cluster()],
                gateways=[_gateway_xr("eu", _CLUSTER)],
                **{
                    "caller-secrets": [_secret("ml-team-keys", {"alice": "key"})],
                    "tls-secret-0": [_secret("eu-tls-0", {"tls.crt": "cert", "tls.key": "key"})],
                },
            ),
        )
        got = await self.runner.RunFunction(req, None)

        composed_secrets = []
        observed_secrets = []
        for key, res in got.desired.resources.items():
            d = resource.struct_to_dict(res.resource)
            manifest = d["spec"]["forProvider"]["manifest"]
            if manifest["kind"] != "Secret":
                continue
            composed_secrets.append(key)
            if "Observe" in d["spec"].get("managementPolicies", []):
                observed_secrets.append(key)
        self.assertEqual(observed_secrets, [], "these observe a Secret, so its private keys reach the control plane")
        self.assertNotEqual(composed_secrets, [], "no Secret composed, so the assertion above proves nothing")

    async def test_client_pki_publishes_the_ca_without_its_key(self) -> None:
        """The client CA's certificate reaches the control plane through a
        trust-manager Bundle, which copies one named key into a ConfigMap, rather
        than through the Secret that also holds the private key."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_xr()))),
            required_resources=_required(cluster=[_cluster()], gateways=[_gateway_xr("eu", _CLUSTER)]),
        )
        got = await self.runner.RunFunction(req, None)

        def manifest(key: str) -> dict:
            return resource.struct_to_dict(got.desired.resources[key].resource)["spec"]["forProvider"]["manifest"]

        self.assertEqual(
            manifest("client-ca-bundle"),
            {
                "apiVersion": "trust.cert-manager.io/v1alpha1",
                "kind": "Bundle",
                "metadata": {"name": "fleet-gateway-ca"},
                "spec": {
                    "sources": [{"secret": {"name": "fleet-gateway-ca", "key": "ca.crt"}}],
                    "target": {
                        "configMap": {"key": "ca.crt"},
                        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "modelplane-system"}},
                    },
                },
            },
        )
        # Named after the Bundle, because that's the ConfigMap a Bundle syncs.
        self.assertEqual(
            manifest("client-ca-configmap"),
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "fleet-gateway-ca", "namespace": "modelplane-system"},
            },
        )
        self.assertEqual(
            resource.struct_to_dict(got.desired.resources["client-ca-configmap"].resource)["spec"][
                "managementPolicies"
            ],
            ["Observe"],
            "trust-manager owns this ConfigMap; Crossplane must not write it",
        )

    async def test_client_ca_published_from_the_observed_configmap(self) -> None:
        """status.clientCACertificate comes from the ConfigMap trust-manager
        syncs, as plain text rather than base64. A cluster only trusts this
        gateway once it has it, so nothing reaches an engine before it appears.
        """
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(_xr())),
                resources={
                    "gateway": _observed_gateway("gw.example.org", ready=True),
                    "client-ca-configmap": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "status": {
                                    "atProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "ConfigMap",
                                            "data": {"ca.crt": "-----BEGIN CERTIFICATE-----\nclient\n"},
                                        }
                                    }
                                },
                            }
                        ),
                    ),
                },
            ),
            required_resources=_required(cluster=[_cluster()], gateways=[_gateway_xr("eu", _CLUSTER)]),
        )
        got = await self.runner.RunFunction(req, None)

        self.assertEqual(
            resource.struct_to_dict(got.desired.composite.resource)["status"]["clientCACertificate"],
            "-----BEGIN CERTIFICATE-----\nclient\n",
        )

    async def test_no_client_ca_before_the_bundle_syncs(self) -> None:
        """With no observed ConfigMap the gateway publishes no CA, so no cluster
        trusts it yet and no cluster publishes a hostname on its account."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(_xr())),
                resources={"gateway": _observed_gateway("gw.example.org", ready=True)},
            ),
            required_resources=_required(cluster=[_cluster()], gateways=[_gateway_xr("eu", _CLUSTER)]),
        )
        got = await self.runner.RunFunction(req, None)

        self.assertNotIn(
            "clientCACertificate",
            resource.struct_to_dict(got.desired.composite.resource)["status"],
        )
