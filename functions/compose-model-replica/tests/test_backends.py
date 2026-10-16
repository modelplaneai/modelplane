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

"""Tests for compose-model-replica's backends and routing.

A backend builds the workload (Deployment, LeaderWorkerSet, or PodCliqueSet) and
the ResourceClaimTemplates for one engine. routing.apply fronts a replica's
engines with an InferencePool, endpoint picker and HTTPRoute.

A device request's CEL selector is as compose-model-deployment stamps it.
"""

import copy
import dataclasses
import json

import pytest
from function import routing
from function.backends import base, grove, llmd, native
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class BuildCase:
    """A test case for a backend's build."""

    name: str
    reason: str
    backend: base.Backend
    replica: v1alpha1.ModelReplica
    provider_config: str
    serving_label: str
    stack: str
    want: dict[str, dict]


@dataclasses.dataclass
class SelectBackendCase:
    """A test case for base.select_backend."""

    name: str
    reason: str
    engine: v1alpha1.Engine
    stack: str
    want: str


@dataclasses.dataclass
class CacheMountsCase:
    """A test case for base.cache_mounts."""

    name: str
    reason: str
    replica: v1alpha1.ModelReplica
    want: tuple[list[dict], list[dict]]


@dataclasses.dataclass
class CacheEnvCase:
    """A test case for base.cache_env."""

    name: str
    reason: str
    replica: v1alpha1.ModelReplica
    want: list[dict]


@dataclasses.dataclass
class ApplyCase:
    """A test case for routing.apply."""

    name: str
    reason: str
    composed: dict[str, dict]
    replica: v1alpha1.ModelReplica
    provider_config: str
    want: dict[str, dict]


@dataclasses.dataclass
class KvBlockSizeCase:
    """A test case for routing._kv_block_size."""

    name: str
    reason: str
    engine_args: list[str]
    want: int


@dataclasses.dataclass
class UnifiedBlockSizeCase:
    """A test case for the blockSizeTokens routing._unified_epp_config_yaml renders."""

    name: str
    reason: str
    block_size: int
    want: str


@dataclasses.dataclass
class PickerCase:
    """A test case for routing._picker."""

    name: str
    reason: str
    serving: v1alpha1.Serving | None
    want: tuple[str, str]


@dataclasses.dataclass
class RoutingMissingCase:
    """A test case for routing.prefill_decode_routing_missing."""

    name: str
    reason: str
    serving: v1alpha1.Serving | None
    want: bool


@dataclasses.dataclass
class RenderPickerConfigCase:
    """A test case for routing._render_picker_config."""

    name: str
    reason: str
    config: dict
    want: str


@dataclasses.dataclass
class RemoteNamespaceCase:
    """A test case for base.remote_namespace."""

    name: str
    reason: str
    replica: v1alpha1.ModelReplica
    want: str


def _route() -> dict:
    """The Object composing the replica's HTTPRoute to its InferencePool."""
    return {
        "spec": {
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
            "readiness": {"policy": "SuccessfulCreate"},
            "forProvider": {
                "manifest": {
                    "apiVersion": "gateway.networking.k8s.io/v1",
                    "kind": "HTTPRoute",
                    "metadata": {"name": "r", "namespace": "mp-ml-team-51733"},
                    "spec": {
                        "parentRefs": [{"name": "cluster-gateway", "namespace": "modelplane-system"}],
                        "rules": [
                            {
                                "matches": [{"path": {"type": "PathPrefix", "value": "/ml-team/r/"}}],
                                # No request timeout, so long token streams
                                # aren't severed.
                                "timeouts": {"request": "0s"},
                                "filters": [
                                    {
                                        "type": "URLRewrite",
                                        "urlRewrite": {
                                            "path": {"type": "ReplacePrefixMatch", "replacePrefixMatch": "/"}
                                        },
                                    }
                                ],
                                "backendRefs": [
                                    {
                                        "group": "inference.networking.k8s.io",
                                        "kind": "InferencePool",
                                        "name": "r-pool",
                                    }
                                ],
                            }
                        ],
                    },
                }
            },
        }
    }


def _inference_pool() -> dict:
    """The Object composing the InferencePool that fronts the replica's serving pods."""
    return {
        "spec": {
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
            "readiness": {"policy": "SuccessfulCreate"},
            "forProvider": {
                "manifest": {
                    "apiVersion": "inference.networking.k8s.io/v1",
                    "kind": "InferencePool",
                    "metadata": {"name": "r-pool", "namespace": "mp-ml-team-51733"},
                    "spec": {
                        "selector": {"matchLabels": {"modelplane.ai/serving": "r"}},
                        "targetPorts": [{"number": 8000}],
                        "endpointPickerRef": {
                            "name": "r-epp",
                            "port": {"number": 9002},
                            "failureMode": "FailOpen",
                        },
                    },
                }
            },
        }
    }


def _epp(*, labels: dict, config_checksum: str, image: str) -> dict:
    """The Object composing the endpoint picker's Deployment, its pod labeled and rolled by its config's checksum.

    The picker serves its metrics on a port named http, which the collector's
    engine job scrapes, and with --metrics-endpoint-auth=false. By default it
    authenticates callers by TokenReview, which needs a ClusterRole the picker's
    namespaced ServiceAccount can't hold, so every scrape would be rejected.
    --secure-serving stays on: it's the ext-proc gRPC server Envoy calls.
    """
    return {
        "spec": {
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
            "readiness": {
                "policy": "DeriveFromCelQuery",
                "celQuery": 'has(object.status.conditions) && object.status.conditions.exists(c, c.type == "Available" && c.status == "True")',
            },
            "forProvider": {
                "manifest": {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "metadata": {"name": "r-epp", "namespace": "mp-ml-team-51733"},
                    "spec": {
                        "replicas": 1,
                        "selector": {"matchLabels": {"app": "r-epp"}},
                        "template": {
                            "metadata": {
                                "labels": labels,
                                "annotations": {"modelplane.ai/epp-config-checksum": config_checksum},
                            },
                            "spec": {
                                "serviceAccountName": "r-epp",
                                "containers": [
                                    {
                                        "name": "epp",
                                        "image": image,
                                        "args": [
                                            "--pool-name=r-pool",
                                            "--pool-namespace=mp-ml-team-51733",
                                            "--pool-group=inference.networking.k8s.io",
                                            "--config-file=/config/epp-config.yaml",
                                            "--grpc-port=9002",
                                            "--metrics-port=9090",
                                            "--metrics-endpoint-auth=false",
                                        ],
                                        "ports": [
                                            {"name": "grpc", "containerPort": 9002},
                                            {"name": "grpc-health", "containerPort": 9003},
                                            {"name": "http", "containerPort": 9090},
                                        ],
                                        "readinessProbe": {"grpc": {"port": 9003}, "periodSeconds": 2},
                                        "volumeMounts": [{"name": "config", "mountPath": "/config"}],
                                    }
                                ],
                                "volumes": [{"name": "config", "configMap": {"name": "r-epp"}}],
                            },
                        },
                    },
                }
            },
        }
    }


def _epp_config(*, config: str) -> dict:
    """The Object composing the ConfigMap that holds the endpoint picker's config."""
    return {
        "spec": {
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
            "readiness": {"policy": "SuccessfulCreate"},
            "forProvider": {
                "manifest": {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "r-epp", "namespace": "mp-ml-team-51733"},
                    "data": {"epp-config.yaml": config},
                }
            },
        }
    }


def _epp_role() -> dict:
    """The Object composing the endpoint picker's Role."""
    return {
        "spec": {
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
            "readiness": {"policy": "SuccessfulCreate"},
            "forProvider": {
                "manifest": {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "Role",
                    "metadata": {"name": "r-epp", "namespace": "mp-ml-team-51733"},
                    "rules": [
                        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "watch", "list"]},
                        {
                            "apiGroups": ["inference.networking.k8s.io"],
                            "resources": ["inferencepools"],
                            "verbs": ["get", "watch", "list"],
                        },
                        {
                            "apiGroups": ["inference.networking.x-k8s.io"],
                            "resources": ["inferenceobjectives"],
                            "verbs": ["get", "watch", "list"],
                        },
                    ],
                }
            },
        }
    }


def _epp_role_binding() -> dict:
    """The Object composing the endpoint picker's RoleBinding."""
    return {
        "spec": {
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
            "readiness": {"policy": "SuccessfulCreate"},
            "forProvider": {
                "manifest": {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "RoleBinding",
                    "metadata": {"name": "r-epp", "namespace": "mp-ml-team-51733"},
                    "subjects": [{"kind": "ServiceAccount", "name": "r-epp", "namespace": "mp-ml-team-51733"}],
                    "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "r-epp"},
                }
            },
        }
    }


def _epp_service_account() -> dict:
    """The Object composing the endpoint picker's ServiceAccount."""
    return {
        "spec": {
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
            "readiness": {"policy": "SuccessfulCreate"},
            "forProvider": {
                "manifest": {
                    "apiVersion": "v1",
                    "kind": "ServiceAccount",
                    "metadata": {"name": "r-epp", "namespace": "mp-ml-team-51733"},
                }
            },
        }
    }


def _epp_service() -> dict:
    """The Object composing the endpoint picker's Service."""
    return {
        "spec": {
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
            "readiness": {"policy": "SuccessfulCreate"},
            "forProvider": {
                "manifest": {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": "r-epp", "namespace": "mp-ml-team-51733"},
                    "spec": {
                        "selector": {"app": "r-epp"},
                        "ports": [{"name": "grpc-ext-proc", "port": 9002, "targetPort": 9002, "appProtocol": "http2"}],
                    },
                }
            },
        }
    }


def _deployment(
    *,
    name: str,
    claim_template_name: str,
    replicas: int,
    pod_metadata: dict,
    containers: list[dict],
    volumes: list[dict],
) -> dict:
    """The Object composing a Standalone engine's Deployment."""
    return {
        "spec": {
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
            "readiness": {
                "policy": "DeriveFromCelQuery",
                "celQuery": 'has(object.status.conditions) && object.status.conditions.exists(c, c.type == "Available" && c.status == "True")',
            },
            "forProvider": {
                "manifest": {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "metadata": {"name": name, "namespace": "mp-ml-team-51733"},
                    "spec": {
                        "replicas": replicas,
                        "selector": {"matchLabels": {"modelplane.ai/workload": name}},
                        "template": {
                            "metadata": pod_metadata,
                            "spec": {
                                "containers": containers,
                                "volumes": volumes,
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": claim_template_name,
                                    }
                                ],
                                "tolerations": [
                                    {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
                                ],
                            },
                        },
                    },
                }
            },
        }
    }


def _leader_worker_set(
    *,
    replicas: int,
    size: int,
    leader_containers: list[dict],
    leader_volumes: list[dict],
    worker_containers: list[dict],
    worker_volumes: list[dict],
) -> dict:
    """The Object composing the main engine's LeaderWorkerSet."""
    return {
        "spec": {
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
            "readiness": {
                "policy": "DeriveFromCelQuery",
                "celQuery": 'has(object.status.conditions) && object.status.conditions.exists(c, c.type == "Available" && c.status == "True")',
            },
            "forProvider": {
                "manifest": {
                    "apiVersion": "leaderworkerset.x-k8s.io/v1",
                    "kind": "LeaderWorkerSet",
                    "metadata": {"name": "r-main-bb4e3", "namespace": "mp-ml-team-51733"},
                    "spec": {
                        "replicas": replicas,
                        "leaderWorkerTemplate": {
                            "size": size,
                            "leaderTemplate": {
                                "metadata": {
                                    "labels": {
                                        "modelplane.ai/engine": "main",
                                        "modelplane.ai/role": "Leader",
                                        "modelplane.ai/serving": "r",
                                        "modelplane.ai/lws-role": "leader",
                                    }
                                },
                                "spec": {
                                    "containers": leader_containers,
                                    "volumes": leader_volumes,
                                    "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                    "resourceClaims": [
                                        {
                                            "name": "devices",
                                            "resourceClaimTemplateName": "r-main-leader-devices-f58c6",
                                        }
                                    ],
                                    "tolerations": [
                                        {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
                                    ],
                                },
                            },
                            "workerTemplate": {
                                "metadata": {
                                    "labels": {"modelplane.ai/engine": "main", "modelplane.ai/role": "Worker"}
                                },
                                "spec": {
                                    "containers": worker_containers,
                                    "volumes": worker_volumes,
                                    "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                    "resourceClaims": [
                                        {
                                            "name": "devices",
                                            "resourceClaimTemplateName": "r-main-worker-devices-99b8a",
                                        }
                                    ],
                                    "tolerations": [
                                        {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
                                    ],
                                },
                            },
                        },
                    },
                }
            },
        }
    }


def _pod_clique_set(*, name: str, cliques: list[dict]) -> dict:
    """The Object composing a Leader/Worker engine's PodCliqueSet, from its leader and worker cliques."""
    return {
        "spec": {
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
            "readiness": {
                "policy": "DeriveFromCelQuery",
                "celQuery": "has(object.status) && has(object.status.observedGeneration) && object.status.observedGeneration == object.metadata.generation && object.spec.replicas > 0 && has(object.status.availableReplicas) && object.status.availableReplicas >= object.spec.replicas",
            },
            "forProvider": {
                "manifest": {
                    "apiVersion": "grove.io/v1alpha1",
                    "kind": "PodCliqueSet",
                    "metadata": {"name": name, "namespace": "mp-ml-team-51733"},
                    "spec": {
                        "replicas": 1,
                        "template": {
                            "cliqueStartupType": "CliqueStartupTypeExplicit",
                            "terminationDelay": "4h",
                            "headlessServiceConfig": {"publishNotReadyAddresses": True},
                            "cliques": cliques,
                            "podCliqueScalingGroups": [
                                {
                                    "name": "gang",
                                    "cliqueNames": ["leader", "worker"],
                                    "replicas": 1,
                                    # 1 whatever the copies, so a wedged gang
                                    # doesn't take the healthy ones down with it.
                                    "minAvailable": 1,
                                }
                            ],
                        },
                    },
                }
            },
        }
    }


def _claim_template(*, name: str, count: int) -> dict:
    """The Object composing a member's ResourceClaimTemplate, for count GPUs of at least 80Gi."""
    return {
        "spec": {
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
            "readiness": {"policy": "SuccessfulCreate"},
            "forProvider": {
                "manifest": {
                    "apiVersion": "resource.k8s.io/v1",
                    "kind": "ResourceClaimTemplate",
                    "metadata": {"name": name, "namespace": "mp-ml-team-51733"},
                    "spec": {
                        "spec": {
                            "devices": {
                                "requests": [
                                    {
                                        "name": "gpu",
                                        "exactly": {
                                            "deviceClassName": "gpu.nvidia.com",
                                            "count": count,
                                            "selectors": [
                                                {
                                                    "cel": {
                                                        "expression": 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                                    }
                                                }
                                            ],
                                        },
                                    }
                                ]
                            }
                        }
                    },
                }
            },
        }
    }


