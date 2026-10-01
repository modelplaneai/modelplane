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

"""Read a cluster's resources through the Kubernetes API.

The official Kubernetes client returns typed objects and typed errors for the
built-in kinds. Cluster wraps the parts of it that need care: Modelplane's own
resources, which come back as dicts for the tests to validate into the
generated models, a command's exit code, and container logs.
"""

import dataclasses
import shlex
from typing import Any

import urllib3
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream

# A bound on each API call, so a hung API server fails the test that hit it
# rather than the whole run. Pass it as _request_timeout: the client has no
# default.
TIMEOUT_SECONDS = 60

# What a wait retries: the condition not holding yet, or the API server
# failing a request while the cluster converges.
RETRY = (AssertionError, ApiException, urllib3.exceptions.HTTPError)


@dataclasses.dataclass(frozen=True)
class Exec:
    """How a command run in a container exited, and what it printed."""

    code: int
    stdout: str
    stderr: str


class Cluster:
    """A cluster, addressed by its kubeconfig context."""

    def __init__(self, context: str) -> None:
        """Connect to the cluster a kubeconfig context names."""
        api = config.new_client_from_config(context=context)
        self.core = client.CoreV1Api(api)
        self.apps = client.AppsV1Api(api)
        self.custom = client.CustomObjectsApi(api)

    def modelplane(self, plural: str, name: str, namespace: str | None) -> dict[str, Any] | None:
        """Return a Modelplane resource, or None if it doesn't exist."""
        try:
            if namespace is None:
                return self.custom.get_cluster_custom_object(
                    "modelplane.ai", "v1alpha1", plural, name, _request_timeout=TIMEOUT_SECONDS
                )
            return self.custom.get_namespaced_custom_object(
                "modelplane.ai", "v1alpha1", namespace, plural, name, _request_timeout=TIMEOUT_SECONDS
            )
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def exec(self, pod: str, namespace: str, command: list[str]) -> Exec:
        """Run a command in a pod's only container, and return how it exited."""
        # The exec API streams over a websocket. Without _preload_content the
        # stream stays open until the command exits, which is what yields its
        # exit code.
        resp = stream(
            self.core.connect_get_namespaced_pod_exec,
            pod,
            namespace,
            command=command,
            stdout=True,
            stderr=True,
            stdin=False,
            tty=False,
            _preload_content=False,
        )
        resp.run_forever(timeout=TIMEOUT_SECONDS)
        if resp.returncode is None:
            msg = f"{shlex.join(command)} in {namespace}/{pod} didn't exit within {TIMEOUT_SECONDS}s"
            raise TimeoutError(msg)
        return Exec(code=resp.returncode, stdout=resp.read_stdout(), stderr=resp.read_stderr())

    def logs(self, pod: str, namespace: str, container: str) -> str:
        """Return a container's logs."""
        # With its content preloaded, the client tries to deserialize the logs,
        # and returns JSON log lines as the repr of a bytes object.
        resp = self.core.read_namespaced_pod_log(
            pod, namespace, container=container, _preload_content=False, _request_timeout=TIMEOUT_SECONDS
        )
        return resp.data.decode()
