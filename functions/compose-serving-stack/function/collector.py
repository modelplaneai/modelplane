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

"""The OpenTelemetry collector each inference cluster runs.

It scrapes everything Modelplane installs, renames what it scraped onto the
modelplane_* surface, merges each deployment's replicas into one series, and
exports to the destination.

Rendered here rather than through the OpenTelemetry Operator: its
OpenTelemetryCollector CRD would be one more operator on every GPU cluster to
install, own and upgrade, for a Deployment and a ConfigMap that this composes
directly. The receiver does its own Kubernetes service discovery, so nothing
here needs the operator's target allocator either.
"""

import hashlib
from typing import Any

import yaml
from models.ai.modelplane.metricmapping import v1alpha1 as mmv1alpha1
from models.ai.modelplane.telemetrydestination import v1alpha1 as tdv1alpha1

NAMESPACE = "modelplane-system"
NAME = "modelplane-collector"
_CREDENTIALS_DIR = "/etc/modelplane/telemetry"

IMAGE = "otel/opentelemetry-collector-contrib:0.161.0"

# Pod labels as Kubernetes service discovery spells them: modelplane.ai/x
# arrives as __meta_kubernetes_pod_label_modelplane_ai_x.
#
# On every serving pod, workers included. The engines job selects on it and the
# substrate job drops on it, so the two partition the same set rather than
# leaving a worker to be collected by both.
_DEPLOYMENT_LABEL = "modelplane_ai_deployment"

# The name of the engine's port, which the user declares or compose-model-replica
# adds, and the containers that carry it: the engine, and the endpoint picker,
# whose metrics port has the same name. Matching the container too keeps the job off a port another
# container in the pod names http, such as a sidecar the user writes.
_METRICS_PORT = "http"
_METRICS_CONTAINERS = "engine|epp"

# The port the AI gateway's ext-proc sidecar serves its own metrics on, which
# is where the GenAI semantic-convention series live. Envoy's admin port
# carries Envoy's own statistics and none of these.
_GENAI_PORT = "aigw-admin"

# What a series is attributed to, and the only resource attributes that survive
# to the exporter. The merge across replicas groups on exactly these, so a
# series differing in nothing else is one series.
#
# node is here for the GPU job, whose series belong to hardware rather than to
# a deployment; an engine's series carry no node, which is what lets replicas on
# different nodes merge.
_IDENTITY = (
    "cluster",
    "namespace",
    "deployment",
    "replica",
    "engine",
    "role",
    "node",
    # What the scrape came from, as the Prometheus receiver names it, which an
    # exporter renders as job and instance.
    #
    # Kept because a series has to be unique to whatever produced it or one
    # producer's numbers silently replace another's. The identity above covers
    # an engine; it covers nothing else. Two gateway pods carry no identity at
    # all, two replicas of a substrate controller share a namespace, and a
    # ModelReplica with copies greater than one runs several pods under one
    # replica index. Each of those is a collision, and a collision is a wrong
    # number that looks right.
    #
    # It is the pod's address, so it does churn on a rolling update, which is
    # the cost. Aggregate it away in the query, grouping by the identity above.
    "service.name",
    "service.instance.id",
)

# OTTL quotes strings with double quotes; a Python list renders single ones and
# the collector refuses to start on it.
_IDENTITY_OTTL = ", ".join(f'"{k}"' for k in sorted(_IDENTITY))

_SCRAPE_INTERVAL = "15s"
_SUBSTRATE_INTERVAL = "30s"


def _relabel_pod_identity() -> list[dict[str, Any]]:
    """Carry Modelplane's identity from the pod's labels onto every series.

    A series inherits these, so a MetricMapping needs no labels of its own.
    compose-model-replica stamps them; a pod carrying none is one no deployment
    owns.

    No model: a ModelReplica doesn't know which ModelService fronts it, and a
    model name carries a slash, which a label value can't. Deployment is finer
    grained anyway - a deployment serves one model, a model may have several.
    """
    return [
        {"source_labels": [f"__meta_kubernetes_pod_label_{src}"], "target_label": dst}
        for src, dst in (
            ("modelplane_ai_deployment", "deployment"),
            ("modelplane_ai_replica", "replica"),
            ("modelplane_ai_engine", "engine"),
            ("modelplane_ai_role", "role"),
        )
    ] + [{"source_labels": ["__meta_kubernetes_namespace"], "target_label": "namespace"}]


