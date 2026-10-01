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

from collections.abc import Iterator

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException
from models.ai.modelplane.inferencecluster import v1alpha1 as icv1alpha1
from models.ai.modelplane.inferencegateway import v1alpha1 as igv1alpha1
from models.ai.modelplane.modelservice import v1alpha1 as msv1alpha1

from e2e import environment, gateway, kube, wait

# Where the curl pod the tests send requests from runs, on each cluster.
CLIENT_NAMESPACE = "e2e"
CLIENT_POD = "curl"


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
    yield from curl_pod(control_plane)


@pytest.fixture(scope="session")
def workload_client(workload: kube.Cluster) -> Iterator[gateway.Client]:
    """A pod on the workload cluster, which can resolve the cluster gateway's Service name."""
    yield from curl_pod(workload)


def curl_pod(cluster: kube.Cluster) -> Iterator[gateway.Client]:
    """Start a curl pod on a cluster, and delete it afterwards."""
    # A run that was interrupted can leave the namespace behind.
    delete_namespace(cluster)
    try:
        cluster.core.create_namespace(
            client.V1Namespace(metadata=client.V1ObjectMeta(name=CLIENT_NAMESPACE)),
            _request_timeout=kube.TIMEOUT_SECONDS,
        )
        cluster.core.create_namespaced_pod(
            CLIENT_NAMESPACE,
            client.V1Pod(
                metadata=client.V1ObjectMeta(name=CLIENT_POD),
                spec=client.V1PodSpec(
                    containers=[
                        client.V1Container(
                            name="curl",
                            # Pinned by digest (a multi-arch manifest list), so
                            # a moving tag can't flake the tests.
                            image="curlimages/curl@sha256:7c12af72ceb38b7432ab85e1a265cff6ae58e06f95539d539b654f2cfa64bb13",
                            command=["sleep", "infinity"],
                        )
                    ],
                    # As PID 1, sleep ignores SIGTERM, so don't wait for it to
                    # exit.
                    termination_grace_period_seconds=0,
                ),
            ),
            _request_timeout=kube.TIMEOUT_SECONDS,
        )

        def ready() -> None:
            pod = cluster.core.read_namespaced_pod(CLIENT_POD, CLIENT_NAMESPACE, _request_timeout=kube.TIMEOUT_SECONDS)
            conditions = pod.status.conditions or []
            assert any(c.type == "Ready" and c.status == "True" for c in conditions), f"pod is {pod.status.phase}"

        wait.until(ready, timeout=2 * 60, what=f"pod {CLIENT_NAMESPACE}/{CLIENT_POD} to be Ready", retry=kube.RETRY)
        yield gateway.Client(cluster, CLIENT_NAMESPACE, CLIENT_POD)
    finally:
        delete_namespace(cluster)


def delete_namespace(cluster: kube.Cluster) -> None:
    """Delete the curl pod's namespace if it exists, and wait for it to go.

    Waiting means a run that follows doesn't create the pod in a namespace
    that's still terminating.
    """

    def gone() -> None:
        try:
            ns = cluster.core.read_namespace(CLIENT_NAMESPACE, _request_timeout=kube.TIMEOUT_SECONDS)
        except ApiException as e:
            if e.status == 404:
                return
            raise
        msg = f"namespace {CLIENT_NAMESPACE} is {ns.status.phase}"
        raise AssertionError(msg)

    try:
        cluster.core.delete_namespace(CLIENT_NAMESPACE, _request_timeout=kube.TIMEOUT_SECONDS)
    except ApiException as e:
        if e.status != 404:
            raise
    wait.until(gone, timeout=2 * 60, what=f"namespace {CLIENT_NAMESPACE} to be deleted", retry=kube.RETRY)


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
    def routing_ready() -> msv1alpha1.ModelService:
        obj = control_plane.modelplane("modelservices", "mock", "ml-team")
        assert obj is not None, "ModelService ml-team/mock doesn't exist"
        ms = msv1alpha1.ModelService.model_validate(obj)
        conditions = (ms.status.conditions if ms.status else None) or []
        assert any(c.type == "RoutingReady" and c.status == "True" for c in conditions), (
            f"ModelService ml-team/mock isn't RoutingReady: {[(c.type, c.status, c.reason) for c in conditions]}"
        )
        return ms

    ms = wait.until(routing_ready, timeout=20 * 60, what="ModelService ml-team/mock to route", retry=kube.RETRY)
    assert ms.status is not None
    assert ms.status.model is not None, "ModelService ml-team/mock is RoutingReady but publishes no model name"

    # AI Gateway rolls the gateway's proxy pods once the first route reaches
    # it, to stamp them with the hash of its sidecar's config, so a fresh
    # gateway is still replacing its pods when the route goes ready. Requests
    # can fail during that rollout, which starts only once AI Gateway has seen
    # the route. So wait for the stamp, then for the rollout to finish.
    def proxies_rolled_out() -> None:
        deployments = workload.apps.list_namespaced_deployment(
            gateway.PROXY_NAMESPACE, label_selector=gateway.PROXY_SELECTOR, _request_timeout=kube.TIMEOUT_SECONDS
        ).items
        assert deployments, "the InferenceGateway has no proxy Deployment"
        for d in deployments:
            annotations = d.spec.template.metadata.annotations or {}
            assert "aigateway.envoyproxy.io/extproc-config-hash" in annotations, (
                f"AI Gateway hasn't stamped Deployment {d.metadata.name}'s pods"
            )
            # The same test as kubectl rollout status.
            assert (d.status.observed_generation or 0) >= d.metadata.generation, (
                f"Deployment {d.metadata.name}'s controller hasn't seen its latest spec"
            )
            want = d.spec.replicas
            assert (d.status.updated_replicas or 0) == want, f"Deployment {d.metadata.name} is still updating pods"
            assert (d.status.replicas or 0) == want, f"Deployment {d.metadata.name} still has old pods"
            assert (d.status.available_replicas or 0) == want, f"Deployment {d.metadata.name} has unavailable pods"

    wait.until(
        proxies_rolled_out, timeout=5 * 60, what="the InferenceGateway's proxy pods to roll out", retry=kube.RETRY
    )

    def endpoints_published() -> igv1alpha1.Endpoints:
        obj = control_plane.modelplane("inferencegateways", "local", None)
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
    need it to serve.
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
        obj = control_plane.modelplane("inferenceclusters", "local", None)
        assert obj is not None, "InferenceCluster local doesn't exist"
        ic = icv1alpha1.InferenceCluster.model_validate(obj)
        assert ic.status is not None
        assert ic.status.gateway is not None, "InferenceCluster local publishes no gateway"
        assert ic.status.gateway.hostname is not None, "InferenceCluster local publishes no gateway hostname"
        return ic.status.gateway.hostname

    return wait.until(
        published, timeout=20 * 60, what="InferenceCluster local to publish its gateway hostname", retry=kube.RETRY
    )