def _replica(
    *,
    name: str | None,
    namespace: str,
    cluster_name: str,
    labels: dict[str, str] | None,
    model_cache_ref: v1alpha1.ModelCacheRef | None,
    serving: v1alpha1.Serving | None,
    engines: list[v1alpha1.Engine],
) -> v1alpha1.ModelReplica:
    """A ModelReplica."""
    return v1alpha1.ModelReplica(
        metadata=metav1.ObjectMeta(name=name, namespace=namespace, labels=labels),
        spec=v1alpha1.SpecModel(
            clusterName=cluster_name,
            modelCacheRef=model_cache_ref,
            serving=serving,
            engines=engines,
        ),
    )


def _to_dicts(*, objects: dict[str, k8sobjv1alpha1.Object]) -> dict[str, dict]:
    """objects as dicts of only the fields the code set."""
    return {key: obj.model_dump(exclude_unset=True, by_alias=True) for key, obj in objects.items()}


def _sorted(*, objects: dict[str, dict]) -> dict[str, dict]:
    """objects with their keys sorted, so pytest's diff of two lines them up."""
    return json.loads(json.dumps(objects, sort_keys=True))


BUILD_CASES = [
    # A Deployment reports readiness from its Available condition, and a claim
    # template is ready once it's created. The engine and role labels are what
    # the collector attributes the pod's metrics to.
    BuildCase(
        name="StandaloneEngine",
        reason=(
            "A Standalone engine composes a Deployment, its pod labeled with the engine and its role, and a claim "
            "template for its member's device request."
        ),
        backend=native.NativeBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=Qwen/Qwen3-0.6B"],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Standard",
        want={
            "model-serving-main": _deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "main",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-main-standalone": _claim_template(name="r-main-standalone-devices-f456f", count=1),
        },
    ),
    # A sidecar takes 8000, so the engine declares its http port elsewhere, and
    # the probe and the collector follow it there. The sidecar is the user's and
    # passes through as written: no claim, env, mounts or probe.
    BuildCase(
        name="StandaloneSidecar",
        reason=(
            "A Standalone member with a container beside the engine composes both into the pod, probing the engine "
            "on the http port it declares and passing the other container through as written."
        ),
        backend=native.NativeBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="sidecar",
                                            image="ghcr.io/llm-d/llm-d-router-disagg-sidecar:v0.9.0",
                                            args=["--kv-connector=sglang", "--vllm-port=8001"],
                                            ports=[v1alpha1.Port(containerPort=8000)],
                                        ),
                                        v1alpha1.Container(
                                            name="engine",
                                            image="lmsysorg/sglang:latest",
                                            args=["--port=8001"],
                                            ports=[v1alpha1.Port(name="http", containerPort=8001)],
                                        ),
                                    ]
                                )
                            ),
                        )
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Standard",
        want={
            "model-serving-main": _deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "main",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "lmsysorg/sglang:latest",
                        "args": ["--port=8001"],
                        "ports": [{"name": "http", "containerPort": 8001}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8001},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    },
                    {
                        "name": "sidecar",
                        "image": "ghcr.io/llm-d/llm-d-router-disagg-sidecar:v0.9.0",
                        "args": ["--kv-connector=sglang", "--vllm-port=8001"],
                        "ports": [{"containerPort": 8000}],
                    },
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-main-standalone": _claim_template(name="r-main-standalone-devices-f456f", count=1),
        },
    ),
    # An unnamed port 8000 on the engine is its http port, not a second entry.
    BuildCase(
        name="StandaloneUnnamedEnginePort",
        reason=(
            "An engine that declares port 8000 without a name gets it named http rather than a second 8000 entry, "
            "and composes first even when another container is listed before it."
        ),
        backend=native.NativeBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(name="log-tail", image="busybox:1.36"),
                                        v1alpha1.Container(
                                            name="engine",
                                            image="lmsysorg/sglang:latest",
                                            ports=[v1alpha1.Port(containerPort=8000)],
                                        ),
                                    ]
                                )
                            ),
                        )
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Standard",
        want={
            "model-serving-main": _deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "main",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "lmsysorg/sglang:latest",
                        "args": [],
                        "ports": [{"containerPort": 8000, "name": "http"}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    },
                    {"name": "log-tail", "image": "busybox:1.36"},
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-main-standalone": _claim_template(name="r-main-standalone-devices-f456f", count=1),
        },
    ),
    # See #378.
    BuildCase(
        name="StandaloneMemberMetadata",
        reason=(
            "A Standalone member's template labels and annotations land on its Deployment's pod template, merged with "
            "the managed labels."
        ),
        backend=native.NativeBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                metadata=v1alpha1.Metadata(
                                    labels={"example.com/role": "standalone"},
                                    annotations={"example.com/config": "standalone"},
                                ),
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=Qwen/Qwen3-0.6B"],
                                        )
                                    ]
                                ),
                            ),
                        )
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Standard",
        want={
            "model-serving-main": _deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "example.com/role": "standalone",
                        "modelplane.ai/engine": "main",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    },
                    "annotations": {"example.com/config": "standalone"},
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-main-standalone": _claim_template(name="r-main-standalone-devices-f456f", count=1),
        },
    ),
    # Two replicas of one deployment on the same cluster, this one and
    # dep-clusterB, must compose distinct resource names there.
    BuildCase(
        name="CoLocatedReplicaA",
        reason="A replica named dep-clusterA qualifies its resource names with its own name.",
        backend=native.NativeBackend(),
        replica=_replica(
            name="dep-clusterA",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=Qwen/Qwen3-0.6B"],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="dep-clusterA",
        stack="Standard",
        want={
            "model-serving-main": _deployment(
                name="dep-clusterA-main-3d1d5",
                claim_template_name="dep-clusterA-main-standalone-devices-145eb",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "main",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "dep-clusterA",
                        "modelplane.ai/workload": "dep-clusterA-main-3d1d5",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-main-standalone": _claim_template(
                name="dep-clusterA-main-standalone-devices-145eb", count=1
            ),
        },
    ),
    BuildCase(
        name="CoLocatedReplicaB",
        reason=(
            "A replica named dep-clusterB qualifies its resource names with its own name, so they don't collide with "
            "dep-clusterA's."
        ),
        backend=native.NativeBackend(),
        replica=_replica(
            name="dep-clusterB",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=Qwen/Qwen3-0.6B"],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="dep-clusterB",
        stack="Standard",
        want={
            "model-serving-main": _deployment(
                name="dep-clusterB-main-d6c52",
                claim_template_name="dep-clusterB-main-standalone-devices-5a8a8",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "main",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "dep-clusterB",
                        "modelplane.ai/workload": "dep-clusterB-main-d6c52",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-main-standalone": _claim_template(
                name="dep-clusterB-main-standalone-devices-5a8a8", count=1
            ),
        },
    ),
    # Qualifying the names by engine keeps a multi-engine replica's from
    # colliding on the remote cluster.
    BuildCase(
        name="MultiEngineReplica",
        reason=(
            "A replica with two engines named prefill and decode composes a Deployment and a claim template for each, "
            "named for its engine."
        ),
        backend=native.NativeBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="prefill",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=Qwen/Qwen3-0.6B"],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                ),
                v1alpha1.Engine(
                    name="decode",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=Qwen/Qwen3-0.6B"],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                ),
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Standard",
        want={
            "model-serving-prefill": _deployment(
                name="r-prefill-d90b0",
                claim_template_name="r-prefill-standalone-devices-f62af",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "prefill",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-prefill-d90b0",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-prefill-standalone": _claim_template(name="r-prefill-standalone-devices-f62af", count=1),
            "model-serving-decode": _deployment(
                name="r-decode-4b27b",
                claim_template_name="r-decode-standalone-devices-63392",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "decode",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-decode-4b27b",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-decode-standalone": _claim_template(name="r-decode-standalone-devices-63392", count=1),
        },
    ),
    # resources.claims is a list-map keyed on name alone, so N device requests
    # mustn't compose N container claims all named "devices".
    BuildCase(
        name="SeveralDeviceRequests",
        reason=(
            "A member requesting GPUs and NICs composes one container claim on the pod's claim, and a claim template "
            "carrying both requests."
        ),
        backend=native.NativeBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(name="gpu", deviceClassName="gpu.nvidia.com", count=8),
                                v1alpha1.DeviceRequest(name="nic", deviceClassName="nic.nvidia.com", count=8),
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=Qwen/Qwen3-0.6B"],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Standard",
        want={
            "model-serving-main": _deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "main",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            # Written out because it's the only claim template that asks for
            # more than GPUs, which is all _claim_template builds.
            "resource-claim-main-standalone": {
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "resource.k8s.io/v1",
                            "kind": "ResourceClaimTemplate",
                            "metadata": {"name": "r-main-standalone-devices-f456f", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "spec": {
                                    "devices": {
                                        "requests": [
                                            {
                                                "name": "gpu",
                                                "exactly": {"deviceClassName": "gpu.nvidia.com", "count": 8},
                                            },
                                            {
                                                "name": "nic",
                                                "exactly": {"deviceClassName": "nic.nvidia.com", "count": 8},
                                            },
                                        ]
                                    }
                                }
                            },
                        }
                    },
                }
            },
        },
    ),
    # HF_HUB_CACHE makes the engine's own --model=<repo> resolve against the
    # cache. Modelplane injects no --model of its own: naming the model is the
    # command's job.
    #
    # HF_HUB_CACHE isn't part of the ModelExpress bundle, and applies on every
    # stack.
    BuildCase(
        name="StandaloneCacheOnStandard",
        reason=(
            "A cached Standalone engine on Standard mounts the cache's PVC and points HF_HUB_CACHE at it, with no "
            "ModelExpress env or security context."
        ),
        backend=native.NativeBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=v1alpha1.ModelCacheRef(name="qwen"),
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest", args=[])
                                    ]
                                )
                            ),
                        )
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Standard",
        want={
            "model-serving-main": _deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "main",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": [],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [
                            {"name": "dshm", "mountPath": "/dev/shm"},
                            {"name": "model-cache", "mountPath": "/mnt/models"},
                        ],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                        "env": [{"name": "HF_HUB_CACHE", "value": "/mnt/models"}],
                    }
                ],
                volumes=[
                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                    {
                        "name": "model-cache",
                        "persistentVolumeClaim": {"claimName": "modelcache-ml-team-qwen-17db2"},
                    },
                ],
            ),
            "resource-claim-main-standalone": _claim_template(name="r-main-standalone-devices-f456f", count=1),
        },
    ),
    # Kubernetes expands $(VAR) left to right, so Modelplane's own entries must
    # precede the user's for a user entry to reference them.
    BuildCase(
        name="StandaloneEnvWithCache",
        reason="A cached Standalone member's own env follows the cache env in its container.",
        backend=native.NativeBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=v1alpha1.ModelCacheRef(name="qwen"),
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=[],
                                            env=[v1alpha1.EnvItem(name="HF_TOKEN", value="x")],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Standard",
        want={
            "model-serving-main": _deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "main",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": [],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [
                            {"name": "dshm", "mountPath": "/dev/shm"},
                            {"name": "model-cache", "mountPath": "/mnt/models"},
                        ],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                        "env": [
                            {"name": "HF_HUB_CACHE", "value": "/mnt/models"},
                            {"name": "HF_TOKEN", "value": "x"},
                        ],
                    }
                ],
                volumes=[
                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                    {
                        "name": "model-cache",
                        "persistentVolumeClaim": {"claimName": "modelcache-ml-team-qwen-17db2"},
                    },
                ],
            ),
            "resource-claim-main-standalone": _claim_template(name="r-main-standalone-devices-f456f", count=1),
        },
    ),
    # A Standalone engine on a Dynamo cluster with a cache is as valid a P2P
    # peer set as a gang. MX_SERVER_ADDRESS is the per-cluster shared server's
    # well-known Service, qualified by its namespace because the engine runs in
    # its team's. MX_MODEL_REVISION isolates this cache's P2P source identity,
    # qualified by the Modelplane namespace like the cache's PVC name, so two
    # namespaces' caches of the same name can't collide at the cluster's one
    # shared server.
    #
    # HF_HUB_CACHE appears once. It's the cache's own env, and ModelExpress
    # reads it only as a fallback for its cache root.
    BuildCase(
        name="StandaloneCacheOnDynamo",
        reason=(
            "A cached Standalone engine on Dynamo gets the ModelExpress env and the IPC_LOCK security context "
            "alongside the cache env."
        ),
        backend=native.NativeBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=v1alpha1.ModelCacheRef(name="qwen"),
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest", args=[])
                                    ]
                                )
                            ),
                        )
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Dynamo",
        want={
            "model-serving-main": _deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "main",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": [],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [
                            {"name": "dshm", "mountPath": "/dev/shm"},
                            {"name": "model-cache", "mountPath": "/mnt/models"},
                        ],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                        "env": [
                            {"name": "HF_HUB_CACHE", "value": "/mnt/models"},
                            {
                                "name": "MX_SERVER_ADDRESS",
                                "value": "modelexpress-server.default.svc:8001",
                            },
                            {
                                "name": "MODEL_EXPRESS_URL",
                                "value": "modelexpress-server.default.svc:8001",
                            },
                            {
                                "name": "MX_MODEL_REVISION",
                                "value": "modelcache-ml-team-qwen-17db2",
                            },
                            {"name": "MX_P2P_METADATA", "value": "1"},
                            {
                                "name": "POD_NAME",
                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}},
                            },
                            {
                                "name": "POD_UID",
                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
                            },
                            {
                                "name": "POD_NAMESPACE",
                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
                            },
                        ],
                        "securityContext": {"capabilities": {"add": ["IPC_LOCK"]}},
                    }
                ],
                volumes=[
                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                    {
                        "name": "model-cache",
                        "persistentVolumeClaim": {"claimName": "modelcache-ml-team-qwen-17db2"},
                    },
                ],
            ),
            "resource-claim-main-standalone": _claim_template(name="r-main-standalone-devices-f456f", count=1),
        },
    ),
    # There's no flag injection or bootstrap. The worker addresses the leader
    # through $(MODELPLANE_LEADER_ADDRESS), which concatenates Grove's PCSG vars
    # because they vary per gang. The PCS-scoped ones are identical across
    # gangs, and would point every copy at gang 0's leader. There's no
    # MODELPLANE_RANK: Grove exposes no group-wide pod index yet (grove#755), so
    # a gang engine's command computes its own rank from GROVE_PCLQ_POD_INDEX.
    #
    # A PodCliqueSet publishes no Available condition, so its readiness derives
    # from its replica counters. A worker with no template metadata carries only
    # the managed labels, and with no cache there's no ModelExpress env or
    # security context.
    BuildCase(
        name="GroveGang",
        reason=(
            "A Leader/Worker engine on Grove composes a PodCliqueSet with its members' commands verbatim, and a claim "
            "template for each member."
        ),
        backend=grove.GroveBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Leader",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                                            ],
                                        )
                                    ]
                                )
                            ),
                        ),
                        v1alpha1.Member(
                            role="Worker",
                            nodePoolName="frontier",
                            worker=v1alpha1.Worker(nodes=1),
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                                            ],
                                        )
                                    ]
                                )
                            ),
                        ),
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Dynamo",
        want={
            "model-serving-main": _pod_clique_set(
                name="r-main-bb4e3",
                cliques=[
                    {
                        "name": "leader",
                        "labels": {
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Leader",
                            "modelplane.ai/serving": "r",
                            "kai.scheduler/queue": "modelplane",
                            "modelplane.ai/clique-role": "leader",
                        },
                        "spec": {
                            "roleName": "leader",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                                        ],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            }
                                        ],
                                        "ports": [{"name": "http", "containerPort": 8000}],
                                        "readinessProbe": {
                                            "httpGet": {"path": "/health", "port": 8000},
                                            "initialDelaySeconds": 30,
                                            "periodSeconds": 10,
                                            "timeoutSeconds": 5,
                                        },
                                    }
                                ],
                                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-main-leader-devices-f58c6",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                    {
                        "name": "worker",
                        "labels": {
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Worker",
                            "kai.scheduler/queue": "modelplane",
                        },
                        "spec": {
                            "roleName": "worker",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                                        ],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            }
                                        ],
                                    }
                                ],
                                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-main-worker-devices-99b8a",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                ],
            ),
            "resource-claim-main-leader": _claim_template(name="r-main-leader-devices-f58c6", count=8),
            "resource-claim-main-worker": _claim_template(name="r-main-worker-devices-99b8a", count=8),
        },
    ),
    # A Grove leader behind a sidecar, and a worker carrying its own ports and
    # an extra container.
    BuildCase(
        name="GroveSidecars",
        reason=(
            "On Grove, a leader behind a sidecar is probed on the engine's http port, and a worker's engine ports and "
            "extra container pass through as written."
        ),
        backend=grove.GroveBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Leader",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8001",
                                            ],
                                            ports=[v1alpha1.Port(name="http", containerPort=8001)],
                                        ),
                                        v1alpha1.Container(
                                            name="proxy",
                                            image="example.com/proxy:v1",
                                            ports=[v1alpha1.Port(containerPort=8000)],
                                        ),
                                    ]
                                )
                            ),
                        ),
                        v1alpha1.Member(
                            role="Worker",
                            nodePoolName="frontier",
                            worker=v1alpha1.Worker(nodes=1),
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                                            ],
                                            ports=[v1alpha1.Port(name="nixl", containerPort=5557)],
                                        ),
                                        v1alpha1.Container(name="log-tail", image="busybox:1.36"),
                                    ]
                                )
                            ),
                        ),
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Dynamo",
        want={
            "model-serving-main": _pod_clique_set(
                name="r-main-bb4e3",
                cliques=[
                    {
                        "name": "leader",
                        "labels": {
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Leader",
                            "modelplane.ai/serving": "r",
                            "kai.scheduler/queue": "modelplane",
                            "modelplane.ai/clique-role": "leader",
                        },
                        "spec": {
                            "roleName": "leader",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8001",
                                        ],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            }
                                        ],
                                        "ports": [{"name": "http", "containerPort": 8001}],
                                        "readinessProbe": {
                                            "httpGet": {"path": "/health", "port": 8001},
                                            "initialDelaySeconds": 30,
                                            "periodSeconds": 10,
                                            "timeoutSeconds": 5,
                                        },
                                    },
                                    {
                                        "name": "proxy",
                                        "image": "example.com/proxy:v1",
                                        "ports": [{"containerPort": 8000}],
                                    },
                                ],
                                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-main-leader-devices-f58c6",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                    {
                        "name": "worker",
                        "labels": {
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Worker",
                            "kai.scheduler/queue": "modelplane",
                        },
                        "spec": {
                            "roleName": "worker",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                                        ],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            }
                                        ],
                                        "ports": [{"name": "nixl", "containerPort": 5557}],
                                    },
                                    {"name": "log-tail", "image": "busybox:1.36"},
                                ],
                                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-main-worker-devices-99b8a",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                ],
            ),
            "resource-claim-main-leader": _claim_template(name="r-main-leader-devices-f58c6", count=8),
            "resource-claim-main-worker": _claim_template(name="r-main-worker-devices-99b8a", count=8),
        },
    ),
    BuildCase(
        name="GroveMemberEnv",
        reason="A Grove leader's own env follows the leader address alias in its container.",
        backend=grove.GroveBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Leader",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                                            ],
                                            env=[v1alpha1.EnvItem(name="HF_TOKEN", value="x")],
                                        )
                                    ]
                                )
                            ),
                        ),
                        v1alpha1.Member(
                            role="Worker",
                            nodePoolName="frontier",
                            worker=v1alpha1.Worker(nodes=1),
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                                            ],
                                        )
                                    ]
                                )
                            ),
                        ),
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Dynamo",
        want={
            "model-serving-main": _pod_clique_set(
                name="r-main-bb4e3",
                cliques=[
                    {
                        "name": "leader",
                        "labels": {
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Leader",
                            "modelplane.ai/serving": "r",
                            "kai.scheduler/queue": "modelplane",
                            "modelplane.ai/clique-role": "leader",
                        },
                        "spec": {
                            "roleName": "leader",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                                        ],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            },
                                            {"name": "HF_TOKEN", "value": "x"},
                                        ],
                                        "ports": [{"name": "http", "containerPort": 8000}],
                                        "readinessProbe": {
                                            "httpGet": {"path": "/health", "port": 8000},
                                            "initialDelaySeconds": 30,
                                            "periodSeconds": 10,
                                            "timeoutSeconds": 5,
                                        },
                                    }
                                ],
                                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-main-leader-devices-f58c6",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                    {
                        "name": "worker",
                        "labels": {
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Worker",
                            "kai.scheduler/queue": "modelplane",
                        },
                        "spec": {
                            "roleName": "worker",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                                        ],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            }
                                        ],
                                    }
                                ],
                                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-main-worker-devices-99b8a",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                ],
            ),
            "resource-claim-main-leader": _claim_template(name="r-main-leader-devices-f58c6", count=8),
            "resource-claim-main-worker": _claim_template(name="r-main-worker-devices-99b8a", count=8),
        },
    ),
    # Multi-NIC RDMA nodes need VLLM_HOST_IP from status.podIP so the engine
    # binds the right interface (#141).
    BuildCase(
        name="GroveFieldRefEnv",
        reason="A Grove leader's env drawn from a pod field passes through unchanged.",
        backend=grove.GroveBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Leader",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                                            ],
                                            env=[
                                                v1alpha1.EnvItem(
                                                    name="VLLM_HOST_IP",
                                                    valueFrom=v1alpha1.ValueFrom(
                                                        fieldRef=v1alpha1.FieldRef(fieldPath="status.podIP")
                                                    ),
                                                )
                                            ],
                                        )
                                    ]
                                )
                            ),
                        ),
                        v1alpha1.Member(
                            role="Worker",
                            nodePoolName="frontier",
                            worker=v1alpha1.Worker(nodes=1),
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                                            ],
                                        )
                                    ]
                                )
                            ),
                        ),
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Dynamo",
        want={
            "model-serving-main": _pod_clique_set(
                name="r-main-bb4e3",
                cliques=[
                    {
                        "name": "leader",
                        "labels": {
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Leader",
                            "modelplane.ai/serving": "r",
                            "kai.scheduler/queue": "modelplane",
                            "modelplane.ai/clique-role": "leader",
                        },
                        "spec": {
                            "roleName": "leader",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                                        ],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            },
                                            {
                                                "name": "VLLM_HOST_IP",
                                                "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}},
                                            },
                                        ],
                                        "ports": [{"name": "http", "containerPort": 8000}],
                                        "readinessProbe": {
                                            "httpGet": {"path": "/health", "port": 8000},
                                            "initialDelaySeconds": 30,
                                            "periodSeconds": 10,
                                            "timeoutSeconds": 5,
                                        },
                                    }
                                ],
                                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-main-leader-devices-f58c6",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                    {
                        "name": "worker",
                        "labels": {
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Worker",
                            "kai.scheduler/queue": "modelplane",
                        },
                        "spec": {
                            "roleName": "worker",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                                        ],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            }
                                        ],
                                    }
                                ],
                                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-main-worker-devices-99b8a",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                ],
            ),
            "resource-claim-main-leader": _claim_template(name="r-main-leader-devices-f58c6", count=8),
            "resource-claim-main-worker": _claim_template(name="r-main-worker-devices-99b8a", count=8),
        },
    ),
    # Grove propagates a clique's labels and annotations to its pods (#378).
    BuildCase(
        name="GroveMemberMetadata",
        reason=(
            "Each Grove member's template labels and annotations land on its own clique alone, merged with the managed "
            "labels."
        ),
        backend=grove.GroveBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Leader",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                metadata=v1alpha1.Metadata(
                                    labels={"example.com/role": "leader"},
                                    annotations={"example.com/config": "leader"},
                                ),
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                                            ],
                                        )
                                    ]
                                ),
                            ),
                        ),
                        v1alpha1.Member(
                            role="Worker",
                            nodePoolName="frontier",
                            worker=v1alpha1.Worker(nodes=1),
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                metadata=v1alpha1.Metadata(
                                    labels={"example.com/role": "worker"},
                                    annotations={"example.com/config": "worker"},
                                ),
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                                            ],
                                        )
                                    ]
                                ),
                            ),
                        ),
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Dynamo",
        want={
            "model-serving-main": _pod_clique_set(
                name="r-main-bb4e3",
                cliques=[
                    {
                        "name": "leader",
                        "labels": {
                            "example.com/role": "leader",
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Leader",
                            "modelplane.ai/serving": "r",
                            "kai.scheduler/queue": "modelplane",
                            "modelplane.ai/clique-role": "leader",
                        },
                        "annotations": {"example.com/config": "leader"},
                        "spec": {
                            "roleName": "leader",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                                        ],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            }
                                        ],
                                        "ports": [{"name": "http", "containerPort": 8000}],
                                        "readinessProbe": {
                                            "httpGet": {"path": "/health", "port": 8000},
                                            "initialDelaySeconds": 30,
                                            "periodSeconds": 10,
                                            "timeoutSeconds": 5,
                                        },
                                    }
                                ],
                                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-main-leader-devices-f58c6",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                    {
                        "name": "worker",
                        "labels": {
                            "example.com/role": "worker",
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Worker",
                            "kai.scheduler/queue": "modelplane",
                        },
                        "annotations": {"example.com/config": "worker"},
                        "spec": {
                            "roleName": "worker",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                                        ],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            }
                                        ],
                                    }
                                ],
                                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-main-worker-devices-99b8a",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                ],
            ),
            "resource-claim-main-leader": _claim_template(name="r-main-leader-devices-f58c6", count=8),
            "resource-claim-main-worker": _claim_template(name="r-main-worker-devices-99b8a", count=8),
        },
    ),
    # A coordinator-only leader, like a vLLM DP head running
    # --data-parallel-size-local=0, has no deviceRequests.
    BuildCase(
        name="ClaimlessGroveLeader",
        reason=(
            "A Grove leader with no device requests composes no claim or claim template, but still pins to its pool "
            "and tolerates the GPU taint."
        ),
        backend=grove.GroveBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Leader",
                            nodePoolName="frontier",
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                                            ],
                                        )
                                    ]
                                )
                            ),
                        ),
                        v1alpha1.Member(
                            role="Worker",
                            nodePoolName="frontier",
                            worker=v1alpha1.Worker(nodes=1),
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                                            ],
                                        )
                                    ]
                                )
                            ),
                        ),
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Dynamo",
        want={
            "model-serving-main": _pod_clique_set(
                name="r-main-bb4e3",
                cliques=[
                    {
                        "name": "leader",
                        "labels": {
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Leader",
                            "modelplane.ai/serving": "r",
                            "kai.scheduler/queue": "modelplane",
                            "modelplane.ai/clique-role": "leader",
                        },
                        "spec": {
                            "roleName": "leader",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                                        ],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            }
                                        ],
                                        "ports": [{"name": "http", "containerPort": 8000}],
                                        "readinessProbe": {
                                            "httpGet": {"path": "/health", "port": 8000},
                                            "initialDelaySeconds": 30,
                                            "periodSeconds": 10,
                                            "timeoutSeconds": 5,
                                        },
                                    }
                                ],
                                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                    {
                        "name": "worker",
                        "labels": {
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Worker",
                            "kai.scheduler/queue": "modelplane",
                        },
                        "spec": {
                            "roleName": "worker",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                                        ],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            }
                                        ],
                                    }
                                ],
                                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-main-worker-devices-99b8a",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                ],
            ),
            "resource-claim-main-worker": _claim_template(name="r-main-worker-devices-99b8a", count=8),
        },
    ),
    # The scheduler may split a gang across pools when no single pool satisfies
    # every member, so each member's pods pin to that member's pool rather than
    # an engine-wide one.
    BuildCase(
        name="GroveMembersSplitPools",
        reason="A Grove leader on the head pool and worker on the frontier pool each pin to their own pool.",
        backend=grove.GroveBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Leader",
                            nodePoolName="head",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                                            ],
                                        )
                                    ]
                                )
                            ),
                        ),
                        v1alpha1.Member(
                            role="Worker",
                            nodePoolName="frontier",
                            worker=v1alpha1.Worker(nodes=1),
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                                            ],
                                        )
                                    ]
                                )
                            ),
                        ),
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Dynamo",
        want={
            "model-serving-main": _pod_clique_set(
                name="r-main-bb4e3",
                cliques=[
                    {
                        "name": "leader",
                        "labels": {
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Leader",
                            "modelplane.ai/serving": "r",
                            "kai.scheduler/queue": "modelplane",
                            "modelplane.ai/clique-role": "leader",
                        },
                        "spec": {
                            "roleName": "leader",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                                        ],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            }
                                        ],
                                        "ports": [{"name": "http", "containerPort": 8000}],
                                        "readinessProbe": {
                                            "httpGet": {"path": "/health", "port": 8000},
                                            "initialDelaySeconds": 30,
                                            "periodSeconds": 10,
                                            "timeoutSeconds": 5,
                                        },
                                    }
                                ],
                                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "head"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-main-leader-devices-f58c6",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                    {
                        "name": "worker",
                        "labels": {
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Worker",
                            "kai.scheduler/queue": "modelplane",
                        },
                        "spec": {
                            "roleName": "worker",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                                        ],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            }
                                        ],
                                    }
                                ],
                                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-main-worker-devices-99b8a",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                ],
            ),
            "resource-claim-main-leader": _claim_template(name="r-main-leader-devices-f58c6", count=8),
            "resource-claim-main-worker": _claim_template(name="r-main-worker-devices-99b8a", count=8),
        },
    ),
    # Pointing HF_HUB_CACHE at the mount lets each clique's own --model=<repo>
    # resolve against it.
    BuildCase(
        name="GroveCacheBareLeader",
        reason=(
            "A cached Grove gang whose leader has no command or args mounts the cache on both cliques and points "
            "HF_HUB_CACHE at it, injecting no --model."
        ),
        backend=grove.GroveBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=v1alpha1.ModelCacheRef(name="kimi"),
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Leader",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest", args=[])
                                    ]
                                )
                            ),
                        ),
                        v1alpha1.Member(
                            role="Worker",
                            nodePoolName="frontier",
                            worker=v1alpha1.Worker(nodes=1),
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=["/bin/sh", "-c", "join"],
                                        )
                                    ]
                                )
                            ),
                        ),
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Dynamo",
        want={
            "model-serving-main": _pod_clique_set(
                name="r-main-bb4e3",
                cliques=[
                    {
                        "name": "leader",
                        "labels": {
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Leader",
                            "modelplane.ai/serving": "r",
                            "kai.scheduler/queue": "modelplane",
                            "modelplane.ai/clique-role": "leader",
                        },
                        "spec": {
                            "roleName": "leader",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [
                                            {"name": "dshm", "mountPath": "/dev/shm"},
                                            {"name": "model-cache", "mountPath": "/mnt/models"},
                                        ],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            },
                                            {"name": "HF_HUB_CACHE", "value": "/mnt/models"},
                                            {
                                                "name": "MX_SERVER_ADDRESS",
                                                "value": "modelexpress-server.default.svc:8001",
                                            },
                                            {
                                                "name": "MODEL_EXPRESS_URL",
                                                "value": "modelexpress-server.default.svc:8001",
                                            },
                                            {
                                                "name": "MX_MODEL_REVISION",
                                                "value": "modelcache-ml-team-kimi-aa322",
                                            },
                                            {"name": "MX_P2P_METADATA", "value": "1"},
                                            {
                                                "name": "POD_NAME",
                                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}},
                                            },
                                            {
                                                "name": "POD_UID",
                                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
                                            },
                                            {
                                                "name": "POD_NAMESPACE",
                                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
                                            },
                                        ],
                                        "securityContext": {"capabilities": {"add": ["IPC_LOCK"]}},
                                        "ports": [{"name": "http", "containerPort": 8000}],
                                        "readinessProbe": {
                                            "httpGet": {"path": "/health", "port": 8000},
                                            "initialDelaySeconds": 30,
                                            "periodSeconds": 10,
                                            "timeoutSeconds": 5,
                                        },
                                    }
                                ],
                                "volumes": [
                                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                                    {
                                        "name": "model-cache",
                                        "persistentVolumeClaim": {"claimName": "modelcache-ml-team-kimi-aa322"},
                                    },
                                ],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-main-leader-devices-f58c6",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                    {
                        "name": "worker",
                        "labels": {
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Worker",
                            "kai.scheduler/queue": "modelplane",
                        },
                        "spec": {
                            "roleName": "worker",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [
                                            {"name": "dshm", "mountPath": "/dev/shm"},
                                            {"name": "model-cache", "mountPath": "/mnt/models"},
                                        ],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": ["/bin/sh", "-c", "join"],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            },
                                            {"name": "HF_HUB_CACHE", "value": "/mnt/models"},
                                            {
                                                "name": "MX_SERVER_ADDRESS",
                                                "value": "modelexpress-server.default.svc:8001",
                                            },
                                            {
                                                "name": "MODEL_EXPRESS_URL",
                                                "value": "modelexpress-server.default.svc:8001",
                                            },
                                            {
                                                "name": "MX_MODEL_REVISION",
                                                "value": "modelcache-ml-team-kimi-aa322",
                                            },
                                            {"name": "MX_P2P_METADATA", "value": "1"},
                                            {
                                                "name": "POD_NAME",
                                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}},
                                            },
                                            {
                                                "name": "POD_UID",
                                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
                                            },
                                            {
                                                "name": "POD_NAMESPACE",
                                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
                                            },
                                        ],
                                        "securityContext": {"capabilities": {"add": ["IPC_LOCK"]}},
                                    }
                                ],
                                "volumes": [
                                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                                    {
                                        "name": "model-cache",
                                        "persistentVolumeClaim": {"claimName": "modelcache-ml-team-kimi-aa322"},
                                    },
                                ],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-main-worker-devices-99b8a",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                ],
            ),
            "resource-claim-main-leader": _claim_template(name="r-main-leader-devices-f58c6", count=8),
            "resource-claim-main-worker": _claim_template(name="r-main-worker-devices-99b8a", count=8),
        },
    ),
    BuildCase(
        name="GroveCacheLeaderCommand",
        reason=(
            "A cached Grove gang whose leader has its own command mounts the cache, keeping the command verbatim with "
            "no --model injected."
        ),
        backend=grove.GroveBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=v1alpha1.ModelCacheRef(name="kimi"),
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Leader",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "python3 -m sglang.launch_server --model-path /mnt/models --tp 16",
                                            ],
                                        )
                                    ]
                                )
                            ),
                        ),
                        v1alpha1.Member(
                            role="Worker",
                            nodePoolName="frontier",
                            worker=v1alpha1.Worker(nodes=1),
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=["/bin/sh", "-c", "join"],
                                        )
                                    ]
                                )
                            ),
                        ),
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Dynamo",
        want={
            "model-serving-main": _pod_clique_set(
                name="r-main-bb4e3",
                cliques=[
                    {
                        "name": "leader",
                        "labels": {
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Leader",
                            "modelplane.ai/serving": "r",
                            "kai.scheduler/queue": "modelplane",
                            "modelplane.ai/clique-role": "leader",
                        },
                        "spec": {
                            "roleName": "leader",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [
                                            {"name": "dshm", "mountPath": "/dev/shm"},
                                            {"name": "model-cache", "mountPath": "/mnt/models"},
                                        ],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "python3 -m sglang.launch_server --model-path /mnt/models --tp 16",
                                        ],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            },
                                            {"name": "HF_HUB_CACHE", "value": "/mnt/models"},
                                            {
                                                "name": "MX_SERVER_ADDRESS",
                                                "value": "modelexpress-server.default.svc:8001",
                                            },
                                            {
                                                "name": "MODEL_EXPRESS_URL",
                                                "value": "modelexpress-server.default.svc:8001",
                                            },
                                            {
                                                "name": "MX_MODEL_REVISION",
                                                "value": "modelcache-ml-team-kimi-aa322",
                                            },
                                            {"name": "MX_P2P_METADATA", "value": "1"},
                                            {
                                                "name": "POD_NAME",
                                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}},
                                            },
                                            {
                                                "name": "POD_UID",
                                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
                                            },
                                            {
                                                "name": "POD_NAMESPACE",
                                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
                                            },
                                        ],
                                        "securityContext": {"capabilities": {"add": ["IPC_LOCK"]}},
                                        "ports": [{"name": "http", "containerPort": 8000}],
                                        "readinessProbe": {
                                            "httpGet": {"path": "/health", "port": 8000},
                                            "initialDelaySeconds": 30,
                                            "periodSeconds": 10,
                                            "timeoutSeconds": 5,
                                        },
                                    }
                                ],
                                "volumes": [
                                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                                    {
                                        "name": "model-cache",
                                        "persistentVolumeClaim": {"claimName": "modelcache-ml-team-kimi-aa322"},
                                    },
                                ],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-main-leader-devices-f58c6",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                    {
                        "name": "worker",
                        "labels": {
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Worker",
                            "kai.scheduler/queue": "modelplane",
                        },
                        "spec": {
                            "roleName": "worker",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [
                                            {"name": "dshm", "mountPath": "/dev/shm"},
                                            {"name": "model-cache", "mountPath": "/mnt/models"},
                                        ],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": ["/bin/sh", "-c", "join"],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            },
                                            {"name": "HF_HUB_CACHE", "value": "/mnt/models"},
                                            {
                                                "name": "MX_SERVER_ADDRESS",
                                                "value": "modelexpress-server.default.svc:8001",
                                            },
                                            {
                                                "name": "MODEL_EXPRESS_URL",
                                                "value": "modelexpress-server.default.svc:8001",
                                            },
                                            {
                                                "name": "MX_MODEL_REVISION",
                                                "value": "modelcache-ml-team-kimi-aa322",
                                            },
                                            {"name": "MX_P2P_METADATA", "value": "1"},
                                            {
                                                "name": "POD_NAME",
                                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}},
                                            },
                                            {
                                                "name": "POD_UID",
                                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
                                            },
                                            {
                                                "name": "POD_NAMESPACE",
                                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
                                            },
                                        ],
                                        "securityContext": {"capabilities": {"add": ["IPC_LOCK"]}},
                                    }
                                ],
                                "volumes": [
                                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                                    {
                                        "name": "model-cache",
                                        "persistentVolumeClaim": {"claimName": "modelcache-ml-team-kimi-aa322"},
                                    },
                                ],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-main-worker-devices-99b8a",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                ],
            ),
            "resource-claim-main-leader": _claim_template(name="r-main-leader-devices-f58c6", count=8),
            "resource-claim-main-worker": _claim_template(name="r-main-worker-devices-99b8a", count=8),
        },
    ),
    # Each pod publishes itself as a source independently, so the worker needs
    # the ModelExpress env too.
    BuildCase(
        name="GroveModelExpressEnv",
        reason=(
            "A cached Grove gang on Dynamo gets the ModelExpress env on both cliques, after the leader address alias "
            "and the cache env."
        ),
        backend=grove.GroveBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=v1alpha1.ModelCacheRef(name="qwen"),
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Leader",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                                            ],
                                        )
                                    ]
                                )
                            ),
                        ),
                        v1alpha1.Member(
                            role="Worker",
                            nodePoolName="frontier",
                            worker=v1alpha1.Worker(nodes=1),
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                                            ],
                                        )
                                    ]
                                )
                            ),
                        ),
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Dynamo",
        want={
            "model-serving-main": _pod_clique_set(
                name="r-main-bb4e3",
                cliques=[
                    {
                        "name": "leader",
                        "labels": {
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Leader",
                            "modelplane.ai/serving": "r",
                            "kai.scheduler/queue": "modelplane",
                            "modelplane.ai/clique-role": "leader",
                        },
                        "spec": {
                            "roleName": "leader",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [
                                            {"name": "dshm", "mountPath": "/dev/shm"},
                                            {"name": "model-cache", "mountPath": "/mnt/models"},
                                        ],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                                        ],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            },
                                            {"name": "HF_HUB_CACHE", "value": "/mnt/models"},
                                            {
                                                "name": "MX_SERVER_ADDRESS",
                                                "value": "modelexpress-server.default.svc:8001",
                                            },
                                            {
                                                "name": "MODEL_EXPRESS_URL",
                                                "value": "modelexpress-server.default.svc:8001",
                                            },
                                            {
                                                "name": "MX_MODEL_REVISION",
                                                "value": "modelcache-ml-team-qwen-17db2",
                                            },
                                            {"name": "MX_P2P_METADATA", "value": "1"},
                                            {
                                                "name": "POD_NAME",
                                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}},
                                            },
                                            {
                                                "name": "POD_UID",
                                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
                                            },
                                            {
                                                "name": "POD_NAMESPACE",
                                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
                                            },
                                        ],
                                        "securityContext": {"capabilities": {"add": ["IPC_LOCK"]}},
                                        "ports": [{"name": "http", "containerPort": 8000}],
                                        "readinessProbe": {
                                            "httpGet": {"path": "/health", "port": 8000},
                                            "initialDelaySeconds": 30,
                                            "periodSeconds": 10,
                                            "timeoutSeconds": 5,
                                        },
                                    }
                                ],
                                "volumes": [
                                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                                    {
                                        "name": "model-cache",
                                        "persistentVolumeClaim": {"claimName": "modelcache-ml-team-qwen-17db2"},
                                    },
                                ],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-main-leader-devices-f58c6",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                    {
                        "name": "worker",
                        "labels": {
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Worker",
                            "kai.scheduler/queue": "modelplane",
                        },
                        "spec": {
                            "roleName": "worker",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [
                                            {"name": "dshm", "mountPath": "/dev/shm"},
                                            {"name": "model-cache", "mountPath": "/mnt/models"},
                                        ],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                                        ],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            },
                                            {"name": "HF_HUB_CACHE", "value": "/mnt/models"},
                                            {
                                                "name": "MX_SERVER_ADDRESS",
                                                "value": "modelexpress-server.default.svc:8001",
                                            },
                                            {
                                                "name": "MODEL_EXPRESS_URL",
                                                "value": "modelexpress-server.default.svc:8001",
                                            },
                                            {
                                                "name": "MX_MODEL_REVISION",
                                                "value": "modelcache-ml-team-qwen-17db2",
                                            },
                                            {"name": "MX_P2P_METADATA", "value": "1"},
                                            {
                                                "name": "POD_NAME",
                                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}},
                                            },
                                            {
                                                "name": "POD_UID",
                                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
                                            },
                                            {
                                                "name": "POD_NAMESPACE",
                                                "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
                                            },
                                        ],
                                        "securityContext": {"capabilities": {"add": ["IPC_LOCK"]}},
                                    }
                                ],
                                "volumes": [
                                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                                    {
                                        "name": "model-cache",
                                        "persistentVolumeClaim": {"claimName": "modelcache-ml-team-qwen-17db2"},
                                    },
                                ],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-main-worker-devices-99b8a",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                ],
            ),
            "resource-claim-main-leader": _claim_template(name="r-main-leader-devices-f58c6", count=8),
            "resource-claim-main-worker": _claim_template(name="r-main-worker-devices-99b8a", count=8),
        },
    ),
    # The worker followers never serve, so without the serving label the
    # replica's Service can't route to them. They do carry the telemetry
    # identity: a worker holds GPUs, and its metrics are the deployment's. Every
    # gang container leads with the backend-neutral coordination vars aliasing
    # LWS_LEADER_ADDRESS and LWS_WORKER_INDEX. A LeaderWorkerSet reports
    # readiness from its Available condition.
    BuildCase(
        name="LLMDGang",
        reason=(
            "A Leader/Worker engine on llm-d composes a LeaderWorkerSet whose leader alone carries the serving label, "
            "and whose worker carries only the engine and its role."
        ),
        backend=llmd.LLMDBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Leader",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")]
                                )
                            ),
                        ),
                        v1alpha1.Member(
                            role="Worker",
                            nodePoolName="frontier",
                            worker=v1alpha1.Worker(nodes=1),
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")]
                                )
                            ),
                        ),
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Standard",
        want={
            "model-serving-main": _leader_worker_set(
                replicas=1,
                size=2,
                leader_containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "resources": {"claims": [{"name": "devices"}]},
                        "env": [
                            {
                                "name": "MODELPLANE_LEADER_ADDRESS",
                                "value": "$(LWS_LEADER_ADDRESS)",
                            },
                            {"name": "MODELPLANE_RANK", "value": "$(LWS_WORKER_INDEX)"},
                        ],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                    }
                ],
                leader_volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                worker_containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "resources": {"claims": [{"name": "devices"}]},
                        "env": [
                            {
                                "name": "MODELPLANE_LEADER_ADDRESS",
                                "value": "$(LWS_LEADER_ADDRESS)",
                            },
                            {"name": "MODELPLANE_RANK", "value": "$(LWS_WORKER_INDEX)"},
                        ],
                    }
                ],
                worker_volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-main-leader": _claim_template(name="r-main-leader-devices-f58c6", count=8),
            "resource-claim-main-worker": _claim_template(name="r-main-worker-devices-99b8a", count=8),
        },
    ),
    # Each member has its own template, so a container the user adds to the
    # Leader runs only in the leader pod.
    BuildCase(
        name="LLMDLeaderSidecar",
        reason="A container beside the engine in a Leader's template runs in the leader pod and not in the workers.",
        backend=llmd.LLMDBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Leader",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            ports=[v1alpha1.Port(name="http", containerPort=8001)],
                                        ),
                                        v1alpha1.Container(name="proxy", image="example.com/proxy:v1"),
                                    ]
                                )
                            ),
                        ),
                        v1alpha1.Member(
                            role="Worker",
                            nodePoolName="frontier",
                            worker=v1alpha1.Worker(nodes=1),
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")]
                                )
                            ),
                        ),
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Standard",
        want={
            "model-serving-main": _leader_worker_set(
                replicas=1,
                size=2,
                leader_containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "resources": {"claims": [{"name": "devices"}]},
                        "env": [
                            {
                                "name": "MODELPLANE_LEADER_ADDRESS",
                                "value": "$(LWS_LEADER_ADDRESS)",
                            },
                            {"name": "MODELPLANE_RANK", "value": "$(LWS_WORKER_INDEX)"},
                        ],
                        "ports": [{"name": "http", "containerPort": 8001}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8001},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                    },
                    {"name": "proxy", "image": "example.com/proxy:v1"},
                ],
                leader_volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                worker_containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "resources": {"claims": [{"name": "devices"}]},
                        "env": [
                            {
                                "name": "MODELPLANE_LEADER_ADDRESS",
                                "value": "$(LWS_LEADER_ADDRESS)",
                            },
                            {"name": "MODELPLANE_RANK", "value": "$(LWS_WORKER_INDEX)"},
                        ],
                    }
                ],
                worker_volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-main-leader": _claim_template(name="r-main-leader-devices-f58c6", count=8),
            "resource-claim-main-worker": _claim_template(name="r-main-worker-devices-99b8a", count=8),
        },
    ),
    # A Worker doesn't serve: its ports pass through and nothing probes it.
    BuildCase(
        name="LLMDWorkerSidecar",
        reason="A Worker's engine ports and the container beside it pass through as written, with no probe.",
        backend=llmd.LLMDBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Leader",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest"),
                                    ]
                                )
                            ),
                        ),
                        v1alpha1.Member(
                            role="Worker",
                            nodePoolName="frontier",
                            worker=v1alpha1.Worker(nodes=1),
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            ports=[v1alpha1.Port(name="nixl", containerPort=5557)],
                                        ),
                                        v1alpha1.Container(name="log-tail", image="busybox:1.36"),
                                    ]
                                )
                            ),
                        ),
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Standard",
        want={
            "model-serving-main": _leader_worker_set(
                replicas=1,
                size=2,
                leader_containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "resources": {"claims": [{"name": "devices"}]},
                        "env": [
                            {
                                "name": "MODELPLANE_LEADER_ADDRESS",
                                "value": "$(LWS_LEADER_ADDRESS)",
                            },
                            {"name": "MODELPLANE_RANK", "value": "$(LWS_WORKER_INDEX)"},
                        ],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                    },
                ],
                leader_volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                worker_containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "resources": {"claims": [{"name": "devices"}]},
                        "env": [
                            {
                                "name": "MODELPLANE_LEADER_ADDRESS",
                                "value": "$(LWS_LEADER_ADDRESS)",
                            },
                            {"name": "MODELPLANE_RANK", "value": "$(LWS_WORKER_INDEX)"},
                        ],
                        "ports": [{"name": "nixl", "containerPort": 5557}],
                    },
                    {"name": "log-tail", "image": "busybox:1.36"},
                ],
                worker_volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-main-leader": _claim_template(name="r-main-leader-devices-f58c6", count=8),
            "resource-claim-main-worker": _claim_template(name="r-main-worker-devices-99b8a", count=8),
        },
    ),
    # Modelplane is unopinionated about the engine: each member's command passes
    # through verbatim, so a launch convention Modelplane has never heard of
    # still works.
    BuildCase(
        name="LLMDGangCommands",
        reason=(
            "A Leader/Worker engine on llm-d whose leader and worker each set a command composes a LeaderWorkerSet "
            "running both commands verbatim."
        ),
        backend=llmd.LLMDBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Leader",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                                            ],
                                        )
                                    ]
                                )
                            ),
                        ),
                        v1alpha1.Member(
                            role="Worker",
                            nodePoolName="frontier",
                            worker=v1alpha1.Worker(nodes=1),
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                                            ],
                                        )
                                    ]
                                )
                            ),
                        ),
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Standard",
        want={
            "model-serving-main": _leader_worker_set(
                replicas=1,
                size=2,
                leader_containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "resources": {"claims": [{"name": "devices"}]},
                        "command": [
                            "/bin/sh",
                            "-c",
                            "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                        ],
                        "env": [
                            {
                                "name": "MODELPLANE_LEADER_ADDRESS",
                                "value": "$(LWS_LEADER_ADDRESS)",
                            },
                            {"name": "MODELPLANE_RANK", "value": "$(LWS_WORKER_INDEX)"},
                        ],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                    }
                ],
                leader_volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                worker_containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "resources": {"claims": [{"name": "devices"}]},
                        "command": [
                            "/bin/sh",
                            "-c",
                            "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                        ],
                        "env": [
                            {
                                "name": "MODELPLANE_LEADER_ADDRESS",
                                "value": "$(LWS_LEADER_ADDRESS)",
                            },
                            {"name": "MODELPLANE_RANK", "value": "$(LWS_WORKER_INDEX)"},
                        ],
                    }
                ],
                worker_volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-main-leader": _claim_template(name="r-main-leader-devices-f58c6", count=8),
            "resource-claim-main-worker": _claim_template(name="r-main-worker-devices-99b8a", count=8),
        },
    ),
    BuildCase(
        name="LLMDMultiNodeWorker",
        reason="Two copies of a gang with a three-node worker compose a LeaderWorkerSet of two replicas of size four.",
        backend=llmd.LLMDBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=2,
                    members=[
                        v1alpha1.Member(
                            role="Leader",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")]
                                )
                            ),
                        ),
                        v1alpha1.Member(
                            role="Worker",
                            nodePoolName="frontier",
                            worker=v1alpha1.Worker(nodes=3),
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")]
                                )
                            ),
                        ),
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Standard",
        want={
            "model-serving-main": _leader_worker_set(
                replicas=2,
                size=4,
                leader_containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "resources": {"claims": [{"name": "devices"}]},
                        "env": [
                            {
                                "name": "MODELPLANE_LEADER_ADDRESS",
                                "value": "$(LWS_LEADER_ADDRESS)",
                            },
                            {"name": "MODELPLANE_RANK", "value": "$(LWS_WORKER_INDEX)"},
                        ],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                    }
                ],
                leader_volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                worker_containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "resources": {"claims": [{"name": "devices"}]},
                        "env": [
                            {
                                "name": "MODELPLANE_LEADER_ADDRESS",
                                "value": "$(LWS_LEADER_ADDRESS)",
                            },
                            {"name": "MODELPLANE_RANK", "value": "$(LWS_WORKER_INDEX)"},
                        ],
                    }
                ],
                worker_volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-main-leader": _claim_template(name="r-main-leader-devices-f58c6", count=8),
            "resource-claim-main-worker": _claim_template(name="r-main-worker-devices-99b8a", count=8),
        },
    ),
    # Only the native and Grove backends wire ModelExpress, and only on Dynamo,
    # which never selects llm-d. HF_HUB_CACHE is the cache's own env, on every
    # stack.
    BuildCase(
        name="LLMDWithCache",
        reason="A cached Leader/Worker engine on llm-d gets the cache env but no ModelExpress env.",
        backend=llmd.LLMDBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=v1alpha1.ModelCacheRef(name="c"),
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Leader",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")]
                                )
                            ),
                        ),
                        v1alpha1.Member(
                            role="Worker",
                            nodePoolName="frontier",
                            worker=v1alpha1.Worker(nodes=1),
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")]
                                )
                            ),
                        ),
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        serving_label="r",
        stack="Standard",
        want={
            "model-serving-main": _leader_worker_set(
                replicas=1,
                size=2,
                leader_containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "volumeMounts": [
                            {"name": "dshm", "mountPath": "/dev/shm"},
                            {"name": "model-cache", "mountPath": "/mnt/models"},
                        ],
                        "resources": {"claims": [{"name": "devices"}]},
                        "env": [
                            {
                                "name": "MODELPLANE_LEADER_ADDRESS",
                                "value": "$(LWS_LEADER_ADDRESS)",
                            },
                            {"name": "MODELPLANE_RANK", "value": "$(LWS_WORKER_INDEX)"},
                            {"name": "HF_HUB_CACHE", "value": "/mnt/models"},
                        ],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                    }
                ],
                leader_volumes=[
                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                    {
                        "name": "model-cache",
                        "persistentVolumeClaim": {"claimName": "modelcache-ml-team-c-c5cc6"},
                    },
                ],
                worker_containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "volumeMounts": [
                            {"name": "dshm", "mountPath": "/dev/shm"},
                            {"name": "model-cache", "mountPath": "/mnt/models"},
                        ],
                        "resources": {"claims": [{"name": "devices"}]},
                        "env": [
                            {
                                "name": "MODELPLANE_LEADER_ADDRESS",
                                "value": "$(LWS_LEADER_ADDRESS)",
                            },
                            {"name": "MODELPLANE_RANK", "value": "$(LWS_WORKER_INDEX)"},
                            {"name": "HF_HUB_CACHE", "value": "/mnt/models"},
                        ],
                    }
                ],
                worker_volumes=[
                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                    {
                        "name": "model-cache",
                        "persistentVolumeClaim": {"claimName": "modelcache-ml-team-c-c5cc6"},
                    },
                ],
            ),
            "resource-claim-main-leader": _claim_template(name="r-main-leader-devices-f58c6", count=8),
            "resource-claim-main-worker": _claim_template(name="r-main-worker-devices-99b8a", count=8),
        },
    ),
]