def _dcgm_selector(action: str) -> dict[str, Any]:
    """Match a GPU exporter, however it was packaged.

    The two spell it differently - gke-managed-dcgm-exporter and dcgm-exporter -
    and put the name on different labels depending on who packaged it, so both
    are read and the match is on what they share. One definition, used by the
    job that keeps them and the job that has to leave them alone, because the
    two drifting apart is how a series gets collected twice.
    """
    return {
        "source_labels": [
            "__meta_kubernetes_pod_label_app_kubernetes_io_name",
            "__meta_kubernetes_pod_label_app",
        ],
        "action": action,
        "regex": ".*dcgm.*",
    }


def _annotated_path() -> list[dict[str, Any]]:
    """Take the metrics path from the pod's own annotation, where it sets one."""
    return [
        {
            "source_labels": ["__meta_kubernetes_pod_annotation_prometheus_io_path"],
            "action": "replace",
            "target_label": "__metrics_path__",
            "regex": "(.+)",
        }
    ]


def _annotated_port() -> list[dict[str, Any]]:
    """Take the port from the pod's own annotation, where it sets one.

    Service discovery makes a target of every declared container port, so
    without this a pod is scraped on whichever it declared first. Rewriting
    them all to the annotated one leaves identical targets, which discovery
    then collapses to one.
    """
    return [
        {
            "source_labels": ["__address__", "__meta_kubernetes_pod_annotation_prometheus_io_port"],
            "action": "replace",
            "target_label": "__address__",
            # A bracketed IPv6 host or a bare one: [^:]+ alone never matches
            # [2001:db8::1]:9090, so an IPv6 pod would keep whichever port
            # discovery happened to pick.
            "regex": r"(\[.+\]|[^:]+)(?::\d+)?;(\d+)",
            "replacement": "$1:$2",
        }
    ]


