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
import copy
import dataclasses
import json
from pathlib import Path

import pytest
from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import dynamo, fn
from function.backends import base
from function.backends.llmd import LLMDBackend
from function.backends.native import NativeBackend
from google.protobuf import json_format
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


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


@pytest.mark.parametrize(
    "path", sorted((Path(__file__).parent / "testdata" / "standard-templates").glob("*.json")), ids=lambda p: p.stem
)
def test_standard_templates(path: Path) -> None:
    """Standard preserves the same user-authored native sidecars and engine probes."""
    case = json.loads(path.read_text())
    replica = v1alpha1.ModelReplica(**case["replica"])
    backend = NativeBackend() if case["backend"] == "native" else LLMDBackend()
    got = {
        key: obj.model_dump(exclude_unset=True, by_alias=True)
        for key, obj in backend.build(
            replica, replica.spec.engines[0], case["provider_config"], case["serving_label"], case["stack"]
        ).items()
    }
    assert got == case["want"], case["reason"]


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
    replica = v1alpha1.ModelReplica(
        metadata=metav1.ObjectMeta(name="r", namespace="ml-team"),
        spec=v1alpha1.SpecModel2(clusterName="cluster-a", engines=[engine]),
    )
    observed = {
        "workload-dynamo-f66c7d7533": fnv1.Resource(
            resource=resource.dict_to_struct(
                {
                    "spec": {"forProvider": {"manifest": {}}},
                    "status": {"atProvider": {"manifest": {"status": {"components": case.components}}}},
                }
            )
        )
    }
    assert dynamo.ready_copies(replica, engine, observed) == case.want, case.reason


@dataclasses.dataclass
class RuntimePortsCase:
    """Declared ports must retain the operator's runtime probe and telemetry contracts."""

    name: str
    reason: str
    container: dict
    want: dict


RUNTIME_PORTS_CASES = [
    RuntimePortsCase(
        name="InheritedDefaults",
        reason="Without authored ports or overrides, the operator supplies the default ports.",
        container={"name": "main"},
        want={"name": "main"},
    ),
    RuntimePortsCase(
        name="EmptyDeclaredPorts",
        reason="An explicit empty port list still replaces defaults and must include named runtime ports.",
        container={"ports": []},
        want={"ports": [{"name": "system", "containerPort": 9090}]},
    ),
    RuntimePortsCase(
        name="DeclaredHTTP",
        reason="An authored HTTP port keeps the named system probe port.",
        container={"ports": [{"name": "http", "containerPort": 8001}]},
        want={
            "ports": [
                {"name": "http", "containerPort": 8001},
                {"name": "system", "containerPort": 9090},
            ]
        },
    ),
    RuntimePortsCase(
        name="UnnamedSystem",
        reason="An unnamed TCP system port acquires its runtime name without a duplicate map key.",
        container={"ports": [{"containerPort": 9090}]},
        want={"ports": [{"name": "system", "containerPort": 9090}]},
    ),
    RuntimePortsCase(
        name="UnnamedNIXL",
        reason="An existing unnamed NIXL port keeps its number and name.",
        container={"ports": [{"containerPort": 19090, "protocol": "TCP"}]},
        want={
            "ports": [
                {"containerPort": 19090, "protocol": "TCP"},
                {"name": "system", "containerPort": 9090},
            ]
        },
    ),
    RuntimePortsCase(
        name="DifferentProtocol",
        reason="UDP and TCP ports have different list-map keys even when their numbers match.",
        container={"ports": [{"name": "udp", "containerPort": 9090, "protocol": "UDP"}]},
        want={
            "ports": [
                {"name": "udp", "containerPort": 9090, "protocol": "UDP"},
                {"name": "system", "containerPort": 9090},
            ]
        },
    ),
    RuntimePortsCase(
        name="ConfiguredUnnamedSystem",
        reason="An unnamed custom system port is reused when its runtime number is configured.",
        container={"ports": [{"containerPort": 9091}], "env": [{"name": "DYN_SYSTEM_PORT", "value": "9091"}]},
        want={
            "ports": [{"name": "system", "containerPort": 9091}],
            "env": [{"name": "DYN_SYSTEM_PORT", "value": "9091"}],
        },
    ),
    RuntimePortsCase(
        name="ExistingSystem",
        reason="An existing system port is retained once and agrees with its runtime environment.",
        container={"ports": [{"name": "system", "containerPort": 9090}]},
        want={
            "ports": [{"name": "system", "containerPort": 9090}],
            "env": [{"name": "DYN_SYSTEM_PORT", "value": "9090"}],
        },
    ),
    RuntimePortsCase(
        name="ConfiguredSystem",
        reason="A configured runtime system port becomes the named probe target.",
        container={"env": [{"name": "DYN_SYSTEM_PORT", "value": "9091"}]},
        want={
            "env": [{"name": "DYN_SYSTEM_PORT", "value": "9091"}],
            "ports": [{"name": "system", "containerPort": 9091}],
        },
    ),
    RuntimePortsCase(
        name="CustomDeclaredSystem",
        reason="A custom named system port configures the runtime to listen on the same port.",
        container={"ports": [{"name": "system", "containerPort": 9091}]},
        want={
            "ports": [{"name": "system", "containerPort": 9091}],
            "env": [{"name": "DYN_SYSTEM_PORT", "value": "9091"}],
        },
    ),
]


