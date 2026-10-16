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

"""Tests for the collector this stack composes.

A table per collector function, each comparing what it renders whole: the
config, the OTTL statements and the blocks they run in, the manifests, the
exporters and the authenticators. Then the properties no single rendering
shows: what the substrate job's port rewrite does to an address, and what holds
of the unit conversions and the built-in mappings.
"""

import dataclasses
import re
import typing

import pytest
import yaml
from function import collector, stacks
from models.ai.modelplane.metricmapping import v1alpha1 as mmv1alpha1
from models.ai.modelplane.telemetrydestination import v1alpha1 as tdv1alpha1
from pydantic import ValidationError


@dataclasses.dataclass
class ConfigCase:
    """A test case for collector.config."""

    name: str
    reason: str
    cluster: str
    mappings: list[mmv1alpha1.MetricMapping]
    sinks: list[tdv1alpha1.Sink]
    extensions: dict
    want: dict


@dataclasses.dataclass
class PortRewriteCase:
    """A test case for the substrate job's rewrite of a pod's address onto its annotated port."""

    name: str
    reason: str
    address: str
    want: str


@dataclasses.dataclass
class StatementsCase:
    """A test case for collector.statements."""

    name: str
    reason: str
    mappings: list[mmv1alpha1.MetricMapping]
    want: tuple[list[str], list[str], list[str], list[str]]  # (extract, scale, datapoint, metric)


@dataclasses.dataclass
class TransformCase:
    """A test case for collector._transform."""

    name: str
    reason: str
    mappings: list[mmv1alpha1.MetricMapping]
    want: dict


@dataclasses.dataclass
class ObjectsCase:
    """A test case for collector.objects."""

    name: str
    reason: str
    cluster: str
    mappings: list[mmv1alpha1.MetricMapping]
    sinks: list[tdv1alpha1.Sink]
    extensions: dict
    want: list[tuple[str, dict, str | None]]


@dataclasses.dataclass
class ExportersCase:
    """A test case for collector.exporters."""

    name: str
    reason: str
    sinks: list[tdv1alpha1.Sink]
    want: dict


@dataclasses.dataclass
class AuthenticatorsCase:
    """A test case for collector.authenticators."""

    name: str
    reason: str
    sinks: list[tdv1alpha1.Sink]
    want: dict


def _sink(*, name: str, type_: str, secret: str | None) -> tdv1alpha1.Sink:
    """A sink exporting to otel.acme.example, with a bearer token read from secret if there is one."""
    return tdv1alpha1.Sink.model_validate(
        {
            "name": name,
            "type": type_,
            "endpoint": "https://otel.acme.example",
            **({"secretRef": {"name": secret}, "auth": {"bearerTokenKey": "token"}} if secret else {}),
        }
    )


