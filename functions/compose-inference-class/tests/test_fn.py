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

import dataclasses
import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.inferenceclass import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


@dataclasses.dataclass
class Case:
    """A test case for compose-inference-class."""

    name: str
    req: fnv1.RunFunctionRequest
    want: fnv1.RunFunctionResponse


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    """Tests for FunctionRunner.RunFunction."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_compose(self) -> None:
        """The function marks the InferenceClass as ready."""
        cases = [
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
                                ).model_dump(exclude_none=True, mode="json")
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
                    conditions=[
                        fnv1.Condition(
                            type="Accepted",
                            status=fnv1.STATUS_CONDITION_TRUE,
                            reason="Available",
                        ),
                    ],
                    context=structpb.Struct(),
                ),
            ),
        ]

        cases.append(
            Case(
                name="accepts an AMD class provisioned on VultrBaremetal",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                v1alpha1.InferenceClass(
                                    metadata=metav1.ObjectMeta(name="gpu-mi355x"),
                                    spec=v1alpha1.Spec(
                                        provisioning=v1alpha1.Provisioning(
                                            provider="VultrBaremetal",
                                            vultrBaremetal=v1alpha1.VultrBaremetal(
                                                plan="vbm-256c-3072gb-8-mi355x-gpu",
                                                accelerator=v1alpha1.AcceleratorModel4(type="amd-mi355x", count=8),
                                            ),
                                        ),
                                        devices=[
                                            v1alpha1.Device(
                                                name="gpu",
                                                claim="DRA",
                                                driver="gpu.amd.com",
                                                deviceClassName="gpu.amd.com",
                                                count=8,
                                                capacity={"memory": v1alpha1.Capacity(value="288Gi")},
                                            ),
                                        ],
                                    ),
                                ).model_dump(exclude_none=True, mode="json")
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
                    conditions=[
                        fnv1.Condition(
                            type="Accepted",
                            status=fnv1.STATUS_CONDITION_TRUE,
                            reason="Available",
                        ),
                    ],
                    context=structpb.Struct(),
                ),
            ),
        )

        cases.append(
            Case(
                name="rejects an AMD class provisioned on an NVIDIA-only provider",
                req=fnv1.RunFunctionRequest(
                    observed=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct(
                                v1alpha1.InferenceClass(
                                    metadata=metav1.ObjectMeta(name="gpu-mi355x-vke"),
                                    spec=v1alpha1.Spec(
                                        provisioning=v1alpha1.Provisioning(
                                            provider="Vultr",
                                            vultr=v1alpha1.Vultr(
                                                plan="vcg-mi355x-hypothetical",
                                                accelerator=v1alpha1.AcceleratorModel3(type="amd-mi355x", count=1),
                                            ),
                                        ),
                                        devices=[
                                            v1alpha1.Device(
                                                name="gpu",
                                                claim="DRA",
                                                driver="gpu.amd.com",
                                                deviceClassName="gpu.amd.com",
                                                count=1,
                                                capacity={"memory": v1alpha1.Capacity(value="288Gi")},
                                            ),
                                        ],
                                    ),
                                ).model_dump(exclude_none=True, mode="json")
                            ),
                        ),
                    ),
                ),
                want=fnv1.RunFunctionResponse(
                    meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
                    desired=fnv1.State(
                        composite=fnv1.Resource(
                            resource=resource.dict_to_struct({"status": {}}),
                        ),
                    ),
                    conditions=[
                        fnv1.Condition(
                            type="Accepted",
                            status=fnv1.STATUS_CONDITION_FALSE,
                            reason="UnsupportedDevices",
                            message=(
                                "AMD devices are not supported on Vultr: "
                                "its serving stack installs only NVIDIA accelerator stacks"
                            ),
                        ),
                    ],
                    results=[
                        fnv1.Result(
                            severity=fnv1.SEVERITY_WARNING,
                            message=(
                                "AMD devices are not supported on Vultr: "
                                "its serving stack installs only NVIDIA accelerator stacks"
                            ),
                        ),
                    ],
                    context=structpb.Struct(),
                ),
            ),
        )

        for case in cases:
            with self.subTest(case.name):
                got = await self.runner.RunFunction(case.req, None)
                self.assertEqual(
                    json_format.MessageToDict(case.want),
                    json_format.MessageToDict(got),
                    "-want, +got",
                )
