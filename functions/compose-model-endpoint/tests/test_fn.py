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

"""Tests for the compose-model-endpoint function."""

import base64
import dataclasses
import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.modelendpoint import v1alpha1

_NS = "ml-team"
_NAME = "together-kimi-k2"


@dataclasses.dataclass
class Case:
    """A test case for compose-model-endpoint."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _xr(**spec) -> dict:  # noqa: ANN003
    """The ModelEndpoint XR, built from the generated model so a field the XRD
    doesn't define can't creep into a test."""
    xr = v1alpha1.ModelEndpoint(
        apiVersion="modelplane.ai/v1alpha1",
        kind="ModelEndpoint",
        metadata={"name": _NAME, "namespace": _NS},
        spec=v1alpha1.Spec(origin="https://api.together.xyz", **spec),
    )
    return xr.model_dump(exclude_none=True, mode="json", by_alias=True)


def _secret(name: str, data: dict[str, str]) -> dict:
    """A Secret as the API server stores it, values base64 encoded."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": _NS},
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()},
    }


def _credential_requirement(name: str) -> fnv1.Requirements:
    return fnv1.Requirements(
        resources={"credential": fnv1.ResourceSelector(api_version="v1", kind="Secret", match_name=name, namespace=_NS)}
    )


def _response(
    *,
    reason: str,
    status: fnv1.Status,
    message: str | None = None,
    requirements: fnv1.Requirements | None = None,
) -> fnv1.RunFunctionResponse:
    """The whole response. This function composes no resources, so desired
    carries only the composite, and asserting the whole thing proves it stays
    that way."""
    rsp = fnv1.RunFunctionResponse(
        meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
        desired=fnv1.State(),
        context=structpb.Struct(),
        conditions=[
            fnv1.Condition(type=fn.CONDITION_TYPE_ENDPOINT_READY, status=status, reason=reason, message=message)
        ],
    )
    if requirements is not None:
        rsp.requirements.CopyFrom(requirements)
    if message is not None:
        rsp.results.append(fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message=message))
    return rsp


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:
        cases = [
            Case(
                name="no credential: usable as soon as it exists",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(_xr()))),
                ),
                want=_response(
                    reason=fn.CONDITION_REASON_ENDPOINT_USABLE,
                    status=fnv1.STATUS_CONDITION_TRUE,
                ),
            ),
            Case(
                name="a credential that resolves",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                _xr(credentialRef=v1alpha1.CredentialRef(name="together-api-key"))
                            )
                        )
                    ),
                    required_resources={
                        "credential": fnv1.Resources(
                            items=[
                                fnv1.Resource(
                                    resource=resource.dict_to_struct(_secret("together-api-key", {"apiKey": "sk-abc"}))
                                )
                            ]
                        )
                    },
                ),
                want=_response(
                    reason=fn.CONDITION_REASON_ENDPOINT_USABLE,
                    status=fnv1.STATUS_CONDITION_TRUE,
                    requirements=_credential_requirement("together-api-key"),
                ),
            ),
            Case(
                name="a credential Secret that does not exist",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                _xr(credentialRef=v1alpha1.CredentialRef(name="together-api-key"))
                            )
                        )
                    ),
                    required_resources={"credential": fnv1.Resources(items=[])},
                ),
                want=_response(
                    reason=fn.CONDITION_REASON_CREDENTIAL_MISSING,
                    status=fnv1.STATUS_CONDITION_FALSE,
                    message="Secret together-api-key does not exist",
                    requirements=_credential_requirement("together-api-key"),
                ),
            ),
            Case(
                # A Secret that exists but lacks the key is the likelier mistake,
                # and would otherwise surface as a 401 from the provider.
                name="a credential Secret missing the key",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                _xr(credentialRef=v1alpha1.CredentialRef(name="together-api-key"))
                            )
                        )
                    ),
                    required_resources={
                        "credential": fnv1.Resources(
                            items=[
                                fnv1.Resource(
                                    resource=resource.dict_to_struct(_secret("together-api-key", {"token": "sk-abc"}))
                                )
                            ]
                        )
                    },
                ),
                want=_response(
                    reason=fn.CONDITION_REASON_CREDENTIAL_MISSING,
                    status=fnv1.STATUS_CONDITION_FALSE,
                    message="Secret together-api-key has no key apiKey",
                    requirements=_credential_requirement("together-api-key"),
                ),
            ),
            Case(
                name="a credential under a non-default key",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                _xr(
                                    credentialRef=v1alpha1.CredentialRef(
                                        name="together-api-key", key="TOGETHER_API_KEY"
                                    )
                                )
                            )
                        )
                    ),
                    required_resources={
                        "credential": fnv1.Resources(
                            items=[
                                fnv1.Resource(
                                    resource=resource.dict_to_struct(
                                        _secret("together-api-key", {"TOGETHER_API_KEY": "sk-abc"})
                                    )
                                )
                            ]
                        )
                    },
                ),
                want=_response(
                    reason=fn.CONDITION_REASON_ENDPOINT_USABLE,
                    status=fnv1.STATUS_CONDITION_TRUE,
                    requirements=_credential_requirement("together-api-key"),
                ),
            ),
            Case(
                name="an unresolved credential requirement",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                _xr(credentialRef=v1alpha1.CredentialRef(name="together-api-key"))
                            )
                        )
                    ),
                ),
                want=_response(
                    reason=fn.CONDITION_REASON_WAITING_FOR_CREDENTIAL,
                    status=fnv1.STATUS_CONDITION_FALSE,
                    message="Waiting for Secret together-api-key to resolve",
                    requirements=_credential_requirement("together-api-key"),
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
