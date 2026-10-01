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

"""Tests for the compose-usages function."""

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


@dataclasses.dataclass
class Case:
    """A test case for compose-usages."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


# compose-usages reads only the observed composite's namespace, and passes the
# desired one through. The ServingStack model requires a spec.cloud the function
# never reads, so both composites are bare dicts rather than built from the
# model.
def _serving_stack(*, namespace: str | None) -> fnv1.Resource:
    """The bare observed ServingStack composite, in namespace unless it's None."""
    metadata = {"name": "test"}
    if namespace is not None:
        metadata["namespace"] = namespace
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                "kind": "ServingStack",
                "metadata": metadata,
            }
        )
    )


def _desired_serving_stack(*, namespace: str | None) -> fnv1.Resource:
    """The bare desired ServingStack composite, in namespace unless it's None."""
    metadata = {"name": "test"}
    if namespace is not None:
        metadata["namespace"] = namespace
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
                "kind": "ServingStack",
                "metadata": metadata,
            }
        )
    )


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


COMPOSE_CASES = [
    Case(
        name="labels each consumer and composes a Usage per ProviderConfig reference",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(namespace="test-ns"),
            ),
            desired=fnv1.State(
                composite=_desired_serving_stack(namespace="test-ns"),
                resources={
                    "cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {"namespace": "test-ns"},
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-cluster"},
                                    "forProvider": {"chart": {"name": "cert-manager"}},
                                },
                            }
                        )
                    ),
                    "gateway-namespace": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"namespace": "test-ns"},
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-cluster"},
                                    "forProvider": {"manifest": {"apiVersion": "v1", "kind": "Namespace"}},
                                },
                            }
                        )
                    ),
                    # A Release that already carries a label, which the
                    # function's label must not replace.
                    "prometheus": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {"namespace": "test-ns", "labels": {"existing": "keep"}},
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-cluster"},
                                    "forProvider": {"chart": {"name": "prometheus"}},
                                },
                            }
                        )
                    ),
                    # A consumer kind that references no ProviderConfig.
                    "config-map": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"namespace": "test-ns"},
                                "spec": {"forProvider": {"manifest": {"apiVersion": "v1", "kind": "ConfigMap"}}},
                            }
                        )
                    ),
                    # Not a consumer kind.
                    "provider-config-helm": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "ProviderConfig",
                                "metadata": {"name": "test-cluster", "namespace": "test-ns"},
                                "spec": {},
                            }
                        )
                    ),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_serving_stack(namespace="test-ns"),
                resources={
                    "cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "namespace": "test-ns",
                                    "labels": {"modelplane.ai/usage-consumer": "cert-manager"},
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-cluster"},
                                    "forProvider": {"chart": {"name": "cert-manager"}},
                                },
                            }
                        )
                    ),
                    "gateway-namespace": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {
                                    "namespace": "test-ns",
                                    "labels": {"modelplane.ai/usage-consumer": "gateway-namespace"},
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-cluster"},
                                    "forProvider": {"manifest": {"apiVersion": "v1", "kind": "Namespace"}},
                                },
                            }
                        )
                    ),
                    "prometheus": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {
                                    "namespace": "test-ns",
                                    "labels": {"existing": "keep", "modelplane.ai/usage-consumer": "prometheus"},
                                },
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-cluster"},
                                    "forProvider": {"chart": {"name": "prometheus"}},
                                },
                            }
                        )
                    ),
                    "config-map": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                "kind": "Object",
                                "metadata": {"namespace": "test-ns"},
                                "spec": {"forProvider": {"manifest": {"apiVersion": "v1", "kind": "ConfigMap"}}},
                            }
                        )
                    ),
                    "provider-config-helm": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "ProviderConfig",
                                "metadata": {"name": "test-cluster", "namespace": "test-ns"},
                                "spec": {},
                            }
                        )
                    ),
                    "usage-pc-cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "metadata": {"namespace": "test-ns"},
                                "spec": {
                                    "of": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "ProviderConfig",
                                        "resourceRef": {"name": "test-cluster"},
                                    },
                                    "by": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/usage-consumer": "cert-manager"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-pc-gateway-namespace": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "metadata": {"namespace": "test-ns"},
                                "spec": {
                                    "of": {
                                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                        "kind": "ProviderConfig",
                                        "resourceRef": {"name": "test-cluster"},
                                    },
                                    "by": {
                                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                                        "kind": "Object",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/usage-consumer": "gateway-namespace"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-pc-prometheus": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "protection.crossplane.io/v1beta1",
                                "kind": "Usage",
                                "metadata": {"namespace": "test-ns"},
                                "spec": {
                                    "of": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "ProviderConfig",
                                        "resourceRef": {"name": "test-cluster"},
                                    },
                                    "by": {
                                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                                        "kind": "Release",
                                        "resourceSelector": {
                                            "matchControllerRef": True,
                                            "matchLabels": {"modelplane.ai/usage-consumer": "prometheus"},
                                        },
                                    },
                                    "replayDeletion": True,
                                },
                            }
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    Case(
        name="no Usages when the composite has no namespace",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(namespace=None),
            ),
            desired=fnv1.State(
                composite=_desired_serving_stack(namespace=None),
                resources={
                    "cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {"namespace": "test-ns"},
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-cluster"},
                                    "forProvider": {"chart": {"name": "cert-manager"}},
                                },
                            }
                        )
                    ),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_serving_stack(namespace=None),
                resources={
                    "cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "Release",
                                "metadata": {"namespace": "test-ns"},
                                "spec": {
                                    "providerConfigRef": {"kind": "ProviderConfig", "name": "test-cluster"},
                                    "forProvider": {"chart": {"name": "cert-manager"}},
                                },
                            }
                        )
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    Case(
        name="no consumers means no Usages",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=_serving_stack(namespace="test-ns"),
            ),
            desired=fnv1.State(
                composite=_desired_serving_stack(namespace="test-ns"),
                resources={
                    "provider-config-helm": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "ProviderConfig",
                                "metadata": {"name": "test-cluster", "namespace": "test-ns"},
                                "spec": {},
                            }
                        )
                    ),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=_desired_serving_stack(namespace="test-ns"),
                resources={
                    "provider-config-helm": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            {
                                "apiVersion": "helm.m.crossplane.io/v1beta1",
                                "kind": "ProviderConfig",
                                "metadata": {"name": "test-cluster", "namespace": "test-ns"},
                                "spec": {},
                            }
                        )
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction labels consumers and composes their Usages."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want)
