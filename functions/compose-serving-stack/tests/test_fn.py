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

"""Tests for the compose-serving-stack function.

Two layers. The Case table compares whole RunFunctionResponses for the
Existing/Dynamo stack across the reconcile passes; its expectations are
built from the provider models with literal arguments typed here, never
from the stacks package, so a stack-data change shows up as a test diff.
The golden inventory then pins the composed-resource key set - the
identity contract; renaming a key deletes and recreates the remote
resource - for every cloud and stack, as frozen literals.
"""

import copy
import dataclasses
import pathlib
import unittest

import yaml
from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.infrastructure.servingstack import v1alpha1
from models.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1
from models.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.crossplane.m.kubernetes.providerconfig import (
    v1alpha1 as k8spcv1alpha1,
)
from models.io.crossplane.protection.usage import v1beta1 as usagev1beta1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


# Precomputed child_name value for test-backend.
_PC_NAME = "test-backend-cluster-63fde"

_RELEASE_REF = ("helm.m.crossplane.io/v1beta1", "Release")
_OBJECT_REF = ("kubernetes.m.crossplane.io/v1alpha1", "Object")

_GATEWAY_READY_CEL = "has(object.status.addresses) && object.status.addresses.size() > 0"
_MODELEXPRESS_READY_CEL = (
    'has(object.status.conditions) && object.status.conditions.exists(c, c.type == "Available" && c.status == "True")'
)

# Resolve the vendored CRD bundles via the installed function package:
# the sandboxed test check runs against the venv's copy, not the tree.
_CRDS_DIR = pathlib.Path(fn.__file__).parent / "stacks" / "crds"


def _crds(filename: str) -> list[dict]:
    """The CRDs a vendored bundle carries, content straight from the file."""
    return [
        doc
        for doc in yaml.safe_load_all((_CRDS_DIR / filename).read_text())
        if doc and doc.get("kind") == "CustomResourceDefinition"
    ]


def _request(cloud: str, stack: str, observed: dict | None = None) -> fnv1.RunFunctionRequest:
    """Build a RunFunctionRequest for a test-backend ServingStack."""
    return fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(
                resource=resource.dict_to_struct(
                    v1alpha1.ServingStack(
                        metadata=metav1.ObjectMeta(name="test-backend", namespace="test-ns"),
                        spec=v1alpha1.Spec(
                            cloud=cloud,  # ty: ignore[invalid-argument-type]  # cases pass values of the literal
                            stack=stack,  # ty: ignore[invalid-argument-type]
                            secrets=[
                                v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                                v1alpha1.Secret(
                                    type="GoogleApplicationCredentials", name="sa-secret", key="private_key"
                                ),
                            ],
                        ),
                    ).model_dump(exclude_none=True, mode="json")
                ),
            ),
            resources=observed or {},
        ),
    )


def _release(
    key: str,
    release: str,
    namespace: str,
    chart: str,
    repository: str,
    version: str,
    values: dict | None = None,
    *,
    wait: bool = False,
) -> fnv1.Resource:
    """The expected Release for a Chart entry, built from literal arguments."""
    model = helmv1beta1.Release(
        metadata=metav1.ObjectMeta(
            annotations={"crossplane.io/external-name": release},
            labels={"modelplane.ai/resource": key},
        ),
        spec=helmv1beta1.Spec(
            providerConfigRef=helmv1beta1.ProviderConfigRef(kind="ProviderConfig", name=_PC_NAME),
            forProvider=helmv1beta1.ForProvider(
                chart=helmv1beta1.Chart(name=chart, repository=repository, version=version),
                namespace=namespace,
            ),
        ),
    )
    if wait:
        model.spec.forProvider.wait = True
        model.spec.forProvider.waitTimeout = "10m"
    if values:
        model.spec.forProvider.values = values
    res = fnv1.Resource()
    resource.update(res, model)
    return res


def _object(key: str, manifest: dict, cel: str | None = None) -> fnv1.Resource:
    """The expected Object for one manifest, built from literal arguments."""
    model = k8sobjv1alpha1.Object(
        metadata=metav1.ObjectMeta(labels={"modelplane.ai/resource": key}),
        spec=k8sobjv1alpha1.Spec(
            providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(kind="ProviderConfig", name=_PC_NAME),
            forProvider=k8sobjv1alpha1.ForProvider(manifest=manifest),
        ),
    )
    if cel is not None:
        model.spec.readiness = k8sobjv1alpha1.Readiness(policy="DeriveFromCelQuery", celQuery=cel)
    res = fnv1.Resource()
    resource.update(res, model)
    return res


