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

"""Test that the InferenceGateway serves ml-team/mock, and the cluster gateway guards it.

Every request goes from a pod, because the gateways' addresses are on the kind
Docker network. See conftest.py for the fixtures.
"""

import pytest

from e2e import gateway, kube, wait

# OpenAI requests send the caller's key as a bearer token.
AUTHORIZED = {"authorization": f"Bearer {gateway.CALLER_KEY}"}


def test_chat_completion_succeeds(serving: gateway.Serving, control_plane_client: gateway.Client) -> None:
    """An OpenAI chat completion naming the ModelService returns 200."""
    r = control_plane_client.request(
        f"{serving.openai}/chat/completions",
        AUTHORIZED,
        {"model": serving.model, "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status == 200, r


@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer sk-wrong"}], ids=["no key", "a key no Secret holds"])
def test_chat_completion_without_a_valid_key_is_refused(
    routed: gateway.Serving, control_plane_client: gateway.Client, headers: dict[str, str]
) -> None:
    """The gateway refuses a caller that presents no key, or one it doesn't know."""
    r = control_plane_client.request(
        f"{routed.openai}/chat/completions",
        headers,
        {"model": routed.model, "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status == 401, r


def test_response_reports_the_served_model(serving: gateway.Serving, control_plane_client: gateway.Client) -> None:
    """The response names the model the engine serves, not the ModelService the caller asked for.

    The engine answers only to the name Modelplane started it under, and refuses
    anything else with a 404. So a 200 already shows the gateway rewrote the
    caller's ModelService name to the deployment's. This asserts the visible
    half of the same mechanism.
    """
    r = control_plane_client.request(
        f"{serving.openai}/chat/completions",
        AUTHORIZED,
        {"model": serving.model, "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status == 200, r
    assert r.json()["model"] == "ml-team/mock-demo"


def test_unclaimed_model_is_not_routed(serving: gateway.Serving, control_plane_client: gateway.Client) -> None:
    """A model no ModelService claims routes nowhere.

    This catches a route that matches too broadly, which would send a caller
    to an arbitrary backend. A 404 only means that once the claimed name
    serves, so this waits for it.
    """
    r = control_plane_client.request(
        f"{serving.openai}/chat/completions",
        AUTHORIZED,
        {"model": "ml-team/nope", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status == 404, r


def test_models_lists_the_model_service(serving: gateway.Serving, control_plane_client: gateway.Client) -> None:
    """/v1/models lists the ModelService.

    It lists only models a route matches exactly, so this also shows the route
    matches the name exactly rather than by pattern.
    """
    r = control_plane_client.request(f"{serving.openai}/models", AUTHORIZED)
    assert r.status == 200, r
    assert serving.model in [m["id"] for m in r.json()["data"]], r


def test_anthropic_message_succeeds(serving: gateway.Serving, control_plane_client: gateway.Client) -> None:
    """An Anthropic Messages API request returns 200.

    The endpoint's API is OpenAI, so the gateway translates the request. The
    mock serves /v1/messages too, the way vLLM does, so a 200 alone doesn't
    tell translation from passthrough. Anthropic clients send the key in
    x-api-key.
    """
    r = control_plane_client.request(
        f"{serving.anthropic}/messages",
        {"x-api-key": gateway.CALLER_KEY, "anthropic-version": "2023-06-01"},
        {"model": serving.model, "max_tokens": 16, "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status == 200, r


def test_gateway_logs_a_usage_record(
    serving: gateway.Serving, control_plane_client: gateway.Client, workload: kube.Cluster
) -> None:
    """The InferenceGateway's access log attributes a request's tokens to its caller.

    The mock engine reports the same tokens for every request, so every request
    the tests send logs an identical record. This counts the matching records
    before its own request, and waits for one more.
    """
    # The endpoint is the ModelRoute's backend for the ModelEndpoint, in
    # ml-team's mirrored namespace on the workload cluster.
    want = {
        "caller": "e2e",
        "service": "ml-team/mock",
        "endpoint": "mp-ml-team-51733/mock-local-934fc-mock-demo-da96c-f8a13",
        "served_model": "ml-team/mock-demo",
        "input_tokens": 12,
        "output_tokens": 9,
        "total_tokens": 21,
        "status": 200,
    }

    def matching() -> int:
        return sum(1 for record in gateway.usage_records(workload) if {k: record.get(k) for k in want} == want)

    before = matching()
    r = control_plane_client.request(
        f"{serving.openai}/chat/completions",
        AUTHORIZED,
        {"model": serving.model, "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status == 200, r

    def logged() -> None:
        assert matching() > before, f"no new usage record matching {want}"

    wait.until(logged, timeout=60, what="the InferenceGateway to log the request", retry=kube.RETRY)


def test_cluster_gateway_refuses_a_caller_without_a_client_certificate(
    workload_client: gateway.Client, cluster_gateway: str
) -> None:
    """The cluster gateway fronting the engines refuses a caller that presents no client certificate.

    Every other test goes through the InferenceGateway, which holds a
    certificate, so none of them would notice this lapsing. A
    ClientTrafficPolicy that stopped applying, or an HTTP listener beside the
    HTTPS one, would leave the engines open to anything that can reach the load
    balancer. The handshake fails before a request is sent, so this checks
    curl's exit code. The trailing dot skips the pod's search domains, which
    ndots:5 would otherwise try first.
    """
    # 35: TLS handshake failed. 52: empty reply. 55 and 56: the connection broke
    # mid-handshake. A refused or unresolved connection is a different failure.
    assert workload_client.connect(f"https://{cluster_gateway}./v1/models") in {35, 52, 55, 56}


def test_cluster_gateway_has_no_plaintext_listener(workload_client: gateway.Client, cluster_gateway: str) -> None:
    """The cluster gateway serves nothing over plain HTTP on port 80.

    The serving HTTPRoutes carry no sectionName, so they attach to every listener
    there is, and an HTTP listener would serve the engines without a
    certificate. The gateway's only listener is HTTPS, and the load balancer
    publishes a port per listener, so a connection to port 80 is refused.
    """
    # 7: connection refused. 28: timed out. The rest mean something answered
    # port 80 without serving HTTP.
    assert workload_client.connect(f"http://{cluster_gateway}./v1/models") in {7, 28, 35, 52, 56}
