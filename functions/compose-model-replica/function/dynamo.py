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

"""Compose one Dynamo graph per ModelReplica; the operator owns its workloads.

Engines run the Dynamo runtime in main. Member templates retain their commands,
sidecars, placement, and device claims. A shared frontend routes requests to all
workers in the graph, including native prefill/decode handoffs.
"""

from collections.abc import Mapping

from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

from function.backends import base

FRONTEND = "frontend"
# Match the serving-stack chart pin. NGC nightly tags carry the build date and
# commit rather than a semantic runtime version, so DGD admission needs this.
NIGHTLY_TAG = "20261103-e37f0b9"
NIGHTLY_RUNTIME_VERSION = "1.7.0"
_MAX_PORT = 65535
READY_CEL = (
    "has(object.status) && has(object.status.observedGeneration) && "
    "object.status.observedGeneration == object.metadata.generation && "
    "has(object.status.conditions) && "
    'object.status.conditions.exists(c, c.type == "Ready" && c.status == "True")'
)


def graph_name(replica: v1alpha1.ModelReplica) -> str:
    """Leave room for the frontend Service suffix."""
    return base.dns_1035_label(f"{base._name(replica.metadata)}-{base.dynamo_graph_revision(replica)}", "dgd", 52)


def component_name(engine: v1alpha1.Engine) -> str:
    """Worker names cannot collide with the graph's frontend component."""
    return base.dns_1035_label(f"engine-{engine.name}", "engine", 16)


def _runtime_version(member: v1alpha1.Member) -> dict[str, str]:
    image = base.engine_container(member).image.split("@")[0]
    if image.endswith(f"-runtime-nightly:{NIGHTLY_TAG}"):
        return {"runtimeVersionOverride": NIGHTLY_RUNTIME_VERSION}
    return {}


def _runtime_ports(container: dict) -> None:
    """An explicit port list replaces operator defaults, so retain its runtime ports."""
    env = {entry["name"]: entry for entry in container.get("env", [])}
    defaults = [("system", "DYN_SYSTEM_PORT", 9090), ("nixl", "NIXL_TELEMETRY_PROMETHEUS_PORT", 19090)]
    if not container.get("ports") and not any(variable in env for _, variable, _ in defaults):
        return
    ports = container.setdefault("ports", [])
    for name, variable, default in defaults:
        declared = next((port for port in ports if port.get("name") == name), None)
        configured = env.get(variable)
        if declared is not None:
            if declared.get("protocol", "TCP") != "TCP":
                raise ValueError(f"Dynamo runtime port {name} must use TCP")
            if configured is None:
                container.setdefault("env", []).append({"name": variable, "value": str(declared["containerPort"])})
            elif "value" in configured and configured["value"] != str(declared["containerPort"]):
                raise ValueError(f"Dynamo runtime port {name} must match {variable}")
            continue
        if configured is not None and "valueFrom" in configured:
            raise ValueError(f"Declare the named {name} port when {variable} uses valueFrom")
        try:
            port = int(configured["value"]) if configured else default
        except (ValueError, KeyError) as err:
            raise ValueError(f"{variable} must be an integer port") from err
        if not 1 <= port <= _MAX_PORT:
            raise ValueError(f"{variable} must be between 1 and 65535")
        ports.append({"name": name, "containerPort": port})


def validate(replica: v1alpha1.ModelReplica) -> str | None:
    """Reject graph shapes that would lose user configuration or be rejected by DGD admission."""
    frameworks = set()
    for engine in replica.spec.engines:
        for member in engine.members:
            try:
                frameworks.add(base.dynamo_framework(member))
                _runtime_ports(base.engine_container(member).model_dump(exclude_none=True))
            except ValueError as err:
                return str(err)
            container = base.engine_container(member)
            if not container.command and not container.args:
                return f"Dynamo member {engine.name}/{base.member_role(member)} requires a command or runtime args"
            if any(c.name == "main" for c in member.template.spec.containers):
                return "Dynamo reserves container name main for the engine; rename the sidecar named main"
    if len(frameworks) != 1:
        return "All engines in a Dynamo graph must use the same backend framework"
    if replica.spec.serving and replica.spec.serving.router:
        return "Dynamo uses its runtime frontend for routing; omit serving.router and serving.endpointPicker"
    return None