@pytest.mark.parametrize("case", BUILD_CASES, ids=lambda case: case.name)
def test_build(case: BuildCase) -> None:
    """A backend composes an engine's workload, and the claim templates its members need."""
    # build composes one engine, so build each of the replica's engines in turn.
    got: dict[str, k8sobjv1alpha1.Object] = {}
    for engine in case.replica.spec.engines:
        got.update(case.backend.build(case.replica, engine, case.provider_config, case.serving_label, case.stack))
    assert _sorted(objects=_to_dicts(objects=got)) == _sorted(objects=case.want), case.reason


SELECT_BACKEND_CASES = [
    SelectBackendCase(
        name="StandaloneOnStandard",
        reason="A Standalone engine on Standard selects the native backend.",
        engine=v1alpha1.Engine(
            name="main",
            copies=1,
            members=[
                v1alpha1.Member(
                    role="Standalone",
                    nodePoolName="frontier",
                    deviceRequests=[
                        v1alpha1.DeviceRequest(
                            name="gpu",
                            deviceClassName="gpu.nvidia.com",
                            count=1,
                            selectors=[
                                v1alpha1.Selector(
                                    cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                )
                            ],
                        )
                    ],
                    template=v1alpha1.Template(
                        spec=v1alpha1.Spec(
                            containers=[
                                v1alpha1.Container(
                                    name="engine", image="vllm/vllm-openai:latest", args=["--model=Qwen/Qwen3-0.6B"]
                                )
                            ]
                        )
                    ),
                )
            ],
        ),
        stack="Standard",
        want="native",
    ),
    SelectBackendCase(
        name="StandaloneOnDynamo",
        reason="A Standalone engine on Dynamo selects the native backend.",
        engine=v1alpha1.Engine(
            name="main",
            copies=1,
            members=[
                v1alpha1.Member(
                    role="Standalone",
                    nodePoolName="frontier",
                    deviceRequests=[
                        v1alpha1.DeviceRequest(
                            name="gpu",
                            deviceClassName="gpu.nvidia.com",
                            count=1,
                            selectors=[
                                v1alpha1.Selector(
                                    cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                )
                            ],
                        )
                    ],
                    template=v1alpha1.Template(
                        spec=v1alpha1.Spec(
                            containers=[
                                v1alpha1.Container(
                                    name="engine", image="vllm/vllm-openai:latest", args=["--model=Qwen/Qwen3-0.6B"]
                                )
                            ]
                        )
                    ),
                )
            ],
        ),
        stack="Dynamo",
        want="native",
    ),
    SelectBackendCase(
        name="GangOnStandard",
        reason="A Leader/Worker engine on Standard selects the llm-d backend.",
        engine=v1alpha1.Engine(
            name="main",
            copies=1,
            members=[
                v1alpha1.Member(
                    role="Leader",
                    nodePoolName="frontier",
                    deviceRequests=[
                        v1alpha1.DeviceRequest(
                            name="gpu",
                            deviceClassName="gpu.nvidia.com",
                            count=8,
                            selectors=[
                                v1alpha1.Selector(
                                    cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                )
                            ],
                        )
                    ],
                    template=v1alpha1.Template(
                        spec=v1alpha1.Spec(
                            containers=[v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")]
                        )
                    ),
                ),
                v1alpha1.Member(
                    role="Worker",
                    nodePoolName="frontier",
                    worker=v1alpha1.Worker(nodes=1),
                    deviceRequests=[
                        v1alpha1.DeviceRequest(
                            name="gpu",
                            deviceClassName="gpu.nvidia.com",
                            count=8,
                            selectors=[
                                v1alpha1.Selector(
                                    cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                )
                            ],
                        )
                    ],
                    template=v1alpha1.Template(
                        spec=v1alpha1.Spec(
                            containers=[v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")]
                        )
                    ),
                ),
            ],
        ),
        stack="Standard",
        want="llmd",
    ),
    SelectBackendCase(
        name="GangOnDynamo",
        reason="A Leader/Worker engine on Dynamo selects the Grove backend.",
        engine=v1alpha1.Engine(
            name="main",
            copies=1,
            members=[
                v1alpha1.Member(
                    role="Leader",
                    nodePoolName="frontier",
                    deviceRequests=[
                        v1alpha1.DeviceRequest(
                            name="gpu",
                            deviceClassName="gpu.nvidia.com",
                            count=8,
                            selectors=[
                                v1alpha1.Selector(
                                    cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                )
                            ],
                        )
                    ],
                    template=v1alpha1.Template(
                        spec=v1alpha1.Spec(
                            containers=[v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")]
                        )
                    ),
                ),
                v1alpha1.Member(
                    role="Worker",
                    nodePoolName="frontier",
                    worker=v1alpha1.Worker(nodes=1),
                    deviceRequests=[
                        v1alpha1.DeviceRequest(
                            name="gpu",
                            deviceClassName="gpu.nvidia.com",
                            count=8,
                            selectors=[
                                v1alpha1.Selector(
                                    cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                )
                            ],
                        )
                    ],
                    template=v1alpha1.Template(
                        spec=v1alpha1.Spec(
                            containers=[v1alpha1.Container(name="engine", image="vllm/vllm-openai:latest")]
                        )
                    ),
                ),
            ],
        ),
        stack="Dynamo",
        want="grove",
    ),
]


@pytest.mark.parametrize("case", SELECT_BACKEND_CASES, ids=lambda case: case.name)
def test_select_backend(case: SelectBackendCase) -> None:
    """An engine's member roles and its cluster's stack select its backend."""
    assert base.select_backend(case.engine, case.stack) == case.want, case.reason


# cache_mounts reads only a replica's namespace and cache, so the replicas here
# have no name and a placeholder cluster, c.
CACHE_MOUNTS_CASES = [
    CacheMountsCase(
        name="NoCache",
        reason="A replica with no cache gets no volume or mount.",
        replica=_replica(
            name=None,
            namespace="ml-team",
            cluster_name="c",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=[],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                )
            ],
        ),
        want=([], []),
    ),
    CacheMountsCase(
        name="WithCache",
        reason="A replica with a cache gets a volume for its PVC and a mount for it.",
        replica=_replica(
            name=None,
            namespace="ml-team",
            cluster_name="c",
            labels=None,
            model_cache_ref=v1alpha1.ModelCacheRef(name="qwen"),
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=[],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                )
            ],
        ),
        want=(
            [{"name": "model-cache", "persistentVolumeClaim": {"claimName": "modelcache-ml-team-qwen-17db2"}}],
            [{"name": "model-cache", "mountPath": "/mnt/models"}],
        ),
    ),
]