def _usage(of_ref: tuple[str, str], of_key: str, by_ref: tuple[str, str], by_key: str) -> fnv1.Resource:
    """The expected teardown Usage for one dependency edge, ready on arrival."""
    res = fnv1.Resource()
    resource.update(
        res,
        usagev1beta1.Usage(
            spec=usagev1beta1.Spec(
                of=usagev1beta1.Of(
                    apiVersion=of_ref[0],
                    kind=of_ref[1],
                    resourceSelector=usagev1beta1.ResourceSelectorModel(
                        matchControllerRef=True,
                        matchLabels={"modelplane.ai/resource": of_key},
                    ),
                ),
                by=usagev1beta1.By(
                    apiVersion=by_ref[0],
                    kind=by_ref[1],
                    resourceSelector=usagev1beta1.ResourceSelector(
                        matchControllerRef=True,
                        matchLabels={"modelplane.ai/resource": by_key},
                    ),
                ),
                replayDeletion=True,
            ),
        ),
    )
    res.ready = fnv1.READY_TRUE
    return res


def _provider_configs(*, ready: bool = True) -> dict[str, fnv1.Resource]:
    """The two expected ProviderConfigs.

    Ready only once observed: on the first pass they and the Usages are
    the whole desired state, and ready-on-arrival would let the
    composite report Ready before any stack component exists.
    """
    k8s = fnv1.Resource()
    resource.update(
        k8s,
        k8spcv1alpha1.ProviderConfig(
            metadata=metav1.ObjectMeta(name=_PC_NAME),
            spec=k8spcv1alpha1.Spec(
                credentials=k8spcv1alpha1.Credentials(
                    source="Secret",
                    secretRef=k8spcv1alpha1.SecretRef(name="kube-secret", namespace="test-ns", key="kubeconfig"),
                ),
                identity=k8spcv1alpha1.Identity(
                    type="GoogleApplicationCredentials",
                    source="Secret",
                    secretRef=k8spcv1alpha1.SecretRef(name="sa-secret", namespace="test-ns", key="private_key"),
                ),
            ),
        ),
    )
    if ready:
        k8s.ready = fnv1.READY_TRUE
    helm = fnv1.Resource()
    resource.update(
        helm,
        helmpcv1beta1.ProviderConfig(
            metadata=metav1.ObjectMeta(name=_PC_NAME),
            spec=helmpcv1beta1.Spec(
                credentials=helmpcv1beta1.Credentials(
                    source="Secret",
                    secretRef=helmpcv1beta1.SecretRef(name="kube-secret", namespace="test-ns", key="kubeconfig"),
                ),
                identity=helmpcv1beta1.Identity(
                    type="GoogleApplicationCredentials",
                    source="Secret",
                    secretRef=helmpcv1beta1.SecretRef(name="sa-secret", namespace="test-ns", key="private_key"),
                ),
            ),
        ),
    )
    if ready:
        helm.ready = fnv1.READY_TRUE
    return {"provider-config-kubernetes": k8s, "provider-config-helm": helm}


def _observed_pcs() -> dict[str, fnv1.Resource]:
    """Observed ProviderConfigs, which gate the rest of the stack open."""
    return {
        "provider-config-kubernetes": fnv1.Resource(
            resource=resource.dict_to_struct(
                {"apiVersion": "kubernetes.m.crossplane.io/v1alpha1", "kind": "ProviderConfig"}
            )
        ),
        "provider-config-helm": fnv1.Resource(
            resource=resource.dict_to_struct({"apiVersion": "helm.m.crossplane.io/v1beta1", "kind": "ProviderConfig"})
        ),
    }