@pytest.mark.parametrize("case", RUNTIME_PORTS_CASES, ids=lambda case: case.name)
def test_runtime_ports(case: RuntimePortsCase) -> None:
    """Preserve named probe targets when the operator replaces its port list."""
    got = copy.deepcopy(case.container)  # noqa: MPT401  # The helper mutates its fresh container dump.
    dynamo._runtime_ports(got)
    assert got == case.want, case.reason


@dataclasses.dataclass
class InvalidRuntimePortsCase:
    """Reject runtime settings that cannot agree with a Kubernetes named probe port."""

    name: str
    reason: str
    container: dict
    want: str


INVALID_RUNTIME_PORTS_CASES = [
    InvalidRuntimePortsCase(
        name="DuplicateAuthoredPorts",
        reason="Explicit runtime names cannot hide duplicate native port-list map keys.",
        container={
            "ports": [
                {"name": "system", "containerPort": 9090},
                {"name": "nixl", "containerPort": 9090, "protocol": "TCP"},
            ]
        },
        want="Dynamo runtime container ports must have unique port numbers per protocol",
    ),
    InvalidRuntimePortsCase(
        name="NamedNumberConflict",
        reason="A differently named TCP port cannot silently be duplicated or lose its authored name.",
        container={"ports": [{"name": "health", "containerPort": 9090}]},
        want="Dynamo system port 9090 conflicts with named port health",
    ),
    InvalidRuntimePortsCase(
        name="InvalidSystemNumber",
        reason="A runtime system port outside the TCP port range cannot be probed.",
        container={"env": [{"name": "DYN_SYSTEM_PORT", "value": "65536"}]},
        want="DYN_SYSTEM_PORT must be between 1 and 65535",
    ),
    InvalidRuntimePortsCase(
        name="ConflictingSystem",
        reason="A declared port and a literal runtime environment override must agree.",
        container={
            "ports": [{"name": "system", "containerPort": 9090}],
            "env": [{"name": "DYN_SYSTEM_PORT", "value": "9091"}],
        },
        want="Dynamo runtime port system must match DYN_SYSTEM_PORT",
    ),
    InvalidRuntimePortsCase(
        name="DynamicSystem",
        reason="An environment reference needs an explicit named port because its value is unknown at composition.",
        container={
            "env": [{"name": "DYN_SYSTEM_PORT", "valueFrom": {"secretKeyRef": {"name": "settings", "key": "port"}}}]
        },
        want="Declare the named system port when DYN_SYSTEM_PORT uses valueFrom",
    ),
    InvalidRuntimePortsCase(
        name="NonTCPSystem",
        reason="HTTP runtime probes require the named system port to use TCP.",
        container={"ports": [{"name": "system", "containerPort": 9090, "protocol": "UDP"}]},
        want="Dynamo runtime port system must use TCP",
    ),
]


@pytest.mark.parametrize("case", INVALID_RUNTIME_PORTS_CASES, ids=lambda case: case.name)
def test_invalid_runtime_ports(case: InvalidRuntimePortsCase) -> None:
    """Fail before composing a graph whose runtime probes cannot resolve correctly."""
    container = copy.deepcopy(case.container)  # noqa: MPT401  # The helper mutates its fresh container dump.
    with pytest.raises(ValueError, match=case.want):
        dynamo._runtime_ports(container)


@pytest.mark.parametrize(
    "path", sorted((Path(__file__).parent / "testdata" / "dynamo-identity").glob("*.json")), ids=lambda p: p.stem
)
def test_graph_identity(path: Path) -> None:
    """Immutable backend and topology transitions replace both the Object and its remote graph."""
    case = json.loads(path.read_text())
    replica = v1alpha1.ModelReplica(**case["replica"])
    got = {"key": base.dynamo_workload_key(replica), "name": dynamo.graph_name(replica)}
    assert got == case["want"], case["reason"]
