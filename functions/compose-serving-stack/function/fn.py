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

"""Install the serving substrate on a remote cluster.

This function composes the serving substrate (the cluster-side CRDs,
controllers, and gateway) that the native and llm-d model-serving backends
depend on: cert-manager, Envoy Gateway, the Envoy AI Gateway and Gateway API
Inference Extension (which together route HTTPRoute -> InferencePool backendRefs
for disaggregated serving), Prometheus, LeaderWorkerSet, and an inference
Gateway. Resources are composed as Helm releases and
provider-kubernetes Objects, all targeting the remote cluster via
ProviderConfigs.

Usage resources protect ProviderConfigs from premature deletion during
teardown, ensuring Helm releases can uninstall before losing connectivity.
"""

import pathlib

import grpc
import yaml
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.infrastructure.servingstack import v1alpha1
from models.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1
from models.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.crossplane.m.kubernetes.providerconfig import (
    v1alpha1 as k8spcv1alpha1,
)
from models.io.crossplane.protection.usage import v1beta1 as usagev1beta1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# Label key for composed resources that need deletion ordering via Usages.
_LABEL_RESOURCE = "modelplane.ai/resource"

# CEL readiness query for the Envoy Gateway Object. The Gateway's LoadBalancer
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
_GATEWAY_READY_CEL = "has(object.status.addresses) && object.status.addresses.size() > 0"

# Secret types that couple compose-gke-cluster (writer) to this function
# (reader) via the InferenceCluster status.
_SECRET_TYPE_KUBECONFIG = "Kubeconfig"
_SECRET_TYPE_GCP_SA_KEY = "GCPServiceAccountKey"

# Identity type for GCP service account credentials.
_IDENTITY_TYPE_GCP = "GoogleApplicationCredentials"

# Prometheus constants.
_PROMETHEUS_NAMESPACE = "monitoring"
_PROMETHEUS_FULLNAME_OVERRIDE = "prometheus"
_PROMETHEUS_URL = f"http://{_PROMETHEUS_FULLNAME_OVERRIDE}-prometheus.{_PROMETHEUS_NAMESPACE}.svc.cluster.local:9090"
_PROMETHEUS_CHART = "kube-prometheus-stack"
_PROMETHEUS_REPO = "https://prometheus-community.github.io/helm-charts"

_DRA_DRIVER_NAMESPACE = "dra-driver-nvidia-gpu"
# Upstream default for the DRA driver's NVIDIA_DRIVER_ROOT. A ServingStack whose
# nvidiaDriverRoot differs from this is on a platform (GKE) that relocates the
# driver and restricts system-critical pods, which needs both DRA accommodations.
_DEFAULT_NVIDIA_DRIVER_ROOT = "/"

# Envoy AI Gateway constants. The AI Gateway controller supplies the ext-proc
# extension server that Envoy Gateway delegates InferencePool backend resolution
# to, so HTTPRoute -> InferencePool backendRefs (disaggregated serving) route.
_AI_GATEWAY_NAMESPACE = "envoy-ai-gateway-system"
_AI_GATEWAY_REPO = "oci://docker.io/envoyproxy"
_AI_GATEWAY_VERSION = "v0.7.0"
_AI_GATEWAY_CONTROLLER_FQDN = f"ai-gateway-controller.{_AI_GATEWAY_NAMESPACE}.svc.cluster.local"
_AI_GATEWAY_CONTROLLER_PORT = 1063


# Gateway API Inference Extension (GAIE) CRDs, providing the InferencePool that
# disaggregated replicas front their decode endpoints with. Vendored from the
# upstream release's manifests.yaml.
_HERE = pathlib.Path(__file__).parent
_GAIE_CRDS = [
    doc
    for doc in yaml.safe_load_all((_HERE / "gaie_crds.yaml").read_text())
    if doc and doc.get("kind") == "CustomResourceDefinition"
]


def _gaie_crd_key(doc: dict) -> str:
    """Stable composed-resource key for a GAIE CRD."""
    return f"gaie-crd-{doc['metadata']['name']}"