# The Usages every Existing/Dynamo pass composes: the two hand-written
# gateway-chain edges, and one derived edge per depends_on in the joined
# stack data.
_EXISTING_DYNAMO_USAGES = {
    "usage-gateway-class-by-gateway": _usage(_OBJECT_REF, "gateway-class", _OBJECT_REF, "gateway"),
    "usage-envoy-gateway-by-gateway-class": _usage(_RELEASE_REF, "envoy-gateway", _OBJECT_REF, "gateway-class"),
    "usage-cert-manager-by-envoy-gateway": _usage(_RELEASE_REF, "cert-manager", _RELEASE_REF, "envoy-gateway"),
    "usage-ai-gateway-crds-by-ai-gateway": _usage(_RELEASE_REF, "ai-gateway-crds", _RELEASE_REF, "ai-gateway"),
    "usage-gateway-namespace-by-gateway-proxy": _usage(_OBJECT_REF, "gateway-namespace", _OBJECT_REF, "gateway-proxy"),
    "usage-kai-scheduler-by-kai-queue-root": _usage(_RELEASE_REF, "kai-scheduler", _OBJECT_REF, "kai-queue-root"),
    "usage-kai-scheduler-by-kai-queue": _usage(_RELEASE_REF, "kai-scheduler", _OBJECT_REF, "kai-queue"),
    "usage-modelexpress-crds-modelmetadatas.modelexpress.nvidia.com-by-modelexpress-server": _usage(
        _OBJECT_REF, "modelexpress-crds-modelmetadatas.modelexpress.nvidia.com", _OBJECT_REF, "modelexpress-server"
    ),
    "usage-modelexpress-crds-modelcacheentries.modelexpress.nvidia.com-by-modelexpress-server": _usage(
        _OBJECT_REF, "modelexpress-crds-modelcacheentries.modelexpress.nvidia.com", _OBJECT_REF, "modelexpress-server"
    ),
}


def _kai_queue(name: str, parent: str | None) -> dict:
    spec: dict = {
        "resources": {
            "cpu": {"quota": -1, "limit": -1, "overQuotaWeight": 1},
            "gpu": {"quota": -1, "limit": -1, "overQuotaWeight": 1},
            "memory": {"quota": -1, "limit": -1, "overQuotaWeight": 1},
        },
    }
    if parent:
        spec["parentQueue"] = parent
    return {"apiVersion": "scheduling.run.ai/v2", "kind": "Queue", "metadata": {"name": name}, "spec": spec}


_MX_META = {"name": "modelexpress-server", "namespace": "default"}
_MX_SELECT = {"modelplane.ai/modelexpress": "modelexpress-server"}


