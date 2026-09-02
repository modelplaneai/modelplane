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

"""Tests for the compose-model-service function."""

import base64
import dataclasses
import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn, names
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.inferencegateway import v1alpha1 as igv1alpha1
from models.ai.modelplane.modelendpoint import v1alpha1 as mev1alpha1
from models.ai.modelplane.modelservice import v1alpha1

_NS = "ml-team"
_SVC = "assistant"
_MODEL = f"{_NS}/{_SVC}"


@dataclasses.dataclass
class Case:
    """A test case for compose-model-service."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _service(entries: list[v1alpha1.Endpoint], labels: dict[str, str] | None = None) -> dict:
    xr = v1alpha1.ModelService(
        apiVersion="modelplane.ai/v1alpha1",
        kind="ModelService",
        metadata={"name": _SVC, "namespace": _NS, **({"labels": labels} if labels else {})},
        spec=v1alpha1.Spec(endpoints=entries),
    )
    return xr.model_dump(exclude_none=True, mode="json", by_alias=True)


def _entry(deployment: str, *, priority: int | None = None, weight: int | None = None) -> v1alpha1.Endpoint:
    kwargs = {}
    if priority is not None:
        kwargs["priority"] = priority
    if weight is not None:
        kwargs["weight"] = weight
    return v1alpha1.Endpoint(selector=v1alpha1.Selector(matchLabels={"modelplane.ai/deployment": deployment}), **kwargs)


def _endpoint(
    name: str,
    *,
    origin: str,
    model: str | None = None,
    credential: str | None = None,
    prefix: str | None = None,
    schema: str | None = None,
    ready: bool = True,
    composed: bool = False,
) -> dict:
    """A ModelEndpoint as observed, built from the generated model so a field the
    XRD doesn't define can't creep in.

    composed marks it as one Modelplane composed for a replica, which it does by
    carrying the cluster label compose-model-deployment stamps. That is what
    decides whether the caller's identity travels to the backend, so a fixture
    for a self-hosted endpoint has to set it.
    """
    api = None
    if prefix or schema:
        api = mev1alpha1.Api(**({"prefix": prefix} if prefix else {}), **({"schema": schema} if schema else {}))
    ep = mev1alpha1.ModelEndpoint(
        apiVersion="modelplane.ai/v1alpha1",
        kind="ModelEndpoint",
        metadata={"name": name, "namespace": _NS},
        spec=mev1alpha1.Spec(
            origin=origin,
            **({"model": model} if model else {}),
            **({"api": api} if api else {}),
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


def _gateway(name: str, cluster: str, *, selector: dict[str, str] | None = None, address: str | None = None) -> dict:
    gw = igv1alpha1.InferenceGateway(
        apiVersion="modelplane.ai/v1alpha1",
        kind="InferenceGateway",
        metadata={"name": name},
        spec=igv1alpha1.Spec(
            clusterName=cluster,
            **({"serviceSelector": igv1alpha1.ServiceSelector(matchLabels=selector)} if selector else {}),
        ),
    )
    d = gw.model_dump(exclude_none=True, mode="json", by_alias=True)
    if address:
        d["status"] = {"address": address}
    return d


def _cluster(name: str, *, provider_config: str | None = None, ca: str | None = None) -> dict:
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


def _manifest(rsp: fnv1.RunFunctionResponse, key: str) -> dict:
    return resource.struct_to_dict(rsp.desired.resources[key].resource)["spec"]["forProvider"]["manifest"]


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


class TestGating(unittest.IsolatedAsyncioTestCase):
    """Passes where there's nothing to compose compose nothing, and say why."""

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:
        entries = [_entry("kimi-k2")]
        cases = [
            Case(
                name="unresolved requirements",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_service(entries)))),
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {"status": {"model": _MODEL, "endpoints": {"total": 0, "ready": 0}}}
                            )
                        )
                    ),
                    context=structpb.Struct(),
                    requirements=fnv1.Requirements(
                        resources={
                            "gateways": fnv1.ResourceSelector(
                                api_version="modelplane.ai/v1alpha1", kind="InferenceGateway"
                            ),
                            "clusters": fnv1.ResourceSelector(
                                api_version="modelplane.ai/v1alpha1", kind="InferenceCluster"
                            ),
                            "endpoints-0": fnv1.ResourceSelector(
                                api_version="modelplane.ai/v1alpha1",
                                kind="ModelEndpoint",
                                namespace=_NS,
                                match_labels=fnv1.MatchLabels(labels={"modelplane.ai/deployment": "kimi-k2"}),
                            ),
                        }
                    ),
                    conditions=[
                        fnv1.Condition(
                            type=fn.CONDITION_TYPE_ROUTING_READY,
                            status=fnv1.STATUS_CONDITION_FALSE,
                            reason=fn.CONDITION_REASON_WAITING_FOR_RESOURCES,
                            message="Waiting for gateways and endpoints to resolve",
                        )
                    ],
                    results=[
                        fnv1.Result(
                            severity=fnv1.SEVERITY_NORMAL, message="Waiting for gateways and endpoints to resolve"
                        )
                    ],
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

    async def test_no_gateway_serves_this_service(self) -> None:
        """A service no gateway selects is unreachable, and says so rather than
        composing a route nobody serves."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(_service([_entry("kimi-k2")], labels={"region": "us"}))
                )
            ),
            required_resources=_required(
                gateways=[_gateway("eu", "gw-eu", selector={"region": "eu"})],
                clusters=[_cluster("gw-eu", provider_config="gw-eu-pc")],
                **{"endpoints-0": [_endpoint("kimi-a", origin="https://a.example.com")]},
            ),
        )
        got = await self.runner.RunFunction(req, None)
        self.assertEqual(len(got.desired.resources), 0, "composes nothing")
        self.assertEqual(next(iter(got.conditions)).reason, fn.CONDITION_REASON_NO_GATEWAY)

    async def test_no_ready_endpoints(self) -> None:
        """An endpoint that isn't ready is kept out of the route entirely, and a
        service with none reports why instead of composing an empty route."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(_service([_entry("kimi-k2")])))
            ),
            required_resources=_required(
                gateways=[_gateway("eu", "gw-eu")],
                clusters=[_cluster("gw-eu", provider_config="gw-eu-pc")],
                **{
                    "endpoints-0": [
                        _endpoint("kimi-a", origin="https://a.example.com", ready=False),
                        _endpoint("kimi-b", origin="https://b.example.com", ready=False),
                    ]
                },
            ),
        )
        got = await self.runner.RunFunction(req, None)
        self.assertEqual(len(got.desired.resources), 0)
        cond = next(iter(got.conditions))
        self.assertEqual(cond.reason, fn.CONDITION_REASON_NO_ENDPOINTS)
        self.assertEqual(cond.message, "None of the 2 selected ModelEndpoints is ready to carry traffic")
        self.assertEqual(
            resource.struct_to_dict(got.desired.composite.resource)["status"]["endpoints"],
            {"total": 2, "ready": 0},
        )

    async def test_a_cluster_without_a_provider_config_is_skipped(self) -> None:
        """A gateway whose cluster hasn't published a ProviderConfig can't be
        composed onto. compose-inference-gateway reports that on the gateway."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(_service([_entry("kimi-k2")])))
            ),
            required_resources=_required(
                gateways=[_gateway("eu", "gw-eu")],
                clusters=[_cluster("gw-eu")],
                **{"endpoints-0": [_endpoint("kimi-a", origin="https://a.example.com")]},
            ),
        )
        got = await self.runner.RunFunction(req, None)
        self.assertEqual(next(iter(got.conditions)).reason, fn.CONDITION_REASON_NO_GATEWAY)


class TestComposition(unittest.IsolatedAsyncioTestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def _run(self, req: fnv1.RunFunctionRequest) -> fnv1.RunFunctionResponse:
        return await self.runner.RunFunction(req, None)

    async def test_route_and_backends(self) -> None:
        """The design's own example: a 90/10 canary across two self-hosted
        deployments at priority 0, with a provider as failover at priority 1."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        _service(
                            [
                                _entry("kimi-k2", priority=0, weight=90),
                                _entry("kimi-k2-next", priority=0, weight=10),
                                _entry("together", priority=1),
                            ]
                        )
                    )
                )
            ),
            required_resources=_required(
                gateways=[_gateway("eu", "gw-eu", address="34.56.129.3")],
                clusters=[_cluster("gw-eu", provider_config="gw-eu-pc")],
                **{
                    "endpoints-0": [
                        _endpoint(
                            "kimi-k2-eu-0",
                            origin="http://gw-eu.clusters.example.com",
                            model="ml-team/kimi-k2",
                            prefix="/ml-team/kimi-k2-eu-0/v1",
                            composed=True,
                        )
                    ],
                    "endpoints-1": [
                        _endpoint(
                            "kimi-next-eu-0",
                            origin="http://gw-eu.clusters.example.com",
                            model="ml-team/kimi-k2-next",
                            prefix="/ml-team/kimi-k2-next-eu-0/v1",
                            composed=True,
                        )
                    ],
                    "endpoints-2": [
                        _endpoint(
                            "together-kimi",
                            origin="https://api.together.xyz",
                            model="moonshotai/Kimi-K2-Instruct",
                            credential="together-api-key",
                        )
                    ],
                    "credential-together-kimi": [_secret("together-api-key", {"apiKey": "sk-together"})],
                },
            ),
        )
        got = await self._run(req)

        route = _manifest(got, "route-eu")
        rule = route["spec"]["rules"][0]
        self.assertEqual(
            rule["matches"],
            [{"headers": [{"type": "Exact", "name": "x-ai-eg-model", "value": _MODEL}]}],
            "Exact, because only exact matches appear in the gateway's /v1/models",
        )
        self.assertEqual(
            rule["backendRefs"],
            [
                {
                    "name": names.backend(_NS, _SVC, "kimi-k2-eu-0"),
                    "weight": 9,
                    "priority": 0,
                    "modelNameOverride": "ml-team/kimi-k2",
                },
                {
                    "name": names.backend(_NS, _SVC, "kimi-next-eu-0"),
                    "weight": 1,
                    "priority": 0,
                    "modelNameOverride": "ml-team/kimi-k2-next",
                },
                {
                    "name": names.backend(_NS, _SVC, "together-kimi"),
                    "weight": 1,
                    "priority": 1,
                    "modelNameOverride": "moonshotai/Kimi-K2-Instruct",
                },
            ],
            "90/10 reduces to 9/1 within priority 0; the provider sits alone at priority 1",
        )
        # Declaring costs is also what makes the gateway ask a backend for usage
        # on a streamed response. Without it streamed requests report no tokens
        # at all and the usage record is silently empty.
        self.assertEqual(
            route["spec"]["llmRequestCosts"],
            [
                {"metadataKey": "llm_input_token", "type": "InputToken"},
                {"metadataKey": "llm_output_token", "type": "OutputToken"},
                {"metadataKey": "llm_total_token", "type": "TotalToken"},
            ],
            "spelled out, because comparing this to the constant that produced it cannot fail",
        )
        self.assertIn(
            "streamIdleTimeout",
            rule,
            "without this a backend hanging before the first token never fails over",
        )

        # Every backend must be addressed by hostname. An address makes Envoy
        # Gateway emit an EDS cluster, where the model rewrite, host rewrite and
        # credential all silently stop applying.
        for ep in ("kimi-k2-eu-0", "kimi-next-eu-0", "together-kimi"):
            spec = _manifest(got, f"backend-eu-{ep}")["spec"]
            self.assertIn("fqdn", spec["endpoints"][0], f"{ep} is addressed by hostname")
            self.assertNotIn("ip", spec["endpoints"][0])

        self.assertEqual(
            _manifest(got, "backend-eu-together-kimi")["spec"],
            {
                "endpoints": [{"fqdn": {"hostname": "api.together.xyz", "port": 443}}],
                "tls": {"wellKnownCACertificates": "System", "sni": "api.together.xyz"},
            },
            "an https origin gets TLS originated to it, with SNI",
        )
        self.assertEqual(
            _manifest(got, "backend-eu-kimi-k2-eu-0")["spec"],
            {"endpoints": [{"fqdn": {"hostname": "gw-eu.clusters.example.com", "port": 80}}]},
        )
        self.assertEqual(
            _manifest(got, "aibackend-eu-kimi-k2-eu-0")["spec"]["schema"],
            {"name": "OpenAI", "prefix": "/ml-team/kimi-k2-eu-0/v1"},
            "the per-replica path its cluster gateway serves this replica on",
        )

        # A third-party backend must not be told which tenant is calling. Our
        # own endpoints keep the header, because the engine behind them is ours.
        self.assertEqual(
            _manifest(got, "aibackend-eu-together-kimi")["spec"]["headerMutation"],
            {"remove": ["x-modelplane-caller"]},
        )
        self.assertNotIn("headerMutation", _manifest(got, "aibackend-eu-kimi-k2-eu-0")["spec"])

        self.assertEqual(
            _manifest(got, "credential-eu-together-kimi")["data"],
            {"apiKey": base64.b64encode(b"sk-together").decode()},
            "republished under the key the AI Gateway reads, base64 copied verbatim",
        )
        self.assertEqual(
            _manifest(got, "credpolicy-eu-together-kimi")["spec"]["targetRefs"],
            [
                {
                    "group": "aigateway.envoyproxy.io",
                    "kind": "AIServiceBackend",
                    "name": names.backend(_NS, _SVC, "together-kimi"),
                }
            ],
        )
        self.assertNotIn("credpolicy-eu-kimi-k2-eu-0", got.desired.resources, "our own endpoints need no credential")

        self.assertEqual(
            resource.struct_to_dict(got.desired.composite.resource)["status"],
            {
                "model": _MODEL,
                "endpoints": {"total": 3, "ready": 3},
                "gateways": [{"name": "eu", "address": "34.56.129.3"}],
            },
        )

    async def test_fans_out_over_every_serving_gateway(self) -> None:
        """Two gateways serving one service each get their own route and their
        own copy of the backends, on their own cluster."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(_service([_entry("kimi-k2")])))
            ),
            required_resources=_required(
                gateways=[_gateway("eu", "gw-eu"), _gateway("us", "gw-us")],
                clusters=[
                    _cluster("gw-eu", provider_config="gw-eu-pc"),
                    _cluster("gw-us", provider_config="gw-us-pc"),
                ],
                **{"endpoints-0": [_endpoint("kimi-a", origin="https://a.example.com")]},
            ),
        )
        got = await self._run(req)
        self.assertEqual(
            sorted(got.desired.resources),
            [
                "aibackend-eu-kimi-a",
                "aibackend-us-kimi-a",
                "backend-eu-kimi-a",
                "backend-us-kimi-a",
                "route-eu",
                "route-us",
            ],
        )
        for key, pc in (("route-eu", "gw-eu-pc"), ("route-us", "gw-us-pc")):
            self.assertEqual(
                resource.struct_to_dict(got.desired.resources[key].resource)["spec"]["providerConfigRef"],
                {"kind": "ClusterProviderConfig", "name": pc},
            )

    async def test_service_selector_scopes_a_gateway(self) -> None:
        """Residency falls out of labels: a gateway scoped to a region serves
        only the services labelled for it, and an unlabelled gateway serves
        everything."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(_service([_entry("kimi-k2")], labels={"example.org/region": "eu"}))
                )
            ),
            required_resources=_required(
                gateways=[
                    _gateway("eu", "gw-eu", selector={"example.org/region": "eu"}),
                    _gateway("us", "gw-us", selector={"example.org/region": "us"}),
                    _gateway("any", "gw-any"),
                ],
                clusters=[
                    _cluster("gw-eu", provider_config="gw-eu-pc"),
                    _cluster("gw-us", provider_config="gw-us-pc"),
                    _cluster("gw-any", provider_config="gw-any-pc"),
                ],
                **{"endpoints-0": [_endpoint("kimi-a", origin="https://a.example.com")]},
            ),
        )
        got = await self._run(req)
        self.assertEqual(
            sorted(k for k in got.desired.resources if k.startswith("route-")),
            ["route-any", "route-eu"],
            "the us gateway's selector doesn't match, so it composes no route there",
        )

    async def test_weights_spread_within_a_tier(self) -> None:
        """An entry's weight is written once but applied per backend, so it is
        spread over however many endpoints the entry matched, and the ratio
        between entries survives."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(_service([_entry("big", weight=90), _entry("small", weight=10)]))
                )
            ),
            required_resources=_required(
                gateways=[_gateway("eu", "gw-eu")],
                clusters=[_cluster("gw-eu", provider_config="gw-eu-pc")],
                **{
                    "endpoints-0": [_endpoint(f"big-{i}", origin=f"https://big-{i}.example.com") for i in range(3)],
                    "endpoints-1": [_endpoint("small-0", origin="https://small-0.example.com")],
                },
            ),
        )
        got = await self._run(req)
        refs = _manifest(got, "route-eu")["spec"]["rules"][0]["backendRefs"]
        weights = [r["weight"] for r in refs]
        # 90 over three endpoints is 30 each, 10 over one is 10, then the whole
        # set is reduced by its greatest common divisor to the smallest
        # equivalent integers. The ratio is what matters, not the magnitude.
        self.assertEqual(weights, [3, 3, 3, 1])
        self.assertEqual(sum(weights[:3]) / sum(weights), 0.9)
        self.assertTrue(all(w > 0 for w in weights), "a backend weighted 0 is dropped, not deprioritised")

    async def test_a_weight_below_its_endpoint_count_still_gives_every_endpoint_traffic(self) -> None:
        """Weight 1 over five endpoints must not floor any of them to 0, which
        would drop them from the load assignment rather than sharing traffic."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(_service([_entry("many", weight=1)])))
            ),
            required_resources=_required(
                gateways=[_gateway("eu", "gw-eu")],
                clusters=[_cluster("gw-eu", provider_config="gw-eu-pc")],
                **{"endpoints-0": [_endpoint(f"many-{i}", origin=f"https://many-{i}.example.com") for i in range(5)]},
            ),
        )
        got = await self._run(req)
        weights = [r["weight"] for r in _manifest(got, "route-eu")["spec"]["rules"][0]["backendRefs"]]
        self.assertEqual(weights, [1, 1, 1, 1, 1])

    async def test_an_endpoint_matched_twice_belongs_to_the_first_entry(self) -> None:
        """Otherwise a canary entry and a catch-all entry would both weight the
        same endpoint, and its share would depend on entry order twice over."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        _service([_entry("kimi-k2", priority=0), _entry("kimi-k2", priority=1)])
                    )
                )
            ),
            required_resources=_required(
                gateways=[_gateway("eu", "gw-eu")],
                clusters=[_cluster("gw-eu", provider_config="gw-eu-pc")],
                **{
                    "endpoints-0": [_endpoint("kimi-a", origin="https://a.example.com")],
                    "endpoints-1": [_endpoint("kimi-a", origin="https://a.example.com")],
                },
            ),
        )
        got = await self._run(req)
        refs = _manifest(got, "route-eu")["spec"]["rules"][0]["backendRefs"]
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["priority"], 0)
        self.assertEqual(
            resource.struct_to_dict(got.desired.composite.resource)["status"]["endpoints"],
            {"total": 1, "ready": 1},
        )

    async def test_ready_once_every_route_is_applied(self) -> None:
        """Every gateway, not any: a service reachable through only some of the
        gateways that should serve it is worth surfacing."""
        base = {
            "gateways": [_gateway("eu", "gw-eu"), _gateway("us", "gw-us")],
            "clusters": [
                _cluster("gw-eu", provider_config="gw-eu-pc"),
                _cluster("gw-us", provider_config="gw-us-pc"),
            ],
        }
        observed_route = fnv1.Resource(
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
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(_service([_entry("kimi-k2")]))),
                resources={"route-eu": observed_route},
            ),
            required_resources=_required(
                **base, **{"endpoints-0": [_endpoint("kimi-a", origin="https://a.example.com")]}
            ),
        )
        got = await self._run(req)
        cond = next(iter(got.conditions))
        self.assertEqual(cond.reason, fn.CONDITION_REASON_WAITING_FOR_ROUTES)
        self.assertEqual(cond.message, "Waiting for routes on gateways: us")

        req.observed.resources["route-us"].CopyFrom(observed_route)
        got = await self._run(req)
        self.assertEqual(next(iter(got.conditions)).reason, fn.CONDITION_REASON_ROUTES_ACCEPTED)

    async def test_a_third_party_needing_no_key_still_loses_the_caller_header(self) -> None:
        """Whether Modelplane operates an endpoint decides whether the caller's
        identity travels to it, and that isn't the same question as whether the
        endpoint needs a credential. A provider authenticating by client
        certificate, or a free one, would otherwise be told which tenant is
        calling."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(_service([_entry("kimi-k2")])))
            ),
            required_resources=_required(
                gateways=[_gateway("eu", "gw-eu")],
                clusters=[_cluster("gw-eu", provider_config="gw-eu-pc")],
                # No credential, and not composed by Modelplane.
                **{"endpoints-0": [_endpoint("free-provider", origin="https://free.example.com")]},
            ),
        )
        got = await self._run(req)
        self.assertEqual(
            _manifest(got, "aibackend-eu-free-provider")["spec"]["headerMutation"],
            {"remove": ["x-modelplane-caller"]},
        )
        self.assertNotIn("credpolicy-eu-free-provider", got.desired.resources, "no credential means no policy")

    async def test_priorities_are_renumbered_without_gaps(self) -> None:
        """A ModelService's priorities are an ordering; Envoy's are levels it
        walks from 0. A user writing 0 and 5, or a tier whose endpoints all go
        unready during a roll, would otherwise leave gaps in what Envoy gets."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        _service([_entry("a", priority=0), _entry("b", priority=5), _entry("c", priority=9)])
                    )
                )
            ),
            required_resources=_required(
                gateways=[_gateway("eu", "gw-eu")],
                clusters=[_cluster("gw-eu", provider_config="gw-eu-pc")],
                **{
                    # The middle tier has no ready endpoint, so it drops out and
                    # must not leave a hole behind it.
                    "endpoints-0": [_endpoint("a-0", origin="https://a.example.com")],
                    "endpoints-1": [_endpoint("b-0", origin="https://b.example.com", ready=False)],
                    "endpoints-2": [_endpoint("c-0", origin="https://c.example.com")],
                },
            ),
        )
        got = await self._run(req)
        refs = _manifest(got, "route-eu")["spec"]["rules"][0]["backendRefs"]
        self.assertEqual(
            [(r["name"].rsplit("-", 1)[-1], r["priority"]) for r in refs],
            [("0", 0), ("0", 1)],
            "two tiers survive, renumbered 0 and 1",
        )

    async def test_a_composed_endpoint_gets_mutual_tls(self) -> None:
        """A cluster gateway's certificate is signed by its own cluster's CA, not
        a public one, and it refuses a request that arrives without a client
        certificate. Validating against the system trust store would fail, and
        omitting the client certificate would be refused, so a composed endpoint
        needs both halves or it carries no traffic at all."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(_service([_entry("kimi-k2")])))
            ),
            required_resources=_required(
                gateways=[_gateway("eu", "gw-eu")],
                clusters=[_cluster("gw-eu", provider_config="gw-eu-pc", ca="-----BEGIN CERTIFICATE-----\nx\n")],
                **{
                    "endpoints-0": [
                        _endpoint(
                            "kimi-eu-0",
                            origin="https://gw-eu.clusters.example.com",
                            model="ml-team/kimi-k2",
                            composed=True,
                        )
                    ]
                },
            ),
        )
        got = await self._run(req)
        self.assertEqual(
            _manifest(got, "backend-eu-kimi-eu-0")["spec"]["tls"],
            {
                "caCertificateRefs": [{"kind": "ConfigMap", "group": "", "name": "cluster-ca-gw-eu"}],
                "sni": "gw-eu.clusters.example.com",
                "clientCertificateRef": {"kind": "Secret", "group": "", "name": "fleet-gateway-client"},
            },
        )
        self.assertEqual(
            _manifest(got, "cluster-ca-eu-gw-eu")["data"],
            {"ca.crt": "-----BEGIN CERTIFICATE-----\nx\n"},
            "the cluster's CA is copied to the gateway's cluster so Envoy can read it",
        )

    async def test_a_provider_is_validated_against_the_system_store(self) -> None:
        """Pinning our own CA would reject a real provider, and presenting a
        client certificate to one would be meaningless."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(_service([_entry("together")])))
            ),
            required_resources=_required(
                gateways=[_gateway("eu", "gw-eu")],
                clusters=[_cluster("gw-eu", provider_config="gw-eu-pc", ca="-----BEGIN CERTIFICATE-----\nx\n")],
                **{"endpoints-0": [_endpoint("together", origin="https://api.together.xyz")]},
            ),
        )
        got = await self._run(req)
        self.assertEqual(
            _manifest(got, "backend-eu-together")["spec"]["tls"],
            {"wellKnownCACertificates": "System", "sni": "api.together.xyz"},
        )

    async def test_an_endpoint_whose_credential_vanished_is_dropped_not_fatal(self) -> None:
        """EndpointReady is written by another XR on its own reconcile loop, so
        between a credential Secret being deleted and that XR noticing, this
        function sees a ready endpoint with no credential. Raising there would
        withdraw the route from every gateway serving the service over one
        endpoint; the rest must keep serving."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(resource=resource.dict_to_struct(_service([_entry("a"), _entry("b")])))
            ),
            required_resources=_required(
                gateways=[_gateway("eu", "gw-eu")],
                clusters=[_cluster("gw-eu", provider_config="gw-eu-pc")],
                **{
                    "endpoints-0": [_endpoint("good", origin="https://a.example.com")],
                    # Ready, but its Secret resolved to nothing.
                    "endpoints-1": [_endpoint("gone", origin="https://b.example.com", credential="vanished")],
                    "credential-gone": [],
                },
            ),
        )
        got = await self._run(req)
        refs = _manifest(got, "route-eu")["spec"]["rules"][0]["backendRefs"]
        self.assertEqual([r["name"] for r in refs], [names.backend(_NS, _SVC, "good")])
        self.assertNotIn("backend-eu-gone", got.desired.resources)
        self.assertTrue(
            any("gone" in r.message for r in got.results),
            "the dropped endpoint is reported rather than silently omitted",
        )

    async def test_a_credential_secret_missing_its_key_is_dropped(self) -> None:
        """A Secret that exists but lacks the named key is the likelier mistake,
        and would otherwise reach the provider as an empty credential."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_service([_entry("a")])))),
            required_resources=_required(
                gateways=[_gateway("eu", "gw-eu")],
                clusters=[_cluster("gw-eu", provider_config="gw-eu-pc")],
                **{
                    "endpoints-0": [_endpoint("wrongkey", origin="https://a.example.com", credential="k")],
                    "credential-wrongkey": [_secret("k", {"token": "sk-1"})],
                },
            ),
        )
        got = await self._run(req)
        self.assertEqual(next(iter(got.conditions)).reason, fn.CONDITION_REASON_NO_ENDPOINTS)
        self.assertEqual(len(got.desired.resources), 0)


class TestNames(unittest.TestCase):
    def test_long_names_stay_within_the_limit_and_stay_unique(self) -> None:
        """A namespace, a service and an endpoint name can each be 253
        characters, so a joined name can overflow. Truncating alone would map
        two names onto one object."""
        long = "e" * 250
        a = names.backend("n" * 250, "s" * 250, long + "a")
        b = names.backend("n" * 250, "s" * 250, long + "b")
        self.assertLessEqual(len(a), 253)
        self.assertLessEqual(len(b), 253)
        self.assertNotEqual(a, b)
        self.assertEqual(a, names.backend("n" * 250, "s" * 250, long + "a"), "stable across calls")

    def test_the_model_a_caller_names_is_never_hashed(self) -> None:
        """It's a value in a request body, not an object name. Shortening it
        would make what a caller types depend on a hash."""
        self.assertEqual(names.model("n" * 250, "s" * 250), f"{'n' * 250}/{'s' * 250}")