@pytest.mark.parametrize("case", CACHE_MOUNTS_CASES, ids=lambda case: case.name)
def test_cache_mounts(case: CacheMountsCase) -> None:
    """A replica's cache contributes a volume and a mount."""
    assert base.cache_mounts(case.replica) == case.want, case.reason


# cache_env reads only whether a replica has a cache, so the replicas here have
# no name and a placeholder cluster, c.
CACHE_ENV_CASES = [
    CacheEnvCase(
        name="NoCache",
        reason="A replica with no cache gets no env.",
        replica=_replica(
            name=None,
            namespace="ml-team",
            cluster_name="c",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=[],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                )
            ],
        ),
        want=[],
    ),
    # The cache is staged in HuggingFace's cache layout, so pointing
    # HF_HUB_CACHE at the mount is what lets an engine's own --model=<repo>
    # resolve against it instead of pulling from HuggingFace (#407). There's no
    # HF_HUB_OFFLINE: it would break an engine that fetches a different repo at
    # startup, like kimi-k2's separately gated tokenizer, and resolution doesn't
    # need it.
    CacheEnvCase(
        name="WithCache",
        reason="A replica with a cache gets HF_HUB_CACHE pointing at the mount, and no HF_HUB_OFFLINE.",
        replica=_replica(
            name=None,
            namespace="ml-team",
            cluster_name="c",
            labels=None,
            model_cache_ref=v1alpha1.ModelCacheRef(name="qwen"),
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=[],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                )
            ],
        ),
        want=[{"name": "HF_HUB_CACHE", "value": "/mnt/models"}],
    ),
]


