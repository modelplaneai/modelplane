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

"""Tests for the compose-model-replica function.

Every resource the function composes on the remote cluster lands in
mp-ml-team-51733, the namespace mirroring the replica's.
"""

import asyncio
import dataclasses
import json
from typing import Any

import pytest
from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format, message
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-model-replica."""

    name: str
    reason: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _model_replica(*, copies: int, model_cache_ref: str | None) -> fnv1.Resource:
    """The ModelReplica XR test-replica in ml-team, with one Standalone engine, reading the named ModelCache if any.

    The device request's CEL selector is as compose-model-deployment stamps it.
    """
    xr = v1alpha1.ModelReplica(
        metadata=metav1.ObjectMeta(
            name="test-replica",
            namespace="ml-team",
            labels={
                "modelplane.ai/deployment": "my-deployment",
                "modelplane.ai/cluster": "cluster-a",
            },
        ),
        spec=v1alpha1.SpecModel1(
            clusterName="cluster-a",
            modelCacheRef=v1alpha1.ModelCacheRef(name=model_cache_ref) if model_cache_ref else None,
            engines=[
                v1alpha1.Engine(
                    name="main",
                    copies=copies,
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
                                ),
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=Qwen/Qwen3-0.6B"],
                                        ),
                                    ],
                                ),
                            ),
                        ),
                    ],
                ),
            ],
        ),
    )
    return fnv1.Resource(resource=resource.dict_to_struct(xr.model_dump(exclude_none=True, mode="json", by_alias=True)))


def _prefill_decode_replica(*, serving: v1alpha1.Serving) -> fnv1.Resource:
    """The ModelReplica XR test-replica in ml-team, serving PrefillDecode from a prefill and a decode engine.

    Each engine's pod runs only its engine container, so the decode pod has no
    sidecar.
    """
    xr = v1alpha1.ModelReplica(
        metadata=metav1.ObjectMeta(
            name="test-replica",
            namespace="ml-team",
            labels={
                "modelplane.ai/deployment": "my-deployment",
                "modelplane.ai/cluster": "cluster-a",
            },
        ),
        spec=v1alpha1.SpecModel1(
            clusterName="cluster-a",
            serving=serving,
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
                                ),
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=Qwen/Qwen3-0.6B"],
                                        ),
                                    ],
                                ),
                            ),
                        ),
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
                                ),
                            ],
                            template=v1alpha1.Template(
                                spec=v1alpha1.Spec(
                                    containers=[
                                        v1alpha1.Container(
                                            name="engine",
                                            image="vllm/vllm-openai:latest",
                                            args=["--model=Qwen/Qwen3-0.6B"],
                                        ),
                                    ],
                                ),
                            ),
                        ),
                    ],
                ),
            ],
        ),
    )
    return fnv1.Resource(resource=resource.dict_to_struct(xr.model_dump(exclude_none=True, mode="json", by_alias=True)))


def _cluster(*, provider_config_ref: str | None) -> fnv1.Resource:
    """The InferenceCluster cluster-a, with no status until it reports a providerConfigRef."""
    cluster = {
        "apiVersion": "modelplane.ai/v1alpha1",
        "kind": "InferenceCluster",
        "metadata": {"name": "cluster-a"},
        "spec": {
            "cluster": {"source": "Existing", "existing": {"secretRef": {"name": "k"}}},
        },
    }
    if provider_config_ref is not None:
        cluster["status"] = {
            "providerConfigRef": {"name": provider_config_ref},
            "gateway": {"address": "10.0.0.1"},
        }
    return fnv1.Resource(resource=resource.dict_to_struct(cluster))


def _deployment(
    *, ready: fnv1.Ready, name: str, claim_template_name: str, labels: dict, copies: int, cache_claim: str | None
) -> fnv1.Resource:
    """The Object composing an engine's Deployment, mounting the cache claim if any.

    Its pod carries the labels the collector attributes the engine's metrics by:
    the deployment, the engine and the member's role.
    """
    engine: dict[str, Any] = {
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
    volumes: list[dict] = [{"name": "dshm", "emptyDir": {"medium": "Memory"}}]
    if cache_claim is not None:
        engine["volumeMounts"].append({"name": "model-cache", "mountPath": "/mnt/models"})
        engine["env"] = [{"name": "HF_HUB_CACHE", "value": "/mnt/models"}]
        volumes.append({"name": "model-cache", "persistentVolumeClaim": {"claimName": cache_claim}})
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                    "readiness": {
                        "policy": "DeriveFromCelQuery",
                        "celQuery": (
                            'has(object.status.conditions) && object.status.conditions.exists(c, c.type == "Available" && c.status == "True")'
                            " && has(object.status.readyReplicas) && object.status.readyReplicas >= object.spec.replicas"
                        ),
                    },
                    "watch": True,
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "apps/v1",
                            "kind": "Deployment",
                            "metadata": {"name": name, "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "replicas": copies,
                                "selector": {"matchLabels": {"modelplane.ai/workload": name}},
                                "template": {
                                    "metadata": {"labels": labels},
                                    "spec": {
                                        "containers": [engine],
                                        "volumes": volumes,
                                        "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                        "resourceClaims": [
                                            {
                                                "name": "devices",
                                                "resourceClaimTemplateName": claim_template_name,
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
                        }
                    },
                },
            }
        ),
        ready=ready,
    )


def _route(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The Object composing the HTTPRoute to the InferencePool."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "gateway.networking.k8s.io/v1",
                            "kind": "HTTPRoute",
                            "metadata": {"name": "test-replica", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "parentRefs": [{"name": "cluster-gateway", "namespace": "modelplane-system"}],
                                "rules": [
                                    {
                                        "matches": [
                                            {
                                                "path": {
                                                    "type": "PathPrefix",
                                                    "value": "/ml-team/test-replica/",
                                                }
                                            }
                                        ],
                                        "timeouts": {"request": "0s"},
                                        "filters": [
                                            {
                                                "type": "URLRewrite",
                                                "urlRewrite": {
                                                    "path": {
                                                        "type": "ReplacePrefixMatch",
                                                        "replacePrefixMatch": "/",
                                                    }
                                                },
                                            }
                                        ],
                                        "backendRefs": [
                                            {
                                                "group": "inference.networking.k8s.io",
                                                "kind": "InferencePool",
                                                "name": "test-replica-pool",
                                            }
                                        ],
                                    }
                                ],
                            },
                        }
                    },
                },
            }
        ),
        ready=ready,
    )


def _claim_template(*, ready: fnv1.Ready, name: str) -> fnv1.Resource:
    """The Object composing an engine's ResourceClaimTemplate."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
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
                },
            }
        ),
        ready=ready,
    )


def _inference_pool(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The Object composing the InferencePool fronting the engine."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "inference.networking.k8s.io/v1",
                            "kind": "InferencePool",
                            "metadata": {"name": "test-replica-pool", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "selector": {"matchLabels": {"modelplane.ai/serving": "test-replica"}},
                                "targetPorts": [{"number": 8000}],
                                "endpointPickerRef": {"name": "test-replica-epp", "port": {"number": 9002}},
                            },
                        }
                    },
                },
            }
        ),
        ready=ready,
    )


def _epp(*, ready: fnv1.Ready, config_checksum: str) -> fnv1.Resource:
    """The Object composing the endpoint picker's Deployment, running llm-d's picker with a config.

    Its pod carries the labels the collector attributes the picker's metrics by:
    the deployment and the picker role. The picker's command line takes the pool
    and the config's path from the environment Modelplane sets.
    """
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                    "watch": True,
                    "readiness": {
                        "policy": "DeriveFromCelQuery",
                        "celQuery": (
                            'has(object.status.conditions) && object.status.conditions.exists(c, c.type == "Available" && c.status == "True")'
                            ' && !object.status.conditions.exists(c, c.type == "Progressing" && c.status == "False")'
                        ),
                    },
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "apps/v1",
                            "kind": "Deployment",
                            "metadata": {"name": "test-replica-epp", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "replicas": 1,
                                "selector": {"matchLabels": {"app": "test-replica-epp"}},
                                "template": {
                                    "metadata": {
                                        "labels": {
                                            "app": "test-replica-epp",
                                            "modelplane.ai/role": "picker",
                                            "modelplane.ai/deployment": "my-deployment",
                                        },
                                        "annotations": {"modelplane.ai/epp-config-checksum": config_checksum},
                                    },
                                    "spec": {
                                        "serviceAccountName": "test-replica-epp",
                                        "containers": [
                                            {
                                                "name": "epp",
                                                "image": "ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0",
                                                "args": [
                                                    "--pool-name=$(MODELPLANE_POOL_NAME)",
                                                    "--pool-namespace=$(MODELPLANE_POOL_NAMESPACE)",
                                                    "--pool-group=inference.networking.k8s.io",
                                                    "--config-file=$(MODELPLANE_PICKER_CONFIG)",
                                                    "--grpc-port=$(MODELPLANE_GRPC_PORT)",
                                                    "--grpc-health-port=$(MODELPLANE_GRPC_HEALTH_PORT)",
                                                    "--metrics-port=$(MODELPLANE_METRICS_PORT)",
                                                    "--metrics-endpoint-auth=false",
                                                ],
                                                "env": [
                                                    {"name": "MODELPLANE_POOL_NAME", "value": "test-replica-pool"},
                                                    {"name": "MODELPLANE_POOL_NAMESPACE", "value": "mp-ml-team-51733"},
                                                    {"name": "MODELPLANE_GRPC_PORT", "value": "9002"},
                                                    {"name": "MODELPLANE_GRPC_HEALTH_PORT", "value": "9003"},
                                                    {"name": "MODELPLANE_METRICS_PORT", "value": "9090"},
                                                    {
                                                        "name": "MODELPLANE_PICKER_CONFIG",
                                                        "value": "/config/epp-config.yaml",
                                                    },
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
                                        "volumes": [
                                            {
                                                "name": "config",
                                                "configMap": {"name": "test-replica-epp"},
                                            }
                                        ],
                                    },
                                },
                            },
                        }
                    },
                },
            }
        ),
        ready=ready,
    )