def _existing_dynamo_stack() -> dict[str, fnv1.Resource]:
    """Every component the Existing/Dynamo stack renders, as literals."""
    out: dict[str, fnv1.Resource] = {}

    # --- the Existing cloud half (hand-written Modelplane pins) ---
    out["cert-manager"] = _release(
        key="cert-manager",
        release="mp-cert-manager",
        namespace="cert-manager",
        chart="cert-manager",
        repository="https://charts.jetstack.io",
        version="v1.20.2",
        wait=True,
        values={"crds": {"enabled": True, "keep": False}},
    )
    out["kube-prometheus-stack"] = _release(
        key="kube-prometheus-stack",
        release="mp-kube-prometheus-stack",
        namespace="monitoring",
        chart="kube-prometheus-stack",
        repository="https://prometheus-community.github.io/helm-charts",
        version="84.4.0",
        values={
            "fullnameOverride": "prometheus",
            "prometheus": {
                "prometheusSpec": {
                    "podMonitorSelectorNilUsesHelmValues": False,
                    "podMonitorNamespaceSelector": {},
                    "additionalScrapeConfigs": [
                        {
                            "job_name": "envoy-gateway-proxy",
                            "kubernetes_sd_configs": [
                                {"role": "pod", "namespaces": {"names": ["envoy-gateway-system"]}},
                            ],
                            "relabel_configs": [
                                {
                                    "source_labels": [
                                        "__meta_kubernetes_pod_label_app_kubernetes_io_component",
                                    ],
                                    "action": "keep",
                                    "regex": "proxy",
                                },
                                {
                                    "source_labels": ["__address__"],
                                    "action": "replace",
                                    "regex": "([^:]+)(?::\\d+)?",
                                    "replacement": "$1:19001",
                                    "target_label": "__address__",
                                },
                            ],
                            "metrics_path": "/stats/prometheus",
                        },
                    ],
                },
            },
            "grafana": {"enabled": False},
            "alertmanager": {"enabled": False},
        },
    )
    out["node-feature-discovery"] = _release(
        key="node-feature-discovery",
        release="mp-node-feature-discovery",
        namespace="node-feature-discovery",
        chart="node-feature-discovery",
        repository="https://kubernetes-sigs.github.io/node-feature-discovery/charts",
        version="0.19.0",
        values={
            "worker": {
                "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}],
            },
        },
    )
    out["nvidia-dra-driver-gpu"] = _release(
        key="nvidia-dra-driver-gpu",
        release="mp-dra-driver-nvidia-gpu",
        namespace="nvidia-dra-driver",
        chart="dra-driver-nvidia-gpu",
        repository="oci://registry.k8s.io/dra-driver-nvidia/charts",
        version="0.4.1",
        values={
            "gpuResourcesEnabledOverride": True,
            "resources": {"computeDomains": {"enabled": False}},
        },
    )

    # --- the common half ---
    out["envoy-gateway"] = _release(
        key="envoy-gateway",
        release="mp-gateway-helm",
        namespace="envoy-gateway-system",
        chart="gateway-helm",
        repository="oci://docker.io/envoyproxy",
        version="v1.8.1",
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
                                "hostname": "ai-gateway-controller.envoy-ai-gateway-system.svc.cluster.local",
                                "port": 1063,
                            },
                        },
                        "backendResources": [
                            {"group": "inference.networking.k8s.io", "kind": "InferencePool", "version": "v1"},
                        ],
                    },
                },
            },
        },
    )
    out["ai-gateway-crds"] = _release(
        key="ai-gateway-crds",
        release="mp-ai-gateway-crds-helm",
        namespace="envoy-ai-gateway-system",
        chart="ai-gateway-crds-helm",
        repository="oci://docker.io/envoyproxy",
        version="v0.7.0",
        wait=True,
    )
    out["ai-gateway"] = _release(
        key="ai-gateway",
        release="mp-ai-gateway-helm",
        namespace="envoy-ai-gateway-system",
        chart="ai-gateway-helm",
        repository="oci://docker.io/envoyproxy",
        version="v0.7.0",
    )
    for doc in _crds("gaie.yaml"):
        key = f"gaie-crds-{doc['metadata']['name']}"
        out[key] = _object(key, doc)
    out["gateway-namespace"] = _object(
        "gateway-namespace",
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "modelplane-system"}},
    )
    out["gateway-proxy"] = _object(
        "gateway-proxy",
        {
            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
            "kind": "EnvoyProxy",
            "metadata": {"name": "inference-gateway", "namespace": "modelplane-system"},
            "spec": {
                "provider": {
                    "type": "Kubernetes",
                    "kubernetes": {"envoyService": {"externalTrafficPolicy": "Cluster"}},
                },
            },
        },
    )
    out["dra-driver-critical-pods-quota"] = _object(
        "dra-driver-critical-pods-quota",
        {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {"name": "allow-critical-pods", "namespace": "nvidia-dra-driver"},
            "spec": {
                "hard": {"pods": "1000"},
                "scopeSelector": {
                    "matchExpressions": [
                        {
                            "operator": "In",
                            "scopeName": "PriorityClass",
                            "values": ["system-node-critical", "system-cluster-critical"],
                        },
                    ],
                },
            },
        },
    )

    # --- the Dynamo half ---
    out["grove"] = _release(
        key="grove",
        release="mp-grove-charts",
        namespace="grove-system",
        chart="grove-charts",
        repository="oci://ghcr.io/ai-dynamo/grove",
        version="v0.1.0-alpha.12-rc2",
    )
    out["kai-scheduler"] = _release(
        key="kai-scheduler",
        release="mp-kai-scheduler",
        namespace="kai-scheduler",
        chart="kai-scheduler",
        repository="oci://ghcr.io/kai-scheduler/kai-scheduler",
        version="v0.16.8",
        wait=True,
    )
    out["kai-queue-root"] = _object("kai-queue-root", _kai_queue("modelplane-root", None))
    out["kai-queue"] = _object("kai-queue", _kai_queue("modelplane", "modelplane-root"))
    for doc in _crds("modelexpress.yaml"):
        key = f"modelexpress-crds-{doc['metadata']['name']}"
        out[key] = _object(key, doc)
    out["modelexpress-server-sa"] = _object(
        "modelexpress-server-sa",
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": _MX_META},
    )
    out["modelexpress-server-role"] = _object(
        "modelexpress-server-role",
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": _MX_META,
            "rules": [
                {
                    "apiGroups": ["modelexpress.nvidia.com"],
                    "resources": ["modelmetadatas", "modelmetadatas/status"],
                    "verbs": ["get", "list", "create", "update", "patch", "delete"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["configmaps"],
                    "verbs": ["get", "list", "create", "update", "patch", "delete"],
                },
                {
                    "apiGroups": ["modelexpress.nvidia.com"],
                    "resources": ["modelcacheentries", "modelcacheentries/status"],
                    "verbs": ["get", "list", "create", "update", "patch", "delete"],
                },
            ],
        },
    )
    out["modelexpress-server-rolebinding"] = _object(
        "modelexpress-server-rolebinding",
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": _MX_META,
            "subjects": [{"kind": "ServiceAccount", "name": "modelexpress-server", "namespace": "default"}],
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "modelexpress-server"},
        },
    )
    out["modelexpress-server-svc"] = _object(
        "modelexpress-server-svc",
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": _MX_META,
            "spec": {
                "selector": _MX_SELECT,
                "ports": [{"name": "grpc", "port": 8001, "targetPort": 8001}],
            },
        },
    )
    out["modelexpress-server"] = _object(
        "modelexpress-server",
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": _MX_META,
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": _MX_SELECT},
                "template": {
                    "metadata": {"labels": _MX_SELECT},
                    "spec": {
                        "serviceAccountName": "modelexpress-server",
                        "containers": [
                            {
                                "name": "modelexpress-server",
                                "image": "nvcr.io/nvidia/ai-dynamo/modelexpress-server:0.4.1",
                                "ports": [{"containerPort": 8001}],
                                "env": [
                                    {"name": "MODEL_EXPRESS_CACHE_DIRECTORY", "value": "/mnt/models"},
                                    {"name": "HF_HUB_CACHE", "value": "/mnt/models"},
                                    {"name": "MX_METADATA_BACKEND", "value": "kubernetes"},
                                    {
                                        "name": "POD_NAMESPACE",
                                        "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
                                    },
                                ],
                                "volumeMounts": [{"name": "cache", "mountPath": "/mnt/models"}],
                                "readinessProbe": {"tcpSocket": {"port": 8001}, "periodSeconds": 10},
                                "livenessProbe": {"tcpSocket": {"port": 8001}, "periodSeconds": 20},
                            },
                        ],
                        "volumes": [{"name": "cache", "emptyDir": {}}],
                    },
                },
            },
        },
        cel=_MODELEXPRESS_READY_CEL,
    )

    # --- the hand-rendered gateway pair ---
    out["gateway-class"] = _object(
        "gateway-class",
        {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "GatewayClass",
            "metadata": {"name": "envoy"},
            "spec": {
                "controllerName": "gateway.envoyproxy.io/gatewayclass-controller",
                "parametersRef": {
                    "group": "gateway.envoyproxy.io",
                    "kind": "EnvoyProxy",
                    "name": "inference-gateway",
                    "namespace": "modelplane-system",
                },
            },
        },
    )
    out["gateway"] = _object(
        "gateway",
        {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "Gateway",
            "metadata": {"name": "inference-gateway", "namespace": "modelplane-system"},
            "spec": {
                "gatewayClassName": "envoy",
                "listeners": [
                    {
                        "name": "http",
                        "protocol": "HTTP",
                        "port": 80,
                        "allowedRoutes": {"namespaces": {"from": "All"}},
                    },
                ],
            },
        },
        cel=_GATEWAY_READY_CEL,
    )

    return out