@pytest.mark.parametrize("case", CACHE_ENV_CASES, ids=lambda case: case.name)
def test_cache_env(case: CacheEnvCase) -> None:
    """A replica's cache contributes the env that resolves a model against it."""
    assert base.cache_env(case.replica) == case.want, case.reason


APPLY_CASES = [
    # Routing only labels the engines' serving pods: the sidecar and any KV
    # transfer environment are the user's, and the picker runs the config the
    # replica sets.
    ApplyCase(
        name="PrefillDecode",
        reason=(
            "PrefillDecode role-labels its prefill and decode engines and fronts both with an InferencePool and "
            "endpoint picker."
        ),
        composed={
            "model-serving-prefill": _deployment(
                name="r-prefill-d90b0",
                claim_template_name="r-prefill-standalone-devices-f62af",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "prefill",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-prefill-d90b0",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-prefill-standalone": _claim_template(name="r-prefill-standalone-devices-f62af", count=1),
            "model-serving-decode": _deployment(
                name="r-decode-4b27b",
                claim_template_name="r-decode-standalone-devices-63392",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "decode",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-decode-4b27b",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-decode-standalone": _claim_template(name="r-decode-standalone-devices-63392", count=1),
        },
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=v1alpha1.Serving(
                mode="PrefillDecode",
                router="EndpointPicker",
                endpointPicker=v1alpha1.EndpointPicker(
                    image="ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0",
                    config={"kind": "EndpointPickerConfig"},
                ),
            ),
            engines=[
                v1alpha1.Engine(
                    name="prefill",
                    phase="Prefill",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=Qwen/Qwen3-0.6B"],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                ),
                v1alpha1.Engine(
                    name="decode",
                    phase="Decode",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=Qwen/Qwen3-0.6B"],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                ),
            ],
        ),
        provider_config="cluster-a-pc",
        want={
            "model-serving-prefill": _deployment(
                name="r-prefill-d90b0",
                claim_template_name="r-prefill-standalone-devices-f62af",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "prefill",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-prefill-d90b0",
                        "llm-d.ai/role": "prefill",
                        "llm-d.ai/inference-serving": "true",
                        "app": "r",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[
                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                ],
            ),
            "resource-claim-prefill-standalone": _claim_template(name="r-prefill-standalone-devices-f62af", count=1),
            "model-serving-decode": _deployment(
                name="r-decode-4b27b",
                claim_template_name="r-decode-standalone-devices-63392",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "decode",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-decode-4b27b",
                        "llm-d.ai/role": "decode",
                        "llm-d.ai/inference-serving": "true",
                        "app": "r",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    },
                ],
                volumes=[
                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                ],
            ),
            "resource-claim-decode-standalone": _claim_template(name="r-decode-standalone-devices-63392", count=1),
            "inference-pool": _inference_pool(),
            "model-route": _route(),
            "epp-serviceaccount": _epp_service_account(),
            "epp-role": _epp_role(),
            "epp-rolebinding": _epp_role_binding(),
            "epp-config": _epp_config(config=json.dumps({"kind": "EndpointPickerConfig"}, indent=2, sort_keys=True)),
            "epp": _epp(
                labels={"app": "r-epp", "modelplane.ai/role": "picker"},
                image="ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0",
                config_checksum="a9d9c5892fd25b535b127de68f30159b3d132f91f37c4c96b9e784b358d4b842",
            ),
            "epp-service": _epp_service(),
        },
    ),
    ApplyCase(
        name="EndpointPicker",
        reason="serving.endpointPicker sets the picker image, and its config replaces the default configuration.",
        composed={
            "model-serving-prefill": _deployment(
                name="r-prefill-d90b0",
                claim_template_name="r-prefill-standalone-devices-f62af",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "prefill",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-prefill-d90b0",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-prefill-standalone": _claim_template(name="r-prefill-standalone-devices-f62af", count=1),
            "model-serving-decode": _deployment(
                name="r-decode-4b27b",
                claim_template_name="r-decode-standalone-devices-63392",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "decode",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-decode-4b27b",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=m", "--block-size=64"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-decode-standalone": _claim_template(name="r-decode-standalone-devices-63392", count=1),
        },
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=v1alpha1.Serving(
                mode="PrefillDecode",
                router="EndpointPicker",
                endpointPicker=v1alpha1.EndpointPicker(
                    image="example.com/picker:v2",
                    config={
                        "apiVersion": "llm-d.ai/v1alpha1",
                        "kind": "EndpointPickerConfig",
                        "plugins": [{"type": "always-disagg-pd-decider"}],
                    },
                ),
            ),
            engines=[
                v1alpha1.Engine(
                    name="prefill",
                    phase="Prefill",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=Qwen/Qwen3-0.6B"],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                ),
                v1alpha1.Engine(
                    name="decode",
                    phase="Decode",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=m", "--block-size=64"],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                ),
            ],
        ),
        provider_config="cluster-a-pc",
        want={
            "model-serving-prefill": _deployment(
                name="r-prefill-d90b0",
                claim_template_name="r-prefill-standalone-devices-f62af",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "prefill",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-prefill-d90b0",
                        "llm-d.ai/role": "prefill",
                        "llm-d.ai/inference-serving": "true",
                        "app": "r",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[
                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                ],
            ),
            "resource-claim-prefill-standalone": _claim_template(name="r-prefill-standalone-devices-f62af", count=1),
            "model-serving-decode": _deployment(
                name="r-decode-4b27b",
                claim_template_name="r-decode-standalone-devices-63392",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "decode",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-decode-4b27b",
                        "llm-d.ai/role": "decode",
                        "llm-d.ai/inference-serving": "true",
                        "app": "r",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=m", "--block-size=64"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    },
                ],
                volumes=[
                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                ],
            ),
            "resource-claim-decode-standalone": _claim_template(name="r-decode-standalone-devices-63392", count=1),
            "inference-pool": _inference_pool(),
            "model-route": _route(),
            "epp-serviceaccount": _epp_service_account(),
            "epp-role": _epp_role(),
            "epp-rolebinding": _epp_role_binding(),
            # A user config is rendered as JSON, which is YAML, in the order the user wrote it.
            "epp-config": _epp_config(
                config=json.dumps(
                    {
                        "apiVersion": "llm-d.ai/v1alpha1",
                        "kind": "EndpointPickerConfig",
                        "plugins": [{"type": "always-disagg-pd-decider"}],
                    },
                    indent=2,
                    sort_keys=True,
                )
            ),
            "epp": _epp(
                labels={"app": "r-epp", "modelplane.ai/role": "picker"},
                config_checksum="b556a38fb374352df960ae5caa04221c7dde46ca6fe483ad6869b7ef2db0dd75",
                image="example.com/picker:v2",
            ),
            "epp-service": _epp_service(),
        },
    ),
    ApplyCase(
        name="EngineNamesUnlikePhases",
        reason=(
            "Engines named alpha and beta take their PrefillDecode roles from their phases, so alpha, the Decode "
            "engine, gets the decode role."
        ),
        composed={
            "model-serving-alpha": _deployment(
                name="r-alpha-b36ce",
                claim_template_name="r-alpha-standalone-devices-9ec1c",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "alpha",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-alpha-b36ce",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-alpha-standalone": _claim_template(name="r-alpha-standalone-devices-9ec1c", count=1),
            "model-serving-beta": _deployment(
                name="r-beta-52d85",
                claim_template_name="r-beta-standalone-devices-9d8b1",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "beta",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-beta-52d85",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-beta-standalone": _claim_template(name="r-beta-standalone-devices-9d8b1", count=1),
        },
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=v1alpha1.Serving(
                mode="PrefillDecode",
                router="EndpointPicker",
                endpointPicker=v1alpha1.EndpointPicker(
                    image="ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0",
                    config={"kind": "EndpointPickerConfig"},
                ),
            ),
            engines=[
                v1alpha1.Engine(
                    name="alpha",
                    phase="Decode",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=Qwen/Qwen3-0.6B"],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                ),
                v1alpha1.Engine(
                    name="beta",
                    phase="Prefill",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=Qwen/Qwen3-0.6B"],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                ),
            ],
        ),
        provider_config="cluster-a-pc",
        want={
            "model-serving-alpha": _deployment(
                name="r-alpha-b36ce",
                claim_template_name="r-alpha-standalone-devices-9ec1c",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "alpha",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-alpha-b36ce",
                        "llm-d.ai/role": "decode",
                        "llm-d.ai/inference-serving": "true",
                        "app": "r",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    },
                ],
                volumes=[
                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                ],
            ),
            "resource-claim-alpha-standalone": _claim_template(name="r-alpha-standalone-devices-9ec1c", count=1),
            "model-serving-beta": _deployment(
                name="r-beta-52d85",
                claim_template_name="r-beta-standalone-devices-9d8b1",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "beta",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-beta-52d85",
                        "llm-d.ai/role": "prefill",
                        "llm-d.ai/inference-serving": "true",
                        "app": "r",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[
                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                ],
            ),
            "resource-claim-beta-standalone": _claim_template(name="r-beta-standalone-devices-9d8b1", count=1),
            "inference-pool": _inference_pool(),
            "model-route": _route(),
            "epp-serviceaccount": _epp_service_account(),
            "epp-role": _epp_role(),
            "epp-rolebinding": _epp_role_binding(),
            "epp-config": _epp_config(config=json.dumps({"kind": "EndpointPickerConfig"}, indent=2, sort_keys=True)),
            "epp": _epp(
                labels={"app": "r-epp", "modelplane.ai/role": "picker"},
                image="ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0",
                config_checksum="a9d9c5892fd25b535b127de68f30159b3d132f91f37c4c96b9e784b358d4b842",
            ),
            "epp-service": _epp_service(),
        },
    ),
    # The leader clique gets the llm-d role, inference-serving and app labels.
    # The worker clique never serves.
    ApplyCase(
        name="GroveDecodeGang",
        reason=(
            "A Grove decode gang has its leader clique decorated like a Deployment's pod template, and its worker "
            "clique left as the backend composed it."
        ),
        composed={
            "model-serving-prefill": _deployment(
                name="r-prefill-d90b0",
                claim_template_name="r-prefill-standalone-devices-f62af",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "prefill",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-prefill-d90b0",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-prefill-standalone": _claim_template(name="r-prefill-standalone-devices-f62af", count=1),
            "model-serving-decode": _pod_clique_set(
                name="r-decode-4b27b",
                cliques=[
                    {
                        "name": "leader",
                        "labels": {
                            "modelplane.ai/engine": "decode",
                            "modelplane.ai/role": "Leader",
                            "modelplane.ai/serving": "r",
                            "kai.scheduler/queue": "modelplane",
                            "modelplane.ai/clique-role": "leader",
                        },
                        "spec": {
                            "roleName": "leader",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                                        ],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            }
                                        ],
                                        "ports": [{"name": "http", "containerPort": 8000}],
                                        "readinessProbe": {
                                            "httpGet": {"path": "/health", "port": 8000},
                                            "initialDelaySeconds": 30,
                                            "periodSeconds": 10,
                                            "timeoutSeconds": 5,
                                        },
                                    }
                                ],
                                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-decode-leader-devices-d2e70",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                    {
                        "name": "worker",
                        "labels": {
                            "modelplane.ai/engine": "decode",
                            "modelplane.ai/role": "Worker",
                            "kai.scheduler/queue": "modelplane",
                        },
                        "spec": {
                            "roleName": "worker",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                                        ],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            }
                                        ],
                                    }
                                ],
                                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-decode-worker-devices-08d44",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                ],
            ),
            "resource-claim-decode-leader": _claim_template(name="r-decode-leader-devices-d2e70", count=8),
            "resource-claim-decode-worker": _claim_template(name="r-decode-worker-devices-08d44", count=8),
        },
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=v1alpha1.Serving(
                mode="PrefillDecode",
                router="EndpointPicker",
                endpointPicker=v1alpha1.EndpointPicker(
                    image="ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0",
                    config={"kind": "EndpointPickerConfig"},
                ),
            ),
            engines=[
                v1alpha1.Engine(
                    name="prefill",
                    phase="Prefill",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=Qwen/Qwen3-0.6B"],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                ),
                v1alpha1.Engine(
                    name="decode",
                    phase="Decode",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Leader",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                                            ],
                                        )
                                    ]
                                )
                            ),
                        ),
                        v1alpha1.Member(
                            role="Worker",
                            nodePoolName="frontier",
                            worker=v1alpha1.Worker(nodes=1),
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                                            ],
                                        )
                                    ]
                                )
                            ),
                        ),
                    ],
                ),
            ],
        ),
        provider_config="cluster-a-pc",
        want={
            "model-serving-prefill": _deployment(
                name="r-prefill-d90b0",
                claim_template_name="r-prefill-standalone-devices-f62af",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "prefill",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-prefill-d90b0",
                        "llm-d.ai/role": "prefill",
                        "llm-d.ai/inference-serving": "true",
                        "app": "r",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[
                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                ],
            ),
            "resource-claim-prefill-standalone": _claim_template(name="r-prefill-standalone-devices-f62af", count=1),
            "model-serving-decode": _pod_clique_set(
                name="r-decode-4b27b",
                cliques=[
                    {
                        "name": "leader",
                        "labels": {
                            "modelplane.ai/engine": "decode",
                            "modelplane.ai/role": "Leader",
                            "modelplane.ai/serving": "r",
                            "kai.scheduler/queue": "modelplane",
                            "modelplane.ai/clique-role": "leader",
                            "llm-d.ai/role": "decode",
                            "llm-d.ai/inference-serving": "true",
                            "app": "r",
                        },
                        "spec": {
                            "roleName": "leader",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                                        ],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            },
                                        ],
                                        "ports": [{"name": "http", "containerPort": 8000}],
                                        "readinessProbe": {
                                            "httpGet": {"path": "/health", "port": 8000},
                                            "initialDelaySeconds": 30,
                                            "periodSeconds": 10,
                                            "timeoutSeconds": 5,
                                        },
                                    },
                                ],
                                "volumes": [
                                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                                ],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-decode-leader-devices-d2e70",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                    {
                        "name": "worker",
                        "labels": {
                            "modelplane.ai/engine": "decode",
                            "modelplane.ai/role": "Worker",
                            "kai.scheduler/queue": "modelplane",
                        },
                        "spec": {
                            "roleName": "worker",
                            "replicas": 1,
                            "minAvailable": 1,
                            "podSpec": {
                                "containers": [
                                    {
                                        "name": "engine",
                                        "image": "vllm/vllm-openai:latest",
                                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                                        "resources": {"claims": [{"name": "devices"}]},
                                        "command": [
                                            "/bin/sh",
                                            "-c",
                                            "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                                        ],
                                        "env": [
                                            {
                                                "name": "MODELPLANE_LEADER_ADDRESS",
                                                "value": "$(GROVE_PCSG_NAME)-$(GROVE_PCSG_INDEX)-leader-0.$(GROVE_HEADLESS_SERVICE)",
                                            }
                                        ],
                                    }
                                ],
                                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                                "schedulerName": "kai-scheduler",
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-decode-worker-devices-08d44",
                                    }
                                ],
                                "tolerations": [
                                    {
                                        "key": "nvidia.com/gpu",
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ],
                            },
                        },
                    },
                ],
            ),
            "resource-claim-decode-leader": _claim_template(name="r-decode-leader-devices-d2e70", count=8),
            "resource-claim-decode-worker": _claim_template(name="r-decode-worker-devices-08d44", count=8),
            "inference-pool": _inference_pool(),
            "model-route": _route(),
            "epp-serviceaccount": _epp_service_account(),
            "epp-role": _epp_role(),
            "epp-rolebinding": _epp_role_binding(),
            "epp-config": _epp_config(config=json.dumps({"kind": "EndpointPickerConfig"}, indent=2, sort_keys=True)),
            "epp": _epp(
                labels={"app": "r-epp", "modelplane.ai/role": "picker"},
                image="ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0",
                config_checksum="a9d9c5892fd25b535b127de68f30159b3d132f91f37c4c96b9e784b358d4b842",
            ),
            "epp-service": _epp_service(),
        },
    ),
    # A replica with no serving block is served Unified.
    #
    # A single serving pod has nothing to pick between, but still gets the pool.
    # Always fronting with one avoids swapping a Service for a pool when a
    # second pod appears, a swap that would drop in-flight requests. The pool
    # selects the pods by the serving label they already carry.
    #
    # The picker scores by prefix cache and queue depth in a single profile,
    # with no prefill/decode split, fed by the approx-prefix-cache-producer. It
    # reads its config once at startup, so its pod template carries a sha256 of
    # the config, and a config change rolls the pod. Its image and the
    # EndpointPickerConfig API group are pinned here too, so a wrong tag or a
    # stale group fails here rather than crashlooping at deploy.
    ApplyCase(
        name="UnifiedOnePod",
        reason=(
            "A replica of one Standalone pod with no serving block is fronted by an InferencePool and endpoint picker."
        ),
        composed={
            "model-serving-main": _deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "main",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-main-standalone": _claim_template(name="r-main-standalone-devices-f456f", count=1),
        },
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=Qwen/Qwen3-0.6B"],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        want={
            "model-serving-main": _deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "main",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-main-standalone": _claim_template(name="r-main-standalone-devices-f456f", count=1),
            "inference-pool": _inference_pool(),
            "model-route": _route(),
            "epp-serviceaccount": _epp_service_account(),
            "epp-role": _epp_role(),
            "epp-rolebinding": _epp_role_binding(),
            "epp-config": _epp_config(
                config="apiVersion: llm-d.ai/v1alpha1\n"
                "kind: EndpointPickerConfig\n"
                "plugins:\n"
                "- type: approx-prefix-cache-producer\n"
                "  parameters:\n"
                "    autoTune: false\n"
                "    blockSizeTokens: 64\n"
                "    maxPrefixBlocksToMatch: 256\n"
                "    lruCapacityPerServer: 31250\n"
                "- type: prefix-cache-scorer\n"
                "- type: queue-scorer\n"
                "- type: max-score-picker\n"
                "schedulingProfiles:\n"
                "- name: default\n"
                "  plugins:\n"
                "  - pluginRef: max-score-picker\n"
                "  - pluginRef: prefix-cache-scorer\n"
                "    weight: 2\n"
                "  - pluginRef: queue-scorer\n"
                "    weight: 1\n"
            ),
            "epp": _epp(
                labels={"app": "r-epp", "modelplane.ai/role": "picker"},
                image="ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0",
                config_checksum="bf2146be008c2e12bb555e5406e1d419b2b8159aa3b2984623e385d3cf934007",
            ),
            "epp-service": _epp_service(),
        },
    ),
    # A built-in MetricMapping renames the picker's scheduling latency, and
    # nothing can match it unless something scrapes the picker. The collector's
    # engine job keeps a pod on two things: the deployment label, which comes
    # from this replica's, and a container port named http, which every picker
    # has. The app label is still the Deployment's own selector, which must not
    # move.
    ApplyCase(
        name="UnifiedLabeledReplica",
        reason="A Unified replica labeled with its deployment and index gets an endpoint picker labeled with both.",
        composed={
            "model-serving-main": _deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/deployment": "qwen3-8b",
                        "modelplane.ai/replica": "2",
                        "modelplane.ai/engine": "main",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-main-standalone": _claim_template(name="r-main-standalone-devices-f456f", count=1),
        },
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels={"modelplane.ai/deployment": "qwen3-8b", "modelplane.ai/replica-index": "2"},
            model_cache_ref=None,
            serving=v1alpha1.Serving(mode="Unified"),
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=Qwen/Qwen3-0.6B"],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        want={
            "model-serving-main": _deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/deployment": "qwen3-8b",
                        "modelplane.ai/replica": "2",
                        "modelplane.ai/engine": "main",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-main-standalone": _claim_template(name="r-main-standalone-devices-f456f", count=1),
            "inference-pool": _inference_pool(),
            "model-route": _route(),
            "epp-serviceaccount": _epp_service_account(),
            "epp-role": _epp_role(),
            "epp-rolebinding": _epp_role_binding(),
            "epp-config": _epp_config(
                config="apiVersion: llm-d.ai/v1alpha1\n"
                "kind: EndpointPickerConfig\n"
                "plugins:\n"
                "- type: approx-prefix-cache-producer\n"
                "  parameters:\n"
                "    autoTune: false\n"
                "    blockSizeTokens: 64\n"
                "    maxPrefixBlocksToMatch: 256\n"
                "    lruCapacityPerServer: 31250\n"
                "- type: prefix-cache-scorer\n"
                "- type: queue-scorer\n"
                "- type: max-score-picker\n"
                "schedulingProfiles:\n"
                "- name: default\n"
                "  plugins:\n"
                "  - pluginRef: max-score-picker\n"
                "  - pluginRef: prefix-cache-scorer\n"
                "    weight: 2\n"
                "  - pluginRef: queue-scorer\n"
                "    weight: 1\n"
            ),
            "epp": _epp(
                labels={
                    "app": "r-epp",
                    "modelplane.ai/role": "picker",
                    "modelplane.ai/deployment": "qwen3-8b",
                    "modelplane.ai/replica": "2",
                },
                image="ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0",
                config_checksum="bf2146be008c2e12bb555e5406e1d419b2b8159aa3b2984623e385d3cf934007",
            ),
            "epp-service": _epp_service(),
        },
    ),
    ApplyCase(
        name="UnifiedSeveralPods",
        reason=(
            "A replica of two Standalone pods with no serving block is fronted by an InferencePool and endpoint picker."
        ),
        composed={
            "model-serving-main": _deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=2,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "main",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-main-standalone": _claim_template(name="r-main-standalone-devices-f456f", count=1),
        },
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=2,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=Qwen/Qwen3-0.6B"],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        want={
            "model-serving-main": _deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=2,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/engine": "main",
                        "modelplane.ai/role": "Standalone",
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                    }
                ],
                volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-main-standalone": _claim_template(name="r-main-standalone-devices-f456f", count=1),
            "inference-pool": _inference_pool(),
            "model-route": _route(),
            "epp-serviceaccount": _epp_service_account(),
            "epp-role": _epp_role(),
            "epp-rolebinding": _epp_role_binding(),
            "epp-config": _epp_config(
                config="apiVersion: llm-d.ai/v1alpha1\n"
                "kind: EndpointPickerConfig\n"
                "plugins:\n"
                "- type: approx-prefix-cache-producer\n"
                "  parameters:\n"
                "    autoTune: false\n"
                "    blockSizeTokens: 64\n"
                "    maxPrefixBlocksToMatch: 256\n"
                "    lruCapacityPerServer: 31250\n"
                "- type: prefix-cache-scorer\n"
                "- type: queue-scorer\n"
                "- type: max-score-picker\n"
                "schedulingProfiles:\n"
                "- name: default\n"
                "  plugins:\n"
                "  - pluginRef: max-score-picker\n"
                "  - pluginRef: prefix-cache-scorer\n"
                "    weight: 2\n"
                "  - pluginRef: queue-scorer\n"
                "    weight: 1\n"
            ),
            "epp": _epp(
                labels={"app": "r-epp", "modelplane.ai/role": "picker"},
                image="ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0",
                config_checksum="bf2146be008c2e12bb555e5406e1d419b2b8159aa3b2984623e385d3cf934007",
            ),
            "epp-service": _epp_service(),
        },
    ),
    # Unified routing reads the engine args for the KV block size through
    # _serving_pod_templates, which normalizes a LeaderWorkerSet's
    # leaderTemplate alongside a Deployment's pod template and a Grove
    # PodCliqueSet's leader clique. A regression case for a normalization that
    # only knew Deployment and PodCliqueSet, and raised KeyError on a
    # LeaderWorkerSet.
    ApplyCase(
        name="UnifiedLeaderWorkerSet",
        reason="A LeaderWorkerSet with no serving block is fronted by an InferencePool and endpoint picker.",
        composed={
            "model-serving-main": _leader_worker_set(
                replicas=1,
                size=2,
                leader_containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "resources": {"claims": [{"name": "devices"}]},
                        "command": [
                            "/bin/sh",
                            "-c",
                            "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                        ],
                        "env": [
                            {
                                "name": "MODELPLANE_LEADER_ADDRESS",
                                "value": "$(LWS_LEADER_ADDRESS)",
                            },
                            {"name": "MODELPLANE_RANK", "value": "$(LWS_WORKER_INDEX)"},
                        ],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                    }
                ],
                leader_volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                worker_containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "resources": {"claims": [{"name": "devices"}]},
                        "command": [
                            "/bin/sh",
                            "-c",
                            "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                        ],
                        "env": [
                            {
                                "name": "MODELPLANE_LEADER_ADDRESS",
                                "value": "$(LWS_LEADER_ADDRESS)",
                            },
                            {"name": "MODELPLANE_RANK", "value": "$(LWS_WORKER_INDEX)"},
                        ],
                    }
                ],
                worker_volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-main-leader": _claim_template(name="r-main-leader-devices-f58c6", count=8),
            "resource-claim-main-worker": _claim_template(name="r-main-worker-devices-99b8a", count=8),
        },
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Leader",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                                            ],
                                        )
                                    ]
                                )
                            ),
                        ),
                        v1alpha1.Member(
                            role="Worker",
                            nodePoolName="frontier",
                            worker=v1alpha1.Worker(nodes=1),
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=8,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            command=[
                                                "/bin/sh",
                                                "-c",
                                                "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                                            ],
                                        )
                                    ]
                                )
                            ),
                        ),
                    ],
                )
            ],
        ),
        provider_config="cluster-a-pc",
        want={
            "model-serving-main": _leader_worker_set(
                replicas=1,
                size=2,
                leader_containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "resources": {"claims": [{"name": "devices"}]},
                        "command": [
                            "/bin/sh",
                            "-c",
                            "ray start --head --port=6379; exec vllm serve --model=meta-llama/Llama-3.1-405B --tensor-parallel-size=8 --pipeline-parallel-size=2 --port=8000",
                        ],
                        "env": [
                            {
                                "name": "MODELPLANE_LEADER_ADDRESS",
                                "value": "$(LWS_LEADER_ADDRESS)",
                            },
                            {"name": "MODELPLANE_RANK", "value": "$(LWS_WORKER_INDEX)"},
                        ],
                        "ports": [{"name": "http", "containerPort": 8000}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                    }
                ],
                leader_volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                worker_containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "resources": {"claims": [{"name": "devices"}]},
                        "command": [
                            "/bin/sh",
                            "-c",
                            "exec ray start --address=$(MODELPLANE_LEADER_ADDRESS):6379 --block",
                        ],
                        "env": [
                            {
                                "name": "MODELPLANE_LEADER_ADDRESS",
                                "value": "$(LWS_LEADER_ADDRESS)",
                            },
                            {"name": "MODELPLANE_RANK", "value": "$(LWS_WORKER_INDEX)"},
                        ],
                    }
                ],
                worker_volumes=[{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            ),
            "resource-claim-main-leader": _claim_template(name="r-main-leader-devices-f58c6", count=8),
            "resource-claim-main-worker": _claim_template(name="r-main-worker-devices-99b8a", count=8),
            "inference-pool": _inference_pool(),
            "model-route": _route(),
            "epp-serviceaccount": _epp_service_account(),
            "epp-role": _epp_role(),
            "epp-rolebinding": _epp_role_binding(),
            "epp-config": _epp_config(
                config="apiVersion: llm-d.ai/v1alpha1\n"
                "kind: EndpointPickerConfig\n"
                "plugins:\n"
                "- type: approx-prefix-cache-producer\n"
                "  parameters:\n"
                "    autoTune: false\n"
                "    blockSizeTokens: 64\n"
                "    maxPrefixBlocksToMatch: 256\n"
                "    lruCapacityPerServer: 31250\n"
                "- type: prefix-cache-scorer\n"
                "- type: queue-scorer\n"
                "- type: max-score-picker\n"
                "schedulingProfiles:\n"
                "- name: default\n"
                "  plugins:\n"
                "  - pluginRef: max-score-picker\n"
                "  - pluginRef: prefix-cache-scorer\n"
                "    weight: 2\n"
                "  - pluginRef: queue-scorer\n"
                "    weight: 1\n"
            ),
            "epp": _epp(
                labels={"app": "r-epp", "modelplane.ai/role": "picker"},
                image="ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0",
                config_checksum="bf2146be008c2e12bb555e5406e1d419b2b8159aa3b2984623e385d3cf934007",
            ),
            "epp-service": _epp_service(),
        },
    ),
]


