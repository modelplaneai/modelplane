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

"""Read and change a cluster's resources with kubectl.

Objects come back as the dicts kubectl's JSON output decodes to. Tests that
read Modelplane's own fields validate them into the generated models.
"""

import dataclasses
import json
import pathlib
import shlex
import subprocess
from typing import Any

from e2e import wait

# A bound on each kubectl call, so a hung API server fails the test that hit it
# rather than the whole run.
KUBECTL_TIMEOUT_SECONDS = 60

type Object = dict[str, Any]


class KubectlError(Exception):
    """kubectl exited non-zero, or timed out."""


# What a wait retries: the condition not holding yet, or a kubectl call failing
# while the cluster converges.
RETRY = (AssertionError, KubectlError)


@dataclasses.dataclass(frozen=True)
class Cluster:
    """A cluster, addressed by its kubeconfig context."""

    context: str

    def run(self, *args: str, timeout: float = KUBECTL_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
        """Run kubectl against this cluster, and return how it exited."""
        cmd = ["kubectl", f"--context={self.context}", *args]
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as e:
            msg = f"{shlex.join(cmd)}: timed out after {timeout:.0f}s"
            raise KubectlError(msg) from e

    def kubectl(self, *args: str, timeout: float = KUBECTL_TIMEOUT_SECONDS) -> str:
        """Run kubectl against this cluster, and return what it wrote to stdout."""
        result = self.run(*args, timeout=timeout)
        if result.returncode != 0:
            msg = f"{shlex.join(result.args)}: {result.stderr.strip()}"
            raise KubectlError(msg)
        return result.stdout

    def get(self, kind: str, name: str, namespace: str | None) -> Object | None:
        """Return the named object, or None if it doesn't exist."""
        out = self.kubectl("get", kind, name, *namespaced(namespace), "--ignore-not-found", "--output=json")
        return json.loads(out) if out else None

    def list_objects(self, kind: str, namespace: str, selector: str) -> list[Object]:
        """Return the objects of a kind in a namespace that match a label selector."""
        out = self.kubectl("get", kind, f"--namespace={namespace}", f"--selector={selector}", "--output=json")
        return json.loads(out)["items"]

    def apply(self, path: pathlib.Path) -> None:
        """Apply a manifest file."""
        self.kubectl("apply", f"--filename={path}")

    def delete(self, kind: str, name: str, namespace: str | None) -> None:
        """Delete an object without waiting for it to go. Deleting one that doesn't exist is a no-op."""
        self.kubectl("delete", kind, name, *namespaced(namespace), "--ignore-not-found", "--wait=false")

    def wait_for_condition(self, kind: str, name: str, namespace: str | None, condition: str, timeout: float) -> Object:
        """Wait for an object's condition to be True, and return the object."""

        def condition_is_true() -> Object:
            obj = self.get(kind, name, namespace)
            assert obj is not None, f"{kind} {qualified(namespace, name)} doesn't exist"
            assert is_true(obj, condition), f"{describe(obj)} isn't {condition}"
            return obj

        what = f"{kind} {qualified(namespace, name)} {condition}"
        return wait.until(condition_is_true, timeout=timeout, what=what, retry=RETRY)

    def wait_until_gone(self, kind: str, name: str, namespace: str | None, timeout: float) -> None:
        """Wait for an object to stop existing."""

        def is_gone() -> None:
            obj = self.get(kind, name, namespace)
            assert obj is None, f"{describe(obj)} still exists"

        wait.until(is_gone, timeout=timeout, what=f"{kind} {qualified(namespace, name)} to be deleted", retry=RETRY)


def namespaced(namespace: str | None) -> list[str]:
    """Return the kubectl flag that scopes a command to a namespace, if there is one."""
    return [f"--namespace={namespace}"] if namespace else []


def qualified(namespace: str | None, name: str) -> str:
    """Return namespace/name, or just name for a cluster-scoped object."""
    return f"{namespace}/{name}" if namespace else name


def is_true(obj: Object, condition: str) -> bool:
    """Report whether an object's status condition is True."""
    return any(c["type"] == condition and c["status"] == "True" for c in obj.get("status", {}).get("conditions", []))


def describe(obj: Object) -> str:
    """Summarise an object and its status conditions on one line, for failure messages."""
    meta = obj["metadata"]
    conditions = []
    for c in obj.get("status", {}).get("conditions", []):
        why = ": ".join(v for v in (c.get("reason"), c.get("message")) if v)
        conditions.append(f"{c['type']}={c['status']} ({why})" if why else f"{c['type']}={c['status']}")
    return f"{obj['kind']} {qualified(meta.get('namespace'), meta['name'])}: {', '.join(conditions) or 'no conditions'}"
