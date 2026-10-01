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
the ResourceClaimTemplates for one worker engine. routing.apply fronts a
replica's engines with an InferencePool, endpoint picker and HTTPRoute.
"""

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
    engine: v1alpha1.Engine
    stack: str
    want: str


@dataclasses.dataclass
class CacheMountsCase:
    """A test case for base.cache_mounts."""

    name: str
    replica: v1alpha1.ModelReplica
    want: tuple[list[dict], list[dict]]


@dataclasses.dataclass
class CacheEnvCase:
    """A test case for base.cache_env."""

    name: str
    replica: v1alpha1.ModelReplica
    want: list[dict]


@dataclasses.dataclass
class ApplyCase:
    """A test case for routing.apply."""

    name: str
    composed: dict[str, dict]
    replica: v1alpha1.ModelReplica
    provider_config: str
    want: dict[str, dict]


@dataclasses.dataclass
class KvBlockSizeCase:
    """A test case for routing._kv_block_size."""

    name: str
    engine_args: list[str]
    want: int


@dataclasses.dataclass
class DisaggregatedEppConfigYamlCase:
    """A test case for routing._disaggregated_epp_config_yaml."""

    name: str
    block_size: int
    want: str


@dataclasses.dataclass
class RemoteNamespaceCase:
    """A test case for base.remote_namespace."""

    name: str
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


def _epp(*, config_checksum: str) -> dict:
    """The Object composing the endpoint picker's Deployment, rolled by its config's checksum."""
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
                                "labels": {"app": "r-epp"},
                                "annotations": {"modelplane.ai/epp-config-checksum": config_checksum},
                            },
                            "spec": {
                                "serviceAccountName": "r-epp",
                                "containers": [
                                    {
                                        "name": "epp",
                                        "image": "ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0",
                                        "args": [
                                            "--pool-name=r-pool",
                                            "--pool-namespace=mp-ml-team-51733",
                                            "--pool-group=inference.networking.k8s.io",
                                            "--config-file=/config/epp-config.yaml",
                                            "--grpc-port=9002",
                                        ],
                                        "ports": [
                                            {"name": "grpc", "containerPort": 9002},
                                            {"name": "grpc-health", "containerPort": 9003},
                                        ],
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


def _main_deployment(
    *,
    name: str,
    claim_template_name: str,
    replicas: int,
    pod_metadata: dict,
    containers: list[dict],
    volumes: list[dict],
) -> dict:
    """The Object composing the main engine's Deployment."""
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


def _prefill_deployment(*, labels: dict, containers: list[dict], volumes: list[dict]) -> dict:
    """The Object composing the prefill engine's Deployment."""
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
                    "metadata": {"name": "r-prefill-d90b0", "namespace": "mp-ml-team-51733"},
                    "spec": {
                        "replicas": 1,
                        "selector": {"matchLabels": {"modelplane.ai/workload": "r-prefill-d90b0"}},
                        "template": {
                            "metadata": {"labels": labels},
                            "spec": {
                                "containers": containers,
                                "volumes": volumes,
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-prefill-standalone-devices-f62af",
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


def _decode_deployment(*, labels: dict, containers: list[dict], volumes: list[dict]) -> dict:
    """The Object composing the decode engine's Deployment."""
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
                    "metadata": {"name": "r-decode-4b27b", "namespace": "mp-ml-team-51733"},
                    "spec": {
                        "replicas": 1,
                        "selector": {"matchLabels": {"modelplane.ai/workload": "r-decode-4b27b"}},
                        "template": {
                            "metadata": {"labels": labels},
                            "spec": {
                                "containers": containers,
                                "volumes": volumes,
                                "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                "resourceClaims": [
                                    {
                                        "name": "devices",
                                        "resourceClaimTemplateName": "r-decode-standalone-devices-63392",
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


def _main_leader_worker_set(
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
                                    "labels": {"modelplane.ai/serving": "r", "modelplane.ai/lws-role": "leader"}
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
                                }
                            },
                        },
                    },
                }
            },
        }
    }


def _main_pod_clique_set(*, cliques: list[dict]) -> dict:
    """The Object composing the main engine's PodCliqueSet, from its leader and worker cliques."""
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
                    "metadata": {"name": "r-main-bb4e3", "namespace": "mp-ml-team-51733"},
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


def _main_standalone_claim_template(*, name: str, requests: list[dict]) -> dict:
    """The Object composing the ResourceClaimTemplate for the main engine's Standalone member."""
    return {
        "spec": {
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
            "readiness": {"policy": "SuccessfulCreate"},
            "forProvider": {
                "manifest": {
                    "apiVersion": "resource.k8s.io/v1",
                    "kind": "ResourceClaimTemplate",
                    "metadata": {"name": name, "namespace": "mp-ml-team-51733"},
                    "spec": {"spec": {"devices": {"requests": requests}}},
                }
            },
        }
    }