def _epp_config(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The Object composing the default endpoint picker's ConfigMap, for an engine with the default block size."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "v1",
                            "kind": "ConfigMap",
                            "metadata": {"name": "test-replica-epp", "namespace": "mp-ml-team-51733"},
                            "data": {
                                "epp-config.yaml": (
                                    "apiVersion: llm-d.ai/v1alpha1\n"
                                    "kind: EndpointPickerConfig\n"
                                    "plugins:\n"
                                    "- type: approx-prefix-cache-producer\n"
                                    "  parameters:\n"
                                    "    autoTune: false\n"
                                    "    blockSizeTokens: 64\n"
                                    "    maxPrefixTokensToMatch: 131072\n"
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
                                )
                            },
                        }
                    },
                },
            }
        ),
        ready=ready,
    )


def _epp_role(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The Object composing the endpoint picker's Role."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "rbac.authorization.k8s.io/v1",
                            "kind": "Role",
                            "metadata": {"name": "test-replica-epp", "namespace": "mp-ml-team-51733"},
                            "rules": [
                                {
                                    "apiGroups": [""],
                                    "resources": ["pods"],
                                    "verbs": ["get", "watch", "list"],
                                },
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
                },
            }
        ),
        ready=ready,
    )


def _epp_role_binding(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The Object composing the endpoint picker's RoleBinding."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "rbac.authorization.k8s.io/v1",
                            "kind": "RoleBinding",
                            "metadata": {"name": "test-replica-epp", "namespace": "mp-ml-team-51733"},
                            "subjects": [
                                {
                                    "kind": "ServiceAccount",
                                    "name": "test-replica-epp",
                                    "namespace": "mp-ml-team-51733",
                                }
                            ],
                            "roleRef": {
                                "apiGroup": "rbac.authorization.k8s.io",
                                "kind": "Role",
                                "name": "test-replica-epp",
                            },
                        }
                    },
                },
            }
        ),
        ready=ready,
    )


def _epp_service_account(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The Object composing the endpoint picker's ServiceAccount."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "v1",
                            "kind": "ServiceAccount",
                            "metadata": {"name": "test-replica-epp", "namespace": "mp-ml-team-51733"},
                        }
                    },
                },
            }
        ),
        ready=ready,
    )


def _epp_service(*, ready: fnv1.Ready) -> fnv1.Resource:
    """The Object composing the endpoint picker's Service."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                    "readiness": {"policy": "SuccessfulCreate"},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "v1",
                            "kind": "Service",
                            "metadata": {"name": "test-replica-epp", "namespace": "mp-ml-team-51733"},
                            "spec": {
                                "selector": {"app": "test-replica-epp"},
                                "ports": [
                                    {
                                        "name": "grpc-ext-proc",
                                        "port": 9002,
                                        "targetPort": 9002,
                                        "appProtocol": "http2",
                                    }
                                ],
                            },
                        }
                    },
                },
            }
        ),
        ready=ready,
    )


def _observed_deployment(*, ready: bool, ready_replicas: int, cache_claim: str | None) -> fnv1.Resource:
    """The engine's Deployment Object as observed back, its pods mounting the cache claim if any.

    ready sets the Object's derived Ready condition, and ready_replicas the
    Deployment's status.readyReplicas, which a Deployment leaves out at 0.
    """
    observed_manifest: dict = {"kind": "Deployment"}
    if ready_replicas:
        observed_manifest["status"] = {"readyReplicas": ready_replicas}
    engine: dict[str, Any] = {
        "name": "engine",
        "image": "vllm/vllm-openai:latest",
        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
    }
    volumes: list[dict] = [{"name": "dshm", "emptyDir": {"medium": "Memory"}}]
    if cache_claim is not None:
        engine["volumeMounts"].append({"name": "model-cache", "mountPath": "/mnt/models"})
        engine["env"] = [{"name": "HF_HUB_CACHE", "value": "/mnt/models"}]
        volumes.append({"name": "model-cache", "persistentVolumeClaim": {"claimName": cache_claim}})
    applied_manifest = {
        "kind": "Deployment",
        "spec": {"template": {"spec": {"containers": [engine], "volumes": volumes}}},
    }
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {"forProvider": {"manifest": applied_manifest}},
                "status": {
                    "atProvider": {"manifest": observed_manifest},
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "True" if ready else "False",
                            "reason": "Available" if ready else "Unavailable",
                            "lastTransitionTime": "2025-01-01T00:00:00Z",
                        },
                    ],
                },
            }
        ),
    )


# One helper plays nine roles: the observed route, claim template, InferencePool
# and six endpoint picker objects. The function reads only an observed Object's
# Ready condition, so all nine have the same shape, and writing out the seven
# that appear in only two cases inline would bury those cases.
def _observed_object(*, ready: bool) -> fnv1.Resource:
    """A composed Object as observed back, with the Ready condition its readiness policy derives."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "status": {
                    "conditions": [
                        {
                            "type": "Ready",
                            "status": "True" if ready else "False",
                            "reason": "Available" if ready else "Unavailable",
                            "lastTransitionTime": "2025-01-01T00:00:00Z",
                        },
                    ],
                },
            }
        ),
    )


