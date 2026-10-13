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

"""Tests that requests and responses cross the InferenceGateway intact."""

import collections.abc
import dataclasses
import json

import pytest

from e2e import gateway


def test_chat_completion(serving: gateway.Serving, control_plane_client: gateway.Client) -> None:
    """An OpenAI chat completion comes back in the shape the OpenAI API promises."""
    r = control_plane_client.request(
        f"{serving.openai}/chat/completions",
        gateway.BEARER,
        {"model": serving.model, "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status == 200
    got = r.json()
    assert got["object"] == "chat.completion"
    assert len(got["choices"]) == 1
    assert got["choices"][0]["message"]["role"] == "assistant"
    assert got["choices"][0]["message"]["content"]
    assert got["choices"][0]["finish_reason"] == "stop"
    assert "usage" in got


def test_message(serving: gateway.Serving, control_plane_client: gateway.Client) -> None:
    """An Anthropic message comes back in the shape the Messages API promises."""
    r = control_plane_client.request(
        f"{serving.anthropic}/messages",
        {"x-api-key": gateway.CALLER_KEY, "anthropic-version": "2023-06-01"},
        {"model": serving.model, "max_tokens": 16, "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status == 200
    got = r.json()
    assert got["type"] == "message"
    assert got["role"] == "assistant"
    assert got["content"][0]["type"] == "text"
    assert got["content"][0]["text"]
    assert got["stop_reason"] == "end_turn"
    assert {"input_tokens", "output_tokens"} <= got["usage"].keys()


def test_last_user_turn(serving: gateway.Serving, control_plane_client: gateway.Client) -> None:
    """The engine sees the caller's last user turn, so the gateway forwards the body intact."""
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "an earlier answer"},
        {"role": "user", "content": "marker-4d91"},
    ]
    r = control_plane_client.request(
        f"{serving.openai}/chat/completions", gateway.BEARER, {"model": serving.model, "messages": messages}
    )
    assert r.status == 200
    assert "marker-4d91" in r.json()["choices"][0]["message"]["content"]


def test_streaming(serving: gateway.Serving, control_plane_client: gateway.Client) -> None:
    """A streamed chat completion comes back as event-stream frames, not one buffered body."""
    r = control_plane_client.request(
        f"{serving.openai}/chat/completions",
        gateway.BEARER,
        {"model": serving.model, "messages": [{"role": "user", "content": "one two"}], "stream": True},
    )
    assert r.status == 200
    assert r.content_type.startswith("text/event-stream"), r
    frames = [line.removeprefix("data: ") for line in r.body.splitlines() if line.startswith("data: ")]
    assert len(frames) > 1, r
    assert frames[-1] == "[DONE]"
    chunks = [json.loads(f) for f in frames[:-1]]
    assert {c["object"] for c in chunks} == {"chat.completion.chunk"}
    assert "one two" in "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)


@dataclasses.dataclass
class Case:
    """A request the InferenceGateway fails rather than serves."""

    name: str
    reason: str
    path: str
    # The request body, given the model the caller names.
    raw: collections.abc.Callable[[str], str]


REJECTED_CASES = [
    Case(
        name="MalformedBody",
        reason="A body that isn't JSON fails, rather than returning a 200.",
        path="/chat/completions",
        raw=lambda _: "not json",
    ),
    Case(
        name="NoMessages",
        reason="A chat completion without messages fails, rather than returning a 200.",
        path="/chat/completions",
        raw=lambda model: json.dumps({"model": model}),
    ),
    Case(
        name="UnknownPath",
        reason="A path no route serves fails, rather than returning a 200.",
        path="/nonexistent",
        raw=lambda model: json.dumps({"model": model, "messages": [{"role": "user", "content": "ping"}]}),
    ),
]


@pytest.mark.parametrize("case", REJECTED_CASES, ids=lambda case: case.name)
def test_rejected(case: Case, serving: gateway.Serving, control_plane_client: gateway.Client) -> None:
    """The InferenceGateway answers a bad request with an error status."""
    r = control_plane_client.request(f"{serving.openai}{case.path}", gateway.BEARER, raw=case.raw(serving.model))
    assert r.status >= 400, case.reason
