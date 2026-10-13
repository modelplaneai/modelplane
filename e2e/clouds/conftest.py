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

"""Fixtures the cloud serving-stack tests share. See e2e/README.md.

The tests run against clusters that are already up, with the cloud's fake GPU
nodes and ServingStack applied. `nix run .#e2e -- --cloud <cloud>` brings them
up first and names the leg under test with pytest's --cloud.
"""

import pytest
from models.ai.modelplane.infrastructure.servingstack import v1alpha1 as ssv1alpha1

from e2e import clouds, environment, kube, wait


def pytest_addoption(parser: pytest.Parser) -> None:
    """Name the cloud leg the environment was brought up with."""
    parser.addoption("--cloud", required=True, choices=sorted(clouds.CLOUDS), help="the cloud leg under test")


@pytest.fixture(scope="session")
def cloud(request: pytest.FixtureRequest) -> clouds.Cloud:
    """The cloud leg under test."""
    return clouds.CLOUDS[request.config.getoption("--cloud")]


@pytest.fixture(scope="session")
def control_plane() -> kube.Cluster:
    """The control plane, running Crossplane and Modelplane."""
    return kube.Cluster(environment.CONTROL_PLANE_CONTEXT)


@pytest.fixture(scope="session")
def workload() -> kube.Cluster:
    """The workload cluster, whose fake nodes play the cloud's tainted GPU pool."""
    return kube.Cluster(environment.WORKLOAD_CONTEXT)


@pytest.fixture(scope="session", autouse=True)
def installed(control_plane: kube.Cluster, cloud: clouds.Cloud) -> None:
    """Wait for the ServingStack to install on the workload cluster.

    Bring-up returns once the XR is applied, and the install - a dozen Helm
    releases, gated in waves - reconciles in the background. Every test needs
    the stack in place, so this waits once for the whole session.
    """

    def ready() -> None:
        obj = control_plane.modelplane(
            "servingstacks", clouds.SERVING_STACK, clouds.NAMESPACE, group="infrastructure.modelplane.ai"
        )
        assert obj is not None, f"ServingStack {clouds.NAMESPACE}/{clouds.SERVING_STACK} doesn't exist"
        ss = ssv1alpha1.ServingStack.model_validate(obj)
        conditions = (ss.status.conditions if ss.status else None) or []
        assert any(c.type == "Ready" and c.status == "True" for c in conditions), (
            f"ServingStack {clouds.SERVING_STACK} isn't Ready: "
            f"{[(c.type, c.status, c.reason, c.message) for c in conditions]}"
        )

    wait.until(ready, timeout=30 * 60, what=f"the {cloud.cloud} serving stack to install", retry=kube.RETRY)
