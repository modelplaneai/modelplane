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
import dataclasses
import json
from pathlib import Path

import pytest
from crossplane.function import resource
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


@dataclasses.dataclass
class ReadyCopiesCase:
    """The operator counts multi-node gangs and single-node pods differently."""

    name: str
    reason: str
    components: dict
    want: int


READY_COPIES_CASES = [
    ReadyCopiesCase(
        name="UnobservedComponent",
        reason="A graph without component status contributes no serving capacity.",
        components={},
        want=0,
    ),
    ReadyCopiesCase(
        name="MultiNodeGangs",
        reason="A scaling group's available replicas count whole engine copies, without readyReplicas.",
        components={"engine-main": {"componentKind": "PodCliqueScalingGroup", "availableReplicas": 2}},
        want=2,
    ),
    ReadyCopiesCase(
        name="SingleNodeCliques",
        reason="A single-node Grove component reports readyReplicas rather than availableReplicas.",
        components={"engine-main": {"componentKind": "PodClique", "readyReplicas": 3}},
        want=3,
    ),
    ReadyCopiesCase(
        name="UnavailableDeployment",
        reason="An explicit zero available count takes precedence over ready pods from an older revision.",
        components={"engine-main": {"componentKind": "Deployment", "availableReplicas": 0, "readyReplicas": 3}},
        want=0,
    ),
]


@pytest.mark.parametrize("case", READY_COPIES_CASES, ids=lambda case: case.name)
def test_ready_copies(case: ReadyCopiesCase) -> None:
    """Read graph counters in component-replica units."""
    engine = v1alpha1.Engine(
        name="main",
        members=[
            v1alpha1.Member(
                nodePoolName="frontier",
                template=v1alpha1.Template(
                    spec=v1alpha1.Spec(
                        containers=[
                            v1alpha1.Container(name="engine", image="nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.6.1")
                        ]
                    )
                ),
            )
        ],
    )
    observed = {
        "workload-dynamo": fnv1.Resource(
            resource=resource.dict_to_struct(
                {
                    "spec": {"forProvider": {"manifest": {}}},
                    "status": {"atProvider": {"manifest": {"status": {"components": case.components}}}},
                }
            )
        )
    }
    assert dynamo.ready_copies(engine, observed) == case.want, case.reason