@pytest.mark.parametrize("case", APPLY_CASES, ids=lambda case: case.name)
def test_apply(case: ApplyCase) -> None:
    """routing.apply fronts a replica's engines with the routing its serving mode selects."""
    composed = {key: k8sobjv1alpha1.Object.model_validate(copy.deepcopy(obj)) for key, obj in case.composed.items()}  # noqa: MPT401  # routing.apply edits the manifests in place.
    got = routing.apply(composed, case.replica, case.provider_config)
    assert _sorted(objects=_to_dicts(objects=got)) == _sorted(objects=case.want), case.reason


# The EPP prefix-cache producer's blockSizeTokens is derived best-effort from
# the engine flags (#179), so it matches the engine's KV block size.
KV_BLOCK_SIZE_CASES = [
    KvBlockSizeCase(
        name="NoArgs",
        reason="With no engine args the block size defaults to 16.",
        engine_args=[],
        want=16,
    ),
    KvBlockSizeCase(
        name="NoBlockSizeFlag",
        reason="With no block size flag the block size defaults to 16.",
        engine_args=["--model=/mnt/models"],
        want=16,
    ),
    KvBlockSizeCase(
        name="VLLMBlockSize",
        reason="vLLM's --block-size followed by 32 sets the block size.",
        engine_args=["--block-size", "32"],
        want=32,
    ),
    KvBlockSizeCase(
        name="VLLMBlockSizeEquals",
        reason="vLLM's --block-size=8 sets the block size.",
        engine_args=["--model=/m", "--block-size=8"],
        want=8,
    ),
    KvBlockSizeCase(
        name="SGLangPageSize",
        reason="SGLang's --page-size=64 sets the block size.",
        engine_args=["--page-size=64"],
        want=64,
    ),
    KvBlockSizeCase(
        name="NonIntegerBlockSize",
        reason="A block size of auto falls back to 16.",
        engine_args=["--block-size", "auto"],
        want=16,
    ),
]


