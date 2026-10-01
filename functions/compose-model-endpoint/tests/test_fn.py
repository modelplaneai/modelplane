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
from models.ai.modelplane.modelendpoint import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-model-endpoint."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _model_endpoint(*, credential_key: str | None) -> fnv1.Resource:
    """The together-kimi-k2 XR, with an API key under credential_key of together-api-key, or no credential if None."""
    credential = None
    if credential_key is not None:
        credential = v1alpha1.Credential(
            method="APIKey",
            apiKey=v1alpha1.ApiKey(secretRef=v1alpha1.SecretRef(name="together-api-key", key=credential_key)),
        )
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            v1alpha1.ModelEndpoint(
                apiVersion="modelplane.ai/v1alpha1",
                kind="ModelEndpoint",
                metadata=metav1.ObjectMeta(name="together-kimi-k2", namespace="ml-team"),
                spec=v1alpha1.Spec(origin="https://api.together.xyz", credential=credential),
            ).model_dump(exclude_none=True, mode="json", by_alias=True)
        )
    )


def _credential_secret(*, key: str) -> fnv1.Resource:
    """The together-api-key Secret, as the credential requirement returns it, holding sk-abc under key."""
    return fnv1.Resource(
        resource=resource.dict_to_struct(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "together-api-key", "namespace": "ml-team"},
                # Base64 encoded, as the API server stores it. c2stYWJj is
                # "sk-abc".
                "data": {key: "c2stYWJj"},
            }
        )
    )


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


# This function composes no resources, so desired carries only the composite's
# readiness, which mirrors EndpointReady. Comparing the whole response proves it
# stays that way. The desired XR is written inline although every case has it:
# it's a bare fnv1.Resource carrying only readiness, so a helper would only
# rename its constructor.
COMPOSE_CASES = [
    Case(
        name="no credential: usable as soon as it exists",
        req=fnv1.RunFunctionRequest(observed=fnv1.State(composite=_model_endpoint(credential_key=None))),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=fnv1.Resource(ready=fnv1.READY_TRUE)),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(type="EndpointReady", status=fnv1.STATUS_CONDITION_TRUE, reason="EndpointUsable"),
            ],
        ),
    ),
    Case(
        name="a credential that resolves: usable",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_model_endpoint(credential_key="apiKey")),
            required_resources={"credential": fnv1.Resources(items=[_credential_secret(key="apiKey")])},
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=fnv1.Resource(ready=fnv1.READY_TRUE)),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "credential": fnv1.ResourceSelector(
                        api_version="v1", kind="Secret", match_name="together-api-key", namespace="ml-team"
                    )
                }
            ),
            conditions=[
                fnv1.Condition(type="EndpointReady", status=fnv1.STATUS_CONDITION_TRUE, reason="EndpointUsable"),
            ],
        ),
    ),
    Case(
        name="a credential Secret that does not exist: credential missing",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_model_endpoint(credential_key="apiKey")),
            required_resources={"credential": fnv1.Resources()},
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=fnv1.Resource(ready=fnv1.READY_FALSE)),
            results=[fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Secret together-api-key does not exist")],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "credential": fnv1.ResourceSelector(
                        api_version="v1", kind="Secret", match_name="together-api-key", namespace="ml-team"
                    )
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="EndpointReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="CredentialMissing",
                    message="Secret together-api-key does not exist",
                ),
            ],
        ),
    ),
    # A Secret that exists but lacks the key is the likelier mistake, and would
    # otherwise surface as a 401 from the provider.
    Case(
        name="a credential Secret missing the key: credential missing",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_model_endpoint(credential_key="apiKey")),
            required_resources={"credential": fnv1.Resources(items=[_credential_secret(key="token")])},
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=fnv1.Resource(ready=fnv1.READY_FALSE)),
            results=[fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Secret together-api-key has no key apiKey")],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "credential": fnv1.ResourceSelector(
                        api_version="v1", kind="Secret", match_name="together-api-key", namespace="ml-team"
                    )
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="EndpointReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="CredentialMissing",
                    message="Secret together-api-key has no key apiKey",
                ),
            ],
        ),
    ),
    Case(
        name="a credential under a non-default key: usable",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_model_endpoint(credential_key="TOGETHER_API_KEY")),
            required_resources={"credential": fnv1.Resources(items=[_credential_secret(key="TOGETHER_API_KEY")])},
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=fnv1.Resource(ready=fnv1.READY_TRUE)),
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "credential": fnv1.ResourceSelector(
                        api_version="v1", kind="Secret", match_name="together-api-key", namespace="ml-team"
                    )
                }
            ),
            conditions=[
                fnv1.Condition(type="EndpointReady", status=fnv1.STATUS_CONDITION_TRUE, reason="EndpointUsable"),
            ],
        ),
    ),
    Case(
        name="an unresolved credential requirement: wait for it to resolve",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(composite=_model_endpoint(credential_key="apiKey")),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(composite=fnv1.Resource(ready=fnv1.READY_FALSE)),
            results=[
                fnv1.Result(severity=fnv1.SEVERITY_NORMAL, message="Waiting for Secret together-api-key to resolve")
            ],
            context=structpb.Struct(),
            requirements=fnv1.Requirements(
                resources={
                    "credential": fnv1.ResourceSelector(
                        api_version="v1", kind="Secret", match_name="together-api-key", namespace="ml-team"
                    )
                }
            ),
            conditions=[
                fnv1.Condition(
                    type="EndpointReady",
                    status=fnv1.STATUS_CONDITION_FALSE,
                    reason="WaitingForCredential",
                    message="Waiting for Secret together-api-key to resolve",
                ),
            ],
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction reports whether the endpoint's credential makes it usable."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want)