def _helm_release(
    chart: str,
    repo: str,
    version: str,
    namespace: str,
    provider_config: str,
    values: dict | None = None,
    labels: dict | None = None,
    metadata_namespace: str | None = None,
) -> helmv1beta1.Release:
    """Build a Helm Release targeting a remote (or local) cluster."""
    md = None
    if labels or metadata_namespace:
        # Only set fields that are present; under exclude_unset, an explicit
        # namespace=None or labels=None would leak a null into the metadata.
        md = metav1.ObjectMeta(
            **({"namespace": metadata_namespace} if metadata_namespace is not None else {}),
            **({"labels": labels} if labels is not None else {}),
        )

    release = helmv1beta1.Release(
        # Only set metadata when present (see _k8s_object: avoids a null
        # metadata leaking under exclude_unset serialization).
        **({"metadata": md} if md is not None else {}),
        spec=helmv1beta1.Spec(
            providerConfigRef=helmv1beta1.ProviderConfigRef(
                kind="ProviderConfig",
                name=provider_config,
            ),
            forProvider=helmv1beta1.ForProvider(
                chart=helmv1beta1.Chart(
                    name=chart,
                    repository=repo,
                    version=version,
                ),
                namespace=namespace,
            ),
        ),
    )
    if values:
        release.spec.forProvider.values = values
    return release


def _k8s_object(
    provider_config: str,
    manifest: dict,
    metadata: metav1.ObjectMeta | None = None,
    management_policies: list | None = None,
    *,
    cel_query: str | None = None,
) -> k8sobjv1alpha1.Object:
    """Build a provider-kubernetes Object wrapping an arbitrary manifest.

    Readiness defaults to SuccessfulCreate (the Object is Ready once applied),
    which suits resources with no meaningful runtime readiness. Pass cel_query
    for an Object whose readiness must reflect a controller-populated field of
    the observed manifest - it selects the DeriveFromCelQuery policy with that
    query (see _GATEWAY_READY_CEL), which also keeps provider-kubernetes
    re-observing on its fast poll until the query passes.
    """
    obj = k8sobjv1alpha1.Object(
        # Only set metadata when present. Under exclude_unset serialization,
        # passing metadata=None would emit a null metadata into the composed
        # resource rather than omitting it.
        **({"metadata": metadata} if metadata is not None else {}),
        spec=k8sobjv1alpha1.Spec(
            providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                kind="ProviderConfig",
                name=provider_config,
            ),
            forProvider=k8sobjv1alpha1.ForProvider(
                manifest=manifest,
            ),
        ),
    )
    if management_policies:
        obj.spec.managementPolicies = management_policies
    if cel_query is not None:
        obj.spec.readiness = k8sobjv1alpha1.Readiness(
            policy="DeriveFromCelQuery",
            celQuery=cel_query,
        )
    return obj