@pytest.mark.parametrize("case", KV_BLOCK_SIZE_CASES, ids=lambda case: case.name)
def test_kv_block_size(case: KvBlockSizeCase) -> None:
    """The KV block size comes from the engine's flags."""
    assert routing._kv_block_size(case.engine_args) == case.want, case.reason


UNIFIED_BLOCK_SIZE_CASES = [
    UnifiedBlockSizeCase(
        name="BelowMinimum",
        reason="A block size below 64 renders as 64, the smallest the picker uses.",
        block_size=16,
        want="    blockSizeTokens: 64\n",
    ),
    UnifiedBlockSizeCase(
        name="AtMinimum",
        reason="A block size of 64 renders as is.",
        block_size=64,
        want="    blockSizeTokens: 64\n",
    ),
    UnifiedBlockSizeCase(
        name="AboveMinimum",
        reason="A block size above 64 renders as is.",
        block_size=128,
        want="    blockSizeTokens: 128\n",
    ),
]


@pytest.mark.parametrize("case", UNIFIED_BLOCK_SIZE_CASES, ids=lambda case: case.name)
def test_unified_block_size(case: UnifiedBlockSizeCase) -> None:
    """The unified config's blockSizeTokens is the engine's block size, at least 64."""
    assert case.want in routing._unified_epp_config_yaml(case.block_size), case.reason


