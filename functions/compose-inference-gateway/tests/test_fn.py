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

import asyncio
import base64
import dataclasses
import json

import pytest
from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format, message
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


def _xr(*, name: str = "eu", **spec) -> dict:  # noqa: ANN003
    """The InferenceGateway XR, built from the generated model so a field the
    XRD doesn't define can't creep into a test."""
    xr = v1alpha1.InferenceGateway(
        apiVersion="modelplane.ai/v1alpha1",
        kind="InferenceGateway",
        metadata={"name": name},
        spec=v1alpha1.Spec(clusterName=_CLUSTER, **spec),
    )
    return xr.model_dump(exclude_none=True, mode="json", by_alias=True)


def _api_key_auth() -> v1alpha1.Auth:
    """Caller auth by API key, against the Secrets _requirements(auth=True) selects."""
    return v1alpha1.Auth(
        method="APIKey",
        apiKey=v1alpha1.ApiKey(
            secretSelector=v1alpha1.SecretSelector(matchLabels={"modelplane.ai/inference-keys": "true"})
        ),
    )


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
        desired=fnv1.State(composite=fnv1.Resource(ready=fnv1.READY_FALSE)),
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


GATES_CASES = [
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
            required_resources=_required(clusters=[], gateways=[_gateway_xr("eu", _CLUSTER)]),
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
                clusters=[_cluster()],
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
                clusters=[_cluster(provider_config=None)], gateways=[_gateway_xr("eu", _CLUSTER)]
            ),
        ),
        want=_not_ready(
            fn.CONDITION_REASON_WAITING_FOR_CLUSTER,
            f"InferenceCluster {_CLUSTER} has not published a providerConfigRef",
            _requirements(),
        ),
    ),
]


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


