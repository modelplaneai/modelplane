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

"""Tests for the compose-k3s-cluster function."""

import dataclasses
import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.infrastructure.k3scluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-k3s-cluster."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


# Names of the composed children. Derived like the function derives them -
# the hash suffix depends only on the parent and child names.
_PROVIDER_CONFIG_NAME = resource.child_name("test-cluster", "ssh")
_CLUSTER_NAME = resource.child_name("test-cluster", "cluster")
_KUBECONFIG_SECRET_NAME = resource.child_name("test-cluster", "kubeconfig")

# A GPU worker with the labels and taint compose-vultr-baremetal-cluster
# would pass.
_GPU_WORKER = v1alpha1.Worker(
    name="gpu-0",
    host="203.0.113.20",
    labels={"modelplane.ai/pool": "gpu", "modelplane.ai/gpu": "amd-mi355x"},
    taints=[v1alpha1.Taint(key="amd.com/gpu", value="true", effect="NoSchedule")],
)

_GPU_WORKER_EXTRA_ARGS = (
    "--node-label modelplane.ai/gpu=amd-mi355x --node-label modelplane.ai/pool=gpu"
    " --node-taint amd.com/gpu=true:NoSchedule"
)


def _xr(
    workers: list[v1alpha1.Worker],
    version: v1alpha1.Version | None = None,
) -> dict:
    """A K3sCluster XR with the given workers, as a request dict."""
    return v1alpha1.K3sCluster(
        metadata=metav1.ObjectMeta(
            name="test-cluster",
            namespace="modelplane-system",
        ),
        spec=v1alpha1.Spec(
            controlPlane=v1alpha1.ControlPlane(host="203.0.113.10"),
            workers=workers,
            auth=v1alpha1.Auth(secretRef=v1alpha1.SecretRef(name="test-ssh")),
            version=version,
        ),
    ).model_dump(exclude_none=True, mode="json")


def _req(
    workers: list[v1alpha1.Worker],
    version: v1alpha1.Version | None = None,
    observed_resources: dict[str, fnv1.Resource] | None = None,
) -> fnv1.RunFunctionRequest:
    return fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(resource=resource.dict_to_struct(_xr(workers, version))),
            resources=observed_resources or {},
        ),
    )


def _provider_config() -> dict:
    """A k3s ProviderConfig golden carrying the SSH identity."""
    return {
        "apiVersion": "k3s.m.crossplane.io/v1alpha1",
        "kind": "ProviderConfig",
        "metadata": {
            "name": _PROVIDER_CONFIG_NAME,
            "namespace": "modelplane-system",
        },
        "spec": {
            "username": "root",
            "credentials": {
                "source": "Secret",
                "secretRef": {
                    "namespace": "modelplane-system",
                    "name": "test-ssh",
                    "key": "ssh-privatekey",
                },
            },
        },
    }


def _cluster(release: dict | None = None) -> dict:
    """A k3s Cluster golden installing the server on the control plane."""
    return {
        "apiVersion": "k3s.m.crossplane.io/v1alpha1",
        "kind": "Cluster",
        "metadata": {"name": _CLUSTER_NAME},
        "spec": {
            "providerConfigRef": {"kind": "ProviderConfig", "name": _PROVIDER_CONFIG_NAME},
            "forProvider": {
                "host": "203.0.113.10",
                "port": 22,
                "tlsSAN": "203.0.113.10",
                "disableTraefik": True,
                **(release if release is not None else {"k3sChannel": "v1.34"}),
            },
            "writeConnectionSecretToRef": {"name": _KUBECONFIG_SECRET_NAME},
        },
    }


def _node(host: str, release: dict | None = None, extra_args: str | None = None) -> dict:
    """A k3s Node golden joining a worker as an agent."""
    fp = {
        "host": host,
        "port": 22,
        "role": "agent",
        "clusterRef": {"name": _CLUSTER_NAME},
        **(release if release is not None else {"k3sChannel": "v1.34"}),
    }
    if extra_args:
        fp["extraArgs"] = extra_args
    return {
        "apiVersion": "k3s.m.crossplane.io/v1alpha1",
        "kind": "Node",
        "spec": {
            "providerConfigRef": {"kind": "ProviderConfig", "name": _PROVIDER_CONFIG_NAME},
            "forProvider": fp,
        },
    }


def _status() -> dict:
    return {
        "status": {
            "secrets": [
                {
                    "type": "Kubeconfig",
                    "name": _KUBECONFIG_SECRET_NAME,
                    "key": "kubeconfig",
                },
            ],
        },
    }


