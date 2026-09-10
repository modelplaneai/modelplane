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

"""Hand-written components only the Dynamo stack installs.

Grove gang-schedules a multi-node engine as a PodCliqueSet, handing the
gang decision to the KAI Scheduler, and the shared ModelExpress server
coordinates P2P weight transfer between engine pods.

kai-scheduler lives here, not in the cloud halves, because it is
contract surface, not hardware surface: compose-model-replica names it
in schedulerName and labels pods for the Queues here, its version has
nothing to do with the GPU, and the hand-written clouds need a
Modelplane pin for it anyway. A generator that carries it in its own
catalog gets it dropped there, so it installs exactly once and only on
Dynamo clusters (see design/serving-stack-generation.md).
"""

import pathlib
from typing import Any

import yaml

from function.stacks.components import Chart, Component, Manifests

# The name and the `default` namespace are a cross-function contract:
# compose-model-replica points engine pods at this Service by name and
# runs them in that namespace. Both functions hard-code the strings, so
# they change together.
_MODELEXPRESS_NAMESPACE = "default"
_MODELEXPRESS_SERVER_NAME = "modelexpress-server"
_MODELEXPRESS_IMAGE = "nvcr.io/nvidia/ai-dynamo/modelexpress-server:0.4.1"
_MODELEXPRESS_MOUNT = "/mnt/models"
_MODELEXPRESS_PORT = 8001
# Selector label for the server Deployment's pods and its Service.
_MODELEXPRESS_SELECTOR = {"modelplane.ai/modelexpress": _MODELEXPRESS_SERVER_NAME}

# CEL readiness for the server Deployment, which publishes Available,
# not Ready, matching the policy the rest of the pipeline derives
# workload readiness from.
_MODELEXPRESS_SERVER_READY_CEL = (
    'has(object.status.conditions) && object.status.conditions.exists(c, c.type == "Available" && c.status == "True")'
)

_CRDS_DIR = pathlib.Path(__file__).parent / "crds"


def _crds(filename: str) -> list[dict[str, Any]]:
    """Load the CRDs from a YAML file vendored under stacks/crds/."""
    return [
        doc
        for doc in yaml.safe_load_all((_CRDS_DIR / filename).read_text())
        if doc and doc.get("kind") == "CustomResourceDefinition"
    ]


def _kai_queue(name: str, parent: str | None) -> dict[str, Any]:
    """A KAI Queue with unbounded quotas.

    Modelplane's own scheduler (compose-model-deployment) already
    decides what fits on a cluster; KAI's queue is just the admission
    point its gang-scheduler requires. Quotas of -1 mean unbounded.
    """
    spec: dict[str, Any] = {
        "resources": {
            "cpu": {"quota": -1, "limit": -1, "overQuotaWeight": 1},
            "gpu": {"quota": -1, "limit": -1, "overQuotaWeight": 1},
            "memory": {"quota": -1, "limit": -1, "overQuotaWeight": 1},
        },
    }
    if parent:
        spec["parentQueue"] = parent
    return {
        "apiVersion": "scheduling.run.ai/v2",
        "kind": "Queue",
        "metadata": {"name": name},
        "spec": spec,
    }


