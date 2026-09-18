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

"""Tests for the compose-model-route function."""

import base64
import dataclasses
import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.inferencegateway import v1alpha1 as igv1alpha1
from models.ai.modelplane.modelendpoint import v1alpha1 as mev1alpha1
from models.ai.modelplane.modelroute import v1alpha1

_NS = "ml-team"
_SVC = "assistant"
_MODEL = f"{_NS}/{_SVC}"
_GW = "eu"
_CLUSTER_CA = "-----BEGIN CERTIFICATE-----\ncluster\n-----END CERTIFICATE-----\n"
_CLIENT_CA = "-----BEGIN CERTIFICATE-----\nclient\n-----END CERTIFICATE-----\n"


@dataclasses.dataclass
class Case:
    """A test case for compose-model-route."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _entry(
    label: str, *, name: str | None = None, priority: int | None = None, weight: int | None = None
) -> v1alpha1.Endpoint:
    kwargs = {}
    if priority is not None:
        kwargs["priority"] = priority
    if weight is not None:
        kwargs["weight"] = weight
    return v1alpha1.Endpoint(
        name=name or label,
        selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": label}),
        **kwargs,
    )


def _route_xr(entries: list[v1alpha1.Endpoint], *, gateway: str = _GW) -> dict:
    xr = v1alpha1.ModelRoute(
        apiVersion="modelplane.ai/v1alpha1",
        kind="ModelRoute",
        metadata={"name": f"{_SVC}-{gateway}", "namespace": _NS},
        spec=v1alpha1.Spec(gatewayName=gateway, serviceName=_SVC, endpoints=entries),
    )
    return xr.model_dump(exclude_none=True, mode="json", by_alias=True)


def _endpoint(
    name: str,
    *,
    origin: str,
    model: str | None = None,
    credential: str | None = None,
    ready: bool = True,
    composed: bool = False,
) -> dict:
    ep = mev1alpha1.ModelEndpoint(
        apiVersion="modelplane.ai/v1alpha1",
        kind="ModelEndpoint",
        metadata={"name": name, "namespace": _NS},
        spec=mev1alpha1.Spec(
            origin=origin,
            **({"model": model} if model else {}),
            **({"credentialRef": mev1alpha1.CredentialRef(name=credential)} if credential else {}),
        ),
    )
    d = ep.model_dump(exclude_none=True, mode="json", by_alias=True)
    if composed:
        d["metadata"]["labels"] = {"modelplane.ai/cluster": "gw-eu", "modelplane.ai/deployment": "d"}
    d["status"] = {
        "conditions": [
            {
                "type": "EndpointReady",
                "status": "True" if ready else "False",
                "reason": "EndpointUsable" if ready else "CredentialMissing",
                "lastTransitionTime": "2026-06-08T00:00:00Z",
            }
        ]
    }
    return d


def _gateway(*, client_ca: str | None = _CLIENT_CA, address: str | None = "203.0.113.1", tls: bool = False) -> dict:
    spec = igv1alpha1.Spec(clusterName="gw-eu", hostname="eu.example.com")
    if tls:
        spec.tls = igv1alpha1.Tls(certificateRefs=[igv1alpha1.CertificateRef(name="eu-tls")])
    gw = igv1alpha1.InferenceGateway(
        apiVersion="modelplane.ai/v1alpha1",
        kind="InferenceGateway",
        metadata={"name": _GW},
        spec=spec,
    )
    d = gw.model_dump(exclude_none=True, mode="json", by_alias=True)
    status: dict = {}
    if address:
        status["address"] = address
    if client_ca:
        status["clientCACertificate"] = client_ca
    if status:
        d["status"] = status
    return d


def _cluster(name: str, *, provider_config: str | None = "gw-eu-pc", ca: str | None = _CLUSTER_CA) -> dict:
    c = icv1alpha1.InferenceCluster(
        apiVersion="modelplane.ai/v1alpha1",
        kind="InferenceCluster",
        metadata={"name": name},
        spec=icv1alpha1.Spec(
            cluster=icv1alpha1.Cluster(
                source="Existing",
                existing=icv1alpha1.Existing(
                    secretRef=icv1alpha1.SecretRef(name=f"{name}-kubeconfig", key="kubeconfig")
                ),
            )
        ),
    )
    d = c.model_dump(exclude_none=True, mode="json", by_alias=True)
    status: dict = {}
    if provider_config:
        status["providerConfigRef"] = {"name": provider_config}
    if ca:
        status["gateway"] = {"caCertificate": ca}
    if status:
        d["status"] = status
    return d


def _secret(name: str, data: dict[str, str]) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": _NS},
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()},
    }


def _required(**resources) -> dict:  # noqa: ANN003
    return {
        name: fnv1.Resources(items=[fnv1.Resource(resource=resource.dict_to_struct(r)) for r in items])
        for name, items in resources.items()
    }


def _requirements(entries: list[v1alpha1.Endpoint], *, credentials: dict[str, str] | None = None) -> fnv1.Requirements:
    """The requirements the function emits: the named gateway, every cluster, a
    ModelEndpoint selector per entry, and a Secret per endpoint that names a
    credential (endpoint name -> Secret name)."""
    reqs = {
        "gateway": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceGateway", match_name=_GW),
        "clusters": fnv1.ResourceSelector(api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"),
    }
    for entry in entries:
        reqs[f"endpoints-{entry.name}"] = fnv1.ResourceSelector(
            api_version="modelplane.ai/v1alpha1",
            kind="ModelEndpoint",
            namespace=_NS,
            match_labels=fnv1.MatchLabels(labels=dict(entry.selector.matchLabels)),
        )
    for endpoint, secret in (credentials or {}).items():
        reqs[f"credential-{endpoint}"] = fnv1.ResourceSelector(
            api_version="v1", kind="Secret", namespace=_NS, match_name=secret
        )
    return fnv1.Requirements(resources=reqs)


def _not_ready(
    status: dict, reason: str, message: str, requirements: fnv1.Requirements, *, warning: str | None = None
) -> fnv1.RunFunctionResponse:
    """The whole response for a pass that composes nothing: the status counts so
    far, a not-ready composite, one RoutingReady=False condition, and the reason
    as a result. A warning about endpoints dropped before the tier emptied
    precedes it."""
    results = []
    if warning is not None:
        results.append(fnv1.Result(severity=fnv1.SEVERITY_WARNING, message=warning))
    results.append(fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message=message))
    return fnv1.RunFunctionResponse(
        meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
        desired=fnv1.State(
            composite=fnv1.Resource(
                resource=resource.dict_to_struct({"status": status}),
                ready=fnv1.READY_FALSE,
            )
        ),
        context=structpb.Struct(),
        requirements=requirements,
        conditions=[
            fnv1.Condition(
                type=fn.CONDITION_TYPE_ROUTING_READY,
                status=fnv1.STATUS_CONDITION_FALSE,
                reason=reason,
                message=message,
            )
        ],
        results=results,
    )


def _manifest(rsp: fnv1.RunFunctionResponse, key: str) -> dict:
    return resource.struct_to_dict(rsp.desired.resources[key].resource)["spec"]["forProvider"]["manifest"]


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


class TestGates(unittest.IsolatedAsyncioTestCase):
    """Passes where a route can't be composed compose nothing and say why.
    Asserting the whole response proves nothing is composed against a cluster the
    route can't yet reach, rather than a subset being applied."""

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_gates(self) -> None:
        composed = _endpoint("self", origin="https://gw-eu.example.com", composed=True)
        cases = [
            Case(
                name="the gateway's client PKI hasn't issued, so nothing can name its certificate",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_route_xr([_entry("d")])))
                    ),
                    required_resources=_required(
                        gateway=[_gateway(client_ca=None)],
                        clusters=[_cluster("gw-eu")],
                        **{"endpoints-d": [composed]},
                    ),
                ),
                want=_not_ready(
                    {"model": _MODEL, "endpoints": {"total": 0, "ready": 0}},
                    fn.CONDITION_REASON_WAITING_FOR_GATEWAY,
                    "InferenceGateway eu has not published its client CA",
                    _requirements([_entry("d")]),
                ),
            ),
            Case(
                name="no selected endpoint is ready",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_route_xr([_entry("d")])))
                    ),
                    required_resources=_required(
                        gateway=[_gateway()],
                        clusters=[_cluster("gw-eu")],
                        **{"endpoints-d": [_endpoint("self", origin="https://gw-eu.example.com", ready=False)]},
                    ),
                ),
                want=_not_ready(
                    {
                        "model": _MODEL,
                        "address": "203.0.113.1",
                        "hostname": "eu.example.com",
                        "endpoints": {"total": 1, "ready": 0},
                    },
                    fn.CONDITION_REASON_NO_ENDPOINTS,
                    "None of the 1 selected ModelEndpoints is ready to carry traffic",
                    _requirements([_entry("d")]),
                ),
            ),
            Case(
                name="a composed endpoint whose cluster withdrew its CA is dropped",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_route_xr([_entry("d")])))
                    ),
                    required_resources=_required(
                        gateway=[_gateway()],
                        clusters=[_cluster("gw-eu", ca=None)],
                        **{"endpoints-d": [composed]},
                    ),
                ),
                want=_not_ready(
                    {
                        "model": _MODEL,
                        "address": "203.0.113.1",
                        "hostname": "eu.example.com",
                        "endpoints": {"total": 1, "ready": 0},
                    },
                    fn.CONDITION_REASON_NO_ENDPOINTS,
                    "None of the 1 selected ModelEndpoints is ready to carry traffic",
                    _requirements([_entry("d")]),
                    warning="Endpoints left out of the route, their cluster has published no gateway CA: self",
                ),
            ),
            Case(
                name="a credential Secret missing its key drops the endpoint",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_route_xr([_entry("a")])))
                    ),
                    required_resources=_required(
                        gateway=[_gateway()],
                        clusters=[_cluster("gw-eu")],
                        **{
                            "endpoints-a": [_endpoint("wrongkey", origin="https://a.example.com", credential="k")],
                            "credential-wrongkey": [_secret("k", {"token": "sk-1"})],
                        },
                    ),
                ),
                want=_not_ready(
                    {
                        "model": _MODEL,
                        "address": "203.0.113.1",
                        "hostname": "eu.example.com",
                        "endpoints": {"total": 1, "ready": 0},
                    },
                    fn.CONDITION_REASON_NO_ENDPOINTS,
                    "None of the 1 selected ModelEndpoints is ready to carry traffic",
                    _requirements([_entry("a")], credentials={"wrongkey": "k"}),
                    warning=(
                        "Endpoints left out of the route, their credential Secret missing or missing its key: wrongkey"
                    ),
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


class TestCompose(unittest.IsolatedAsyncioTestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:
        """A composed self-hosted endpoint at priority 0 and a third-party
        provider at priority 1: backends, credential, cluster CA and route."""
        entries = [_entry("d", priority=0), _entry("together", priority=1)]
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_route_xr(entries)))),
            required_resources=_required(
                gateway=[_gateway()],
                clusters=[_cluster("gw-eu")],
                **{
                    "endpoints-d": [
                        _endpoint("self", origin="https://gw-eu.example.com", model="d", composed=True),
                    ],
                    "endpoints-together": [
                        _endpoint(
                            "together",
                            origin="https://api.together.xyz",
                            model="Qwen/Qwen2.5",
                            credential="together-key",
                        ),
                    ],
                    "credential-together": [_secret("together-key", {"apiKey": "sk-tog"})],
                },
            ),
        )
        got = await self.runner.RunFunction(req, None)

        # The exact set, so an unexpected extra object fails the test.
        self.assertEqual(
            set(got.desired.resources),
            {
                "backend-self",
                "aibackend-self",
                "backend-together",
                "aibackend-together",
                "credential-together",
                "credpolicy-together",
                "cluster-ca-gw-eu",
                "route",
            },
        )

        route = _manifest(got, "route")
        rule = route["spec"]["rules"][0]
        self.assertEqual(
            rule["backendRefs"],
            [
                {"name": resource.child_name(_NS, _SVC, "self"), "weight": 1, "priority": 0, "modelNameOverride": "d"},
                {
                    "name": resource.child_name(_NS, _SVC, "together"),
                    "weight": 1,
                    "priority": 1,
                    "modelNameOverride": "Qwen/Qwen2.5",
                },
            ],
        )
        self.assertEqual(
            rule["matches"][0]["headers"][0],
            {"type": "Exact", "name": "x-ai-eg-model", "value": _MODEL},
        )
        # Declaring the token costs is what makes the ext-proc ask a backend for
        # usage on a streamed response, which otherwise reports none, and is
        # where the metered counts in the access log come from.
        self.assertEqual(
            route["spec"]["llmRequestCosts"],
            [
                {"metadataKey": "llm_input_token", "type": "InputToken"},
                {"metadataKey": "llm_output_token", "type": "OutputToken"},
                {"metadataKey": "llm_total_token", "type": "TotalToken"},
            ],
        )

        # The composed backend pins its cluster's CA and presents the client
        # certificate; the third-party one uses the system trust store.
        self.assertEqual(
            _manifest(got, "backend-self")["spec"]["tls"],
            {
                "caCertificateRefs": [
                    {"kind": "ConfigMap", "group": "", "name": resource.child_name("cluster-ca", "gw-eu")}
                ],
                "sni": "gw-eu.example.com",
                "clientCertificateRef": {"kind": "Secret", "group": "", "name": "inference-gateway-client"},
            },
        )
        self.assertEqual(
            _manifest(got, "backend-together")["spec"]["tls"],
            {"wellKnownCACertificates": "System", "sni": "api.together.xyz"},
        )

        # The caller header is stripped only for the backend we don't operate.
        self.assertNotIn("headerMutation", _manifest(got, "aibackend-self")["spec"])
        self.assertEqual(
            _manifest(got, "aibackend-together")["spec"]["headerMutation"],
            {"remove": ["x-modelplane-caller"]},
        )

        # The credential is republished under the fixed apiKey key.
        self.assertEqual(
            _manifest(got, "credential-together")["data"],
            {"apiKey": base64.b64encode(b"sk-tog").decode()},
        )
        self.assertEqual(_manifest(got, "cluster-ca-gw-eu")["data"], {"ca.crt": _CLUSTER_CA})

    async def test_route_binds_to_the_listener_matching_the_gateways_tls(self) -> None:
        """A TLS gateway serves inference on its HTTPS listener alone, so the
        route binds there; without TLS there's only the HTTP listener. Binding to
        :80 on a TLS gateway would carry credentials in the clear."""
        for tls, want in ((False, "http"), (True, "https")):
            with self.subTest(tls=tls):
                req = fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_route_xr([_entry("d")])))
                    ),
                    required_resources=_required(
                        gateway=[_gateway(tls=tls)],
                        clusters=[_cluster("gw-eu")],
                        **{"endpoints-d": [_endpoint("self", origin="https://gw-eu.example.com", composed=True)]},
                    ),
                )
                got = await self.runner.RunFunction(req, None)
                self.assertEqual(_manifest(got, "route")["spec"]["parentRefs"][0]["sectionName"], want)

    async def test_status_reports_address_and_counts(self) -> None:
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_route_xr([_entry("d")])))),
            required_resources=_required(
                gateway=[_gateway(address="203.0.113.9")],
                clusters=[_cluster("gw-eu")],
                **{"endpoints-d": [_endpoint("self", origin="https://gw-eu.example.com", composed=True)]},
            ),
        )
        got = await self.runner.RunFunction(req, None)
        self.assertEqual(
            resource.struct_to_dict(got.desired.composite.resource)["status"],
            {
                "model": _MODEL,
                "address": "203.0.113.9",
                "hostname": "eu.example.com",
                "endpoints": {"total": 1, "ready": 1},
            },
        )

    async def test_an_endpoint_matched_twice_belongs_to_the_first_entry(self) -> None:
        """A canary entry and a catch-all entry must not both weight one
        endpoint; the first that matches it wins."""
        entries = [_entry("kimi", name="canary", priority=0), _entry("kimi", name="catchall", priority=1)]
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_route_xr(entries)))),
            required_resources=_required(
                gateway=[_gateway()],
                clusters=[_cluster("gw-eu")],
                **{
                    "endpoints-canary": [_endpoint("kimi-a", origin="https://a.example.com")],
                    "endpoints-catchall": [_endpoint("kimi-a", origin="https://a.example.com")],
                },
            ),
        )
        got = await self.runner.RunFunction(req, None)
        refs = _manifest(got, "route")["spec"]["rules"][0]["backendRefs"]
        self.assertEqual(refs, [{"name": resource.child_name(_NS, _SVC, "kimi-a"), "weight": 1, "priority": 0}])
        self.assertEqual(
            resource.struct_to_dict(got.desired.composite.resource)["status"]["endpoints"],
            {"total": 1, "ready": 1},
        )

    async def test_priorities_are_renumbered_without_gaps(self) -> None:
        """A ModelService's priorities are an ordering; Envoy's are levels it
        walks from 0. A user writing 0 and 5, or a tier gone unready during a
        roll, would otherwise leave gaps in what Envoy gets."""
        entries = [_entry("a", priority=0), _entry("b", priority=5), _entry("c", priority=9)]
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_route_xr(entries)))),
            required_resources=_required(
                gateway=[_gateway()],
                clusters=[_cluster("gw-eu")],
                **{
                    # The middle tier has no ready endpoint, so it drops out and
                    # must not leave a hole behind it.
                    "endpoints-a": [_endpoint("a-0", origin="https://a.example.com")],
                    "endpoints-b": [_endpoint("b-0", origin="https://b.example.com", ready=False)],
                    "endpoints-c": [_endpoint("c-0", origin="https://c.example.com")],
                },
            ),
        )
        got = await self.runner.RunFunction(req, None)
        refs = _manifest(got, "route")["spec"]["rules"][0]["backendRefs"]
        self.assertEqual([r["priority"] for r in refs], [0, 1], "two tiers survive, renumbered 0 and 1")