# This builds CONFIG_CASES' whole want, which the helper rule forbids. The config
# runs to nearly 300 lines, and the cases differ only in their exporters and
# extensions, each a keyword argument here. Writing it out once per case would
# hide the parts that vary. OBJECTS_CASES embeds the same config in the
# collector's ConfigMap, which is the use the helper rule allows.
def _config(
    *, exporters: dict, pipeline_exporters: list[str], extensions: dict | None, service_extensions: list[str] | None
) -> dict:
    """The collector's config for prod-us-east with the built-in mappings, as the dict config dumps to YAML."""
    service: dict = {
        "pipelines": {
            "metrics": {
                "receivers": ["prometheus"],
                # The renames run before groupbyattrs lifts the identity, while
                # the datapoints are still where a statement matching on a
                # metric's name can reach them.
                "processors": [
                    "memory_limiter",
                    "resource/cluster",
                    "transform/identity",
                    "transform/modelplane",
                    "groupbyattrs/identity",
                    "filter/modelplane",
                    "batch",
                ],
                "exporters": pipeline_exporters,
            }
        },
    }
    if service_extensions is not None:
        # An authenticator the service doesn't list is one the collector won't
        # load.
        service["extensions"] = service_extensions
    config: dict = {
        "receivers": {
            "prometheus": {
                "config": {
                    "scrape_configs": [
                        {
                            "job_name": "modelplane-engines",
                            "scrape_interval": "15s",
                            "kubernetes_sd_configs": [{"role": "pod"}],
                            "relabel_configs": [
                                {
                                    "source_labels": ["__meta_kubernetes_pod_label_modelplane_ai_deployment"],
                                    "action": "keep",
                                    "regex": ".+",
                                },
                                # By name, on the engine or picker container:
                                # matching by number would find a sidecar on
                                # 8000, and another container may name a port
                                # http.
                                {
                                    "source_labels": ["__meta_kubernetes_pod_container_port_name"],
                                    "action": "keep",
                                    "regex": "http",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_container_name"],
                                    "action": "keep",
                                    "regex": "engine|epp",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_label_modelplane_ai_deployment"],
                                    "target_label": "deployment",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_label_modelplane_ai_replica"],
                                    "target_label": "replica",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_label_modelplane_ai_engine"],
                                    "target_label": "engine",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_label_modelplane_ai_role"],
                                    "target_label": "role",
                                },
                                {"source_labels": ["__meta_kubernetes_namespace"], "target_label": "namespace"},
                            ],
                        },
                        {
                            "job_name": "modelplane-gateway",
                            "scrape_interval": "15s",
                            "kubernetes_sd_configs": [{"role": "pod"}],
                            "relabel_configs": [
                                {
                                    "source_labels": [
                                        "__meta_kubernetes_pod_label_gateway_envoyproxy_io_owning_gateway_name"
                                    ],
                                    "action": "keep",
                                    "regex": ".+",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_container_port_name"],
                                    "action": "keep",
                                    "regex": "metrics",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_annotation_prometheus_io_path"],
                                    "action": "replace",
                                    "target_label": "__metrics_path__",
                                    "regex": "(.+)",
                                },
                            ],
                        },
                        # A target of the gateway's own: its GenAI metrics are on
                        # the ext-proc sidecar, not the proxy's port.
                        {
                            "job_name": "modelplane-gateway-genai",
                            "scrape_interval": "15s",
                            "kubernetes_sd_configs": [{"role": "pod"}],
                            "relabel_configs": [
                                {
                                    "source_labels": [
                                        "__meta_kubernetes_pod_label_gateway_envoyproxy_io_owning_gateway_name"
                                    ],
                                    "action": "keep",
                                    "regex": ".+",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_container_port_name"],
                                    "action": "keep",
                                    "regex": "aigw-admin",
                                },
                            ],
                        },
                        {
                            "job_name": "modelplane-gpu",
                            "scrape_interval": "15s",
                            "kubernetes_sd_configs": [{"role": "pod"}],
                            "relabel_configs": [
                                {
                                    "source_labels": [
                                        "__meta_kubernetes_pod_label_app_kubernetes_io_name",
                                        "__meta_kubernetes_pod_label_app",
                                    ],
                                    "action": "keep",
                                    "regex": ".*dcgm.*",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_container_port_name"],
                                    "action": "keep",
                                    "regex": "metrics",
                                },
                                {"source_labels": ["__meta_kubernetes_pod_node_name"], "target_label": "node"},
                            ],
                        },
                        {
                            "job_name": "modelplane-substrate",
                            "scrape_interval": "30s",
                            "kubernetes_sd_configs": [{"role": "pod"}],
                            "relabel_configs": [
                                {
                                    "source_labels": ["__meta_kubernetes_pod_annotation_prometheus_io_scrape"],
                                    "action": "keep",
                                    "regex": "true",
                                },
                                # Every pod a job above keeps, dropped on the same
                                # terms, so the jobs cover disjoint pods: a pod two
                                # jobs both collect arrives twice, under two job
                                # names.
                                {
                                    "source_labels": [
                                        "__meta_kubernetes_pod_label_gateway_envoyproxy_io_owning_gateway_name"
                                    ],
                                    "action": "drop",
                                    "regex": ".+",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_label_modelplane_ai_deployment"],
                                    "action": "drop",
                                    "regex": ".+",
                                },
                                {
                                    "source_labels": [
                                        "__meta_kubernetes_pod_label_app_kubernetes_io_name",
                                        "__meta_kubernetes_pod_label_app",
                                    ],
                                    "action": "drop",
                                    "regex": ".*dcgm.*",
                                },
                                {
                                    "source_labels": ["__meta_kubernetes_pod_annotation_prometheus_io_path"],
                                    "action": "replace",
                                    "target_label": "__metrics_path__",
                                    "regex": "(.+)",
                                },
                                {
                                    "source_labels": [
                                        "__address__",
                                        "__meta_kubernetes_pod_annotation_prometheus_io_port",
                                    ],
                                    "action": "replace",
                                    "target_label": "__address__",
                                    "regex": r"(\[.+\]|[^:]+)(?::\d+)?;(\d+)",
                                    "replacement": "$1:$2",
                                },
                                {"source_labels": ["__meta_kubernetes_namespace"], "target_label": "namespace"},
                            ],
                        },
                    ]
                }
            }
        },
        "processors": {
            # First in the pipeline: nothing bounds what one interval brings off
            # a fleet of engines.
            "memory_limiter": {"check_interval": "1s", "limit_percentage": 80, "spike_limit_percentage": 25},
            # Stamped here, because one receiver downstream sees a merged stream
            # and can't tell senders apart.
            "resource/cluster": {"attributes": [{"key": "cluster", "value": "prod-us-east", "action": "upsert"}]},
            # Only the identity survives to the exporter: discovery attaches the
            # pod's name and uid, and neither is the deployment's. It includes
            # service.instance.id because two producers whose series are
            # identical are one series, and one is lost. OTTL quotes with double
            # quotes, and the single ones a Python list renders stop the
            # collector starting.
            "transform/identity": {
                "metric_statements": [
                    {
                        "context": "resource",
                        "statements": [
                            'keep_keys(resource.attributes, ["cluster", "deployment", "engine", "namespace", "node", "replica", "role", "service.instance.id", "service.name"])'
                        ],
                    }
                ]
            },
            "transform/modelplane": {
                "metric_statements": [
                    # DCGM reports mJ and MiB, and the names say joules and bytes.
                    # scale_metric converts a histogram's sum, bounds and buckets
                    # where setting value_double would convert a gauge and leave a
                    # histogram lying. It refuses an exponential histogram, which
                    # under the default error mode fails the whole batch. A block
                    # ahead of the renames rather than a line, because the
                    # processor finishes a block over every metric before the next
                    # one starts.
                    {
                        "context": "metric",
                        "error_mode": "ignore",
                        "statements": [
                            'scale_metric(1048576.0) where metric.name == "DCGM_FI_DEV_FB_USED"',
                            'scale_metric(0.001) where metric.name == "DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION"',
                        ],
                    },
                    {
                        "context": "metric",
                        "statements": [
                            'set(metric.name, "modelplane_frontend_request_duration_seconds") where metric.name == "gen_ai_server_request_duration_seconds"',
                            'set(metric.name, "modelplane_frontend_ttft_seconds") where metric.name == "gen_ai_server_time_to_first_token_seconds"',
                            'set(metric.name, "modelplane_frontend_tpot_seconds") where metric.name == "gen_ai_server_time_per_output_token_seconds"',
                            'set(metric.name, "modelplane_request_ttft_seconds") where metric.name == "vllm:time_to_first_token_seconds"',
                            'set(metric.name, "modelplane_request_duration_seconds") where metric.name == "vllm:e2e_request_latency_seconds"',
                            'set(metric.name, "modelplane_request_queue_seconds") where metric.name == "vllm:request_queue_time_seconds"',
                            'set(metric.name, "modelplane_request_prefill_seconds") where metric.name == "vllm:request_prefill_time_seconds"',
                            'set(metric.name, "modelplane_request_decode_seconds") where metric.name == "vllm:request_decode_time_seconds"',
                            'set(metric.name, "modelplane_request_input_tokens") where metric.name == "vllm:request_prompt_tokens"',
                            'set(metric.name, "modelplane_request_output_tokens") where metric.name == "vllm:request_generation_tokens"',
                            'set(metric.name, "modelplane_requests_running") where metric.name == "vllm:num_requests_running"',
                            'set(metric.name, "modelplane_requests_waiting") where metric.name == "vllm:num_requests_waiting"',
                            'set(metric.name, "modelplane_kv_cache_utilization_ratio") where metric.name == "vllm:kv_cache_usage_perc"',
                            'set(metric.name, "modelplane_requests_preempted_total") where metric.name == "vllm:num_preemptions_total"',
                            'set(metric.name, "modelplane_prefix_cache_hits_total") where metric.name == "vllm:prefix_cache_hits_total"',
                            'set(metric.name, "modelplane_prefix_cache_lookups_total") where metric.name == "vllm:prefix_cache_queries_total"',
                            'set(metric.name, "modelplane_requests_running") where metric.name == "sglang:num_running_reqs"',
                            'set(metric.name, "modelplane_requests_waiting") where metric.name == "sglang:num_queue_reqs"',
                            'set(metric.name, "modelplane_kv_cache_utilization_ratio") where metric.name == "sglang:token_usage"',
                            'set(metric.name, "modelplane_request_input_tokens") where metric.name == "sglang:prompt_tokens_histogram"',
                            'set(metric.name, "modelplane_request_output_tokens") where metric.name == "sglang:generation_tokens_histogram"',
                            'set(metric.name, "modelplane_route_decision_seconds") where metric.name == "llm_d_epp_scheduler_e2e_duration_seconds"',
                            'set(metric.name, "modelplane_gpu_memory_used_bytes") where metric.name == "DCGM_FI_DEV_FB_USED"',
                            'set(metric.name, "modelplane_gpu_compute_active_ratio") where metric.name == "DCGM_FI_PROF_GR_ENGINE_ACTIVE"',
                            'set(metric.name, "modelplane_gpu_tensor_active_ratio") where metric.name == "DCGM_FI_PROF_PIPE_TENSOR_ACTIVE"',
                            'set(metric.name, "modelplane_gpu_memory_bandwidth_ratio") where metric.name == "DCGM_FI_PROF_DRAM_ACTIVE"',
                            'set(metric.name, "modelplane_gpu_temperature_celsius") where metric.name == "DCGM_FI_DEV_GPU_TEMP"',
                            'set(metric.name, "modelplane_gpu_power_watts") where metric.name == "DCGM_FI_DEV_POWER_USAGE"',
                            'set(metric.name, "modelplane_energy_joules_total") where metric.name == "DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION"',
                        ],
                    },
                ]
            },
            # Lifts the identity discovery wrote onto each datapoint onto the
            # resource, where an exporter that flattens a series into labels
            # reads it. Without it a series arrives carrying only the cluster.
            "groupbyattrs/identity": {
                "keys": [
                    "cluster",
                    "namespace",
                    "deployment",
                    "replica",
                    "engine",
                    "role",
                    "node",
                    "service.name",
                    "service.instance.id",
                ]
            },
            # Only modelplane_* leaves the cluster: a series the statements
            # didn't rename is dropped.
            "filter/modelplane": {"metrics": {"metric": ['not IsMatch(name, "^modelplane_.*")']}},
            "batch": {"timeout": "10s"},
        },
        "exporters": exporters,
        "service": service,
    }
    if extensions is not None:
        config["extensions"] = extensions
    return config


