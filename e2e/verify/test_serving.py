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

"""Behavioral checks against a running ModelService.

A 200 only proves the path is wired. These assert what came back: the response
shapes the OpenAI and Anthropic surfaces promise, that a request body reaches
the engine intact, that a streamed response survives the two-hop route as
event-stream frames, and that bad input fails instead of returning a 200 with
an error in the body.

MODELPLANE_ADDRESS is the ModelService's status.address. It's on the kind
Docker subnet, so this runs from a pod on the control plane (see run.sh), not
from the host.
"""

import json
import os
import time
import unittest
import urllib.error
import urllib.request

ADDRESS = os.environ.get("MODELPLANE_ADDRESS", "").rstrip("/")
MODEL = os.environ.get("MODELPLANE_MODEL", "mock")
READY_TIMEOUT = float(os.environ.get("MODELPLANE_READY_TIMEOUT", "300"))

OK = 200
BAD_REQUEST = 400
ANTHROPIC_HEADERS = {"anthropic-version": "2023-06-01"}

Response = tuple[int, dict[str, str], bytes]


def call(
    path: str, data: bytes | None = None, *, headers: dict[str, str] | None = None, timeout: float = 30.0
) -> Response:
    """POST data (GET when it's None) and return the status, headers and body, 4xx included."""
    req = urllib.request.Request(f"{ADDRESS}{path}", data=data, method="GET" if data is None else "POST")
    req.add_header("content-type", "application/json")
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        with err:
            return err.code, dict(err.headers), err.read()


def chat(*turns: str, stream: bool = False) -> bytes:
    """An OpenAI chat request. Turns alternate user, assistant, user, so the last one is the user's."""
    messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": t} for i, t in enumerate(turns)]
    payload: dict[str, object] = {"model": MODEL, "messages": messages}
    if stream:
        payload["stream"] = True
    return json.dumps(payload).encode()


def frames(body: bytes) -> list[str]:
    return [line[len("data: ") :] for line in body.decode().splitlines() if line.startswith("data: ")]


def setUpModule() -> None:
    """Wait for the route to serve. The address publishes before the path carries traffic."""
    if not ADDRESS:
        raise RuntimeError("MODELPLANE_ADDRESS is not set")
    deadline = time.monotonic() + READY_TIMEOUT
    status = 0
    while True:
        try:
            status, _, _ = call("/v1/chat/completions", chat("ready?"), timeout=15.0)
        except OSError:
            status = 0
        if status == OK:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"{ADDRESS} did not serve a chat completion within {READY_TIMEOUT:.0f}s (last status {status})"
            )
        time.sleep(5)


class ChatCompletions(unittest.TestCase):
    def test_response_shape(self) -> None:
        status, _, body = call("/v1/chat/completions", chat("ping"))
        self.assertEqual(status, OK)
        got = json.loads(body)
        self.assertEqual(got["object"], "chat.completion")
        self.assertEqual(got["model"], MODEL)
        self.assertEqual(len(got["choices"]), 1)
        choice = got["choices"][0]
        self.assertEqual(choice["message"]["role"], "assistant")
        self.assertTrue(choice["message"]["content"], "empty assistant content")
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertIn("usage", got)

    def test_last_user_turn_reaches_the_engine(self) -> None:
        marker = "marker-4d91"
        status, _, body = call("/v1/chat/completions", chat("first", "an earlier answer", marker))
        self.assertEqual(status, OK)
        content = json.loads(body)["choices"][0]["message"]["content"]
        self.assertIn(marker, content, "the engine did not see the last user turn")

    def test_streaming_returns_event_stream_frames(self) -> None:
        status, headers, body = call("/v1/chat/completions", chat("one two", stream=True))
        self.assertEqual(status, OK)
        self.assertIn("text/event-stream", headers.get("content-type", ""))
        got = frames(body)
        self.assertGreater(len(got), 1, "a stream collapsed to a single frame")
        self.assertEqual(got[-1], "[DONE]")
        self.assertEqual(json.loads(got[0])["object"], "chat.completion.chunk")
        streamed = "".join(json.loads(f)["choices"][0]["delta"].get("content", "") for f in got[:-1])
        self.assertIn("one two", streamed)

    def test_malformed_body_is_rejected(self) -> None:
        status, _, _ = call("/v1/chat/completions", b"not json")
        self.assertGreaterEqual(status, BAD_REQUEST, "malformed JSON was accepted")

    def test_request_without_messages_is_rejected(self) -> None:
        status, _, _ = call("/v1/chat/completions", json.dumps({"model": MODEL}).encode())
        self.assertGreaterEqual(status, BAD_REQUEST, "a request with no messages was accepted")


class Messages(unittest.TestCase):
    """The Anthropic surface, served on the same address."""

    def test_response_shape(self) -> None:
        payload = json.dumps({"model": MODEL, "max_tokens": 16, "messages": [{"role": "user", "content": "ping"}]})
        status, _, body = call("/v1/messages", payload.encode(), headers=ANTHROPIC_HEADERS)
        self.assertEqual(status, OK)
        got = json.loads(body)
        self.assertEqual(got["type"], "message")
        self.assertEqual(got["role"], "assistant")
        self.assertEqual(got["model"], MODEL)
        self.assertEqual(got["content"][0]["type"], "text")
        self.assertTrue(got["content"][0]["text"], "empty text block")
        self.assertEqual(got["stop_reason"], "end_turn")
        self.assertIn("input_tokens", got["usage"])
        self.assertIn("output_tokens", got["usage"])


class Routing(unittest.TestCase):
    def test_models_lists_the_served_model(self) -> None:
        status, _, body = call("/v1/models")
        self.assertEqual(status, OK)
        self.assertIn(MODEL, [m["id"] for m in json.loads(body)["data"]])

    def test_unknown_path_does_not_serve(self) -> None:
        status, _, _ = call("/v1/nonexistent", chat("ping"))
        self.assertGreaterEqual(status, BAD_REQUEST, "an unrouted path returned a success")


if __name__ == "__main__":
    unittest.main()
