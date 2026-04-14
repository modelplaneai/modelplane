"""Prometheus configuration for backend clusters.

Both KServe and Dynamo backends install kube-prometheus-stack for autoscaling
metrics. This module provides shared configuration and a helper to compose the
Helm release so that every backend uses the same chart, namespace, and service
name.
"""

from ..model.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from . import helm

# The Prometheus service name is pinned via fullnameOverride so that KEDA
# ScaledObjects and backend operators can reference it at a known address
# regardless of the auto-generated Helm release name.
NAMESPACE = "monitoring"
FULLNAME_OVERRIDE = "prometheus"
URL = f"http://{FULLNAME_OVERRIDE}-prometheus.{NAMESPACE}.svc.cluster.local:9090"

CHART = "kube-prometheus-stack"
REPO = "https://prometheus-community.github.io/helm-charts"


def helm_release(version: str, provider_config: str) -> helmv1beta1.Release:
    """Build a kube-prometheus-stack Helm release for a backend cluster."""
    return helm.helm_release(
        chart=CHART,
        repo=REPO,
        version=version,
        namespace=NAMESPACE,
        provider_config=provider_config,
        values={
            "fullnameOverride": FULLNAME_OVERRIDE,
            "prometheus": {
                "prometheusSpec": {
                    # Discover PodMonitors across all namespaces. Backend
                    # operators (Dynamo) auto-create PodMonitors for their
                    # services.
                    "podMonitorSelectorNilUsesHelmValues": False,
                    "podMonitorNamespaceSelector": {},
                    # Scrape Envoy Gateway proxy pods for upstream request
                    # metrics (envoy_cluster_upstream_rq_active). Both
                    # KServe and Dynamo backends use Envoy Gateway for
                    # ingress, and this metric measures in-flight requests
                    # at the proxy level -- the same signal regardless of
                    # backend engine.
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
            # Disable components we don't need for autoscaling.
            "grafana": {"enabled": False},
            "alertmanager": {"enabled": False},
        },
    )