def _scrape_configs() -> list[dict[str, Any]]:
    """What to scrape on an inference cluster.

    Jobs over disjoint sets of pods, so nothing is scraped twice: the engines
    Modelplane runs, the gateways in front of them, the GPU exporter the
    cluster came with, and everything else the serving stack installs.

    The front door needs two of them. Envoy publishes its own statistics on its
    admin port, and the GenAI metrics a caller's experience is measured by come
    from the AI gateway's ext-proc on a different port, at a different path.
    """
    return [
        {
            "job_name": "modelplane-engines",
            "scrape_interval": _SCRAPE_INTERVAL,
            "kubernetes_sd_configs": [{"role": "pod"}],
            "relabel_configs": [
                # Every serving pod carries the deployment it belongs to,
                # workers included: a worker holds GPUs, and the GPU series are
                # the deployment's. Matched on presence, because the value is
                # the deployment's name.
                {
                    "source_labels": [f"__meta_kubernetes_pod_label_{_DEPLOYMENT_LABEL}"],
                    "action": "keep",
                    "regex": ".+",
                },
                {
                    "source_labels": ["__meta_kubernetes_pod_container_port_name"],
                    "action": "keep",
                    "regex": _METRICS_PORT,
                },
                {
                    "source_labels": ["__meta_kubernetes_pod_container_name"],
                    "action": "keep",
                    "regex": _METRICS_CONTAINERS,
                },
                *_relabel_pod_identity(),
            ],
        },
        {
            "job_name": "modelplane-gateway",
            "scrape_interval": _SCRAPE_INTERVAL,
            "kubernetes_sd_configs": [{"role": "pod"}],
            "relabel_configs": [
                {
                    "source_labels": ["__meta_kubernetes_pod_label_gateway_envoyproxy_io_owning_gateway_name"],
                    "action": "keep",
                    "regex": ".+",
                },
                {"source_labels": ["__meta_kubernetes_pod_container_port_name"], "action": "keep", "regex": "metrics"},
                # Envoy publishes Prometheus on its admin port, not at
                # /metrics, and says so in its own annotation. Without this the
                # front door 404s every interval and the modelplane_frontend_*
                # series - the ones an SLO is written against - never arrive.
                *_annotated_path(),
            ],
        },
        {
            # The GenAI metrics, which are the SLO ones: what a caller waited,
            # measured the same way whatever engine served it.
            #
            # They come from the AI gateway's ext-proc, not from the proxy. It
            # runs as a native sidecar - an initContainer with restartPolicy
            # Always - so it is easy to miss when reading the pod, and it
            # serves its own admin port rather than Envoy's. A job of its own
            # because the two ports want different paths: the proxy publishes
            # at the path its annotation names, the ext-proc at /metrics.
            "job_name": "modelplane-gateway-genai",
            "scrape_interval": _SCRAPE_INTERVAL,
            "kubernetes_sd_configs": [{"role": "pod"}],
            "relabel_configs": [
                {
                    "source_labels": ["__meta_kubernetes_pod_label_gateway_envoyproxy_io_owning_gateway_name"],
                    "action": "keep",
                    "regex": ".+",
                },
                {
                    "source_labels": ["__meta_kubernetes_pod_container_port_name"],
                    "action": "keep",
                    "regex": _GENAI_PORT,
                },
            ],
        },
        {
            "job_name": "modelplane-gpu",
            "scrape_interval": _SCRAPE_INTERVAL,
            "kubernetes_sd_configs": [{"role": "pod"}],
            "relabel_configs": [
                # A job of its own because nobody annotates DCGM for scraping
                # and Modelplane doesn't install it: GKE runs a managed one in
                # gke-managed-system, the NVIDIA GPU operator installs its own
                # elsewhere, and the substrate job sees neither. Without this
                # every modelplane_gpu_* series is empty on a cloud that
                # provides its own - which is every cloud.
                #
                # Matched on the name rather than an exact label, because the
                # two spell it differently: gke-managed-dcgm-exporter and
                # dcgm-exporter. Both labels are read, since which one carries
                # the name depends on who packaged it.
                _dcgm_selector("keep"),
                {"source_labels": ["__meta_kubernetes_pod_container_port_name"], "action": "keep", "regex": "metrics"},
                # The node, because a GPU series belongs to hardware rather
                # than to a deployment. DCGM names the card itself.
                {"source_labels": ["__meta_kubernetes_pod_node_name"], "target_label": "node"},
            ],
        },
        {
            "job_name": "modelplane-substrate",
            "scrape_interval": _SUBSTRATE_INTERVAL,
            "kubernetes_sd_configs": [{"role": "pod"}],
            "relabel_configs": [
                {
                    "source_labels": ["__meta_kubernetes_pod_annotation_prometheus_io_scrape"],
                    "action": "keep",
                    "regex": "true",
                },
                # Everything a job above already names. Each of these can
                # annotate itself for scraping - the gateway does, and a
                # GPU operator's DCGM usually does - and collecting one here
                # as well would carry it twice under two job names.
                {
                    "source_labels": ["__meta_kubernetes_pod_label_gateway_envoyproxy_io_owning_gateway_name"],
                    "action": "drop",
                    "regex": ".+",
                },
                {
                    "source_labels": [f"__meta_kubernetes_pod_label_{_DEPLOYMENT_LABEL}"],
                    "action": "drop",
                    "regex": ".+",
                },
                _dcgm_selector("drop"),
                # The rest of the same convention, not just the first line of
                # it. A pod that says scrape me generally also says where: the
                # cert-manager webhook declares 10250 first and annotates 9402,
                # so honouring only the keep scrapes its TLS port over plain
                # HTTP and logs a 400 every interval.
                *_annotated_path(),
                *_annotated_port(),
                {"source_labels": ["__meta_kubernetes_namespace"], "target_label": "namespace"},
            ],
        },
    ]


# Taking a part of a histogram is a function that mints a new metric beside it,
# named for the part. The rename then applies to that.
_PART_FUNCTION = {"Count": "extract_count_metric(true)", "Sum": "extract_sum_metric(true)"}
_PART_SUFFIX = {"Count": "_count", "Sum": "_sum"}

