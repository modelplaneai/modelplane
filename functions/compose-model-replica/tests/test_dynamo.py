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

"""DGD contracts replacing the former Grove lowering cases.

Large complete inputs and outputs live in JSON so role templates remain readable
without hiding differences in test builders. They are literal expectations, not
snapshots updated by the code under test.
"""

import asyncio
import json
from pathlib import Path

import pytest
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import dynamo, fn
from google.protobuf import json_format
from models.ai.modelplane.modelreplica import v1alpha1


@pytest.mark.parametrize(
    "path", sorted((Path(__file__).parent / "testdata" / "dynamo").glob("*.json")), ids=lambda p: p.stem
)
def test_build(path: Path) -> None:
    """Compose the entire graph and its claims, preserving each member's contract."""
    case = json.loads(path.read_text())
    replica = v1alpha1.ModelReplica(**case["replica"])
    got = {
        key: obj.model_dump(exclude_unset=True, by_alias=True)
        for key, obj in dynamo.build(replica, case["provider_config"]).items()
    }
    assert got == case["want"], case["reason"]
    assert dynamo.validate(replica) == case["validation"], case["reason"]


@pytest.mark.parametrize(
    "path", sorted((Path(__file__).parent / "testdata" / "dynamo-function").glob("*.json")), ids=lambda p: p.stem
)
def test_compose(path: Path) -> None:
    """Compare complete function responses, including routing and all readiness conditions."""
    case = json.loads(path.read_text())
    req = json_format.ParseDict(case["request"], fnv1.RunFunctionRequest())
    want = json_format.ParseDict(case["want"], fnv1.RunFunctionResponse())
    got = asyncio.run(fn.FunctionRunner().RunFunction(req, None))
    assert got == want, case["reason"]


@pytest.mark.parametrize(
    "path", sorted((Path(__file__).parent / "testdata" / "dynamo-validation").glob("*.json")), ids=lambda p: p.stem
)
def test_validate(path: Path) -> None:
    """Reject unsupported runtime and router choices before applying a graph."""
    case = json.loads(path.read_text())
    replica = v1alpha1.ModelReplica(**case["replica"])
    assert dynamo.validate(replica) == case["want"], case["reason"]