def _service_account() -> dict:
    """The collector's ServiceAccount."""
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": "modelplane-collector",
            "namespace": "modelplane-system",
            "labels": {"app.kubernetes.io/name": "modelplane-collector", "app.kubernetes.io/managed-by": "modelplane"},
        },
    }


def _cluster_role() -> dict:
    """The collector's ClusterRole."""
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRole",
        "metadata": {
            "name": "modelplane-collector",
            "labels": {"app.kubernetes.io/name": "modelplane-collector", "app.kubernetes.io/managed-by": "modelplane"},
        },
        # Read only: service discovery needs to list pods, and nothing needs to
        # write.
        "rules": [
            {
                "apiGroups": [""],
                "resources": ["pods", "services", "endpoints", "nodes", "nodes/metrics"],
                "verbs": ["get", "list", "watch"],
            },
            {"nonResourceURLs": ["/metrics"], "verbs": ["get"]},
        ],
    }


def _cluster_role_binding() -> dict:
    """The collector's ClusterRoleBinding."""
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {
            "name": "modelplane-collector",
            "labels": {"app.kubernetes.io/name": "modelplane-collector", "app.kubernetes.io/managed-by": "modelplane"},
        },
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "modelplane-collector"},
        "subjects": [{"kind": "ServiceAccount", "name": "modelplane-collector", "namespace": "modelplane-system"}],
    }


def _config_map(*, config: dict) -> dict:
    """The ConfigMap holding the collector's config, as YAML."""
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "modelplane-collector",
            "namespace": "modelplane-system",
            "labels": {"app.kubernetes.io/name": "modelplane-collector", "app.kubernetes.io/managed-by": "modelplane"},
        },
        "data": {"collector.yaml": yaml.safe_dump(config, sort_keys=False)},
    }


def _deployment(
    *, config_hash: str, volumes: list[dict], volume_mounts: list[dict], env_from: list[dict] | None
) -> dict:
    """The collector's Deployment, restarted by its config's hash and mounting volumes."""
    container: dict = {
        "name": "collector",
        "image": "otel/opentelemetry-collector-contrib:0.161.0",
        "args": ["--config=/conf/collector.yaml"],
        "volumeMounts": volume_mounts,
        "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "512Mi"}},
    }
    if env_from is not None:
        container["envFrom"] = env_from
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "modelplane-collector",
            "namespace": "modelplane-system",
            "labels": {"app.kubernetes.io/name": "modelplane-collector", "app.kubernetes.io/managed-by": "modelplane"},
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app.kubernetes.io/name": "modelplane-collector"}},
            "template": {
                "metadata": {
                    "labels": {"app.kubernetes.io/name": "modelplane-collector"},
                    "annotations": {"modelplane.ai/config-hash": config_hash},
                },
                "spec": {
                    "serviceAccountName": "modelplane-collector",
                    "containers": [container],
                    "volumes": volumes,
                },
            },
        },
    }