@pytest.mark.parametrize("case", GATES_CASES, ids=lambda case: case.name)
def test_gates(case: Case) -> None:
    """Passes where the gateway can't be composed compose nothing, and say
    why. Asserting the whole response proves nothing is composed against a
    cluster we can't reach, rather than a subset being applied."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want)


def test_minimal_gateway() -> None:
    """A gateway with no TLS or auth: the getting-started shape.

    Composes the gateway objects and no auth policies, and reports no
    endpoints until the Gateway has an address.
    """
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_xr()))),
        required_resources=_required(clusters=[_cluster()], gateways=[_gateway_xr("eu", _CLUSTER)]),
    )
    got = asyncio.run(fn.FunctionRunner().RunFunction(req, None))

    assert sorted(got.desired.resources) == sorted(
        [
            # The CA whose client certificates a cluster gateway trusts,
            # published as a ClusterIssuer for compose-model-route to issue
            # per-namespace client certificates from.
            "client-ca-certificate",
            "client-ca-issuer",
            "client-ca-bundle",
            "client-ca-configmap",
            "client-selfsigned-issuer",
            "client-traffic-policy",
            "envoy-proxy",
            "failover-policy",
            "gateway",
            "healthz-filter",
            "healthz-route",
        ]
    ), "composes the gateway objects and its client PKI, and no caller auth"
    for key, res in got.desired.resources.items():
        d = resource.struct_to_dict(res.resource)
        assert d["kind"] == "Object", f"{key} targets the gateway's cluster"
        assert d["spec"]["providerConfigRef"] == {"kind": "ClusterProviderConfig", "name": _PC}, (
            f"{key} uses the cluster's ClusterProviderConfig"
        )
        # An InferenceGateway is cluster-scoped, and Crossplane only
        # defaults a composed namespaced resource's namespace from a
        # namespaced composite. Without this every reconcile fails with
        # "an empty namespace may not be set when a resource name is
        # provided" and nothing is composed at all.
        assert d["metadata"]["namespace"] == fn.CONTROL_PLANE_NAMESPACE, (
            f"{key} sets its own namespace, which a cluster-scoped XR must"
        )
        manifest = d["spec"]["forProvider"]["manifest"]
        if manifest["kind"] == "ClusterIssuer":
            # Cluster-scoped: compose-model-route issues client certs from it
            # into team namespaces, so it has no namespace of its own.
            assert "namespace" not in manifest["metadata"], f"{key} is cluster-scoped, so it sets no namespace"
            continue
        if manifest["kind"] == "Bundle":
            # A Bundle is cluster-scoped, so it has no namespace of its own.
            # It picks the namespace it syncs its ConfigMap to by selector.
            assert "namespace" not in manifest["metadata"], f"{key} is cluster-scoped, so it sets no namespace"
            assert manifest["spec"]["target"]["namespaceSelector"] == {
                "matchLabels": {"kubernetes.io/metadata.name": fn.REMOTE_NAMESPACE}
            }, f"{key} syncs only to the remote namespace"
            continue
        assert manifest["metadata"]["namespace"] == fn.REMOTE_NAMESPACE, f"{key} lands in the remote namespace"

    # Two attempts per priority, so a retry tries another endpoint at the
    # same priority before moving down. At one, a single transient failure
    # on one replica would send the request to the next priority, which may
    # be a paid provider.
    failover = resource.struct_to_dict(got.desired.resources["failover-policy"].resource)
    assert failover["spec"]["forProvider"]["manifest"]["spec"]["retry"] == {
        "numAttemptsPerPriority": 2,
        "numRetries": 3,
        "retryOn": {
            # retriable-status-codes has to be present for the status
            # codes below to do anything: Envoy Gateway replaces retry_on
            # wholesale with this list, and Envoy only consults
            # retriable_status_codes when retry_on names it. Without it a
            # provider answering 503 or 429 is never retried, which is
            # the case failover exists for.
            "triggers": [
                "connect-failure",
                "refused-stream",
                "reset",
                "retriable-status-codes",
            ],
            # 429 so a rate-limited provider's traffic overflows to
            # another endpoint rather than failing back to the caller.
            "httpStatusCodes": [429, 503],
        },
    }
    # Panic mode defaults to 50%, above which Envoy ignores health and
    # spreads traffic over every endpoint including the ejected ones. Every
    # endpoint of a ModelService shares one cluster, so ejecting a whole
    # priority tier usually crosses it and failover stops working.
    #
    # Asserted on the whole healthCheck, because panicThreshold is a sibling
    # of passive rather than a field inside it, and nested wrongly the API
    # server prunes it while the policy still applies.
    assert failover["spec"]["forProvider"]["manifest"]["spec"]["healthCheck"] == {
        "passive": {
            "baseEjectionTime": "30s",
            "consecutive5XxErrors": 5,
            "interval": "5s",
            "maxEjectionPercent": 100,
        },
        "panicThreshold": 0,
    }
    assert failover["spec"]["forProvider"]["manifest"]["spec"]["targetRefs"] == [
        {"group": "gateway.networking.k8s.io", "kind": "Gateway", "name": fn._GATEWAY_NAME}
    ], "targets the Gateway, so it covers every ModelService's route"

    # AI Gateway buffers whole bodies, and Envoy Gateway's 32KiB default
    # buffer limit answers 413 to a long prompt or non-streamed completion.
    assert resource.struct_to_dict(got.desired.resources["client-traffic-policy"].resource)["spec"]["forProvider"][
        "manifest"
    ] == {
        "apiVersion": "gateway.envoyproxy.io/v1alpha1",
        "kind": "ClientTrafficPolicy",
        "metadata": {"name": "inference-gateway-client-traffic", "namespace": "modelplane-system"},
        "spec": {
            "targetRefs": [{"group": "gateway.networking.k8s.io", "kind": "Gateway", "name": "inference-gateway"}],
            "connection": {"bufferLimit": "50Mi"},
            "http2": {"initialStreamWindowSize": "16Mi", "initialConnectionWindowSize": "24Mi"},
        },
    }

    # The token fields must read request metadata, not the response body or
    # a header. The caller header is stripped before a third-party backend
    # sees it, so a log reading the header loses the caller on exactly the
    # records that attribute provider spend.
    log = resource.struct_to_dict(got.desired.resources["envoy-proxy"].resource)
    fields = log["spec"]["forProvider"]["manifest"]["spec"]["telemetry"]["accessLog"]["settings"][0]["format"]["json"]
    # Two proxy pods spread softly across nodes and zones, a disruption
    # budget so a drain can't evict both, and ndots:1. Without ndots:1 every
    # backend hostname is resolved against each of the pod's search domains
    # first, since they all have fewer than five dots. A cluster whose
    # upstream resolver is slow then stalls resolution, and Envoy answers 503
    # with nothing but DNS timeouts to show for it.
    proxy_labels = {
        "gateway.envoyproxy.io/owning-gateway-name": "inference-gateway",
        "gateway.envoyproxy.io/owning-gateway-namespace": "modelplane-system",
    }
    assert log["spec"]["forProvider"]["manifest"]["spec"]["provider"] == {
        "type": "Kubernetes",
        "kubernetes": {
            "envoyService": {"externalTrafficPolicy": "Cluster"},
            "envoyDeployment": {
                "replicas": 2,
                "patch": {"type": "StrategicMerge", "value": fn._NDOTS_PATCH},
                "pod": {
                    "topologySpreadConstraints": [
                        {
                            "maxSkew": 1,
                            "topologyKey": "kubernetes.io/hostname",
                            "whenUnsatisfiable": "ScheduleAnyway",
                            "labelSelector": {"matchLabels": proxy_labels},
                        },
                        {
                            "maxSkew": 1,
                            "topologyKey": "topology.kubernetes.io/zone",
                            "whenUnsatisfiable": "ScheduleAnyway",
                            "labelSelector": {"matchLabels": proxy_labels},
                        },
                    ]
                },
            },
            "envoyPDB": {"maxUnavailable": 1},
        },
    }
    # A stopping pod drains for as long as a request may run by default, so
    # a restart doesn't cut off streams in flight.
    assert log["spec"]["forProvider"]["manifest"]["spec"]["shutdown"] == {"drainTimeout": "300s"}
    assert fn._NDOTS_PATCH["spec"]["template"]["spec"]["dnsConfig"]["options"] == [{"name": "ndots", "value": "1"}]

    assert fields["caller"] == "%DYNAMIC_METADATA(io.envoy.ai_gateway:caller)%"
    assert fields["input_tokens"] == "%DYNAMIC_METADATA(io.envoy.ai_gateway:llm_input_token)%"
    assert fields["output_tokens"] == "%DYNAMIC_METADATA(io.envoy.ai_gateway:llm_output_token)%"

    gw = resource.struct_to_dict(got.desired.resources["gateway"].resource)
    manifest = gw["spec"]["forProvider"]["manifest"]
    assert manifest["spec"]["listeners"] == [
        {
            "name": "http",
            "protocol": "HTTP",
            "port": 80,
            "allowedRoutes": {
                "namespaces": {
                    "from": "Selector",
                    "selector": {"matchExpressions": [{"key": "modelplane.ai/namespace", "operator": "Exists"}]},
                }
            },
        }
    ], "one HTTP listener, no hostname, accepting routes from the mirrored namespaces"
    assert manifest["spec"]["infrastructure"]["parametersRef"] == {
        "group": "gateway.envoyproxy.io",
        "kind": "EnvoyProxy",
        "name": fn._GATEWAY_NAME,
    }, "its own EnvoyProxy, not the GatewayClass's"
    assert resource.struct_to_dict(got.desired.composite.resource).get("status") == {}, (
        "nothing to report until the Gateway has an address"
    )


def test_full_gateway() -> None:
    """A gateway with TLS and auth, whose Gateway has an address.

    Checks the things a caller depends on: the HTTPS listener, the Secrets
    copied to the cluster, the caller policy naming them, /healthz exempted
    from that policy, and a status publishing no URLs, since a caller
    reaches a TLS gateway on a DNS name only its owner knows.
    """
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(
                resource=resource.dict_to_struct(
                    _xr(
                        tls=v1alpha1.Tls(certificateRefs=[v1alpha1.CertificateRef(name="eu-tls-0")]),
                        auth=_api_key_auth(),
                    )
                )
            ),
            resources={
                "gateway": _observed_gateway(_ADDRESS, ready=True),
                "caller-auth": _observed_accepted(),
            },
        ),
        required_resources=_required(
            clusters=[_cluster()],
            gateways=[_gateway_xr("eu", _CLUSTER)],
            **{
                "caller-secrets": [_secret("ml-team-keys", {"ml-team-assistant": "sk-mp-a1b2c3"})],
                "tls-secret-0": [_secret("eu-tls-0", {"tls.crt": "cert", "tls.key": "key"})],
            },
        ),
    )
    got = asyncio.run(fn.FunctionRunner().RunFunction(req, None))

    assert sorted(got.desired.resources) == [
        "caller-auth",
        "caller-secret-ml-team-keys",
        "client-ca-bundle",
        "client-ca-certificate",
        "client-ca-configmap",
        "client-ca-issuer",
        "client-selfsigned-issuer",
        "client-traffic-policy",
        "envoy-proxy",
        "failover-policy",
        "gateway",
        "healthz-auth",
        "healthz-filter",
        "healthz-route",
        "redirect-auth",
        "redirect-route",
        "tls-secret-eu-tls-0",
    ]

    def manifest(key: str) -> dict:
        return resource.struct_to_dict(got.desired.resources[key].resource)["spec"]["forProvider"]["manifest"]

    assert manifest("gateway")["spec"]["listeners"][1] == {
        "name": "https",
        "protocol": "HTTPS",
        "port": 443,
        "tls": {"mode": "Terminate", "certificateRefs": [{"name": "eu-tls-0"}]},
        "allowedRoutes": {
            "namespaces": {
                "from": "Selector",
                "selector": {"matchExpressions": [{"key": "modelplane.ai/namespace", "operator": "Exists"}]},
            }
        },
    }
    assert _to_dict(got.requirements) == _to_dict(_requirements(auth=True, tls=1))
    assert manifest("tls-secret-eu-tls-0") == {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "eu-tls-0", "namespace": fn.REMOTE_NAMESPACE},
        "type": "kubernetes.io/tls",
        "data": {
            "tls.crt": base64.b64encode(b"cert").decode(),
            "tls.key": base64.b64encode(b"key").decode(),
        },
    }, "the certificate is copied verbatim, keeping the name the Gateway refers to it by"
    assert manifest("caller-auth")["spec"]["apiKeyAuth"] == {
        "credentialRefs": [{"name": "callers-ml-team-keys"}],
        # Authorization for OpenAI clients, x-api-key for Anthropic ones.
        "extractFrom": [{"headers": ["Authorization", "x-api-key"]}],
        "forwardClientIDHeader": fn._CALLER_HEADER,
        "sanitize": True,
    }
    assert manifest("healthz-auth")["spec"] == {
        "targetRefs": [{"group": "gateway.networking.k8s.io", "kind": "HTTPRoute", "name": fn._HEALTHZ_NAME}],
        "authorization": {"defaultAction": "Allow"},
    }, "/healthz overrides the Gateway-level policy so a health check needs no credential"
    # Inference binds to the HTTPS listener alone, so :80 carries only
    # /healthz and this catch-all redirect to it. /healthz is an Exact match,
    # so it still answers a plain-HTTP health check.
    assert manifest("healthz-route")["spec"]["parentRefs"] == [
        {
            "group": "gateway.networking.k8s.io",
            "kind": "Gateway",
            "name": fn._GATEWAY_NAME,
            "sectionName": "http",
        }
    ]
    assert manifest("redirect-route")["spec"] == {
        "parentRefs": [
            {
                "group": "gateway.networking.k8s.io",
                "kind": "Gateway",
                "name": fn._GATEWAY_NAME,
                "sectionName": "http",
            }
        ],
        "rules": [
            {
                "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                "filters": [{"type": "RequestRedirect", "requestRedirect": {"scheme": "https", "statusCode": 301}}],
            }
        ],
    }
    assert manifest("redirect-auth")["spec"] == {
        "targetRefs": [{"group": "gateway.networking.k8s.io", "kind": "HTTPRoute", "name": fn._REDIRECT_NAME}],
        "authorization": {"defaultAction": "Allow"},
    }, "the redirect must happen before auth, or an unauthenticated caller gets 401 instead of being sent to HTTPS"
    assert resource.struct_to_dict(got.desired.composite.resource)["status"] == {"address": _ADDRESS}
    assert [_to_dict(c) for c in got.conditions] == [
        _to_dict(
            fnv1.Condition(
                type=fn.CONDITION_TYPE_GATEWAY_READY,
                status=fnv1.STATUS_CONDITION_TRUE,
                reason=fn.CONDITION_REASON_GATEWAY_PROGRAMMED,
            )
        )
    ]


def test_endpoints_are_built_from_the_address() -> None:
    """A gateway serving plain HTTP publishes URLs on its address, which is
    something a caller can actually put in an SDK's base_url."""
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(resource=resource.dict_to_struct(_xr())),
            resources={"gateway": _observed_gateway(_ADDRESS, ready=False)},
        ),
        required_resources=_required(clusters=[_cluster()], gateways=[_gateway_xr("eu", _CLUSTER)]),
    )
    got = asyncio.run(fn.FunctionRunner().RunFunction(req, None))
    assert resource.struct_to_dict(got.desired.composite.resource)["status"] == {
        "address": _ADDRESS,
        "endpoints": {
            "openAI": f"http://{_ADDRESS}/v1",
            "anthropic": f"http://{_ADDRESS}/anthropic/v1",
        },
    }
    assert next(iter(got.conditions)).reason == fn.CONDITION_REASON_WAITING_FOR_GATEWAY, (
        "an address alone isn't readiness; the Gateway must be programmed"
    )


