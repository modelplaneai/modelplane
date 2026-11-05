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

Native engines run in main, with user-authored runtime init sidecars. Member
templates retain their launches, probes, placement, and device claims. The user
also supplies the shared frontend; Modelplane does not infer engine configuration.
"""

from collections.abc import Mapping

from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from models.ai.modelplane.modelreplica import v1alpha1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1

from function.backends import base

FRONTEND = "frontend"
_MAX_PORT = 65535
READY_CEL = (
    "has(object.status) && has(object.status.observedGeneration) && "
    "object.status.observedGeneration == object.metadata.generation && "
    "has(object.status.conditions) && "
    'object.status.conditions.exists(c, c.type == "Ready" && c.status == "True")'
)


def graph_name(replica: v1alpha1.ModelReplica) -> str:
    """Leave room for a hyphen and the longest 16-character component name."""
    return base.dns_1035_label(f"{base._name(replica.metadata)}-{base.dynamo_graph_revision(replica)}", "dgd", 46)


def component_name(engine: v1alpha1.Engine) -> str:
    """Worker names cannot collide with the graph's frontend component."""
    return base.dns_1035_label(f"engine-{engine.name}", "engine", 16)


def _runtime_port_number(name: str, variable: str, configured: dict | None, default: int) -> int:
    """Resolve a static runtime port, requiring a named port for dynamic configuration."""
    if configured is not None and "valueFrom" in configured:
        raise ValueError(f"Declare the named {name} port when {variable} uses valueFrom")
    try:
        port = int(configured["value"]) if configured else default
    except (ValueError, KeyError) as err:
        raise ValueError(f"{variable} must be an integer port") from err
    if not 1 <= port <= _MAX_PORT:
        raise ValueError(f"{variable} must be between 1 and 65535")
    return port


def _runtime_ports(container: dict) -> None:
    """An explicit port list replaces operator defaults, so retain its runtime ports."""
    env = {entry["name"]: entry for entry in container.get("env", [])}
    defaults = [("system", "DYN_SYSTEM_PORT", 9090)]
    if "ports" not in container and not any(variable in env for _, variable, _ in defaults):
        return
    ports = container.setdefault("ports", [])
    if len({(p["containerPort"], p.get("protocol", "TCP")) for p in ports}) != len(ports):
        raise ValueError("Dynamo runtime container ports must have unique port numbers per protocol")
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
        port = _runtime_port_number(name, variable, configured, default)
        existing = next((p for p in ports if p["containerPort"] == port and p.get("protocol", "TCP") == "TCP"), None)
        if existing is None:
            ports.append({"name": name, "containerPort": port})
        elif existing.get("name"):
            raise ValueError(f"Dynamo {name} port {port} conflicts with named port {existing['name']}")
        else:
            existing["name"] = name


def _validate_member(engine: v1alpha1.Engine, member: v1alpha1.Member) -> None:
    """Protect the native-sidecar pod contract, leaving engine launch semantics to its author."""
    if member.template.metadata and "dynamo.nvidia.com/gpu-power-limit" in (member.template.metadata.annotations or {}):
        raise ValueError("Dynamo gpu-power-limit annotations are incompatible with Modelplane DRA claims")
    init = base.init_containers(member)
    if any(c.name == "main" for c in member.template.spec.containers) or any(c["name"] == "main" for c in init):
        raise ValueError("Dynamo reserves container name main for the engine; rename the sidecar named main")
    sidecar = next((c for c in init if c["name"] == "runtime"), None)
    if member.role != base.ROLE_WORKER and sidecar is None:
        raise ValueError(f"Dynamo member {engine.name}/{base.member_role(member)} requires a runtime init sidecar")
    if sidecar is not None:
        if sidecar.get("restartPolicy") != "Always":
            raise ValueError("The Dynamo runtime init sidecar requires restartPolicy: Always")
        _runtime_ports(sidecar)
    container = base.engine_container(member)
    if member.role != base.ROLE_WORKER and container.readinessProbe is None:
        raise ValueError("Dynamo serving engines require their own readinessProbe")
    for entry in container.env or []:
        if entry.value and any(f"$({name})" in entry.value for name in (base.LEADER_ADDRESS_ENV, base.RANK_ENV)):
            raise ValueError("Use Dynamo topology aliases in command or args, not dependent env values")