def _pod_template(replica: v1alpha1.ModelReplica, engine: v1alpha1.Engine, member: v1alpha1.Member) -> dict:
    container = base.engine_container(member).model_dump(exclude_none=True)
    container["name"] = "main"
    _runtime_ports(container)
    if not container.get("command"):
        container["command"] = ["python3", "-m", f"dynamo.{base.dynamo_framework(member)}"]
    resources = base.engine_resources(base.engine_container(member), claim=bool(member.deviceRequests))
    if resources:
        container["resources"] = resources
    volumes, mounts = base.cache_mounts(replica)
    container["volumeMounts"] = mounts
    env = [*base.cache_env(replica), *base.modelexpress_env(replica, "Dynamo")]
    if member.role != base.ROLE_STANDALONE:
        env[0:0] = [
            {"name": base.LEADER_ADDRESS_ENV, "value": "$(DYNAMO_LEADER_ADDRESS)"},
            {"name": base.RANK_ENV, "value": "$(DYNAMO_RANK)"},
        ]
    container["env"] = [*env, *container.get("env", [])]
    if security := base.modelexpress_security_context(replica, "Dynamo"):
        container["securityContext"] = security
    spec = {
        "containers": [container, *base.sidecar_containers(member)],
        "volumes": volumes,
        "schedulerName": base.GROVE_SCHEDULER_NAME,
    }
    base.place_pod(spec, replica, engine, member)
    if secrets := member.template.spec.imagePullSecrets:
        spec["imagePullSecrets"] = [s.model_dump(exclude_none=True) for s in secrets]
    return {
        "metadata": base.pod_metadata(
            member, {base.GROVE_QUEUE_LABEL: base.GROVE_QUEUE}, replica=replica, engine=engine
        ),
        "spec": spec,
    }


def build(replica: v1alpha1.ModelReplica, provider_config: str) -> dict[str, k8sobjv1alpha1.Object]:
    assert replica.metadata is not None
    first = replica.spec.engines[0]
    first_member = base.engine_member(first, base.ROLE_LEADER) or first.members[0]
    runtime = base.engine_container(first_member)
    volumes, mounts = base.cache_mounts(replica)
    frontend = {
        "name": FRONTEND,
        "type": "frontend",
        **_runtime_version(first_member),
        "replicas": 1,
        "podTemplate": {
            "metadata": {
                "labels": {
                    base.LABEL_SERVING: base.serving_label(replica),
                    base.LABEL_REPLICA: base._name(replica.metadata),
                    base.LABEL_DEPLOYMENT: (replica.metadata.labels or {}).get(
                        base.LABEL_DEPLOYMENT, base._name(replica.metadata)
                    ),
                }
            },
            "spec": {
                "containers": [
                    {
                        "name": "main",
                        "image": runtime.image,
                        "command": ["python3", "-m", "dynamo.frontend"],
                        "args": ["--http-port", "8000", "--router-mode", "kv"],
                        "env": [
                            *base.cache_env(replica),
                            *[e.model_dump(exclude_none=True) for e in runtime.env or []],
                        ],
                        "volumeMounts": mounts,
                    }
                ],
                "volumes": volumes,
            },
        },
    }
    if secrets := first_member.template.spec.imagePullSecrets:
        frontend["podTemplate"]["spec"]["imagePullSecrets"] = [s.model_dump(exclude_none=True) for s in secrets]
    components = [frontend]
    composed = {}
    for engine in replica.spec.engines:
        component = {
            "name": component_name(engine),
            "type": (engine.phase or "worker").lower(),
            **_runtime_version(engine.members[0]),
            "replicas": int(engine.copies or 1),
        }
        if standalone := base.engine_member(engine, base.ROLE_STANDALONE):
            component["podTemplate"] = _pod_template(replica, engine, standalone)
        else:
            worker = base.engine_member(engine, base.ROLE_WORKER)
            assert worker is not None
            nodes = int(worker.worker.nodes) if worker.worker else 1
            component["multinode"] = {"nodeCount": nodes + 1}
            component["roles"] = [
                {
                    "name": base.member_role(m),
                    "replicas": nodes if m.role == base.ROLE_WORKER else 1,
                    "podTemplate": _pod_template(replica, engine, m),
                }
                for m in engine.members
            ]
        components.append(component)
        for member in engine.members:
            if member.deviceRequests:
                composed[base.claim_key(engine, member)] = base.resource_claim_template(
                    replica, engine, member, provider_config
                )
    composed[base.dynamo_workload_key(replica)] = base.wrap_object(
        provider_config,
        {
            "apiVersion": "nvidia.com/v1beta1",
            "kind": "DynamoGraphDeployment",
            "metadata": {"name": graph_name(replica), "namespace": base.remote_namespace(replica)},
            "spec": {"backendFramework": base.dynamo_framework(first_member), "components": components},
        },
        cel_query=READY_CEL,
        watch=True,
    )
    return composed


def ready_copies(replica: v1alpha1.ModelReplica, engine: v1alpha1.Engine, observed: Mapping[str, fnv1.Resource]) -> int:
    """Use component counters, which count gang copies rather than individual pods."""
    status = base.observed_status(observed, base.dynamo_workload_key(replica)) or {}
    component = status.get("components", {}).get(component_name(engine), {})
    return int(component.get("availableReplicas", component.get("readyReplicas")) or 0)


def frontend_ready(replica: v1alpha1.ModelReplica, observed: Mapping[str, fnv1.Resource]) -> bool:
    status = base.observed_status(observed, base.dynamo_workload_key(replica)) or {}
    component = status.get("components", {}).get(FRONTEND, {})
    return int(component.get("readyReplicas", component.get("availableReplicas")) or 0) > 0