def test_an_ipv6_address_is_bracketed_in_the_endpoints() -> None:
    """A bare IPv6 literal collides with the port separator in a URL, so an
    SDK given http://2001:db8::1/v1 as a base_url can't use it."""
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(resource=resource.dict_to_struct(_xr())),
            resources={"gateway": _observed_gateway("2001:db8::1", ready=False)},
        ),
        required_resources=_required(clusters=[_cluster()], gateways=[_gateway_xr("eu", _CLUSTER)]),
    )
    got = asyncio.run(fn.FunctionRunner().RunFunction(req, None))
    assert resource.struct_to_dict(got.desired.composite.resource)["status"]["endpoints"] == {
        "openAI": "http://[2001:db8::1]/v1",
        "anthropic": "http://[2001:db8::1]/anthropic/v1",
    }


def test_resolves_each_cluster_gateway_name() -> None:
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
            gateways=[_gateway_xr("eu", _CLUSTER)],
            clusters=[
                _cluster(),  # this gateway's own cluster, no gateway published yet
                _cluster_with_gateway("prod-ipv4", address="203.0.113.7", hostname=ipv4),
                _cluster_with_gateway("prod-ipv6", address="2001:db8::1", hostname=ipv6),
                _cluster_with_gateway("prod-dns", address="lb-x.elb.amazonaws.com", hostname=dns),
            ],
        ),
    )
    got = asyncio.run(fn.FunctionRunner().RunFunction(req, None))

    resolvers = {
        key: resource.struct_to_dict(res.resource)
        for key, res in got.desired.resources.items()
        if key.startswith("cluster-name")
    }
    for key, obj in resolvers.items():
        assert obj["spec"]["providerConfigRef"] == {"kind": "ClusterProviderConfig", "name": _PC}, (
            f"{key} is composed against this gateway's own cluster"
        )
    manifests = {key: obj["spec"]["forProvider"]["manifest"] for key, obj in resolvers.items()}
    assert manifests == {
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
    }, (
        "IP clusters get a headless Service + EndpointSlice, the hostname cluster an ExternalName, "
        "and the own cluster with nothing published gets neither"
    )


