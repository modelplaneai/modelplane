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

_NAMESPACE = "test-ns"
_PC = "test-cluster"

_RELEASE = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "metadata": {"namespace": _NAMESPACE},
    "spec": {
        "providerConfigRef": {"kind": "ProviderConfig", "name": _PC},
        "forProvider": {"chart": {"name": "cert-manager"}},
    },
}

_OBJECT = {
    "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
    "kind": "Object",
    "metadata": {"namespace": _NAMESPACE},
    "spec": {
        "providerConfigRef": {"kind": "ProviderConfig", "name": _PC},
        "forProvider": {"manifest": {"apiVersion": "v1", "kind": "Namespace"}},
    },
}

# Not a consumer kind: a ProviderConfig gets no Usage of its own.
_PROVIDER_CONFIG = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "ProviderConfig",
    "metadata": {"name": _PC, "namespace": _NAMESPACE},
    "spec": {},
}

# A consumer kind (Object) that references no ProviderConfig: gets no Usage.
_OBJECT_NO_PC = {
    "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
    "kind": "Object",
    "metadata": {"namespace": _NAMESPACE},
    "spec": {"forProvider": {"manifest": {"apiVersion": "v1", "kind": "ConfigMap"}}},
}

# A Release that already carries a label, to check relabeling preserves it.
_RELEASE_WITH_LABEL = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "metadata": {"namespace": _NAMESPACE, "labels": {"existing": "keep"}},
    "spec": {
        "providerConfigRef": {"kind": "ProviderConfig", "name": _PC},
        "forProvider": {"chart": {"name": "prometheus"}},
    },
}


def _labelled(d: dict, consumer: str) -> dict:
    """A copy of d with the usage-consumer label stamped on it."""
    out = {**d, "metadata": {**d.get("metadata", {})}}
    out["metadata"]["labels"] = {
        **d.get("metadata", {}).get("labels", {}),
        "modelplane.ai/usage-consumer": consumer,
    }
    return out


def _usage(api_version: str, kind: str, consumer: str) -> dict:
    return {
        "apiVersion": "protection.crossplane.io/v1beta1",
        "kind": "Usage",
        "metadata": {"namespace": _NAMESPACE},
        "spec": {
            "of": {
                "apiVersion": api_version,
                "kind": "ProviderConfig",
                "resourceRef": {"name": _PC},
            },
            "by": {
                "apiVersion": api_version,
                "kind": kind,
                "resourceSelector": {
                    "matchControllerRef": True,
                    "matchLabels": {"modelplane.ai/usage-consumer": consumer},
                },
            },
            "replayDeletion": True,
        },
    }


def _composite(namespace: str | None = _NAMESPACE) -> structpb.Struct:
    metadata = {"name": "test"}
    if namespace is not None:
        metadata["namespace"] = namespace
    return resource.dict_to_struct(
        {
            "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
            "kind": "ServingStack",
            "metadata": metadata,
        }
    )


@dataclasses.dataclass
class Case:
    """A test case for compose-usages."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


COMPOSE_CASES = [
    Case(
        name="labels each consumer and composes a Usage per ProviderConfig reference",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=fnv1.Resource(resource=_composite())),
            desired=fnv1.State(
                composite=fnv1.Resource(resource=_composite()),
                resources={
                    "cert-manager": fnv1.Resource(resource=resource.dict_to_struct(_RELEASE)),
                    "gateway-namespace": fnv1.Resource(resource=resource.dict_to_struct(_OBJECT)),
                    "prometheus": fnv1.Resource(resource=resource.dict_to_struct(_RELEASE_WITH_LABEL)),
                    "config-map": fnv1.Resource(resource=resource.dict_to_struct(_OBJECT_NO_PC)),
                    "provider-config-helm": fnv1.Resource(resource=resource.dict_to_struct(_PROVIDER_CONFIG)),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(resource=_composite()),
                resources={
                    "cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(_labelled(_RELEASE, "cert-manager")),
                    ),
                    "gateway-namespace": fnv1.Resource(
                        resource=resource.dict_to_struct(_labelled(_OBJECT, "gateway-namespace")),
                    ),
                    # Existing labels are preserved when the consumer label is stamped.
                    "prometheus": fnv1.Resource(
                        resource=resource.dict_to_struct(_labelled(_RELEASE_WITH_LABEL, "prometheus")),
                    ),
                    # An Object with no providerConfigRef is left untouched, no Usage.
                    "config-map": fnv1.Resource(
                        resource=resource.dict_to_struct(_OBJECT_NO_PC),
                    ),
                    "provider-config-helm": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROVIDER_CONFIG),
                    ),
                    "usage-pc-cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            _usage("helm.m.crossplane.io/v1beta1", "Release", "cert-manager")
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-pc-gateway-namespace": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            _usage("kubernetes.m.crossplane.io/v1alpha1", "Object", "gateway-namespace")
                        ),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-pc-prometheus": fnv1.Resource(
                        resource=resource.dict_to_struct(
                            _usage("helm.m.crossplane.io/v1beta1", "Release", "prometheus")
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
            observed=fnv1.State(composite=fnv1.Resource(resource=_composite(namespace=None))),
            desired=fnv1.State(
                composite=fnv1.Resource(resource=_composite(namespace=None)),
                resources={
                    "cert-manager": fnv1.Resource(resource=resource.dict_to_struct(_RELEASE)),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(resource=_composite(namespace=None)),
                resources={
                    "cert-manager": fnv1.Resource(resource=resource.dict_to_struct(_RELEASE)),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
    Case(
        name="no consumers means no Usages",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=fnv1.Resource(resource=_composite())),
            desired=fnv1.State(
                composite=fnv1.Resource(resource=_composite()),
                resources={
                    "provider-config-helm": fnv1.Resource(resource=resource.dict_to_struct(_PROVIDER_CONFIG)),
                },
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(resource=_composite()),
                resources={
                    "provider-config-helm": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROVIDER_CONFIG),
                    ),
                },
            ),
            context=structpb.Struct(),
        ),
    ),
]


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction labels consumers and composes their Usages."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want)
