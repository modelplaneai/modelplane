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

"""Tests for the compose-vultr-baremetal-cluster function."""

import base64
import dataclasses
import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.infrastructure.vultrbaremetalcluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-vultr-baremetal-cluster."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


_PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA test@modelplane"

# The GPU pool used across cases.
_GPU_POOL = v1alpha1.NodePool(
    name="gpu",
    plan="vbm-256c-3072gb-8-mi355x-gpu",
    gpu=v1alpha1.Gpu(acceleratorType="amd-mi355x"),
)


def _xr(pools: list[v1alpha1.NodePool] | None = None) -> dict:
    """A VultrBaremetalCluster XR as a request dict."""
    return v1alpha1.VultrBaremetalCluster(
        metadata=metav1.ObjectMeta(
            name="test-cluster",
            namespace="modelplane-system",
        ),
        spec=v1alpha1.Spec(
            region="ord",
            ssh=v1alpha1.Ssh(secretRef=v1alpha1.SecretRef(name="test-ssh")),
            nodePools=pools if pools is not None else [_GPU_POOL],
        ),
    ).model_dump(exclude_none=True, mode="json")


def _ssh_secret() -> fnv1.Resource:
    """The observed SSH key pair Secret."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "test-ssh", "namespace": "modelplane-system"},
                "data": {
                    "ssh-publickey": base64.b64encode(f"{_PUBLIC_KEY}\n".encode()).decode(),
                    "ssh-privatekey": base64.b64encode(b"private").decode(),
                },
            },
        ),
    )


def _req(
    observed_resources: dict[str, fnv1.Resource] | None = None,
    *,
    pools: list[v1alpha1.NodePool] | None = None,
    with_secret: bool = True,
    secret_data: dict[str, str] | None = None,
) -> fnv1.RunFunctionRequest:
    req = fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(resource=resource.dict_to_struct(_xr(pools))),
            resources=observed_resources or {},
        ),
    )
    if with_secret:
        secret = _ssh_secret()
        if secret_data is not None:
            secret = fnv1.Resource(
                resource=resource.dict_to_struct(
                    {
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "metadata": {"name": "test-ssh", "namespace": "modelplane-system"},
                        "data": secret_data,
                    },
                ),
            )
        req.required_resources["ssh-secret"].items.append(secret)
    return req


def _ssh_selector() -> fnv1.ResourceSelector:
    """The requirement declared for the SSH key pair Secret."""
    return fnv1.ResourceSelector(
        api_version="v1",
        kind="Secret",
        match_name="test-ssh",
        namespace="modelplane-system",
    )


def _want(
    resources: dict[str, fnv1.Resource],
    composite: fnv1.Resource | None = None,
) -> fnv1.RunFunctionResponse:
    want = fnv1.RunFunctionResponse(
        meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
        desired=fnv1.State(composite=composite, resources=resources),
        context=structpb.Struct(),
    )
    want.requirements.resources["ssh-secret"].CopyFrom(_ssh_selector())
    return want


def _ssh_key() -> dict:
    """An SSHKey golden registering the public key with Vultr."""
    return {
        "apiVersion": "compute.vultr.m.upbound.io/v1beta1",
        "kind": "SSHKey",
        "spec": {
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
            "forProvider": {
                "name": "test-cluster",
                "sshKey": _PUBLIC_KEY,
            },
        },
    }


def _server(label: str, plan: str) -> dict:
    """A BareMetalServer golden."""
    return {
        "apiVersion": "compute.vultr.m.upbound.io/v1beta1",
        "kind": "BareMetalServer",
        "spec": {
            "providerConfigRef": {"kind": "ClusterProviderConfig", "name": "default"},
            "forProvider": {
                "label": label,
                "hostname": label,
                "plan": plan,
                "region": "ord",
                "osId": 2284,
                "sshKeyIdsSelector": {"matchControllerRef": True},
                "tags": ["modelplane.ai/cluster=test-cluster"],
                "userData": fn._USER_DATA,
            },
        },
    }


_MANAGEMENT_SERVER = _server("test-cluster-management", "vbm-6c-32gb-amd")
_GPU_SERVER = _server("test-cluster-gpu-0", "vbm-256c-3072gb-8-mi355x-gpu")


def _k3s_cluster() -> dict:
    """A K3sCluster golden built from the servers' observed IPs."""
    return {
        "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
        "kind": "K3sCluster",
        "spec": {
            "controlPlane": {"host": "203.0.113.10"},
            "workers": [
                {
                    "name": "gpu-0",
                    "host": "203.0.113.20",
                    "labels": {
                        "modelplane.ai/pool": "gpu",
                        "modelplane.ai/gpu": "amd-mi355x",
                    },
                    "taints": [
                        {"key": "amd.com/gpu", "value": "true", "effect": "NoSchedule"},
                    ],
                },
            ],
            "auth": {
                "username": "root",
                "secretRef": {"name": "test-ssh", "key": "ssh-privatekey"},
            },
            "version": {"channel": "v1.34"},
        },
    }