COMPONENTS: list[Component] = [
    Chart(
        key="grove",
        release="mp-grove-charts",
        namespace="grove-system",
        chart="grove-charts",
        repository="oci://ghcr.io/ai-dynamo/grove",
        version="v0.1.0-alpha.12-rc2",
    ),
    Chart(
        key="kai-scheduler",
        release="mp-kai-scheduler",
        namespace="kai-scheduler",
        chart="kai-scheduler",
        repository="oci://ghcr.io/kai-scheduler/kai-scheduler",
        version="v0.16.8",
        # The Queue CRs below depend on this chart: KAI must serve the
        # Queue CRD and its webhook before they are first applied.
        wait=True,
    ),
    # KAI refuses to schedule a pod whose queue doesn't exist. Its chart
    # installs a default hierarchy, but nothing ties Modelplane's
    # workloads to it, so Modelplane owns its own: an unbounded root and
    # a child queue every Grove-composed PodCliqueSet is labelled into.
    # depends_on holds the kai-scheduler release until the Queues are
    # gone - Queue is a CRD that release owns, and deleting the CRD
    # first leaves the CRs hanging with no controller to finalize them.
    Manifests(
        key="kai-queue-root",
        depends_on=["kai-scheduler"],
        manifests=[_kai_queue("modelplane-root", None)],
    ),
    Manifests(
        key="kai-queue",
        depends_on=["kai-scheduler"],
        manifests=[_kai_queue("modelplane", "modelplane-root")],
    ),
    # ModelExpress CRDs (ModelMetadata, ModelCacheEntry), the metadata
    # backend the shared server uses. Vendored from the upstream
    # release.
    Manifests(
        key="modelexpress-crds",
        manifests=_crds("modelexpress.yaml"),
    ),
    # The shared ModelExpress server, one per Dynamo cluster. It's
    # metadata-only, coordinating P2P weight transfer between engine
    # pods that opt into --load-format modelexpress. It holds no weights
    # itself, so its cache directory is an emptyDir; engine pods keep
    # their own per-cache PVC and register with this server at load.
    # One entry per object: only the Deployment carries a readiness CEL
    # and the depends_on edge, and a single-doc entry keeps its key.
    Manifests(
        key="modelexpress-server-sa",
        manifests=[
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": _MODELEXPRESS_SERVER_NAME, "namespace": _MODELEXPRESS_NAMESPACE},
            },
        ],
    ),
    # RBAC for the Kubernetes CRD metadata backend: ModelMetadata
    # (P2P worker coordination) and ModelCacheEntry (the download
    # registry), plus ConfigMaps holding tensor descriptors too
    # large for a ModelMetadata status field. Mirrors ModelExpress's
    # own Helm chart Role.
    Manifests(
        key="modelexpress-server-role",
        manifests=[
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": _MODELEXPRESS_SERVER_NAME, "namespace": _MODELEXPRESS_NAMESPACE},
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
        ],
    ),
    Manifests(
        key="modelexpress-server-rolebinding",
        manifests=[
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {"name": _MODELEXPRESS_SERVER_NAME, "namespace": _MODELEXPRESS_NAMESPACE},
                "subjects": [
                    {
                        "kind": "ServiceAccount",
                        "name": _MODELEXPRESS_SERVER_NAME,
                        "namespace": _MODELEXPRESS_NAMESPACE,
                    },
                ],
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "Role",
                    "name": _MODELEXPRESS_SERVER_NAME,
                },
            },
        ],
    ),
    Manifests(
        key="modelexpress-server-svc",
        manifests=[
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": _MODELEXPRESS_SERVER_NAME, "namespace": _MODELEXPRESS_NAMESPACE},
                "spec": {
                    "selector": _MODELEXPRESS_SELECTOR,
                    "ports": [{"name": "grpc", "port": _MODELEXPRESS_PORT, "targetPort": _MODELEXPRESS_PORT}],
                },
            },
        ],
    ),
    # depends_on: the server writes CRs of the CRDs above, so they must
    # outlive it for cleanup to resolve.
    Manifests(
        key="modelexpress-server",
        depends_on=["modelexpress-crds"],
        ready=_MODELEXPRESS_SERVER_READY_CEL,
        manifests=[
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": _MODELEXPRESS_SERVER_NAME, "namespace": _MODELEXPRESS_NAMESPACE},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": _MODELEXPRESS_SELECTOR},
                    "template": {
                        "metadata": {"labels": _MODELEXPRESS_SELECTOR},
                        "spec": {
                            "serviceAccountName": _MODELEXPRESS_SERVER_NAME,
                            "containers": [
                                {
                                    "name": "modelexpress-server",
                                    "image": _MODELEXPRESS_IMAGE,
                                    "ports": [{"containerPort": _MODELEXPRESS_PORT}],
                                    "env": [
                                        {
                                            "name": "MODEL_EXPRESS_CACHE_DIRECTORY",
                                            "value": _MODELEXPRESS_MOUNT,
                                        },
                                        {"name": "HF_HUB_CACHE", "value": _MODELEXPRESS_MOUNT},
                                        {"name": "MX_METADATA_BACKEND", "value": "kubernetes"},
                                        {
                                            "name": "POD_NAMESPACE",
                                            "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
                                        },
                                    ],
                                    "volumeMounts": [{"name": "cache", "mountPath": _MODELEXPRESS_MOUNT}],
                                    # Readiness keeps the Service from
                                    # advertising a server that isn't
                                    # listening yet; liveness restarts
                                    # one that stops.
                                    "readinessProbe": {
                                        "tcpSocket": {"port": _MODELEXPRESS_PORT},
                                        "periodSeconds": 10,
                                    },
                                    "livenessProbe": {
                                        "tcpSocket": {"port": _MODELEXPRESS_PORT},
                                        "periodSeconds": 20,
                                    },
                                },
                            ],
                            "volumes": [{"name": "cache", "emptyDir": {}}],
                        },
                    },
                },
            },
        ],
    ),
]