# Every case renders the built-in mappings, read from the stacks package rather
# than written as literals, because fn.py always renders them and so every real
# config carries them. None of these cases is about them: BuiltIn in
# STATEMENTS_CASES pins what they compile to. The cost is that changing a
# built-in changes _config's transform/modelplane block too.
CONFIG_CASES = [
    ConfigCase(
        name="OneSink",
        reason=(
            "With one sink and one extension, the collector exports the built-in mappings' series to the sink, "
            "stamped with the cluster, and loads the extension."
        ),
        cluster="prod-us-east",
        mappings=list(stacks.BUILTIN_MAPPINGS),
        sinks=[_sink(name="primary", type_="otlphttp", secret=None)],
        # A client authenticator: an exporter needs one of those, not the oidc
        # extension, which authenticates callers of a receiver.
        extensions={"oauth2client/acme": {"token_url": "https://issuer.acme.example/token"}},
        want=_config(
            exporters={"otlphttp/primary": {"endpoint": "https://otel.acme.example"}},
            pipeline_exporters=["otlphttp/primary"],
            extensions={"oauth2client/acme": {"token_url": "https://issuer.acme.example/token"}},
            service_extensions=["oauth2client/acme"],
        ),
    ),
    ConfigCase(
        name="NoExtensions",
        reason="With no extensions, the config declares none, to the service or at the top level.",
        cluster="prod-us-east",
        mappings=list(stacks.BUILTIN_MAPPINGS),
        sinks=[_sink(name="primary", type_="otlphttp", secret=None)],
        extensions={},
        want=_config(
            exporters={"otlphttp/primary": {"endpoint": "https://otel.acme.example"}},
            pipeline_exporters=["otlphttp/primary"],
            extensions=None,
            service_extensions=None,
        ),
    ),
    # The remote-write exporter registers as prometheus_remote_write in 0.161.0
    # and still answers to the older prometheusremotewrite. A sink writing the
    # one the collector's own documentation gives would otherwise match no
    # default and export every series stripped of the cluster, deployment,
    # engine and role it belongs to - silently, because the sink itself works.
    ConfigCase(
        name="RemoteWrite",
        reason="A prometheus_remote_write sink carries each series' resource attributes as labels.",
        cluster="prod-us-east",
        mappings=list(stacks.BUILTIN_MAPPINGS),
        sinks=[_sink(name="primary", type_="prometheus_remote_write", secret=None)],
        extensions={"oauth2client/acme": {"token_url": "https://issuer.acme.example/token"}},
        want=_config(
            exporters={
                "prometheus_remote_write/primary": {
                    "resource_to_telemetry_conversion": {"enabled": True},
                    "endpoint": "https://otel.acme.example",
                }
            },
            pipeline_exporters=["prometheus_remote_write/primary"],
            extensions={"oauth2client/acme": {"token_url": "https://issuer.acme.example/token"}},
            service_extensions=["oauth2client/acme"],
        ),
    ),
    ConfigCase(
        name="RemoteWriteOldName",
        reason="A prometheusremotewrite sink carries each series' resource attributes as labels.",
        cluster="prod-us-east",
        mappings=list(stacks.BUILTIN_MAPPINGS),
        sinks=[_sink(name="primary", type_="prometheusremotewrite", secret=None)],
        extensions={"oauth2client/acme": {"token_url": "https://issuer.acme.example/token"}},
        want=_config(
            exporters={
                "prometheusremotewrite/primary": {
                    "resource_to_telemetry_conversion": {"enabled": True},
                    "endpoint": "https://otel.acme.example",
                }
            },
            pipeline_exporters=["prometheusremotewrite/primary"],
            extensions={"oauth2client/acme": {"token_url": "https://issuer.acme.example/token"}},
            service_extensions=["oauth2client/acme"],
        ),
    ),
    ConfigCase(
        name="Prometheus",
        reason="A prometheus sink carries each series' resource attributes as labels.",
        cluster="prod-us-east",
        mappings=list(stacks.BUILTIN_MAPPINGS),
        sinks=[_sink(name="primary", type_="prometheus", secret=None)],
        extensions={"oauth2client/acme": {"token_url": "https://issuer.acme.example/token"}},
        want=_config(
            exporters={
                "prometheus/primary": {
                    "resource_to_telemetry_conversion": {"enabled": True},
                    "endpoint": "https://otel.acme.example",
                }
            },
            pipeline_exporters=["prometheus/primary"],
            extensions={"oauth2client/acme": {"token_url": "https://issuer.acme.example/token"}},
            service_extensions=["oauth2client/acme"],
        ),
    ),
]


@pytest.mark.parametrize("case", CONFIG_CASES, ids=lambda case: case.name)
def test_config(case: ConfigCase) -> None:
    """config renders the collector's whole configuration."""
    got = collector.config(case.cluster, case.mappings, case.sinks, case.extensions)
    # The cases write the config as the dict the function dumps, in the order
    # it builds it, so they read as config rather than as wrapped YAML. PyYAML
    # dumps it on both sides.
    assert got == yaml.safe_dump(case.want, sort_keys=False), case.reason


PORT_REWRITE_CASES = [
    PortRewriteCase(
        name="IPv4",
        reason="An IPv4 pod's address moves onto its annotated port.",
        address="10.1.0.5:8000",
        want="10.1.0.5:9402",
    ),
    PortRewriteCase(
        name="IPv6",
        reason="An IPv6 pod's bracketed address, which [^:]+ never matches, moves onto its annotated port.",
        address="[2001:db8::1]:9090",
        want="[2001:db8::1]:9402",
    ),
]