def test_certificate_common_names_fit_the_x509_limit() -> None:
    """A long gateway name must not push a certificate commonName past the
    64-byte X.509 limit, which cert-manager's webhook rejects. A gateway name
    is a cluster-scoped resource name, so it can be up to 253 characters."""
    long_name = "g" + "a" * 62
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_xr(name=long_name)))),
        required_resources=_required(clusters=[_cluster()], gateways=[_gateway_xr(long_name, _CLUSTER)]),
    )
    got = asyncio.run(fn.FunctionRunner().RunFunction(req, None))
    manifest = resource.struct_to_dict(got.desired.resources["client-ca-certificate"].resource)["spec"]["forProvider"][
        "manifest"
    ]
    cn = manifest["spec"]["commonName"]
    assert len(cn.encode()) <= 64, "client-ca-certificate commonName exceeds the 64-byte X.509 limit"


def test_a_rejected_caller_policy_is_not_ready() -> None:
    """A gateway whose caller policy was rejected refuses every request with
    a 500 while its Gateway still has an address. Envoy Gateway rejects the
    policy when two selected Secrets share a key value, so this is reachable
    by writing two Secrets."""
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(resource=resource.dict_to_struct(_xr(auth=_api_key_auth()))),
            # The Gateway is programmed; the policy is not accepted.
            resources={"gateway": _observed_gateway(_ADDRESS, ready=True)},
        ),
        required_resources=_required(
            clusters=[_cluster()],
            gateways=[_gateway_xr("eu", _CLUSTER)],
            **{"caller-secrets": [_secret("ml-team-keys", {"a": "sk-1"})]},
        ),
    )
    got = asyncio.run(fn.FunctionRunner().RunFunction(req, None))
    cond = next(iter(got.conditions))
    assert cond.status == fnv1.STATUS_CONDITION_FALSE
    assert cond.reason == fn.CONDITION_REASON_AUTH_NOT_ACCEPTED


