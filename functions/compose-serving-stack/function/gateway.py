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

"""The cluster gateway pair, rendered from the XR's spec.

The GatewayClass and Gateway read spec.gateway (className, hostname),
which is per-cluster configuration rather than a software version, so
they can't live in the stacks package. Everything around them is stack
data - the gateway namespace and the EnvoyProxy they reference come from
the stack's common components, and Envoy Gateway itself is a component.
fn.py renders these manifests with the same machinery it renders a
Manifests entry with.
"""

from typing import Any

from models.ai.modelplane.infrastructure.servingstack import v1alpha1

# The Secret cert-manager issues the gateway's serving certificate into (see
# fn.compose_gateway_pki). The HTTPS listener terminates TLS with it.
_GATEWAY_SERVING_SECRET = "cluster-gateway-serving"

# Label compose-model-replica stamps on the namespaces it mirrors onto this
# cluster. The gateway's listeners select on it so a replica's HTTPRoute attaches
# from its own team's namespace.
_NS_LABEL = "modelplane.ai/namespace"

# CEL readiness query for the Gateway Object. The Gateway's LoadBalancer
# address is assigned asynchronously by the controller after the Object is
# applied. With the default SuccessfulCreate policy the Object is Ready the
# instant it's created, so provider-kubernetes' poll-interval hook re-observes
# it only on the slow (10m) drift poll - leaving status.atProvider.manifest
# frozen at a pre-address snapshot, and the downstream scheduler with no gateway
# address, for up to ~10m. Gating readiness on status.addresses keeps the Object
# un-Ready until the address is observed, which drops the poll to ~30s so the
# address propagates promptly. `object` is the observed Gateway manifest; the
# has() guard keeps the query false (not erroring) before the controller first
# writes status.addresses.
READY_CEL = "has(object.status.addresses) && object.status.addresses.size() > 0"


def objects(gw: v1alpha1.Gateway) -> list[tuple[str, dict[str, Any], str | None]]:
    """The gateway pair as (key, manifest, readiness CEL) triples."""
    return [
        (
            "gateway-class",
            {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "GatewayClass",
                "metadata": {"name": gw.className},
                "spec": {
                    "controllerName": "gateway.envoyproxy.io/gatewayclass-controller",
                    # The EnvoyProxy the stack's common components lay
                    # down in modelplane-system.
                    "parametersRef": {
                        "group": "gateway.envoyproxy.io",
                        "kind": "EnvoyProxy",
                        "name": "cluster-gateway",
                        "namespace": "modelplane-system",
                    },
                },
            },
            None,
        ),
        (
            "gateway",
            {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "Gateway",
                "metadata": {
                    "name": "cluster-gateway",
                    "namespace": "modelplane-system",
                },
                "spec": {
                    "gatewayClassName": gw.className,
                    # One HTTPS listener and nothing else. The gateway's Service
                    # is a public load balancer with a port per listener, and the
                    # serving HTTPRoutes carry no sectionName, so they attach to
                    # every listener there is: an HTTP listener would serve the
                    # engines to anyone, with no certificate asked for. The
                    # ClientTrafficPolicy demanding one is composed with the
                    # client CAs, and fn.serves_gateway withholds this Gateway
                    # until there are some.
                    "listeners": [
                        {
                            "name": "https",
                            "protocol": "HTTPS",
                            "port": 443,
                            "hostname": gw.hostname,
                            "tls": {
                                "mode": "Terminate",
                                "certificateRefs": [{"name": _GATEWAY_SERVING_SECRET}],
                            },
                            # Serving HTTPRoutes attach from the mirrored team
                            # namespaces, selected by the label they carry.
                            "allowedRoutes": {
                                "namespaces": {
                                    "from": "Selector",
                                    "selector": {"matchExpressions": [{"key": _NS_LABEL, "operator": "Exists"}]},
                                }
                            },
                        }
                    ],
                },
            },
            READY_CEL,
        ),
    ]