@dataclasses.dataclass
class WeightCase:
    """A weight-distribution case: the entries and the endpoints each matched,
    and the whole backendRefs list the route should carry."""

    name: str
    entries: list[v1alpha1.Endpoint]
    endpoints: dict[str, list[dict]]
    want_refs: list[dict]


class TestWeights(unittest.IsolatedAsyncioTestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_weight_distribution(self) -> None:
        def _origins(*names_: str) -> list[dict]:
            return [_endpoint(n, origin=f"https://{n}.example.com") for n in names_]

        def _ref(ep: str, weight: int, priority: int = 0) -> dict:
            return {"name": resource.child_name(_NS, _SVC, ep), "weight": weight, "priority": priority}

        cases = [
            WeightCase(
                # An entry's weight is written once but applied per backend, so it
                # spreads over the endpoints it matched while the ratio between
                # entries survives: 90 over three is 30 each, 10 over one is 10,
                # reduced by the gcd to the smallest equivalent integers.
                name="a weight spreads across a tier's endpoints, ratio preserved",
                entries=[_entry("big", weight=90), _entry("small", weight=10)],
                endpoints={
                    "endpoints-big": _origins("big-0", "big-1", "big-2"),
                    "endpoints-small": _origins("small-0"),
                },
                want_refs=[
                    _ref("big-0", 3),
                    _ref("big-1", 3),
                    _ref("big-2", 3),
                    _ref("small-0", 1),
                ],
            ),
            WeightCase(
                # Weight 1 over five endpoints must floor none of them to 0, which
                # would drop them from the load assignment rather than share.
                name="a weight below its endpoint count floors no endpoint",
                entries=[_entry("many", weight=1)],
                endpoints={"endpoints-many": _origins("many-0", "many-1", "many-2", "many-3", "many-4")},
                want_refs=[_ref(f"many-{i}", 1) for i in range(5)],
            ),
            WeightCase(
                # A max-weight entry beside a tiny one spread over two endpoints
                # scales past the per-backendRef limit even though every weight is
                # in bounds, so it rescales to the limit rather than composing a
                # route the API server rejects.
                name="an extreme but valid ratio is clamped to the limit",
                entries=[_entry("big", weight=1000000, priority=0), _entry("small", weight=1, priority=0)],
                endpoints={"endpoints-big": _origins("big-0"), "endpoints-small": _origins("small-0", "small-1")},
                want_refs=[_ref("big-0", 1000000), _ref("small-0", 1), _ref("small-1", 1)],
            ),
            WeightCase(
                # The remainder is handed to the first endpoints of a tier, so the
                # order must be the endpoints' names rather than the API server's
                # unspecified list order, or the composed weights churn.
                name="endpoints are ordered by name for a stable split",
                entries=[_entry("d")],
                endpoints={"endpoints-d": _origins("z", "a", "m")},
                want_refs=[_ref("a", 1), _ref("m", 1), _ref("z", 1)],
            ),
        ]
        for case in cases:
            with self.subTest(case.name):
                req = fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_route_xr(case.entries)))
                    ),
                    required_resources=_required(gateway=[_gateway()], clusters=[_cluster("gw-eu")], **case.endpoints),
                )
                got = await self.runner.RunFunction(req, None)
                self.assertEqual(_manifest(got, "route")["spec"]["rules"][0]["backendRefs"], case.want_refs)