@pytest.mark.parametrize("case", PORT_REWRITE_CASES, ids=lambda case: case.name)
def test_port_rewrite(case: PortRewriteCase) -> None:
    """The substrate job rewrites a pod's address onto the port it annotates."""
    # This checks what the rewrite does to an address, which comparing the
    # config can't, so it picks the one rule out of a rendered config. The
    # config renders the built-in mappings, read from the stacks package, as
    # fn.py always does, though the rule doesn't depend on them.
    config = yaml.safe_load(
        collector.config(
            "prod-us-east",
            list(stacks.BUILTIN_MAPPINGS),
            [_sink(name="primary", type_="otlphttp", secret=None)],
            {"oauth2client/acme": {"token_url": "https://issuer.acme.example/token"}},
        )
    )
    rule = next(
        r
        for j in config["receivers"]["prometheus"]["config"]["scrape_configs"]
        if j["job_name"] == "modelplane-substrate"
        for r in j["relabel_configs"]
        if r.get("target_label") == "__address__"
    )
    matched = re.fullmatch(rule["regex"], f"{case.address};9402")
    got = matched.expand(r"\1:\2") if matched else None
    assert got == case.want, case.reason


STATEMENTS_CASES = [
    # The built-in mappings, read from the stacks package rather than restated,
    # because what the collector compiles them to is what this case pins.
    StatementsCase(
        name="BuiltIn",
        reason="The built-in mappings convert DCGM's units, then rename every series they map.",
        mappings=list(stacks.BUILTIN_MAPPINGS),
        want=(
            [],
            [
                'scale_metric(1048576.0) where metric.name == "DCGM_FI_DEV_FB_USED"',
                'scale_metric(0.001) where metric.name == "DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION"',
            ],
            [],
            [
                'set(metric.name, "modelplane_frontend_request_duration_seconds") where metric.name == "gen_ai_server_request_duration_seconds"',
                'set(metric.name, "modelplane_frontend_ttft_seconds") where metric.name == "gen_ai_server_time_to_first_token_seconds"',
                'set(metric.name, "modelplane_frontend_tpot_seconds") where metric.name == "gen_ai_server_time_per_output_token_seconds"',
                'set(metric.name, "modelplane_request_ttft_seconds") where metric.name == "vllm:time_to_first_token_seconds"',
                'set(metric.name, "modelplane_request_duration_seconds") where metric.name == "vllm:e2e_request_latency_seconds"',
                'set(metric.name, "modelplane_request_queue_seconds") where metric.name == "vllm:request_queue_time_seconds"',
                'set(metric.name, "modelplane_request_prefill_seconds") where metric.name == "vllm:request_prefill_time_seconds"',
                'set(metric.name, "modelplane_request_decode_seconds") where metric.name == "vllm:request_decode_time_seconds"',
                'set(metric.name, "modelplane_request_input_tokens") where metric.name == "vllm:request_prompt_tokens"',
                'set(metric.name, "modelplane_request_output_tokens") where metric.name == "vllm:request_generation_tokens"',
                'set(metric.name, "modelplane_requests_running") where metric.name == "vllm:num_requests_running"',
                'set(metric.name, "modelplane_requests_waiting") where metric.name == "vllm:num_requests_waiting"',
                'set(metric.name, "modelplane_kv_cache_utilization_ratio") where metric.name == "vllm:kv_cache_usage_perc"',
                'set(metric.name, "modelplane_requests_preempted_total") where metric.name == "vllm:num_preemptions_total"',
                'set(metric.name, "modelplane_prefix_cache_hits_total") where metric.name == "vllm:prefix_cache_hits_total"',
                'set(metric.name, "modelplane_prefix_cache_lookups_total") where metric.name == "vllm:prefix_cache_queries_total"',
                # Not SGLang's latency histograms, sglang:time_to_first_token_seconds
                # and sglang:inter_token_latency: their buckets resolve to 100ms
                # where vLLM's resolve to 1ms.
                'set(metric.name, "modelplane_requests_running") where metric.name == "sglang:num_running_reqs"',
                'set(metric.name, "modelplane_requests_waiting") where metric.name == "sglang:num_queue_reqs"',
                'set(metric.name, "modelplane_kv_cache_utilization_ratio") where metric.name == "sglang:token_usage"',
                'set(metric.name, "modelplane_request_input_tokens") where metric.name == "sglang:prompt_tokens_histogram"',
                'set(metric.name, "modelplane_request_output_tokens") where metric.name == "sglang:generation_tokens_histogram"',
                'set(metric.name, "modelplane_route_decision_seconds") where metric.name == "llm_d_epp_scheduler_e2e_duration_seconds"',
                'set(metric.name, "modelplane_gpu_memory_used_bytes") where metric.name == "DCGM_FI_DEV_FB_USED"',
                'set(metric.name, "modelplane_gpu_compute_active_ratio") where metric.name == "DCGM_FI_PROF_GR_ENGINE_ACTIVE"',
                'set(metric.name, "modelplane_gpu_tensor_active_ratio") where metric.name == "DCGM_FI_PROF_PIPE_TENSOR_ACTIVE"',
                'set(metric.name, "modelplane_gpu_memory_bandwidth_ratio") where metric.name == "DCGM_FI_PROF_DRAM_ACTIVE"',
                'set(metric.name, "modelplane_gpu_temperature_celsius") where metric.name == "DCGM_FI_DEV_GPU_TEMP"',
                'set(metric.name, "modelplane_gpu_power_watts") where metric.name == "DCGM_FI_DEV_POWER_USAGE"',
                'set(metric.name, "modelplane_energy_joules_total") where metric.name == "DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION"',
            ],
        ),
    ),
    # vLLM and SGLang both publish a fraction, so no built-in needs this, but
    # vLLM's is called kv_cache_usage_perc: the name is no guide, and an engine
    # that means it has to be able to say so.
    StatementsCase(
        name="Percent",
        reason="A metric counted in percent is scaled by 0.01 into the ratio its new name claims.",
        mappings=[
            mmv1alpha1.MetricMapping.model_validate(
                {
                    "spec": {
                        "metrics": [
                            {
                                "from": "my_engine_cache_percent",
                                "to": "modelplane_kv_cache_utilization_ratio",
                                "fromUnit": "Percent",
                            }
                        ]
                    }
                }
            )
        ],
        want=(
            [],
            ['scale_metric(0.01) where metric.name == "my_engine_cache_percent"'],
            [],
            [
                'set(metric.name, "modelplane_kv_cache_utilization_ratio") where metric.name == "my_engine_cache_percent"'
            ],
        ),
    ),
    # `from` is pattern-constrained, but a label's value can't be: a value and
    # a `values` remap carry whatever vocabulary the component already writes,
    # so the schema has to take free text. An unescaped quote would close the
    # OTTL literal early and leave the rest of the value as OTTL.
    StatementsCase(
        name="QuotedLabelValue",
        reason="A quote in a remapped label value is escaped, so it can't end the OTTL string it sits in.",
        mappings=[
            mmv1alpha1.MetricMapping.model_validate(
                {
                    "spec": {
                        "metrics": [
                            {
                                "from": "my_engine_finish",
                                "to": "modelplane_requests_total",
                                "labels": [{"name": "reason", "from": "finish", "values": {'ab"c': 'x"y'}}],
                            }
                        ]
                    }
                }
            )
        ],
        want=(
            [],
            [],
            [
                'set(datapoint.attributes["reason"], datapoint.attributes["finish"]) where metric.name == "my_engine_finish"',
                r'set(datapoint.attributes["reason"], "x\"y") where metric.name == "my_engine_finish" and datapoint.attributes["finish"] == "ab\"c"',
                'delete_key(datapoint.attributes, "finish") where metric.name == "my_engine_finish"',
            ],
            ['set(metric.name, "modelplane_requests_total") where metric.name == "my_engine_finish"'],
        ),
    ),
    # A label carried onto its own name is how a mapping remaps values in place.
    # The delete that stops a carried label costing twice the cardinality would
    # take the label the statements before it just set.
    StatementsCase(
        name="LabelOntoItself",
        reason="A label carried onto its own name has its values remapped and isn't deleted afterwards.",
        mappings=[
            mmv1alpha1.MetricMapping.model_validate(
                {
                    "spec": {
                        "metrics": [
                            {
                                "from": "my_engine_finish",
                                "to": "modelplane_requests_total",
                                "labels": [{"name": "reason", "from": "reason", "values": {"eos": "stop"}}],
                            }
                        ]
                    }
                }
            )
        ],
        want=(
            [],
            [],
            [
                'set(datapoint.attributes["reason"], datapoint.attributes["reason"]) where metric.name == "my_engine_finish"',
                'set(datapoint.attributes["reason"], "stop") where metric.name == "my_engine_finish" and datapoint.attributes["reason"] == "eos"',
            ],
            ['set(metric.name, "modelplane_requests_total") where metric.name == "my_engine_finish"'],
        ),
    ),
]