SHARED_CALLER_KEY_CASES = [
    (
        "two Secrets share a key",
        [_secret("team-a-keys", {"a": "sk-1"}), _secret("team-b-keys", {"b": "sk-1"})],
        fnv1.Condition(
            type=fn.CONDITION_TYPE_GATEWAY_READY,
            status=fnv1.STATUS_CONDITION_FALSE,
            reason=fn.CONDITION_REASON_AUTH_NOT_ACCEPTED,
            message="Caller team-b-keys/b has the same key as team-a-keys/a, so Envoy Gateway rejects the "
            "caller authentication policy and every request is refused",
        ),
    ),
    (
        "one Secret shares a key",
        [_secret("team-a-keys", {"a": "sk-1", "z": "sk-1"})],
        fnv1.Condition(
            type=fn.CONDITION_TYPE_GATEWAY_READY,
            status=fnv1.STATUS_CONDITION_FALSE,
            reason=fn.CONDITION_REASON_AUTH_NOT_ACCEPTED,
            message="Caller team-a-keys/z has the same key as team-a-keys/a, so Envoy Gateway rejects the "
            "caller authentication policy and every request is refused",
        ),
    ),
    (
        # Listed out of order. Walked in name order, team-a's x is seen
        # first, so team-b's x is skipped and its y repeats x's key.
        # Walked as listed, team-a's x would be the skipped one.
        "Secrets are walked in name order",
        [_secret("team-b-keys", {"x": "sk-2", "y": "sk-1"}), _secret("team-a-keys", {"x": "sk-1"})],
        fnv1.Condition(
            type=fn.CONDITION_TYPE_GATEWAY_READY,
            status=fnv1.STATUS_CONDITION_FALSE,
            reason=fn.CONDITION_REASON_AUTH_NOT_ACCEPTED,
            message="Caller team-b-keys/y has the same key as team-a-keys/x, so Envoy Gateway rejects the "
            "caller authentication policy and every request is refused",
        ),
    ),
    (
        "a repeated caller name is skipped, whatever its key",
        [_secret("team-a-keys", {"a": "sk-1"}), _secret("team-b-keys", {"a": "sk-1", "b": "sk-2"})],
        fnv1.Condition(
            type=fn.CONDITION_TYPE_GATEWAY_READY,
            status=fnv1.STATUS_CONDITION_TRUE,
            reason=fn.CONDITION_REASON_GATEWAY_PROGRAMMED,
        ),
    ),
]


