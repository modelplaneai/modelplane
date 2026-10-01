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
from typing import Any

from e2e import kube

# The InferenceGateway's Envoy proxy pods, on the workload cluster.
PROXY_NAMESPACE = "envoy-gateway-system"
PROXY_SELECTOR = "gateway.envoyproxy.io/owning-gateway-name=inference-gateway"

# The key manifests/10-inference-gateway.yaml gives caller e2e.
CALLER_KEY = "sk-e2e-caller"


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

    status: int
    body: str

    def json(self) -> Any:  # noqa: ANN401 - a JSON body can decode to any type.
        """Decode the body as JSON."""
        return json.loads(self.body)


@dataclasses.dataclass(frozen=True)
class Client:
    """Runs curl in a pod on a cluster that can reach the gateways."""

    cluster: kube.Cluster
    namespace: str
    pod: str

    def request(self, url: str, headers: dict[str, str], body: object | None = None) -> Response:
        """Send a request, a POST of body as JSON if there is one and otherwise a GET."""
        curl = ["curl", "--silent", "--show-error", "--max-time", "15", "--write-out", "\n%{http_code}", url]
        for name, value in headers.items():
            curl += ["--header", f"{name}: {value}"]
        if body is not None:
            curl += ["--header", "content-type: application/json", "--data", json.dumps(body)]
        # curl exits non-zero only when no HTTP response came back, which fails
        # the request rather than answering it.
        out = self.cluster.kubectl("exec", f"--namespace={self.namespace}", self.pod, "--", *curl)
        # --write-out puts the status on a line of its own, after the body.
        text, _, status = out.rpartition("\n")
        return Response(status=int(status), body=text)

    def connect(self, url: str) -> int:
        """GET a URL without verifying the server's certificate, and return curl's exit code."""
        curl = ["curl", "--silent", "--show-error", "--insecure", "--max-time", "15", "--output", "/dev/null", url]
        return self.cluster.run("exec", f"--namespace={self.namespace}", self.pod, "--", *curl).returncode


def usage_records(cluster: kube.Cluster) -> list[dict[str, Any]]:
    """Return every usage record the InferenceGateway's proxy pods have logged.

    A request lands on any one of the proxy pods, so this reads them all.
    """
    records = []
    for pod in cluster.list_objects("pod", PROXY_NAMESPACE, PROXY_SELECTOR):
        logs = cluster.kubectl("logs", f"--namespace={PROXY_NAMESPACE}", pod["metadata"]["name"], "--container=envoy")
        for line in logs.splitlines():
            # Envoy logs other things too. The access log is the JSON objects.
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and "caller" in record:
                records.append(record)
    return records
