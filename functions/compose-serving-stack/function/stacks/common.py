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
# AIGatewayRoute's streamIdleTimeout, which resets a backend that hangs
# before the first token and fails over to the next priority, arrived in
# Envoy AI Gateway v1.1.0.
_AI_GATEWAY_VERSION = "v1.1.0"

# The header an InferenceGateway stamps the authenticated caller's identity
# onto, mapped into AI Gateway request metadata (see the ai-gateway
# component below).
_CALLER_HEADER = "x-modelplane-caller"

# The cluster gateway's cert-manager Issuer that signs its CA. Composed on
# every cluster (see the gateway-selfsigned-issuer component); fn.py's PKI
# names it as the CA certificate's issuer, so the two must agree.
SELFSIGNED_ISSUER = "modelplane-selfsigned"

# trust-manager republishes the cluster CA's certificate into a ConfigMap
# without its private key, which is what lets the control plane read the
# certificate to hand to an InferenceGateway. Modelplane's pin: it's gateway-path
# contract surface, not hardware, so no generator resolves it.
_TRUST_MANAGER_VERSION = "v0.25.0"

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
        # Envoy AI Gateway v1.1.x is tested against Envoy Gateway v1.8.x
        # with Gateway API v1.5.x, so v1.9.x is out of range until the AI
        # Gateway release that pairs with it.
        version="v1.8.4",
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
        # logRequestHeaderAttributes copies the caller identity into the
        # io.envoy.ai_gateway metadata namespace, so an InferenceGateway's
        # access log can read the caller from metadata rather than the
        # request header. The header is stripped before a request reaches
        # a backend Modelplane doesn't operate, so reading the log from the
        # header would lose the caller from exactly the third-party records
        # that attribute provider spend.
        #
        # Not left unset: unset, the controller defaults the mapping to
        # "agent-session-id:session.id", and any non-empty mapping makes the
        # PostTranslateModify hook walk every listener for an HTTP connection
        # manager and error on the first filter chain without one, taking a
        # co-located TCP/UDPRoute Gateway's whole xDS update down with it
        # (envoyproxy/ai-gateway#2600, fix in flight as #2601). That failure
        # is loud; losing the caller is silent.
        values={"controller": {"logRequestHeaderAttributes": f"{_CALLER_HEADER}:caller"}},
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
                "metadata": {"name": "cluster-gateway", "namespace": "modelplane-system"},
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
    # The cluster gateway's mTLS trust anchor. A self-signed Issuer signs the
    # per-cluster CA (fn.py's compose_gateway_pki), which signs the gateway's
    # serving certificate and validates an InferenceGateway's client one. Composed
    # on every cluster, not just one InferenceGateways route to: any cluster may
    # host an InferenceGateway whose client PKI needs it, and an Issuer costs
    # nothing.
    # Ordered after cert-manager (its CRDs and admission webhook) and the
    # namespace it lives in.
    Manifests(
        key="gateway-selfsigned-issuer",
        depends_on=["cert-manager", "gateway-namespace"],
        manifests=[
            {
                "apiVersion": "cert-manager.io/v1",
                "kind": "Issuer",
                "metadata": {"name": SELFSIGNED_ISSUER, "namespace": "modelplane-system"},
                "spec": {"selfSigned": {}},
            },
        ],
    ),
    # trust-manager republishes the cluster CA's certificate into a ConfigMap
    # without its private key, so the control plane reads the certificate
    # through a Bundle (fn.py's compose_gateway_pki) rather than the Secret that
    # holds the key too.
    #
    # Ordered after the self-signed Issuer, not just cert-manager: this chart
    # ships its own Issuer and Certificate for its webhook, and Helm applies
    # custom resources last, so installed alongside cert-manager they lose a
    # race with its validating webhook and the release fails terminally
    # (nothing sets rollbackLimit). An Issuer we composed reporting Ready proves
    # the same webhook admits the same kind, and a component's install gate
    # waits for exactly that.
    Chart(
        key="trust-manager",
        release="mp-trust-manager",
        namespace="modelplane-system",
        chart="trust-manager",
        repository="oci://quay.io/jetstack/charts",
        version=_TRUST_MANAGER_VERSION,
        depends_on=["gateway-selfsigned-issuer"],
        values={
            # Kept because the Bundles are Objects owned by other XRs, and an
            # Object whose CRD has gone can't be observed, so it never
            # finalizes.
            "crds": {"enabled": True, "keep": True},
            "app": {"trust": {"namespace": "modelplane-system"}},
            # The default package is a public-CA trust store, for Bundles that
            # set useDefaultCAs. These trust one private CA each, so disabling
            # it drops an init container and the image pull it waits on.
            "defaultPackage": {"enabled": False},
        },
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