PICKER_CASES = [
    PickerCase(
        name="NoServing",
        reason="A replica with no serving block runs the default picker image and configuration.",
        serving=None,
        want=("ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0", "default"),
    ),
    PickerCase(
        name="ImageAndConfig",
        reason="An endpointPicker with an image and a config runs both, the config replacing the default.",
        serving=v1alpha1.Serving(
            mode="Unified",
            router="EndpointPicker",
            endpointPicker=v1alpha1.EndpointPicker(image="example.com/picker:v2", config={"kind": "K"}),
        ),
        want=("example.com/picker:v2", '{\n  "kind": "K"\n}'),
    ),
]


@pytest.mark.parametrize("case", PICKER_CASES, ids=lambda case: case.name)
def test_picker(case: PickerCase) -> None:
    """The picker runs the user's image and config where set, and the defaults otherwise."""
    replica = _replica(
        name="r",
        namespace="ml-team",
        cluster_name="cluster-a",
        labels=None,
        model_cache_ref=None,
        serving=case.serving,
        engines=[
            v1alpha1.Engine(
                name="main",
                members=[
                    v1alpha1.Member(
                        role="Standalone",
                        nodePoolName="frontier",
                        template=v1alpha1.Template(
                            spec=v1alpha1.Spec(containers=[v1alpha1.Container(name="engine", image="e")])
                        ),
                    )
                ],
            )
        ],
    )
    assert routing._picker(replica, "default") == case.want, case.reason


ROUTING_MISSING_CASES = [
    RoutingMissingCase(
        name="Unified",
        reason="A Unified replica needs no routing of its own.",
        serving=v1alpha1.Serving(mode="Unified"),
        want=False,
    ),
    RoutingMissingCase(
        name="PrefillDecodeWithRouting",
        reason="A PrefillDecode replica with a router and a picker config has its routing.",
        serving=v1alpha1.Serving(
            mode="PrefillDecode",
            router="EndpointPicker",
            endpointPicker=v1alpha1.EndpointPicker(image="example.com/picker:v2", config={"kind": "K"}),
        ),
        want=False,
    ),
    RoutingMissingCase(
        name="PrefillDecodeStored",
        reason="A PrefillDecode replica without router or picker config lacks its routing.",
        serving=v1alpha1.Serving(mode="PrefillDecode"),
        want=True,
    ),
    RoutingMissingCase(
        name="PrefillDecodeNoConfig",
        reason="A PrefillDecode replica with a router but no picker config lacks its routing.",
        serving=v1alpha1.Serving(
            mode="PrefillDecode",
            router="EndpointPicker",
            endpointPicker=v1alpha1.EndpointPicker(image="example.com/picker:v2"),
        ),
        want=True,
    ),
]


@pytest.mark.parametrize("case", ROUTING_MISSING_CASES, ids=lambda case: case.name)
def test_routing_missing(case: RoutingMissingCase) -> None:
    """A PrefillDecode replica without its routing is caught before it's composed."""
    replica = _replica(
        name="r",
        namespace="ml-team",
        cluster_name="cluster-a",
        labels=None,
        model_cache_ref=None,
        serving=case.serving,
        engines=[
            v1alpha1.Engine(
                name="main",
                members=[
                    v1alpha1.Member(
                        role="Standalone",
                        nodePoolName="frontier",
                        template=v1alpha1.Template(
                            spec=v1alpha1.Spec(containers=[v1alpha1.Container(name="engine", image="e")])
                        ),
                    )
                ],
            )
        ],
    )
    assert routing.prefill_decode_routing_missing(replica) == case.want, case.reason


# A user's config reaches the function through a protobuf Struct, which keeps no
# key order and turns every number into a float. Either would change the
# rendering, and so the checksum that rolls the picker, between runs.
RENDER_PICKER_CONFIG_CASES = [
    RenderPickerConfigCase(
        name="KeysSorted",
        reason="Keys render sorted at every level, whatever order they arrive in.",
        config={"plugins": [{"type": "queue-scorer", "parameters": {"b": "x", "a": "y"}}], "kind": "K"},
        want=(
            "{\n"
            '  "kind": "K",\n'
            '  "plugins": [\n'
            "    {\n"
            '      "parameters": {\n'
            '        "a": "y",\n'
            '        "b": "x"\n'
            "      },\n"
            '      "type": "queue-scorer"\n'
            "    }\n"
            "  ]\n"
            "}"
        ),
    ),
    RenderPickerConfigCase(
        name="WholeFloatsAsInts",
        reason="A whole float renders as an int, and a fractional float stays a float.",
        config={"blockSizeTokens": 64.0, "weights": [2.0, 0.5]},
        want='{\n  "blockSizeTokens": 64,\n  "weights": [\n    2,\n    0.5\n  ]\n}',
    ),
]


@pytest.mark.parametrize("case", RENDER_PICKER_CONFIG_CASES, ids=lambda case: case.name)
def test_render_picker_config(case: RenderPickerConfigCase) -> None:
    """A user's picker config renders the same way every run."""
    assert routing._render_picker_config(case.config) == case.want, case.reason


# compose-inference-cluster creates the namespace a replica's objects land in,
# and compose-model-route and compose-model-cache land objects in it by the same
# derivation, so all four must agree on it.
REMOTE_NAMESPACE_CASES = [
    RemoteNamespaceCase(
        name="ShortNamespace",
        reason="A short namespace keeps its name, prefixed with mp- and suffixed with a hash.",
        replica=_replica(
            name="r",
            namespace="ml-team",
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=Qwen/Qwen3-0.6B"],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                )
            ],
        ),
        want="mp-ml-team-51733",
    ),
    # 63 is the longest a namespace can be, so mp- plus it can't be used as
    # is. The lengths are the point, so the names are written as repeats rather
    # than 63-character literals.
    RemoteNamespaceCase(
        name="LongestNamespace",
        reason="A 63-character namespace is truncated to leave room for the hash, yielding a 63-character name.",
        replica=_replica(
            name="r",
            namespace="a" * 63,
            cluster_name="cluster-a",
            labels=None,
            model_cache_ref=None,
            serving=None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=1,
                    members=[
                        v1alpha1.Member(
                            role="Standalone",
                            nodePoolName="frontier",
                            deviceRequests=[
                                v1alpha1.DeviceRequest(
                                    name="gpu",
                                    deviceClassName="gpu.nvidia.com",
                                    count=1,
                                    selectors=[
                                        v1alpha1.Selector(
                                            cel='device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                        )
                                    ],
                                )
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=Qwen/Qwen3-0.6B"],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                )
            ],
        ),
        want="mp-" + "a" * 54 + "-38bfb",
    ),
]


@pytest.mark.parametrize("case", REMOTE_NAMESPACE_CASES, ids=lambda case: case.name)
def test_remote_namespace(case: RemoteNamespaceCase) -> None:
    """A replica's objects land in a namespace mirroring its own."""
    assert base.remote_namespace(case.replica) == case.want, case.reason
