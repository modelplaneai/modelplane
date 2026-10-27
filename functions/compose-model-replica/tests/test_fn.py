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


def _model_replica(*, copies: int) -> fnv1.Resource:
    """The ModelReplica XR test-replica in ml-team, with one Standalone engine.

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
    *,
    ready: fnv1.Ready,
    max_unavailable: int | str,
    name: str,
    claim_template_name: str,
    labels: dict,
    copies: int,
) -> fnv1.Resource:
    """The Object composing an engine's Deployment.

    Its pod carries the labels the collector attributes the engine's metrics by:
    the deployment, the engine and the member's role. Its rolling update surges
    by 25%, and lets max_unavailable copies go down.
    """
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
                                "strategy": {
                                    "type": "RollingUpdate",
                                    "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": max_unavailable},
                                },
                                "template": {
                                    "metadata": {"labels": labels},
                                    "spec": {
                                        "containers": [
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
                                        "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
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


def _observed_deployment(*, ready: bool, ready_replicas: int, deadline_exceeded: bool) -> fnv1.Resource:
    """The engine's Deployment Object as observed back.

    ready sets the Object's derived Ready condition, and ready_replicas the
    Deployment's status.readyReplicas and status.availableReplicas, which a
    Deployment leaves out at 0. With no minReadySeconds a ready pod is
    available. deadline_exceeded sets the Progressing condition a rollout gets
    once it passes its progress deadline.
    """
    observed_manifest: dict = {"kind": "Deployment"}
    status: dict = {}
    if ready_replicas:
        status = {"readyReplicas": ready_replicas, "availableReplicas": ready_replicas}
    if deadline_exceeded:
        status["conditions"] = [{"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"}]
    if status:
        observed_manifest["status"] = status
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "spec": {"forProvider": {"manifest": {"kind": "Deployment"}}},
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
                    "model-serving-prefill": _observed_deployment(
                        ready=True, ready_replicas=1, deadline_exceeded=False
                    ),
                    "model-serving-decode": _observed_deployment(ready=True, ready_replicas=1, deadline_exceeded=False),
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
                    "model-serving-prefill": _observed_deployment(
                        ready=True, ready_replicas=1, deadline_exceeded=False
                    ),
                    "model-serving-decode": _observed_deployment(
                        ready=False, ready_replicas=0, deadline_exceeded=False
                    ),
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
                        max_unavailable="25%",
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
                        max_unavailable="25%",
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
            observed=fnv1.State(composite=_model_replica(copies=1)),
            required_resources={"cluster": fnv1.Resources(items=[_cluster(provider_config_ref="cluster-a-pc")])},
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                resources={
                    "model-serving-main": _deployment(
                        max_unavailable="25%",
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
            observed=fnv1.State(composite=_model_replica(copies=1)),
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
            observed=fnv1.State(composite=_model_replica(copies=1)),
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
                composite=_model_replica(copies=1),
                resources={
                    "model-serving-main": _observed_deployment(ready=True, ready_replicas=1, deadline_exceeded=False),
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
                        max_unavailable="25%",
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
                composite=_model_replica(copies=1),
                resources={
                    "model-serving-main": _observed_deployment(ready=True, ready_replicas=1, deadline_exceeded=False),
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
                        max_unavailable="25%",
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
                composite=_model_replica(copies=1),
                resources={
                    "model-serving-main": _observed_deployment(ready=True, ready_replicas=1, deadline_exceeded=False),
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
                        max_unavailable="25%",
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
                composite=_model_replica(copies=2),
                resources={
                    "model-serving-main": _observed_deployment(ready=False, ready_replicas=1, deadline_exceeded=False),
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
                        max_unavailable="25%",
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
                composite=_model_replica(copies=2),
                resources={
                    "model-serving-main": _observed_deployment(ready=False, ready_replicas=0, deadline_exceeded=False),
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
                        max_unavailable="25%",
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
        name="RolloutPastDeadline",
        reason=(
            "A rollout past its progress deadline with no copy available replaces both copies without waiting for "
            "their replacements, which may need their devices."
        ),
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_model_replica(copies=2),
                resources={
                    "model-serving-main": _observed_deployment(ready=False, ready_replicas=0, deadline_exceeded=True),
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
                        max_unavailable=2,
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
                composite=_model_replica(copies=2),
                resources={
                    "model-serving-main": _observed_deployment(ready=False, ready_replicas=2, deadline_exceeded=False),
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
                        max_unavailable="25%",
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
                composite=_model_replica(copies=2),
                resources={
                    "model-serving-main": _observed_deployment(ready=True, ready_replicas=2, deadline_exceeded=False),
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
                        max_unavailable="25%",
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
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction dispatches to a backend to compose serving resources on a remote cluster."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want), case.reason