@pytest.mark.parametrize("case", STATEMENTS_CASES, ids=lambda case: case.name)
def test_statements(case: StatementsCase) -> None:
    """statements compiles the mappings to OTTL extract, scale, datapoint and metric statements."""
    got = collector.statements(case.mappings)
    assert got == case.want, case.reason


TRANSFORM_CASES = [
    # The extraction mints my_engine_duration_ms_count, and the conversion, the
    # label and the rename all select on that name. Run any of them first and it
    # matches nothing, silently.
    TransformCase(
        name="ExtractedPart",
        reason="A histogram part is extracted in a block ahead of everything that selects on its name.",
        mappings=[
            mmv1alpha1.MetricMapping.model_validate(
                {
                    "spec": {
                        "metrics": [
                            {
                                "from": "my_engine_duration_ms",
                                "to": "modelplane_requests_total",
                                "part": "Count",
                                "fromUnit": "Milliseconds",
                                "labels": [{"name": "status", "value": "ok"}],
                            }
                        ]
                    }
                }
            )
        ],
        want={
            "metric_statements": [
                {
                    "context": "metric",
                    "statements": ['extract_count_metric(true) where metric.name == "my_engine_duration_ms"'],
                },
                {
                    "context": "metric",
                    "error_mode": "ignore",
                    "statements": ['scale_metric(0.001) where metric.name == "my_engine_duration_ms_count"'],
                },
                {
                    "context": "datapoint",
                    "statements": [
                        'set(datapoint.attributes["status"], "ok") where metric.name == "my_engine_duration_ms_count"'
                    ],
                },
                {
                    "context": "metric",
                    "statements": [
                        'set(metric.name, "modelplane_requests_total") where metric.name == "my_engine_duration_ms_count"'
                    ],
                },
            ]
        },
    ),
]


@pytest.mark.parametrize("case", TRANSFORM_CASES, ids=lambda case: case.name)
def test_transform(case: TransformCase) -> None:
    """_transform orders the mappings' statements into the blocks the transform processor runs."""
    got = collector._transform(case.mappings)
    assert got == case.want, case.reason