def validate(replica: v1alpha1.ModelReplica) -> str | None:
    """Validate the operator's sidecar contract without interpreting engine launches."""
    serving = replica.spec.serving
    if not serving or serving.router != "Dynamo" or serving.dynamo is None:
        return "Dynamo clusters require serving.router: Dynamo and serving.dynamo.frontend"
    frontend = next((c for c in serving.dynamo.frontend.template.spec.containers if c.name == "main"), None)
    if frontend is None or not frontend.command:
        return "The Dynamo frontend requires a main container with an explicit command"
    if frontend.ports is not None and not any(p.name == "http" for p in frontend.ports):
        return "Explicit Dynamo frontend ports must declare the named http port targeted by its Service"
    try:
        for engine in replica.spec.engines:
            for member in engine.members:
                _validate_member(engine, member)
    except ValueError as err:
        return str(err)
    return None


def _pod_template(replica: v1alpha1.ModelReplica, engine: v1alpha1.Engine, member: v1alpha1.Member) -> dict:
    container = base.engine_container(member).model_dump(exclude_none=True)
    container["name"] = "main"
    resources = base.engine_resources(base.engine_container(member), claim=bool(member.deviceRequests))
    if resources:
        container["resources"] = resources
    volumes, mounts = base.cache_mounts(replica)
    container["volumeMounts"] = [{"name": "dshm", "mountPath": "/dev/shm"}, *mounts]
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
        "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}, *volumes],
        "schedulerName": base.GROVE_SCHEDULER_NAME,
    }
    if init := base.init_containers(member):
        for sidecar in init:
            if sidecar["name"] == "runtime":
                _runtime_ports(sidecar)
        spec["initContainers"] = init
    base.place_pod(spec, replica, engine, member)
    if secrets := member.template.spec.imagePullSecrets:
        spec["imagePullSecrets"] = [s.model_dump(exclude_none=True) for s in secrets]
    return {
        "metadata": base.pod_metadata(member, replica=replica, engine=engine),
        "spec": spec,
    }


def build(replica: v1alpha1.ModelReplica, provider_config: str) -> dict[str, k8sobjv1alpha1.Object]:
    assert replica.metadata is not None
    assert replica.spec.serving is not None and replica.spec.serving.dynamo is not None
    config = replica.spec.serving.dynamo
    version = {"runtimeVersionOverride": config.runtimeVersionOverride} if config.runtimeVersionOverride else {}
    frontend_template = config.frontend.template.model_dump(exclude_none=True)
    labels = frontend_template.setdefault("metadata", {}).setdefault("labels", {})
    labels.update(base.fleet_labels(replica, "frontend"))
    volumes, mounts = base.cache_mounts(replica)
    if volumes:
        frontend_template["spec"]["volumes"] = volumes
    for container in frontend_template["spec"]["containers"]:
        if container["name"] == "main":
            if mounts:
                container["volumeMounts"] = mounts
            container["env"] = [*base.cache_env(replica), *container.get("env", [])]
    frontend = {
        "name": FRONTEND,
        "type": "frontend",
        **version,
        "replicas": int(config.frontend.replicas or 1),
        "podTemplate": frontend_template,
    }
    components = [frontend]
    composed = {}
    for engine in replica.spec.engines:
        component = {
            "name": component_name(engine),
            "type": (engine.phase or "worker").lower(),
            **version,
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
            "metadata": {
                "name": graph_name(replica),
                "namespace": base.remote_namespace(replica),
                # The pinned operator selects Grove's strategy through this annotation.
                "annotations": {
                    "nvidia.com/grove-update-strategy": "Coherent",
                    "nvidia.com/kai-scheduler-queue": base.GROVE_QUEUE,
                },
            },
            "spec": {
                **({"backendFramework": config.backendFramework} if config.backendFramework else {}),
                "components": components,
            },
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