def _observed_active(desired: dict, main_ip: str) -> fnv1.Resource:
    """An observed server that is active with a main IP and Ready."""
    observed = {
        **desired,
        "status": {
            "atProvider": {"mainIp": main_ip, "status": "active"},
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


def _observed_provisioning(desired: dict) -> fnv1.Resource:
    """An observed server that is still provisioning: no IP, not Ready."""
    observed = {
        **desired,
        "status": {
            "atProvider": {"status": "pending"},
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "reason": "Creating",
                    "lastTransitionTime": "2024-01-01T00:00:00Z",
                },
            ],
        },
    }
    return fnv1.Resource(resource=resource.dict_to_struct(observed))


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


def _observed_k3s_with_secrets(desired: dict) -> fnv1.Resource:
    """An observed K3sCluster that is Ready and publishes its kubeconfig."""
    observed = {
        **desired,
        "status": {
            "secrets": [
                {
                    "type": "Kubeconfig",
                    "name": "test-cluster-abc12-kubeconfig-d34db",
                    "key": "kubeconfig",
                },
            ],
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


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    """Tests for FunctionRunner.RunFunction."""

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:
        """The function composes bare metal servers into a k3s cluster."""
        cases = [
            Case(
                name="nothing composed until the SSH key Secret resolves",
                req=_req(with_secret=False),
                want=self._waiting_want(
                    "Waiting for SSH key Secret test-ssh",
                    fnv1.Result(
                        severity=fnv1.SEVERITY_NORMAL,
                        message="Waiting for SSH key Secret test-ssh",
                    ),
                ),
            ),
            Case(
                name="nothing composed when the Secret lacks the public key",
                req=_req(secret_data={"ssh-privatekey": base64.b64encode(b"private").decode()}),
                want=self._waiting_want(
                    "SSH key Secret test-ssh has no ssh-publickey key",
                    fnv1.Result(
                        severity=fnv1.SEVERITY_WARNING,
                        message="SSH key Secret test-ssh has no ssh-publickey key",
                    ),
                ),
            ),
            Case(
                name="servers composed; K3sCluster withheld until every server is active",
                req=_req(),
                want=_want(
                    {
                        "ssh-key": fnv1.Resource(resource=resource.dict_to_struct(_ssh_key())),
                        "server-management": fnv1.Resource(resource=resource.dict_to_struct(_MANAGEMENT_SERVER)),
                        "server-gpu-0": fnv1.Resource(resource=resource.dict_to_struct(_GPU_SERVER)),
                    },
                ),
            ),
            Case(
                name="K3sCluster withheld while a GPU server is still provisioning",
                req=_req(
                    {
                        "ssh-key": _observed_ready(_ssh_key()),
                        "server-management": _observed_active(_MANAGEMENT_SERVER, "203.0.113.10"),
                        "server-gpu-0": _observed_provisioning(_GPU_SERVER),
                    },
                ),
                want=_want(
                    {
                        "ssh-key": fnv1.Resource(
                            resource=resource.dict_to_struct(_ssh_key()),
                            ready=fnv1.READY_TRUE,
                        ),
                        "server-management": fnv1.Resource(
                            resource=resource.dict_to_struct(_MANAGEMENT_SERVER),
                            ready=fnv1.READY_TRUE,
                        ),
                        "server-gpu-0": fnv1.Resource(resource=resource.dict_to_struct(_GPU_SERVER)),
                    },
                ),
            ),
            Case(
                name="K3sCluster composed from the servers' IPs once all are active",
                req=_req(
                    {
                        "ssh-key": _observed_ready(_ssh_key()),
                        "server-management": _observed_active(_MANAGEMENT_SERVER, "203.0.113.10"),
                        "server-gpu-0": _observed_active(_GPU_SERVER, "203.0.113.20"),
                    },
                ),
                want=_want(
                    {
                        "ssh-key": fnv1.Resource(
                            resource=resource.dict_to_struct(_ssh_key()),
                            ready=fnv1.READY_TRUE,
                        ),
                        "server-management": fnv1.Resource(
                            resource=resource.dict_to_struct(_MANAGEMENT_SERVER),
                            ready=fnv1.READY_TRUE,
                        ),
                        "server-gpu-0": fnv1.Resource(
                            resource=resource.dict_to_struct(_GPU_SERVER),
                            ready=fnv1.READY_TRUE,
                        ),
                        "k3s-cluster": fnv1.Resource(resource=resource.dict_to_struct(_k3s_cluster())),
                    },
                ),
            ),
            Case(
                name="NVIDIA pool workers carry the nvidia.com/gpu taint",
                req=_req(
                    {
                        "ssh-key": _observed_ready(_ssh_key()),
                        "server-management": _observed_active(_MANAGEMENT_SERVER, "203.0.113.10"),
                        "server-h100-0": _observed_active(
                            _server("test-cluster-h100-0", "vbm-64c-2048gb-8-h100-gpu"),
                            "203.0.113.30",
                        ),
                    },
                    pools=[
                        v1alpha1.NodePool(
                            name="h100",
                            plan="vbm-64c-2048gb-8-h100-gpu",
                            gpu=v1alpha1.Gpu(acceleratorType="nvidia-h100"),
                        ),
                    ],
                ),
                want=_want(
                    {
                        "ssh-key": fnv1.Resource(
                            resource=resource.dict_to_struct(_ssh_key()),
                            ready=fnv1.READY_TRUE,
                        ),
                        "server-management": fnv1.Resource(
                            resource=resource.dict_to_struct(_MANAGEMENT_SERVER),
                            ready=fnv1.READY_TRUE,
                        ),
                        "server-h100-0": fnv1.Resource(
                            resource=resource.dict_to_struct(
                                _server("test-cluster-h100-0", "vbm-64c-2048gb-8-h100-gpu"),
                            ),
                            ready=fnv1.READY_TRUE,
                        ),
                        "k3s-cluster": fnv1.Resource(
                            resource=resource.dict_to_struct(
                                {
                                    "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                                    "kind": "K3sCluster",
                                    "spec": {
                                        "controlPlane": {"host": "203.0.113.10"},
                                        "workers": [
                                            {
                                                "name": "h100-0",
                                                "host": "203.0.113.30",
                                                "labels": {
                                                    "modelplane.ai/pool": "h100",
                                                    "modelplane.ai/gpu": "nvidia-h100",
                                                },
                                                "taints": [
                                                    {
                                                        "key": "nvidia.com/gpu",
                                                        "value": "true",
                                                        "effect": "NoSchedule",
                                                    },
                                                ],
                                            },
                                        ],
                                        "auth": {
                                            "username": "root",
                                            "secretRef": {"name": "test-ssh", "key": "ssh-privatekey"},
                                        },
                                        "version": {"channel": "v1.34"},
                                    },
                                },
                            ),
                        ),
                    },
                ),
            ),
            Case(
                name="kubeconfig relayed once the K3sCluster publishes it",
                req=_req(
                    {
                        "ssh-key": _observed_ready(_ssh_key()),
                        "server-management": _observed_active(_MANAGEMENT_SERVER, "203.0.113.10"),
                        "server-gpu-0": _observed_active(_GPU_SERVER, "203.0.113.20"),
                        "k3s-cluster": _observed_k3s_with_secrets(_k3s_cluster()),
                    },
                ),
                want=_want(
                    {
                        "ssh-key": fnv1.Resource(
                            resource=resource.dict_to_struct(_ssh_key()),
                            ready=fnv1.READY_TRUE,
                        ),
                        "server-management": fnv1.Resource(
                            resource=resource.dict_to_struct(_MANAGEMENT_SERVER),
                            ready=fnv1.READY_TRUE,
                        ),
                        "server-gpu-0": fnv1.Resource(
                            resource=resource.dict_to_struct(_GPU_SERVER),
                            ready=fnv1.READY_TRUE,
                        ),
                        "k3s-cluster": fnv1.Resource(
                            resource=resource.dict_to_struct(_k3s_cluster()),
                            ready=fnv1.READY_TRUE,
                        ),
                    },
                    composite=fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "status": {
                                    "secrets": [
                                        {
                                            "type": "Kubeconfig",
                                            "name": "test-cluster-abc12-kubeconfig-d34db",
                                            "key": "kubeconfig",
                                        },
                                    ],
                                },
                            },
                        ),
                    ),
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

    @staticmethod
    def _waiting_want(message: str, result: fnv1.Result) -> fnv1.RunFunctionResponse:
        """A response that only declares the Secret requirement and reports
        why nothing was composed."""
        want = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(),
            conditions=[
                fnv1.Condition(
                    type="ClusterReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForSSHSecret",
                    message=message,
                ),
            ],
            results=[result],
            context=structpb.Struct(),
        )
        want.requirements.resources["ssh-secret"].CopyFrom(_ssh_selector())
        return want