# Every case renders the built-in mappings, read from the stacks package, for
# the reason CONFIG_CASES gives. The config hashes are of a config that carries
# them, so changing a built-in changes every hash here too.
OBJECTS_CASES = [
    # The config hash is a literal, so it pins one computed in another process:
    # hash() is seeded per process, and would redeploy the collector on every
    # reconcile.
    ObjectsCase(
        name="NoSecret",
        reason="A sink with no Secret gets a collector that mounts only its config.",
        cluster="prod-us-east",
        mappings=list(stacks.BUILTIN_MAPPINGS),
        sinks=[_sink(name="primary", type_="otlphttp", secret=None)],
        extensions={"oauth2client/acme": {"token_url": "https://issuer.acme.example/token"}},
        want=[
            ("collector-serviceaccount", _service_account(), None),
            ("collector-clusterrole", _cluster_role(), None),
            ("collector-clusterrolebinding", _cluster_role_binding(), None),
            (
                "collector-config",
                _config_map(
                    config=_config(
                        exporters={"otlphttp/primary": {"endpoint": "https://otel.acme.example"}},
                        pipeline_exporters=["otlphttp/primary"],
                        extensions={"oauth2client/acme": {"token_url": "https://issuer.acme.example/token"}},
                        service_extensions=["oauth2client/acme"],
                    )
                ),
                None,
            ),
            (
                "collector",
                _deployment(
                    config_hash="c2427ebfd7dfcefa",
                    volumes=[{"name": "config", "configMap": {"name": "modelplane-collector"}}],
                    volume_mounts=[{"name": "config", "mountPath": "/conf"}],
                    env_from=None,
                ),
                "object.status.readyReplicas > 0",
            ),
        ],
    ),
    # Mounted as a file as well as the environment, because a rotated token in
    # an environment variable needs a restart to be read.
    ObjectsCase(
        name="Secret",
        reason="A sink's Secret mounts as a file under the sink's own directory and as environment variables.",
        cluster="prod-us-east",
        mappings=list(stacks.BUILTIN_MAPPINGS),
        sinks=[_sink(name="primary", type_="otlphttp", secret="telemetry-credentials")],
        extensions={"oauth2client/acme": {"token_url": "https://issuer.acme.example/token"}},
        want=[
            ("collector-serviceaccount", _service_account(), None),
            ("collector-clusterrole", _cluster_role(), None),
            ("collector-clusterrolebinding", _cluster_role_binding(), None),
            (
                "collector-config",
                _config_map(
                    config=_config(
                        exporters={
                            "otlphttp/primary": {
                                "endpoint": "https://otel.acme.example",
                                "auth": {"authenticator": "bearertokenauth/primary"},
                            }
                        },
                        pipeline_exporters=["otlphttp/primary"],
                        extensions={
                            "oauth2client/acme": {"token_url": "https://issuer.acme.example/token"},
                            "bearertokenauth/primary": {"filename": "/etc/modelplane/telemetry/primary/token"},
                        },
                        service_extensions=["bearertokenauth/primary", "oauth2client/acme"],
                    )
                ),
                None,
            ),
            (
                "collector",
                _deployment(
                    config_hash="78fbb1dd9d8974d2",
                    volumes=[
                        {"name": "config", "configMap": {"name": "modelplane-collector"}},
                        {"name": "credentials-primary", "secret": {"secretName": "telemetry-credentials"}},
                    ],
                    volume_mounts=[
                        {"name": "config", "mountPath": "/conf"},
                        {
                            "name": "credentials-primary",
                            "mountPath": "/etc/modelplane/telemetry/primary",
                            "readOnly": True,
                        },
                    ],
                    env_from=[{"secretRef": {"name": "telemetry-credentials"}}],
                ),
                "object.status.readyReplicas > 0",
            ),
        ],
    ),
    # Two sinks can both hold a key called token, and neither reads the other's.
    ObjectsCase(
        name="TwoSecrets",
        reason="Two sinks' Secrets mount under a directory each.",
        cluster="prod-us-east",
        mappings=list(stacks.BUILTIN_MAPPINGS),
        sinks=[
            _sink(name="vendor", type_="otlphttp", secret="vendor-token"),
            _sink(name="prometheus", type_="prometheusremotewrite", secret="prom-token"),
        ],
        extensions={"oauth2client/acme": {"token_url": "https://issuer.acme.example/token"}},
        want=[
            ("collector-serviceaccount", _service_account(), None),
            ("collector-clusterrole", _cluster_role(), None),
            ("collector-clusterrolebinding", _cluster_role_binding(), None),
            (
                "collector-config",
                _config_map(
                    config=_config(
                        exporters={
                            "otlphttp/vendor": {
                                "endpoint": "https://otel.acme.example",
                                "auth": {"authenticator": "bearertokenauth/vendor"},
                            },
                            "prometheusremotewrite/prometheus": {
                                "resource_to_telemetry_conversion": {"enabled": True},
                                "endpoint": "https://otel.acme.example",
                                "auth": {"authenticator": "bearertokenauth/prometheus"},
                            },
                        },
                        pipeline_exporters=["otlphttp/vendor", "prometheusremotewrite/prometheus"],
                        extensions={
                            "oauth2client/acme": {"token_url": "https://issuer.acme.example/token"},
                            "bearertokenauth/vendor": {"filename": "/etc/modelplane/telemetry/vendor/token"},
                            "bearertokenauth/prometheus": {"filename": "/etc/modelplane/telemetry/prometheus/token"},
                        },
                        service_extensions=[
                            "bearertokenauth/prometheus",
                            "bearertokenauth/vendor",
                            "oauth2client/acme",
                        ],
                    )
                ),
                None,
            ),
            (
                "collector",
                _deployment(
                    config_hash="9596c2f8945186e3",
                    volumes=[
                        {"name": "config", "configMap": {"name": "modelplane-collector"}},
                        {"name": "credentials-vendor", "secret": {"secretName": "vendor-token"}},
                        {"name": "credentials-prometheus", "secret": {"secretName": "prom-token"}},
                    ],
                    volume_mounts=[
                        {"name": "config", "mountPath": "/conf"},
                        {
                            "name": "credentials-vendor",
                            "mountPath": "/etc/modelplane/telemetry/vendor",
                            "readOnly": True,
                        },
                        {
                            "name": "credentials-prometheus",
                            "mountPath": "/etc/modelplane/telemetry/prometheus",
                            "readOnly": True,
                        },
                    ],
                    env_from=[{"secretRef": {"name": "vendor-token"}}, {"secretRef": {"name": "prom-token"}}],
                ),
                "object.status.readyReplicas > 0",
            ),
        ],
    ),
]


@pytest.mark.parametrize("case", OBJECTS_CASES, ids=lambda case: case.name)
def test_objects(case: ObjectsCase) -> None:
    """objects composes the collector's manifests and their readiness queries."""
    got = collector.objects(case.cluster, case.mappings, case.sinks, case.extensions)
    assert got == case.want, case.reason