def _main_leader_claim_template() -> dict:
    """The Object composing the ResourceClaimTemplate for the main engine's leader, for 8 GPUs."""
    return {
        "spec": {
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
            "readiness": {"policy": "SuccessfulCreate"},
            "forProvider": {
                "manifest": {
                    "apiVersion": "resource.k8s.io/v1",
                    "kind": "ResourceClaimTemplate",
                    "metadata": {"name": "r-main-leader-devices-f58c6", "namespace": "mp-ml-team-51733"},
                    "spec": {
                        "spec": {
                            "devices": {
                                "requests": [
                                    {
                                        "name": "gpu",
                                        "exactly": {
                                            "deviceClassName": "gpu.nvidia.com",
                                            "count": 8,
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


def _main_worker_claim_template() -> dict:
    """The Object composing the ResourceClaimTemplate for the main engine's worker, for 8 GPUs."""
    return {
        "spec": {
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
            "readiness": {"policy": "SuccessfulCreate"},
            "forProvider": {
                "manifest": {
                    "apiVersion": "resource.k8s.io/v1",
                    "kind": "ResourceClaimTemplate",
                    "metadata": {"name": "r-main-worker-devices-99b8a", "namespace": "mp-ml-team-51733"},
                    "spec": {
                        "spec": {
                            "devices": {
                                "requests": [
                                    {
                                        "name": "gpu",
                                        "exactly": {
                                            "deviceClassName": "gpu.nvidia.com",
                                            "count": 8,
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


def _prefill_claim_template() -> dict:
    """The Object composing the ResourceClaimTemplate for the prefill engine's member, for 1 GPU."""
    return {
        "spec": {
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
            "readiness": {"policy": "SuccessfulCreate"},
            "forProvider": {
                "manifest": {
                    "apiVersion": "resource.k8s.io/v1",
                    "kind": "ResourceClaimTemplate",
                    "metadata": {"name": "r-prefill-standalone-devices-f62af", "namespace": "mp-ml-team-51733"},
                    "spec": {
                        "spec": {
                            "devices": {
                                "requests": [
                                    {
                                        "name": "gpu",
                                        "exactly": {
                                            "deviceClassName": "gpu.nvidia.com",
                                            "count": 1,
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


def _decode_claim_template() -> dict:
    """The Object composing the ResourceClaimTemplate for the decode engine's member, for 1 GPU."""
    return {
        "spec": {
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
            "readiness": {"policy": "SuccessfulCreate"},
            "forProvider": {
                "manifest": {
                    "apiVersion": "resource.k8s.io/v1",
                    "kind": "ResourceClaimTemplate",
                    "metadata": {"name": "r-decode-standalone-devices-63392", "namespace": "mp-ml-team-51733"},
                    "spec": {
                        "spec": {
                            "devices": {
                                "requests": [
                                    {
                                        "name": "gpu",
                                        "exactly": {
                                            "deviceClassName": "gpu.nvidia.com",
                                            "count": 1,
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
    name: str,
    namespace: str,
    model_cache_ref: v1alpha1.ModelCacheRef | None,
    serving: v1alpha1.Serving | None,
    engines: list[v1alpha1.Engine],
) -> v1alpha1.ModelReplica:
    """A ModelReplica on cluster-a."""
    return v1alpha1.ModelReplica(
        metadata=metav1.ObjectMeta(name=name, namespace=namespace),
        spec=v1alpha1.SpecModel(
            clusterName="cluster-a",
            modelCacheRef=model_cache_ref,
            serving=serving,
            engines=engines,
        ),
    )


def _unnamed_replica(*, model_cache_ref: v1alpha1.ModelCacheRef | None) -> v1alpha1.ModelReplica:
    """A ModelReplica with no name in namespace ml-team, on cluster c, with one Standalone engine."""
    return v1alpha1.ModelReplica(
        metadata=metav1.ObjectMeta(namespace="ml-team"),
        spec=v1alpha1.SpecModel(
            clusterName="c",
            modelCacheRef=model_cache_ref,
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
    )


def _to_dicts(objs: dict[str, k8sobjv1alpha1.Object]) -> dict[str, dict]:
    """objs as dicts of only the fields the code set."""
    return {key: obj.model_dump(exclude_unset=True, by_alias=True) for key, obj in objs.items()}


def _sorted(d: dict) -> dict:
    """d with its keys sorted, so pytest's diff of two lines them up."""
    return json.loads(json.dumps(d, sort_keys=True))


BUILD_CASES = [
    # A Deployment reports readiness from its Available condition, and a claim
    # template is ready once it's created. The device request's CEL selector,
    # here and throughout, is as compose-model-deployment stamps it.
    BuildCase(
        name="a Standalone engine composes a Deployment",
        backend=native.NativeBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-main": _main_deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-main-standalone": _main_standalone_claim_template(
                name="r-main-standalone-devices-f456f",
                requests=[
                    {
                        "name": "gpu",
                        "exactly": {
                            "deviceClassName": "gpu.nvidia.com",
                            "count": 1,
                            "selectors": [
                                {
                                    "cel": {
                                        "expression": 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                    }
                                }
                            ],
                        },
                    }
                ],
            ),
        },
    ),
    # The member's labels merge with the managed ones (#378).
    BuildCase(
        name="a Standalone member's template metadata lands on its pod template",
        backend=native.NativeBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-main": _main_deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "example.com/role": "standalone",
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
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-main-standalone": _main_standalone_claim_template(
                name="r-main-standalone-devices-f456f",
                requests=[
                    {
                        "name": "gpu",
                        "exactly": {
                            "deviceClassName": "gpu.nvidia.com",
                            "count": 1,
                            "selectors": [
                                {
                                    "cel": {
                                        "expression": 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                    }
                                }
                            ],
                        },
                    }
                ],
            ),
        },
    ),
    # Two replicas of one deployment on the same cluster, this one and
    # dep-clusterB, must compose distinct resource names there.
    BuildCase(
        name="a co-located replica's resource names are qualified by its own name (dep-clusterA)",
        backend=native.NativeBackend(),
        replica=_replica(
            name="dep-clusterA",
            namespace="ml-team",
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
            "model-serving-main": _main_deployment(
                name="dep-clusterA-main-3d1d5",
                claim_template_name="dep-clusterA-main-standalone-devices-145eb",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/serving": "dep-clusterA",
                        "modelplane.ai/workload": "dep-clusterA-main-3d1d5",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-main-standalone": _main_standalone_claim_template(
                name="dep-clusterA-main-standalone-devices-145eb",
                requests=[
                    {
                        "name": "gpu",
                        "exactly": {
                            "deviceClassName": "gpu.nvidia.com",
                            "count": 1,
                            "selectors": [
                                {
                                    "cel": {
                                        "expression": 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                    }
                                }
                            ],
                        },
                    }
                ],
            ),
        },
    ),
    BuildCase(
        name="a co-located replica's resource names are qualified by its own name (dep-clusterB)",
        backend=native.NativeBackend(),
        replica=_replica(
            name="dep-clusterB",
            namespace="ml-team",
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
            "model-serving-main": _main_deployment(
                name="dep-clusterB-main-d6c52",
                claim_template_name="dep-clusterB-main-standalone-devices-5a8a8",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/serving": "dep-clusterB",
                        "modelplane.ai/workload": "dep-clusterB-main-d6c52",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-main-standalone": _main_standalone_claim_template(
                name="dep-clusterB-main-standalone-devices-5a8a8",
                requests=[
                    {
                        "name": "gpu",
                        "exactly": {
                            "deviceClassName": "gpu.nvidia.com",
                            "count": 1,
                            "selectors": [
                                {
                                    "cel": {
                                        "expression": 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                    }
                                }
                            ],
                        },
                    }
                ],
            ),
        },
    ),
    # Workload and claim template names are qualified by the engine, so a
    # multi-engine replica's don't collide on the remote cluster.
    BuildCase(
        name="each engine of a multi-engine replica composes distinctly named resources",
        backend=native.NativeBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-prefill": _prefill_deployment(
                labels={
                    "modelplane.ai/serving": "r",
                    "modelplane.ai/workload": "r-prefill-d90b0",
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-prefill-standalone": _prefill_claim_template(),
            "model-serving-decode": _decode_deployment(
                labels={
                    "modelplane.ai/serving": "r",
                    "modelplane.ai/workload": "r-decode-4b27b",
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-decode-standalone": _decode_claim_template(),
        },
    ),
    # resources.claims is a list-map keyed on name alone, so N device requests
    # mustn't compose N container claims all named "devices". The container
    # references the whole pod claim once, and the template carries every
    # request.
    BuildCase(
        name="several device requests share one container claim",
        backend=native.NativeBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-main": _main_deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-main-standalone": _main_standalone_claim_template(
                name="r-main-standalone-devices-f456f",
                requests=[
                    {
                        "name": "gpu",
                        "exactly": {"deviceClassName": "gpu.nvidia.com", "count": 8},
                    },
                    {
                        "name": "nic",
                        "exactly": {"deviceClassName": "nic.nvidia.com", "count": 8},
                    },
                ],
            ),
        },
    ),
    # HF_HUB_CACHE makes the engine's own --model=<repo> resolve against the
    # cache. Modelplane injects no --model of its own: naming the model is the
    # command's job.
    #
    # On a Standard cluster there's no ModelExpress env and no security context.
    # HF_HUB_CACHE isn't part of the ModelExpress bundle, and applies on every
    # stack.
    BuildCase(
        name="a cache mounts its PVC and points HF_HUB_CACHE at it",
        backend=native.NativeBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-main": _main_deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": [],
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-main-standalone": _main_standalone_claim_template(
                name="r-main-standalone-devices-f456f",
                requests=[
                    {
                        "name": "gpu",
                        "exactly": {
                            "deviceClassName": "gpu.nvidia.com",
                            "count": 1,
                            "selectors": [
                                {
                                    "cel": {
                                        "expression": 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                    }
                                }
                            ],
                        },
                    }
                ],
            ),
        },
    ),
    # Kubernetes expands $(VAR) left to right, so Modelplane's own entries must
    # precede the user's for a user entry to reference them.
    BuildCase(
        name="a Standalone member's own env follows the cache env",
        backend=native.NativeBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-main": _main_deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": [],
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-main-standalone": _main_standalone_claim_template(
                name="r-main-standalone-devices-f456f",
                requests=[
                    {
                        "name": "gpu",
                        "exactly": {
                            "deviceClassName": "gpu.nvidia.com",
                            "count": 1,
                            "selectors": [
                                {
                                    "cel": {
                                        "expression": 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                    }
                                }
                            ],
                        },
                    }
                ],
            ),
        },
    ),
    # A Standalone engine on a Dynamo cluster with a cache is as valid a P2P
    # peer set as a gang, so it gets the ModelExpress env and the IPC_LOCK
    # security context. MX_SERVER_ADDRESS is the per-cluster shared server's
    # well-known Service, qualified by its namespace because the engine runs in
    # its team's. MX_MODEL_REVISION isolates this cache's P2P source identity,
    # qualified by the Modelplane namespace like the cache's PVC name, so two
    # namespaces' caches of the same name can't collide at the cluster's one
    # shared server.
    #
    # HF_HUB_CACHE appears once. It's the cache's own env, and ModelExpress
    # reads it only as a fallback for its cache root.
    BuildCase(
        name="a cached Standalone engine on Dynamo gets the ModelExpress env",
        backend=native.NativeBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-main": _main_deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": [],
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-main-standalone": _main_standalone_claim_template(
                name="r-main-standalone-devices-f456f",
                requests=[
                    {
                        "name": "gpu",
                        "exactly": {
                            "deviceClassName": "gpu.nvidia.com",
                            "count": 1,
                            "selectors": [
                                {
                                    "cel": {
                                        "expression": 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                    }
                                }
                            ],
                        },
                    }
                ],
            ),
        },
    ),
    # The members' commands pass through verbatim, with no flag injection or
    # bootstrap. The worker addresses the leader through
    # $(MODELPLANE_LEADER_ADDRESS), which concatenates Grove's PCSG vars because
    # they vary per gang. The PCS-scoped ones are identical across gangs, and
    # would point every copy at gang 0's leader. There's no MODELPLANE_RANK:
    # Grove exposes no group-wide pod index yet (grove#755), so a gang engine's
    # command computes its own rank from GROVE_PCLQ_POD_INDEX.
    #
    # A PodCliqueSet publishes no Available condition, so its readiness derives
    # from its replica counters. A worker with no template metadata carries only
    # the queue label, and with no cache there's no ModelExpress env or security
    # context.
    BuildCase(
        name="a Leader/Worker engine on Grove composes a PodCliqueSet, commands verbatim",
        backend=grove.GroveBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-main": _main_pod_clique_set(
                cliques=[
                    {
                        "name": "leader",
                        "labels": {
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
                                        "ports": [{"containerPort": 8000}],
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
                        "labels": {"kai.scheduler/queue": "modelplane"},
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
                ]
            ),
            "resource-claim-main-leader": _main_leader_claim_template(),
            "resource-claim-main-worker": _main_worker_claim_template(),
        },
    ),
    BuildCase(
        name="a Grove member's own env follows the leader address alias",
        backend=grove.GroveBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-main": _main_pod_clique_set(
                cliques=[
                    {
                        "name": "leader",
                        "labels": {
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
                                        "ports": [{"containerPort": 8000}],
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
                        "labels": {"kai.scheduler/queue": "modelplane"},
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
                ]
            ),
            "resource-claim-main-leader": _main_leader_claim_template(),
            "resource-claim-main-worker": _main_worker_claim_template(),
        },
    ),
    # Multi-NIC RDMA nodes need VLLM_HOST_IP from status.podIP so the engine
    # binds the right interface (#141).
    BuildCase(
        name="a Grove member's pod-field env passes through",
        backend=grove.GroveBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-main": _main_pod_clique_set(
                cliques=[
                    {
                        "name": "leader",
                        "labels": {
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
                                        "ports": [{"containerPort": 8000}],
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
                        "labels": {"kai.scheduler/queue": "modelplane"},
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
                ]
            ),
            "resource-claim-main-leader": _main_leader_claim_template(),
            "resource-claim-main-worker": _main_worker_claim_template(),
        },
    ),
    # Neither member's metadata leaks into the other's clique. Grove propagates
    # a clique's labels and annotations to its pods (#378).
    BuildCase(
        name="each Grove member's template metadata lands on its own clique",
        backend=grove.GroveBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-main": _main_pod_clique_set(
                cliques=[
                    {
                        "name": "leader",
                        "labels": {
                            "example.com/role": "leader",
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
                                        "ports": [{"containerPort": 8000}],
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
                ]
            ),
            "resource-claim-main-leader": _main_leader_claim_template(),
            "resource-claim-main-worker": _main_worker_claim_template(),
        },
    ),
    # A coordinator-only leader, like a vLLM DP head running
    # --data-parallel-size-local=0, has no deviceRequests. It gets no pod claim,
    # no container claim and no ResourceClaimTemplate, but it still pins to its
    # pool and tolerates the GPU taint.
    BuildCase(
        name="a claimless Grove leader composes no claim, but still pins and tolerates",
        backend=grove.GroveBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-main": _main_pod_clique_set(
                cliques=[
                    {
                        "name": "leader",
                        "labels": {
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
                                        "ports": [{"containerPort": 8000}],
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
                        "labels": {"kai.scheduler/queue": "modelplane"},
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
                ]
            ),
            "resource-claim-main-worker": _main_worker_claim_template(),
        },
    ),
    # The scheduler may split a gang across pools when no single pool satisfies
    # every member, so each member's pods pin to that member's pool rather than
    # an engine-wide one.
    BuildCase(
        name="each Grove member pins to its own pool",
        backend=grove.GroveBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-main": _main_pod_clique_set(
                cliques=[
                    {
                        "name": "leader",
                        "labels": {
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
                                        "ports": [{"containerPort": 8000}],
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
                        "labels": {"kai.scheduler/queue": "modelplane"},
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
                ]
            ),
            "resource-claim-main-leader": _main_leader_claim_template(),
            "resource-claim-main-worker": _main_worker_claim_template(),
        },
    ),
    # Both cliques' HF_HUB_CACHE points at the mount, so their own
    # --model=<repo> resolves against it. Modelplane adds no --model itself.
    BuildCase(
        name="a cache mounts on both Grove cliques",
        backend=grove.GroveBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-main": _main_pod_clique_set(
                cliques=[
                    {
                        "name": "leader",
                        "labels": {
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
                                        "ports": [{"containerPort": 8000}],
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
                        "labels": {"kai.scheduler/queue": "modelplane"},
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
                ]
            ),
            "resource-claim-main-leader": _main_leader_claim_template(),
            "resource-claim-main-worker": _main_worker_claim_template(),
        },
    ),
    # A member that points at the cache with its own flag gets no injected
    # --model.
    BuildCase(
        name="a Grove member with its own command mounts a cache, command verbatim",
        backend=grove.GroveBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-main": _main_pod_clique_set(
                cliques=[
                    {
                        "name": "leader",
                        "labels": {
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
                                        "ports": [{"containerPort": 8000}],
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
                        "labels": {"kai.scheduler/queue": "modelplane"},
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
                ]
            ),
            "resource-claim-main-leader": _main_leader_claim_template(),
            "resource-claim-main-worker": _main_worker_claim_template(),
        },
    ),
    # Each pod publishes itself as a source independently, so the worker gets
    # the ModelExpress env too. The leader address alias leads, ahead of the
    # cache env and the ModelExpress bundle.
    BuildCase(
        name="a cached Grove gang on Dynamo gets the ModelExpress env on both cliques",
        backend=grove.GroveBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-main": _main_pod_clique_set(
                cliques=[
                    {
                        "name": "leader",
                        "labels": {
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
                                        "ports": [{"containerPort": 8000}],
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
                        "labels": {"kai.scheduler/queue": "modelplane"},
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
                ]
            ),
            "resource-claim-main-leader": _main_leader_claim_template(),
            "resource-claim-main-worker": _main_worker_claim_template(),
        },
    ),
    # Only the leader carries the serving label. The worker followers never
    # serve, so they carry no metadata at all. Every gang container leads with
    # the backend-neutral coordination vars aliasing LWS_LEADER_ADDRESS and
    # LWS_WORKER_INDEX. A LeaderWorkerSet reports readiness from its Available
    # condition.
    BuildCase(
        name="a Leader/Worker engine on llm-d composes a LeaderWorkerSet",
        backend=llmd.LLMDBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-main": _main_leader_worker_set(
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
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-main-leader": _main_leader_claim_template(),
            "resource-claim-main-worker": _main_worker_claim_template(),
        },
    ),
    BuildCase(
        name="a LeaderWorkerSet's gang is the leader plus its worker's nodes",
        backend=llmd.LLMDBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-main": _main_leader_worker_set(
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
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-main-leader": _main_leader_claim_template(),
            "resource-claim-main-worker": _main_worker_claim_template(),
        },
    ),
    # Only the native and Grove backends wire ModelExpress, and only on Dynamo,
    # which never selects llm-d. HF_HUB_CACHE is the cache's own env, on every
    # stack.
    BuildCase(
        name="llm-d injects no ModelExpress env, even with a cache",
        backend=llmd.LLMDBackend(),
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-main": _main_leader_worker_set(
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
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-main-leader": _main_leader_claim_template(),
            "resource-claim-main-worker": _main_worker_claim_template(),
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
    assert _sorted(_to_dicts(got)) == _sorted(case.want)


SELECT_BACKEND_CASES = [
    SelectBackendCase(
        name="a Standalone engine on Standard is native",
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
    # A Standalone engine is native regardless of the cluster's stack.
    SelectBackendCase(
        name="a Standalone engine on Dynamo is native",
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
        name="a Leader/Worker engine on Standard is llm-d",
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
        name="a Leader/Worker engine on Dynamo is Grove",
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
    assert base.select_backend(case.engine, case.stack) == case.want


CACHE_MOUNTS_CASES = [
    CacheMountsCase(
        name="no cache mounts nothing",
        replica=_unnamed_replica(model_cache_ref=None),
        want=([], []),
    ),
    CacheMountsCase(
        name="a cache mounts its PVC",
        replica=_unnamed_replica(model_cache_ref=v1alpha1.ModelCacheRef(name="qwen")),
        want=(
            [{"name": "model-cache", "persistentVolumeClaim": {"claimName": "modelcache-ml-team-qwen-17db2"}}],
            [{"name": "model-cache", "mountPath": "/mnt/models"}],
        ),
    ),
]


@pytest.mark.parametrize("case", CACHE_MOUNTS_CASES, ids=lambda case: case.name)
def test_cache_mounts(case: CacheMountsCase) -> None:
    """A replica's cache contributes a volume and a mount."""
    assert base.cache_mounts(case.replica) == case.want


CACHE_ENV_CASES = [
    CacheEnvCase(
        name="no cache sets no env",
        replica=_unnamed_replica(model_cache_ref=None),
        want=[],
    ),
    # The cache is staged in HuggingFace's cache layout, so pointing
    # HF_HUB_CACHE at the mount is what lets an engine's own --model=<repo>
    # resolve against it instead of pulling from HuggingFace (#407). There's no
    # HF_HUB_OFFLINE: it would break an engine that fetches a different repo at
    # startup, like kimi-k2's separately gated tokenizer, and resolution doesn't
    # need it.
    CacheEnvCase(
        name="a cache points HF_HUB_CACHE at the mount",
        replica=_unnamed_replica(model_cache_ref=v1alpha1.ModelCacheRef(name="qwen")),
        want=[{"name": "HF_HUB_CACHE", "value": "/mnt/models"}],
    ),
]


@pytest.mark.parametrize("case", CACHE_ENV_CASES, ids=lambda case: case.name)
def test_cache_env(case: CacheEnvCase) -> None:
    """A replica's cache contributes the env that resolves a model against it."""
    assert base.cache_env(case.replica) == case.want


APPLY_CASES = [
    # PrefillDecode role-labels the two engines, sidecars decode, and fronts
    # both with an InferencePool and endpoint picker rather than a Service. Both
    # engines get the NIXL plumbing the schema can't express: a Memory /dev/shm,
    # and VLLM_NIXL_SIDE_CHANNEL_HOST set to the pod IP. The decode engine moves
    # to port 8001, behind the pd-sidecar on 8000.
    #
    # PrefillDecode silently serves decode-only unless the picker's config arms
    # the prefix-based PD decider. That needs nonCachedTokens > 0, the
    # approx-prefix-cache-producer that populates the attribute it reads, pinned
    # to autoTune: false, and no prepareDataPlugins feature gate, which the
    # v0.8.0 EPP image rejects and crashloops on. The picker watches
    # InferenceObjectives, so its Role must allow that.
    #
    # A wrong picker or sidecar image tag, or a stale EndpointPickerConfig API
    # group, would otherwise only surface as a crashloop at deploy. Pinning them
    # here means a bump shows up to be reviewed.
    ApplyCase(
        name="PrefillDecode fronts prefill and decode engines with a pool and endpoint picker",
        composed={
            "model-serving-prefill": _prefill_deployment(
                labels={
                    "modelplane.ai/serving": "r",
                    "modelplane.ai/workload": "r-prefill-d90b0",
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-prefill-standalone": _prefill_claim_template(),
            "model-serving-decode": _decode_deployment(
                labels={
                    "modelplane.ai/serving": "r",
                    "modelplane.ai/workload": "r-decode-4b27b",
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-decode-standalone": _decode_claim_template(),
        },
        replica=_replica(
            name="r",
            namespace="ml-team",
            model_cache_ref=None,
            serving=v1alpha1.Serving(mode="PrefillDecode"),
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
            "model-serving-prefill": _prefill_deployment(
                labels={
                    "modelplane.ai/serving": "r",
                    "modelplane.ai/workload": "r-prefill-d90b0",
                    "llm-d.ai/role": "prefill",
                    "llm-d.ai/inference-serving": "true",
                    "app": "r",
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                        "env": [
                            {
                                "name": "VLLM_NIXL_SIDE_CHANNEL_HOST",
                                "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}},
                            },
                            {"name": "VLLM_NIXL_SIDE_CHANNEL_PORT", "value": "5557"},
                        ],
                    }
                ],
                volumes=[
                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                    {"name": "nixl-shm", "emptyDir": {"medium": "Memory"}},
                ],
            ),
            "resource-claim-prefill-standalone": _prefill_claim_template(),
            "model-serving-decode": _decode_deployment(
                labels={
                    "modelplane.ai/serving": "r",
                    "modelplane.ai/workload": "r-decode-4b27b",
                    "llm-d.ai/role": "decode",
                    "llm-d.ai/inference-serving": "true",
                    "app": "r",
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"containerPort": 8001}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8001},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                        "env": [
                            {
                                "name": "VLLM_NIXL_SIDE_CHANNEL_HOST",
                                "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}},
                            },
                            {"name": "VLLM_NIXL_SIDE_CHANNEL_PORT", "value": "5557"},
                        ],
                    },
                    {
                        "name": "pd-sidecar",
                        "image": "ghcr.io/llm-d/llm-d-router-disagg-sidecar:v0.9.0",
                        "args": [
                            "--secure-proxy=false",
                            "--kv-connector=nixlv2",
                            "--vllm-port=8001",
                        ],
                        "ports": [{"containerPort": 8000}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                    },
                ],
                volumes=[
                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                    {"name": "nixl-shm", "emptyDir": {"medium": "Memory"}},
                ],
            ),
            "resource-claim-decode-standalone": _decode_claim_template(),
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
                "    blockSizeTokens: 16\n"
                "    maxPrefixBlocksToMatch: 256\n"
                "    lruCapacityPerServer: 31250\n"
                "- type: prefix-cache-scorer\n"
                "- type: disagg-headers-handler\n"
                "- type: queue-scorer\n"
                "- type: prefill-filter\n"
                "- type: decode-filter\n"
                "- type: max-score-picker\n"
                "- type: prefix-based-pd-decider\n"
                "  parameters:\n"
                "    nonCachedTokens: 16\n"
                "- type: disagg-profile-handler\n"
                "  parameters:\n"
                "    deciders:\n"
                "      prefill: prefix-based-pd-decider\n"
                "schedulingProfiles:\n"
                "- name: prefill\n"
                "  plugins:\n"
                "  - pluginRef: prefill-filter\n"
                "  - pluginRef: max-score-picker\n"
                "  - pluginRef: prefix-cache-scorer\n"
                "    weight: 2\n"
                "  - pluginRef: queue-scorer\n"
                "    weight: 1\n"
                "- name: decode\n"
                "  plugins:\n"
                "  - pluginRef: decode-filter\n"
                "  - pluginRef: max-score-picker\n"
                "  - pluginRef: prefix-cache-scorer\n"
                "    weight: 2\n"
                "  - pluginRef: queue-scorer\n"
                "    weight: 1\n"
            ),
            "epp": _epp(config_checksum="f715f3024f37e7042d42628f10cbb04c7da78b96dc65953dac0f289ef2c7ef98"),
            "epp-service": _epp_service(),
        },
    ),
    # The sidecar and the decode container's port track the user's --port rather
    # than a hardcoded one.
    ApplyCase(
        name="the decode port follows the user's --port",
        composed={
            "model-serving-prefill": _prefill_deployment(
                labels={
                    "modelplane.ai/serving": "r",
                    "modelplane.ai/workload": "r-prefill-d90b0",
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-prefill-standalone": _prefill_claim_template(),
            "model-serving-decode": _decode_deployment(
                labels={
                    "modelplane.ai/serving": "r",
                    "modelplane.ai/workload": "r-decode-4b27b",
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=m", "--port=9000"],
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-decode-standalone": _decode_claim_template(),
        },
        replica=_replica(
            name="r",
            namespace="ml-team",
            model_cache_ref=None,
            serving=v1alpha1.Serving(mode="PrefillDecode"),
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
                                            args=["--model=m", "--port=9000"],
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
            "model-serving-prefill": _prefill_deployment(
                labels={
                    "modelplane.ai/serving": "r",
                    "modelplane.ai/workload": "r-prefill-d90b0",
                    "llm-d.ai/role": "prefill",
                    "llm-d.ai/inference-serving": "true",
                    "app": "r",
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                        "env": [
                            {
                                "name": "VLLM_NIXL_SIDE_CHANNEL_HOST",
                                "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}},
                            },
                            {"name": "VLLM_NIXL_SIDE_CHANNEL_PORT", "value": "5557"},
                        ],
                    }
                ],
                volumes=[
                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                    {"name": "nixl-shm", "emptyDir": {"medium": "Memory"}},
                ],
            ),
            "resource-claim-prefill-standalone": _prefill_claim_template(),
            "model-serving-decode": _decode_deployment(
                labels={
                    "modelplane.ai/serving": "r",
                    "modelplane.ai/workload": "r-decode-4b27b",
                    "llm-d.ai/role": "decode",
                    "llm-d.ai/inference-serving": "true",
                    "app": "r",
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=m", "--port=9000"],
                        "ports": [{"containerPort": 9000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 9000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                        "env": [
                            {
                                "name": "VLLM_NIXL_SIDE_CHANNEL_HOST",
                                "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}},
                            },
                            {"name": "VLLM_NIXL_SIDE_CHANNEL_PORT", "value": "5557"},
                        ],
                    },
                    {
                        "name": "pd-sidecar",
                        "image": "ghcr.io/llm-d/llm-d-router-disagg-sidecar:v0.9.0",
                        "args": [
                            "--secure-proxy=false",
                            "--kv-connector=nixlv2",
                            "--vllm-port=9000",
                        ],
                        "ports": [{"containerPort": 8000}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                    },
                ],
                volumes=[
                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                    {"name": "nixl-shm", "emptyDir": {"medium": "Memory"}},
                ],
            ),
            "resource-claim-decode-standalone": _decode_claim_template(),
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
                "    blockSizeTokens: 16\n"
                "    maxPrefixBlocksToMatch: 256\n"
                "    lruCapacityPerServer: 31250\n"
                "- type: prefix-cache-scorer\n"
                "- type: disagg-headers-handler\n"
                "- type: queue-scorer\n"
                "- type: prefill-filter\n"
                "- type: decode-filter\n"
                "- type: max-score-picker\n"
                "- type: prefix-based-pd-decider\n"
                "  parameters:\n"
                "    nonCachedTokens: 16\n"
                "- type: disagg-profile-handler\n"
                "  parameters:\n"
                "    deciders:\n"
                "      prefill: prefix-based-pd-decider\n"
                "schedulingProfiles:\n"
                "- name: prefill\n"
                "  plugins:\n"
                "  - pluginRef: prefill-filter\n"
                "  - pluginRef: max-score-picker\n"
                "  - pluginRef: prefix-cache-scorer\n"
                "    weight: 2\n"
                "  - pluginRef: queue-scorer\n"
                "    weight: 1\n"
                "- name: decode\n"
                "  plugins:\n"
                "  - pluginRef: decode-filter\n"
                "  - pluginRef: max-score-picker\n"
                "  - pluginRef: prefix-cache-scorer\n"
                "    weight: 2\n"
                "  - pluginRef: queue-scorer\n"
                "    weight: 1\n"
            ),
            "epp": _epp(config_checksum="f715f3024f37e7042d42628f10cbb04c7da78b96dc65953dac0f289ef2c7ef98"),
            "epp-service": _epp_service(),
        },
    ),
    # alpha is Decode, so it gets the sidecar, and beta is Prefill, whatever
    # their names suggest.
    ApplyCase(
        name="PrefillDecode takes each engine's role from its phase, not its name",
        composed={
            "model-serving-alpha": {
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
                            "metadata": {"name": "r-alpha-b36ce", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "replicas": 1,
                                "selector": {"matchLabels": {"modelplane.ai/workload": "r-alpha-b36ce"}},
                                "template": {
                                    "metadata": {
                                        "labels": {
                                            "modelplane.ai/serving": "r",
                                            "modelplane.ai/workload": "r-alpha-b36ce",
                                        }
                                    },
                                    "spec": {
                                        "containers": [
                                            {
                                                "name": "engine",
                                                "image": "vllm/vllm-openai:latest",
                                                "args": ["--model=Qwen/Qwen3-0.6B"],
                                                "ports": [{"containerPort": 8000}],
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
                                        "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                                        "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                        "resourceClaims": [
                                            {
                                                "name": "devices",
                                                "resourceClaimTemplateName": "r-alpha-standalone-devices-9ec1c",
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
            },
            "resource-claim-alpha-standalone": {
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "resource.k8s.io/v1",
                            "kind": "ResourceClaimTemplate",
                            "metadata": {"name": "r-alpha-standalone-devices-9ec1c", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "spec": {
                                    "devices": {
                                        "requests": [
                                            {
                                                "name": "gpu",
                                                "exactly": {
                                                    "deviceClassName": "gpu.nvidia.com",
                                                    "count": 1,
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
            },
            "model-serving-beta": {
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
                            "metadata": {"name": "r-beta-52d85", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "replicas": 1,
                                "selector": {"matchLabels": {"modelplane.ai/workload": "r-beta-52d85"}},
                                "template": {
                                    "metadata": {
                                        "labels": {
                                            "modelplane.ai/serving": "r",
                                            "modelplane.ai/workload": "r-beta-52d85",
                                        }
                                    },
                                    "spec": {
                                        "containers": [
                                            {
                                                "name": "engine",
                                                "image": "vllm/vllm-openai:latest",
                                                "args": ["--model=Qwen/Qwen3-0.6B"],
                                                "ports": [{"containerPort": 8000}],
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
                                        "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
                                        "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                        "resourceClaims": [
                                            {
                                                "name": "devices",
                                                "resourceClaimTemplateName": "r-beta-standalone-devices-9d8b1",
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
            },
            "resource-claim-beta-standalone": {
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "resource.k8s.io/v1",
                            "kind": "ResourceClaimTemplate",
                            "metadata": {"name": "r-beta-standalone-devices-9d8b1", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "spec": {
                                    "devices": {
                                        "requests": [
                                            {
                                                "name": "gpu",
                                                "exactly": {
                                                    "deviceClassName": "gpu.nvidia.com",
                                                    "count": 1,
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
            },
        },
        replica=_replica(
            name="r",
            namespace="ml-team",
            model_cache_ref=None,
            serving=v1alpha1.Serving(mode="PrefillDecode"),
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
            "model-serving-alpha": {
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
                            "metadata": {"name": "r-alpha-b36ce", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "replicas": 1,
                                "selector": {"matchLabels": {"modelplane.ai/workload": "r-alpha-b36ce"}},
                                "template": {
                                    "metadata": {
                                        "labels": {
                                            "modelplane.ai/serving": "r",
                                            "modelplane.ai/workload": "r-alpha-b36ce",
                                            "llm-d.ai/role": "decode",
                                            "llm-d.ai/inference-serving": "true",
                                            "app": "r",
                                        }
                                    },
                                    "spec": {
                                        "containers": [
                                            {
                                                "name": "engine",
                                                "image": "vllm/vllm-openai:latest",
                                                "args": ["--model=Qwen/Qwen3-0.6B"],
                                                "ports": [{"containerPort": 8001}],
                                                "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                                                "readinessProbe": {
                                                    "httpGet": {"path": "/health", "port": 8001},
                                                    "initialDelaySeconds": 30,
                                                    "periodSeconds": 10,
                                                    "timeoutSeconds": 5,
                                                },
                                                "resources": {"claims": [{"name": "devices"}]},
                                                "env": [
                                                    {
                                                        "name": "VLLM_NIXL_SIDE_CHANNEL_HOST",
                                                        "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}},
                                                    },
                                                    {"name": "VLLM_NIXL_SIDE_CHANNEL_PORT", "value": "5557"},
                                                ],
                                            },
                                            {
                                                "name": "pd-sidecar",
                                                "image": "ghcr.io/llm-d/llm-d-router-disagg-sidecar:v0.9.0",
                                                "args": [
                                                    "--secure-proxy=false",
                                                    "--kv-connector=nixlv2",
                                                    "--vllm-port=8001",
                                                ],
                                                "ports": [{"containerPort": 8000}],
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
                                            {"name": "nixl-shm", "emptyDir": {"medium": "Memory"}},
                                        ],
                                        "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                        "resourceClaims": [
                                            {
                                                "name": "devices",
                                                "resourceClaimTemplateName": "r-alpha-standalone-devices-9ec1c",
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
            },
            "resource-claim-alpha-standalone": {
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "resource.k8s.io/v1",
                            "kind": "ResourceClaimTemplate",
                            "metadata": {"name": "r-alpha-standalone-devices-9ec1c", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "spec": {
                                    "devices": {
                                        "requests": [
                                            {
                                                "name": "gpu",
                                                "exactly": {
                                                    "deviceClassName": "gpu.nvidia.com",
                                                    "count": 1,
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
            },
            "model-serving-beta": {
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
                            "metadata": {"name": "r-beta-52d85", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "replicas": 1,
                                "selector": {"matchLabels": {"modelplane.ai/workload": "r-beta-52d85"}},
                                "template": {
                                    "metadata": {
                                        "labels": {
                                            "modelplane.ai/serving": "r",
                                            "modelplane.ai/workload": "r-beta-52d85",
                                            "llm-d.ai/role": "prefill",
                                            "llm-d.ai/inference-serving": "true",
                                            "app": "r",
                                        }
                                    },
                                    "spec": {
                                        "containers": [
                                            {
                                                "name": "engine",
                                                "image": "vllm/vllm-openai:latest",
                                                "args": ["--model=Qwen/Qwen3-0.6B"],
                                                "ports": [{"containerPort": 8000}],
                                                "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                                                "readinessProbe": {
                                                    "httpGet": {"path": "/health", "port": 8000},
                                                    "initialDelaySeconds": 30,
                                                    "periodSeconds": 10,
                                                    "timeoutSeconds": 5,
                                                },
                                                "resources": {"claims": [{"name": "devices"}]},
                                                "env": [
                                                    {
                                                        "name": "VLLM_NIXL_SIDE_CHANNEL_HOST",
                                                        "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}},
                                                    },
                                                    {"name": "VLLM_NIXL_SIDE_CHANNEL_PORT", "value": "5557"},
                                                ],
                                            }
                                        ],
                                        "volumes": [
                                            {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                                            {"name": "nixl-shm", "emptyDir": {"medium": "Memory"}},
                                        ],
                                        "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                        "resourceClaims": [
                                            {
                                                "name": "devices",
                                                "resourceClaimTemplateName": "r-beta-standalone-devices-9d8b1",
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
            },
            "resource-claim-beta-standalone": {
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "resource.k8s.io/v1",
                            "kind": "ResourceClaimTemplate",
                            "metadata": {"name": "r-beta-standalone-devices-9d8b1", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "spec": {
                                    "devices": {
                                        "requests": [
                                            {
                                                "name": "gpu",
                                                "exactly": {
                                                    "deviceClassName": "gpu.nvidia.com",
                                                    "count": 1,
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
            },
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
                "    blockSizeTokens: 16\n"
                "    maxPrefixBlocksToMatch: 256\n"
                "    lruCapacityPerServer: 31250\n"
                "- type: prefix-cache-scorer\n"
                "- type: disagg-headers-handler\n"
                "- type: queue-scorer\n"
                "- type: prefill-filter\n"
                "- type: decode-filter\n"
                "- type: max-score-picker\n"
                "- type: prefix-based-pd-decider\n"
                "  parameters:\n"
                "    nonCachedTokens: 16\n"
                "- type: disagg-profile-handler\n"
                "  parameters:\n"
                "    deciders:\n"
                "      prefill: prefix-based-pd-decider\n"
                "schedulingProfiles:\n"
                "- name: prefill\n"
                "  plugins:\n"
                "  - pluginRef: prefill-filter\n"
                "  - pluginRef: max-score-picker\n"
                "  - pluginRef: prefix-cache-scorer\n"
                "    weight: 2\n"
                "  - pluginRef: queue-scorer\n"
                "    weight: 1\n"
                "- name: decode\n"
                "  plugins:\n"
                "  - pluginRef: decode-filter\n"
                "  - pluginRef: max-score-picker\n"
                "  - pluginRef: prefix-cache-scorer\n"
                "    weight: 2\n"
                "  - pluginRef: queue-scorer\n"
                "    weight: 1\n"
            ),
            "epp": _epp(config_checksum="f715f3024f37e7042d42628f10cbb04c7da78b96dc65953dac0f289ef2c7ef98"),
            "epp-service": _epp_service(),
        },
    ),
    # Routing decorates a Grove PodCliqueSet's leader clique with the role and
    # serving labels, the pd-sidecar and the NIXL plumbing, exactly as it does a
    # Deployment's pod template. The worker clique never serves, so routing
    # leaves it as the Grove backend composed it.
    ApplyCase(
        name="a PrefillDecode decode engine can be a Grove gang",
        composed={
            "model-serving-prefill": _prefill_deployment(
                labels={
                    "modelplane.ai/serving": "r",
                    "modelplane.ai/workload": "r-prefill-d90b0",
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-prefill-standalone": _prefill_claim_template(),
            "model-serving-decode": {
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
                            "metadata": {"name": "r-decode-4b27b", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "replicas": 1,
                                "template": {
                                    "cliqueStartupType": "CliqueStartupTypeExplicit",
                                    "terminationDelay": "4h",
                                    "headlessServiceConfig": {"publishNotReadyAddresses": True},
                                    "cliques": [
                                        {
                                            "name": "leader",
                                            "labels": {
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
                                                            "ports": [{"containerPort": 8000}],
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
                                            "labels": {"kai.scheduler/queue": "modelplane"},
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
                                    "podCliqueScalingGroups": [
                                        {
                                            "name": "gang",
                                            "cliqueNames": ["leader", "worker"],
                                            "replicas": 1,
                                            "minAvailable": 1,
                                        }
                                    ],
                                },
                            },
                        }
                    },
                }
            },
            "resource-claim-decode-leader": {
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "resource.k8s.io/v1",
                            "kind": "ResourceClaimTemplate",
                            "metadata": {"name": "r-decode-leader-devices-d2e70", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "spec": {
                                    "devices": {
                                        "requests": [
                                            {
                                                "name": "gpu",
                                                "exactly": {
                                                    "deviceClassName": "gpu.nvidia.com",
                                                    "count": 8,
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
            },
            "resource-claim-decode-worker": {
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "resource.k8s.io/v1",
                            "kind": "ResourceClaimTemplate",
                            "metadata": {"name": "r-decode-worker-devices-08d44", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "spec": {
                                    "devices": {
                                        "requests": [
                                            {
                                                "name": "gpu",
                                                "exactly": {
                                                    "deviceClassName": "gpu.nvidia.com",
                                                    "count": 8,
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
            },
        },
        replica=_replica(
            name="r",
            namespace="ml-team",
            model_cache_ref=None,
            serving=v1alpha1.Serving(mode="PrefillDecode"),
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
            "model-serving-prefill": _prefill_deployment(
                labels={
                    "modelplane.ai/serving": "r",
                    "modelplane.ai/workload": "r-prefill-d90b0",
                    "llm-d.ai/role": "prefill",
                    "llm-d.ai/inference-serving": "true",
                    "app": "r",
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"containerPort": 8000}],
                        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
                        "readinessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                            "timeoutSeconds": 5,
                        },
                        "resources": {"claims": [{"name": "devices"}]},
                        "env": [
                            {
                                "name": "VLLM_NIXL_SIDE_CHANNEL_HOST",
                                "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}},
                            },
                            {"name": "VLLM_NIXL_SIDE_CHANNEL_PORT", "value": "5557"},
                        ],
                    }
                ],
                volumes=[
                    {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                    {"name": "nixl-shm", "emptyDir": {"medium": "Memory"}},
                ],
            ),
            "resource-claim-prefill-standalone": _prefill_claim_template(),
            "model-serving-decode": {
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
                            "metadata": {"name": "r-decode-4b27b", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "replicas": 1,
                                "template": {
                                    "cliqueStartupType": "CliqueStartupTypeExplicit",
                                    "terminationDelay": "4h",
                                    "headlessServiceConfig": {"publishNotReadyAddresses": True},
                                    "cliques": [
                                        {
                                            "name": "leader",
                                            "labels": {
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
                                                                {
                                                                    "name": "VLLM_NIXL_SIDE_CHANNEL_HOST",
                                                                    "valueFrom": {
                                                                        "fieldRef": {"fieldPath": "status.podIP"}
                                                                    },
                                                                },
                                                                {
                                                                    "name": "VLLM_NIXL_SIDE_CHANNEL_PORT",
                                                                    "value": "5557",
                                                                },
                                                            ],
                                                            "ports": [{"containerPort": 8001}],
                                                            "readinessProbe": {
                                                                "httpGet": {"path": "/health", "port": 8001},
                                                                "initialDelaySeconds": 30,
                                                                "periodSeconds": 10,
                                                                "timeoutSeconds": 5,
                                                            },
                                                        },
                                                        {
                                                            "name": "pd-sidecar",
                                                            "image": "ghcr.io/llm-d/llm-d-router-disagg-sidecar:v0.9.0",
                                                            "args": [
                                                                "--secure-proxy=false",
                                                                "--kv-connector=nixlv2",
                                                                "--vllm-port=8001",
                                                            ],
                                                            "ports": [{"containerPort": 8000}],
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
                                                        {"name": "nixl-shm", "emptyDir": {"medium": "Memory"}},
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
                                            "labels": {"kai.scheduler/queue": "modelplane"},
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
                                    "podCliqueScalingGroups": [
                                        {
                                            "name": "gang",
                                            "cliqueNames": ["leader", "worker"],
                                            "replicas": 1,
                                            "minAvailable": 1,
                                        }
                                    ],
                                },
                            },
                        }
                    },
                }
            },
            "resource-claim-decode-leader": {
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "resource.k8s.io/v1",
                            "kind": "ResourceClaimTemplate",
                            "metadata": {"name": "r-decode-leader-devices-d2e70", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "spec": {
                                    "devices": {
                                        "requests": [
                                            {
                                                "name": "gpu",
                                                "exactly": {
                                                    "deviceClassName": "gpu.nvidia.com",
                                                    "count": 8,
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
            },
            "resource-claim-decode-worker": {
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "resource.k8s.io/v1",
                            "kind": "ResourceClaimTemplate",
                            "metadata": {"name": "r-decode-worker-devices-08d44", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "spec": {
                                    "devices": {
                                        "requests": [
                                            {
                                                "name": "gpu",
                                                "exactly": {
                                                    "deviceClassName": "gpu.nvidia.com",
                                                    "count": 8,
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
            },
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
                "    blockSizeTokens: 16\n"
                "    maxPrefixBlocksToMatch: 256\n"
                "    lruCapacityPerServer: 31250\n"
                "- type: prefix-cache-scorer\n"
                "- type: disagg-headers-handler\n"
                "- type: queue-scorer\n"
                "- type: prefill-filter\n"
                "- type: decode-filter\n"
                "- type: max-score-picker\n"
                "- type: prefix-based-pd-decider\n"
                "  parameters:\n"
                "    nonCachedTokens: 16\n"
                "- type: disagg-profile-handler\n"
                "  parameters:\n"
                "    deciders:\n"
                "      prefill: prefix-based-pd-decider\n"
                "schedulingProfiles:\n"
                "- name: prefill\n"
                "  plugins:\n"
                "  - pluginRef: prefill-filter\n"
                "  - pluginRef: max-score-picker\n"
                "  - pluginRef: prefix-cache-scorer\n"
                "    weight: 2\n"
                "  - pluginRef: queue-scorer\n"
                "    weight: 1\n"
                "- name: decode\n"
                "  plugins:\n"
                "  - pluginRef: decode-filter\n"
                "  - pluginRef: max-score-picker\n"
                "  - pluginRef: prefix-cache-scorer\n"
                "    weight: 2\n"
                "  - pluginRef: queue-scorer\n"
                "    weight: 1\n"
            ),
            "epp": _epp(config_checksum="f715f3024f37e7042d42628f10cbb04c7da78b96dc65953dac0f289ef2c7ef98"),
            "epp-service": _epp_service(),
        },
    ),
    # A replica with no serving block is served Unified, here and in the cases
    # that follow.
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
        name="Unified fronts one pod with a pool and endpoint picker",
        composed={
            "model-serving-main": _main_deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-main-standalone": _main_standalone_claim_template(
                name="r-main-standalone-devices-f456f",
                requests=[
                    {
                        "name": "gpu",
                        "exactly": {
                            "deviceClassName": "gpu.nvidia.com",
                            "count": 1,
                            "selectors": [
                                {
                                    "cel": {
                                        "expression": 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                    }
                                }
                            ],
                        },
                    }
                ],
            ),
        },
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-main": _main_deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=1,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-main-standalone": _main_standalone_claim_template(
                name="r-main-standalone-devices-f456f",
                requests=[
                    {
                        "name": "gpu",
                        "exactly": {
                            "deviceClassName": "gpu.nvidia.com",
                            "count": 1,
                            "selectors": [
                                {
                                    "cel": {
                                        "expression": 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                    }
                                }
                            ],
                        },
                    }
                ],
            ),
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
                "    blockSizeTokens: 16\n"
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
            "epp": _epp(config_checksum="20c1dfea3fc4ad41e335cc74edbeb1e8689a607bc4bf7395849ffd2cf0bb2ae1"),
            "epp-service": _epp_service(),
        },
    ),
    ApplyCase(
        name="Unified fronts several pods with a pool and endpoint picker",
        composed={
            "model-serving-main": _main_deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=2,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-main-standalone": _main_standalone_claim_template(
                name="r-main-standalone-devices-f456f",
                requests=[
                    {
                        "name": "gpu",
                        "exactly": {
                            "deviceClassName": "gpu.nvidia.com",
                            "count": 1,
                            "selectors": [
                                {
                                    "cel": {
                                        "expression": 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                    }
                                }
                            ],
                        },
                    }
                ],
            ),
        },
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-main": _main_deployment(
                name="r-main-bb4e3",
                claim_template_name="r-main-standalone-devices-f456f",
                replicas=2,
                pod_metadata={
                    "labels": {
                        "modelplane.ai/serving": "r",
                        "modelplane.ai/workload": "r-main-bb4e3",
                    }
                },
                containers=[
                    {
                        "name": "engine",
                        "image": "vllm/vllm-openai:latest",
                        "args": ["--model=Qwen/Qwen3-0.6B"],
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-main-standalone": _main_standalone_claim_template(
                name="r-main-standalone-devices-f456f",
                requests=[
                    {
                        "name": "gpu",
                        "exactly": {
                            "deviceClassName": "gpu.nvidia.com",
                            "count": 1,
                            "selectors": [
                                {
                                    "cel": {
                                        "expression": 'device.capacity["gpu.nvidia.com"].memory.compareTo(quantity("80Gi")) >= 0'
                                    }
                                }
                            ],
                        },
                    }
                ],
            ),
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
                "    blockSizeTokens: 16\n"
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
            "epp": _epp(config_checksum="20c1dfea3fc4ad41e335cc74edbeb1e8689a607bc4bf7395849ffd2cf0bb2ae1"),
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
        name="Unified fronts a LeaderWorkerSet",
        composed={
            "model-serving-main": _main_leader_worker_set(
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
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-main-leader": _main_leader_claim_template(),
            "resource-claim-main-worker": _main_worker_claim_template(),
        },
        replica=_replica(
            name="r",
            namespace="ml-team",
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
            "model-serving-main": _main_leader_worker_set(
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
                        "ports": [{"containerPort": 8000}],
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
            "resource-claim-main-leader": _main_leader_claim_template(),
            "resource-claim-main-worker": _main_worker_claim_template(),
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
                "    blockSizeTokens: 16\n"
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
            "epp": _epp(config_checksum="20c1dfea3fc4ad41e335cc74edbeb1e8689a607bc4bf7395849ffd2cf0bb2ae1"),
            "epp-service": _epp_service(),
        },
    ),
]


@pytest.mark.parametrize("case", APPLY_CASES, ids=lambda case: case.name)
def test_apply(case: ApplyCase) -> None:
    """routing.apply fronts a replica's engines with the routing its serving mode selects."""
    composed = {key: k8sobjv1alpha1.Object.model_validate(obj) for key, obj in case.composed.items()}
    got = routing.apply(composed, case.replica, case.provider_config)
    assert _sorted(_to_dicts(got)) == _sorted(case.want)


# The EPP prefix-cache producer's blockSizeTokens is derived best-effort from
# the engine flags (#179), so it matches the engine's KV block size.
KV_BLOCK_SIZE_CASES = [
    KvBlockSizeCase(
        name="defaults to 16 with no args",
        engine_args=[],
        want=16,
    ),
    KvBlockSizeCase(
        name="defaults to 16 with no block size flag",
        engine_args=["--model=/mnt/models"],
        want=16,
    ),
    KvBlockSizeCase(
        name="reads vLLM's --block-size",
        engine_args=["--block-size", "32"],
        want=32,
    ),
    KvBlockSizeCase(
        name="reads vLLM's --block-size=",
        engine_args=["--model=/m", "--block-size=8"],
        want=8,
    ),
    KvBlockSizeCase(
        name="reads SGLang's --page-size=",
        engine_args=["--page-size=64"],
        want=64,
    ),
    KvBlockSizeCase(
        name="a non-integer block size falls back to 16",
        engine_args=["--block-size", "auto"],
        want=16,
    ),
]


@pytest.mark.parametrize("case", KV_BLOCK_SIZE_CASES, ids=lambda case: case.name)
def test_kv_block_size(case: KvBlockSizeCase) -> None:
    """The KV block size comes from the engine's flags."""
    assert routing._kv_block_size(case.engine_args) == case.want


DISAGGREGATED_EPP_CONFIG_YAML_CASES = [
    DisaggregatedEppConfigYamlCase(
        name="renders the block size in place of its placeholder",
        block_size=32,
        want=(
            "apiVersion: llm-d.ai/v1alpha1\n"
            "kind: EndpointPickerConfig\n"
            "plugins:\n"
            "- type: approx-prefix-cache-producer\n"
            "  parameters:\n"
            "    autoTune: false\n"
            "    blockSizeTokens: 32\n"
            "    maxPrefixBlocksToMatch: 256\n"
            "    lruCapacityPerServer: 31250\n"
            "- type: prefix-cache-scorer\n"
            "- type: disagg-headers-handler\n"
            "- type: queue-scorer\n"
            "- type: prefill-filter\n"
            "- type: decode-filter\n"
            "- type: max-score-picker\n"
            "- type: prefix-based-pd-decider\n"
            "  parameters:\n"
            "    nonCachedTokens: 16\n"
            "- type: disagg-profile-handler\n"
            "  parameters:\n"
            "    deciders:\n"
            "      prefill: prefix-based-pd-decider\n"
            "schedulingProfiles:\n"
            "- name: prefill\n"
            "  plugins:\n"
            "  - pluginRef: prefill-filter\n"
            "  - pluginRef: max-score-picker\n"
            "  - pluginRef: prefix-cache-scorer\n"
            "    weight: 2\n"
            "  - pluginRef: queue-scorer\n"
            "    weight: 1\n"
            "- name: decode\n"
            "  plugins:\n"
            "  - pluginRef: decode-filter\n"
            "  - pluginRef: max-score-picker\n"
            "  - pluginRef: prefix-cache-scorer\n"
            "    weight: 2\n"
            "  - pluginRef: queue-scorer\n"
            "    weight: 1\n"
        ),
    ),
]


@pytest.mark.parametrize("case", DISAGGREGATED_EPP_CONFIG_YAML_CASES, ids=lambda case: case.name)
def test_disaggregated_epp_config_yaml(case: DisaggregatedEppConfigYamlCase) -> None:
    """The disaggregated EPP config renders with the engine's KV block size."""
    assert routing._disaggregated_epp_config_yaml(case.block_size) == case.want


# compose-inference-cluster creates the namespace a replica's objects land in,
# and compose-model-route and compose-model-cache land objects in it by the same
# derivation, so all four must agree on it.
REMOTE_NAMESPACE_CASES = [
    RemoteNamespaceCase(
        name="a short namespace keeps its name, prefixed and hashed",
        replica=_replica(
            name="r",
            namespace="ml-team",
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
    # is. It's truncated to leave room for the hash, to 63 characters. The
    # lengths are the point, so the names are written as repeats rather than
    # 63-character literals.
    RemoteNamespaceCase(
        name="the longest valid namespace still yields a valid one",
        replica=_replica(
            name="r",
            namespace="a" * 63,
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
    assert base.remote_namespace(case.replica) == case.want
