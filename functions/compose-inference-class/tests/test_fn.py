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

        for case in cases:
            with self.subTest(case.name):
                got = await self.runner.RunFunction(case.req, None)
                self.assertEqual(
                    json_format.MessageToDict(case.want),
                    json_format.MessageToDict(got),
                    "-want, +got",
                )
