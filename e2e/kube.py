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
import typing

import urllib3
from kubernetes import client, config, stream

# A bound on each API call, so a hung API server fails the test that hit it
# rather than the whole run. Pass it as _request_timeout: the client has no
# default.
TIMEOUT_SECONDS = 60

# What a wait retries: the condition not holding yet, or the API server
# failing a request or a command's exec stalling while the cluster converges.
RETRY = (AssertionError, client.ApiException, urllib3.exceptions.HTTPError, TimeoutError)


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

    def modelplane(
        self, plural: str, name: str, namespace: str | None, *, group: str = "modelplane.ai"
    ) -> dict[str, typing.Any] | None:
        """Return a Modelplane resource, or None if it doesn't exist.

        The model kinds live in modelplane.ai; a ServingStack lives in
        infrastructure.modelplane.ai.
        """
        try:
            if namespace is None:
                return self.custom.get_cluster_custom_object(
                    group, "v1alpha1", plural, name, _request_timeout=TIMEOUT_SECONDS
                )
            return self.custom.get_namespaced_custom_object(
                group, "v1alpha1", namespace, plural, name, _request_timeout=TIMEOUT_SECONDS
            )
        except client.ApiException as e:
            if e.status == 404:
                return None
            raise

    def exec(self, pod: str, namespace: str, command: list[str]) -> Exec:
        """Run a command in a pod's only container, and return how it exited."""
        # The exec API streams over a websocket. Without _preload_content the
        # stream stays open until the command exits, which is what yields its
        # exit code.
        resp = stream.stream(
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

    def logs(self, pod: str, namespace: str, container: str, *, tail_lines: int | None) -> str:
        """Return a container's logs: its last tail_lines lines, or all of them if tail_lines is None."""
        # With its content preloaded, the client tries to deserialize the logs,
        # and returns them as the repr of a bytes object.
        resp = self.core.read_namespaced_pod_log(
            pod,
            namespace,
            container=container,
            tail_lines=tail_lines,
            _preload_content=False,
            _request_timeout=TIMEOUT_SECONDS,
        )
        return resp.data.decode()


def rolled_out(d: client.V1Deployment) -> None:
    """Fail unless a Deployment has finished rolling out, by the test kubectl rollout status makes."""
    name = d.metadata.name
    assert (d.status.observed_generation or 0) >= d.metadata.generation, (
        f"Deployment {name}'s controller hasn't seen its latest spec"
    )
    want = d.spec.replicas
    assert (d.status.updated_replicas or 0) == want, f"Deployment {name} is still updating pods"
    assert (d.status.replicas or 0) == want, f"Deployment {name} still has old pods"
    assert (d.status.available_replicas or 0) == want, f"Deployment {name} has unavailable pods"