def _response(resources: dict[str, fnv1.Resource], status: dict | None = None) -> fnv1.RunFunctionResponse:
    """A whole expected response: 60s TTL, empty context, the XR status."""
    return fnv1.RunFunctionResponse(
        meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
        desired=fnv1.State(
            composite=fnv1.Resource(resource=resource.dict_to_struct({"status": status if status is not None else {}})),
            resources=resources,
        ),
        context=structpb.Struct(),
    )


@dataclasses.dataclass
class Case:
    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:
        full = _provider_configs() | _EXISTING_DYNAMO_USAGES | _existing_dynamo_stack()

        # Second pass: PCs observed. depends_on gates first creation, so
        # only the dependency-free wave renders; each dependent waits for
        # its dependency's Ready before it is first created.
        dep_gated = {
            "envoy-gateway",  # -> cert-manager
            "ai-gateway",  # -> ai-gateway-crds
            "gateway-proxy",  # -> gateway-namespace
            "kai-queue-root",  # -> kai-scheduler
            "kai-queue",  # -> kai-scheduler
            "modelexpress-server",  # -> modelexpress-crds
        }
        first_wave = {k: v for k, v in full.items() if k not in dep_gated}

        # Third pass: every rendered resource observed Ready (the gateway
        # with its address assigned), so everything is marked ready and
        # the address lands in the XR status.
        rendered = [k for k in _existing_dynamo_stack() if k != "gateway"]
        observed_ready = _observed_pcs()
        for key in rendered:
            observed_ready[key] = fnv1.Resource(
                resource=resource.dict_to_struct({"status": {"conditions": [{"type": "Ready", "status": "True"}]}})
            )
        observed_ready["gateway"] = fnv1.Resource(
            resource=resource.dict_to_struct(
                {
                    "status": {
                        "conditions": [{"type": "Ready", "status": "True"}],
                        "atProvider": {
                            "manifest": {"status": {"addresses": [{"type": "IPAddress", "value": "203.0.113.7"}]}},
                        },
                    },
                }
            )
        )
        # Every component observed Ready; PCs and Usages are ready on arrival.
        all_ready = copy.deepcopy(full)
        for res in all_ready.values():
            res.ready = fnv1.READY_TRUE

        cases = [
            Case(
                name="first pass composes only the provider configs and usages",
                req=_request("Existing", "Dynamo"),
                # Everything targeting the remote cluster is gated on the
                # ProviderConfigs having been observed; Usages reference
                # nothing remote and compose immediately. The unready
                # ProviderConfigs keep the composite unready until the
                # stack actually renders.
                want=_response(_provider_configs(ready=False) | _EXISTING_DYNAMO_USAGES),
            ),
            Case(
                name="second pass renders the dependency-free wave",
                req=_request("Existing", "Dynamo", observed=_observed_pcs()),
                want=_response(first_wave),
            ),
            Case(
                name="all dependencies ready renders the whole stack, marks it ready, and writes the gateway address",
                req=_request("Existing", "Dynamo", observed=observed_ready),
                want=_response(all_ready, status={"gateway": {"address": "203.0.113.7"}}),
            ),
        ]
        for case in cases:
            with self.subTest(case.name):
                got = await self.runner.RunFunction(case.req, None)
                self.assertEqual(
                    json_format.MessageToDict(case.want),
                    json_format.MessageToDict(got),
                    "-want, +got",
                )

    async def test_identity_secret_type_flows_to_provider_configs(self) -> None:
        """A non-GCP identity secret's type is stamped verbatim on both
        ProviderConfigs rather than being forced to GoogleApplicationCredentials,
        and its own namespace wins over the XR's."""
        req = fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        v1alpha1.ServingStack(
                            metadata=metav1.ObjectMeta(name="test-backend", namespace="test-ns"),
                            spec=v1alpha1.Spec(
                                cloud="Nebius",
                                secrets=[
                                    v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                                    v1alpha1.Secret(
                                        type="NebiusServiceAccountCredentials",
                                        name="nebius-secret",
                                        key="credentials.json",
                                        namespace="other-ns",
                                    ),
                                ],
                            ),
                        ).model_dump(exclude_none=True, mode="json")
                    ),
                ),
            ),
        )
        got = await self.runner.RunFunction(req, None)
        pc = resource.struct_to_dict(got.desired.resources["provider-config-kubernetes"].resource)
        self.assertEqual("NebiusServiceAccountCredentials", pc["spec"]["identity"]["type"])
        self.assertEqual("other-ns", pc["spec"]["identity"]["secretRef"]["namespace"])
        helm_pc = resource.struct_to_dict(got.desired.resources["provider-config-helm"].resource)
        self.assertEqual("NebiusServiceAccountCredentials", helm_pc["spec"]["identity"]["type"])


