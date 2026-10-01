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

"""Tests for the compose-inference-class function."""

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
from models.ai.modelplane.inferenceclass import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-inference-class."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def _to_dict(msg: message.Message) -> dict:
    """msg as a dict with sorted keys, so pytest's diff of two lines them up."""
    return json.loads(json_format.MessageToJson(msg, sort_keys=True))


COMPOSE_CASES = [
    Case(
        name="marks XR ready with Accepted condition and empty status",
        req=fnv1.RunFunctionRequest(
            observed=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        v1alpha1.InferenceClass(
                            metadata=metav1.ObjectMeta(name="gpu-l4"),
                            spec=v1alpha1.Spec(
                                devices=[
                                    v1alpha1.Device(
                                        name="gpu",
                                        claim="DRA",
                                        driver="gpu.nvidia.com",
                                        deviceClassName="gpu.nvidia.com",
                                        count=1,
                                        capacity={"memory": v1alpha1.Capacity(value="24Gi")},
                                    ),
                                ],
                            ),
                        ).model_dump(exclude_none=True, mode="json", by_alias=True)
                    ),
                ),
            ),
        ),
        want=fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct({"status": {}}),
                    ready=fnv1.READY_TRUE,
                ),
            ),
            context=structpb.Struct(),
            conditions=[
                fnv1.Condition(
                    type="Accepted",
                    status=fnv1.STATUS_CONDITION_TRUE,
                    reason="Available",
                ),
            ],
        ),
    ),
]


@pytest.mark.parametrize("case", COMPOSE_CASES, ids=lambda case: case.name)
def test_compose(case: Case) -> None:
    """RunFunction marks the InferenceClass ready."""
    got = asyncio.run(fn.FunctionRunner().RunFunction(case.req, None))
    assert _to_dict(got) == _to_dict(case.want)