def _model_cache(*, clusters: list[dict]) -> fnv1.Resource:
    """The ModelCache qwen in ml-team, reporting clusters as its per-cluster status."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "modelplane.ai/v1alpha1",
                "kind": "ModelCache",
                "metadata": {"name": "qwen", "namespace": "ml-team"},
                "spec": {"source": "HuggingFace", "huggingFace": {"repo": "Qwen/Qwen3-0.6B", "sizeGiB": 20}},
                "status": {"clusters": clusters},
            }
        )
    )


def _cache_mount(*, claim_name: str) -> dict:
    """The mount a HuggingFace ModelCache publishes for a cluster it has staged the model on."""
    return {
        "volumes": [{"name": "model-cache", "persistentVolumeClaim": {"claimName": claim_name}}],
        "volumeMounts": [{"name": "model-cache", "mountPath": "/mnt/models"}],
        "env": [{"name": "HF_HUB_CACHE", "value": "/mnt/models"}],
    }


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


COMPOSE_CASES = [
    # A fatal result leaves the replica's existing resources as they are, so
    # nothing is composed. Crossplane marks any condition the function doesn't
    # set Unknown, so the conditions still report the pair's observed state.
    Case(
        name="PrefillDecodeRoutingMissing",
        reason=(
            "A stored PrefillDecode replica without a router or picker that still serves stops with a fatal result, "
            "composes nothing, and reports itself accepted and serving."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_prefill_decode_replica(serving=v1alpha1.Serving(mode="PrefillDecode")),
                resources={
                    "model-serving-prefill": _observed_deployment(ready=True, ready_replicas=1, cache_claim=None),
                    "model-serving-decode": _observed_deployment(ready=True, ready_replicas=1, cache_claim=None),
                },
            ),
            required_resources={"cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")])},
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message="PrefillDecode serving requires serving.router: EndpointPicker and serving.endpointPicker",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_TRUE, reason="Accepted"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_TRUE, reason="Serving"),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="AllCopiesReady",
                    message="2 of 2 engine copies ready",
                ),
            ],
        ),
    ),
    Case(
        name="DecodeEngineDown",
        reason=(
            "A stored PrefillDecode replica whose decode engine has no ready copy isn't serving, though its "
            "prefill engine is up."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_prefill_decode_replica(serving=v1alpha1.Serving(mode="PrefillDecode")),
                resources={
                    "model-serving-prefill": _observed_deployment(ready=True, ready_replicas=1, cache_claim=None),
                    "model-serving-decode": _observed_deployment(ready=False, ready_replicas=0, cache_claim=None),
                },
            ),
            required_resources={"cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")])},
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message="PrefillDecode serving requires serving.router: EndpointPicker and serving.endpointPicker",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_TRUE, reason="Accepted"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="ModelStarting"),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="1 of 2 engine copies ready",
                ),
            ],
        ),
    ),
    Case(
        name="PrefillDecodePickerMissing",
        reason=(
            "A PrefillDecode replica with router EndpointPicker but no endpointPicker stops with a fatal result and "
            "composes nothing."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_prefill_decode_replica(
                    serving=v1alpha1.Serving(mode="PrefillDecode", router="EndpointPicker")
                )
            ),
            required_resources={"cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")])},
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_FATAL,
                    message="PrefillDecode serving requires serving.router: EndpointPicker and serving.endpointPicker",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_FALSE, reason="Deploying"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForModel"),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForModel",
                    message="0 of 2 engine copies ready",
                ),
            ],
        ),
    ),
    # Modelplane can't tell which container is the disaggregation sidecar, so
    # it composes the pair as written and warns. The warning comes before the
    # first reconcile's Composing event.
    Case(
        name="DecodeWithoutSidecar",
        reason=(
            "A PrefillDecode replica whose decode pod runs only its engine composes its engines, pool and picker, "
            "and warns that every request skips prefill."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_prefill_decode_replica(
                    serving=v1alpha1.Serving(
                        mode="PrefillDecode",
                        router="EndpointPicker",
                        endpointPicker=v1alpha1.EndpointPicker(
                            template=v1alpha1.TemplateModel(
                                spec=v1alpha1.SpecModel(
                                    containers=[
                                        v1alpha1.ContainerModel(
                                            name="epp",
                                            image="ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0",
                                            args=[
                                                "--pool-name=$(MODELPLANE_POOL_NAME)",
                                                "--pool-namespace=$(MODELPLANE_POOL_NAMESPACE)",
                                                "--pool-group=inference.networking.k8s.io",
                                                "--config-file=$(MODELPLANE_PICKER_CONFIG)",
                                                "--grpc-port=$(MODELPLANE_GRPC_PORT)",
                                                "--grpc-health-port=$(MODELPLANE_GRPC_HEALTH_PORT)",
                                                "--metrics-port=$(MODELPLANE_METRICS_PORT)",
                                                "--metrics-endpoint-auth=false",
                                            ],
                                        )
                                    ]
                                )
                            ),
                            config={"apiVersion": "llm-d.ai/v1alpha1", "kind": "EndpointPickerConfig"},
                        ),
                    )
                )
            ),
            required_resources={"cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")])},
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "model-serving-prefill": _deployment(
                        cache_claim=None,
                        copies=1,
                        ready=fnv1.READY_UNSPECIFIED,
                        name="test-replica-prefill-10d7e",
                        claim_template_name="test-replica-prefill-standalone-devices-51762",
                        labels={
                            "modelplane.ai/deployment": "my-deployment",
                            "modelplane.ai/engine": "prefill",
                            "modelplane.ai/phase": "Prefill",
                            "modelplane.ai/role": "Standalone",
                            "modelplane.ai/serving": "test-replica",
                            "modelplane.ai/workload": "test-replica-prefill-10d7e",
                            "llm-d.ai/role": "prefill",
                        },
                    ),
                    "resource-claim-prefill-standalone": _claim_template(
                        ready=fnv1.READY_UNSPECIFIED, name="test-replica-prefill-standalone-devices-51762"
                    ),
                    "model-serving-decode": _deployment(
                        cache_claim=None,
                        copies=1,
                        ready=fnv1.READY_UNSPECIFIED,
                        name="test-replica-decode-44185",
                        claim_template_name="test-replica-decode-standalone-devices-e863a",
                        labels={
                            "modelplane.ai/deployment": "my-deployment",
                            "modelplane.ai/engine": "decode",
                            "modelplane.ai/phase": "Decode",
                            "modelplane.ai/role": "Standalone",
                            "modelplane.ai/serving": "test-replica",
                            "modelplane.ai/workload": "test-replica-decode-44185",
                            "llm-d.ai/role": "decode",
                        },
                    ),
                    "resource-claim-decode-standalone": _claim_template(
                        ready=fnv1.READY_UNSPECIFIED, name="test-replica-decode-standalone-devices-e863a"
                    ),
                    "model-route": _route(ready=fnv1.READY_UNSPECIFIED),
                    "inference-pool": _inference_pool(ready=fnv1.READY_UNSPECIFIED),
                    "epp": _epp(
                        ready=fnv1.READY_UNSPECIFIED,
                        config_checksum="cea77cf711f4c10cb373847380535aa103899d74fa07e9a914cacc6ed05cd479",
                    ),
                    # The user's config, rendered as JSON with its keys sorted.
                    "epp-config": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "spec": {
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                                    "readiness": {"policy": "SuccessfulCreate"},
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "v1",
                                            "kind": "ConfigMap",
                                            "metadata": {"name": "test-replica-epp", "namespace": "mp-ml-team-51733"},
                                            "data": {
                                                "epp-config.yaml": (
                                                    "{\n"
                                                    '  "apiVersion": "llm-d.ai/v1alpha1",\n'
                                                    '  "kind": "EndpointPickerConfig"\n'
                                                    "}"
                                                )
                                            },
                                        }
                                    },
                                },
                            }
                        ),
                    ),
                    "epp-role": _epp_role(ready=fnv1.READY_UNSPECIFIED),
                    "epp-rolebinding": _epp_role_binding(ready=fnv1.READY_UNSPECIFIED),
                    "epp-serviceaccount": _epp_service_account(ready=fnv1.READY_UNSPECIFIED),
                    "epp-service": _epp_service(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message=(
                        "The Decode engine's serving pod has no container beside the engine. llm-d's picker sends "
                        "prefill through a sidecar in the decode pod, so without one every request skips prefill."
                    ),
                ),
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Composing vllm/vllm-openai:latest on cluster-a",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_FALSE, reason="Deploying"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForModel"),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForModel",
                    message="0 of 2 engine copies ready",
                ),
            ],
        ),
    ),
    # The function only asserts readiness for a resource it can see in observed
    # state.
    Case(
        name="ClusterResolved",
        reason=(
            "With its cluster resolved, a first reconcile composes a native Deployment fronted by an InferencePool and "
            "endpoint picker, and marks none of it ready."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_model_replica(copies=1, model_cache_ref=None)),
            required_resources={"cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")])},
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "model-serving-main": _deployment(
                        cache_claim=None,
                        copies=1,
                        ready=fnv1.READY_UNSPECIFIED,
                        name="test-replica-main-6b608",
                        claim_template_name="test-replica-main-standalone-devices-e609d",
                        labels={
                            "modelplane.ai/deployment": "my-deployment",
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Standalone",
                            "modelplane.ai/serving": "test-replica",
                            "modelplane.ai/workload": "test-replica-main-6b608",
                        },
                    ),
                    "model-route": _route(ready=fnv1.READY_UNSPECIFIED),
                    "resource-claim-main-standalone": _claim_template(
                        ready=fnv1.READY_UNSPECIFIED, name="test-replica-main-standalone-devices-e609d"
                    ),
                    "inference-pool": _inference_pool(ready=fnv1.READY_UNSPECIFIED),
                    "epp": _epp(
                        ready=fnv1.READY_UNSPECIFIED,
                        config_checksum="c4f1cefa4a1fe67069eaa6e9dde3e5b9bc7e79d3f807ecd076dd021259b2ab39",
                    ),
                    "epp-config": _epp_config(ready=fnv1.READY_UNSPECIFIED),
                    "epp-role": _epp_role(ready=fnv1.READY_UNSPECIFIED),
                    "epp-rolebinding": _epp_role_binding(ready=fnv1.READY_UNSPECIFIED),
                    "epp-serviceaccount": _epp_service_account(ready=fnv1.READY_UNSPECIFIED),
                    "epp-service": _epp_service(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Composing vllm/vllm-openai:latest on cluster-a",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_FALSE, reason="Deploying"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForModel"),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForModel",
                    message="0 of 1 engine copies ready",
                ),
            ],
        ),
    ),
    # Nothing is composed while waiting, so the XR is marked not ready rather
    # than left to aggregate to trivially ready.
    Case(
        name="ClusterUnresolved",
        reason="Before its cluster resolves, a replica composes nothing and reports waiting, with the XR not ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_model_replica(copies=1, model_cache_ref=None)),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=fnv1.Resource(ready=fnv1.READY_FALSE)),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Waiting for cluster to be resolved",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForCluster"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForModel"),
                fnv1.Condition(type="ModelReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForModel"),
            ],
        ),
    ),
    Case(
        name="NoProviderConfigRef",
        reason=(
            "A replica whose cluster has no providerConfigRef yet composes nothing and reports waiting, with the XR "
            "not ready."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_model_replica(copies=1, model_cache_ref=None)),
            required_resources={"cluster": fnv1.Resources(items=[_cluster(provider_config_ref=None)])},
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=fnv1.Resource(ready=fnv1.READY_FALSE)),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Waiting for cluster providerConfigRef",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForCluster"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForModel"),
                fnv1.Condition(type="ModelReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForModel"),
            ],
        ),
    ),
    # The route and claim template have no runtime readiness to wait on, so
    # their SuccessfulCreate Objects report Ready once applied. The "Composing
    # ..." event fires only on the first reconcile, before the workload is
    # observed, so there are no results.
    Case(
        name="ResourcesObserved",
        reason=(
            "With the workload observed Available and the route and claim template observed Ready, each is marked "
            "ready, while the unobserved routing objects stay unready, so the replica waits for its routing."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_replica(copies=1, model_cache_ref=None),
                resources={
                    "model-serving-main": _observed_deployment(ready=True, ready_replicas=1, cache_claim=None),
                    "model-route": _observed_object(ready=True),
                    "resource-claim-main-standalone": _observed_object(ready=True),
                },
            ),
            required_resources={"cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")])},
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "model-serving-main": _deployment(
                        cache_claim=None,
                        copies=1,
                        ready=fnv1.READY_TRUE,
                        name="test-replica-main-6b608",
                        claim_template_name="test-replica-main-standalone-devices-e609d",
                        labels={
                            "modelplane.ai/deployment": "my-deployment",
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Standalone",
                            "modelplane.ai/serving": "test-replica",
                            "modelplane.ai/workload": "test-replica-main-6b608",
                        },
                    ),
                    "model-route": _route(ready=fnv1.READY_TRUE),
                    "resource-claim-main-standalone": _claim_template(
                        ready=fnv1.READY_TRUE, name="test-replica-main-standalone-devices-e609d"
                    ),
                    "inference-pool": _inference_pool(ready=fnv1.READY_UNSPECIFIED),
                    "epp": _epp(
                        ready=fnv1.READY_UNSPECIFIED,
                        config_checksum="c4f1cefa4a1fe67069eaa6e9dde3e5b9bc7e79d3f807ecd076dd021259b2ab39",
                    ),
                    "epp-config": _epp_config(ready=fnv1.READY_UNSPECIFIED),
                    "epp-role": _epp_role(ready=fnv1.READY_UNSPECIFIED),
                    "epp-rolebinding": _epp_role_binding(ready=fnv1.READY_UNSPECIFIED),
                    "epp-serviceaccount": _epp_service_account(ready=fnv1.READY_UNSPECIFIED),
                    "epp-service": _epp_service(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_TRUE, reason="Accepted"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForRouting"),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRouting",
                    message="1 of 1 engine copies ready",
                ),
            ],
        ),
    ),
    # An observed Object that isn't Ready is how a Service that failed to apply
    # looks, say because its name was invalid. Being observed isn't being
    # applied, and Crossplane holds the XR unready with it.
    Case(
        name="PickerServiceUnready",
        reason=(
            "With everything observed but the endpoint picker's Service Object not Ready, that Object stays unready "
            "and the replica waits for its routing."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_replica(copies=1, model_cache_ref=None),
                resources={
                    "model-serving-main": _observed_deployment(ready=True, ready_replicas=1, cache_claim=None),
                    "model-route": _observed_object(ready=True),
                    "resource-claim-main-standalone": _observed_object(ready=True),
                    "inference-pool": _observed_object(ready=True),
                    "epp": _observed_object(ready=True),
                    "epp-config": _observed_object(ready=True),
                    "epp-role": _observed_object(ready=True),
                    "epp-rolebinding": _observed_object(ready=True),
                    "epp-serviceaccount": _observed_object(ready=True),
                    "epp-service": _observed_object(ready=False),
                },
            ),
            required_resources={"cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")])},
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "model-serving-main": _deployment(
                        cache_claim=None,
                        copies=1,
                        ready=fnv1.READY_TRUE,
                        name="test-replica-main-6b608",
                        claim_template_name="test-replica-main-standalone-devices-e609d",
                        labels={
                            "modelplane.ai/deployment": "my-deployment",
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Standalone",
                            "modelplane.ai/serving": "test-replica",
                            "modelplane.ai/workload": "test-replica-main-6b608",
                        },
                    ),
                    "model-route": _route(ready=fnv1.READY_TRUE),
                    "resource-claim-main-standalone": _claim_template(
                        ready=fnv1.READY_TRUE, name="test-replica-main-standalone-devices-e609d"
                    ),
                    "inference-pool": _inference_pool(ready=fnv1.READY_TRUE),
                    "epp": _epp(
                        ready=fnv1.READY_TRUE,
                        config_checksum="c4f1cefa4a1fe67069eaa6e9dde3e5b9bc7e79d3f807ecd076dd021259b2ab39",
                    ),
                    "epp-config": _epp_config(ready=fnv1.READY_TRUE),
                    "epp-role": _epp_role(ready=fnv1.READY_TRUE),
                    "epp-rolebinding": _epp_role_binding(ready=fnv1.READY_TRUE),
                    "epp-serviceaccount": _epp_service_account(ready=fnv1.READY_TRUE),
                    "epp-service": _epp_service(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_TRUE, reason="Accepted"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForRouting"),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRouting",
                    message="1 of 1 engine copies ready",
                ),
            ],
        ),
    ),
    # The Object's CEL-derived Ready is False. Crossplane holds the XR unready
    # with it, because the gateway fails closed without a picker, whatever the
    # InferencePool's failureMode says.
    Case(
        name="PickerUnavailable",
        reason=(
            "With everything observed but the endpoint picker's Object reporting Ready False, as it does until its "
            "Deployment is Available, that Object stays unready and the replica waits for its routing."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_replica(copies=1, model_cache_ref=None),
                resources={
                    "model-serving-main": _observed_deployment(ready=True, ready_replicas=1, cache_claim=None),
                    "model-route": _observed_object(ready=True),
                    "resource-claim-main-standalone": _observed_object(ready=True),
                    "inference-pool": _observed_object(ready=True),
                    "epp": _observed_object(ready=False),
                    "epp-config": _observed_object(ready=True),
                    "epp-role": _observed_object(ready=True),
                    "epp-rolebinding": _observed_object(ready=True),
                    "epp-serviceaccount": _observed_object(ready=True),
                    "epp-service": _observed_object(ready=True),
                },
            ),
            required_resources={"cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")])},
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "model-serving-main": _deployment(
                        cache_claim=None,
                        copies=1,
                        ready=fnv1.READY_TRUE,
                        name="test-replica-main-6b608",
                        claim_template_name="test-replica-main-standalone-devices-e609d",
                        labels={
                            "modelplane.ai/deployment": "my-deployment",
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Standalone",
                            "modelplane.ai/serving": "test-replica",
                            "modelplane.ai/workload": "test-replica-main-6b608",
                        },
                    ),
                    "model-route": _route(ready=fnv1.READY_TRUE),
                    "resource-claim-main-standalone": _claim_template(
                        ready=fnv1.READY_TRUE, name="test-replica-main-standalone-devices-e609d"
                    ),
                    "inference-pool": _inference_pool(ready=fnv1.READY_TRUE),
                    "epp": _epp(
                        ready=fnv1.READY_UNSPECIFIED,
                        config_checksum="c4f1cefa4a1fe67069eaa6e9dde3e5b9bc7e79d3f807ecd076dd021259b2ab39",
                    ),
                    "epp-config": _epp_config(ready=fnv1.READY_TRUE),
                    "epp-role": _epp_role(ready=fnv1.READY_TRUE),
                    "epp-rolebinding": _epp_role_binding(ready=fnv1.READY_TRUE),
                    "epp-serviceaccount": _epp_service_account(ready=fnv1.READY_TRUE),
                    "epp-service": _epp_service(ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_TRUE, reason="Accepted"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForRouting"),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRouting",
                    message="1 of 1 engine copies ready",
                ),
            ],
        ),
    ),
    Case(
        name="OneCopyDown",
        reason=(
            "With one of two copies ready and the routing in place, the replica serves through the ready copy but "
            "isn't at full capacity: ModelServing is True and ModelReady is False, and the workload stays unready."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_replica(copies=2, model_cache_ref=None),
                resources={
                    "model-serving-main": _observed_deployment(ready=False, ready_replicas=1, cache_claim=None),
                    "model-route": _observed_object(ready=True),
                    "resource-claim-main-standalone": _observed_object(ready=True),
                    "inference-pool": _observed_object(ready=True),
                    "epp": _observed_object(ready=True),
                    "epp-config": _observed_object(ready=True),
                    "epp-role": _observed_object(ready=True),
                    "epp-rolebinding": _observed_object(ready=True),
                    "epp-serviceaccount": _observed_object(ready=True),
                    "epp-service": _observed_object(ready=True),
                },
            ),
            required_resources={"cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")])},
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "model-serving-main": _deployment(
                        cache_claim=None,
                        ready=fnv1.READY_UNSPECIFIED,
                        copies=2,
                        name="test-replica-main-6b608",
                        claim_template_name="test-replica-main-standalone-devices-e609d",
                        labels={
                            "modelplane.ai/deployment": "my-deployment",
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Standalone",
                            "modelplane.ai/serving": "test-replica",
                            "modelplane.ai/workload": "test-replica-main-6b608",
                        },
                    ),
                    "model-route": _route(ready=fnv1.READY_TRUE),
                    "resource-claim-main-standalone": _claim_template(
                        ready=fnv1.READY_TRUE, name="test-replica-main-standalone-devices-e609d"
                    ),
                    "inference-pool": _inference_pool(ready=fnv1.READY_TRUE),
                    "epp": _epp(
                        ready=fnv1.READY_TRUE,
                        config_checksum="c4f1cefa4a1fe67069eaa6e9dde3e5b9bc7e79d3f807ecd076dd021259b2ab39",
                    ),
                    "epp-config": _epp_config(ready=fnv1.READY_TRUE),
                    "epp-role": _epp_role(ready=fnv1.READY_TRUE),
                    "epp-rolebinding": _epp_role_binding(ready=fnv1.READY_TRUE),
                    "epp-serviceaccount": _epp_service_account(ready=fnv1.READY_TRUE),
                    "epp-service": _epp_service(ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_TRUE, reason="Accepted"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_TRUE, reason="Serving"),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="Degraded",
                    message="1 of 2 engine copies ready",
                ),
            ],
        ),
    ),
    Case(
        name="NoCopyReady",
        reason=("With no copy ready, the replica has started but isn't serving, whatever its routing."),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_replica(copies=2, model_cache_ref=None),
                resources={
                    "model-serving-main": _observed_deployment(ready=False, ready_replicas=0, cache_claim=None),
                    "model-route": _observed_object(ready=True),
                    "resource-claim-main-standalone": _observed_object(ready=True),
                    "inference-pool": _observed_object(ready=True),
                    "epp": _observed_object(ready=True),
                    "epp-config": _observed_object(ready=True),
                    "epp-role": _observed_object(ready=True),
                    "epp-rolebinding": _observed_object(ready=True),
                    "epp-serviceaccount": _observed_object(ready=True),
                    "epp-service": _observed_object(ready=True),
                },
            ),
            required_resources={"cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")])},
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "model-serving-main": _deployment(
                        cache_claim=None,
                        ready=fnv1.READY_UNSPECIFIED,
                        copies=2,
                        name="test-replica-main-6b608",
                        claim_template_name="test-replica-main-standalone-devices-e609d",
                        labels={
                            "modelplane.ai/deployment": "my-deployment",
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Standalone",
                            "modelplane.ai/serving": "test-replica",
                            "modelplane.ai/workload": "test-replica-main-6b608",
                        },
                    ),
                    "model-route": _route(ready=fnv1.READY_TRUE),
                    "resource-claim-main-standalone": _claim_template(
                        ready=fnv1.READY_TRUE, name="test-replica-main-standalone-devices-e609d"
                    ),
                    "inference-pool": _inference_pool(ready=fnv1.READY_TRUE),
                    "epp": _epp(
                        ready=fnv1.READY_TRUE,
                        config_checksum="c4f1cefa4a1fe67069eaa6e9dde3e5b9bc7e79d3f807ecd076dd021259b2ab39",
                    ),
                    "epp-config": _epp_config(ready=fnv1.READY_TRUE),
                    "epp-role": _epp_role(ready=fnv1.READY_TRUE),
                    "epp-rolebinding": _epp_role_binding(ready=fnv1.READY_TRUE),
                    "epp-serviceaccount": _epp_service_account(ready=fnv1.READY_TRUE),
                    "epp-service": _epp_service(ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_TRUE, reason="Accepted"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="ModelStarting"),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="ModelStarting",
                    message="0 of 2 engine copies ready",
                ),
            ],
        ),
    ),
    Case(
        name="RollingOut",
        reason=(
            "With every copy ready but the workload not yet, as during a rolling update, the replica serves "
            "and reports itself rolling out rather than degraded."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_replica(copies=2, model_cache_ref=None),
                resources={
                    "model-serving-main": _observed_deployment(ready=False, ready_replicas=2, cache_claim=None),
                    "model-route": _observed_object(ready=True),
                    "resource-claim-main-standalone": _observed_object(ready=True),
                    "inference-pool": _observed_object(ready=True),
                    "epp": _observed_object(ready=True),
                    "epp-config": _observed_object(ready=True),
                    "epp-role": _observed_object(ready=True),
                    "epp-rolebinding": _observed_object(ready=True),
                    "epp-serviceaccount": _observed_object(ready=True),
                    "epp-service": _observed_object(ready=True),
                },
            ),
            required_resources={"cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")])},
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "model-serving-main": _deployment(
                        cache_claim=None,
                        ready=fnv1.READY_UNSPECIFIED,
                        copies=2,
                        name="test-replica-main-6b608",
                        claim_template_name="test-replica-main-standalone-devices-e609d",
                        labels={
                            "modelplane.ai/deployment": "my-deployment",
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Standalone",
                            "modelplane.ai/serving": "test-replica",
                            "modelplane.ai/workload": "test-replica-main-6b608",
                        },
                    ),
                    "model-route": _route(ready=fnv1.READY_TRUE),
                    "resource-claim-main-standalone": _claim_template(
                        ready=fnv1.READY_TRUE, name="test-replica-main-standalone-devices-e609d"
                    ),
                    "inference-pool": _inference_pool(ready=fnv1.READY_TRUE),
                    "epp": _epp(
                        ready=fnv1.READY_TRUE,
                        config_checksum="c4f1cefa4a1fe67069eaa6e9dde3e5b9bc7e79d3f807ecd076dd021259b2ab39",
                    ),
                    "epp-config": _epp_config(ready=fnv1.READY_TRUE),
                    "epp-role": _epp_role(ready=fnv1.READY_TRUE),
                    "epp-rolebinding": _epp_role_binding(ready=fnv1.READY_TRUE),
                    "epp-serviceaccount": _epp_service_account(ready=fnv1.READY_TRUE),
                    "epp-service": _epp_service(ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_TRUE, reason="Accepted"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_TRUE, reason="Serving"),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="RollingOut",
                    message="2 of 2 engine copies ready",
                ),
            ],
        ),
    ),
    Case(
        name="AllCopiesReady",
        reason=("With both copies ready and the routing in place, the replica is serving at full capacity."),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_replica(copies=2, model_cache_ref=None),
                resources={
                    "model-serving-main": _observed_deployment(ready=True, ready_replicas=2, cache_claim=None),
                    "model-route": _observed_object(ready=True),
                    "resource-claim-main-standalone": _observed_object(ready=True),
                    "inference-pool": _observed_object(ready=True),
                    "epp": _observed_object(ready=True),
                    "epp-config": _observed_object(ready=True),
                    "epp-role": _observed_object(ready=True),
                    "epp-rolebinding": _observed_object(ready=True),
                    "epp-serviceaccount": _observed_object(ready=True),
                    "epp-service": _observed_object(ready=True),
                },
            ),
            required_resources={"cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")])},
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "model-serving-main": _deployment(
                        cache_claim=None,
                        ready=fnv1.READY_TRUE,
                        copies=2,
                        name="test-replica-main-6b608",
                        claim_template_name="test-replica-main-standalone-devices-e609d",
                        labels={
                            "modelplane.ai/deployment": "my-deployment",
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Standalone",
                            "modelplane.ai/serving": "test-replica",
                            "modelplane.ai/workload": "test-replica-main-6b608",
                        },
                    ),
                    "model-route": _route(ready=fnv1.READY_TRUE),
                    "resource-claim-main-standalone": _claim_template(
                        ready=fnv1.READY_TRUE, name="test-replica-main-standalone-devices-e609d"
                    ),
                    "inference-pool": _inference_pool(ready=fnv1.READY_TRUE),
                    "epp": _epp(
                        ready=fnv1.READY_TRUE,
                        config_checksum="c4f1cefa4a1fe67069eaa6e9dde3e5b9bc7e79d3f807ecd076dd021259b2ab39",
                    ),
                    "epp-config": _epp_config(ready=fnv1.READY_TRUE),
                    "epp-role": _epp_role(ready=fnv1.READY_TRUE),
                    "epp-rolebinding": _epp_role_binding(ready=fnv1.READY_TRUE),
                    "epp-serviceaccount": _epp_service_account(ready=fnv1.READY_TRUE),
                    "epp-service": _epp_service(ready=fnv1.READY_TRUE),
                },
            ),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_TRUE, reason="Accepted"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_TRUE, reason="Serving"),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="AllCopiesReady",
                    message="2 of 2 engine copies ready",
                ),
            ],
        ),
    ),
    Case(
        name="CacheReady",
        reason="A replica whose ModelCache is Ready on its cluster mounts the cache the way the ModelCache says to.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_model_replica(copies=1, model_cache_ref="qwen")),
            required_resources={
                "cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")]),
                "cache": fnv1.Resources(
                    items=[
                        _model_cache(
                            clusters=[
                                {
                                    "name": "cluster-a",
                                    "phase": "Ready",
                                    "mount": _cache_mount(claim_name="modelcache-ml-team-qwen-17db2"),
                                }
                            ]
                        )
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "model-serving-main": _deployment(
                        cache_claim="modelcache-ml-team-qwen-17db2",
                        copies=1,
                        ready=fnv1.READY_UNSPECIFIED,
                        name="test-replica-main-6b608",
                        claim_template_name="test-replica-main-standalone-devices-e609d",
                        labels={
                            "modelplane.ai/deployment": "my-deployment",
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Standalone",
                            "modelplane.ai/serving": "test-replica",
                            "modelplane.ai/workload": "test-replica-main-6b608",
                        },
                    ),
                    "model-route": _route(ready=fnv1.READY_UNSPECIFIED),
                    "resource-claim-main-standalone": _claim_template(
                        ready=fnv1.READY_UNSPECIFIED, name="test-replica-main-standalone-devices-e609d"
                    ),
                    "inference-pool": _inference_pool(ready=fnv1.READY_UNSPECIFIED),
                    "epp": _epp(
                        ready=fnv1.READY_UNSPECIFIED,
                        config_checksum="c4f1cefa4a1fe67069eaa6e9dde3e5b9bc7e79d3f807ecd076dd021259b2ab39",
                    ),
                    "epp-config": _epp_config(ready=fnv1.READY_UNSPECIFIED),
                    "epp-role": _epp_role(ready=fnv1.READY_UNSPECIFIED),
                    "epp-rolebinding": _epp_role_binding(ready=fnv1.READY_UNSPECIFIED),
                    "epp-serviceaccount": _epp_service_account(ready=fnv1.READY_UNSPECIFIED),
                    "epp-service": _epp_service(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="Composing vllm/vllm-openai:latest on cluster-a",
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                    "cache": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelCache",
                        match_name="qwen",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_FALSE, reason="Deploying"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForModel"),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForModel",
                    message="0 of 1 engine copies ready",
                ),
            ],
        ),
    ),
    Case(
        name="CacheHydrating",
        reason="Before its ModelCache is Ready on its cluster, a new replica composes nothing and says why it waits.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_model_replica(copies=1, model_cache_ref="qwen")),
            required_resources={
                "cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")]),
                "cache": fnv1.Resources(
                    items=[
                        _model_cache(
                            clusters=[
                                {
                                    "name": "cluster-a",
                                    "phase": "Hydrating",
                                    "message": "Downloading Qwen/Qwen3-0.6B into PersistentVolumeClaim mp-ml-team-51733/modelcache-ml-team-qwen-17db2",
                                }
                            ]
                        )
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=fnv1.Resource(ready=fnv1.READY_FALSE)),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                    "cache": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelCache",
                        match_name="qwen",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ModelAccepted",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForCache",
                    message="ModelCache qwen is Hydrating on cluster cluster-a: Downloading Qwen/Qwen3-0.6B into PersistentVolumeClaim mp-ml-team-51733/modelcache-ml-team-qwen-17db2",
                ),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForModel"),
                fnv1.Condition(type="ModelReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForModel"),
            ],
        ),
    ),
    Case(
        name="CacheNotOnCluster",
        reason="A new replica on a cluster its ModelCache doesn't stage to composes nothing and says so.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_model_replica(copies=1, model_cache_ref="qwen")),
            required_resources={
                "cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")]),
                "cache": fnv1.Resources(
                    items=[
                        _model_cache(
                            clusters=[
                                {
                                    "name": "cluster-b",
                                    "phase": "Ready",
                                    "mount": _cache_mount(claim_name="modelcache-ml-team-qwen-17db2"),
                                }
                            ]
                        )
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=fnv1.Resource(ready=fnv1.READY_FALSE)),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                    "cache": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelCache",
                        match_name="qwen",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ModelAccepted",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForCache",
                    message="ModelCache qwen isn't staged on cluster cluster-a",
                ),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForModel"),
                fnv1.Condition(type="ModelReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForModel"),
            ],
        ),
    ),
    Case(
        name="CacheReadyWithoutMount",
        reason="A new replica whose cache is Ready but hasn't published how to mount it waits for the mount.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_model_replica(copies=1, model_cache_ref="qwen")),
            required_resources={
                "cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")]),
                "cache": fnv1.Resources(
                    items=[
                        _model_cache(
                            clusters=[
                                {
                                    "name": "cluster-a",
                                    "phase": "Ready",
                                }
                            ]
                        )
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=fnv1.Resource(ready=fnv1.READY_FALSE)),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                    "cache": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelCache",
                        match_name="qwen",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ModelAccepted",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForCache",
                    message="Waiting for ModelCache qwen to publish how to mount it on cluster cluster-a",
                ),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForModel"),
                fnv1.Condition(type="ModelReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForModel"),
            ],
        ),
    ),
    Case(
        name="CacheNotFound",
        reason="A new replica whose ModelCache doesn't exist composes nothing and says so.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_model_replica(copies=1, model_cache_ref="qwen")),
            required_resources={
                "cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")]),
                "cache": fnv1.Resources(items=[]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=fnv1.Resource(ready=fnv1.READY_FALSE)),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                    "cache": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelCache",
                        match_name="qwen",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ModelAccepted",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForCache",
                    message="ModelCache qwen not found in namespace ml-team",
                ),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForModel"),
                fnv1.Condition(type="ModelReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForModel"),
            ],
        ),
    ),
    Case(
        name="CacheUnresolved",
        reason="Before its ModelCache resolves, a new replica composes nothing.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_model_replica(copies=1, model_cache_ref="qwen")),
            required_resources={
                "cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=fnv1.Resource(ready=fnv1.READY_FALSE)),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                    "cache": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelCache",
                        match_name="qwen",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(
                    type="ModelAccepted",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForCache",
                    message="Waiting for ModelCache qwen",
                ),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForModel"),
                fnv1.Condition(type="ModelReady", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForModel"),
            ],
        ),
    ),
    Case(
        name="CacheLostKeepsEngines",
        reason="A replica whose engines already run keeps the cache they mount when the cache stops being Ready.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_replica(copies=1, model_cache_ref="qwen"),
                resources={
                    "model-serving-main": _observed_deployment(
                        ready=True, ready_replicas=1, cache_claim="modelcache-ml-team-qwen-17db2"
                    ),
                    "model-route": _observed_object(ready=True),
                    "resource-claim-main-standalone": _observed_object(ready=True),
                },
            ),
            required_resources={
                "cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")]),
                "cache": fnv1.Resources(
                    items=[
                        _model_cache(
                            clusters=[
                                {
                                    "name": "cluster-a",
                                    "phase": "Pending",
                                    "message": "Waiting for PersistentVolumeClaim mp-ml-team-51733/modelcache-ml-team-qwen-17db2 to bind",
                                }
                            ]
                        )
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "model-serving-main": _deployment(
                        cache_claim="modelcache-ml-team-qwen-17db2",
                        copies=1,
                        ready=fnv1.READY_TRUE,
                        name="test-replica-main-6b608",
                        claim_template_name="test-replica-main-standalone-devices-e609d",
                        labels={
                            "modelplane.ai/deployment": "my-deployment",
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Standalone",
                            "modelplane.ai/serving": "test-replica",
                            "modelplane.ai/workload": "test-replica-main-6b608",
                        },
                    ),
                    "model-route": _route(ready=fnv1.READY_TRUE),
                    "resource-claim-main-standalone": _claim_template(
                        ready=fnv1.READY_TRUE, name="test-replica-main-standalone-devices-e609d"
                    ),
                    "inference-pool": _inference_pool(ready=fnv1.READY_UNSPECIFIED),
                    "epp": _epp(
                        ready=fnv1.READY_UNSPECIFIED,
                        config_checksum="c4f1cefa4a1fe67069eaa6e9dde3e5b9bc7e79d3f807ecd076dd021259b2ab39",
                    ),
                    "epp-config": _epp_config(ready=fnv1.READY_UNSPECIFIED),
                    "epp-role": _epp_role(ready=fnv1.READY_UNSPECIFIED),
                    "epp-rolebinding": _epp_role_binding(ready=fnv1.READY_UNSPECIFIED),
                    "epp-serviceaccount": _epp_service_account(ready=fnv1.READY_UNSPECIFIED),
                    "epp-service": _epp_service(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message=(
                        "ModelCache qwen publishes no mount for cluster cluster-a. The replica's engines keep the "
                        "cache they mount now."
                    ),
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                    "cache": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelCache",
                        match_name="qwen",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_TRUE, reason="Accepted"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForRouting"),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRouting",
                    message="1 of 1 engine copies ready",
                ),
            ],
        ),
    ),
    Case(
        name="UpgradeKeepsCacheMount",
        reason="A running replica whose Ready cache hasn't published a mount yet, as after an upgrade, composes its workload as before.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_replica(copies=1, model_cache_ref="qwen"),
                resources={
                    "model-serving-main": _observed_deployment(
                        ready=True, ready_replicas=1, cache_claim="modelcache-ml-team-qwen-17db2"
                    ),
                    "model-route": _observed_object(ready=True),
                    "resource-claim-main-standalone": _observed_object(ready=True),
                },
            ),
            required_resources={
                "cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")]),
                "cache": fnv1.Resources(
                    items=[
                        _model_cache(
                            clusters=[
                                {
                                    "name": "cluster-a",
                                    "phase": "Ready",
                                }
                            ]
                        )
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "model-serving-main": _deployment(
                        cache_claim="modelcache-ml-team-qwen-17db2",
                        copies=1,
                        ready=fnv1.READY_TRUE,
                        name="test-replica-main-6b608",
                        claim_template_name="test-replica-main-standalone-devices-e609d",
                        labels={
                            "modelplane.ai/deployment": "my-deployment",
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Standalone",
                            "modelplane.ai/serving": "test-replica",
                            "modelplane.ai/workload": "test-replica-main-6b608",
                        },
                    ),
                    "model-route": _route(ready=fnv1.READY_TRUE),
                    "resource-claim-main-standalone": _claim_template(
                        ready=fnv1.READY_TRUE, name="test-replica-main-standalone-devices-e609d"
                    ),
                    "inference-pool": _inference_pool(ready=fnv1.READY_UNSPECIFIED),
                    "epp": _epp(
                        ready=fnv1.READY_UNSPECIFIED,
                        config_checksum="c4f1cefa4a1fe67069eaa6e9dde3e5b9bc7e79d3f807ecd076dd021259b2ab39",
                    ),
                    "epp-config": _epp_config(ready=fnv1.READY_UNSPECIFIED),
                    "epp-role": _epp_role(ready=fnv1.READY_UNSPECIFIED),
                    "epp-rolebinding": _epp_role_binding(ready=fnv1.READY_UNSPECIFIED),
                    "epp-serviceaccount": _epp_service_account(ready=fnv1.READY_UNSPECIFIED),
                    "epp-service": _epp_service(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message=(
                        "ModelCache qwen publishes no mount for cluster cluster-a. The replica's engines keep the "
                        "cache they mount now."
                    ),
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                    "cache": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelCache",
                        match_name="qwen",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_TRUE, reason="Accepted"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForRouting"),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRouting",
                    message="1 of 1 engine copies ready",
                ),
            ],
        ),
    ),
    Case(
        name="CacheAddedKeepsPods",
        reason="A running replica that gains a cache that isn't Ready keeps its engines without one.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_replica(copies=1, model_cache_ref="qwen"),
                resources={
                    "model-serving-main": _observed_deployment(ready=True, ready_replicas=1, cache_claim=None),
                    "model-route": _observed_object(ready=True),
                    "resource-claim-main-standalone": _observed_object(ready=True),
                },
            ),
            required_resources={
                "cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")]),
                "cache": fnv1.Resources(
                    items=[
                        _model_cache(
                            clusters=[
                                {
                                    "name": "cluster-a",
                                    "phase": "Hydrating",
                                    "message": "Downloading Qwen/Qwen3-0.6B into PersistentVolumeClaim mp-ml-team-51733/modelcache-ml-team-qwen-17db2",
                                }
                            ]
                        )
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "model-serving-main": _deployment(
                        cache_claim=None,
                        copies=1,
                        ready=fnv1.READY_TRUE,
                        name="test-replica-main-6b608",
                        claim_template_name="test-replica-main-standalone-devices-e609d",
                        labels={
                            "modelplane.ai/deployment": "my-deployment",
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Standalone",
                            "modelplane.ai/serving": "test-replica",
                            "modelplane.ai/workload": "test-replica-main-6b608",
                        },
                    ),
                    "model-route": _route(ready=fnv1.READY_TRUE),
                    "resource-claim-main-standalone": _claim_template(
                        ready=fnv1.READY_TRUE, name="test-replica-main-standalone-devices-e609d"
                    ),
                    "inference-pool": _inference_pool(ready=fnv1.READY_UNSPECIFIED),
                    "epp": _epp(
                        ready=fnv1.READY_UNSPECIFIED,
                        config_checksum="c4f1cefa4a1fe67069eaa6e9dde3e5b9bc7e79d3f807ecd076dd021259b2ab39",
                    ),
                    "epp-config": _epp_config(ready=fnv1.READY_UNSPECIFIED),
                    "epp-role": _epp_role(ready=fnv1.READY_UNSPECIFIED),
                    "epp-rolebinding": _epp_role_binding(ready=fnv1.READY_UNSPECIFIED),
                    "epp-serviceaccount": _epp_service_account(ready=fnv1.READY_UNSPECIFIED),
                    "epp-service": _epp_service(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message=(
                        "ModelCache qwen publishes no mount for cluster cluster-a. The replica's engines keep "
                        "running without a cache."
                    ),
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                    "cache": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelCache",
                        match_name="qwen",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_TRUE, reason="Accepted"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForRouting"),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRouting",
                    message="1 of 1 engine copies ready",
                ),
            ],
        ),
    ),
    Case(
        name="CacheAddedThenReady",
        reason="A running replica that gains a cache mounts it once the cache is Ready on its cluster.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_replica(copies=1, model_cache_ref="qwen"),
                resources={
                    "model-serving-main": _observed_deployment(ready=True, ready_replicas=1, cache_claim=None),
                    "model-route": _observed_object(ready=True),
                    "resource-claim-main-standalone": _observed_object(ready=True),
                },
            ),
            required_resources={
                "cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")]),
                "cache": fnv1.Resources(
                    items=[
                        _model_cache(
                            clusters=[
                                {
                                    "name": "cluster-a",
                                    "phase": "Ready",
                                    "mount": _cache_mount(claim_name="modelcache-ml-team-qwen-17db2"),
                                }
                            ]
                        )
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "model-serving-main": _deployment(
                        cache_claim="modelcache-ml-team-qwen-17db2",
                        copies=1,
                        ready=fnv1.READY_TRUE,
                        name="test-replica-main-6b608",
                        claim_template_name="test-replica-main-standalone-devices-e609d",
                        labels={
                            "modelplane.ai/deployment": "my-deployment",
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Standalone",
                            "modelplane.ai/serving": "test-replica",
                            "modelplane.ai/workload": "test-replica-main-6b608",
                        },
                    ),
                    "model-route": _route(ready=fnv1.READY_TRUE),
                    "resource-claim-main-standalone": _claim_template(
                        ready=fnv1.READY_TRUE, name="test-replica-main-standalone-devices-e609d"
                    ),
                    "inference-pool": _inference_pool(ready=fnv1.READY_UNSPECIFIED),
                    "epp": _epp(
                        ready=fnv1.READY_UNSPECIFIED,
                        config_checksum="c4f1cefa4a1fe67069eaa6e9dde3e5b9bc7e79d3f807ecd076dd021259b2ab39",
                    ),
                    "epp-config": _epp_config(ready=fnv1.READY_UNSPECIFIED),
                    "epp-role": _epp_role(ready=fnv1.READY_UNSPECIFIED),
                    "epp-rolebinding": _epp_role_binding(ready=fnv1.READY_UNSPECIFIED),
                    "epp-serviceaccount": _epp_service_account(ready=fnv1.READY_UNSPECIFIED),
                    "epp-service": _epp_service(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            results=[],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                    "cache": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelCache",
                        match_name="qwen",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_TRUE, reason="Accepted"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForRouting"),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRouting",
                    message="1 of 1 engine copies ready",
                ),
            ],
        ),
    ),
    Case(
        name="SwitchedCacheKeepsMount",
        reason="A running replica switched to a cache that isn't Ready keeps the cache its engines mount now.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_replica(copies=1, model_cache_ref="qwen"),
                resources={
                    "model-serving-main": _observed_deployment(
                        ready=True, ready_replicas=1, cache_claim="modelcache-ml-team-llama-0a1b2"
                    ),
                    "model-route": _observed_object(ready=True),
                    "resource-claim-main-standalone": _observed_object(ready=True),
                },
            ),
            required_resources={
                "cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")]),
                "cache": fnv1.Resources(
                    items=[
                        _model_cache(
                            clusters=[
                                {
                                    "name": "cluster-a",
                                    "phase": "Pending",
                                    "message": "Waiting for PersistentVolumeClaim mp-ml-team-51733/modelcache-ml-team-qwen-17db2 to bind",
                                }
                            ]
                        )
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "model-serving-main": _deployment(
                        cache_claim="modelcache-ml-team-llama-0a1b2",
                        copies=1,
                        ready=fnv1.READY_TRUE,
                        name="test-replica-main-6b608",
                        claim_template_name="test-replica-main-standalone-devices-e609d",
                        labels={
                            "modelplane.ai/deployment": "my-deployment",
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Standalone",
                            "modelplane.ai/serving": "test-replica",
                            "modelplane.ai/workload": "test-replica-main-6b608",
                        },
                    ),
                    "model-route": _route(ready=fnv1.READY_TRUE),
                    "resource-claim-main-standalone": _claim_template(
                        ready=fnv1.READY_TRUE, name="test-replica-main-standalone-devices-e609d"
                    ),
                    "inference-pool": _inference_pool(ready=fnv1.READY_UNSPECIFIED),
                    "epp": _epp(
                        ready=fnv1.READY_UNSPECIFIED,
                        config_checksum="c4f1cefa4a1fe67069eaa6e9dde3e5b9bc7e79d3f807ecd076dd021259b2ab39",
                    ),
                    "epp-config": _epp_config(ready=fnv1.READY_UNSPECIFIED),
                    "epp-role": _epp_role(ready=fnv1.READY_UNSPECIFIED),
                    "epp-rolebinding": _epp_role_binding(ready=fnv1.READY_UNSPECIFIED),
                    "epp-serviceaccount": _epp_service_account(ready=fnv1.READY_UNSPECIFIED),
                    "epp-service": _epp_service(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message=(
                        "ModelCache qwen publishes no mount for cluster cluster-a. The replica's engines keep the "
                        "cache they mount now."
                    ),
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                    "cache": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelCache",
                        match_name="qwen",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_TRUE, reason="Accepted"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForRouting"),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRouting",
                    message="1 of 1 engine copies ready",
                ),
            ],
        ),
    ),
    Case(
        name="CacheDeletedKeepsMount",
        reason="A running replica whose cache is deleted keeps the cache its engines mount now.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_replica(copies=1, model_cache_ref="qwen"),
                resources={
                    "model-serving-main": _observed_deployment(
                        ready=True, ready_replicas=1, cache_claim="modelcache-ml-team-qwen-17db2"
                    ),
                    "model-route": _observed_object(ready=True),
                    "resource-claim-main-standalone": _observed_object(ready=True),
                },
            ),
            required_resources={
                "cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")]),
                "cache": fnv1.Resources(items=[]),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "model-serving-main": _deployment(
                        cache_claim="modelcache-ml-team-qwen-17db2",
                        copies=1,
                        ready=fnv1.READY_TRUE,
                        name="test-replica-main-6b608",
                        claim_template_name="test-replica-main-standalone-devices-e609d",
                        labels={
                            "modelplane.ai/deployment": "my-deployment",
                            "modelplane.ai/engine": "main",
                            "modelplane.ai/role": "Standalone",
                            "modelplane.ai/serving": "test-replica",
                            "modelplane.ai/workload": "test-replica-main-6b608",
                        },
                    ),
                    "model-route": _route(ready=fnv1.READY_TRUE),
                    "resource-claim-main-standalone": _claim_template(
                        ready=fnv1.READY_TRUE, name="test-replica-main-standalone-devices-e609d"
                    ),
                    "inference-pool": _inference_pool(ready=fnv1.READY_UNSPECIFIED),
                    "epp": _epp(
                        ready=fnv1.READY_UNSPECIFIED,
                        config_checksum="c4f1cefa4a1fe67069eaa6e9dde3e5b9bc7e79d3f807ecd076dd021259b2ab39",
                    ),
                    "epp-config": _epp_config(ready=fnv1.READY_UNSPECIFIED),
                    "epp-role": _epp_role(ready=fnv1.READY_UNSPECIFIED),
                    "epp-rolebinding": _epp_role_binding(ready=fnv1.READY_UNSPECIFIED),
                    "epp-serviceaccount": _epp_service_account(ready=fnv1.READY_UNSPECIFIED),
                    "epp-service": _epp_service(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message=(
                        "ModelCache qwen publishes no mount for cluster cluster-a. The replica's engines keep the "
                        "cache they mount now."
                    ),
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                    "cache": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelCache",
                        match_name="qwen",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_TRUE, reason="Accepted"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForRouting"),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRouting",
                    message="1 of 1 engine copies ready",
                ),
            ],
        ),
    ),
    Case(
        name="GangKeepsCacheMount",
        reason="A running LeaderWorkerSet replica whose cache isn't Ready keeps the cache its leader and workers mount.",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        v1alpha1.ModelReplica(
                            metadata=metav1.ObjectMeta(
                                name="test-replica",
                                namespace="ml-team",
                                labels={
                                    "modelplane.ai/deployment": "my-deployment",
                                    "modelplane.ai/cluster": "cluster-a",
                                },
                            ),
                            spec=v1alpha1.SpecModel1(
                                clusterName="cluster-a",
                                modelCacheRef=v1alpha1.ModelCacheRef(name="kimi"),
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
                                                                args=["--model=moonshotai/Kimi-K2-Instruct"],
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
                                                                args=["--model=moonshotai/Kimi-K2-Instruct"],
                                                            )
                                                        ]
                                                    )
                                                ),
                                            ),
                                        ],
                                    )
                                ],
                            ),
                        ).model_dump(exclude_none=True, mode="json", by_alias=True)
                    )
                ),
                resources={
                    # Only the pod templates of the LeaderWorkerSet it last
                    # composed matter here, so the rest is left out.
                    "model-serving-main": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "spec": {
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "leaderworkerset.x-k8s.io/v1",
                                            "kind": "LeaderWorkerSet",
                                            "spec": {
                                                "leaderWorkerTemplate": {
                                                    "leaderTemplate": {
                                                        "spec": {
                                                            "containers": [
                                                                {
                                                                    "name": "engine",
                                                                    "env": [
                                                                        {"name": "HF_HUB_CACHE", "value": "/mnt/models"}
                                                                    ],
                                                                    "volumeMounts": [
                                                                        {"name": "dshm", "mountPath": "/dev/shm"},
                                                                        {
                                                                            "name": "model-cache",
                                                                            "mountPath": "/mnt/models",
                                                                        },
                                                                    ],
                                                                }
                                                            ],
                                                            "volumes": [
                                                                {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                                                                {
                                                                    "name": "model-cache",
                                                                    "persistentVolumeClaim": {
                                                                        "claimName": "modelcache-ml-team-kimi-aa322"
                                                                    },
                                                                },
                                                            ],
                                                        }
                                                    },
                                                    "workerTemplate": {
                                                        "spec": {
                                                            "containers": [
                                                                {
                                                                    "name": "engine",
                                                                    "env": [
                                                                        {"name": "HF_HUB_CACHE", "value": "/mnt/models"}
                                                                    ],
                                                                    "volumeMounts": [
                                                                        {"name": "dshm", "mountPath": "/dev/shm"},
                                                                        {
                                                                            "name": "model-cache",
                                                                            "mountPath": "/mnt/models",
                                                                        },
                                                                    ],
                                                                }
                                                            ],
                                                            "volumes": [
                                                                {"name": "dshm", "emptyDir": {"medium": "Memory"}},
                                                                {
                                                                    "name": "model-cache",
                                                                    "persistentVolumeClaim": {
                                                                        "claimName": "modelcache-ml-team-kimi-aa322"
                                                                    },
                                                                },
                                                            ],
                                                        }
                                                    },
                                                }
                                            },
                                        }
                                    }
                                },
                                "status": {
                                    "atProvider": {"manifest": {"status": {"readyReplicas": 1}}},
                                    "conditions": [
                                        {
                                            "type": "Ready",
                                            "status": "True",
                                            "reason": "Available",
                                            "lastTransitionTime": "2025-01-01T00:00:00Z",
                                        }
                                    ],
                                },
                            }
                        )
                    ),
                    "model-route": _observed_object(ready=True),
                    "resource-claim-main-leader": _observed_object(ready=True),
                    "resource-claim-main-worker": _observed_object(ready=True),
                },
            ),
            required_resources={
                "cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")]),
                "cache": fnv1.Resources(
                    items=[
                        _model_cache(
                            clusters=[
                                {
                                    "name": "cluster-a",
                                    "phase": "Hydrating",
                                    "message": "Downloading moonshotai/Kimi-K2-Instruct into PersistentVolumeClaim mp-ml-team-51733/modelcache-ml-team-kimi-aa322",
                                }
                            ]
                        )
                    ]
                ),
            },
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "model-serving-main": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "spec": {
                                    "forProvider": {
                                        "manifest": {
                                            "apiVersion": "leaderworkerset.x-k8s.io/v1",
                                            "kind": "LeaderWorkerSet",
                                            "metadata": {
                                                "name": "test-replica-main-6b608",
                                                "namespace": "mp-ml-team-51733",
                                            },
                                            "spec": {
                                                "leaderWorkerTemplate": {
                                                    "leaderTemplate": {
                                                        "metadata": {
                                                            "labels": {
                                                                "modelplane.ai/deployment": "my-deployment",
                                                                "modelplane.ai/engine": "main",
                                                                "modelplane.ai/lws-role": "leader",
                                                                "modelplane.ai/role": "Leader",
                                                                "modelplane.ai/serving": "test-replica",
                                                            }
                                                        },
                                                        "spec": {
                                                            "containers": [
                                                                {
                                                                    "args": ["--model=moonshotai/Kimi-K2-Instruct"],
                                                                    "env": [
                                                                        {
                                                                            "name": "MODELPLANE_LEADER_ADDRESS",
                                                                            "value": "$(LWS_LEADER_ADDRESS)",
                                                                        },
                                                                        {
                                                                            "name": "MODELPLANE_RANK",
                                                                            "value": "$(LWS_WORKER_INDEX)",
                                                                        },
                                                                        {
                                                                            "name": "HF_HUB_CACHE",
                                                                            "value": "/mnt/models",
                                                                        },
                                                                    ],
                                                                    "image": "vllm/vllm-openai:latest",
                                                                    "name": "engine",
                                                                    "ports": [{"containerPort": 8000, "name": "http"}],
                                                                    "readinessProbe": {
                                                                        "httpGet": {"path": "/health", "port": 8000},
                                                                        "initialDelaySeconds": 30,
                                                                        "periodSeconds": 10,
                                                                        "timeoutSeconds": 5,
                                                                    },
                                                                    "resources": {"claims": [{"name": "devices"}]},
                                                                    "volumeMounts": [
                                                                        {"mountPath": "/dev/shm", "name": "dshm"},
                                                                        {
                                                                            "mountPath": "/mnt/models",
                                                                            "name": "model-cache",
                                                                        },
                                                                    ],
                                                                }
                                                            ],
                                                            "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                                            "resourceClaims": [
                                                                {
                                                                    "name": "devices",
                                                                    "resourceClaimTemplateName": "test-replica-main-leader-devices-34ec6",
                                                                }
                                                            ],
                                                            "tolerations": [
                                                                {
                                                                    "effect": "NoSchedule",
                                                                    "key": "nvidia.com/gpu",
                                                                    "operator": "Exists",
                                                                }
                                                            ],
                                                            "volumes": [
                                                                {"emptyDir": {"medium": "Memory"}, "name": "dshm"},
                                                                {
                                                                    "name": "model-cache",
                                                                    "persistentVolumeClaim": {
                                                                        "claimName": "modelcache-ml-team-kimi-aa322"
                                                                    },
                                                                },
                                                            ],
                                                        },
                                                    },
                                                    "size": 2,
                                                    "workerTemplate": {
                                                        "metadata": {
                                                            "labels": {
                                                                "modelplane.ai/deployment": "my-deployment",
                                                                "modelplane.ai/engine": "main",
                                                                "modelplane.ai/role": "Worker",
                                                            }
                                                        },
                                                        "spec": {
                                                            "containers": [
                                                                {
                                                                    "args": ["--model=moonshotai/Kimi-K2-Instruct"],
                                                                    "env": [
                                                                        {
                                                                            "name": "MODELPLANE_LEADER_ADDRESS",
                                                                            "value": "$(LWS_LEADER_ADDRESS)",
                                                                        },
                                                                        {
                                                                            "name": "MODELPLANE_RANK",
                                                                            "value": "$(LWS_WORKER_INDEX)",
                                                                        },
                                                                        {
                                                                            "name": "HF_HUB_CACHE",
                                                                            "value": "/mnt/models",
                                                                        },
                                                                    ],
                                                                    "image": "vllm/vllm-openai:latest",
                                                                    "name": "engine",
                                                                    "resources": {"claims": [{"name": "devices"}]},
                                                                    "volumeMounts": [
                                                                        {"mountPath": "/dev/shm", "name": "dshm"},
                                                                        {
                                                                            "mountPath": "/mnt/models",
                                                                            "name": "model-cache",
                                                                        },
                                                                    ],
                                                                }
                                                            ],
                                                            "nodeSelector": {"modelplane.ai/pool": "frontier"},
                                                            "resourceClaims": [
                                                                {
                                                                    "name": "devices",
                                                                    "resourceClaimTemplateName": "test-replica-main-worker-devices-db183",
                                                                }
                                                            ],
                                                            "tolerations": [
                                                                {
                                                                    "effect": "NoSchedule",
                                                                    "key": "nvidia.com/gpu",
                                                                    "operator": "Exists",
                                                                }
                                                            ],
                                                            "volumes": [
                                                                {"emptyDir": {"medium": "Memory"}, "name": "dshm"},
                                                                {
                                                                    "name": "model-cache",
                                                                    "persistentVolumeClaim": {
                                                                        "claimName": "modelcache-ml-team-kimi-aa322"
                                                                    },
                                                                },
                                                            ],
                                                        },
                                                    },
                                                },
                                                "replicas": 1,
                                            },
                                        }
                                    },
                                    "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "cluster-a-pc"},
                                    "readiness": {
                                        "celQuery": "has(object.status.conditions) && object.status.conditions.exists(c, "
                                        'c.type == "Available" && c.status == "True") && '
                                        "has(object.status.readyReplicas) && object.status.readyReplicas >= "
                                        "object.spec.replicas",
                                        "policy": "DeriveFromCelQuery",
                                    },
                                    "watch": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "model-route": _route(ready=fnv1.READY_TRUE),
                    "resource-claim-main-leader": _claim_template(
                        ready=fnv1.READY_TRUE, name="test-replica-main-leader-devices-34ec6"
                    ),
                    "resource-claim-main-worker": _claim_template(
                        ready=fnv1.READY_TRUE, name="test-replica-main-worker-devices-db183"
                    ),
                    "inference-pool": _inference_pool(ready=fnv1.READY_UNSPECIFIED),
                    "epp": _epp(
                        ready=fnv1.READY_UNSPECIFIED,
                        config_checksum="c4f1cefa4a1fe67069eaa6e9dde3e5b9bc7e79d3f807ecd076dd021259b2ab39",
                    ),
                    "epp-config": _epp_config(ready=fnv1.READY_UNSPECIFIED),
                    "epp-role": _epp_role(ready=fnv1.READY_UNSPECIFIED),
                    "epp-rolebinding": _epp_role_binding(ready=fnv1.READY_UNSPECIFIED),
                    "epp-serviceaccount": _epp_service_account(ready=fnv1.READY_UNSPECIFIED),
                    "epp-service": _epp_service(ready=fnv1.READY_UNSPECIFIED),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_WARNING,
                    message=(
                        "ModelCache kimi publishes no mount for cluster cluster-a. The replica's engines keep the "
                        "cache they mount now."
                    ),
                ),
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "cluster": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="InferenceCluster",
                        match_name="cluster-a",
                    ),
                    "cache": fnv1.ResourceSelector(
                        api_version="modelplane.ai/v1alpha1",
                        kind="ModelCache",
                        match_name="kimi",
                        namespace="ml-team",
                    ),
                },
            ),
            conditions=[
                fnv1.Condition(type="ModelAccepted", status=fnv1.STATUS_CONDITION_TRUE, reason="Accepted"),
                fnv1.Condition(type="ModelServing", status=fnv1.STATUS_CONDITION_FALSE, reason="WaitingForRouting"),
                fnv1.Condition(
                    type="ModelReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForRouting",
                    message="1 of 1 engine copies ready",
                ),
            ],
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction dispatches to a backend to compose serving resources on a remote cluster."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
