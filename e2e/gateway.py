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

"""Send requests through Modelplane's gateways, and read what they log.

The gateways' addresses are on the kind Docker network, which a macOS host
can't route to. So requests go from a pod on one of the clusters, which can.
"""

import dataclasses
import json
import typing

from e2e import kube

# The InferenceGateway's Envoy proxy pods, on the workload cluster.
PROXY_NAMESPACE = "envoy-gateway-system"
PROXY_SELECTOR = "gateway.envoyproxy.io/owning-gateway-name=inference-gateway"

# The key manifests/10-inference-gateway.yaml gives caller e2e.
CALLER_KEY = "sk-e2e-caller"

# How an OpenAI client sends the caller's key.
BEARER = {"authorization": f"Bearer {CALLER_KEY}"}


@dataclasses.dataclass(frozen=True)
class Serving:
    """A model the InferenceGateway routes, and where to reach it."""

    # The name a caller sends as the request's model: the ModelService's
    # status.model.
    model: str
    openai: str
    anthropic: str


@dataclasses.dataclass(frozen=True)
class Response:
    """What came back from a request."""

    # The HTTP status, or 0 if no HTTP response came back.
    status: int
    # The body, or why no HTTP response came back.
    body: str
    content_type: str = ""

    def json(self) -> typing.Any:  # noqa: ANN401 - a JSON body can decode to any type.
        """Decode the body as JSON."""
        return json.loads(self.body)


@dataclasses.dataclass(frozen=True)
class Client:
    """Runs curl in a pod on a cluster that can reach the gateways."""

    cluster: kube.Cluster
    namespace: str
    pod: str

    def request(
        self, url: str, headers: dict[str, str], body: object | None = None, *, raw: str | None = None
    ) -> Response:
        """Send a request, a POST of body as JSON (or of raw as is) if there is one and otherwise a GET."""
        write_out = "\n%{content_type}\n%{http_code}"
        curl = ["curl", "--silent", "--show-error", "--max-time", "15", "--write-out", write_out, url]
        for name, value in headers.items():
            curl += ["--header", f"{name}: {value}"]
        if body is not None:
            raw = json.dumps(body)
        if raw is not None:
            curl += ["--header", "content-type: application/json", "--data", raw]
        result = self.cluster.exec(self.pod, self.namespace, curl)
        # curl exits non-zero only when no HTTP response came back.
        if result.code != 0:
            return Response(status=0, body=result.stderr.strip())
        # --write-out puts the content type and status on lines of their own, after the body.
        rest, _, status = result.stdout.rpartition("\n")
        text, _, content_type = rest.rpartition("\n")
        return Response(status=int(status), body=text, content_type=content_type)

    def connect(self, url: str) -> int:
        """GET a URL without verifying the server's certificate, and return curl's exit code."""
        curl = ["curl", "--silent", "--show-error", "--insecure", "--max-time", "15", "--output", "/dev/null", url]
        return self.cluster.exec(self.pod, self.namespace, curl).code


def usage_records(cluster: kube.Cluster) -> list[dict[str, typing.Any]]:
    """Return every usage record the InferenceGateway's proxy pods have logged.

    A request lands on any one of the proxy pods, so this reads them all.
    """
    pods = cluster.core.list_namespaced_pod(
        PROXY_NAMESPACE, label_selector=PROXY_SELECTOR, _request_timeout=kube.TIMEOUT_SECONDS
    )
    records = []
    for pod in pods.items:
        for line in cluster.logs(pod.metadata.name, PROXY_NAMESPACE, "envoy", tail_lines=None).splitlines():
            # Envoy logs other things too. The access log is the JSON objects.
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and "caller" in record:
                records.append(record)
    return records