# The composed-resource key a component renders under is its identity:
# renaming one deletes and recreates the remote resource (for an Object
# holding a CRD, the CRD and its CRs). This pins the full key set per
# cloud and stack, including the Usage keys derived from depends_on, as
# reviewed literals. A failure here means the stack data changed a key -
# make sure that's intended, then update the inventory and the release
# notes.

_ALWAYS = frozenset(
    {
        "provider-config-kubernetes",
        "provider-config-helm",
        "gateway",
        "gateway-class",
        "usage-gateway-class-by-gateway",
        "usage-envoy-gateway-by-gateway-class",
    }
)

_COMMON = frozenset(
    {
        "ai-gateway",
        "ai-gateway-crds",
        "dra-driver-critical-pods-quota",
        "envoy-gateway",
        "gaie-crds-inferenceobjectives.inference.networking.x-k8s.io",
        "gaie-crds-inferencepools.inference.networking.k8s.io",
        "gaie-crds-inferencepools.inference.networking.x-k8s.io",
        "gateway-namespace",
        "gateway-proxy",
        "usage-ai-gateway-crds-by-ai-gateway",
        "usage-cert-manager-by-envoy-gateway",
        "usage-gateway-namespace-by-gateway-proxy",
    }
)

_STANDARD = frozenset(
    {
        "leader-worker-set",
    }
)