# What one of the source unit is worth in the base unit the target name claims.
#
# Applied with scale_metric in the metric context rather than by setting a
# datapoint's value, because a histogram has no single value to set: its sum,
# its minimum and maximum, and every one of its bucket boundaries are all in
# the source unit, and a conversion that reached only the value would rename a
# histogram to seconds with its buckets still in milliseconds. scale_metric
# carries all of them, and an integer datapoint as well, which reading
# value_double would have read as nought.
#
# Written out rather than as a Python float so nothing has to render one:
# 1e-09 is not an OTTL literal. Every one carries a decimal point, because
# scale_metric takes a float and the collector refuses to start on an integer
# literal in that position.
_UNIT_FACTOR = {
    "Millijoules": "0.001",
    "Milliseconds": "0.001",
    "Nanoseconds": "0.000000001",
    "Mebibytes": "1048576.0",
    "Percent": "0.01",
}


def statements(mappings: list[mmv1alpha1.MetricMapping]) -> tuple[list[str], list[str], list[str], list[str]]:
    """Compile the mappings to OTTL, as (extract, scale, datapoint, metric) statements.

    OTTL is rendered here rather than written in a MetricMapping so the kind
    stays a description of what a component emits, and the collector's own
    configuration language stays Modelplane's problem. It is also the only
    place that knows a unit conversion has to run somewhere a rename cannot.
    """
    extract: list[str] = []
    scale: list[str] = []
    datapoint: list[str] = []
    metric: list[str] = []
    for mapping in mappings:
        for m in mapping.spec.metrics:
            source = m.from_
            if m.part:
                # In a block of its own, ahead of the datapoint statements. The
                # extraction mints a new metric, and a label or a unit
                # conversion for it selects on the name that extraction gives
                # it - a name that does not exist until the extraction has run.
                # The extraction leaves the histogram itself alone, though the
                # filter downstream drops it unless a mapping renames it too.
                extract.append(f'{_PART_FUNCTION[m.part]} where metric.name == "{source}"')
                source = f"{source}{_PART_SUFFIX[m.part]}"
            if m.fromUnit:
                scale.append(f'scale_metric({_UNIT_FACTOR[m.fromUnit]}) where metric.name == "{source}"')
            # Labels before the rename, while the series still answers to the
            # name this mapping selected on. After it, two folded mappings share
            # one name and a statement could no longer tell them apart.
            datapoint.extend(_label_statements(source, m.labels or []))
            metric.append(f'set(metric.name, "{m.to}") where metric.name == "{source}"')
    return extract, scale, datapoint, metric


def _quote(value: str) -> str:
    """One OTTL string literal, with anything that would end it escaped.

    A metric name and a label name are pattern-constrained by the XRD, but a
    label's value is free text, and so is every key and value of a `values`
    remap - they have to be, because they carry whatever vocabulary the
    component already emits. An unescaped quote in one of them would close the
    literal early and leave the rest of it as OTTL, which at best stops the
    collector from starting and at worst runs.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _label_statements(source: str, labels: list[Any]) -> list[str]:
    """Set this mapping's labels on the datapoints of one source metric."""
    out: list[str] = []
    for label in labels:
        if label.value is not None:
            out.append(
                f'set(datapoint.attributes["{label.name}"], {_quote(label.value)}) where metric.name == "{source}"'
            )
            continue
        carried = f'datapoint.attributes["{label.from_}"]'
        out.append(f'set(datapoint.attributes["{label.name}"], {carried}) where metric.name == "{source}"')
        for old, new in sorted((label.values or {}).items()):
            out.append(
                f'set(datapoint.attributes["{label.name}"], {_quote(new)}) '
                f'where metric.name == "{source}" and {carried} == {_quote(old)}'
            )
        # The component's own name for it goes, or the series carries the same
        # fact twice under two labels and costs twice the cardinality. Unless
        # the mapping carried the label onto its own name, which is how a pure
        # value remap is written: there the delete would take the label the
        # statements above just set.
        if label.from_ != label.name:
            out.append(f'delete_key(datapoint.attributes, "{label.from_}") where metric.name == "{source}"')
    return out