EXPORTERS_CASES = [
    # The collector names a second instance of a component <type>/<name>.
    ExportersCase(
        name="TwoOfOneType",
        reason="Two sinks of one type render as two exporters, each named for its sink.",
        sinks=[
            _sink(name="a", type_="otlphttp", secret=None),
            _sink(name="b", type_="otlphttp", secret=None),
        ],
        want={
            "otlphttp/a": {"endpoint": "https://otel.acme.example"},
            "otlphttp/b": {"endpoint": "https://otel.acme.example"},
        },
    ),
    ExportersCase(
        name="NoEndpoint",
        reason="A sink that addresses its destination another way, by brokers or not at all, renders no endpoint.",
        sinks=[
            tdv1alpha1.Sink.model_validate(
                {"name": "bus", "type": "kafka", "config": {"brokers": ["kafka.acme.example:9092"]}}
            ),
            tdv1alpha1.Sink.model_validate({"name": "seen", "type": "debug"}),
        ],
        want={"kafka/bus": {"brokers": ["kafka.acme.example:9092"]}, "debug/seen": {}},
    ),
    # The collector carries no credential on an exporter, only a reference to
    # the authenticator AUTHENTICATORS_CASES composes.
    ExportersCase(
        name="Auth",
        reason="A sink with auth references the authenticator composed for it.",
        sinks=[_sink(name="primary", type_="otlphttp", secret="telemetry-credentials")],
        want={
            "otlphttp/primary": {
                "endpoint": "https://otel.acme.example",
                "auth": {"authenticator": "bearertokenauth/primary"},
            }
        },
    ),
    # The endpoint is Modelplane's, and goes on after the operator's config.
    ExportersCase(
        name="ConfigEndpoint",
        reason="An endpoint in a sink's own config can't redirect it, though the rest of that config applies.",
        sinks=[
            tdv1alpha1.Sink.model_validate(
                {
                    "name": "primary",
                    "type": "otlphttp",
                    "endpoint": "https://otel.acme.example",
                    "config": {"endpoint": "https://elsewhere.example", "compression": "gzip"},
                }
            )
        ],
        want={"otlphttp/primary": {"endpoint": "https://otel.acme.example", "compression": "gzip"}},
    ),
]


@pytest.mark.parametrize("case", EXPORTERS_CASES, ids=lambda case: case.name)
def test_exporters(case: ExportersCase) -> None:
    """exporters renders each sink as a collector exporter."""
    got = collector.exporters(case.sinks)
    assert got == case.want, case.reason


AUTHENTICATORS_CASES = [
    AuthenticatorsCase(
        name="BearerToken",
        reason="A sink with a bearer token key gets an authenticator reading the token from its mounted Secret.",
        sinks=[_sink(name="primary", type_="otlphttp", secret="telemetry-credentials")],
        want={"bearertokenauth/primary": {"filename": "/etc/modelplane/telemetry/primary/token"}},
    ),
]


@pytest.mark.parametrize("case", AUTHENTICATORS_CASES, ids=lambda case: case.name)
def test_authenticators(case: AuthenticatorsCase) -> None:
    """authenticators composes an extension for each sink that asks for auth."""
    got = collector.authenticators(case.sinks)
    assert got == case.want, case.reason


def test_unit_factors() -> None:
    """Every unit conversion factor is an OTTL float literal."""
    # scale_metric takes a float, and 1048576 is an integer to OTTL. The
    # collector refuses to start on it - "must be a float" - which takes the
    # whole cluster's telemetry down, and nothing short of running the
    # collector catches it.
    not_floats = [unit for unit, factor in collector._UNIT_FACTOR.items() if "." not in factor]
    assert not_floats == [], "an OTTL float literal needs a decimal point"
    for factor in collector._UNIT_FACTOR.values():
        float(factor)


def test_units_converted() -> None:
    """Every unit the MetricMapping API offers has a conversion factor."""
    # A unit the API accepts with no conversion is a KeyError at render time.
    annotation = mmv1alpha1.Metric.model_fields["fromUnit"].annotation
    literal = next(a for a in typing.get_args(annotation) if typing.get_origin(a) is typing.Literal)
    assert set(collector._UNIT_FACTOR) == set(typing.get_args(literal))


def test_metric_name_pattern() -> None:
    """The MetricMapping schema rejects a quote in a metric name."""
    # A quote in `from` would end the OTTL comparison early, and rename
    # whatever the rest of the line matched.
    with pytest.raises(ValidationError, match="String should match pattern"):
        mmv1alpha1.Metric.model_validate({"from": 'x" or true or name == "y', "to": "modelplane_x"})
    # This re-validates every built-in metric, read from the stacks package, and
    # can't fail: stacks.metrics builds each one with Metric.model_validate at
    # import, so a name the pattern rejected would fail this module's import
    # before it got here.
    builtin = [m for mapping in stacks.BUILTIN_MAPPINGS for m in mapping.spec.metrics]
    got = [mmv1alpha1.Metric.model_validate({"from": m.from_, "to": m.to}).from_ for m in builtin]
    assert got == [m.from_ for m in builtin]


def test_sglang_mappings() -> None:
    """The SGLang built-in renames nothing onto queue time or preemption."""
    # SGLang publishes neither. Checked against a running SGLang v0.4.9.post2:
    # it has no per-request queue-time metric and no retraction counters at
    # all. The nearest thing, sglang:avg_request_queue_latency, is a gauge of
    # the mean over the last batch - a different measurement from vLLM's
    # per-request histogram, and one name holding both makes a fleet quantile
    # meaningless. The built-in mappings are read from the stacks package,
    # because they're what this checks.
    sglang = {
        m.from_: m.to
        for mapping in stacks.BUILTIN_MAPPINGS
        for m in mapping.spec.metrics
        if m.from_.startswith("sglang:")
    }
    assert sglang, "the SGLang built-in went missing"
    unpublished = {
        source: target
        for source, target in sglang.items()
        if target in {"modelplane_request_queue_seconds", "modelplane_requests_preempted_total"}
        or "retracted" in source
        or "queue_time" in source
    }
    assert unpublished == {}, "SGLang publishes no queue time or preemption to rename"
