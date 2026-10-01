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

"""Fixtures the end-to-end tests share. See README.md.

The tests run against clusters that are already up. `nix run .#e2e -- --verify`
brings them up first.
"""

import pathlib
from collections.abc import Iterator

import pytest
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.inferencegateway import v1alpha1 as igv1alpha1
from models.ai.modelplane.modelservice import v1alpha1 as msv1alpha1

from e2e import environment, gateway, kube, wait

CLIENT = pathlib.Path(__file__).parent / "client.yaml"


@pytest.fixture(scope="session")
def control_plane() -> kube.Cluster:
    """The control plane, running Crossplane and Modelplane."""
    return kube.Cluster(environment.CONTROL_PLANE_CONTEXT)


@pytest.fixture(scope="session")
def workload() -> kube.Cluster:
    """The workload cluster, running the serving stack, both gateways and the model."""
    return kube.Cluster(environment.WORKLOAD_CONTEXT)


@pytest.fixture(scope="session")
def control_plane_client(control_plane: kube.Cluster) -> Iterator[gateway.Client]:
    """A pod on the control plane, which sends requests across clusters to the InferenceGateway."""
    yield from client(control_plane)


@pytest.fixture(scope="session")
def workload_client(workload: kube.Cluster) -> Iterator[gateway.Client]:
    """A pod on the workload cluster, which can resolve the cluster gateway's Service name."""
    yield from client(workload)


def client(cluster: kube.Cluster) -> Iterator[gateway.Client]:
    """Start a curl pod on a cluster, and delete it afterwards."""
    try:
        cluster.apply(CLIENT)
        cluster.wait_for_condition("pod", "curl", "e2e", "Ready", timeout=2 * 60)
        yield gateway.Client(cluster, "e2e", "curl")
    finally:
        # Wait, so a run that follows doesn't create the pod in a namespace
        # that's still terminating.
        cluster.delete("namespace", "e2e", None)
        cluster.wait_until_gone("namespace", "e2e", None, timeout=2 * 60)


@pytest.fixture(scope="session")
def routed(control_plane: kube.Cluster, workload: kube.Cluster) -> gateway.Serving:
    """Wait for the InferenceGateway to route ModelService ml-team/mock.

    Bring-up returns once Modelplane is installed and the manifests are applied,
    so the serving stack and the model are still reconciling. On an environment
    that's already up, each wait returns at once.
    """
    # RoutingReady means the route is composed and applied on every gateway
    # serving the ModelService. Its status.model and the gateway's endpoints
    # both publish before that, without a replica, so waiting on either would
    # start sending requests while the engine is still rolling out.
    obj = control_plane.wait_for_condition("modelservice", "mock", "ml-team", "RoutingReady", timeout=20 * 60)
    ms = msv1alpha1.ModelService.model_validate(obj)
    assert ms.status is not None
    assert ms.status.model is not None, "ModelService ml-team/mock is RoutingReady but publishes no model name"

    # AI Gateway rolls the gateway's proxy pods once the first route reaches
    # it, to stamp them with the hash of its sidecar's config, so a fresh
    # gateway is still replacing its pods when the route goes ready. Requests
    # can fail during that rollout, which starts only once AI Gateway has seen
    # the route. So wait for the stamp, then for the rollout.
    def proxies_stamped() -> None:
        deployments = workload.list_objects("deployment", gateway.PROXY_NAMESPACE, gateway.PROXY_SELECTOR)
        annotations = [d["spec"]["template"]["metadata"].get("annotations", {}) for d in deployments]
        assert annotations, "the InferenceGateway has no proxy Deployment"
        assert all("aigateway.envoyproxy.io/extproc-config-hash" in a for a in annotations), (
            "AI Gateway hasn't stamped the InferenceGateway's proxy pods"
        )

    wait.until(
        proxies_stamped, timeout=2 * 60, what="AI Gateway to stamp the InferenceGateway's proxy pods", retry=kube.RETRY
    )
    workload.kubectl(
        "rollout", "status", "deployment", f"--namespace={gateway.PROXY_NAMESPACE}",
        f"--selector={gateway.PROXY_SELECTOR}", "--timeout=5m",
        timeout=6 * 60,
    )  # fmt: skip

    def endpoints_published() -> igv1alpha1.Endpoints:
        obj = control_plane.get("inferencegateway", "local", None)
        assert obj is not None, "InferenceGateway local doesn't exist"
        ig = igv1alpha1.InferenceGateway.model_validate(obj)
        assert ig.status is not None
        assert ig.status.endpoints is not None, "InferenceGateway local publishes no endpoints"
        return ig.status.endpoints

    endpoints = wait.until(
        endpoints_published, timeout=5 * 60, what="InferenceGateway local to publish its endpoints", retry=kube.RETRY
    )
    assert endpoints.openAI is not None, "InferenceGateway local publishes no OpenAI endpoint"
    assert endpoints.anthropic is not None, "InferenceGateway local publishes no Anthropic endpoint"
    return gateway.Serving(model=ms.status.model, openai=endpoints.openAI, anthropic=endpoints.anthropic)


@pytest.fixture(scope="session")
def serving(routed: gateway.Serving, control_plane_client: gateway.Client) -> gateway.Serving:
    """Wait for the InferenceGateway to serve ModelService ml-team/mock to caller e2e.

    The gateway can publish its endpoints a moment before the route serves, and
    a slow CI runner widens that gap. Tests that expect a refusal use routed
    instead, so a gateway that refuses everyone still fails only the tests that
    expect it to serve.
    """

    def serves() -> None:
        r = control_plane_client.request(
            f"{routed.openai}/chat/completions",
            {"authorization": f"Bearer {gateway.CALLER_KEY}"},
            {"model": routed.model, "messages": [{"role": "user", "content": "ping"}]},
        )
        assert r.status == 200, r

    wait.until(serves, timeout=2 * 60, what=f"the InferenceGateway to serve {routed.model}", retry=kube.RETRY)
    return routed


@pytest.fixture(scope="session")
def cluster_gateway(control_plane: kube.Cluster) -> str:
    """Wait for the gateway fronting the engines on the workload cluster, and return its hostname.

    InferenceCluster local publishes the hostname once the gateway requires
    client certificates.
    """

    def published() -> str:
        obj = control_plane.get("inferencecluster", "local", None)
        assert obj is not None, "InferenceCluster local doesn't exist"
        ic = icv1alpha1.InferenceCluster.model_validate(obj)
        assert ic.status is not None
        assert ic.status.gateway is not None, "InferenceCluster local publishes no gateway"
        assert ic.status.gateway.hostname is not None, "InferenceCluster local publishes no gateway hostname"
        return ic.status.gateway.hostname

    return wait.until(
        published, timeout=20 * 60, what="InferenceCluster local to publish its gateway hostname", retry=kube.RETRY
    )