def _transform(mappings: list[mmv1alpha1.MetricMapping]) -> dict[str, Any]:
    """Parts extracted, then units converted, then labels, then every rename.

    Four blocks rather than one list. Each block finishes over every metric
    before the next one starts, and each step here depends on the one before
    having finished everywhere: an extraction mints the metric a conversion
    scales, a conversion has to reach a datapoint the rename would otherwise
    have stranded in the source unit, and a label has to be set while the
    series still answers to the name its mapping selected on. A statement
    reaching a datapoint's attributes also can't run in the metric context.

    The conversions ignore their own errors. scale_metric refuses an
    exponential histogram, and under the default error mode that one refusal
    would drop the whole batch - every metric from every pod in the scrape,
    not just the one it could not convert.
    """
    extract, scale, datapoint, metric = statements(mappings)
    blocks: list[dict[str, Any]] = []
    if extract:
        blocks.append({"context": "metric", "statements": extract})
    if scale:
        blocks.append({"context": "metric", "error_mode": "ignore", "statements": scale})
    if datapoint:
        blocks.append({"context": "datapoint", "statements": datapoint})
    blocks.append({"context": "metric", "statements": metric})
    return {"metric_statements": blocks}


# Exporters that flatten a series into labels, losing anything held as a
# resource attribute unless told otherwise. Modelplane's identity - the
# cluster, deployment, engine and role a series belongs to - is all held there,
# because that is what the merge across replicas groups on, so without this a
# Prometheus backend receives every series stripped of everything that says
# what it measures.
#
# Applied under an operator's own config rather than over it: this is a default,
# and a sink that sets it wins.
#
# Keyed under both spellings of the remote-write exporter. The component
# registers as prometheus_remote_write and still answers to the older
# prometheusremotewrite, so a sink writing the name the collector's own
# documentation gives would otherwise match nothing here and export every
# series stripped of its identity - a silent loss, since the sink itself works.
_RESOURCE_TO_LABELS = {"resource_to_telemetry_conversion": {"enabled": True}}
_SINK_DEFAULTS = {
    "prometheus_remote_write": _RESOURCE_TO_LABELS,
    "prometheusremotewrite": _RESOURCE_TO_LABELS,
    "prometheus": _RESOURCE_TO_LABELS,
}


def _sink_defaults(exporter: str) -> dict[str, Any]:
    return {k: dict(v) for k, v in _SINK_DEFAULTS.get(exporter, {}).items()}


def _credential_path(sink: tdv1alpha1.Sink, key: str) -> str:
    return f"{_CREDENTIALS_DIR}/{sink.name}/{key}"


def authenticators(sinks: list[tdv1alpha1.Sink]) -> dict[str, Any]:
    """The extensions Modelplane composes for the sinks that asked for one.

    The collector carries no credential on an exporter: it authenticates
    through an extension the exporter names. A typed auth block is therefore
    an extension plus a reference, not a field, and composing both is what
    keeps `auth` from being something an operator has to assemble by hand.
    """
    composed: dict[str, Any] = {}
    for sink in sinks:
        if sink.auth and sink.auth.bearerTokenKey:
            composed[f"bearertokenauth/{sink.name}"] = {
                # A file rather than the environment: an environment variable
                # is fixed for the life of the process, so a rotated token
                # would need a restart to be read.
                "filename": _credential_path(sink, sink.auth.bearerTokenKey),
            }
    return composed


def exporters(sinks: list[tdv1alpha1.Sink]) -> dict[str, Any]:
    """The sinks as the collector's exporters block.

    A sink renders under `<type>/<name>`, which is how the collector names a
    second instance of one component, so two sinks of the same type don't
    collide.

    The endpoint and the authenticator reference are Modelplane's, and go on
    last: an operator's own config can carry anything the exporter takes, but
    not quietly redirect the sink somewhere else or unpick its credential.
    """
    rendered: dict[str, Any] = {}
    for sink in sinks:
        cfg: dict[str, Any] = _sink_defaults(sink.type) | dict(sink.config or {})
        if sink.endpoint:
            cfg["endpoint"] = sink.endpoint
        if sink.auth and sink.auth.bearerTokenKey:
            cfg["auth"] = {"authenticator": f"bearertokenauth/{sink.name}"}
        rendered[f"{sink.type}/{sink.name}"] = cfg
    return rendered


