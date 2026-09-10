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

"""Hand-written components on every cloud and every stack.

Modelplane's own half of the serving stack, where the Standard and
Dynamo stacks don't differ: the gateway path (Envoy Gateway, Envoy AI
Gateway and its CRDs, the Gateway API Inference Extension CRDs, the
gateway namespace and EnvoyProxy) and the DRA critical-pods quota.

The GatewayClass and Gateway are deliberately absent: they read
spec.gateway (className, listeners), which stays per-cluster API, so the
function renders them itself.

Versions here are Modelplane's pins: this file carries what no
generator resolves - the contract surface a ModelReplica composes
against, whose versions have nothing to do with the hardware (see
design/serving-stack-generation.md, "Component sources").
"""

import pathlib
from typing import Any

import yaml

from function.stacks.components import Chart, Component, Manifests

# The AI Gateway controller supplies the ext-proc extension server that
# Envoy Gateway delegates InferencePool backend resolution to, so
# HTTPRoute -> InferencePool backendRefs (disaggregated serving) route.
_AI_GATEWAY_NAMESPACE = "envoy-ai-gateway-system"
_AI_GATEWAY_REPO = "oci://docker.io/envoyproxy"
_AI_GATEWAY_VERSION = "v0.7.0"

# Must match the namespace every cloud half installs the NVIDIA DRA
# driver into - generated and hand-written alike - so this quota lands
# where the kubelet plugin runs.
_DRA_DRIVER_NAMESPACE = "nvidia-dra-driver"

_CRDS_DIR = pathlib.Path(__file__).parent / "crds"


def _crds(filename: str) -> list[dict[str, Any]]:
    """Load the CRDs from a YAML file vendored under stacks/crds/."""
    return [
        doc
        for doc in yaml.safe_load_all((_CRDS_DIR / filename).read_text())
        if doc and doc.get("kind") == "CustomResourceDefinition"
    ]


COMPONENTS: list[Component] = [
    Chart(
        key="envoy-gateway",
        release="mp-gateway-helm",
        namespace="envoy-gateway-system",
        chart="gateway-helm",
        repository="oci://docker.io/envoyproxy",
        version="v1.8.1",
        # cert-manager lives in every cloud half - generated or
        # hand-written - so this edge crosses the halves and resolves
        # against the joined list. Envoy Gateway needs it for its
        # webhooks.
        depends_on=["cert-manager"],
        # The extensionManager block points Envoy Gateway at the Envoy AI
        # Gateway controller's ext-proc server and declares InferencePool
        # a backend resource, so HTTPRoute -> InferencePool backendRefs
        # resolve. enableBackend turns on the Backend API the AI Gateway
        # relies on.
        values={
            "config": {
                "envoyGateway": {
                    "extensionApis": {"enableBackend": True},
                    "extensionManager": {
                        "hooks": {
                            "xdsTranslator": {
                                "translation": {
                                    "listener": {"includeAll": True},
                                    "route": {"includeAll": True},
                                    "cluster": {"includeAll": True},
                                    "secret": {"includeAll": True},
                                },
                                "post": ["Translation", "Cluster", "Route"],
                            },
                        },
                        "service": {
                            "fqdn": {
                                "hostname": f"ai-gateway-controller.{_AI_GATEWAY_NAMESPACE}.svc.cluster.local",
                                "port": 1063,
                            },
                        },
                        "backendResources": [
                            {
                                "group": "inference.networking.k8s.io",
                                "kind": "InferencePool",
                                "version": "v1",
                            },
                        ],
                    },
                },
            },
        },
    ),
    Chart(
        key="ai-gateway-crds",
        release="mp-ai-gateway-crds-helm",
        namespace=_AI_GATEWAY_NAMESPACE,
        chart="ai-gateway-crds-helm",
        repository=_AI_GATEWAY_REPO,
        version=_AI_GATEWAY_VERSION,
        # ai-gateway depends on this chart.
        wait=True,
    ),
    Chart(
        key="ai-gateway",
        release="mp-ai-gateway-helm",
        namespace=_AI_GATEWAY_NAMESPACE,
        chart="ai-gateway-helm",
        repository=_AI_GATEWAY_REPO,
        version=_AI_GATEWAY_VERSION,
        depends_on=["ai-gateway-crds"],
    ),
    # Gateway API Inference Extension CRDs, providing the InferencePool
    # that disaggregated replicas front their decode endpoints with.
    # Vendored from the upstream release's manifests.yaml.
    Manifests(
        key="gaie-crds",
        manifests=_crds("gaie.yaml"),
    ),
    # The Gateway (and the model-serving HTTPRoutes that target it) live
    # in modelplane-system on the remote cluster; nothing else
    # provisions the namespace.
    Manifests(
        key="gateway-namespace",
        manifests=[
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": "modelplane-system"},
            },
        ],
    ),
    # EnvoyProxy pins the managed LoadBalancer Service's
    # externalTrafficPolicy to Cluster. Envoy Gateway defaults it to
    # Local, which some clouds' load balancers reject (Nebius returns
    # SyncLoadBalancerFailed and never assigns an external IP). Cluster
    # is accepted by every cloud Modelplane runs on; the inference
    # gateway does not need client source-IP preservation. The
    # GatewayClass references it via parametersRef.
    Manifests(
        key="gateway-proxy",
        depends_on=["gateway-namespace"],
        manifests=[
            {
                "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                "kind": "EnvoyProxy",
                "metadata": {"name": "inference-gateway", "namespace": "modelplane-system"},
                "spec": {
                    "provider": {
                        "type": "Kubernetes",
                        "kubernetes": {
                            "envoyService": {"externalTrafficPolicy": "Cluster"},
                        },
                    },
                },
            },
        ],
    ),
    # The DRA driver's kubelet plugin runs at system-node-critical
    # priority. GKE only admits such pods in a namespace whose
    # ResourceQuota permits those priority classes; without it the
    # daemonset gets FailedCreate and never publishes ResourceSlices.
    # Laid down everywhere: it only grants headroom, so it's harmless on
    # clusters that don't restrict them.
    Manifests(
        key="dra-driver-critical-pods-quota",
        manifests=[
            {
                "apiVersion": "v1",
                "kind": "ResourceQuota",
                "metadata": {
                    "name": "allow-critical-pods",
                    "namespace": _DRA_DRIVER_NAMESPACE,
                },
                "spec": {
                    "hard": {"pods": "1000"},
                    "scopeSelector": {
                        "matchExpressions": [
                            {
                                "operator": "In",
                                "scopeName": "PriorityClass",
                                "values": [
                                    "system-node-critical",
                                    "system-cluster-critical",
                                ],
                            },
                        ],
                    },
                },
            },
        ],
    ),
]