@pytest.mark.parametrize("case", SHARED_CALLER_KEY_CASES, ids=lambda case: case[0])
def test_a_shared_caller_key_is_not_ready_before_the_policy_is_observed(
    case: tuple[str, list[dict], fnv1.Condition],
) -> None:
    """Envoy Gateway rejects the caller policy when two callers share a key,
    but the policy's Object still reads as accepted until provider-kubernetes
    next observes it. The gateway reports the outage from the Secrets
    themselves, and skips a repeated caller name before comparing its key,
    as Envoy Gateway does."""
    _, secrets, want = case
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(resource=resource.dict_to_struct(_xr(auth=_api_key_auth()))),
            resources={
                "gateway": _observed_gateway(_ADDRESS, ready=True),
                "caller-auth": _observed_accepted(),
            },
        ),
        required_resources=_required(
            clusters=[_cluster()],
            gateways=[_gateway_xr("eu", _CLUSTER)],
            **{"caller-secrets": secrets},
        ),
    )
    got = asyncio.run(fn.FunctionRunner().RunFunction(req, None))
    assert [_to_dict(c) for c in got.conditions] == [_to_dict(want)]


def test_caller_secrets_are_listed_in_name_order() -> None:
    """Envoy Gateway keeps the first Secret listed when two hold the same
    caller name, so the policy lists them by name rather than in the order
    they resolved in, and the winner doesn't change between reconciles."""
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_xr(auth=_api_key_auth())))),
        required_resources=_required(
            clusters=[_cluster()],
            gateways=[_gateway_xr("eu", _CLUSTER)],
            **{
                "caller-secrets": [
                    _secret("team-b-keys", {"b": "sk-2"}),
                    _secret("team-a-keys", {"a": "sk-1"}),
                ]
            },
        ),
    )
    got = asyncio.run(fn.FunctionRunner().RunFunction(req, None))
    policy = resource.struct_to_dict(got.desired.resources["caller-auth"].resource)
    assert policy["spec"]["forProvider"]["manifest"]["spec"]["apiKeyAuth"]["credentialRefs"] == [
        {"name": "callers-team-a-keys"},
        {"name": "callers-team-b-keys"},
    ]