def config(
    cluster: str,
    mappings: list[mmv1alpha1.MetricMapping],
    sinks: list[tdv1alpha1.Sink],
    extensions: dict[str, Any],
) -> str:
    """The collector's configuration, as YAML.

    Only modelplane_* leaves the cluster: a series no mapping renamed is one
    whose meaning Modelplane cannot vouch for across engines, and it costs the
    same to carry as one that was renamed.
    """
    sink_exporters = exporters(sinks)
    # Modelplane's authenticators last: an operator's extensions can define
    # anything, but not replace the one composed for a sink's own auth block.
    extensions = {**extensions, **authenticators(sinks)}
    processors: dict[str, Any] = {
        # First in the pipeline, so it refuses work before anything allocates
        # for it. Nothing bounds what one interval brings off a fleet of
        # engines, and the kill that follows a spike takes the whole cluster's
        # telemetry with it until the pod is back.
        "memory_limiter": {"check_interval": "1s", "limit_percentage": 80, "spike_limit_percentage": 25},
        # cluster is stamped here rather than downstream: one receiver on the
        # control plane sees a merged stream and cannot tell senders apart.
        "resource/cluster": {"attributes": [{"key": "cluster", "value": cluster, "action": "upsert"}]},
        # Everything a replica carries that is the replica's rather than the
        # deployment's. Discovery attaches the pod's name, uid and replicaset,
        # and the scrape its address, and all of it lands on the resource -
        # where it does two kinds of damage.
        #
        # The merge below groups on the resource, so a pod name on it keeps
        # every replica in a resource of its own and nothing merges. And an
        # exporter that flattens resources into labels then publishes a pod
        # label, which a rolling update mints afresh on every deploy and a
        # billing backend counts as active for half an hour after it dies.
        #
        # An allowlist rather than a list of what to drop: what discovery
        # attaches grows, and a series carrying something nobody chose is the
        # failure this prevents.
        "transform/identity": {
            "metric_statements": [
                {"context": "resource", "statements": [f"keep_keys(resource.attributes, [{_IDENTITY_OTTL}])"]}
            ]
        },
        "transform/modelplane": _transform(mappings),
        # Discovery writes the identity onto each datapoint; this lifts it onto
        # the resource, which is where an exporter that flattens a series into
        # labels looks for it. Without it a series arrives carrying only the
        # cluster.
        #
        # It does not merge a deployment's replicas, whatever its name
        # suggests. Each replica is scraped separately, so each is its own
        # batch, and there is never a second replica here to merge with. They
        # stay separate series, told apart by the replica label, and a query
        # over the deployment combines them - which is the only place the
        # arithmetic can be right, because adding two cumulative readings taken
        # at different moments is not the traffic that happened.
        "groupbyattrs/identity": {"keys": list(_IDENTITY)},
        "filter/modelplane": {"metrics": {"metric": ['not IsMatch(name, "^modelplane_.*")']}},
        "batch": {"timeout": "10s"},
    }
    pipeline = [
        "memory_limiter",
        "resource/cluster",
        "transform/identity",
        "transform/modelplane",
        "groupbyattrs/identity",
        "filter/modelplane",
        "batch",
    ]

    service: dict[str, Any] = {
        "pipelines": {
            "metrics": {
                "receivers": ["prometheus"],
                "processors": pipeline,
                "exporters": sorted(sink_exporters),
            }
        },
    }
    cfg: dict[str, Any] = {
        "receivers": {"prometheus": {"config": {"scrape_configs": _scrape_configs()}}},
        "processors": processors,
        "exporters": sink_exporters,
        "service": service,
    }
    if extensions:
        cfg["extensions"] = extensions
        # An authenticator the service does not list is one the collector will
        # not load, and an exporter referencing it then fails at startup.
        service["extensions"] = sorted(extensions)
    return yaml.safe_dump(cfg, sort_keys=False)


def _digest(rendered: str) -> str:
    """A stable hash of the rendered config, so the pod restarts when it changes."""
    return hashlib.sha256(rendered.encode()).hexdigest()[:16]