def _observed_ready(desired: dict) -> fnv1.Resource:
    """An observed variant of a desired resource with a Ready=True condition."""
    observed = {
        **desired,
        "status": {
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True",
                    "reason": "Available",
                    "lastTransitionTime": "2024-01-01T00:00:00Z",
                },
            ],
        },
    }
    return fnv1.Resource(resource=resource.dict_to_struct(observed))


def _observed_unready(desired: dict) -> fnv1.Resource:
    """An observed variant of a desired resource with a Ready=False condition."""
    observed = {
        **desired,
        "status": {
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "reason": "Unavailable",
                    "lastTransitionTime": "2024-01-01T00:00:00Z",
                },
            ],
        },
    }
    return fnv1.Resource(resource=resource.dict_to_struct(observed))


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    """Tests for FunctionRunner.RunFunction."""

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:
        """The function composes a k3s cluster over SSH."""
        cases = [
            Case(
                name="cluster composed first; nodes withheld until cluster Ready",
                req=_req([_GPU_WORKER]),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "provider-config-k3s": fnv1.Resource(
                                resource=resource.dict_to_struct(_provider_config()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "cluster": fnv1.Resource(
                                resource=resource.dict_to_struct(_cluster()),
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="nodes composed once the cluster is Ready; labels and taints as agent args",
                req=_req(
                    [_GPU_WORKER],
                    observed_resources={
                        "cluster": _observed_ready(_cluster()),
                    },
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "provider-config-k3s": fnv1.Resource(
                                resource=resource.dict_to_struct(_provider_config()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "cluster": fnv1.Resource(
                                resource=resource.dict_to_struct(_cluster()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "node-gpu-0": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _node("203.0.113.20", extra_args=_GPU_WORKER_EXTRA_ARGS),
                                ),
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="nodes kept when the cluster Ready condition transiently regresses",
                req=_req(
                    [_GPU_WORKER],
                    observed_resources={
                        "cluster": _observed_unready(_cluster()),
                        "node-gpu-0": _observed_ready(_node("203.0.113.20", extra_args=_GPU_WORKER_EXTRA_ARGS)),
                    },
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "provider-config-k3s": fnv1.Resource(
                                resource=resource.dict_to_struct(_provider_config()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "cluster": fnv1.Resource(
                                resource=resource.dict_to_struct(_cluster()),
                            ),
                            "node-gpu-0": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _node("203.0.113.20", extra_args=_GPU_WORKER_EXTRA_ARGS),
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="exact version pins k3sVersion; plain worker joins with no agent args",
                req=_req(
                    [v1alpha1.Worker(name="w0", host="203.0.113.30")],
                    version=v1alpha1.Version(version="v1.34.1+k3s1"),
                    observed_resources={
                        "cluster": _observed_ready(_cluster({"k3sVersion": "v1.34.1+k3s1"})),
                    },
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "provider-config-k3s": fnv1.Resource(
                                resource=resource.dict_to_struct(_provider_config()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "cluster": fnv1.Resource(
                                resource=resource.dict_to_struct(_cluster({"k3sVersion": "v1.34.1+k3s1"})),
                                ready=fnv1.READY_TRUE,
                            ),
                            "node-w0": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _node("203.0.113.30", {"k3sVersion": "v1.34.1+k3s1"}),
                                ),
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                ),
            ),
            Case(
                name="K3sCluster Ready only once the server and every agent are Ready",
                req=_req(
                    [_GPU_WORKER],
                    observed_resources={
                        "cluster": _observed_ready(_cluster()),
                        "node-gpu-0": _observed_ready(_node("203.0.113.20", extra_args=_GPU_WORKER_EXTRA_ARGS)),
                    },
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(resource=resource.dict_to_struct(_status())),
                        resources={
                            "provider-config-k3s": fnv1.Resource(
                                resource=resource.dict_to_struct(_provider_config()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "cluster": fnv1.Resource(
                                resource=resource.dict_to_struct(_cluster()),
                                ready=fnv1.READY_TRUE,
                            ),
                            "node-gpu-0": fnv1.Resource(
                                resource=resource.dict_to_struct(
                                    _node("203.0.113.20", extra_args=_GPU_WORKER_EXTRA_ARGS),
                                ),
                                ready=fnv1.READY_TRUE,
                            ),
                        },
                    ),
                    context=structpb.Struct(),
                ),
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