MISSING_CALLER_SECRET_CASES = [
    (
        "the selector matches no Secret",
        {"caller-secrets": []},
        "spec.auth.apiKey.secretSelector matches no Secret, so no caller could authenticate",
    ),
    (
        "the caller Secrets have not resolved yet",
        {},
        "Waiting for caller key Secrets to resolve",
    ),
]


@pytest.mark.parametrize("case", MISSING_CALLER_SECRET_CASES, ids=lambda case: case[0])
def test_a_missing_caller_secret_denies_but_keeps_the_gateway(case: tuple[str, dict, str]) -> None:
    """Auth is asked for but no caller Secret has resolved. The Gateway is
    still composed, so its load balancer and address survive, and its caller
    policy denies every request rather than leaving the door open. Two states
    reach this, the selector matching no Secret and the requirement not having
    resolved yet, differing only in the reason reported."""
    _, extra, want_message = case
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_xr(auth=_api_key_auth())))),
        required_resources=_required(clusters=[_cluster()], gateways=[_gateway_xr("eu", _CLUSTER)], **extra),
    )
    got = asyncio.run(fn.FunctionRunner().RunFunction(req, None))

    assert "gateway" in got.desired.resources, "the Gateway is kept, so its address survives"
    assert not any(key.startswith("caller-secret-") for key in got.desired.resources), (
        "no caller Secret resolved, so none is copied to the cluster"
    )
    spec = resource.struct_to_dict(got.desired.resources["caller-auth"].resource)["spec"]["forProvider"]["manifest"][
        "spec"
    ]
    assert spec == {
        "targetRefs": [{"group": "gateway.networking.k8s.io", "kind": "Gateway", "name": fn._GATEWAY_NAME}],
        "authorization": {"defaultAction": "Deny"},
    }, "with no caller key the policy denies every request rather than authenticating nobody by omission"
    cond = next(iter(got.conditions))
    assert cond.status == fnv1.STATUS_CONDITION_FALSE
    assert cond.reason == fn.CONDITION_REASON_SECRETS_MISSING
    assert cond.message == want_message


def test_a_missing_tls_secret_keeps_the_gateway() -> None:
    """A referenced TLS Secret hasn't resolved. The Gateway is still composed,
    so its address survives; the HTTPS listener is left without a certificate
    on the cluster until the Secret appears, rather than the whole Gateway
    withdrawn and its load balancer moved."""
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(
                resource=resource.dict_to_struct(
                    _xr(tls=v1alpha1.Tls(certificateRefs=[v1alpha1.CertificateRef(name="eu-tls-0")]))
                )
            )
        ),
        required_resources=_required(
            clusters=[_cluster()], gateways=[_gateway_xr("eu", _CLUSTER)], **{"tls-secret-0": []}
        ),
    )
    got = asyncio.run(fn.FunctionRunner().RunFunction(req, None))

    assert "gateway" in got.desired.resources, "the Gateway is kept, so its address survives"
    assert "tls-secret-eu-tls-0" not in got.desired.resources, "the missing Secret isn't copied to the cluster"
    listeners = resource.struct_to_dict(got.desired.resources["gateway"].resource)["spec"]["forProvider"]["manifest"][
        "spec"
    ]["listeners"]
    assert [ln["name"] for ln in listeners] == ["http", "https"], "the HTTPS listener is still declared"
    cond = next(iter(got.conditions))
    assert cond.status == fnv1.STATUS_CONDITION_FALSE
    assert cond.reason == fn.CONDITION_REASON_SECRETS_MISSING
    assert cond.message == "Waiting for TLS Secrets: eu-tls-0"