def objects(
    cluster: str,
    mappings: list[mmv1alpha1.MetricMapping],
    sinks: list[tdv1alpha1.Sink],
    extensions: dict[str, Any],
) -> list[tuple[str, dict[str, Any], str | None]]:
    """The collector as (key, manifest, readiness CEL) triples.

    Composed here rather than as a Manifests entry in the stack because the
    stack is fixed at build time and every object here depends on request-time
    data: the ConfigMap holds config rendered from the TelemetryDestination and
    the MetricMappings, the Deployment carries that config's digest and the
    destination's optional Secret mounts, and none of it exists at all until a
    TelemetryDestination does. The ServiceAccount and RBAC would fit the stack,
    but splitting one component across two mechanisms would put a collector's
    permissions on clusters running no collector.
    """
    labels = {"app.kubernetes.io/name": NAME, "app.kubernetes.io/managed-by": "modelplane"}
    rendered = config(cluster, mappings, sinks, extensions)
    volumes: list[dict[str, Any]] = [{"name": "config", "configMap": {"name": NAME}}]
    mounts: list[dict[str, Any]] = [{"name": "config", "mountPath": "/conf"}]
    env_from: list[dict[str, Any]] = []
    for sink in sinks:
        if not sink.secretRef:
            continue
        # Mounted both ways. An environment variable is fixed for the life of a
        # process, so a rotated credential would need a restart to be read; a
        # mounted file is refreshed in place and an authenticator reading one
        # picks the new credential up without one. The file is under the sink's
        # own directory, so two sinks can both hold a key called `token`.
        volume = f"credentials-{sink.name}"
        volumes.append({"name": volume, "secret": {"secretName": sink.secretRef.name}})
        mounts.append({"name": volume, "mountPath": f"{_CREDENTIALS_DIR}/{sink.name}", "readOnly": True})
        env_from.append({"secretRef": {"name": sink.secretRef.name}})

    return [
        (
            "collector-serviceaccount",
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": NAME, "namespace": NAMESPACE, "labels": labels},
            },
            None,
        ),
        (
            "collector-clusterrole",
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRole",
                "metadata": {"name": NAME, "labels": labels},
                "rules": [
                    {
                        "apiGroups": [""],
                        "resources": ["pods", "services", "endpoints", "nodes", "nodes/metrics"],
                        "verbs": ["get", "list", "watch"],
                    },
                    {"nonResourceURLs": ["/metrics"], "verbs": ["get"]},
                ],
            },
            None,
        ),
        (
            "collector-clusterrolebinding",
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRoleBinding",
                "metadata": {"name": NAME, "labels": labels},
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": NAME},
                "subjects": [{"kind": "ServiceAccount", "name": NAME, "namespace": NAMESPACE}],
            },
            None,
        ),
        (
            "collector-config",
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": NAME, "namespace": NAMESPACE, "labels": labels},
                "data": {"collector.yaml": rendered},
            },
            None,
        ),
        (
            "collector",
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": NAME, "namespace": NAMESPACE, "labels": labels},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app.kubernetes.io/name": NAME}},
                    "template": {
                        "metadata": {
                            "labels": {"app.kubernetes.io/name": NAME},
                            # A changed ConfigMap doesn't restart the pod on
                            # its own. sha256 rather than hash(), whose string
                            # seed is randomised per process and would redeploy
                            # the collector on every reconcile.
                            "annotations": {"modelplane.ai/config-hash": _digest(rendered)},
                        },
                        "spec": {
                            "serviceAccountName": NAME,
                            "containers": [
                                {
                                    "name": "collector",
                                    "image": IMAGE,
                                    "args": ["--config=/conf/collector.yaml"],
                                    "volumeMounts": mounts,
                                    **({"envFrom": env_from} if env_from else {}),
                                    "resources": {
                                        "requests": {"cpu": "100m", "memory": "256Mi"},
                                        "limits": {"memory": "512Mi"},
                                    },
                                }
                            ],
                            "volumes": volumes,
                        },
                    },
                },
            },
            "object.status.readyReplicas > 0",
        ),
    ]