_DYNAMO = frozenset(
    {
        "grove",
        "kai-queue",
        "kai-queue-root",
        "kai-scheduler",
        "modelexpress-crds-modelcacheentries.modelexpress.nvidia.com",
        "modelexpress-crds-modelmetadatas.modelexpress.nvidia.com",
        "modelexpress-server",
        "modelexpress-server-role",
        "modelexpress-server-rolebinding",
        "modelexpress-server-sa",
        "modelexpress-server-svc",
        "usage-kai-scheduler-by-kai-queue",
        "usage-kai-scheduler-by-kai-queue-root",
        "usage-modelexpress-crds-modelcacheentries.modelexpress.nvidia.com-by-modelexpress-server",
        "usage-modelexpress-crds-modelmetadatas.modelexpress.nvidia.com-by-modelexpress-server",
    }
)

_EKS = frozenset(
    {
        "cert-manager",
        "gpu-operator",
        "k8s-ephemeral-storage-metrics",
        "kube-prometheus-stack",
        "node-feature-discovery",
        "nodewright-operator",
        "nvidia-dra-driver-gpu",
        "nvsentinel",
        "prometheus-adapter",
        "prometheus-operator-crds",
        "usage-cert-manager-by-gpu-operator",
        "usage-cert-manager-by-nvsentinel",
        "usage-gpu-operator-by-nvidia-dra-driver-gpu",
        "usage-gpu-operator-by-nvsentinel",
        "usage-kube-prometheus-stack-by-gpu-operator",
        "usage-kube-prometheus-stack-by-k8s-ephemeral-storage-metrics",
        "usage-kube-prometheus-stack-by-prometheus-adapter",
        "usage-node-feature-discovery-by-gpu-operator",
        "usage-prometheus-operator-crds-by-k8s-ephemeral-storage-metrics",
        "usage-prometheus-operator-crds-by-kube-prometheus-stack",
        "usage-prometheus-operator-crds-by-nvsentinel",
    }
)

# AKS additionally carries the gpu-operator's toolkit-hardening manifest.
_AKS = _EKS | frozenset(
    {
        "gpu-operator-manifests",
        "usage-gpu-operator-by-gpu-operator-manifests",
    }
)

_GKE = _EKS

_HAND_WRITTEN = frozenset(
    {
        "cert-manager",
        "kube-prometheus-stack",
        "node-feature-discovery",
        "nvidia-dra-driver-gpu",
    }
)

# VKE pre-installs NFD via its managed GPU Operator add-on, so the
# Vultr half carries no node-feature-discovery of its own.
_VULTR = _HAND_WRITTEN - frozenset({"node-feature-discovery"})

_INVENTORY = {
    "EKS": _EKS,
    "AKS": _AKS,
    "GKE": _GKE,
    "Nebius": _HAND_WRITTEN,
    "Vultr": _VULTR,
    "Existing": _HAND_WRITTEN,
}


class TestKeyInventory(unittest.IsolatedAsyncioTestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_composed_resource_keys(self) -> None:
        for cloud, cloud_keys in _INVENTORY.items():
            for stack, stack_keys in (("Standard", _STANDARD), ("Dynamo", _DYNAMO)):
                with self.subTest(cloud=cloud, stack=stack):
                    expected = _ALWAYS | _COMMON | cloud_keys | stack_keys
                    # Observe every expected key Ready so the depends_on
                    # install gate opens and the full stack renders; a
                    # key the function doesn't render still fails the
                    # comparison.
                    observed = _observed_pcs()
                    for key in expected:
                        observed[key] = fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}
                            )
                        )
                    got = await self.runner.RunFunction(_request(cloud, stack, observed=observed), None)
                    self.assertEqual(expected, set(got.desired.resources.keys()))