def _prometheus_release(version: str, provider_config: str) -> helmv1beta1.Release:
    """Build a kube-prometheus-stack Helm release for a backend cluster."""
    return _helm_release(
        chart=_PROMETHEUS_CHART,
        repo=_PROMETHEUS_REPO,
        version=version,
        namespace=_PROMETHEUS_NAMESPACE,
        provider_config=provider_config,
        values={
            "fullnameOverride": _PROMETHEUS_FULLNAME_OVERRIDE,
            "prometheus": {
                "prometheusSpec": {
                    # Discover PodMonitors across all namespaces.
                    "podMonitorSelectorNilUsesHelmValues": False,
                    "podMonitorNamespaceSelector": {},
                    # Scrape Envoy Gateway proxy pods for upstream request
                    # metrics (envoy_cluster_upstream_rq_active). Envoy
                    # Gateway is used for ingress, and this metric measures
                    # in-flight requests at the proxy level.
                    "additionalScrapeConfigs": [
                        {
                            "job_name": "envoy-gateway-proxy",
                            "kubernetes_sd_configs": [
                                {
                                    "role": "pod",
                                    "namespaces": {
                                        "names": ["envoy-gateway-system"],
                                    },
                                },
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
            # Disable components we don't need for observability.
            "grafana": {"enabled": False},
            "alertmanager": {"enabled": False},
        },
    )


def _pc_name(xr):
    """Derive the ProviderConfig name from the XR."""
    return resource.child_name(xr.metadata.name, "cluster")


class FunctionRunner(grpcv1.FunctionRunnerServiceServicer):
    """A FunctionRunner handles gRPC RunFunctionRequests."""

    def __init__(self) -> None:
        """Create a new FunctionRunner."""
        self.log = logging.get_logger()

    async def RunFunction(
        self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext | None
    ) -> fnv1.RunFunctionResponse:  # ty: ignore[invalid-method-override]  # the generated grpc servicer base is untyped
        """Run the function."""
        log = self.log.bind(tag=req.meta.tag)
        log.info("Running function")

        rsp = response.to(req)
        c = Composer(req, rsp)
        c.compose()
        return rsp


class Composer:
    def __init__(self, req, rsp) -> None:
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.ServingStack(**resource.struct_to_dict(req.observed.composite.resource))

    def compose(self) -> None:
        self.compose_provider_configs()
        self.compose_usages()
        self.compose_cert_manager()
        self.compose_envoy_gateway()
        self.compose_ai_gateway()
        self.compose_gaie_crds()
        self.compose_prometheus()
        self.compose_leader_worker_set()
        self.compose_node_feature_discovery()
        self.compose_dra_driver()
        self.compose_gateway()
        self.write_status()
        self.mark_readiness()

    def compose_provider_configs(self) -> None:
        """Build ProviderConfigs from the XR's secrets.

        The XRD requires a Kubeconfig secret, so one is always present.
        """
        xr_secrets = self.xr.spec.secrets or []

        kubeconfig_secret = next(s for s in xr_secrets if s.type == _SECRET_TYPE_KUBECONFIG)

        # The kubeconfig provides the cluster endpoint and CA cert. If a
        # cloud-specific credential secret is present, it's layered on as an
        # identity block so the provider authenticates via the cloud's IAM
        # instead of relying on whatever auth is baked into the kubeconfig.
        k8s_pc_spec = k8spcv1alpha1.Spec(
            credentials=k8spcv1alpha1.Credentials(
                source="Secret",
                secretRef=k8spcv1alpha1.SecretRef(
                    name=kubeconfig_secret.name,
                    namespace=self.xr.metadata.namespace,  # ty: ignore[unresolved-attribute, invalid-argument-type]  # metadata is always set on resources read from the API server
                    key=kubeconfig_secret.key,
                ),
            ),
        )
        helm_pc_spec = helmpcv1beta1.Spec(
            credentials=helmpcv1beta1.Credentials(
                source="Secret",
                secretRef=helmpcv1beta1.SecretRef(
                    name=kubeconfig_secret.name,
                    namespace=self.xr.metadata.namespace,  # ty: ignore[unresolved-attribute, invalid-argument-type]  # metadata is always set on resources read from the API server
                    key=kubeconfig_secret.key,
                ),
            ),
        )

        gcp_secret = next(
            (s for s in xr_secrets if s.type == _SECRET_TYPE_GCP_SA_KEY),
            None,
        )
        if gcp_secret:
            k8s_pc_spec.identity = k8spcv1alpha1.Identity(
                type=_IDENTITY_TYPE_GCP,
                source="Secret",
                secretRef=k8spcv1alpha1.SecretRef(
                    name=gcp_secret.name,
                    namespace=self.xr.metadata.namespace,  # ty: ignore[unresolved-attribute, invalid-argument-type]  # metadata is always set on resources read from the API server
                    key=gcp_secret.key,
                ),
            )
            helm_pc_spec.identity = helmpcv1beta1.Identity(
                type=_IDENTITY_TYPE_GCP,
                source="Secret",
                secretRef=helmpcv1beta1.SecretRef(
                    name=gcp_secret.name,
                    namespace=self.xr.metadata.namespace,  # ty: ignore[unresolved-attribute, invalid-argument-type]  # metadata is always set on resources read from the API server
                    key=gcp_secret.key,
                ),
            )

        resource.update(
            self.rsp.desired.resources["provider-config-kubernetes"],
            k8spcv1alpha1.ProviderConfig(
                metadata=metav1.ObjectMeta(name=_pc_name(self.xr)),
                spec=k8s_pc_spec,
            ),
        )

        resource.update(
            self.rsp.desired.resources["provider-config-helm"],
            helmpcv1beta1.ProviderConfig(
                metadata=metav1.ObjectMeta(name=_pc_name(self.xr)),
                spec=helm_pc_spec,
            ),
        )

    def compose_usages(self) -> None:
        """Compose Usages ordering the Envoy Gateway teardown.

        The Envoy Gateway controller must outlive the Gateway and GatewayClass
        resources it manages: they carry finalizers it has to process on delete.
        The chain is Gateway Object → GatewayClass Object → envoy-gateway
        Release.

        ProviderConfig protection (every Release and Object must outlive the
        ProviderConfig it references) is handled generically by the
        compose-usages pipeline function, which runs after this one.
        """
        # GatewayClass Object protected by Gateway Object. The GatewayClass
        # has a gateway-exists-finalizer that the EG controller won't remove
        # while Gateways reference it.
        resource.update(
            self.rsp.desired.resources["usage-gateway-class-by-gateway"],
            usagev1beta1.Usage(
                spec=usagev1beta1.Spec(
                    of=usagev1beta1.Of(
                        apiVersion="kubernetes.m.crossplane.io/v1alpha1",
                        kind="Object",
                        resourceSelector=usagev1beta1.ResourceSelectorModel(
                            matchControllerRef=True,
                            matchLabels={_LABEL_RESOURCE: "gateway-class"},
                        ),
                    ),
                    by=usagev1beta1.By(
                        apiVersion="kubernetes.m.crossplane.io/v1alpha1",
                        kind="Object",
                        resourceSelector=usagev1beta1.ResourceSelector(
                            matchControllerRef=True,
                            matchLabels={_LABEL_RESOURCE: "gateway"},
                        ),
                    ),
                    replayDeletion=True,
                ),
            ),
        )
        self.rsp.desired.resources["usage-gateway-class-by-gateway"].ready = fnv1.READY_TRUE

        # Envoy Gateway Release protected by GatewayClass Object. The EG
        # controller must be running to process the GatewayClass's
        # gateway-exists-finalizer during deletion.
        resource.update(
            self.rsp.desired.resources["usage-envoy-gw-by-gateway-class"],
            usagev1beta1.Usage(
                spec=usagev1beta1.Spec(
                    of=usagev1beta1.Of(
                        apiVersion="helm.m.crossplane.io/v1beta1",
                        kind="Release",
                        resourceSelector=usagev1beta1.ResourceSelectorModel(
                            matchControllerRef=True,
                            matchLabels={_LABEL_RESOURCE: "envoy-gateway"},
                        ),
                    ),
                    by=usagev1beta1.By(
                        apiVersion="kubernetes.m.crossplane.io/v1alpha1",
                        kind="Object",
                        resourceSelector=usagev1beta1.ResourceSelector(
                            matchControllerRef=True,
                            matchLabels={_LABEL_RESOURCE: "gateway-class"},
                        ),
                    ),
                    replayDeletion=True,
                ),
            ),
        )
        self.rsp.desired.resources["usage-envoy-gw-by-gateway-class"].ready = fnv1.READY_TRUE

    def compose_cert_manager(self) -> None:
        """Compose cert-manager. Gated on ProviderConfigs being observed."""
        pc_observed = self.provider_configs_observed()
        if not (pc_observed or "cert-manager" in self.req.observed.resources):
            return

        v = self.xr.spec.versions or v1alpha1.Versions()
        resource.update(
            self.rsp.desired.resources["cert-manager"],
            _helm_release(
                chart="cert-manager",
                repo="https://charts.jetstack.io",
                version=v.certManager,  # ty: ignore[invalid-argument-type]  # XRD defaults this version and forbids null
                namespace="cert-manager",
                provider_config=_pc_name(self.xr),
                values={"crds": {"enabled": True, "keep": False}},
            ),
        )

    def compose_envoy_gateway(self) -> None:
        """Compose Envoy Gateway. Gated on ProviderConfigs being observed.

        The extensionManager block points Envoy Gateway at the Envoy AI Gateway
        controller's ext-proc server and declares InferencePool a backend
        resource, so HTTPRoute -> InferencePool backendRefs (disaggregated
        serving) resolve. enableBackend turns on the Backend API the AI Gateway
        relies on.
        """
        pc_observed = self.provider_configs_observed()
        if not (pc_observed or "envoy-gateway" in self.req.observed.resources):
            return

        v = self.xr.spec.versions or v1alpha1.Versions()
        resource.update(
            self.rsp.desired.resources["envoy-gateway"],
            _helm_release(
                chart="gateway-helm",
                repo="oci://docker.io/envoyproxy",
                version=v.envoyGateway,  # ty: ignore[invalid-argument-type]  # XRD defaults this version and forbids null
                namespace="envoy-gateway-system",
                provider_config=_pc_name(self.xr),
                labels={_LABEL_RESOURCE: "envoy-gateway"},
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
                                        "hostname": _AI_GATEWAY_CONTROLLER_FQDN,
                                        "port": _AI_GATEWAY_CONTROLLER_PORT,
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
        )

    def compose_ai_gateway(self) -> None:
        """Compose the Envoy AI Gateway CRDs and controller. Gated on the same
        ProviderConfigs as Envoy Gateway.

        The controller runs the ext-proc extension server that Envoy Gateway's
        extensionManager delegates InferencePool backend resolution to.
        """
        pc_observed = self.provider_configs_observed()
        if not (pc_observed or "ai-gateway-crds" in self.req.observed.resources):
            return

        resource.update(
            self.rsp.desired.resources["ai-gateway-crds"],
            _helm_release(
                chart="ai-gateway-crds-helm",
                repo=_AI_GATEWAY_REPO,
                version=_AI_GATEWAY_VERSION,
                namespace=_AI_GATEWAY_NAMESPACE,
                provider_config=_pc_name(self.xr),
            ),
        )
        resource.update(
            self.rsp.desired.resources["ai-gateway"],
            _helm_release(
                chart="ai-gateway-helm",
                repo=_AI_GATEWAY_REPO,
                version=_AI_GATEWAY_VERSION,
                namespace=_AI_GATEWAY_NAMESPACE,
                provider_config=_pc_name(self.xr),
            ),
        )

    def compose_gaie_crds(self) -> None:
        """Compose the Gateway API Inference Extension (GAIE) CRDs as
        provider-kubernetes Objects on the remote cluster. Gated on the same
        ProviderConfigs as Envoy Gateway.
        """
        pc_observed = self.provider_configs_observed()
        for doc in _GAIE_CRDS:
            key = _gaie_crd_key(doc)
            if not (pc_observed or key in self.req.observed.resources):
                continue
            resource.update(
                self.rsp.desired.resources[key],
                _k8s_object(_pc_name(self.xr), doc),
            )
            if resource.get_condition(self.req.observed.resources.get(key), "Ready").status == "True":
                self.rsp.desired.resources[key].ready = fnv1.READY_TRUE

    def compose_prometheus(self) -> None:
        """Compose the kube-prometheus-stack. Gated on ProviderConfigs being
        observed. Provides cluster observability (metrics scraping)."""
        pc_observed = self.provider_configs_observed()
        if not (pc_observed or "prometheus" in self.req.observed.resources):
            return

        v = self.xr.spec.versions or v1alpha1.Versions()
        resource.update(
            self.rsp.desired.resources["prometheus"],
            _prometheus_release(v.prometheus, _pc_name(self.xr)),  # ty: ignore[invalid-argument-type]  # XRD defaults this version and forbids null
        )

    def compose_leader_worker_set(self) -> None:
        """Compose LeaderWorkerSet. Gated on ProviderConfigs being observed."""
        pc_observed = self.provider_configs_observed()
        if not (pc_observed or "leader-worker-set" in self.req.observed.resources):
            return

        v = self.xr.spec.versions or v1alpha1.Versions()
        resource.update(
            self.rsp.desired.resources["leader-worker-set"],
            _helm_release(
                chart="lws",
                repo="oci://registry.k8s.io/lws/charts",
                version=v.leaderWorkerSet,  # ty: ignore[invalid-argument-type]  # XRD defaults this version and forbids null
                namespace="lws-system",
                provider_config=_pc_name(self.xr),
            ),
        )

    def compose_node_feature_discovery(self) -> None:
        """Compose Node Feature Discovery. Gated on ProviderConfigs being
        observed. NFD labels GPU nodes (e.g. feature.node.kubernetes.io/pci-10de
        for NVIDIA) so the DRA driver can target its kubelet plugin to them."""
        pc_observed = self.provider_configs_observed()
        if not (pc_observed or "node-feature-discovery" in self.req.observed.resources):
            return

        v = self.xr.spec.versions or v1alpha1.Versions()
        resource.update(
            self.rsp.desired.resources["node-feature-discovery"],
            _helm_release(
                chart="node-feature-discovery",
                repo="oci://registry.k8s.io/nfd/charts",
                version=v.nodeFeatureDiscovery,  # ty: ignore[invalid-argument-type]  # XRD defaults this version and forbids null
                namespace="node-feature-discovery",
                provider_config=_pc_name(self.xr),
            ),
        )

    def compose_dra_driver(self) -> None:
        """Compose the NVIDIA DRA driver. Gated on ProviderConfigs being
        observed. The driver publishes each GPU node's devices as DRA
        ResourceSlices and registers the gpu.nvidia.com DeviceClass that
        ModelReplica ResourceClaims request through, replacing the legacy
        device plugin. GPU allocation is opt-in (gpuResourcesEnabledOverride);
        ComputeDomains (Multi-Node NVLink) is disabled - we don't use it, and
        it would pull in extra prerequisites (GPU Feature Discovery)."""
        pc_observed = self.provider_configs_observed()
        if not (pc_observed or "dra-driver" in self.req.observed.resources):
            return

        v = self.xr.spec.versions or v1alpha1.Versions()
        # nvidiaDriverRoot is set by the cluster composition for platforms that
        # install the NVIDIA driver off the upstream default (/) — GKE uses
        # /home/kubernetes/bin/nvidia. Without it the kubelet plugin's init
        # container can't find nvidia-smi / libnvidia-ml and never starts. A
        # non-default value is the serving stack's signal that it's on such a
        # platform; the serving stack never inspects its own cloud.
        driver_root = self.xr.spec.nvidiaDriverRoot or _DEFAULT_NVIDIA_DRIVER_ROOT
        dra_values = {
            "gpuResourcesEnabledOverride": True,
            "resources": {"computeDomains": {"enabled": False}},
        }
        if driver_root != _DEFAULT_NVIDIA_DRIVER_ROOT:
            dra_values["nvidiaDriverRoot"] = driver_root
        resource.update(
            self.rsp.desired.resources["dra-driver"],
            _helm_release(
                chart="dra-driver-nvidia-gpu",
                repo="oci://registry.k8s.io/dra-driver-nvidia/charts",
                version=v.nvidiaDraDriver,  # ty: ignore[invalid-argument-type]  # XRD defaults this version and forbids null
                namespace=_DRA_DRIVER_NAMESPACE,
                provider_config=_pc_name(self.xr),
                values=dra_values,
            ),
        )

        # The DRA driver's kubelet plugin runs at system-node-critical priority.
        # GKE only admits system-node-critical / system-cluster-critical pods in a
        # namespace that has a ResourceQuota permitting those priority classes, so
        # without this the daemonset gets FailedCreate ("insufficient quota to
        # match these scopes") and never publishes ResourceSlices. Lay it down
        # everywhere: we only know GKE needs it, but it only *grants* headroom for
        # those two priority classes (it constrains nothing), so it's harmless on
        # clusters that don't restrict them (EKS, self-managed).
        resource.update(
            self.rsp.desired.resources["dra-driver-critical-pods-quota"],
            _k8s_object(
                _pc_name(self.xr),
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
            ),
        )

    def compose_gateway(self) -> None:
        """Compose the GatewayClass and Gateway on the remote cluster. Gated on
        ProviderConfigs being observed."""
        pc_observed = self.provider_configs_observed()
        pc = _pc_name(self.xr)

        gw = self.xr.spec.gateway or v1alpha1.Gateway()

        if gw.listeners:
            listeners = [{"name": ln.name, "protocol": ln.protocol, "port": ln.port} for ln in gw.listeners]
        else:
            listeners = [{"name": "http", "protocol": "HTTP", "port": 80}]

        # The Gateway (and the model-serving HTTPRoutes that target it) live in
        # modelplane-system on the remote cluster. Create the namespace; unlike
        # the old KServe path (whose chart created its kserve namespace), nothing
        # else provisions it.
        if pc_observed or "gateway-namespace" in self.req.observed.resources:
            resource.update(
                self.rsp.desired.resources["gateway-namespace"],
                _k8s_object(
                    pc,
                    {
                        "apiVersion": "v1",
                        "kind": "Namespace",
                        "metadata": {"name": "modelplane-system"},
                    },
                ),
            )

        if pc_observed or "gateway-class" in self.req.observed.resources:
            resource.update(
                self.rsp.desired.resources["gateway-class"],
                _k8s_object(
                    pc,
                    {
                        "apiVersion": "gateway.networking.k8s.io/v1",
                        "kind": "GatewayClass",
                        "metadata": {"name": gw.className},
                        "spec": {
                            "controllerName": "gateway.envoyproxy.io/gatewayclass-controller",
                        },
                    },
                    metadata=metav1.ObjectMeta(labels={_LABEL_RESOURCE: "gateway-class"}),
                ),
            )

        if pc_observed or "gateway" in self.req.observed.resources:
            resource.update(
                self.rsp.desired.resources["gateway"],
                _k8s_object(
                    pc,
                    {
                        "apiVersion": "gateway.networking.k8s.io/v1",
                        "kind": "Gateway",
                        "metadata": {
                            "name": "inference-gateway",
                            "namespace": "modelplane-system",
                        },
                        "spec": {
                            "gatewayClassName": gw.className,
                            "listeners": [
                                {
                                    **ln,
                                    "allowedRoutes": {"namespaces": {"from": "All"}},
                                }
                                for ln in listeners
                            ],
                        },
                    },
                    metadata=metav1.ObjectMeta(labels={_LABEL_RESOURCE: "gateway"}),
                    cel_query=_GATEWAY_READY_CEL,
                ),
            )

    def write_status(self) -> None:
        """Extract the gateway address from the observed Gateway Object and
        write it to the XR's status."""
        gateway_address = None
        gateway_observed = self.req.observed.resources.get("gateway")
        if gateway_observed:
            gw_dict = resource.struct_to_dict(gateway_observed.resource)
            addresses = (
                gw_dict.get("status", {})
                .get("atProvider", {})
                .get("manifest", {})
                .get("status", {})
                .get("addresses", [])
            )
            if addresses:
                gateway_address = addresses[0].get("value")

        status = v1alpha1.Status()
        if gateway_address:
            status.gateway = v1alpha1.GatewayModel(address=gateway_address)
        resource.update_status(self.rsp.desired.composite, status)

    def mark_readiness(self) -> None:
        """Mark composed resources as ready. Resources that don't need external
        readiness tracking are always marked ready. Others are marked ready when
        their observed condition is True."""
        # These resources don't have meaningful readiness signals — mark them
        # ready unconditionally so they don't block the XR.
        always_ready = [
            "provider-config-kubernetes",
            "provider-config-helm",
        ]
        for r in always_ready:
            if r in self.rsp.desired.resources:
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE

        condition_ready = [
            "cert-manager",
            "envoy-gateway",
            "ai-gateway-crds",
            "ai-gateway",
            "prometheus",
            "leader-worker-set",
            "node-feature-discovery",
            "dra-driver",
            "dra-driver-critical-pods-quota",
            "gateway-namespace",
            "gateway-class",
            "gateway",
        ]
        for r in condition_ready:
            if (
                r in self.rsp.desired.resources
                and resource.get_condition(self.req.observed.resources.get(r), "Ready").status == "True"
            ):
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE

    def provider_configs_observed(self):
        """Check if both ProviderConfigs have been persisted by Crossplane from
        a previous reconcile. Resources targeting the remote cluster are gated
        on this to avoid transient 'ProviderConfig not found' errors on first
        creation."""
        return (
            "provider-config-helm" in self.req.observed.resources
            and "provider-config-kubernetes" in self.req.observed.resources
        )