def test_the_incumbent_keeps_its_cluster() -> None:
    """A gateway created later must not take a cluster off one already
    serving traffic. Doing so would delete the incumbent's Gateway and bring
    its load balancer back on a different address."""
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
            clusters=[_cluster()],
            gateways=[
                _gateway_xr("aaa", _CLUSTER),
                {**_gateway_xr("zzz", _CLUSTER), "status": {"address": _ADDRESS}},
            ],
        ),
    )
    got = asyncio.run(fn.FunctionRunner().RunFunction(req, None))
    assert len(got.desired.resources) == 0, "the newcomer composes nothing"
    cond = next(iter(got.conditions))
    assert cond.reason == fn.CONDITION_REASON_CLUSTER_TAKEN
    assert "zzz" in cond.message


def test_no_composed_object_observes_a_secret() -> None:
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
                        tls={"certificateRefs": [{"name": "eu-tls-0"}]},
                        auth={"method": "APIKey", "apiKey": {"secretSelector": {"matchLabels": {"team": "ml"}}}},
                    )
                )
            ),
        ),
        required_resources=_required(
            clusters=[_cluster()],
            gateways=[_gateway_xr("eu", _CLUSTER)],
            **{
                "caller-secrets": [_secret("ml-team-keys", {"alice": "key"})],
                "tls-secret-0": [_secret("eu-tls-0", {"tls.crt": "cert", "tls.key": "key"})],
            },
        ),
    )
    got = asyncio.run(fn.FunctionRunner().RunFunction(req, None))

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
    assert observed_secrets == [], "these observe a Secret, so its private keys reach the control plane"
    assert composed_secrets != [], "no Secret composed, so the assertion above proves nothing"


def test_client_pki_publishes_the_ca_without_its_key() -> None:
    """The client CA's certificate reaches the control plane through a
    trust-manager Bundle, which copies one named key into a ConfigMap, rather
    than through the Secret that also holds the private key."""
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_xr()))),
        required_resources=_required(clusters=[_cluster()], gateways=[_gateway_xr("eu", _CLUSTER)]),
    )
    got = asyncio.run(fn.FunctionRunner().RunFunction(req, None))

    def manifest(key: str) -> dict:
        return resource.struct_to_dict(got.desired.resources[key].resource)["spec"]["forProvider"]["manifest"]

    assert manifest("client-ca-bundle") == {
        "apiVersion": "trust.cert-manager.io/v1alpha1",
        "kind": "Bundle",
        "metadata": {"name": "inference-gateway-ca"},
        "spec": {
            "sources": [{"secret": {"name": "inference-gateway-ca", "key": "ca.crt"}}],
            "target": {
                "configMap": {"key": "ca.crt"},
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "modelplane-system"}},
            },
        },
    }
    # Named after the Bundle, because that's the ConfigMap a Bundle syncs.
    assert manifest("client-ca-configmap") == {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "inference-gateway-ca", "namespace": "modelplane-system"},
    }
    assert resource.struct_to_dict(got.desired.resources["client-ca-configmap"].resource)["spec"][
        "managementPolicies"
    ] == ["Observe"], "trust-manager owns this ConfigMap; Crossplane must not write it"


def test_client_ca_published_from_the_observed_configmap() -> None:
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
        required_resources=_required(clusters=[_cluster()], gateways=[_gateway_xr("eu", _CLUSTER)]),
    )
    got = asyncio.run(fn.FunctionRunner().RunFunction(req, None))

    assert (
        resource.struct_to_dict(got.desired.composite.resource)["status"]["clientCACertificate"]
        == "-----BEGIN CERTIFICATE-----\nclient\n"
    )


def test_no_client_ca_before_the_bundle_syncs() -> None:
    """With no observed ConfigMap the gateway publishes no CA, so no cluster
    trusts it yet and no cluster publishes a hostname on its account."""
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(resource=resource.dict_to_struct(_xr())),
            resources={"gateway": _observed_gateway("gw.example.org", ready=True)},
        ),
        required_resources=_required(clusters=[_cluster()], gateways=[_gateway_xr("eu", _CLUSTER)]),
    )
    got = asyncio.run(fn.FunctionRunner().RunFunction(req, None))

    assert "clientCACertificate" not in resource.struct_to_dict(got.desired.composite.resource)["status"]
